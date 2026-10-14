"""Genie access bundles: one Genie space <-> one account group.

Genie always runs a question's SQL with the ASKING user's own Unity Catalog
permissions, so "give people a Genie agent" really means "give them CAN_RUN on
the space AND read access to every table the space uses". A bundle does both,
ONCE, for a single account-level group; onboarding is then just group
membership.

Lifecycle (see docs/genie-access-bundles.md):

  create   -> validates the space and that the group is an ACCOUNT group
  preview  -> read-only: discovered tables, required grants, what already
              exists, row-filter / column-mask status, blockers
  apply    -> CAN_RUN on the space + the missing UC grants, recorded in the
              grant ledger
  resync   -> re-reads the space; grants new tables and releases ledger rows
              that are no longer needed
  delete   -> releases every ledger row, then removes the bundle

Revocation is ledger-driven: a privilege is only ever revoked when the ledger
says THIS app granted it (``app_granted``) and no other bundle still relies on
it. Privileges that already existed when a bundle was applied are recorded as
needed (``app_granted=False``) but never revoked.
"""

import json
import re
from dataclasses import dataclass, field

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .logger import logger
from .models import (
    GenieAccessBundle,
    GenieBundleActionOut,
    GenieBundlePreviewOut,
    GenieGrantLedger,
    GenieGrantOut,
    GenieGroupCheckOut,
    GenieSpaceObjectOut,
    PermissionLevel,
    PersonaGroupMapping,
    ResourceType,
)
from .resources import apply_group_acl
from .uc_grants import (
    CATALOG,
    SCHEMA,
    TABLE,
    VOLUME,
    UCGrantError,
    add_privileges,
    effective_privileges,
    find_effective,
    remove_privileges,
)

# Ledger securable type for the space's own ACL entry (not a UC securable).
GENIE_SPACE = "genie_space"
SPACE_PRIVILEGE = PermissionLevel.CAN_RUN.value
# Space ACL levels that already include CAN_RUN.
_SPACE_SATISFYING = {
    PermissionLevel.CAN_RUN.value,
    PermissionLevel.CAN_EDIT.value,
    PermissionLevel.CAN_MANAGE.value,
}

# SCIM meta.resourceType values on the WORKSPACE groups API.
ACCOUNT_GROUP_RESOURCE_TYPE = "Group"
WORKSPACE_GROUP_RESOURCE_TYPE = "WorkspaceGroup"

# Group display names are interpolated into a SCIM filter string; quotes and
# backslashes (and control chars) could break out of it, so they are refused.
_GROUP_NAME_RE = re.compile(r'^[^"\\\x00-\x1f]{1,256}\Z')

# Revoke children before parents; grant parents before children.
_TYPE_ORDER = {GENIE_SPACE: 0, CATALOG: 1, SCHEMA: 2, TABLE: 3, VOLUME: 3}


@dataclass(frozen=True)
class GrantKey:
    securable_type: str
    full_name: str
    privilege: str


@dataclass
class _Evaluated:
    key: GrantKey
    status: str  # missing | present | managed | error
    detail: str | None = None


@dataclass
class BundlePlan:
    space_title: str = ""
    warehouse_id: str | None = None
    group: GenieGroupCheckOut | None = None
    objects: list[GenieSpaceObjectOut] = field(default_factory=list)
    evaluated: list[_Evaluated] = field(default_factory=list)
    stale_rows: list[GenieGrantLedger] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ─── Discovery ───────────────────────────────────────────────────────────────


def parse_serialized_space(serialized: str | None) -> list[tuple[str, str]]:
    """Extract ``(kind, identifier)`` data sources from a serialized space.

    The serialized space (``GET /api/2.0/genie/spaces/{id}?include_serialized_
    space=true``) lists its sources under ``data_sources``: ``tables`` (tables,
    views, materialized views and metric views — all UC TABLE securables) with
    an ``identifier``, and ``volumes`` with a ``/Volumes/...`` ``path``. Any
    other list under ``data_sources`` whose entries carry an ``identifier`` is
    treated as table-like, so a future ``metric_views`` key is still picked up.
    Order is preserved; duplicates are dropped. Raises ``ValueError`` on
    malformed JSON.
    """
    if not serialized:
        return []
    try:
        data = json.loads(serialized)
    except (TypeError, ValueError) as e:
        raise ValueError(f"serialized_space is not valid JSON: {e}") from e
    sources = data.get("data_sources") if isinstance(data, dict) else None
    if not isinstance(sources, dict):
        return []
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key, entries in sources.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if key == "volumes":
                kind, ident = "volume", entry.get("path") or entry.get("identifier")
            else:
                kind, ident = "table", entry.get("identifier")
            if not isinstance(ident, str) or not ident.strip():
                continue
            ref = (kind, ident.strip())
            if ref not in seen:
                seen.add(ref)
                out.append(ref)
    return out


def split_name(identifier: str) -> list[str]:
    """Split a dotted UC name into parts, honouring `back-tick` quoting."""
    parts: list[str] = []
    buf: list[str] = []
    quoted = False
    i = 0
    while i < len(identifier):
        ch = identifier[i]
        if ch == "`":
            if quoted and i + 1 < len(identifier) and identifier[i + 1] == "`":
                buf.append("`")  # escaped back-tick inside a quoted part
                i += 2
                continue
            quoted = not quoted
        elif ch == "." and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts]


def _volume_parts(path: str) -> list[str] | None:
    # /Volumes/<catalog>/<schema>/<volume>[/sub/dirs...]
    segs = [s for s in path.split("/") if s]
    if len(segs) >= 4 and segs[0] == "Volumes":
        return segs[1:4]
    return None


def resolve_objects(
    ws: WorkspaceClient, refs: list[tuple[str, str]]
) -> list[GenieSpaceObjectOut]:
    """Resolve each data source to a full three-part name via the UC API.

    Unresolvable references are RETURNED (``resolved=False`` + ``reason``),
    never dropped, so the preview can show them.
    """
    out: list[GenieSpaceObjectOut] = []
    for kind, ident in refs:
        parts = _volume_parts(ident) if kind == "volume" else split_name(ident)
        if not parts or len(parts) != 3 or not all(parts):
            out.append(
                GenieSpaceObjectOut(
                    kind=kind,
                    identifier=ident,
                    resolved=False,
                    reason="Not a fully-qualified catalog.schema.name reference.",
                )
            )
            continue
        name = ".".join(parts)
        try:
            if kind == "volume":
                vol = ws.volumes.read(name=name)
                out.append(
                    GenieSpaceObjectOut(
                        kind=kind,
                        identifier=ident,
                        full_name=vol.full_name or name,
                        table_type="VOLUME",
                        resolved=True,
                    )
                )
                continue
            info = ws.tables.get(full_name=name)
        except NotFound:
            out.append(
                GenieSpaceObjectOut(
                    kind=kind,
                    identifier=ident,
                    full_name=name,
                    resolved=False,
                    reason="Does not exist, or is not visible to the app service principal.",
                )
            )
            continue
        except PermissionDenied:
            out.append(
                GenieSpaceObjectOut(
                    kind=kind,
                    identifier=ident,
                    full_name=name,
                    resolved=False,
                    reason="The app service principal cannot read this object's metadata.",
                )
            )
            continue
        except Exception as e:
            logger.warning(f"Could not resolve Genie data source {ident}: {e}")
            out.append(
                GenieSpaceObjectOut(
                    kind=kind,
                    identifier=ident,
                    full_name=name,
                    resolved=False,
                    reason=f"Lookup failed ({type(e).__name__}).",
                )
            )
            continue
        masked = [c.name or "" for c in (info.columns or []) if c.mask is not None]
        out.append(
            GenieSpaceObjectOut(
                kind=kind,
                identifier=ident,
                full_name=info.full_name or name,
                table_type=info.table_type.value if info.table_type else None,
                resolved=True,
                has_row_filter=info.row_filter is not None,
                row_filter_function=(
                    info.row_filter.function_name if info.row_filter else None
                ),
                masked_columns=masked,
            )
        )
    return out


def required_grants(space_id: str, objects: list[GenieSpaceObjectOut]) -> list[GrantKey]:
    """CAN_RUN on the space + per-object UC privileges (deduped, parents first).

    SELECT on each table/view/MV/metric view (READ_VOLUME on volumes), plus
    USE_SCHEMA on each containing schema and USE_CATALOG on each containing
    catalog. Never schema-wide SELECT.
    """
    keys: list[GrantKey] = [GrantKey(GENIE_SPACE, space_id, SPACE_PRIVILEGE)]
    catalogs: list[GrantKey] = []
    schemas: list[GrantKey] = []
    leaves: list[GrantKey] = []
    for obj in objects:
        if not obj.resolved or not obj.full_name:
            continue
        cat, sch, _ = split_name(obj.full_name)
        catalogs.append(GrantKey(CATALOG, cat, "USE_CATALOG"))
        schemas.append(GrantKey(SCHEMA, f"{cat}.{sch}", "USE_SCHEMA"))
        if obj.kind == "volume":
            leaves.append(GrantKey(VOLUME, obj.full_name, "READ_VOLUME"))
        else:
            leaves.append(GrantKey(TABLE, obj.full_name, "SELECT"))
    for k in catalogs + schemas + leaves:
        if k not in keys:
            keys.append(k)
    return keys


# ─── Group validation ────────────────────────────────────────────────────────


def check_group(ws: WorkspaceClient, group_name: str) -> GenieGroupCheckOut:
    """Verify ``group_name`` exists in the workspace AND is an ACCOUNT group.

    The app never creates groups. Workspace-local groups are refused because
    Unity Catalog cannot grant privileges to them.
    """
    if not _GROUP_NAME_RE.match(group_name or ""):
        return GenieGroupCheckOut(
            group_name=group_name,
            exists=False,
            is_account_group=False,
            error="Invalid group name (quotes, backslashes and control characters are not allowed).",
        )
    try:
        matches = [
            g
            for g in ws.groups.list(
                filter=f'displayName eq "{group_name}"',
                attributes="id,displayName,meta",
            )
            if g.display_name == group_name
        ]
    except Exception as e:
        logger.error(f"Could not look up group {group_name}: {e}")
        return GenieGroupCheckOut(
            group_name=group_name,
            exists=False,
            is_account_group=False,
            error=f"Could not look up the group ({type(e).__name__}).",
        )
    if not matches:
        return GenieGroupCheckOut(
            group_name=group_name,
            exists=False,
            is_account_group=False,
            error=(
                f"Group '{group_name}' was not found in this workspace. Create it "
                f"as an ACCOUNT-level group (account console) and assign it to "
                f"this workspace; UC Central does not create groups."
            ),
        )
    g = matches[0]
    resource_type = g.meta.resource_type if g.meta else None
    if resource_type == ACCOUNT_GROUP_RESOURCE_TYPE:
        return GenieGroupCheckOut(
            group_name=group_name,
            group_id=g.id,
            exists=True,
            is_account_group=True,
            resource_type=resource_type,
        )
    if resource_type == WORKSPACE_GROUP_RESOURCE_TYPE:
        error = (
            f"Group '{group_name}' is a workspace-local group. Unity Catalog can "
            f"only grant privileges to ACCOUNT-level groups — create an account "
            f"group (account console), assign it to this workspace, and use that."
        )
    else:
        error = (
            f"Could not confirm that '{group_name}' is an account-level group "
            f"(SCIM resourceType={resource_type!r})."
        )
    return GenieGroupCheckOut(
        group_name=group_name,
        group_id=g.id,
        exists=True,
        is_account_group=False,
        resource_type=resource_type,
        error=error,
    )


# ─── Space ACL helpers ───────────────────────────────────────────────────────


def _space_direct_level(ws: WorkspaceClient, space_id: str, group_name: str) -> str | None:
    acl = ws.permissions.get(
        request_object_type=ResourceType.GENIE_SPACES.value, request_object_id=space_id
    )
    for entry in acl.access_control_list or []:
        if entry.group_name != group_name:
            continue
        for p in entry.all_permissions or []:
            if p.permission_level and not p.inherited:
                return p.permission_level.value
    return None


def _space_error(e: Exception, action: str, space_id: str) -> str:
    if isinstance(e, PermissionDenied):
        return (
            f"Permission denied while trying to {action} on Genie space "
            f"'{space_id}': the app service principal needs CAN_MANAGE on the space."
        )
    return f"Failed to {action} on Genie space '{space_id}' ({type(e).__name__})."


# ─── Ledger helpers ──────────────────────────────────────────────────────────


def _key_of(row: GenieGrantLedger) -> GrantKey:
    return GrantKey(row.securable_type, row.full_name, row.privilege)


def _ledger_for_bundle(session: Session, bundle_id: int) -> list[GenieGrantLedger]:
    return list(
        session.exec(
            select(GenieGrantLedger).where(GenieGrantLedger.bundle_id == bundle_id)
        ).all()
    )


def _ledger_for_grant(
    session: Session, principal: str, key: GrantKey
) -> list[GenieGrantLedger]:
    return list(
        session.exec(
            select(GenieGrantLedger).where(
                GenieGrantLedger.principal == principal,
                GenieGrantLedger.securable_type == key.securable_type,
                GenieGrantLedger.full_name == key.full_name,
                GenieGrantLedger.privilege == key.privilege,
            )
        ).all()
    )


def _commit_row(session: Session, row: GenieGrantLedger) -> None:
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent apply of the same bundle already recorded it.
        session.rollback()


def _grant_out(key: GrantKey, status: str, detail: str | None = None) -> GenieGrantOut:
    return GenieGrantOut(
        securable_type=key.securable_type,
        full_name=key.full_name,
        privilege=key.privilege,
        status=status,
        detail=detail,
    )


# ─── Plan ────────────────────────────────────────────────────────────────────


def build_plan(
    ws: WorkspaceClient, session: Session, bundle: GenieAccessBundle
) -> BundlePlan:
    """Compute everything the preview shows — with ZERO writes."""
    plan = BundlePlan(space_title=bundle.space_title)

    plan.group = check_group(ws, bundle.group_name)
    if plan.group.error:
        plan.blockers.append(plan.group.error)

    try:
        space = ws.genie.get_space(bundle.space_id, include_serialized_space=True)
        plan.space_title = space.title or bundle.space_title
        plan.warehouse_id = space.warehouse_id
        refs = parse_serialized_space(space.serialized_space)
    except ValueError as e:
        plan.blockers.append(f"Could not parse the Genie space definition: {e}")
        refs = []
    except PermissionDenied:
        plan.blockers.append(
            f"The app service principal cannot read Genie space '{bundle.space_id}' "
            f"(it needs CAN_MANAGE on the space to read its definition and set its ACL)."
        )
        refs = []
    except NotFound:
        plan.blockers.append(f"Genie space '{bundle.space_id}' was not found.")
        refs = []

    plan.objects = resolve_objects(ws, refs)
    if refs and not any(o.resolved for o in plan.objects):
        plan.blockers.append("None of the space's data sources could be resolved.")
    for o in plan.objects:
        if not o.resolved:
            plan.warnings.append(
                f"Data source '{o.identifier}' could not be resolved and will NOT "
                f"be granted: {o.reason}"
            )
    restricted = [o.full_name for o in plan.objects if o.has_row_filter or o.masked_columns]
    if restricted:
        plan.warnings.append(
            f"{len(restricted)} table(s) already have a row filter or column mask; "
            f"those functions run as the asking user, so check that members of "
            f"'{bundle.group_name}' see the intended rows/columns."
        )
    if plan.warehouse_id:
        plan.warnings.append(
            f"The space runs on SQL warehouse '{plan.warehouse_id}'. Warehouse "
            f"access is not managed by the bundle."
        )
    mapped = session.exec(
        select(PersonaGroupMapping).where(
            PersonaGroupMapping.group_name == bundle.group_name
        )
    ).first()
    if mapped:
        plan.warnings.append(
            f"'{bundle.group_name}' is also mapped to persona '{mapped.persona}'. "
            f"Applying that persona rewrites this group's Genie-space ACL; use a "
            f"dedicated group for the bundle."
        )

    required = required_grants(bundle.space_id, plan.objects)
    own_rows = _ledger_for_bundle(session, bundle.id or 0)
    required_set = set(required)
    plan.stale_rows = [r for r in own_rows if _key_of(r) not in required_set]

    if plan.blockers and plan.group and not plan.group.is_account_group:
        # Cannot evaluate grants for a principal UC does not know about.
        plan.evaluated = [_Evaluated(k, "missing") for k in required]
        return plan

    effective_cache: dict[tuple[str, str], list] = {}
    for key in required:
        plan.evaluated.append(
            _evaluate(ws, session, bundle, key, effective_cache)
        )
    return plan


def _evaluate(
    ws: WorkspaceClient,
    session: Session,
    bundle: GenieAccessBundle,
    key: GrantKey,
    cache: dict[tuple[str, str], list],
) -> _Evaluated:
    principal = bundle.group_name
    try:
        if key.securable_type == GENIE_SPACE:
            level = _space_direct_level(ws, key.full_name, principal)
            present = level in _SPACE_SATISFYING
            detail = f"group holds {level}" if level else None
        else:
            ck = (key.securable_type, key.full_name)
            if ck not in cache:
                cache[ck] = effective_privileges(
                    ws, key.securable_type, key.full_name, principal
                )
            hit = find_effective(cache[ck], key.privilege)
            present = hit is not None
            detail = None
            if hit is not None:
                via = hit.via_principal if hit.via_principal != principal else None
                parts = [hit.privilege]
                if hit.inherited_from:
                    parts.append(f"inherited from {hit.inherited_from}")
                if via:
                    parts.append(f"via {via}")
                detail = ", ".join(parts)
    except UCGrantError as e:
        return _Evaluated(key, "error", str(e))
    except Exception as e:
        return _Evaluated(key, "error", _space_error(e, "read the ACL", key.full_name))

    if not present:
        return _Evaluated(key, "missing", detail)
    rows = _ledger_for_grant(session, principal, key)
    if any(r.app_granted for r in rows):
        return _Evaluated(key, "managed", detail)
    return _Evaluated(key, "present", detail)


def to_preview(bundle: GenieAccessBundle, plan: BundlePlan) -> GenieBundlePreviewOut:
    assert plan.group is not None
    return GenieBundlePreviewOut(
        bundle_id=bundle.id or 0,
        space_id=bundle.space_id,
        space_title=plan.space_title,
        warehouse_id=plan.warehouse_id,
        group=plan.group,
        objects=plan.objects,
        grants=[_grant_out(e.key, e.status, e.detail) for e in plan.evaluated],
        stale_grants=[_grant_out(_key_of(r), "stale") for r in plan.stale_rows],
        blockers=plan.blockers,
        warnings=plan.warnings,
        can_apply=not plan.blockers,
    )


# ─── Apply / resync ──────────────────────────────────────────────────────────


def _grant(ws: WorkspaceClient, principal: str, key: GrantKey) -> None:
    if key.securable_type == GENIE_SPACE:
        try:
            apply_group_acl(
                ws, ResourceType.GENIE_SPACES.value, key.full_name, {principal: key.privilege}
            )
        except Exception as e:
            raise UCGrantError(
                _space_error(e, "grant CAN_RUN", key.full_name),
                permission_denied=isinstance(e, PermissionDenied),
            ) from e
        return
    add_privileges(ws, key.securable_type, key.full_name, principal, [key.privilege])


def apply_plan(
    ws: WorkspaceClient,
    session: Session,
    bundle: GenieAccessBundle,
    plan: BundlePlan,
    actor: str,
    *,
    revoke_stale: bool,
) -> GenieBundleActionOut:
    """Grant what is missing and record every required privilege in the ledger.

    Idempotent: a second run finds everything ``present``/``managed`` and makes
    no grant calls. With ``revoke_stale`` (resync) ledger rows that the
    current space definition no longer needs are released afterwards.
    """
    result = GenieBundleActionOut(
        bundle_id=bundle.id or 0,
        action="resync" if revoke_stale else "apply",
        status="ok",
        warnings=list(plan.warnings),
    )
    principal = bundle.group_name
    bundle_id = bundle.id or 0

    for ev in plan.evaluated:
        own = next(
            (r for r in _ledger_for_grant(session, principal, ev.key) if r.bundle_id == bundle_id),
            None,
        )
        if ev.status == "error":
            result.errors.append(ev.detail or f"Could not evaluate {ev.key}.")
            continue
        if ev.status == "missing":
            try:
                _grant(ws, principal, ev.key)
            except UCGrantError as e:
                logger.error(f"Genie bundle {bundle_id}: {e}")
                result.errors.append(str(e))
                continue
            if own is not None:
                own.app_granted = True
                session.add(own)
                session.commit()
            else:
                _commit_row(
                    session,
                    GenieGrantLedger(
                        bundle_id=bundle_id,
                        space_id=bundle.space_id,
                        principal=principal,
                        securable_type=ev.key.securable_type,
                        full_name=ev.key.full_name,
                        privilege=ev.key.privilege,
                        app_granted=True,
                        granted_by=actor,
                    ),
                )
            result.granted.append(_grant_out(ev.key, "granted"))
            continue
        # Already effective: record that this bundle relies on it, but do not
        # take ownership — only an app-issued grant is ever revoked.
        if own is None:
            _commit_row(
                session,
                GenieGrantLedger(
                    bundle_id=bundle_id,
                    space_id=bundle.space_id,
                    principal=principal,
                    securable_type=ev.key.securable_type,
                    full_name=ev.key.full_name,
                    privilege=ev.key.privilege,
                    app_granted=False,
                    granted_by=actor,
                ),
            )
            result.adopted.append(_grant_out(ev.key, ev.status, ev.detail))

    if revoke_stale:
        _release_rows(ws, session, plan.stale_rows, result)

    _finish(result)
    return result


# ─── Release (revoke) ────────────────────────────────────────────────────────


def _revoke(ws: WorkspaceClient, principal: str, key: GrantKey, result: GenieBundleActionOut) -> None:
    """Targeted revoke of one app-issued privilege. Raises UCGrantError."""
    if key.securable_type == GENIE_SPACE:
        try:
            level = _space_direct_level(ws, key.full_name, principal)
            if level is None:
                return
            if level != key.privilege:
                # Someone changed the group's level after the app granted it;
                # that is no longer the app's grant, so leave it alone.
                result.warnings.append(
                    f"'{principal}' now holds {level} on Genie space "
                    f"'{key.full_name}' (not the app-granted {key.privilege}); left in place."
                )
                return
            apply_group_acl(
                ws,
                ResourceType.GENIE_SPACES.value,
                key.full_name,
                {principal: PermissionLevel.NO_PERMISSIONS.value},
            )
        except NotFound:
            return  # space is gone; nothing to revoke
        except Exception as e:
            raise UCGrantError(
                _space_error(e, "revoke CAN_RUN", key.full_name),
                permission_denied=isinstance(e, PermissionDenied),
            ) from e
        return
    try:
        remove_privileges(ws, key.securable_type, key.full_name, principal, [key.privilege])
    except UCGrantError as e:
        if e.not_found:
            return  # securable was dropped; the grant went with it
        raise


def _release_rows(
    ws: WorkspaceClient,
    session: Session,
    rows: list[GenieGrantLedger],
    result: GenieBundleActionOut,
) -> None:
    """Drop ledger rows, revoking a privilege only when the app granted it and
    no other bundle still relies on it. A row whose revoke fails is KEPT so the
    operation can be retried."""
    for row in sorted(rows, key=lambda r: -_TYPE_ORDER.get(r.securable_type, 9)):
        key = _key_of(row)
        others = [
            r for r in _ledger_for_grant(session, row.principal, key) if r.bundle_id != row.bundle_id
        ]
        if others:
            if row.app_granted and not any(o.app_granted for o in others):
                # Hand ownership of the app-issued grant to a remaining bundle.
                others[0].app_granted = True
                session.add(others[0])
            session.delete(row)
            session.commit()
            result.released.append(
                _grant_out(key, "released", f"still needed by bundle {others[0].bundle_id}")
            )
            continue
        if row.app_granted:
            try:
                _revoke(ws, row.principal, key, result)
            except UCGrantError as e:
                logger.error(f"Genie bundle {row.bundle_id}: {e}")
                result.errors.append(str(e))
                continue
            session.delete(row)
            session.commit()
            result.revoked.append(_grant_out(key, "revoked"))
        else:
            session.delete(row)
            session.commit()
            result.released.append(
                _grant_out(key, "released", "pre-existing grant; not created by the app, left in place")
            )


def delete_bundle(
    ws: WorkspaceClient, session: Session, bundle: GenieAccessBundle
) -> GenieBundleActionOut:
    """Release every ledger row (same safe-revoke rule), then delete the bundle.

    If any revoke fails the bundle and the failed rows are kept, so nothing the
    app granted is ever orphaned without a ledger record.
    """
    result = GenieBundleActionOut(bundle_id=bundle.id or 0, action="delete", status="ok")
    _release_rows(ws, session, _ledger_for_bundle(session, bundle.id or 0), result)
    if not result.errors:
        session.delete(bundle)
        session.commit()
        result.deleted = True
    _finish(result)
    return result


def _finish(result: GenieBundleActionOut) -> None:
    if result.errors:
        done = result.granted or result.revoked or result.released or result.adopted
        result.status = "partial" if done else "failed"
