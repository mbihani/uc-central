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
import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select, text

from .logger import logger
from .models import (
    LEDGER_CONFIRMED,
    LEDGER_PENDING,
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


# ─── Group identity ──────────────────────────────────────────────────────────


def resolve_bundle_group(
    ws: WorkspaceClient,
    session: Session,
    bundle: GenieAccessBundle,
    *,
    persist_rename: bool,
) -> GenieGroupCheckOut:
    """Resolve the bundle's group by its immutable SCIM id — never by name.

    ``persist_rename=False`` (preview) is WRITE-FREE: a detected rename is only
    reported (``renamed_from``). Mutating paths (apply / resync / delete /
    membership) pass True and the rename is propagated to the app's records.

    Run before every preview / apply / resync / delete / membership operation:

      * the id must still exist and still be an ACCOUNT group;
      * its CURRENT display name is what UC grants are issued to; if the group
        was renamed, the bundle and its ledger rows are updated in place;
      * if the id is gone the bundle is blocked — even if a new group with the
        same name exists, because that is a different principal and granting
        to (or revoking from) it would act on someone else's access.
    """
    def blocked(error: str) -> GenieGroupCheckOut:
        return GenieGroupCheckOut(
            group_name=bundle.group_name,
            group_id=bundle.group_id or None,
            exists=False,
            is_account_group=False,
            error=error,
        )

    if not bundle.group_id:
        return blocked("This bundle has no stored group id; delete it and create a new bundle.")
    try:
        g = ws.groups.get(id=bundle.group_id)
    except NotFound:
        return blocked(
            (
                f"The account group this bundle was created for (id "
                f"{bundle.group_id}, last known as '{bundle.group_name}') no longer "
                f"exists. UC Central will not grant to, or revoke from, a different "
                f"group — even one recreated with the same name. Delete this bundle "
                f"with 'forget orphaned' (drops its records without touching the "
                f"workspace), then create a new bundle for the new group."
            ),
        )
    except Exception as e:
        logger.error(f"Could not look up group id {bundle.group_id}: {e}")
        return blocked(f"Could not look up the bundle's group ({type(e).__name__}).")

    resource_type = g.meta.resource_type if g.meta else None
    current = g.display_name or bundle.group_name
    if resource_type != ACCOUNT_GROUP_RESOURCE_TYPE:
        return GenieGroupCheckOut(
            group_name=current,
            group_id=bundle.group_id,
            exists=True,
            is_account_group=False,
            resource_type=resource_type,
            error=(
                f"Group '{current}' (id {bundle.group_id}) is "
                + (
                    "a workspace-local group"
                    if resource_type == WORKSPACE_GROUP_RESOURCE_TYPE
                    else f"not a confirmed account-level group (SCIM resourceType={resource_type!r})"
                )
                + "; Unity Catalog can only grant to ACCOUNT-level groups."
            ),
        )
    renamed_from = bundle.group_name if current != bundle.group_name else None
    if renamed_from is not None and persist_rename:
        _rename_principal(session, bundle, current)
    return GenieGroupCheckOut(
        group_name=current,
        group_id=bundle.group_id,
        exists=True,
        is_account_group=True,
        resource_type=resource_type,
        renamed_from=renamed_from,
    )


def _rename_principal(session: Session, bundle: GenieAccessBundle, new_name: str) -> None:
    """The group (same id) was renamed: UC grants follow the principal, so only
    the app's records need updating — every bundle on that group id, and every
    ledger row that names the old principal for those bundles."""
    old_name = bundle.group_name
    bundles = session.exec(
        select(GenieAccessBundle).where(GenieAccessBundle.group_id == bundle.group_id)
    ).all()
    ids = [b.id for b in bundles if b.id is not None]
    for b in bundles:
        b.group_name = new_name
        session.add(b)
    rows = session.exec(
        select(GenieGrantLedger).where(
            col(GenieGrantLedger.bundle_id).in_(ids),
            GenieGrantLedger.principal == old_name,
        )
    ).all()
    for r in rows:
        r.principal = new_name
        session.add(r)
    session.commit()
    logger.info(
        f"Group id {bundle.group_id} renamed '{old_name}' -> '{new_name}'; updated "
        f"{len(bundles)} bundle(s) and {len(rows)} ledger row(s)"
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


# ─── Per-grant serialization ─────────────────────────────────────────────────
#
# Every ledger decision about ONE grant — "does anyone else still rely on it?",
# deleting/adding a row, and the revoke/grant decision that follows — runs while
# holding a lock keyed on (principal, securable_type, full_name, privilege).
# Two bundles sharing a table / schema / catalog grant that are deleted or
# resynced concurrently therefore can never both see the other's row, both
# drop their own, and orphan an app-created grant.

_LOCAL_LOCKS: dict[int, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def grant_lock_key(principal: str, key: GrantKey) -> int:
    """Stable signed 64-bit key for ``pg_advisory_lock`` (same on every
    worker/process, unlike ``hash()``)."""
    raw = "\x1f".join(
        ["genie-grant", principal, key.securable_type, key.full_name, key.privilege]
    ).encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big", signed=True)


_HELD_LOCKS = "genie_grant_locks_held"


@contextmanager
def _grant_lock(session: Session, principal: str, key: GrantKey) -> Iterator[None]:
    """Hold the per-grant lock across the WHOLE block — including commits and
    workspace calls inside it.

    Postgres (production): a SESSION-level ``pg_advisory_lock`` taken on a
    DEDICATED pooled connection (not the ORM session's), so the body can commit
    as often as it needs to (e.g. pending row -> grant call -> confirm) while
    the lock stays held. ``pg_advisory_unlock`` always runs in ``finally``; if
    the unlock itself fails the lock connection is invalidated (its backend is
    closed, which releases the lock) instead of going back to the pool still
    holding it. Other dialects (SQLite in tests / local dev): a process-local
    lock with the same scope.

    Deadlock safety: an operation holds at most one grant lock at a time today;
    if a caller ever nests them, keys must be taken in ascending order, which is
    enforced here (RuntimeError otherwise).
    """
    k = grant_lock_key(principal, key)
    held: list[int] = session.info.setdefault(_HELD_LOCKS, [])
    if held and k <= max(held):
        raise RuntimeError("Grant locks must be acquired in ascending key order.")
    if session.in_transaction():
        # Start the locked section on a fresh transaction (callers have already
        # committed their work; this just ends any read-only snapshot).
        session.commit()
    held.append(k)
    try:
        engine = session.get_bind().engine
        if engine.dialect.name == "postgresql":
            with engine.connect() as lock_conn:
                lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": k})
                lock_conn.commit()
                try:
                    yield
                finally:
                    if session.in_transaction():
                        session.rollback()
                    try:
                        lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": k})
                        lock_conn.commit()
                    except Exception:
                        lock_conn.invalidate()
                        raise
            return
        with _LOCAL_LOCKS_GUARD:
            lock = _LOCAL_LOCKS.setdefault(k, threading.Lock())
        with lock:
            try:
                yield
            finally:
                if session.in_transaction():
                    session.rollback()
    finally:
        held.remove(k)


# ─── Ledger helpers ──────────────────────────────────────────────────────────

LEDGER_UNIQUE = "uq_genie_grant_ledger_bundle_grant"


def is_unique_violation(exc: IntegrityError, constraint: str, table: str) -> bool:
    """True only for the specific UNIQUE conflict we expect — never for other
    integrity errors (NOT NULL, FK, a different constraint), which must surface.
    Postgres/psycopg reports the constraint (or unique index) name; SQLite only
    names the table's columns."""
    orig = exc.orig
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None) if diag is not None else None
    if name:
        return name == constraint
    msg = str(orig)
    return msg.startswith("UNIQUE constraint failed") and f" {table}." in msg


def _key_of(row: GenieGrantLedger) -> GrantKey:
    return GrantKey(row.securable_type, row.full_name, row.privilege)


def _ledger_for_bundle(session: Session, bundle_id: int) -> list[GenieGrantLedger]:
    return list(
        session.exec(
            select(GenieGrantLedger)
            .where(GenieGrantLedger.bundle_id == bundle_id)
            .execution_options(populate_existing=True)
        ).all()
    )


def _ledger_for_grant(
    session: Session, principal: str, key: GrantKey
) -> list[GenieGrantLedger]:
    # populate_existing: inside a lock we must see what other workers committed,
    # not attribute values cached in this session's identity map.
    return list(
        session.exec(
            select(GenieGrantLedger)
            .where(
                GenieGrantLedger.principal == principal,
                GenieGrantLedger.securable_type == key.securable_type,
                GenieGrantLedger.full_name == key.full_name,
                GenieGrantLedger.privilege == key.privilege,
            )
            .execution_options(populate_existing=True)
        ).all()
    )


def _own_row(
    session: Session, bundle_id: int, principal: str, key: GrantKey
) -> GenieGrantLedger | None:
    return next(
        (r for r in _ledger_for_grant(session, principal, key) if r.bundle_id == bundle_id),
        None,
    )


def _commit(session: Session) -> None:
    """Commit; an expected (bundle, grant) UNIQUE conflict means the row is
    already recorded and is tolerated — every other integrity error raises."""
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        if not is_unique_violation(e, LEDGER_UNIQUE, GenieGrantLedger.__tablename__):
            raise


def _grant_out(key: GrantKey, status: str, detail: str | None = None) -> GenieGrantOut:
    return GenieGrantOut(
        securable_type=key.securable_type,
        full_name=key.full_name,
        privilege=key.privilege,
        status=status,
        detail=detail,
    )


# ─── Presence ────────────────────────────────────────────────────────────────


def _is_present(
    ws: WorkspaceClient,
    principal: str,
    key: GrantKey,
    cache: dict[tuple[str, str], list] | None = None,
) -> tuple[bool, str | None]:
    """Whether ``principal`` already effectively holds ``key``. Raises UCGrantError."""
    if key.securable_type == GENIE_SPACE:
        try:
            level = _space_direct_level(ws, key.full_name, principal)
        except Exception as e:
            raise UCGrantError(
                _space_error(e, "read the ACL", key.full_name),
                permission_denied=isinstance(e, PermissionDenied),
                not_found=isinstance(e, NotFound),
            ) from e
        return level in _SPACE_SATISFYING, (f"group holds {level}" if level else None)
    ck = (key.securable_type, key.full_name)
    if cache is not None and ck in cache:
        privs = cache[ck]
    else:
        privs = effective_privileges(ws, key.securable_type, key.full_name, principal)
        if cache is not None:
            cache[ck] = privs
    hit = find_effective(privs, key.privilege)
    if hit is None:
        return False, None
    parts = [hit.privilege]
    if hit.inherited_from:
        parts.append(f"inherited from {hit.inherited_from}")
    if hit.via_principal and hit.via_principal != principal:
        parts.append(f"via {hit.via_principal}")
    return True, ", ".join(parts)


# ─── Plan ────────────────────────────────────────────────────────────────────


def build_plan(
    ws: WorkspaceClient,
    session: Session,
    bundle: GenieAccessBundle,
    *,
    persist_rename: bool,
) -> BundlePlan:
    """Compute everything the preview shows. Never writes to the workspace.

    With ``persist_rename=False`` (preview) it performs NO database writes
    either: a group rename is detected and reported, and the ledger is read
    under the stored (old) principal name. apply / resync pass True so the
    rename is propagated before anything is granted.
    """
    plan = BundlePlan(space_title=bundle.space_title)

    # Captured BEFORE resolution: the name the ledger rows currently carry.
    ledger_principal = bundle.group_name
    plan.group = resolve_bundle_group(ws, session, bundle, persist_rename=persist_rename)
    if plan.group.error:
        plan.blockers.append(plan.group.error)
    principal = plan.group.group_name  # CURRENT name: what UC grants use
    if persist_rename:
        ledger_principal = principal
    if plan.group.renamed_from and not persist_rename:
        plan.warnings.append(
            f"Group id {bundle.group_id} was renamed '{plan.group.renamed_from}' -> "
            f"'{principal}'. Grants follow the group; UC Central's records are updated "
            f"on the next apply / resync / delete / membership change."
        )

    serialized: str | None = None
    try:
        space = ws.genie.get_space(bundle.space_id, include_serialized_space=True)
        plan.space_title = space.title or bundle.space_title
        plan.warehouse_id = space.warehouse_id
        serialized = space.serialized_space
        refs = parse_serialized_space(serialized)
        space_read = True
    except ValueError as e:
        plan.blockers.append(f"Could not parse the Genie space definition: {e}")
        refs, space_read = [], False
    except PermissionDenied:
        plan.blockers.append(
            f"The app service principal cannot read Genie space '{bundle.space_id}' "
            f"(it needs CAN_MANAGE on the space to read its definition and set its ACL)."
        )
        refs, space_read = [], False
    except NotFound:
        plan.blockers.append(f"Genie space '{bundle.space_id}' was not found.")
        refs, space_read = [], False

    plan.objects = resolve_objects(ws, refs)
    if space_read and not any(o.resolved for o in plan.objects):
        # CAN_RUN on a space whose tables the group cannot read is useless, and
        # an empty plan usually means the definition format was not understood.
        if not serialized:
            reason = "the space definition (serialized_space) was not returned"
        elif not refs:
            reason = (
                "no data sources were found (data_sources is absent or empty, or "
                "the definition format is not recognized)"
            )
        else:
            reason = "none of the space's data sources could be resolved"
        plan.blockers.append(f"Nothing to grant: {reason}.")
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
            f"'{principal}' see the intended rows/columns."
        )
    if plan.warehouse_id:
        plan.warnings.append(
            f"The space runs on SQL warehouse '{plan.warehouse_id}'. Warehouse "
            f"access is not managed by the bundle."
        )
    mapped = session.exec(
        select(PersonaGroupMapping).where(PersonaGroupMapping.group_name == principal)
    ).first()
    if mapped:
        plan.warnings.append(
            f"'{principal}' is also mapped to persona '{mapped.persona}'. "
            f"Applying that persona rewrites this group's Genie-space ACL; use a "
            f"dedicated group for the bundle."
        )

    required = required_grants(bundle.space_id, plan.objects)
    own_rows = _ledger_for_bundle(session, bundle.id or 0)
    pending = [r for r in own_rows if r.state == LEDGER_PENDING]
    if pending:
        plan.warnings.append(
            f"{len(pending)} grant(s) from an interrupted apply are PENDING; the next "
            f"apply/resync/delete reconciles them against the workspace."
        )
    required_set = set(required)
    plan.stale_rows = [r for r in own_rows if _key_of(r) not in required_set]

    if plan.group.error:
        # Never look up (or later act on) grants for an unverified principal.
        plan.evaluated = [_Evaluated(k, "missing") for k in required]
        return plan

    cache: dict[tuple[str, str], list] = {}
    for key in required:
        try:
            present, detail = _is_present(ws, principal, key, cache)
        except UCGrantError as e:
            plan.evaluated.append(_Evaluated(key, "error", str(e)))
            continue
        if not present:
            plan.evaluated.append(_Evaluated(key, "missing", detail))
        elif any(r.app_granted for r in _ledger_for_grant(session, ledger_principal, key)):
            plan.evaluated.append(_Evaluated(key, "managed", detail))
        else:
            plan.evaluated.append(_Evaluated(key, "present", detail))
    return plan


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


def reconcile_pending(
    ws: WorkspaceClient,
    session: Session,
    bundle: GenieAccessBundle,
    principal: str,
    result: GenieBundleActionOut,
) -> None:
    """Resolve PENDING rows left behind by an apply that died mid-grant.

    Every apply holds the per-grant lock from writing the pending row through
    the workspace call to the confirm/rollback, and this function takes the
    same lock — so it can never touch a row whose grant is still in flight. A
    pending row seen here therefore means the process crashed (or its DB
    connection died) somewhere inside that window, and the outcome is
    ambiguous: the app may or may not have issued the grant, and someone else
    could have granted the same privilege meanwhile.

    The rule is CONSERVATIVE:
      * privilege effective now -> keep the row, CONFIRMED, with its
        ``app_granted`` flag UNCHANGED. Pending rows are written with the flag
        the row had before the attempt (False for a new row), so an ambiguous
        grant is recorded as "needed" but is NEVER auto-revoked. It is reported
        so an operator can review it.
      * privilege not effective -> the grant never happened: DROP the row (the
        apply that follows grants it normally).
      * workspace unreadable -> leave it pending and report the error.
    """
    for row in _ledger_for_bundle(session, bundle.id or 0):
        if row.state != LEDGER_PENDING:
            continue
        key = _key_of(row)
        try:
            with _grant_lock(session, principal, key):
                own = _own_row(session, bundle.id or 0, principal, key)
                if own is None or own.state != LEDGER_PENDING:
                    session.commit()
                    continue
                present, _ = _is_present(ws, principal, key)
                if present:
                    own.state = LEDGER_CONFIRMED
                    session.add(own)
                    session.commit()
                    detail = (
                        "in effect; app-granted before this attempt, stays revocable"
                        if own.app_granted
                        else "in effect but the app cannot prove it issued it after a "
                        "crash: recorded as needed, NOT app-granted (never auto-revoked)"
                    )
                    result.reconciled.append(_grant_out(key, "confirmed", detail))
                    if not own.app_granted:
                        result.warnings.append(
                            f"Review {key.privilege} on {key.securable_type} "
                            f"'{key.full_name}' for '{principal}': an interrupted apply may "
                            f"have granted it; UC Central will not revoke it automatically."
                        )
                else:
                    session.delete(own)
                    session.commit()
                    result.reconciled.append(_grant_out(key, "dropped", "pending grant was never applied"))
        except UCGrantError as e:
            result.errors.append(f"Could not reconcile pending grant: {e}")


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

    Each grant runs under ONE continuous per-grant lock span: fresh presence
    check -> durable PENDING row (committed) -> workspace grant -> CONFIRMED
    (``app_granted=True``) or rolled back. Delete / resync / reconcile take the
    same lock, so none of them can act on a pending row while its grant is in
    flight. A crash inside the span leaves a pending row for
    ``reconcile_pending``. Idempotent: a second run makes no grant calls. With
    ``revoke_stale`` (resync) rows no longer needed are released.
    """
    result = GenieBundleActionOut(
        bundle_id=bundle.id or 0,
        action="resync" if revoke_stale else "apply",
        status="ok",
        warnings=list(plan.warnings),
    )
    assert plan.group is not None and not plan.group.error
    principal = plan.group.group_name
    bundle_id = bundle.id or 0

    reconcile_pending(ws, session, bundle, principal, result)

    for ev in plan.evaluated:
        if ev.status == "error":
            result.errors.append(ev.detail or f"Could not evaluate {ev.key}.")
            continue
        key = ev.key
        with _grant_lock(session, principal, key):
            own = _own_row(session, bundle_id, principal, key)
            try:
                present, detail = _is_present(ws, principal, key)
            except UCGrantError as e:
                result.errors.append(str(e))
                continue
            if present:
                # Already effective: record that this bundle relies on it,
                # without taking ownership — only app-issued grants are revoked.
                if own is None:
                    session.add(_new_row(bundle, principal, key, actor, app_granted=False))
                    result.adopted.append(_grant_out(key, ev.status, detail))
                _commit(session)
                continue

            previous = (own.app_granted, own.state) if own is not None else None
            if own is None:
                # app_granted stays False until the grant is CONFIRMED below, so
                # a crash can never leave an unproven row marked app-granted.
                own = _new_row(bundle, principal, key, actor, app_granted=False)
            own.state = LEDGER_PENDING
            session.add(own)
            _commit(session)  # durable BEFORE the workspace call
            row_id = own.id
            if row_id is None:
                # The expected UNIQUE conflict: another run recorded this exact
                # row first. Never grant without our own durable record.
                result.errors.append(
                    f"Grant {key.privilege} on {key.securable_type} '{key.full_name}' "
                    f"was recorded concurrently by another run; re-run apply."
                )
                continue

            granted_error: UCGrantError | None = None
            try:
                _grant(ws, principal, key)
            except UCGrantError as e:
                logger.error(f"Genie bundle {bundle_id}: {e}")
                granted_error = e

            row = session.get(GenieGrantLedger, row_id, populate_existing=True)
            if row is not None:
                if granted_error is None:
                    row.state = LEDGER_CONFIRMED
                    row.app_granted = True  # proven: this process just issued it
                    session.add(row)
                elif previous is not None:
                    row.app_granted, row.state = previous
                    session.add(row)
                else:
                    session.delete(row)
            session.commit()
        if granted_error is None:
            result.granted.append(_grant_out(key, "granted"))
        else:
            result.errors.append(str(granted_error))

    if revoke_stale:
        _release_rows(ws, session, plan.stale_rows, result)

    _finish(result)
    return result


def _new_row(
    bundle: GenieAccessBundle, principal: str, key: GrantKey, actor: str, *, app_granted: bool
) -> GenieGrantLedger:
    return GenieGrantLedger(
        bundle_id=bundle.id or 0,
        space_id=bundle.space_id,
        principal=principal,
        securable_type=key.securable_type,
        full_name=key.full_name,
        privilege=key.privilege,
        app_granted=app_granted,
        state=LEDGER_CONFIRMED,
        granted_by=actor,
    )


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
    no other bundle still relies on it.

    The decision for each grant is ATOMIC under its per-grant lock: re-read the
    other bundles' rows, drop this row, and revoke (if this was the last
    app-granted user) — all in one transaction, so two bundles released
    concurrently can never both conclude "the other one still needs it". A
    PENDING row from another bundle counts as "still needed". A row whose revoke
    fails is KEPT (the transaction rolls back) so the operation can be retried.
    """
    plan = [(r.id, r.bundle_id, r.principal, _key_of(r)) for r in rows]
    plan.sort(key=lambda t: -_TYPE_ORDER.get(t[3].securable_type, 9))
    for row_id, bundle_id, principal, key in plan:
        with _grant_lock(session, principal, key):
            row = session.get(GenieGrantLedger, row_id, populate_existing=True)
            if row is None:
                session.commit()
                continue
            others = [r for r in _ledger_for_grant(session, principal, key) if r.bundle_id != bundle_id]
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
                    _revoke(ws, principal, key, result)
                except UCGrantError as e:
                    logger.error(f"Genie bundle {bundle_id}: {e}")
                    result.errors.append(str(e))
                    session.rollback()
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


class BundleBlocked(Exception):
    """The bundle's group can no longer be verified; nothing may be changed."""


def delete_bundle(
    ws: WorkspaceClient,
    session: Session,
    bundle: GenieAccessBundle,
    *,
    forget_orphaned: bool = False,
) -> GenieBundleActionOut:
    """Release every ledger row (same safe-revoke rule), then delete the bundle.

    Blocked (``BundleBlocked``) when the group id no longer resolves to an
    account group: revoking by name could hit a same-named replacement group.
    If any revoke fails the bundle and the failed rows are kept, so nothing the
    app granted is ever orphaned without a ledger record.

    ``forget_orphaned`` is the operator escape hatch for a bundle whose group
    was DELETED (by-id lookup returns NotFound — never on a mere lookup error):
    the ledger rows and the bundle are dropped WITHOUT any workspace call, so
    the space can get a new bundle. The deleted principal's privileges cannot
    be revoked by name safely; they are listed in the result for audit.
    """
    group = resolve_bundle_group(ws, session, bundle, persist_rename=True)
    if group.error and forget_orphaned:
        return _forget_orphaned(ws, session, bundle)
    if group.error:
        raise BundleBlocked(group.error)
    result = GenieBundleActionOut(bundle_id=bundle.id or 0, action="delete", status="ok")
    reconcile_pending(ws, session, bundle, group.group_name, result)
    _release_rows(ws, session, _ledger_for_bundle(session, bundle.id or 0), result)
    if not result.errors and not _ledger_for_bundle(session, bundle.id or 0):
        session.delete(bundle)
        session.commit()
        result.deleted = True
    _finish(result)
    return result


def _forget_orphaned(
    ws: WorkspaceClient, session: Session, bundle: GenieAccessBundle
) -> GenieBundleActionOut:
    try:
        ws.groups.get(id=bundle.group_id)
    except NotFound:
        pass
    else:
        raise BundleBlocked(
            "forget_orphaned is only allowed when the bundle's group no longer exists."
        )
    result = GenieBundleActionOut(bundle_id=bundle.id or 0, action="delete", status="ok")
    for row in _ledger_for_bundle(session, bundle.id or 0):
        result.released.append(
            _grant_out(
                _key_of(row),
                "forgotten",
                f"group id {bundle.group_id} was deleted; record dropped, nothing revoked",
            )
        )
        session.delete(row)
    session.delete(bundle)
    session.commit()
    result.deleted = True
    result.warnings.append(
        f"Bundle records for deleted group id {bundle.group_id} ('{bundle.group_name}') "
        f"were dropped without revoking anything."
    )
    return result


def _finish(result: GenieBundleActionOut) -> None:
    if result.errors:
        done = (
            result.granted
            or result.revoked
            or result.released
            or result.adopted
            or result.reconciled
        )
        result.status = "partial" if done else "failed"
