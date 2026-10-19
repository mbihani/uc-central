from collections.abc import Sequence
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import iam
from databricks.sdk.service.iam import User as DatabricksUserOut
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, col, select

from .._metadata import api_prefix
from .dependencies import (
    ActorDep,
    AdminRequired,
    CurrentUserDep,
    SessionDep,
    _is_workspace_admin,
    get_obo_ws,
    get_session,
)
from .logger import logger
from .models import (
    OVERRIDABLE_RESOURCE_TYPES,
    AddMemberIn,
    GenieGrantLedger,
    GroupLevelOut,
    GroupResyncOut,
    GroupResyncPreviewOut,
    PermissionOverrideIn,
    PermissionOverrideOut,
    PersonaResourceOverride,
    TableGrantPlanOut,
    ApplyAllResultOut,
    ApplyPermissionsIn,
    ApplyPlanItemOut,
    ApplyPlanSkippedOut,
    ApplyPreviewOut,
    ApplyResultOut,
    DashboardStatsOut,
    GroupMemberOut,
    GroupOut,
    IsAdminOut,
    PersonaDefinition,
    PersonaDefinitionIn,
    PersonaDefinitionOut,
    PersonaDefinitionUpdateIn,
    PersonaGroupMapping,
    PersonaGroupMappingIn,
    PersonaGroupMappingOut,
    PersonaMemberOut,
    PersonaOut,
    PersonaUserMapping,
    PersonaUserMappingIn,
    PermissionConflictDetail,
    PermissionConflictsOut,
    PermissionLevel,
    PermissionLevelOut,
    PermissionMatrixCell,
    PermissionMatrixOut,
    PermissionTemplate,
    PermissionTemplateOut,
    RESOURCE_PERMISSION_LEVELS,
    RESOURCE_TYPE_LABELS,
    ResourceItemOut,
    ResourcePermissionOut,
    ResourcePermissionsOut,
    ResourceType,
    SetPermissionIn,
    UserOut,
    UserPermissionConflict,
    VersionOut,
    get_higher_permission,
    get_permission_rank,
    is_permission_level_allowed,
)
from .genie_bundles import PERSONA_SOURCE_PREFIX, operation_locks
from .persona_sync import (
    GENIE,
    NONE,
    GroupRef,
    PersonaCells,
    execute_genie_table_grants,
    genie_level_rows,
    group_effective,
    load_cells,
    personas_by_group,
    plan_genie,
    release_stale_table_grants,
    resolve_groups,
    table_grant_rows,
)
from .resources import SUPPORTED_ACL_RESOURCE_TYPES, apply_group_acl, apply_user_acl, list_resources
from .seed import default_permission_template_rows, seed_persona_definitions
from .utils import raise_internal_error

api = APIRouter(prefix=api_prefix)


def _assert_level_allowed(resource_type: str, level: str) -> None:
    """Reject an invalid ``(resource_type, level)`` combination with a clear 400
    BEFORE any Databricks SDK call.

    Enforces the per-resource-type allow-lists (``RESOURCE_PERMISSION_LEVELS`` in
    models.py). ``NO_PERMISSIONS`` (revoke) is always accepted. An unknown
    resource type is left to the existing unsupported-type error path (see
    ``is_permission_level_allowed``), so this never masks that clearer error.
    """
    if is_permission_level_allowed(resource_type, level):
        return
    try:
        allowed = [
            pl.value for pl in RESOURCE_PERMISSION_LEVELS[ResourceType(resource_type)]
        ]
    except (ValueError, KeyError):
        allowed = []
    raise HTTPException(
        status_code=400,
        detail=(
            f"Permission level '{level}' is not valid for resource type "
            f"'{resource_type}'. Allowed levels: {', '.join(allowed)}."
        ),
    )


# ─── Version ──────────────────────────────────────────────────────────────────


@api.get("/version", response_model=VersionOut, operation_id="version")
async def version():
    return VersionOut.from_metadata()


@api.get("/current-user", response_model=DatabricksUserOut, operation_id="currentUser")
def me(user: CurrentUserDep):
    """Return the REAL proxied request user (resolved from the forwarded token),
    not the app service principal."""
    return user


@api.get("/current-user/is-admin", response_model=IsAdminOut, operation_id="checkIsAdmin")
def check_is_admin(user: CurrentUserDep):
    """Check whether the REAL proxied request user is a workspace admin."""
    return IsAdminOut(is_admin=_is_workspace_admin(user))


# ─── Dashboard Stats ──────────────────────────────────────────────────────────


@api.get(
    "/dashboard/stats",
    response_model=DashboardStatsOut,
    operation_id="getDashboardStats",
)
def get_dashboard_stats(
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """Get overview stats for the dashboard.

    Gracefully degrades when Databricks credentials are unavailable:
    SDK-dependent stats (groups, users) return 0, DB-backed stats still work.
    """
    total_groups = 0
    total_users = 0
    unassigned = 0
    users: list = []

    # Try to get SDK-dependent stats, but don't fail the whole endpoint
    try:
        # Use app service-principal creds (see get_obo_ws rationale); the forwarded
        # OBO user token is scope-restricted and cannot list groups/users.
        ws = WorkspaceClient()

        groups = list(ws.groups.list())
        total_groups = len(groups)

        users = list(ws.users.list(attributes="id,userName,displayName,groups"))
        total_users = len(users)
    except Exception as e:
        logger.warning(f"Could not fetch workspace groups/users for dashboard: {e}")
        # SDK not available — zero out SDK-dependent stats and continue

    # Count persona-mapped groups from DB (always works)
    mappings = session.exec(select(PersonaGroupMapping)).all()
    mapped_group_ids = {m.group_id for m in mappings}
    mapped_groups = len(mapped_group_ids)

    # Count personas with at least one group
    personas_with_groups = len({m.persona for m in mappings})

    # Count unassigned users (only if SDK fetched users successfully)
    if total_users > 0:
        try:
            for u in users:
                user_group_ids = set()
                if u.groups:
                    for g in u.groups:
                        if g.value:
                            user_group_ids.add(g.value)
                if not user_group_ids.intersection(mapped_group_ids):
                    unassigned += 1
        except Exception:
            pass

    return DashboardStatsOut(
        total_groups=total_groups,
        total_users=total_users,
        mapped_groups=mapped_groups,
        unassigned_users=unassigned,
        personas_with_groups=personas_with_groups,
    )


# ─── Groups (from Databricks workspace) ──────────────────────────────────────


@api.get("/groups", response_model=list[GroupOut], operation_id="listGroups")
def list_workspace_groups(
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    _admin: AdminRequired = True,
):
    """List all workspace groups with their members."""
    results = []
    for g in obo_ws.groups.list():
        members = []
        if g.members:
            for m in g.members:
                members.append(
                    GroupMemberOut(
                        user_id=m.value or "",
                        display_name=m.display or None,
                    )
                )
        results.append(
            GroupOut(
                id=g.id or "",
                display_name=g.display_name or "",
                member_count=len(members),
                members=members,
            )
        )
    return results


@api.get(
    "/groups/{group_id}",
    response_model=GroupOut,
    operation_id="getGroup",
)
def get_group(
    group_id: str,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    _admin: AdminRequired = True,
):
    """Get a single group with members."""
    g = obo_ws.groups.get(id=group_id)
    members = []
    if g.members:
        for m in g.members:
            members.append(
                GroupMemberOut(
                    user_id=m.value or "",
                    display_name=m.display or None,
                )
            )
    return GroupOut(
        id=g.id or "",
        display_name=g.display_name or "",
        member_count=len(members),
        members=members,
    )


# A group-membership principal id is a SCIM id (numeric), a UUID, or a
# username/email — never free text. Anything outside this set could break out of
# the SCIM `members[value eq "..."]` filter (injection), so it is rejected.
# NOTE: anchored with `\Z` (absolute end), NOT `$`, because Python's `$` also
# matches just before a trailing newline — so `"abc\n"` would slip through `$`
# and inject a newline into the SCIM filter. `\Z` forbids the trailing newline.
_PRINCIPAL_ID_RE = re.compile(r"^[A-Za-z0-9._@-]+\Z")


def _assert_valid_principal_id(user_id: str) -> None:
    """Reject a principal id that could inject into / break the SCIM filter.

    Raises HTTP 400 for empty ids or ids containing quotes, backslashes, spaces or
    any other filter metacharacter, so ``user_id`` can be safely interpolated into
    the SCIM ``members[value eq "..."]`` path filter.
    """
    if not user_id or not _PRINCIPAL_ID_RE.match(user_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid user id: only letters, digits and '.', '_', '-', '@' are "
                "allowed (received a value with disallowed characters)."
            ),
        )


def _set_group_membership(
    obo_ws: WorkspaceClient, group_id: str, user_id: str, *, add: bool
) -> bool:
    """Idempotently add or remove a user as a DIRECT member of a workspace group
    via SCIM, executed through the app service-principal client (``obo_ws``).

    The group is read first so the operation is a no-op when it is already in the
    desired state — adding an existing member or removing a non-member both
    return ``False`` without issuing a PATCH. Returns ``True`` when a change was
    actually pushed to the workspace.

    This is the single source of truth for group-membership mutations, shared by
    the group editor and the persona add/off-board flows.
    """
    # Defence in depth: never interpolate an unvalidated id into the SCIM filter.
    _assert_valid_principal_id(user_id)

    group = obo_ws.groups.get(id=group_id)
    is_member = any(m.value == user_id for m in (group.members or []))

    if add and not is_member:
        obo_ws.groups.patch(
            id=group_id,
            operations=[
                iam.Patch(
                    op=iam.PatchOp.ADD,
                    value={"members": [{"value": user_id}]},
                )
            ],
            schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
        )
        return True

    if not add and is_member:
        # SCIM REMOVE targets the specific member via a path filter so no other
        # members are disturbed.
        obo_ws.groups.patch(
            id=group_id,
            operations=[
                iam.Patch(
                    op=iam.PatchOp.REMOVE,
                    path=f'members[value eq "{user_id}"]',
                )
            ],
            schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
        )
        return True

    return False


@api.post(
    "/groups/{group_id}/members",
    response_model=dict,
    operation_id="addGroupMember",
)
def add_group_member(
    group_id: str,
    body: AddMemberIn,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    _admin: AdminRequired = True,
):
    """Add a user to a group (idempotent)."""
    _set_group_membership(obo_ws, group_id, body.user_id, add=True)
    return {"status": "ok", "message": f"User {body.user_id} added to group {group_id}"}


# ─── Users (from Databricks workspace) ───────────────────────────────────────


@api.get("/users", response_model=list[UserOut], operation_id="listUsers")
def list_workspace_users(
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    _admin: AdminRequired = True,
):
    """List all workspace users."""
    results = []
    for u in obo_ws.users.list(attributes="id,userName,displayName,active,groups"):
        group_names = []
        if u.groups:
            for g in u.groups:
                if g.display:
                    group_names.append(g.display)
        results.append(
            UserOut(
                id=u.id or "",
                user_name=u.user_name,
                display_name=u.display_name,
                active=u.active,
                groups=group_names,
            )
        )
    return results


@api.get(
    "/users/unassigned",
    response_model=list[UserOut],
    operation_id="listUnassignedUsers",
)
def list_unassigned_users(
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """List users who are not in any persona-mapped group."""
    # Get mapped group names
    mappings = session.exec(select(PersonaGroupMapping)).all()
    mapped_group_names = {m.group_name for m in mappings}

    results = []
    for u in obo_ws.users.list(attributes="id,userName,displayName,active,groups"):
        user_group_names = set()
        group_display_names = []
        if u.groups:
            for g in u.groups:
                if g.display:
                    user_group_names.add(g.display)
                    group_display_names.append(g.display)

        # User is unassigned if none of their groups is mapped to a persona
        if not user_group_names.intersection(mapped_group_names):
            results.append(
                UserOut(
                    id=u.id or "",
                    user_name=u.user_name,
                    display_name=u.display_name,
                    active=u.active,
                    groups=group_display_names,
                )
            )
    return results


# ─── Personas (DB-backed) ────────────────────────────────────────────────────


@contextmanager
def _persona_write(session: Session, persona: str, *, missing_status: int = 404) -> Iterator[PersonaDefinition]:
    """Every write keyed by a persona runs under its PERSONA operation lock
    (the one persona delete holds through its final commit) and re-checks
    the persona exists UNDER the lock, so nothing can be written for a persona
    that is being — or has been — deleted. Lock order persona -> group ->
    grant (see ``genie_bundles.operation_locks``)."""
    with operation_locks(session, personas=[persona]):
        defn = session.exec(
            select(PersonaDefinition)
            .where(PersonaDefinition.key == persona)
            .execution_options(populate_existing=True)
        ).first()
        if not defn:
            raise HTTPException(
                status_code=missing_status,
                detail=f"Persona '{persona}' does not exist.",
            )
        yield defn


def _insert_template_rows(session: Session, rows: list[dict]) -> None:
    """Insert matrix cells (ON CONFLICT DO NOTHING) only for personas that
    still exist, checked under their persona locks — the lazy seed / backfill
    must never (re)create cells for a deleted persona."""
    personas = sorted({r["persona"] for r in rows})
    if not personas:
        return
    with operation_locks(session, personas=personas):
        alive = set(
            session.exec(
                select(PersonaDefinition.key).where(col(PersonaDefinition.key).in_(personas))
            ).all()
        )
        keep = [r for r in rows if r["persona"] in alive]
        if keep:
            session.execute(
                pg_insert(PermissionTemplate.__table__)
                .values(keep)
                .on_conflict_do_nothing(index_elements=["persona", "resource_type"])
            )
        session.commit()


def _seed_persona_definitions(session: Session):
    """Seed default persona definitions (idempotent upsert; see backend.seed).

    Concurrency-safe: uses ``INSERT ... ON CONFLICT DO NOTHING`` on the UNIQUE
    ``persona_definition.key``, so a racing/repeated seed inserts nothing rather
    than duplicating rows or raising IntegrityError.
    """
    seed_persona_definitions(session)


@api.get(
    "/personas",
    response_model=list[PersonaOut],
    operation_id="listPersonas",
)
def list_personas(
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """List all personas with their mapped groups and their LIVE members.

    Members are NOT read from any local table. They are derived from REAL SCIM
    membership — the UNION of the members of each persona's mapped workspace
    groups, read via the SP client — so the displayed list always reflects
    reality: external/out-of-band membership changes appear automatically and
    there are never phantom (access-less) or stale members.
    """
    # Seed defaults if empty
    definitions = session.exec(select(PersonaDefinition)).all()
    if not definitions:
        _seed_persona_definitions(session)
        definitions = session.exec(select(PersonaDefinition)).all()

    mappings = session.exec(select(PersonaGroupMapping)).all()

    # Group mappings by persona key
    persona_groups: dict[str, list[PersonaGroupMappingOut]] = {}
    for m in mappings:
        persona_groups.setdefault(m.persona, []).append(
            PersonaGroupMappingOut(
                id=m.id,  # type: ignore
                group_id=m.group_id,
                group_name=m.group_name,
                persona=m.persona,
            )
        )

    # Best-effort id -> (userName, displayName) index to enrich member rows with
    # an email/username. Members not found here (e.g. service principals) fall
    # back to the display name carried by the group membership entry.
    user_index: dict[str, tuple[str | None, str | None]] = {}
    try:
        for u in obo_ws.users.list(attributes="id,userName,displayName"):
            if u.id:
                user_index[u.id] = (u.user_name, u.display_name)
    except Exception as e:
        # Enrichment ONLY (username/email). Its failure does not hide any member —
        # members still come from the group reads below — so this stays best-effort.
        logger.warning(f"Could not list users to enrich persona members: {e}")

    # Read each mapped group's live members once (a group may map to >1 persona).
    group_members_cache: dict[str, list] = {}

    def _live_members(group_id: str, group_name: str) -> list:
        if group_id not in group_members_cache:
            try:
                g = obo_ws.groups.get(id=group_id)
                group_members_cache[group_id] = list(g.members or [])
            except Exception as e:
                # NEVER swallow a membership read into an empty list — for a
                # permissions app that would HIDE real members and present a false
                # "authoritative" list. Surface it so the caller sees the failure.
                logger.error(
                    f"Could not read members of mapped group {group_name} "
                    f"({group_id}): {e}"
                )
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"Could not read live membership of mapped group "
                        f"'{group_name}' ({group_id}): {e}. Refusing to return a "
                        f"members list that may hide real members."
                    ),
                )
        return group_members_cache[group_id]

    results = []
    for defn in definitions:
        gmaps = persona_groups.get(defn.key, [])
        # Union the live members across this persona's mapped groups (dedupe by id).
        # Key is user_name when available (emails are stable), else SCIM id.
        members: dict[str, PersonaMemberOut] = {}
        for gm in gmaps:
            for m in _live_members(gm.group_id, gm.group_name):
                uid = m.value
                if not uid:
                    continue
                uname, dname = user_index.get(uid, (None, m.display))
                key = uname or uid
                if key in members:
                    if gm.group_name not in members[key].groups:
                        members[key].groups.append(gm.group_name)
                    continue
                members[key] = PersonaMemberOut(
                    user_id=uid,
                    user_name=uname,
                    display_name=dname or m.display,
                    persona=defn.key,
                    groups=[gm.group_name],
                    assignment_type="group",
                )

        # Add direct-assignment members from the PersonaUserMapping table.
        # Overlap detection: a group member whose enrichment failed is keyed by
        # SCIM numeric user_id (not user_name). Check both keys so that a user
        # who is both a group member AND directly assigned always gets "both",
        # regardless of whether user_index enrichment succeeded.
        direct_maps = session.exec(
            select(PersonaUserMapping).where(PersonaUserMapping.persona == defn.key)
        ).all()
        for dm in direct_maps:
            # Primary key: stable user_name (email). Fallback: SCIM numeric id
            # (used as dict key when enrichment failed and uname was None).
            primary_key = dm.user_name
            fallback_key = dm.user_id
            existing_key = (
                primary_key if primary_key in members
                else fallback_key if fallback_key in members
                else None
            )
            if existing_key is not None:
                # Already present via group path — mark as "both" and normalise
                # the dict key to user_name so future lookups are consistent.
                entry = members.pop(existing_key)
                entry.assignment_type = "both"
                # Ensure user_name is populated from the direct-mapping row
                # (which always has it, unlike the enrichment-failed group path).
                if not entry.user_name:
                    entry.user_name = dm.user_name
                members[primary_key] = entry
            else:
                members[primary_key] = PersonaMemberOut(
                    user_id=dm.user_id,
                    user_name=dm.user_name,
                    display_name=dm.display_name or dm.user_name,
                    persona=defn.key,
                    groups=[],
                    assignment_type="direct",
                )

        results.append(
            PersonaOut(
                persona=defn.key,
                label=defn.label,
                description=defn.description,
                is_default=defn.is_default,
                groups=gmaps,
                users=sorted(
                    members.values(),
                    key=lambda x: (x.display_name or x.user_name or x.user_id).lower(),
                ),
            )
        )
    return results


@api.post(
    "/personas",
    response_model=PersonaDefinitionOut,
    operation_id="createPersona",
)
def create_persona(body: PersonaDefinitionIn, session: SessionDep, _admin: AdminRequired = True):
    """Create a new custom persona."""
    # Validate key format (lowercase, underscores, no spaces)
    import re

    if not re.match(r"^[a-z][a-z0-9_]*$", body.key):
        raise HTTPException(
            status_code=400,
            detail="Persona key must start with a letter and contain only lowercase letters, numbers, and underscores.",
        )

    # Under the persona lock of the NEW key: never interleaves with a delete of
    # the same key (see _persona_write).
    with operation_locks(session, personas=[body.key]):
        # Check uniqueness
        existing = session.exec(
            select(PersonaDefinition).where(PersonaDefinition.key == body.key)
        ).first()
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"A persona with key '{body.key}' already exists.",
            )

        defn = PersonaDefinition(
            key=body.key,
            label=body.label,
            description=body.description,
            is_default=False,
        )
        session.add(defn)
        session.commit()
        session.refresh(defn)

        # Seed NO_PERMISSIONS for all resource types so the persona shows in the matrix
        for rt in ResourceType:
            session.add(
                PermissionTemplate(
                    persona=defn.key,
                    resource_type=rt.value,
                    permission_level=PermissionLevel.NO_PERMISSIONS.value,
                )
            )
        session.commit()

        return PersonaDefinitionOut(
            id=defn.id,  # type: ignore
            key=defn.key,
            label=defn.label,
            description=defn.description,
            is_default=defn.is_default,
        )


@api.put(
    "/personas/{persona_key}",
    response_model=PersonaDefinitionOut,
    operation_id="updatePersona",
)
def update_persona(persona_key: str, body: PersonaDefinitionUpdateIn, session: SessionDep, _admin: AdminRequired = True):
    """Update a persona's label and/or description."""
    with _persona_write(session, persona_key) as defn:
        return _update_persona_locked(session, defn, body)


def _update_persona_locked(
    session: Session, defn: PersonaDefinition, body: PersonaDefinitionUpdateIn
) -> PersonaDefinitionOut:
    if body.label is not None:
        defn.label = body.label
    if body.description is not None:
        defn.description = body.description

    session.add(defn)
    session.commit()
    session.refresh(defn)

    return PersonaDefinitionOut(
        id=defn.id,  # type: ignore
        key=defn.key,
        label=defn.label,
        description=defn.description,
        is_default=defn.is_default,
    )


@api.delete(
    "/personas/{persona_key}",
    response_model=dict,
    operation_id="deletePersona",
)
def delete_persona(
    persona_key: str,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    actor: ActorDep = "",
    _admin: AdminRequired = True,
):
    """Delete a persona and all its associated mappings and permission templates.

    Every group the persona is mapped to is first RECOMPUTED without it, exactly
    like removing the mapping (see ``_remove_group_persona``). A mapping is
    deleted only once its group's cleanup succeeded; if any group's cleanup
    fails, that mapping AND the persona (templates, overrides, user mappings)
    are kept and the errors are returned, so the delete can simply be retried.
    """
    if not session.exec(
        select(PersonaDefinition).where(PersonaDefinition.key == persona_key)
    ).first():
        raise HTTPException(status_code=404, detail="Persona not found")

    def mapped():
        return session.exec(
            select(PersonaGroupMapping).where(PersonaGroupMapping.persona == persona_key)
        ).all()

    # Lock order: persona -> its groups -> grants (see operation_locks). The
    # PERSONA lock is held through the final commit, and mapping creation takes
    # it too, so no mapping can appear for this persona once it is checked.
    with operation_locks(session, personas=[persona_key]):
        defn = session.exec(
            select(PersonaDefinition)
            .where(PersonaDefinition.key == persona_key)
            .execution_options(populate_existing=True)
        ).first()
        if not defn:
            raise HTTPException(status_code=404, detail="Persona not found")
        gids = {m.group_id for m in mapped()}
        with operation_locks(session, group_ids=gids):
            mappings = [m for m in mapped() if m.group_id in gids]
            refs = resolve_groups(obo_ws, mappings)
            resynced = [
                _remove_group_persona(obo_ws, session, m, refs[m.group_id], actor)
                for m in mappings
            ]
        if not all(r.mapping_removed for r in resynced) or mapped():
            # A failed cleanup keeps its mapping; ``mapped()`` is also a
            # belt-and-braces check (creation is excluded by the persona lock).
            return {
                "status": "incomplete",
                "persona_deleted": False,
                "message": (
                    f"Persona '{persona_key}' was NOT deleted: cleanup failed for "
                    f"{sum(not r.mapping_removed for r in resynced)} group(s); their "
                    f"mappings were kept. Fix the errors and retry."
                ),
                "groups_recomputed": [r.model_dump() for r in resynced],
            }

        _before_persona_delete_commit()
        for um in session.exec(
            select(PersonaUserMapping).where(PersonaUserMapping.persona == persona_key)
        ).all():
            session.delete(um)
        for t in session.exec(
            select(PermissionTemplate).where(PermissionTemplate.persona == persona_key)
        ).all():
            session.delete(t)
        for o in session.exec(
            select(PersonaResourceOverride).where(PersonaResourceOverride.persona == persona_key)
        ).all():
            session.delete(o)
        session.delete(defn)
        session.commit()

    return {
        "status": "ok",
        "persona_deleted": True,
        "message": f"Persona '{persona_key}' deleted",
        "groups_recomputed": [r.model_dump() for r in resynced],
    }


def _before_persona_delete_commit() -> None:
    """No-op seam: persona delete has verified that no mapping is left and is
    about to delete the persona (tests pause here)."""


@api.post(
    "/personas/mappings",
    response_model=PersonaGroupMappingOut,
    operation_id="createPersonaMapping",
)
def create_persona_mapping(
    body: PersonaGroupMappingIn,
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """Map a Databricks group to a persona.

    Runs under the PERSONA lock (shared with persona delete) and re-checks the
    persona under it, so a mapping can never be created for a persona whose
    deletion is in progress or done.
    """
    with operation_locks(session, personas=[body.persona]):
        persona_def = session.exec(
            select(PersonaDefinition)
            .where(PersonaDefinition.key == body.persona)
            .execution_options(populate_existing=True)
        ).first()
        if not persona_def:
            raise HTTPException(
                status_code=400,
                detail=f"Persona '{body.persona}' does not exist.",
            )

        # Personas stack: a group may map to several personas, each once.
        existing = session.exec(
            select(PersonaGroupMapping).where(
                PersonaGroupMapping.group_id == body.group_id,
                PersonaGroupMapping.persona == body.persona,
            )
        ).first()
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Group {body.group_name} is already mapped to persona {existing.persona}",
            )

        mapping = PersonaGroupMapping(
            group_id=body.group_id,
            group_name=body.group_name,
            persona=body.persona,
        )
        session.add(mapping)
        session.commit()
        session.refresh(mapping)
        return PersonaGroupMappingOut(
            id=mapping.id,  # type: ignore
            group_id=mapping.group_id,
            group_name=mapping.group_name,
            persona=mapping.persona,
        )


@api.delete(
    "/personas/mappings/{mapping_id}",
    response_model=GroupResyncOut,
    operation_id="deletePersonaMapping",
)
def delete_persona_mapping(
    mapping_id: int,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    actor: ActorDep = "",
    _admin: AdminRequired = True,
):
    """Remove a persona from a group: RECOMPUTE the group's access without that
    persona (from its remaining personas and applied bundles) and only THEN
    delete the mapping.

    If any part of the cleanup fails (an ACL write, a UC revoke, a space whose
    grants could not be released), the mapping is KEPT, ``mapping_removed`` is
    false and the errors are returned: calling this endpoint again retries the
    cleanup. Nothing is orphaned, even for a group with no other persona.
    """
    mapping = session.get(PersonaGroupMapping, mapping_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")
    with operation_locks(session, group_ids=[mapping.group_id]):
        # Re-read under the lock (a concurrent removal may have finished).
        mapping = session.get(PersonaGroupMapping, mapping_id, populate_existing=True)
        if not mapping:
            raise HTTPException(status_code=404, detail="Mapping not found")
        group_ref = resolve_groups(obo_ws, [mapping])[mapping.group_id]
        return _remove_group_persona(obo_ws, session, mapping, group_ref, actor)


def _acl_types() -> list[str]:
    """Every resource type the app can enumerate and write ACLs for."""
    return [rt.value for rt in ResourceType if rt.value in SUPPORTED_ACL_RESOURCE_TYPES]


def _persona_ledger_rows(session: Session, group_id: str, persona: str) -> list[GenieGrantLedger]:
    prefix = f"{PERSONA_SOURCE_PREFIX}{persona}:space:"
    return list(
        session.exec(
            select(GenieGrantLedger).where(
                GenieGrantLedger.group_id == group_id,
                col(GenieGrantLedger.source).startswith(prefix),
            )
        ).all()
    )


def _remove_group_persona(
    obo_ws: WorkspaceClient,
    session: Session,
    mapping: PersonaGroupMapping,
    group_ref: GroupRef,
    actor: str,
) -> GroupResyncOut:
    """Recompute ``mapping``'s group WITHOUT its persona, then delete the mapping
    only if the cleanup fully succeeded.

    The scope does not depend on what the persona grants NOW (its cells may
    have been lowered without an Apply): EVERY supported ACL type is rewritten
    to what the remaining personas / bundles grant, and Genie always runs, so
    table grants the persona sourced in the past are released (revoked only if
    the app made them and no other persona / bundle still needs them).
    Success = no write / revoke errors, every Genie entry recomputed (a
    BLOCKED entry — unreadable space, group not confirmed as an account group —
    is an error: its ACL was left untouched), AND no ledger row of this
    persona left for the group. The caller holds the group operation lock.

    A group CONFIRMED deleted (by-id lookup: not found) has no principal left
    to revoke from: this persona's ledger rows for it are forgotten with no
    workspace call (like a bundle's ``forget_orphaned``), then the mapping is
    deleted. A transient lookup error keeps everything for a retry.
    """
    gid, removed = mapping.group_id, mapping.persona
    out = GroupResyncOut(group_id=gid, group_name=group_ref.name, removed_persona=removed)
    if group_ref.missing:
        forgotten = _persona_ledger_rows(session, gid, removed)
        for r in forgotten:
            session.delete(r)
        out.warnings.append(
            f"Group id {gid} no longer exists (confirmed); mapping removed and "
            f"{len(forgotten)} ledger row(s) of persona '{removed}' forgotten without "
            f"any workspace call"
            + (
                f": {', '.join(sorted(f'{r.privilege} on {r.full_name}' for r in forgotten))}."
                if forgotten
                else "."
            )
        )
    elif group_ref.lookup_error:
        out.errors.append(f"{group_ref.lookup_error} Mapping kept; retry the removal.")
    else:
        exclude = frozenset({(gid, removed)})
        groups = {gid: group_ref}
        personas_of = personas_by_group(session, groups, exclude=exclude)
        cells = load_cells(session, personas_of[gid])
        for rt in _acl_types():
            if rt == GENIE:
                outcome = _apply_genie(obo_ws, session, groups, actor, exclude=exclude)
                out.resources_updated += outcome.updated
                out.table_grants_added += outcome.added
                out.table_grants_revoked += outcome.revoked
                out.errors.extend(outcome.errors)
                # Blocked = the group's ACL entry on that space was NOT
                # recomputed (it may still carry this persona's access): the
                # mapping must stay until it can be.
                out.errors.extend(
                    f"{b} Mapping kept until this space can be recomputed; retry the removal."
                    for b in outcome.blocked
                )
                out.warnings.extend(outcome.warnings)
                continue
            updated, errors = _apply_type(obo_ws, rt, _group_levels(groups, personas_of, cells, rt))
            out.resources_updated += updated
            out.errors.extend(errors)
        left = _persona_ledger_rows(session, gid, removed)
        if left:
            out.errors.append(
                f"{len(left)} table grant(s) needed by persona '{removed}' for "
                f"'{group_ref.name}' could not be released yet (spaces "
                f"{', '.join(sorted({r.space_id for r in left}))}). Mapping kept; retry the removal."
            )
    if not out.errors:
        session.delete(mapping)
        session.commit()
        out.mapping_removed = True
    return out


@api.get(
    "/personas/mappings/{mapping_id}/removal-preview",
    response_model=GroupResyncPreviewOut,
    operation_id="previewPersonaMappingRemoval",
)
def preview_persona_mapping_removal(
    mapping_id: int,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """Read-only: what removing this persona from the group would change."""
    mapping = session.get(PersonaGroupMapping, mapping_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")
    group_ref = resolve_groups(obo_ws, [mapping])[mapping.group_id]
    out = GroupResyncPreviewOut(
        group_id=group_ref.group_id, group_name=group_ref.name, removed_persona=mapping.persona
    )
    if group_ref.missing:
        out.warnings.append(f"Group id {group_ref.group_id} no longer exists.")
        return out
    exclude = frozenset({(mapping.group_id, mapping.persona)})
    groups = {group_ref.group_id: group_ref}
    removed_cells = load_cells(session, [mapping.persona])
    for rt in _acl_types():
        levels, grants, warnings = _preview_type(obo_ws, session, groups, rt, exclude=exclude)
        # Show what changes: types the persona grants, anything still granted,
        # and every Genie row (old persona table grants are released there).
        if rt == GENIE or removed_cells.grants_anything(mapping.persona, rt) or any(
            r.effective_level != NONE for r in levels
        ):
            out.group_levels.extend(levels)
        out.table_grants.extend(grants)
        out.warnings.extend(warnings)
    out.warnings.append(
        "Removal recomputes EVERY persona-managed resource type for this group, "
        "not only the ones this persona grants: wherever no remaining persona (or "
        "Genie bundle) grants access, the group's entry is REMOVED — including "
        "entries set by hand on the Resources page."
    )
    return out


def _group_levels(
    groups: dict[str, GroupRef],
    personas_of: dict[str, list[str]],
    cells: PersonaCells,
    resource_type: str,
) -> dict[str, str]:
    """group CURRENT name -> effective (highest) level across its personas."""
    return {
        ref.name: group_effective(cells, personas_of.get(gid, []), resource_type).level
        for gid, ref in groups.items()
        if not ref.missing
    }


def _apply_type(
    obo_ws: WorkspaceClient, resource_type: str, group_levels: dict[str, str]
) -> tuple[int, list[str]]:
    """Merge-apply ``group_levels`` onto every resource of a (non-Genie) type."""
    updated = 0
    errors: list[str] = []
    if not group_levels:
        return 0, errors
    try:
        resources = list_resources(obo_ws, resource_type)
    except Exception as e:
        msg = f"Failed to list {resource_type} resources: {e}"
        logger.error(msg)
        return 0, [msg]
    for resource in resources:
        try:
            # Read-modify-write MERGE: only these groups are changed; existing
            # users, owners, admins, and other groups are preserved.
            apply_group_acl(obo_ws, resource_type, resource.id, group_levels)
            updated += 1
        except Exception as e:
            msg = f"Failed to set permissions on {resource.name} ({resource.id}): {str(e)}"
            logger.error(msg)
            errors.append(msg)
    return updated, errors


class _GenieOutcome:
    def __init__(self) -> None:
        self.updated = 0
        self.added = 0
        self.revoked = 0
        self.errors: list[str] = []
        # Entries deliberately left untouched (workspace-local group, unreadable
        # space): errors for Apply, warnings for removal (whose success is
        # judged by what is left in the ledger instead).
        self.blocked: list[str] = []
        self.warnings: list[str] = []


def _apply_genie(
    obo_ws: WorkspaceClient,
    session: Session,
    groups: dict[str, GroupRef],
    actor: str,
    *,
    exclude: frozenset[tuple[str, str]] = frozenset(),
) -> _GenieOutcome:
    """Per space: grant the tables each group's Genie level needs, write the
    group's effective space ACL, then release table grants nothing needs."""
    out = _GenieOutcome()
    try:
        spaces = list_resources(obo_ws, GENIE)
    except Exception as e:
        out.errors.append(f"Failed to list Genie spaces: {e}")
        return out
    plan = plan_genie(obo_ws, session, groups, spaces, exclude=exclude)
    tables = execute_genie_table_grants(obo_ws, session, plan, actor)
    out.blocked.extend(plan.errors)
    for space in spaces:
        levels = plan.acl_levels(space.id)
        if not levels:
            continue
        try:
            apply_group_acl(obo_ws, GENIE, space.id, levels)
            out.updated += 1
        except Exception as e:
            msg = f"Failed to set permissions on {space.name} ({space.id}): {str(e)}"
            logger.error(msg)
            out.errors.append(msg)
    release_stale_table_grants(obo_ws, session, plan, tables)
    out.added = len(tables.granted)
    out.revoked = len(tables.revoked)
    out.errors.extend(tables.errors)
    out.warnings.extend(plan.warnings + tables.warnings)
    return out


def _preview_type(
    obo_ws: WorkspaceClient,
    session: Session,
    groups: dict[str, GroupRef],
    resource_type: str,
    *,
    exclude: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[list[GroupLevelOut], list[TableGrantPlanOut], list[str]]:
    """Read-only per-group effective levels (+ Genie table grants) for a type."""
    live = {gid: r for gid, r in groups.items() if not r.missing}
    if resource_type == GENIE:
        plan = plan_genie(obo_ws, session, live, list_resources(obo_ws, GENIE), exclude=exclude)
        return genie_level_rows(plan), table_grant_rows(obo_ws, session, plan), plan.warnings
    personas_of = personas_by_group(session, live, exclude=exclude)
    cells = load_cells(session, [p for ps in personas_of.values() for p in ps])
    rows = []
    for gid, ref in live.items():
        eff = group_effective(cells, personas_of[gid], resource_type)
        rows.append(
            GroupLevelOut(
                group_id=gid,
                group_name=ref.name,
                resource_type=resource_type,
                resource_type_label=_resource_type_label(resource_type),
                effective_level=eff.level,
                granted_by=eff.granted_by,
                source_levels=eff.source_levels,
                escalation=eff.escalation,
                escalation_detail=eff.escalation_detail,
            )
        )
    return rows, [], []


def _persist_group_renames(session: Session, groups: dict[str, GroupRef]) -> None:
    """Groups are identified by id; refresh the stored display names of every
    mapping of a group that was renamed (mutating paths only)."""
    renamed = {gid: r.name for gid, r in groups.items() if not r.missing and r.name != r.stored_name}
    if not renamed:
        return
    for m in session.exec(
        select(PersonaGroupMapping).where(col(PersonaGroupMapping.group_id).in_(sorted(renamed)))
    ).all():
        if m.group_id in renamed and m.group_name != renamed[m.group_id]:
            m.group_name = renamed[m.group_id]
            session.add(m)
    session.commit()


# ─── Persona User Mappings ────────────────────────────────────────────────────


def _effective_user_level(
    session,
    user_name: str,
    resource_type: str,
    exclude_persona: str | None = None,
) -> str:
    """Compute the effective (MAX) permission level for ``user_name`` across all
    personas they are directly assigned to, for a given ``resource_type``.

    When ``exclude_persona`` is set, that persona is excluded from the computation
    (used during remove to find the residual level from remaining assignments).

    Returns ``NO_PERMISSIONS`` when the user has no remaining direct assignments.
    """
    mappings = session.exec(
        select(PersonaUserMapping).where(
            PersonaUserMapping.user_name == user_name
        )
    ).all()

    best = PermissionLevel.NO_PERMISSIONS.value
    for um in mappings:
        if exclude_persona and um.persona == exclude_persona:
            continue
        tmpl = session.exec(
            select(PermissionTemplate).where(
                PermissionTemplate.persona == um.persona,
                PermissionTemplate.resource_type == resource_type,
            )
        ).first()
        if tmpl:
            best = get_higher_permission(resource_type, best, tmpl.permission_level)
    return best


def _sync_direct_user_acl(
    obo_ws: WorkspaceClient,
    session,
    user_name: str,
    templates,
    exclude_persona: str | None = None,
) -> tuple[int, list[str]]:
    """Re-sync all per-user ACL entries for ``user_name`` across the given templates.

    For each supported resource type in ``templates``:
      - Compute the effective level (MAX across all direct-persona assignments,
        optionally excluding one persona).
      - Apply that level to every resource of that type (NO_PERMISSIONS = revoke).

    Returns (updated_count, error_strings). updated_count is the number of
    resource ACL writes that succeeded.
    """
    errors: list[str] = []
    updated = 0
    for template in templates:
        rt = template.resource_type
        if rt not in SUPPORTED_ACL_RESOURCE_TYPES:
            continue
        effective = _effective_user_level(session, user_name, rt, exclude_persona=exclude_persona)
        if not is_permission_level_allowed(rt, effective):
            continue
        resources = list_resources(obo_ws, rt)
        for resource in resources:
            try:
                apply_user_acl(obo_ws, rt, resource.id, {user_name: effective})
                updated += 1
            except Exception as e:
                errors.append(
                    f"Failed to set per-user ACL on {resource.name} ({resource.id}): {e}"
                )
    return updated, errors


@api.post(
    "/personas/user-mappings",
    response_model=dict,
    operation_id="createPersonaUserMapping",
)
def create_persona_user_mapping(
    body: PersonaUserMappingIn,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """Directly assign a user to a persona: grants the persona's matrix permissions
    to that individual user via per-user ACL entries (``user_name`` principal),
    WITHOUT touching workspace-group membership or SCIM.

    The user retains their original identity. Group-tagged-to-persona paths remain
    fully intact and unchanged. Both mechanisms coexist independently.

    If the user is already directly assigned to other personas, their effective
    ACL is the MAX level across ALL direct-persona assignments (computed per
    resource type). The row is upserted (idempotent on repeated add to same persona).

    Apply is NOT required after this call — ACLs are written immediately.
    """
    # Input validation — user_name is an email; user_id is a SCIM numeric id.
    # Both go through the same character-set guard to block injection.
    _assert_valid_principal_id(body.user_id)
    _assert_valid_principal_id(body.user_name)

    # Persona lock + existence re-check, held through the row upsert and the
    # per-user ACL writes (persona -> no group / grant locks).
    with _persona_write(session, body.persona, missing_status=400) as persona_def:
        return _create_persona_user_mapping_locked(obo_ws, session, body, persona_def)


def _create_persona_user_mapping_locked(
    obo_ws: WorkspaceClient,
    session: Session,
    body: PersonaUserMappingIn,
    persona_def: PersonaDefinition,
) -> dict:
    # Upsert the PersonaUserMapping row race-safely via ON CONFLICT DO NOTHING.
    # The UNIQUE(persona, user_name) constraint turns a concurrent duplicate add
    # into a clean no-op rather than a 500 IntegrityError.
    stmt = (
        pg_insert(PersonaUserMapping.__table__)
        .values(
            user_id=body.user_id,
            user_name=body.user_name,
            display_name=body.display_name,
            persona=body.persona,
        )
        .on_conflict_do_nothing(
            index_elements=["persona", "user_name"]
        )
        .returning(PersonaUserMapping.__table__.c.id)
    )
    result = session.execute(stmt)
    session.commit()
    was_new = result.fetchone() is not None

    # Load the persona's permission templates and immediately apply per-user ACLs.
    templates = session.exec(
        select(PermissionTemplate).where(PermissionTemplate.persona == body.persona)
    ).all()

    _, acl_errors = _sync_direct_user_acl(obo_ws, session, body.user_name, templates)

    if acl_errors:
        logger.error(
            f"Direct-add of user {body.user_name} to persona '{body.persona}': "
            f"{len(acl_errors)} ACL error(s): {acl_errors}"
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"User was recorded as directly assigned to persona "
                f"'{persona_def.label}' but {len(acl_errors)} ACL write(s) failed. "
                f"Re-run Add or trigger Apply to retry. Errors: {acl_errors}"
            ),
        )

    action = "added" if was_new else "already assigned (ACLs re-synced)"
    return {
        "status": "ok",
        "persona": body.persona,
        "user_id": body.user_id,
        "user_name": body.user_name,
        "message": (
            f"User {action} directly to persona '{persona_def.label}' — "
            f"per-user ACL entries written immediately (no Apply needed). "
            f"Group membership was NOT changed."
        ),
    }


@api.delete(
    "/personas/{persona}/members/{user_name}",
    response_model=dict,
    operation_id="removePersonaMember",
)
def remove_persona_member(
    persona: str,
    user_name: str,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """Revoke a user's direct-persona assignment: delete the PersonaUserMapping row
    and recompute their effective ACL across any remaining direct-persona assignments.

    If the user is directly assigned to OTHER personas for the same resource, their
    ACL is set to the MAX level of those remaining assignments (not a blank revoke).
    If no other direct assignments remain, their per-user ACL entry is removed.

    Group membership is NOT touched by this endpoint. If the user is also a member
    of a mapped group, that group-based ACL continues to apply independently.
    """
    _assert_valid_principal_id(user_name)

    with _persona_write(session, persona) as persona_def:
        return _remove_persona_member_locked(obo_ws, session, persona, user_name, persona_def)


def _remove_persona_member_locked(
    obo_ws: WorkspaceClient,
    session: Session,
    persona: str,
    user_name: str,
    persona_def: PersonaDefinition,
) -> dict:
    mapping = session.exec(
        select(PersonaUserMapping).where(
            PersonaUserMapping.persona == persona,
            PersonaUserMapping.user_name == user_name,
        )
    ).first()
    if not mapping:
        raise HTTPException(
            status_code=404,
            detail=(
                f"User '{user_name}' is not directly assigned to persona "
                f"'{persona}'. (Group-based membership cannot be removed here.)"
            ),
        )

    # ACL revoke FIRST, DB delete only on success.
    # Rationale: if we delete the row first and the ACL write fails, the mapping
    # is gone — a retry returns 404 and Apply won't find the user either, leaving
    # a stale per-user ACL grant with no recovery path. By doing the ACL write
    # first we keep the row intact on failure so the caller can retry or Apply.
    templates = session.exec(
        select(PermissionTemplate).where(PermissionTemplate.persona == persona)
    ).all()

    _, acl_errors = _sync_direct_user_acl(
        obo_ws, session, user_name, templates, exclude_persona=persona
    )

    if acl_errors:
        logger.error(
            f"Direct-remove of user {user_name} from persona '{persona}': "
            f"{len(acl_errors)} ACL error(s): {acl_errors}"
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"ACL revoke failed for '{persona_def.label}' — DB mapping was "
                f"NOT deleted so a retry or Apply can recover. "
                f"Errors: {acl_errors}"
            ),
        )

    # ACL writes succeeded — now safe to delete the mapping row.
    session.delete(mapping)
    session.commit()

    return {
        "status": "ok",
        "persona": persona,
        "user_name": user_name,
        "message": (
            f"Direct assignment of '{user_name}' to persona '{persona_def.label}' "
            f"removed — per-user ACL entries recomputed across remaining assignments. "
            f"Group membership was NOT changed."
        ),
    }


# ─── Permissions Matrix (DB-backed) ──────────────────────────────────────────


@api.get(
    "/permissions/matrix",
    response_model=PermissionMatrixOut,
    operation_id="getPermissionMatrix",
)
def get_permission_matrix(session: SessionDep, _admin: AdminRequired = True):
    """Get the full permissions matrix (seeded from defaults on first call)."""
    # Ensure persona definitions are seeded
    definitions = session.exec(select(PersonaDefinition)).all()
    if not definitions:
        _seed_persona_definitions(session)
        definitions = session.exec(select(PersonaDefinition)).all()

    templates = session.exec(select(PermissionTemplate)).all()

    # Seed defaults if empty
    if not templates:
        _seed_permission_templates(session)
        templates = session.exec(select(PermissionTemplate)).all()

    # Defensive backfill: guarantee EVERY persona has a cell for EVERY resource
    # type, so the matrix always reflects (a) newly created personas — even if
    # their create-time template seeding was incomplete — and (b) resource types
    # added in a later release (e.g. Genie/Apps/Lakebase), for which pre-existing
    # personas (custom ones especially) would otherwise have no row. Missing cells
    # default to NO_PERMISSIONS. Runs a write ONLY when a gap actually exists
    # (steady state: no write), and ON CONFLICT DO NOTHING keeps it idempotent and
    # never overwrites an admin's chosen level.
    existing_cells = {(t.persona, t.resource_type) for t in templates}
    missing_rows = [
        {
            "persona": d.key,
            "resource_type": rt.value,
            "permission_level": PermissionLevel.NO_PERMISSIONS.value,
        }
        for d in definitions
        for rt in ResourceType
        if (d.key, rt.value) not in existing_cells
    ]
    if missing_rows:
        _insert_template_rows(session, missing_rows)
        templates = session.exec(select(PermissionTemplate)).all()
        definitions = session.exec(select(PersonaDefinition)).all()

    matrix = [
        PermissionTemplateOut(
            id=t.id,  # type: ignore
            persona=t.persona,
            resource_type=t.resource_type,
            permission_level=t.permission_level,
        )
        for t in templates
    ]

    # Build persona list and labels from DB definitions
    persona_keys = [d.key for d in definitions]
    persona_labels = {d.key: d.label for d in definitions}

    return PermissionMatrixOut(
        matrix=matrix,
        personas=persona_keys,
        resource_types=[r.value for r in ResourceType],
        persona_labels=persona_labels,
        resource_type_labels={r.value: RESOURCE_TYPE_LABELS[r] for r in ResourceType},
        allowed_permission_levels={
            r.value: [pl.value for pl in RESOURCE_PERMISSION_LEVELS[r]]
            for r in ResourceType
        },
        overrides=[_override_out(o) for o in session.exec(select(PersonaResourceOverride)).all()],
        overridable_resource_types=sorted(OVERRIDABLE_RESOURCE_TYPES),
    )


def _override_out(o: PersonaResourceOverride) -> PermissionOverrideOut:
    return PermissionOverrideOut(
        id=o.id or 0,
        persona=o.persona,
        resource_type=o.resource_type,
        resource_id=o.resource_id,
        permission_level=o.permission_level,
    )


@api.put(
    "/permissions/matrix/overrides",
    response_model=list[PermissionOverrideOut],
    operation_id="setPermissionOverride",
)
def set_permission_override(
    body: PermissionOverrideIn, session: SessionDep, _admin: AdminRequired = True
):
    """Set a persona's level on ONE resource (e.g. one Genie space), replacing
    its "all resources" cell there; ``permission_level: null`` clears it.
    Returns that persona's overrides. Nothing is written to the workspace until
    the persona is applied."""
    rt = body.resource_type.value
    if rt not in OVERRIDABLE_RESOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Per-resource levels are only supported for: "
                f"{', '.join(sorted(OVERRIDABLE_RESOURCE_TYPES))}."
            ),
        )
    if not body.resource_id.strip():
        raise HTTPException(status_code=400, detail="resource_id is required.")
    with _persona_write(session, body.persona):
        return _set_override_locked(session, body, rt)


def _set_override_locked(
    session: Session, body: PermissionOverrideIn, rt: str
) -> list[PermissionOverrideOut]:
    existing = session.exec(
        select(PersonaResourceOverride).where(
            PersonaResourceOverride.persona == body.persona,
            PersonaResourceOverride.resource_type == rt,
            PersonaResourceOverride.resource_id == body.resource_id,
        )
    ).first()
    if body.permission_level is None:
        if existing is not None:
            session.delete(existing)
    else:
        _assert_level_allowed(rt, body.permission_level.value)
        if existing is None:
            existing = PersonaResourceOverride(
                persona=body.persona, resource_type=rt, resource_id=body.resource_id,
                permission_level=body.permission_level.value,
            )
        else:
            existing.permission_level = body.permission_level.value
        session.add(existing)
    session.commit()
    return [
        _override_out(o)
        for o in session.exec(
            select(PersonaResourceOverride).where(PersonaResourceOverride.persona == body.persona)
        ).all()
    ]


@api.put(
    "/permissions/matrix",
    response_model=PermissionTemplateOut,
    operation_id="updatePermissionMatrix",
)
def update_permission_matrix(body: PermissionMatrixCell, session: SessionDep, _admin: AdminRequired = True):
    """Update a single cell in the permissions matrix."""
    # Never store a (resource_type, level) combination the resource type does not
    # support (e.g. CAN_MANAGE on a cluster-policy). Reject with a 400 before the
    # DB write; NO_PERMISSIONS is always allowed.
    _assert_level_allowed(body.resource_type.value, body.permission_level.value)
    with _persona_write(session, body.persona):
        return _update_cell_locked(session, body)


def _update_cell_locked(session: Session, body: PermissionMatrixCell) -> PermissionTemplateOut:
    template = session.exec(
        select(PermissionTemplate).where(
            PermissionTemplate.persona == body.persona,
            PermissionTemplate.resource_type == body.resource_type.value,
        )
    ).first()

    if not template:
        # Create if it doesn't exist
        template = PermissionTemplate(
            persona=body.persona,
            resource_type=body.resource_type.value,
            permission_level=body.permission_level.value,
        )
        session.add(template)
    else:
        template.permission_level = body.permission_level.value

    session.commit()
    session.refresh(template)

    return PermissionTemplateOut(
        id=template.id,  # type: ignore
        persona=template.persona,
        resource_type=template.resource_type,
        permission_level=template.permission_level,
    )


@api.get(
    "/permissions/conflicts",
    response_model=PermissionConflictsOut,
    operation_id="checkPermissionConflicts",
)
def check_permission_conflicts(
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """Detect permission conflicts for users who belong to multiple persona groups.

    A conflict occurs when a user is in groups mapped to different personas,
    and those personas have different permission levels for the same resource type.
    Conflicts are resolved by taking the highest permission level.
    """
    # Build persona key -> label lookup from DB
    definitions = session.exec(select(PersonaDefinition)).all()
    persona_label_map: dict[str, str] = {d.key: d.label for d in definitions}

    # 1. Get all persona-group mappings
    mappings = session.exec(select(PersonaGroupMapping)).all()
    if not mappings:
        return PermissionConflictsOut(
            total_users_checked=0, users_with_conflicts=0, conflicts=[]
        )

    # Build group_name -> personas lookup (personas stack: a group may map to
    # several personas).
    group_name_to_personas: dict[str, set[str]] = {}
    for m in mappings:
        group_name_to_personas.setdefault(m.group_name, set()).add(m.persona)

    mapped_group_names = set(group_name_to_personas.keys())

    # 2. Get the full permission template matrix
    templates = session.exec(select(PermissionTemplate)).all()
    if not templates:
        return PermissionConflictsOut(
            total_users_checked=0, users_with_conflicts=0, conflicts=[]
        )

    # Build persona+resource_type -> permission_level lookup
    template_map: dict[str, str] = {}
    for t in templates:
        template_map[f"{t.persona}::{t.resource_type}"] = t.permission_level

    # 3. Fetch all workspace users with group memberships
    users_with_multiple_personas: list[UserPermissionConflict] = []
    total_users_checked = 0

    try:
        all_users = list(
            obo_ws.users.list(attributes="id,userName,displayName,groups")
        )
    except Exception as e:
        logger.warning(f"Could not fetch users for conflict check: {e}")
        return PermissionConflictsOut(
            total_users_checked=0, users_with_conflicts=0, conflicts=[]
        )

    for user in all_users:
        # Find which mapped groups this user belongs to
        user_group_names: set[str] = set()
        if user.groups:
            for g in user.groups:
                if g.display:
                    user_group_names.add(g.display)

        user_mapped_groups = user_group_names.intersection(mapped_group_names)
        if not user_mapped_groups:
            continue

        total_users_checked += 1

        # Find which personas this user has through their groups
        user_personas: set[str] = set()
        for gname in user_mapped_groups:
            user_personas.update(group_name_to_personas.get(gname, set()))

        if len(user_personas) < 2:
            continue  # No conflict possible with a single persona

        # 4. Check for conflicts across resource types
        conflicts: list[PermissionConflictDetail] = []
        for rt in ResourceType:
            # Gather permission levels from each persona for this resource type
            persona_levels: dict[str, str] = {}
            levels_set: set[str] = set()
            for persona_val in user_personas:
                level = template_map.get(
                    f"{persona_val}::{rt.value}",
                    PermissionLevel.NO_PERMISSIONS.value,
                )
                persona_label = persona_label_map.get(persona_val, persona_val)
                persona_levels[persona_label] = level
                levels_set.add(level)

            # If there are different permission levels, it's a conflict
            if len(levels_set) > 1:
                effective = get_higher_permission(
                    rt.value, *list(levels_set)
                )
                conflicts.append(
                    PermissionConflictDetail(
                        resource_type=rt.value,
                        resource_type_label=RESOURCE_TYPE_LABELS.get(
                            rt, rt.value
                        ),
                        persona_levels=persona_levels,
                        effective_level=effective,
                    )
                )

        if conflicts:
            persona_labels_list = [
                persona_label_map.get(p, p) for p in user_personas
            ]
            users_with_multiple_personas.append(
                UserPermissionConflict(
                    user_id=user.id or "",
                    user_name=user.user_name,
                    display_name=user.display_name,
                    personas=sorted(persona_labels_list),
                    conflict_count=len(conflicts),
                    conflicts=conflicts,
                )
            )

    return PermissionConflictsOut(
        total_users_checked=total_users_checked,
        users_with_conflicts=len(users_with_multiple_personas),
        conflicts=users_with_multiple_personas,
    )


def _resource_type_label(resource_type: str) -> str:
    """Human-readable label for a resource type, tolerant of unknown values."""
    try:
        return RESOURCE_TYPE_LABELS[ResourceType(resource_type)]
    except (ValueError, KeyError):
        return resource_type


def _compute_apply_plan(
    obo_ws: WorkspaceClient,
    templates,
    session: Session | None = None,
    groups: dict[str, GroupRef] | None = None,
) -> tuple[list[ApplyPlanItemOut], list[ApplyPlanSkippedOut], list[GroupLevelOut], list[TableGrantPlanOut], list[str]]:
    """Compute the DRY-RUN plan for applying a persona — ZERO workspace or DB writes.

    The read-only twin of the write loop in ``apply_permissions``, mirroring its
    per-type branching so the preview never lies about what a real apply does:

      * unsupported type (no lister) -> skipped (``unsupported``);
      * type-invalid level           -> skipped (``invalid_level``);
      * no mapped group ends up with any level (no persona grants anything)
                                      -> skipped (``no_permissions``);
      * otherwise                     -> a plan row with the LIVE resource count
        and the highest effective level.

    Plus the least-privilege detail: per group and object, the EFFECTIVE level
    and which personas / bundles grant it (with escalation flags), and the Genie
    table grants that would be added / kept / revoked (with row-filter / mask
    status). Reads only (``list_resources``, group / space / table lookups,
    effective-grant reads); it NEVER calls ``apply_group_acl`` or ``grants.update``.
    """
    plan: list[ApplyPlanItemOut] = []
    skipped: list[ApplyPlanSkippedOut] = []
    level_rows: list[GroupLevelOut] = []
    grant_rows: list[TableGrantPlanOut] = []
    warnings: list[str] = []
    groups = groups or {}

    for template in templates:
        level = template.permission_level
        rt = template.resource_type
        label = _resource_type_label(rt)

        if rt not in SUPPORTED_ACL_RESOURCE_TYPES:
            # Mirrors apply: an unsupported type at NO_PERMISSIONS is a genuine
            # no-op; any other level cannot be applied (no lister).
            reason = (
                "no_permissions"
                if level == PermissionLevel.NO_PERMISSIONS.value
                else "unsupported"
            )
            skipped.append(
                ApplyPlanSkippedOut(
                    resource_type=rt,
                    resource_type_label=label,
                    target_level=level,
                    reason=reason,
                )
            )
            continue

        if not is_permission_level_allowed(rt, level):
            skipped.append(
                ApplyPlanSkippedOut(
                    resource_type=rt,
                    resource_type_label=label,
                    target_level=level,
                    reason="invalid_level",
                )
            )
            continue

        if session is not None and groups:
            rows, grants, warns = _preview_type(obo_ws, session, groups, rt)
            level_rows.extend(rows)
            grant_rows.extend(grants)
            warnings.extend(warns)
            effective = [r.effective_level for r in rows if r.effective_level != NONE]
        else:
            rows, effective = [], ([level] if level != NONE else [])

        if not effective:
            skipped.append(
                ApplyPlanSkippedOut(
                    resource_type=rt,
                    resource_type_label=label,
                    target_level=PermissionLevel.NO_PERMISSIONS.value,
                    reason="no_permissions",
                )
            )
            continue

        if rt == GENIE and rows:
            count = len({r.resource_id for r in rows if not r.blocked})
        else:
            # Read-only: count how many resources of this type would be rewritten.
            count = len(list_resources(obo_ws, rt))
        plan.append(
            ApplyPlanItemOut(
                resource_type=rt,
                resource_type_label=label,
                target_level=get_higher_permission(rt, *effective),
                resource_count=count,
            )
        )

    return plan, skipped, level_rows, grant_rows, sorted(set(warnings))


@api.get(
    "/permissions/apply/{persona}/preview",
    response_model=ApplyPreviewOut,
    operation_id="previewApplyPermissions",
)
def preview_apply_permissions(
    persona: str,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """DRY-RUN preview of applying a persona's template — computes the blast
    radius per resource type WITHOUT writing anything (workspace or database).

    Returns, for every resource type the real apply WOULD write, the target
    level and how many live workspace resources of that type would be affected;
    types at ``NO_PERMISSIONS`` or that are unsupported/invalid are reported
    separately as skipped. Admin-gated exactly like the real apply, and
    validated with the same persona/mappings/template preconditions so a
    successful preview implies a runnable apply.
    """
    persona_def = session.exec(
        select(PersonaDefinition).where(PersonaDefinition.key == persona)
    ).first()
    if not persona_def:
        raise HTTPException(status_code=404, detail=f"Persona '{persona}' not found")

    mappings = session.exec(
        select(PersonaGroupMapping).where(PersonaGroupMapping.persona == persona)
    ).all()

    templates = session.exec(
        select(PermissionTemplate).where(PermissionTemplate.persona == persona)
    ).all()
    if not templates:
        raise HTTPException(
            status_code=400,
            detail="Permission template not found. Load the matrix first.",
        )

    # For direct-only personas (no mapped groups) the group-ACL plan is empty
    # but we still need to surface the direct_user_count so the confirmation
    # modal can show it.  Pass an empty list to _compute_apply_plan — it will
    # iterate zero templates that have mappings, producing an all-skipped plan.
    # The real apply handles the no-groups case identically (the group_levels
    # dict is empty, so it emits a zero-updated ApplyResultOut per type).
    groups = resolve_groups(obo_ws, mappings)
    plan, skipped, level_rows, grant_rows, warnings = _compute_apply_plan(
        obo_ws, templates if mappings else [], session, groups
    )
    warnings = [
        f"Group id {r.group_id} ('{r.stored_name}') no longer exists; Apply will skip it."
        for r in groups.values()
        if r.missing
    ] + warnings
    filtered = [o for o in grant_rows if o.has_row_filter or o.masked_columns]
    if filtered:
        warnings.append(
            f"{len({o.full_name for o in filtered})} table(s) to be granted already have a "
            f"row filter or column mask; check what members of the group will see."
        )

    direct_user_count = len(
        session.exec(
            select(PersonaUserMapping).where(PersonaUserMapping.persona == persona)
        ).all()
    )

    return ApplyPreviewOut(
        persona=persona,
        group_count=len(mappings),
        groups=[m.group_name for m in mappings],
        plan=plan,
        skipped=skipped,
        total_resources_affected=sum(p.resource_count for p in plan),
        direct_user_count=direct_user_count,
        group_levels=level_rows,
        table_grants=grant_rows,
        escalation_count=sum(1 for r in level_rows if r.escalation),
        warnings=warnings,
    )


@api.post(
    "/permissions/apply/{persona}",
    response_model=ApplyAllResultOut,
    operation_id="applyPermissions",
)
def apply_permissions(
    persona: str,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    body: ApplyPermissionsIn | None = None,
    actor: ActorDep = "",
    _admin: AdminRequired = True,
):
    """Apply the persona's template permissions to resources in the workspace.

    By default this covers ALL resource types in the persona's template. Supply
    an optional body ``{"resource_types": [...]}`` to SCOPE the apply to only
    those types (blast-radius reduction); omitting the body / field preserves
    the original all-types behaviour. Each mapped group is set to its EFFECTIVE
    level — the highest any of its personas grants (personas stack).
    """
    # Validate persona exists
    persona_def = session.exec(
        select(PersonaDefinition).where(PersonaDefinition.key == persona)
    ).first()
    if not persona_def:
        raise HTTPException(status_code=404, detail=f"Persona '{persona}' not found")

    # Get groups mapped to this persona
    mappings = session.exec(
        select(PersonaGroupMapping).where(
            PersonaGroupMapping.persona == persona
        )
    ).all()

    # Get the permission template for this persona
    templates = session.exec(
        select(PermissionTemplate).where(
            PermissionTemplate.persona == persona
        )
    ).all()

    if not templates:
        raise HTTPException(
            status_code=400,
            detail="Permission template not found. Load the matrix first.",
        )

    # Optional per-resource-TYPE scoping. When the caller provides resource_types,
    # apply ONLY those types (each group still at its effective, stacked level);
    # None/omitted => all template types (original behaviour). Requested types are
    # validated against the persona's own template so an unknown/mistyped type is a
    # clear 400 rather than a silent no-op.
    requested_types = body.resource_types if body else None
    if requested_types is not None:
        template_types = {t.resource_type for t in templates}
        invalid = [rt for rt in requested_types if rt not in template_types]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"resource_types contains value(s) not in persona "
                    f"'{persona}''s template: {', '.join(invalid)}. Valid types: "
                    f"{', '.join(sorted(template_types))}."
                ),
            )
        requested_set = set(requested_types)
        templates = [t for t in templates if t.resource_type in requested_set]

    # Serialize with mapping removal / persona delete on the same groups (see
    # genie_bundles.operation_locks; order persona -> groups -> grants). The
    # persona is re-checked and mappings are RE-READ under the lock:
    # a removal that finished while we waited deleted its mapping, so that group
    # is skipped instead of being re-granted from a stale read. A group mapped
    # only after the first read is not locked and therefore not applied now.
    gids = {m.group_id for m in mappings}
    with operation_locks(session, personas=[persona], group_ids=gids):
        if not session.exec(
            select(PersonaDefinition)
            .where(PersonaDefinition.key == persona)
            .execution_options(populate_existing=True)
        ).first():
            raise HTTPException(status_code=404, detail=f"Persona '{persona}' not found")
        mappings = [
            m
            for m in session.exec(
                select(PersonaGroupMapping).where(PersonaGroupMapping.persona == persona)
            ).all()
            if m.group_id in gids
        ]
        _after_apply_mappings_read()
        return _apply_mapped(persona, obo_ws, session, mappings, templates, actor)


def _after_apply_mappings_read() -> None:
    """No-op seam: called by Apply right after it re-read the mappings under the
    group operation locks (tests pause here)."""


def _apply_mapped(
    persona: str,
    obo_ws: WorkspaceClient,
    session: Session,
    mappings: Sequence[PersonaGroupMapping],
    templates: Sequence[PermissionTemplate],
    actor: str,
) -> ApplyAllResultOut:
    """The Apply write loop; the caller holds the group operation locks."""
    # Personas STACK (see persona_sync). Each mapped group's level on an object
    # is the HIGHEST its own personas (and, for Genie, applied bundles) grant
    # there. Every member of the group holds all of the GROUP's personas, so this
    # never over-grants anyone; the app still never raises a group because of
    # personas its MEMBERS hold via OTHER groups (Databricks unions those at
    # access time). A persona's NO_PERMISSIONS grants nothing: a group's entry is
    # removed only when no source grants anything, so applying this persona never
    # strips access another persona of the same group grants. Groups are looked
    # up by id; a deleted group is skipped (never re-granted by name).
    groups = resolve_groups(obo_ws, mappings)
    live = {gid: r for gid, r in groups.items() if not r.missing}
    missing_errors = [
        f"Group id {r.group_id} ('{r.stored_name}') no longer exists; it was skipped."
        for r in groups.values()
        if r.missing
    ]
    _persist_group_renames(session, live)
    personas_of = personas_by_group(session, live)
    cells = load_cells(session, {persona, *(p for ps in personas_of.values() for p in ps)})
    table_added = table_revoked = 0
    table_errors: list[str] = []
    warnings: list[str] = list(missing_errors)
    results = []
    total_updated = 0
    total_errors = 0

    for template in templates:
        # THIS persona's cell decides only whether the type is valid / supported;
        # the level written per group is its effective (stacked) level below.
        level = template.permission_level

        # Resource types advertised in the matrix but that the app cannot
        # actually enumerate (no lister) must NOT report a false "success".
        if template.resource_type not in SUPPORTED_ACL_RESOURCE_TYPES:
            if level == PermissionLevel.NO_PERMISSIONS.value:
                # Nothing requested for an unsupported type — a genuine no-op.
                results.append(
                    ApplyResultOut(
                        persona=persona,
                        resource_type=template.resource_type,
                        resources_updated=0,
                        errors=[],
                    )
                )
            else:
                msg = (
                    f"Resource type '{template.resource_type}' is not supported "
                    f"by the apply path (the app cannot enumerate resources of "
                    f"this type), so the requested level "
                    f"'{level}' was NOT applied to the workspace."
                )
                logger.warning(msg)
                results.append(
                    ApplyResultOut(
                        persona=persona,
                        resource_type=template.resource_type,
                        resources_updated=0,
                        errors=[msg],
                    )
                )
                total_errors += 1
            continue

        # Reject a level the resource type does not support (e.g. a stale matrix
        # cell) with a clear per-type error instead of firing N doomed SDK calls.
        # NO_PERMISSIONS (revoke) is always valid and handled by the merge below.
        # A hard 400 here would abort the still-valid resource types, so this is
        # reported per-type — mirroring the unsupported-type branch above.
        if not is_permission_level_allowed(template.resource_type, level):
            allowed = [
                pl.value
                for pl in RESOURCE_PERMISSION_LEVELS.get(
                    ResourceType(template.resource_type), []
                )
            ]
            msg = (
                f"Permission level '{level}' is not valid for resource type "
                f"'{template.resource_type}'. Allowed levels: "
                f"{', '.join(allowed)}. Skipped — fix the matrix cell and re-apply."
            )
            logger.warning(msg)
            results.append(
                ApplyResultOut(
                    persona=persona,
                    resource_type=template.resource_type,
                    resources_updated=0,
                    errors=[msg],
                )
            )
            total_errors += 1
            continue

        # Every mapped group is set to its EFFECTIVE level across ALL of its
        # personas (NO_PERMISSIONS = nothing grants it -> the merge helper
        # removes the group's entry). When the persona has no (live) mapped
        # groups (direct-only persona), skip the group ACL loop entirely.
        if not live:
            results.append(
                ApplyResultOut(
                    persona=persona,
                    resource_type=template.resource_type,
                    resources_updated=0,
                    errors=[],
                )
            )
            continue

        if template.resource_type == GENIE:
            # Per space (overrides), and Genie access always comes WITH the
            # table grants the space needs.
            genie = _apply_genie(obo_ws, session, live, actor)
            updated, resource_errors = genie.updated, genie.blocked + genie.errors
            table_added += genie.added
            table_revoked += genie.revoked
            table_errors.extend(genie.errors)
            warnings.extend(genie.warnings)
        else:
            updated, resource_errors = _apply_type(
                obo_ws,
                template.resource_type,
                _group_levels(live, personas_of, cells, template.resource_type),
            )

        total_updated += updated
        total_errors += len(resource_errors)
        results.append(
            ApplyResultOut(
                persona=persona,
                resource_type=template.resource_type,
                resources_updated=updated,
                errors=resource_errors,
            )
        )

    # Re-sync per-user ACL entries for every user directly assigned to this persona.
    # Each user's effective level is the MAX across ALL their direct-persona assignments
    # (so a user in two personas always gets the higher of the two, never stale).
    direct_users = session.exec(
        select(PersonaUserMapping).where(PersonaUserMapping.persona == persona)
    ).all()
    direct_users_synced = 0
    for dm in direct_users:
        user_updated, user_errors = _sync_direct_user_acl(obo_ws, session, dm.user_name, templates)
        total_updated += user_updated
        if user_errors:
            logger.error(
                f"Apply re-sync for direct user {dm.user_name} on persona "
                f"'{persona}': {len(user_errors)} error(s): {user_errors}"
            )
            total_errors += len(user_errors)
        else:
            direct_users_synced += 1

    return ApplyAllResultOut(
        persona=persona,
        results=results,
        total_resources_updated=total_updated,
        total_errors=total_errors + len(missing_errors),
        direct_users_synced=direct_users_synced,
        table_grants_added=table_added,
        table_grants_revoked=table_revoked,
        table_grant_errors=table_errors,
        warnings=warnings,
    )


# ─── Resources (from Databricks workspace) ───────────────────────────────────


@api.get(
    "/resources/{resource_type}",
    response_model=list[ResourceItemOut],
    operation_id="listResourcesByType",
)
def list_resources_by_type(
    resource_type: str,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    _admin: AdminRequired = True,
):
    """List all resources of a given type."""
    return list_resources(obo_ws, resource_type)


@api.get(
    "/resources/{resource_type}/{resource_id}/permissions",
    response_model=ResourcePermissionsOut,
    operation_id="getResourcePermissions",
)
def get_resource_permissions(
    resource_type: str,
    resource_id: str,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    _admin: AdminRequired = True,
):
    """Get current permissions on a specific resource."""
    try:
        perms = obo_ws.permissions.get(
            request_object_type=resource_type,
            request_object_id=resource_id,
        )
    except Exception as e:
        # Redact raw SDK detail: log full server-side under a correlation id,
        # return a generic message (same 400 status) to the client.
        raise_internal_error("Could not get permissions", e, status_code=400)

    acl = []
    if perms.access_control_list:
        for entry in perms.access_control_list:
            all_permissions = []
            if entry.all_permissions:
                for p in entry.all_permissions:
                    all_permissions.append(
                        {
                            "permission_level": p.permission_level.value if p.permission_level else None,
                            "inherited": p.inherited or False,
                            "inherited_from_object": (
                                list(p.inherited_from_object)
                                if p.inherited_from_object
                                else []
                            ),
                        }
                    )
            acl.append(
                ResourcePermissionOut(
                    group_name=entry.group_name,
                    user_name=entry.user_name,
                    all_permissions=all_permissions,
                )
            )

    return ResourcePermissionsOut(
        resource_id=resource_id,
        resource_type=resource_type,
        access_control_list=acl,
    )


@api.get(
    "/resources/{resource_type}/{resource_id}/permission-levels",
    response_model=list[PermissionLevelOut],
    operation_id="getPermissionLevels",
)
def get_permission_levels(
    resource_type: str,
    resource_id: str,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    _admin: AdminRequired = True,
):
    """Get available permission levels for a resource."""
    try:
        levels = obo_ws.permissions.get_permission_levels(
            request_object_type=resource_type,
            request_object_id=resource_id,
        )
    except Exception as e:
        # Redact raw SDK detail: log full server-side under a correlation id,
        # return a generic message (same 400 status) to the client.
        raise_internal_error("Could not get permission levels", e, status_code=400)

    results = []
    if levels.permission_levels:
        for lvl in levels.permission_levels:
            results.append(
                PermissionLevelOut(
                    permission_level=lvl.permission_level.value if lvl.permission_level else "",
                    description=lvl.description,
                )
            )
    return results


@api.put(
    "/resources/{resource_type}/{resource_id}/permissions",
    response_model=dict,
    operation_id="setResourcePermissions",
)
def set_resource_permissions(
    resource_type: str,
    resource_id: str,
    body: list[SetPermissionIn],
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    _admin: AdminRequired = True,
):
    """Set permissions on a specific resource for the given groups.

    Only the groups named in the request are touched (merge semantics): each is
    set to its level, or REMOVED when the level is NO_PERMISSIONS. All other
    principals already on the resource — users, owners, admins, other groups —
    are preserved.
    """
    # Reject a level the resource type does not support (e.g. CAN_MANAGE on a
    # cluster-policy) with a 400 BEFORE any SDK call. NO_PERMISSIONS (revoke) is
    # always allowed; an unsupported resource type falls through to the existing
    # apply_group_acl error path below.
    for item in body:
        _assert_level_allowed(resource_type, item.permission_level.value)

    group_levels = {item.group_name: item.permission_level.value for item in body}

    try:
        apply_group_acl(obo_ws, resource_type, resource_id, group_levels)
    except Exception as e:
        # Redact raw SDK detail: log full server-side under a correlation id,
        # return a generic message (same 400 status) to the client.
        raise_internal_error("Failed to update permissions", e, status_code=400)

    return {"status": "ok", "message": "Permissions updated"}


# ─── Internal helpers ─────────────────────────────────────────────────────────


def _seed_permission_templates(session: Session):
    """Seed the permission template matrix (idempotent upsert; see backend.seed).

    Concurrency-safe: uses ``INSERT ... ON CONFLICT DO NOTHING`` on the UNIQUE
    ``(persona, resource_type)`` constraint, so a racing/repeated seed can never
    duplicate matrix cells. Cells are inserted only for personas that still
    exist, under their persona locks (a deleted default persona is never
    given cells again).
    """
    _insert_template_rows(session, default_permission_template_rows())
