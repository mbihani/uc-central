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
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, text

from .logger import logger
from .models import (
    GENIE_LEDGER_SOURCE_UNIQUE,
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


BUNDLE_SOURCE_PREFIX = "bundle:"
PERSONA_SOURCE_PREFIX = "persona:"


@dataclass(frozen=True)
class GrantSource:
    """WHY a set of UC grants is needed — the unit of ledger refcounting.

    ``bundle:<id>`` for a Genie access bundle, ``persona:<key>:space:<id>`` for
    a persona whose Genie level on that space reaches the table-grant
    threshold for a group. Two sources for the same group id can rely on the
    same grant; it is only revoked when the LAST source releases it (and only
    if the app created it).
    """

    key: str
    group_id: str
    space_id: str
    bundle_id: int | None = None
    persona: str | None = None

    @classmethod
    def for_bundle(cls, bundle: GenieAccessBundle) -> "GrantSource":
        return cls(
            key=f"{BUNDLE_SOURCE_PREFIX}{bundle.id or 0}",
            group_id=bundle.group_id,
            space_id=bundle.space_id,
            bundle_id=bundle.id,
        )

    @classmethod
    def for_persona(cls, persona: str, group_id: str, space_id: str) -> "GrantSource":
        return cls(
            key=f"{PERSONA_SOURCE_PREFIX}{persona}:space:{space_id}",
            group_id=group_id,
            space_id=space_id,
            persona=persona,
        )


def _as_source(owner: "GenieAccessBundle | GrantSource") -> GrantSource:
    return owner if isinstance(owner, GrantSource) else GrantSource.for_bundle(owner)


def describe_source(source_key: str) -> str:
    if source_key.startswith(BUNDLE_SOURCE_PREFIX):
        return f"bundle {source_key[len(BUNDLE_SOURCE_PREFIX):]}"
    if source_key.startswith(PERSONA_SOURCE_PREFIX):
        persona, _, space = source_key[len(PERSONA_SOURCE_PREFIX):].partition(":space:")
        return f"persona '{persona}' (space {space})"
    return source_key


@dataclass
class SpaceDiscovery:
    """A Genie space's data sources, resolved — shared by bundles and personas."""

    space_id: str
    title: str = ""
    warehouse_id: str | None = None
    objects: list[GenieSpaceObjectOut] = field(default_factory=list)
    # Non-empty => nothing may be granted for this space (and no access that
    # would need table grants may be given).
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def table_keys(self) -> list[GrantKey]:
        """UC grants the space needs (parents first), WITHOUT the space ACL."""
        return [k for k in required_grants(self.space_id, self.objects) if k.securable_type != GENIE_SPACE]


def discover_space(ws: WorkspaceClient, space_id: str, fallback_title: str = "") -> SpaceDiscovery:
    """Read a space's definition and resolve its data sources. Read-only."""
    d = SpaceDiscovery(space_id=space_id, title=fallback_title)
    serialized: str | None = None
    try:
        space = ws.genie.get_space(space_id, include_serialized_space=True)
        d.title = space.title or fallback_title
        d.warehouse_id = space.warehouse_id
        serialized = space.serialized_space
        refs = parse_serialized_space(serialized)
        space_read = True
    except ValueError as e:
        d.blockers.append(f"Could not parse the Genie space definition: {e}")
        refs, space_read = [], False
    except PermissionDenied:
        d.blockers.append(
            f"The app service principal cannot read Genie space '{space_id}' "
            f"(it needs CAN_MANAGE on the space to read its definition and set its ACL)."
        )
        refs, space_read = [], False
    except NotFound:
        d.blockers.append(f"Genie space '{space_id}' was not found.")
        refs, space_read = [], False

    d.objects = resolve_objects(ws, refs)
    if space_read and not any(o.resolved for o in d.objects):
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
        d.blockers.append(f"Nothing to grant: {reason}.")
    for o in d.objects:
        if not o.resolved:
            d.warnings.append(
                f"Data source '{o.identifier}' could not be resolved and will NOT "
                f"be granted: {o.reason}"
            )
    return d


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
    ledger row of that group id.

    Each ledger row is updated while holding ITS per-grant lock (keyed on the
    group id), so a rename can never interleave with an in-flight grant on the
    same row. The ledger name is informational only: identity, locking and
    sharing all use ``group_id``, and every UC call re-resolves the current
    name under the lock (``_current_principal``).
    """
    old_name = bundle.group_name
    gid = bundle.group_id
    bundles = session.exec(
        select(GenieAccessBundle).where(GenieAccessBundle.group_id == gid)
    ).all()
    for b in bundles:
        b.group_name = new_name
        session.add(b)
    session.commit()
    keys = sorted(
        {_key_of(r) for r in _ledger_for_group(session, gid)},
        key=lambda k: grant_lock_key(gid, k),
    )
    updated = 0
    for key in keys:
        with _grant_lock(session, gid, key):
            for r in _ledger_for_grant(session, gid, key):
                if r.principal != new_name:
                    r.principal = new_name
                    session.add(r)
                    updated += 1
            session.commit()
    logger.info(
        f"Group id {gid} renamed '{old_name}' -> '{new_name}'; updated "
        f"{len(bundles)} bundle(s) and {updated} ledger row(s)"
    )


def _current_principal(ws: WorkspaceClient, group_id: str) -> str:
    """The group's CURRENT display name, looked up by its immutable id.

    Called inside each per-grant lock span right before a UC / ACL call, so a
    grant or revoke is always addressed to the group the bundle was created
    for — never to whatever group now carries an old name. Raises UCGrantError
    if the id is gone or is no longer an account group.
    """
    try:
        g = ws.groups.get(id=group_id)
    except NotFound as e:
        raise UCGrantError(
            f"Group id {group_id} no longer exists; nothing was granted or revoked.",
            not_found=True,
        ) from e
    except Exception as e:
        raise UCGrantError(f"Could not look up group id {group_id} ({type(e).__name__}).") from e
    rtype = g.meta.resource_type if g.meta else None
    if rtype != ACCOUNT_GROUP_RESOURCE_TYPE or not g.display_name:
        raise UCGrantError(
            f"Group id {group_id} is no longer an account-level group; nothing was "
            f"granted or revoked."
        )
    return g.display_name


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
# holding a lock keyed on the IMMUTABLE group id plus (securable_type,
# full_name, privilege). Keying on the id (not the mutable display name) means a
# group rename cannot let a second operation take a "different" lock for the
# same grant. Two bundles sharing a table / schema / catalog grant that are
# deleted or resynced concurrently can never both see the other's row, both
# drop their own, and orphan an app-created grant.

_LOCAL_LOCKS: dict[int, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def grant_lock_key(group_id: str, key: GrantKey) -> int:
    """Stable signed 64-bit key for ``pg_advisory_lock`` (same on every
    worker/process, unlike ``hash()``), derived from the group's immutable id."""
    raw = "\x1f".join(
        ["genie-grant", group_id, key.securable_type, key.full_name, key.privilege]
    ).encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big", signed=True)


_HELD_LOCKS = "genie_grant_locks_held"
_LOCK_CONN = "genie_grant_lock_conn"
_HELD_OP_LOCKS = "persona_group_op_locks_held"

# Operation-lock classes, in GLOBAL acquisition order. The complete lock order,
# everywhere, is:
#
#     persona locks  ->  group locks  ->  user locks  ->  per-grant locks
#
# each class in ascending key order; an operation needing several locks of one
# class takes them all at once. Persona delete: persona -> its groups ->
# grants, then (still under persona) its direct users. Persona Apply: persona
# -> its groups -> grants, then its direct users. Direct-user add / remove:
# persona -> user. Group mapping creation: persona. Mapping removal: group ->
# grants. Bundles: grants only.
# Nothing ever waits for a lock of an EARLIER class (or a smaller key of the
# same class) while holding one, so there is no deadlock; both rules are
# enforced at runtime (RuntimeError).
PERSONA_LOCK, GROUP_LOCK, USER_LOCK = 0, 1, 2


def persona_op_lock_key(persona: str) -> int:
    raw = "\x1f".join(["persona-op", persona]).encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big", signed=True)


def user_op_lock_key(user_name: str) -> int:
    """Lock key for a DIRECTLY assigned user. Keyed on ``user_name``: it is the
    ACL principal, part of ``persona_user_mapping``'s unique key and what the
    effective-level computation looks mappings up by (``user_id`` is stored
    but nothing keys on it)."""
    raw = "\x1f".join(["persona-user-op", user_name]).encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big", signed=True)


def group_op_lock_key(group_id: str) -> int:
    """Advisory-lock key for persona OPERATIONS on one group (separate
    namespace from the per-grant keys)."""
    raw = "\x1f".join(["persona-group-op", group_id]).encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big", signed=True)


@contextmanager
def operation_locks(
    session: Session,
    *,
    personas: Iterable[str] = (),
    group_ids: Iterable[str] = (),
    users: Iterable[str] = (),
) -> Iterator[None]:
    """Serialize persona operations, held across the whole block.

    * PERSONA locks: persona delete (through its final commit), mapping
      creation (which re-checks the persona exists under it) and persona Apply
      — so no mapping can be created for a persona that is being deleted.
    * GROUP locks: Apply, mapping removal, persona delete — held from the
      mapping re-read through the plan, workspace writes, ledger verification
      and mapping deletion, so Apply never acts on a stale plan after a
      concurrent removal. Keyed on the group id, not (group, persona): Apply
      recreates rows for EVERY persona of a group.
    * USER locks (direct-user assignments, keyed on ``user_name``): add,
      remove, persona Apply's per-user ACL sync and persona delete's user
      cleanup hold them across the user's mapping read, the per-user ACL
      writes and the mapping commit, so two operations on DIFFERENT personas
      of one user can never both compute from stale mappings.

    May be nested only in the global order above (e.g. a persona lock, then
    group locks inside it); never inside a grant lock. Postgres: session-level
    advisory locks on ONE pooled connection, which the ORM session and any
    nested per-grant locks share. Other dialects: process-local locks.
    """
    wanted = [(PERSONA_LOCK, k) for k in sorted({persona_op_lock_key(p) for p in personas if p})]
    wanted += [(GROUP_LOCK, k) for k in sorted({group_op_lock_key(g) for g in group_ids if g})]
    wanted += [(USER_LOCK, k) for k in sorted({user_op_lock_key(u) for u in users if u})]
    if session.info.get(_HELD_LOCKS):
        raise RuntimeError("Operation locks must be taken before any grant lock.")
    held: list[tuple[int, int]] = session.info.setdefault(_HELD_OP_LOCKS, [])
    if not wanted:
        yield
        return
    if held and wanted[0] <= max(held):
        raise RuntimeError(
            "Operation locks must follow the global order "
            "(persona -> group -> user, ascending keys)."
        )
    if session.in_transaction():
        session.commit()
    held.extend(wanted)
    try:
        engine = session.get_bind().engine
        if engine.dialect.name != "postgresql":
            with _LOCAL_LOCKS_GUARD:
                locks = [_LOCAL_LOCKS.setdefault(k, threading.Lock()) for _, k in wanted]
            with ExitStack() as stack:
                for lock in locks:
                    stack.enter_context(lock)
                try:
                    yield
                finally:
                    if session.in_transaction():
                        session.rollback()
            return

        owns_conn = _LOCK_CONN not in session.info
        if owns_conn:
            lock_conn = engine.connect()
            session.info[_LOCK_CONN] = (lock_conn, session.bind)
        else:
            lock_conn = session.info[_LOCK_CONN][0]
        acquired: list[int] = []
        try:
            for _, k in wanted:
                lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": k})
                acquired.append(k)
            lock_conn.commit()
            session.bind = lock_conn  # ORM work shares the lock connection
            yield
        finally:
            if session.in_transaction():
                session.rollback()
            try:
                if lock_conn.in_transaction():
                    lock_conn.rollback()
                for k in reversed(acquired):
                    lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": k})
                lock_conn.commit()
            except Exception:
                lock_conn.invalidate()
                raise
            finally:
                if owns_conn:
                    _, original_bind = session.info.pop(_LOCK_CONN)
                    session.bind = original_bind
                    lock_conn.close()
    finally:
        del held[len(held) - len(wanted):]


@contextmanager
def _grant_lock(session: Session, group_id: str, key: GrantKey) -> Iterator[None]:
    """Hold the per-grant lock across the WHOLE block — including commits and
    workspace calls inside it.

    Postgres (production): a SESSION-level ``pg_advisory_lock`` on a dedicated
    pooled connection, and — while it is held — the ORM session is BOUND to that
    same connection. So the body can commit as often as it needs to (pending
    row -> grant call -> confirm) while the lock stays held, and each operation
    uses exactly ONE pooled connection (no lock-connection + ORM-connection
    pair that could exhaust the pool under concurrency). ``pg_advisory_unlock``
    always runs in ``finally``; if it fails, the connection is invalidated (its
    backend closes, releasing the lock) instead of returning to the pool still
    holding it. Other dialects (SQLite in tests / local dev): a process-local
    lock with the same scope.

    Deadlock safety: an operation holds at most one grant lock at a time; if a
    caller ever nests them (same session, same connection), keys must be taken
    in ascending order, which is enforced (RuntimeError otherwise).
    """
    if not group_id:
        # An empty id would collapse every unkeyed grant onto one lock.
        raise RuntimeError("Refusing a grant lock without a group id.")
    k = grant_lock_key(group_id, key)
    held: list[int] = session.info.setdefault(_HELD_LOCKS, [])
    if held and k <= max(held):
        raise RuntimeError("Grant locks must be acquired in ascending key order.")
    if session.in_transaction():
        # Start the locked section on a fresh transaction (callers have already
        # committed their work; this just ends any read-only snapshot and
        # returns the session's own pooled connection).
        session.commit()
    # Own the lock connection only if no enclosing grant / operation lock does.
    outermost = _LOCK_CONN not in session.info
    held.append(k)
    try:
        engine = session.get_bind().engine
        if engine.dialect.name != "postgresql":
            with _LOCAL_LOCKS_GUARD:
                lock = _LOCAL_LOCKS.setdefault(k, threading.Lock())
            with lock:
                try:
                    yield
                finally:
                    if session.in_transaction():
                        session.rollback()
            return

        if outermost:
            lock_conn = engine.connect()
            session.info[_LOCK_CONN] = (lock_conn, session.bind)
        else:
            lock_conn = session.info[_LOCK_CONN][0]
        try:
            lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": k})
            lock_conn.commit()
            if outermost:
                session.bind = lock_conn  # ORM work shares the lock connection
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
        finally:
            if outermost:
                _, original_bind = session.info.pop(_LOCK_CONN)
                if session.in_transaction():
                    session.rollback()
                session.bind = original_bind
                lock_conn.close()
    finally:
        held.remove(k)


# ─── Ledger helpers ──────────────────────────────────────────────────────────

LEDGER_UNIQUE = GENIE_LEDGER_SOURCE_UNIQUE


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


def _ledger_for_source(session: Session, source_key: str) -> list[GenieGrantLedger]:
    return list(
        session.exec(
            select(GenieGrantLedger)
            .where(GenieGrantLedger.source == source_key)
            .execution_options(populate_existing=True)
        ).all()
    )


def _ledger_for_bundle(session: Session, bundle_id: int) -> list[GenieGrantLedger]:
    return _ledger_for_source(session, f"{BUNDLE_SOURCE_PREFIX}{bundle_id}")


def _ledger_for_group(session: Session, group_id: str) -> list[GenieGrantLedger]:
    return list(
        session.exec(
            select(GenieGrantLedger)
            .where(GenieGrantLedger.group_id == group_id)
            .execution_options(populate_existing=True)
        ).all()
    )


def _ledger_for_grant(
    session: Session, group_id: str, key: GrantKey
) -> list[GenieGrantLedger]:
    # populate_existing: inside a lock we must see what other workers committed,
    # not attribute values cached in this session's identity map.
    return list(
        session.exec(
            select(GenieGrantLedger)
            .where(
                GenieGrantLedger.group_id == group_id,
                GenieGrantLedger.securable_type == key.securable_type,
                GenieGrantLedger.full_name == key.full_name,
                GenieGrantLedger.privilege == key.privilege,
            )
            .execution_options(populate_existing=True)
        ).all()
    )


def _own_row(
    session: Session, source_key: str, group_id: str, key: GrantKey
) -> GenieGrantLedger | None:
    return next(
        (r for r in _ledger_for_grant(session, group_id, key) if r.source == source_key),
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
    by group id, so it is unaffected by the rename. apply / resync pass True so
    the rename is propagated to the app's records.
    """
    plan = BundlePlan(space_title=bundle.space_title)

    plan.group = resolve_bundle_group(ws, session, bundle, persist_rename=persist_rename)
    if plan.group.error:
        plan.blockers.append(plan.group.error)
    principal = plan.group.group_name  # CURRENT name: what UC grants use
    if plan.group.renamed_from and not persist_rename:
        plan.warnings.append(
            f"Group id {bundle.group_id} was renamed '{plan.group.renamed_from}' -> "
            f"'{principal}'. Grants follow the group; UC Central's records are updated "
            f"on the next apply / resync / delete / membership change."
        )

    discovery = discover_space(ws, bundle.space_id, bundle.space_title)
    plan.space_title = discovery.title
    plan.warehouse_id = discovery.warehouse_id
    plan.objects = discovery.objects
    plan.blockers.extend(discovery.blockers)
    plan.warnings.extend(discovery.warnings)
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
    # By group ID (never by display name): personas mapped to this same group.
    mapped = session.exec(
        select(PersonaGroupMapping).where(PersonaGroupMapping.group_id == bundle.group_id)
    ).all()
    if mapped:
        names = ", ".join(sorted({m.persona for m in mapped}))
        plan.warnings.append(
            f"'{principal}' is also mapped to persona(s) {names}. They stack with this "
            f"bundle: persona Apply keeps the group's Genie level on this space at the "
            f"highest of the personas and this bundle, and shared table grants are "
            f"only revoked when no persona or bundle still needs them."
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
        elif any(r.app_granted for r in _ledger_for_grant(session, bundle.group_id, key)):
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
    owner: "GenieAccessBundle | GrantSource",
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
    source = _as_source(owner)
    for row in _ledger_for_source(session, source.key):
        if row.state != LEDGER_PENDING:
            continue
        key = _key_of(row)
        try:
            with _grant_lock(session, source.group_id, key):
                own = _own_row(session, source.key, source.group_id, key)
                if own is None or own.state != LEDGER_PENDING:
                    session.commit()
                    continue
                principal = _current_principal(ws, source.group_id)
                own.principal = principal
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
    apply_source_grants(ws, session, GrantSource.for_bundle(bundle), plan.evaluated, actor, result)

    if revoke_stale:
        _release_rows(ws, session, plan.stale_rows, result)

    _finish(result)
    return result


def apply_source_grants(
    ws: WorkspaceClient,
    session: Session,
    source: GrantSource,
    evaluated: list[_Evaluated],
    actor: str,
    result: GenieBundleActionOut,
) -> None:
    """Grant each missing key for ``source`` and record every key it needs.

    The shared grant engine for bundles AND persona-driven table grants. Each
    grant runs under ONE continuous per-grant lock span (keyed on the group
    id): fresh presence check -> durable PENDING row (committed) -> workspace
    grant -> CONFIRMED (``app_granted=True``) or rolled back. Pending rows from
    an earlier crash are reconciled first. Already-effective privileges are
    recorded as needed (``app_granted=False``) and never revoked.
    """
    gid = source.group_id
    reconcile_pending(ws, session, source, result)

    for ev in evaluated:
        if ev.status == "error":
            result.errors.append(ev.detail or f"Could not evaluate {ev.key}.")
            continue
        key = ev.key
        with _grant_lock(session, gid, key):
            own = _own_row(session, source.key, gid, key)
            try:
                # Resolved by id UNDER the lock: the name UC calls must use now.
                principal = _current_principal(ws, gid)
                present, detail = _is_present(ws, principal, key)
            except UCGrantError as e:
                result.errors.append(str(e))
                continue
            if present:
                # Already effective: record that this bundle relies on it,
                # without taking ownership — only app-issued grants are revoked.
                if own is None:
                    session.add(_new_row(source, principal, key, actor, app_granted=False))
                    result.adopted.append(_grant_out(key, ev.status, detail))
                _commit(session)
                continue

            previous = (own.app_granted, own.state) if own is not None else None
            if own is None:
                # app_granted stays False until the grant is CONFIRMED below, so
                # a crash can never leave an unproven row marked app-granted.
                own = _new_row(source, principal, key, actor, app_granted=False)
            own.state = LEDGER_PENDING
            own.principal = principal
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

            _after_pending_commit(key)
            granted_error: UCGrantError | None = None
            try:
                # Re-resolve right before the call: the group may have been
                # renamed while this span held the lock (renames are external).
                principal = _current_principal(ws, gid)
                _grant(ws, principal, key)
            except UCGrantError as e:
                logger.error(f"Grant source {source.key}: {e}")
                granted_error = e

            row = session.get(GenieGrantLedger, row_id, populate_existing=True)
            if row is not None:
                if granted_error is None:
                    row.state = LEDGER_CONFIRMED
                    row.app_granted = True  # proven: this process just issued it
                    row.principal = principal
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


def _after_pending_commit(key: GrantKey) -> None:
    """No-op seam: called right after a pending row is committed and before the
    workspace grant, while the per-grant lock is held (tests pause here)."""


def _new_row(
    owner: "GenieAccessBundle | GrantSource",
    principal: str,
    key: GrantKey,
    actor: str,
    *,
    app_granted: bool,
) -> GenieGrantLedger:
    source = _as_source(owner)
    if not source.group_id:
        raise RuntimeError(
            f"Grant source {source.key} has no group id; refusing to write a ledger row."
        )
    return GenieGrantLedger(
        source=source.key,
        bundle_id=source.bundle_id,
        persona=source.persona,
        group_id=source.group_id,
        space_id=source.space_id,
        principal=principal,
        securable_type=key.securable_type,
        full_name=key.full_name,
        privilege=key.privilege,
        app_granted=app_granted,
        state=LEDGER_CONFIRMED,
        granted_by=actor,
    )


# ─── Release (revoke) ────────────────────────────────────────────────────────


def _revoke(
    ws: WorkspaceClient,
    session: Session,
    group_id: str,
    principal: str,
    key: GrantKey,
    result: GenieBundleActionOut,
) -> None:
    """Targeted revoke of one app-issued privilege. Raises UCGrantError.

    For a bundle's space ACL entry, personas of the same group still count: if
    any of them grants the space, the entry is set to the personas' highest
    level instead of being removed (stacked sources never strip each other).
    """
    if key.securable_type == GENIE_SPACE:
        from .persona_sync import persona_space_level  # avoid an import cycle

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
            remaining, personas = persona_space_level(session, group_id, key.full_name)
            if remaining != PermissionLevel.NO_PERMISSIONS.value:
                if remaining != level:
                    apply_group_acl(
                        ws, ResourceType.GENIE_SPACES.value, key.full_name, {principal: remaining}
                    )
                result.warnings.append(
                    f"'{principal}' keeps {remaining} on Genie space '{key.full_name}' "
                    f"from persona(s) {', '.join(personas)}."
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
    no other SOURCE (bundle or persona) of the same group still relies on it.

    The decision for each grant is ATOMIC under its per-grant lock: re-read the
    other bundles' rows, drop this row, and revoke (if this was the last
    app-granted user) — all in one transaction, so two bundles released
    concurrently can never both conclude "the other one still needs it". A
    PENDING row from another bundle counts as "still needed". A row whose revoke
    fails is KEPT (the transaction rolls back) so the operation can be retried.
    """
    plan = [(r.id, r.source, r.group_id, _key_of(r)) for r in rows]
    plan.sort(key=lambda t: -_TYPE_ORDER.get(t[3].securable_type, 9))
    for row_id, source_key, gid, key in plan:
        with _grant_lock(session, gid, key):
            row = session.get(GenieGrantLedger, row_id, populate_existing=True)
            if row is None:
                session.commit()
                continue
            # "Still needed" is decided by group ID (never by display name),
            # across every OTHER source — bundles and personas alike.
            others = [r for r in _ledger_for_grant(session, gid, key) if r.source != source_key]
            if others:
                if row.app_granted and not any(o.app_granted for o in others):
                    # Hand ownership of the app-issued grant to a remaining bundle.
                    others[0].app_granted = True
                    session.add(others[0])
                session.delete(row)
                session.commit()
                result.released.append(
                    _grant_out(key, "released", f"still needed by {describe_source(others[0].source)}")
                )
                continue
            if row.app_granted:
                try:
                    # Resolved by id UNDER the lock — never the stored name.
                    _revoke(ws, session, gid, _current_principal(ws, gid), key, result)
                except UCGrantError as e:
                    logger.error(f"Grant source {source_key}: {e}")
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
    reconcile_pending(ws, session, bundle, result)
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
