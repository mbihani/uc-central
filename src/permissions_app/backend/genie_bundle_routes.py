"""HTTP endpoints for Genie access bundles (see backend/genie_bundles.py).

All routes are admin-gated (forwarded user must be in ``admins``); workspace
reads/writes run as the app service principal, exactly like the rest of the
app. Errors that an operator must act on — group is workspace-local, the SP
lacks MANAGE on a catalog/schema/table, a membership change was refused — are
returned with their specific message rather than the generic SDK 502.
"""

from typing import Annotated

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from .._metadata import api_prefix
from .dependencies import AdminRequired, CurrentUserDep, SessionDep, get_obo_ws
from .genie_bundles import (
    BundleBlocked,
    apply_plan,
    build_plan,
    check_group,
    delete_bundle,
    is_unique_violation,
    resolve_bundle_group,
    to_preview,
)
from .logger import logger
from .models import (
    GENIE_BUNDLE_SPACE_UNIQUE,
    AddMemberIn,
    GenieAccessBundle,
    GenieBundleActionOut,
    GenieBundleIn,
    GenieBundleOut,
    GenieBundlePreviewOut,
    GenieGrantLedger,
    GroupMemberOut,
    _utcnow,
)
from .router import _set_group_membership

genie_bundles_api = APIRouter(prefix=api_prefix)


def _bundle_out(session: Session, b: GenieAccessBundle) -> GenieBundleOut:
    count = session.exec(
        select(func.count()).select_from(GenieGrantLedger).where(GenieGrantLedger.bundle_id == b.id)
    ).one()
    return GenieBundleOut(
        id=b.id or 0,
        space_id=b.space_id,
        space_title=b.space_title,
        group_name=b.group_name,
        group_id=b.group_id,
        created_by=b.created_by,
        created_at=b.created_at,
        last_applied_at=b.last_applied_at,
        last_applied_by=b.last_applied_by,
        restriction_mode=b.restriction_mode,
        ledger_count=int(count or 0),
    )


def _one_per_space(space_id: str, group_name: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            f"Genie space '{space_id}' already has a bundle (group '{group_name}'). "
            f"Each space has exactly one bundle group; add people to that group."
        ),
    )


def _verified_bundle(
    obo_ws: WorkspaceClient, session: Session, bundle_id: int
) -> GenieAccessBundle:
    """Load the bundle and re-verify its group BY ID (renames are picked up;
    a deleted / non-account group blocks the request with a 400)."""
    bundle = _get_bundle(session, bundle_id)
    group = resolve_bundle_group(obo_ws, session, bundle)
    if group.error:
        raise HTTPException(status_code=400, detail=group.error)
    return bundle


def _get_bundle(session: Session, bundle_id: int) -> GenieAccessBundle:
    b = session.get(GenieAccessBundle, bundle_id)
    if b is None:
        raise HTTPException(status_code=404, detail=f"Genie access bundle {bundle_id} not found")
    return b


@genie_bundles_api.get(
    "/genie-bundles", response_model=list[GenieBundleOut], operation_id="listGenieBundles"
)
def list_genie_bundles(session: SessionDep, _admin: AdminRequired = True):
    bundles = session.exec(select(GenieAccessBundle)).all()
    return [_bundle_out(session, b) for b in sorted(bundles, key=lambda b: b.id or 0)]


@genie_bundles_api.post(
    "/genie-bundles", response_model=GenieBundleOut, operation_id="createGenieBundle"
)
def create_genie_bundle(
    body: GenieBundleIn,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    user: CurrentUserDep,
    _admin: AdminRequired = True,
):
    """Create a bundle for an EXISTING account group. Writes nothing to the
    workspace — run preview, then apply."""
    space_id = body.space_id.strip()
    group_name = body.group_name.strip()
    if not space_id or not group_name:
        raise HTTPException(status_code=400, detail="space_id and group_name are required.")

    existing = session.exec(
        select(GenieAccessBundle).where(GenieAccessBundle.space_id == space_id)
    ).first()
    if existing is not None:
        raise _one_per_space(space_id, existing.group_name)

    group = check_group(obo_ws, group_name)
    if group.error:
        raise HTTPException(status_code=400, detail=group.error)

    try:
        space = obo_ws.genie.get_space(space_id)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"Genie space '{space_id}' was not found.")
    except PermissionDenied:
        raise HTTPException(
            status_code=403,
            detail=(
                f"The app service principal cannot read Genie space '{space_id}'; "
                f"grant it CAN_MANAGE on the space."
            ),
        )

    bundle = GenieAccessBundle(
        space_id=space_id,
        space_title=space.title or space_id,
        group_name=group_name,
        group_id=group.group_id or "",
        created_by=user.user_name or "",
    )
    session.add(bundle)
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        if not is_unique_violation(e, GENIE_BUNDLE_SPACE_UNIQUE, GenieAccessBundle.__tablename__):
            raise
        raise _one_per_space(space_id, group_name)  # lost a concurrent create
    session.refresh(bundle)
    return _bundle_out(session, bundle)


@genie_bundles_api.get(
    "/genie-bundles/{bundle_id}/preview",
    response_model=GenieBundlePreviewOut,
    operation_id="previewGenieBundle",
)
def preview_genie_bundle(
    bundle_id: int,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """Read-only plan: tables, required grants, what already exists, row
    filter / mask status, group validity and blockers. No writes."""
    bundle = _get_bundle(session, bundle_id)
    return to_preview(bundle, build_plan(obo_ws, session, bundle))


def _run(
    bundle_id: int,
    obo_ws: WorkspaceClient,
    session: Session,
    actor: str,
    *,
    revoke_stale: bool,
) -> GenieBundleActionOut:
    bundle = _get_bundle(session, bundle_id)
    plan = build_plan(obo_ws, session, bundle)
    if plan.blockers:
        raise HTTPException(status_code=400, detail=" ".join(plan.blockers))
    result = apply_plan(obo_ws, session, bundle, plan, actor, revoke_stale=revoke_stale)
    bundle.space_title = plan.space_title or bundle.space_title
    bundle.last_applied_at = _utcnow()
    bundle.last_applied_by = actor
    session.add(bundle)
    session.commit()
    return result


@genie_bundles_api.post(
    "/genie-bundles/{bundle_id}/apply",
    response_model=GenieBundleActionOut,
    operation_id="applyGenieBundle",
)
def apply_genie_bundle(
    bundle_id: int,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    user: CurrentUserDep,
    _admin: AdminRequired = True,
):
    """Grant CAN_RUN on the space and the missing UC privileges (idempotent).
    Per-securable failures (e.g. the SP lacks MANAGE) are listed in ``errors``
    with ``status`` = partial/failed."""
    return _run(bundle_id, obo_ws, session, user.user_name or "", revoke_stale=False)


@genie_bundles_api.post(
    "/genie-bundles/{bundle_id}/resync",
    response_model=GenieBundleActionOut,
    operation_id="resyncGenieBundle",
)
def resync_genie_bundle(
    bundle_id: int,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    user: CurrentUserDep,
    _admin: AdminRequired = True,
):
    """Re-read the space, grant newly added tables, and release ledger rows the
    space no longer needs (revoking only app-granted privileges that no other
    bundle relies on)."""
    return _run(bundle_id, obo_ws, session, user.user_name or "", revoke_stale=True)


@genie_bundles_api.delete(
    "/genie-bundles/{bundle_id}",
    response_model=GenieBundleActionOut,
    operation_id="deleteGenieBundle",
)
def delete_genie_bundle(
    bundle_id: int,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    forget_orphaned: bool = False,
    _admin: AdminRequired = True,
):
    """Release every ledger row with the safe-revoke rule, then delete the
    bundle. The account group itself is never deleted.

    ``forget_orphaned=true`` is accepted only when the bundle's group id no
    longer exists: the records are dropped without any workspace call."""
    try:
        return delete_bundle(
            obo_ws, session, _get_bundle(session, bundle_id), forget_orphaned=forget_orphaned
        )
    except BundleBlocked as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── Membership (account group, via the workspace SCIM API) ─────────────────


def _membership_error(e: Exception, group_name: str, action: str) -> HTTPException:
    if isinstance(e, HTTPException):
        return e
    logger.error(f"Could not {action} account group {group_name}: {e}")
    if isinstance(e, PermissionDenied):
        return HTTPException(
            status_code=403,
            detail=(
                f"The workspace refused to {action} account group '{group_name}'. "
                f"Account-group membership may have to be managed in the account "
                f"console (or the app service principal must be a manager of the "
                f"group)."
            ),
        )
    return HTTPException(
        status_code=502,
        detail=(
            f"Could not {action} account group '{group_name}' ({type(e).__name__}). "
            f"Manage membership in the account console if this persists."
        ),
    )


@genie_bundles_api.get(
    "/genie-bundles/{bundle_id}/members",
    response_model=list[GroupMemberOut],
    operation_id="listGenieBundleMembers",
)
def list_genie_bundle_members(
    bundle_id: int,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    _admin: AdminRequired = True,
):
    bundle = _verified_bundle(obo_ws, session, bundle_id)
    try:
        g = obo_ws.groups.get(id=bundle.group_id)
    except Exception as e:
        raise _membership_error(e, bundle.group_name, "read members of")
    return [
        GroupMemberOut(user_id=m.value or "", display_name=m.display or None)
        for m in (g.members or [])
    ]


@genie_bundles_api.post(
    "/genie-bundles/{bundle_id}/members",
    response_model=dict,
    operation_id="addGenieBundleMember",
)
def add_genie_bundle_member(
    bundle_id: int,
    body: AddMemberIn,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """Onboard: add a user to the bundle's account group (idempotent)."""
    bundle = _verified_bundle(obo_ws, session, bundle_id)
    try:
        changed = _set_group_membership(obo_ws, bundle.group_id, body.user_id, add=True)
    except Exception as e:
        raise _membership_error(e, bundle.group_name, "add a member to")
    return {"status": "ok", "changed": changed}


@genie_bundles_api.delete(
    "/genie-bundles/{bundle_id}/members/{user_id}",
    response_model=dict,
    operation_id="removeGenieBundleMember",
)
def remove_genie_bundle_member(
    bundle_id: int,
    user_id: str,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """Offboard: remove a user from the bundle's account group (idempotent)."""
    bundle = _verified_bundle(obo_ws, session, bundle_id)
    try:
        changed = _set_group_membership(obo_ws, bundle.group_id, user_id, add=False)
    except Exception as e:
        raise _membership_error(e, bundle.group_name, "remove a member from")
    return {"status": "ok", "changed": changed}
