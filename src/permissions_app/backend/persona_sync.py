"""Stacked personas: effective levels, and Genie table grants that follow them.

Personas STACK. A group may map to several personas, and on every object its
EFFECTIVE level is the highest level any of its personas (or an applied Genie
access bundle) grants there — union / highest-wins, exactly like Databricks'
own additive ACL model. A persona's NO_PERMISSIONS means "this persona grants
nothing", never "deny": persona Apply removes a group's ACL entry only when NO
source grants anything, so applying one persona can never strip access that
another persona of the same group grants.

Genie spaces can be set per space (``PersonaResourceOverride``): a persona's
type-level cell is its "all spaces" level; an override replaces THAT persona's
level on one space (higher or lower). Stacking across personas still takes the
highest.

Genie always runs queries with the asking user's own Unity Catalog rights, so
when a group's Genie level on a space reaches ``TABLE_GRANT_THRESHOLD``, every
persona responsible for that is recorded as a grant SOURCE
(``persona:<key>:space:<id>``) and the group gets SELECT on each table / view
the space uses (READ_VOLUME on volumes) plus USE_SCHEMA / USE_CATALOG on the
parents — per table, never schema-wide. This reuses the bundle engine
(``genie_bundles``): space discovery, pending -> confirmed ledger rows,
per-grant locks keyed on the group id, pre-existing grants never revoked, and a
grant only revoked when no source (persona or bundle) of that group still
needs it.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from sqlmodel import Session, col, select

from .genie_bundles import (
    ACCOUNT_GROUP_RESOURCE_TYPE,
    BUNDLE_SOURCE_PREFIX,
    GENIE_SPACE,
    PERSONA_SOURCE_PREFIX,
    WORKSPACE_GROUP_RESOURCE_TYPE,
    GrantKey,
    GrantSource,
    SpaceDiscovery,
    _Evaluated,
    _finish,
    _is_present,
    _key_of,
    _ledger_for_grant,
    _release_rows,
    apply_source_grants,
    discover_space,
)
from .logger import logger
from .models import (
    LEDGER_CONFIRMED,
    GenieBundleActionOut,
    GenieGrantLedger,
    GroupLevelOut,
    PermissionLevel,
    PermissionTemplate,
    PersonaGroupMapping,
    PersonaResourceOverride,
    ResourceItemOut,
    ResourceType,
    TableGrantPlanOut,
    get_higher_permission,
    get_permission_rank,
    is_permission_level_allowed,
)
from .uc_grants import TABLE, UCGrantError

GENIE = ResourceType.GENIE_SPACES.value
NONE = PermissionLevel.NO_PERMISSIONS.value
# Genie's lowest real level, CAN_READ, is "Can View" in the UI. A group that can
# open a space must also be able to read its data, or the space is useless.
TABLE_GRANT_THRESHOLD = PermissionLevel.CAN_READ.value
# What an applied bundle contributes to its group's level on its space.
BUNDLE_SPACE_LEVEL = PermissionLevel.CAN_RUN.value


def _at_least(rt: str, level: str, threshold: str) -> bool:
    return level != NONE and get_permission_rank(rt, level) >= get_permission_rank(rt, threshold)


# ─── Persona cells (type-level + per-resource overrides) ─────────────────────


@dataclass
class PersonaCells:
    type_levels: dict[tuple[str, str], str] = field(default_factory=dict)
    overrides: dict[tuple[str, str, str], str] = field(default_factory=dict)

    def level(self, persona: str, resource_type: str, resource_id: str | None = None) -> str:
        """This persona's own level: the per-resource override if there is one,
        else its type-level ("all resources") cell. Invalid / stale levels count
        as granting nothing."""
        level = None
        if resource_id is not None:
            level = self.overrides.get((persona, resource_type, resource_id))
        if level is None:
            level = self.type_levels.get((persona, resource_type), NONE)
        return level if is_permission_level_allowed(resource_type, level) else NONE

    def grants_anything(self, persona: str, resource_type: str) -> bool:
        if self.type_levels.get((persona, resource_type), NONE) != NONE:
            return True
        return any(
            p == persona and rt == resource_type and lvl != NONE
            for (p, rt, _), lvl in self.overrides.items()
        )


def load_cells(session: Session, personas: Iterable[str]) -> PersonaCells:
    wanted = sorted(set(personas))
    if not wanted:
        return PersonaCells()
    templates = session.exec(
        select(PermissionTemplate).where(col(PermissionTemplate.persona).in_(wanted))
    ).all()
    overrides = session.exec(
        select(PersonaResourceOverride).where(col(PersonaResourceOverride.persona).in_(wanted))
    ).all()
    keep = set(wanted)
    return PersonaCells(
        type_levels={
            (t.persona, t.resource_type): t.permission_level for t in templates if t.persona in keep
        },
        overrides={
            (o.persona, o.resource_type, o.resource_id): o.permission_level
            for o in overrides
            if o.persona in keep
        },
    )


def personas_by_group(
    session: Session,
    group_ids: Iterable[str],
    *,
    exclude: frozenset[tuple[str, str]] = frozenset(),
) -> dict[str, list[str]]:
    """group id -> its personas (sorted), optionally excluding (group, persona)
    pairs (used to preview removing a persona from a group)."""
    gids = sorted(set(group_ids))
    out: dict[str, list[str]] = {g: [] for g in gids}
    if not gids:
        return out
    rows = session.exec(
        select(PersonaGroupMapping).where(col(PersonaGroupMapping.group_id).in_(gids))
    ).all()
    for m in rows:
        if m.group_id in out and (m.group_id, m.persona) not in exclude and m.persona not in out[m.group_id]:
            out[m.group_id].append(m.persona)
    for g in out:
        out[g].sort()
    return out


def applied_bundle_spaces(session: Session, group_ids: Iterable[str]) -> dict[tuple[str, str], list[str]]:
    """(group id, space id) -> sources of APPLIED bundles (confirmed space ACL row)."""
    gids = sorted(set(group_ids))
    if not gids:
        return {}
    rows = session.exec(
        select(GenieGrantLedger).where(
            col(GenieGrantLedger.group_id).in_(gids),
            GenieGrantLedger.securable_type == GENIE_SPACE,
            col(GenieGrantLedger.source).startswith(BUNDLE_SOURCE_PREFIX),
        )
    ).all()
    out: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        if r.state == LEDGER_CONFIRMED and r.group_id in gids:
            out.setdefault((r.group_id, r.full_name), []).append(r.source)
    return out


# ─── Merge (union / highest wins) ────────────────────────────────────────────


@dataclass
class Effective:
    level: str
    granted_by: list[str]
    source_levels: dict[str, str]
    escalation: bool = False
    escalation_detail: str | None = None


def merge(resource_type: str, source_levels: dict[str, str]) -> Effective:
    """Highest level wins; NO_PERMISSIONS sources contribute nothing.

    ``escalation`` flags stacking that RAISES the group above the best level
    any of its other granting sources gives (e.g. one persona adds CAN_MANAGE
    on top of another's CAN_RUN). A group with a single granting source is
    never flagged — that is just the persona's own level.
    """
    granting = {s: lvl for s, lvl in source_levels.items() if lvl != NONE}
    if not granting:
        return Effective(NONE, [], dict(source_levels))
    level = get_higher_permission(resource_type, *granting.values())
    granted_by = sorted(s for s, lvl in granting.items() if lvl == level)
    others = {s: lvl for s, lvl in granting.items() if s not in granted_by}
    eff = Effective(level, granted_by, dict(source_levels))
    if others:
        best_other = get_higher_permission(resource_type, *others.values())
        if get_permission_rank(resource_type, level) > get_permission_rank(resource_type, best_other):
            eff.escalation = True
            eff.escalation_detail = (
                f"Stacking raises this group from {best_other} (best of "
                f"{', '.join(sorted(others))}) to {level} via {', '.join(granted_by)}."
            )
    return eff


def group_effective(
    cells: PersonaCells,
    personas: list[str],
    resource_type: str,
    resource_id: str | None = None,
    extra: dict[str, str] | None = None,
) -> Effective:
    levels = {p: cells.level(p, resource_type, resource_id) for p in personas}
    levels.update(extra or {})
    return merge(resource_type, levels)


def persona_space_level(session: Session, group_id: str, space_id: str) -> tuple[str, list[str]]:
    """Highest Genie level the group's PERSONAS (not bundles) give on a space."""
    personas = personas_by_group(session, [group_id])[group_id]
    eff = group_effective(load_cells(session, personas), personas, GENIE, space_id)
    return eff.level, eff.granted_by


# ─── Groups (by immutable id) ────────────────────────────────────────────────


@dataclass
class GroupRef:
    group_id: str
    stored_name: str
    name: str  # CURRENT display name when it could be resolved by id
    resource_type: str | None = None
    missing: bool = False  # the id no longer exists
    lookup_error: str | None = None

    @property
    def is_account_group(self) -> bool:
        return self.resource_type == ACCOUNT_GROUP_RESOURCE_TYPE

    def genie_block_reason(self) -> str | None:
        """Why this group cannot receive Genie table grants (None = it can)."""
        if self.missing:
            return f"Group id {self.group_id} ('{self.stored_name}') no longer exists."
        if self.lookup_error:
            return self.lookup_error
        if self.resource_type == WORKSPACE_GROUP_RESOURCE_TYPE:
            return (
                f"Group '{self.name}' is a workspace-local group. Unity Catalog can only "
                f"grant table access to ACCOUNT-level groups, so its Genie access (which "
                f"needs table grants) was not changed. Use an account group."
            )
        if not self.is_account_group:
            return (
                f"Could not confirm that '{self.name}' is an account-level group "
                f"(SCIM resourceType={self.resource_type!r}); Genie access not changed."
            )
        return None


def resolve_groups(ws: WorkspaceClient, mappings: Iterable[PersonaGroupMapping]) -> dict[str, GroupRef]:
    """Look up each mapped group BY ID (never by name). Read-only."""
    out: dict[str, GroupRef] = {}
    for m in mappings:
        if m.group_id in out:
            continue
        ref = GroupRef(group_id=m.group_id, stored_name=m.group_name, name=m.group_name)
        try:
            g = ws.groups.get(id=m.group_id)
        except NotFound:
            ref.missing = True
        except Exception as e:
            logger.warning(f"Could not look up group id {m.group_id}: {e}")
            ref.lookup_error = f"Could not look up group id {m.group_id} ({type(e).__name__})."
        else:
            if isinstance(g.display_name, str) and g.display_name:
                ref.name = g.display_name
            rtype = g.meta.resource_type if g.meta else None
            ref.resource_type = rtype if isinstance(rtype, str) else None
        out[m.group_id] = ref
    return out


# ─── Genie plan ──────────────────────────────────────────────────────────────


@dataclass
class GenieEntry:
    """One group on one Genie space."""

    group_id: str
    group_name: str
    space_id: str
    space_title: str
    effective: Effective
    # Level to write for this group's ACL entry; None = leave it untouched.
    acl_level: str | None
    blocked: str | None = None
    sources: list[GrantSource] = field(default_factory=list)
    keys: list[GrantKey] = field(default_factory=list)


@dataclass
class GeniePlan:
    entries: list[GenieEntry] = field(default_factory=list)
    discoveries: dict[str, SpaceDiscovery] = field(default_factory=dict)
    stale_rows: list[GenieGrantLedger] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def acl_levels(self, space_id: str) -> dict[str, str]:
        return {
            e.group_name: e.acl_level
            for e in self.entries
            if e.space_id == space_id and e.acl_level is not None
        }

    @property
    def errors(self) -> list[str]:
        return [f"Genie space '{e.space_title}' for '{e.group_name}': {e.blocked}" for e in self.entries if e.blocked]


def plan_genie(
    ws: WorkspaceClient,
    session: Session,
    groups: dict[str, GroupRef],
    spaces: list[ResourceItemOut],
    *,
    exclude: frozenset[tuple[str, str]] = frozenset(),
) -> GeniePlan:
    """Per group and space: the effective Genie level, what to write, and the
    table grants it needs. Read-only (workspace and database)."""
    plan = GeniePlan()
    personas = personas_by_group(session, groups, exclude=exclude)
    cells = load_cells(session, [p for ps in personas.values() for p in ps])
    bundles = applied_bundle_spaces(session, groups)

    for space in spaces:
        for gid, ref in groups.items():
            extra = {b: BUNDLE_SPACE_LEVEL for b in bundles.get((gid, space.id), [])}
            eff = group_effective(cells, personas[gid], GENIE, space.id, extra)
            entry = GenieEntry(gid, ref.name, space.id, space.name, eff, acl_level=eff.level)
            plan.entries.append(entry)
            if ref.missing or ref.lookup_error:
                entry.acl_level, entry.blocked = None, ref.genie_block_reason()
                continue
            needing = [p for p in personas[gid] if _at_least(GENIE, cells.level(p, GENIE, space.id), TABLE_GRANT_THRESHOLD)]
            if not needing:
                continue  # NONE -> remove; or only a bundle grants (it owns its tables)
            reason = ref.genie_block_reason()
            if reason is None:
                d = plan.discoveries.get(space.id)
                if d is None:
                    d = plan.discoveries[space.id] = discover_space(ws, space.id, space.name)
                if d.blockers:
                    reason = " ".join(d.blockers)
            if reason is not None:
                # Least privilege: never hand out Genie access whose data cannot
                # be granted. The group's existing entry is left untouched.
                entry.acl_level, entry.blocked = None, reason
                continue
            d = plan.discoveries[space.id]
            entry.sources = [GrantSource.for_persona(p, gid, space.id) for p in needing]
            entry.keys = d.table_keys()

    _find_stale(ws, session, groups, spaces, plan)
    return plan


def _find_stale(
    ws: WorkspaceClient,
    session: Session,
    groups: dict[str, GroupRef],
    spaces: list[ResourceItemOut],
    plan: GeniePlan,
) -> None:
    """Persona-sourced ledger rows of these groups that nothing needs any more."""
    if not groups:
        return
    needed = {(src.key, k) for e in plan.entries for src in e.sources for k in e.keys}
    blocked = {(e.group_id, e.space_id) for e in plan.entries if e.blocked}
    listed = {s.id for s in spaces}
    gone: dict[str, bool] = {}
    rows = session.exec(
        select(GenieGrantLedger).where(
            col(GenieGrantLedger.group_id).in_(sorted(groups)),
            col(GenieGrantLedger.source).startswith(PERSONA_SOURCE_PREFIX),
        )
    ).all()
    for r in rows:
        if (r.source, _key_of(r)) in needed or (r.group_id, r.space_id) in blocked:
            continue
        if r.space_id not in listed:
            # Not in the listing: only treat as gone when the space is
            # CONFIRMED deleted — a failed / partial listing must never cause
            # a mass revoke.
            if r.space_id not in gone:
                try:
                    ws.genie.get_space(r.space_id)
                    gone[r.space_id] = False
                except NotFound:
                    gone[r.space_id] = True
                except Exception:
                    gone[r.space_id] = False
            if not gone[r.space_id]:
                plan.warnings.append(
                    f"Genie space {r.space_id} was not in the space listing; its table "
                    f"grants for group id {r.group_id} were kept."
                )
                continue
        plan.stale_rows.append(r)
    # One warning per space, not per row.
    plan.warnings = sorted(set(plan.warnings))


# ─── Preview rows ────────────────────────────────────────────────────────────


def genie_level_rows(plan: GeniePlan) -> list[GroupLevelOut]:
    label = "Genie Spaces"
    return [
        GroupLevelOut(
            group_id=e.group_id,
            group_name=e.group_name,
            resource_type=GENIE,
            resource_type_label=label,
            resource_id=e.space_id,
            resource_name=e.space_title,
            effective_level=e.effective.level,
            granted_by=e.effective.granted_by,
            source_levels=e.effective.source_levels,
            escalation=e.effective.escalation,
            escalation_detail=e.effective.escalation_detail,
            blocked=e.blocked,
        )
        for e in plan.entries
    ]


def table_grant_rows(ws: WorkspaceClient, session: Session, plan: GeniePlan) -> list[TableGrantPlanOut]:
    """What applying the plan would do to UC grants. Read-only."""
    out: list[TableGrantPlanOut] = []
    pending_needed: dict[tuple[str, GrantKey], set[str]] = {}
    for e in plan.entries:
        for src in e.sources:
            for k in e.keys:
                pending_needed.setdefault((e.group_id, k), set()).add(src.key)
    stale_ids = {r.id for r in plan.stale_rows}
    cache: dict[tuple[str, str], list] = {}

    def row_out(e_gid, e_name, space_id, title, source, k, action, detail=None):
        obj = None
        d = plan.discoveries.get(space_id)
        if d is not None and k.securable_type == TABLE:
            obj = next((o for o in d.objects if o.full_name == k.full_name), None)
        return TableGrantPlanOut(
            group_id=e_gid,
            group_name=e_name,
            space_id=space_id,
            space_title=title,
            source=source,
            securable_type=k.securable_type,
            full_name=k.full_name,
            privilege=k.privilege,
            action=action,
            detail=detail,
            has_row_filter=bool(obj and obj.has_row_filter),
            masked_columns=list(obj.masked_columns) if obj else [],
        )

    for e in plan.entries:
        if e.blocked:
            continue
        for src in e.sources:
            for k in e.keys:
                rows = _ledger_for_grant(session, e.group_id, k)
                own = next((r for r in rows if r.source == src.key), None)
                if own is not None:
                    out.append(row_out(e.group_id, e.group_name, e.space_id, e.space_title, src.key, k, "keep"))
                    continue
                shared = next((r for r in rows if r.id not in stale_ids), None)
                if shared is not None:
                    out.append(row_out(
                        e.group_id, e.group_name, e.space_id, e.space_title, src.key, k, "keep",
                        f"already granted for {shared.source}",
                    ))
                    continue
                try:
                    present, detail = _is_present(ws, e.group_name, k, cache)
                except UCGrantError as err:
                    out.append(row_out(e.group_id, e.group_name, e.space_id, e.space_title, src.key, k, "error", str(err)))
                    continue
                action = "present" if present else "add"
                if present:
                    detail = f"pre-existing ({detail}); recorded as needed, never revoked"
                out.append(row_out(e.group_id, e.group_name, e.space_id, e.space_title, src.key, k, action, detail))

    names = {e.group_id: e.group_name for e in plan.entries}
    titles = {e.space_id: e.space_title for e in plan.entries}
    for r in plan.stale_rows:
        k = _key_of(r)
        still = (
            any(o.id not in stale_ids and o.source != r.source for o in _ledger_for_grant(session, r.group_id, k))
            or bool(pending_needed.get((r.group_id, k)))
        )
        if r.app_granted and not still:
            action, detail = "revoke", "created by the app; no other source needs it"
        elif still:
            action, detail = "release", "another source still needs it"
        else:
            action, detail = "release", "pre-existing grant; left in place"
        out.append(TableGrantPlanOut(
            group_id=r.group_id,
            group_name=names.get(r.group_id, r.principal),
            space_id=r.space_id,
            space_title=titles.get(r.space_id, r.space_id),
            source=r.source,
            securable_type=r.securable_type,
            full_name=r.full_name,
            privilege=r.privilege,
            action=action,
            detail=detail,
        ))
    return out


# ─── Execute ─────────────────────────────────────────────────────────────────


def execute_genie_table_grants(
    ws: WorkspaceClient, session: Session, plan: GeniePlan, actor: str
) -> GenieBundleActionOut:
    """Grant (and ledger) every table grant the plan's sources need."""
    result = GenieBundleActionOut(bundle_id=0, action="persona", status="ok")
    for e in plan.entries:
        for src in e.sources:
            apply_source_grants(ws, session, src, [_Evaluated(k, "missing") for k in e.keys], actor, result)
    return result


def release_stale_table_grants(
    ws: WorkspaceClient, session: Session, plan: GeniePlan, result: GenieBundleActionOut
) -> GenieBundleActionOut:
    """Release rows nothing needs any more (refcounted, app-made only)."""
    _release_rows(ws, session, plan.stale_rows, result)
    _finish(result)
    return result
