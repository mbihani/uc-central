"""Tests for stacked personas, per-space Genie levels and persona table grants.

Reuses the stateful fake workspace from ``test_genie_bundles`` (UC grants,
Genie-space ACLs, SCIM groups) with a real SQLite (or, with
``UCG_TEST_PG_URL``, Postgres) database, and drives the real route functions.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from permissions_app.backend import persona_sync as ps
from permissions_app.backend.models import (
    LEGACY_PERSONA_GROUP_UNIQUE,
    GenieGrantLedger,
    PermissionLevel,
    PermissionOverrideIn,
    PermissionTemplate,
    PersonaDefinition,
    PersonaGroupMapping,
    PersonaResourceOverride,
    ResourceItemOut,
    ResourceType,
)
from permissions_app.backend.router import (
    apply_permissions,
    create_persona_mapping,
    delete_persona,
    delete_persona_mapping,
    preview_apply_permissions,
    set_permission_override,
)
from permissions_app.backend.models import PersonaGroupMappingIn
from permissions_app.backend.seed import ensure_persona_stacking_schema, ensure_unique_constraints
from permissions_app.tests.test_genie_bundles import (
    CAT,
    GROUP,
    PG_URL,
    FakeWorkspace,
    _apply,
    _bundle,
    _migration_engines,
    _record_writes,
    _serialized,
)

GENIE = ResourceType.GENIE_SPACES.value
CLUSTERS = ResourceType.CLUSTERS.value
NONE = PermissionLevel.NO_PERMISSIONS.value
CAN_READ = PermissionLevel.CAN_READ.value
CAN_RUN = PermissionLevel.CAN_RUN.value
CAN_MANAGE = PermissionLevel.CAN_MANAGE.value

SALES = "01f0000000000000000000000000abcd"  # == test_genie_bundles.SPACE_ID
HR = "01f00000000000000000000000000hr1"
ORDERS = f"{CAT}.sales.orders"
REVENUE = f"{CAT}.finance.revenue_metrics"  # has a row filter in the fake
LOOKUP = f"{CAT}.other.lookup"
HR_GROUP = "hr-users"


class StackWorkspace(FakeWorkspace):
    """FakeWorkspace with one definition per Genie space (Sales + HR)."""

    def __init__(self):
        super().__init__(_serialized([ORDERS, REVENUE]))
        self.space_defs = {
            SALES: ("Sales Genie", _serialized([ORDERS, REVENUE])),
            HR: ("HR Genie", _serialized([LOOKUP])),
        }
        self.space_acl[HR] = {}
        self.groups_by_id["g-3"] = (HR_GROUP, "Group")
        self.genie = SimpleNamespace(get_space=self._get_space)
        self.unreadable: set[str] = set()  # spaces the SP cannot read

    def _get_space(self, space_id, include_serialized_space=None):
        if space_id in self.unreadable:
            from databricks.sdk.errors import PermissionDenied

            raise PermissionDenied("no access to the space")
        if space_id not in self.space_defs or space_id not in self.space_acl:
            from databricks.sdk.errors import NotFound

            raise NotFound("no such space")
        title, ser = self.space_defs[space_id]
        return SimpleNamespace(
            space_id=space_id,
            title=title,
            warehouse_id="0000000000000000",
            serialized_space=ser if include_serialized_space else None,
        )

    def spaces(self):
        return [
            ResourceItemOut(id=sid, name=self.space_defs[sid][0], resource_type=GENIE)
            for sid in self.space_defs
            if sid in self.space_acl
        ]

    def level(self, space_id, group=GROUP):
        return self.space_acl.get(space_id, {}).get(("group", group))


@pytest.fixture(params=_migration_engines())
def session(request):
    """SQLite always; Postgres too (advisory locks, real constraints) when
    UCG_TEST_PG_URL is set."""
    engine = create_engine("sqlite://") if request.param == "sqlite" else create_engine(PG_URL or "")
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def ws():
    return StackWorkspace()


@pytest.fixture(autouse=True)
def _listing(ws):
    """Resource listing comes from the fake (Genie) — nothing else is listed."""

    def fake_list(_w, rt):
        return ws.spaces() if rt == GENIE else []

    with patch("permissions_app.backend.router.list_resources", side_effect=fake_list):
        yield


def _persona(session, key, cells, overrides=None):
    session.add(PersonaDefinition(key=key, label=key.replace("_", " ").title()))
    for rt, level in cells.items():
        session.add(PermissionTemplate(persona=key, resource_type=rt, permission_level=level))
    for space, level in (overrides or {}).items():
        session.add(
            PersonaResourceOverride(persona=key, resource_type=GENIE, resource_id=space, permission_level=level)
        )
    session.commit()


def _map(session, persona, group_id="g-1", group_name=GROUP):
    m = PersonaGroupMapping(group_id=group_id, group_name=group_name, persona=persona)
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def _rows(session, **where):
    rows = session.exec(select(GenieGrantLedger)).all()
    return [r for r in rows if all(getattr(r, k) == v for k, v in where.items())]


def _sales_hr(session):
    """Sales Genie / HR Genie example: 'all spaces' = no access, each persona
    overrides ONE space to CAN_RUN."""
    _persona(session, "sales_analyst", {GENIE: NONE}, {SALES: CAN_RUN})
    _persona(session, "hr_analyst", {GENIE: NONE}, {HR: CAN_RUN})


# ─── Stacking: highest wins, NO_PERMISSIONS grants nothing ──────────────────


def test_merge_highest_wins_and_none_is_not_deny():
    eff = ps.merge(GENIE, {"a": NONE, "b": CAN_READ, "c": CAN_RUN})
    assert eff.level == CAN_RUN and eff.granted_by == ["c"]
    assert eff.escalation and "CAN_READ" in (eff.escalation_detail or "")
    # A single granting source is never an escalation; NONE contributes nothing.
    single = ps.merge(GENIE, {"a": NONE, "b": CAN_RUN})
    assert single.level == CAN_RUN and not single.escalation
    assert ps.merge(GENIE, {"a": NONE}).level == NONE
    # Ties: every source at the top level is credited.
    assert ps.merge(GENIE, {"a": CAN_RUN, "b": CAN_RUN}).granted_by == ["a", "b"]


def test_apply_sets_each_group_to_its_highest_stacked_level(session, ws):
    _persona(session, "viewer", {CLUSTERS: PermissionLevel.CAN_ATTACH_TO.value})
    _persona(session, "operator", {CLUSTERS: PermissionLevel.CAN_RESTART.value})
    _persona(session, "nothing", {CLUSTERS: NONE})
    for p in ("viewer", "operator", "nothing"):
        _map(session, p)
    _map(session, "viewer", "g-3", HR_GROUP)  # a second group with one persona

    calls = []
    with patch("permissions_app.backend.router.list_resources", return_value=[
        ResourceItemOut(id="c1", name="c1", resource_type=CLUSTERS)
    ]), patch(
        "permissions_app.backend.router.apply_group_acl",
        side_effect=lambda _w, rt, rid, levels: calls.append((rt, rid, dict(levels))),
    ):
        # Applying the LOWEST (or the no-permission) persona must not lower or
        # strip what another persona of the same group grants.
        res = apply_permissions("viewer", ws, session, None, "admin")
        assert res.total_errors == 0
        assert calls == [(CLUSTERS, "c1", {GROUP: "CAN_RESTART", HR_GROUP: "CAN_ATTACH_TO"})]
        calls.clear()
        apply_permissions("nothing", ws, session, None, "admin")  # only g-1 maps it
        assert calls == [(CLUSTERS, "c1", {GROUP: "CAN_RESTART"})]


def test_create_mapping_allows_several_personas_per_group_but_not_twice(session):
    _persona(session, "a", {GENIE: NONE})
    _persona(session, "b", {GENIE: NONE})
    create_persona_mapping(PersonaGroupMappingIn(group_id="g-1", group_name=GROUP, persona="a"), session)
    create_persona_mapping(PersonaGroupMappingIn(group_id="g-1", group_name=GROUP, persona="b"), session)
    with pytest.raises(HTTPException) as exc:
        create_persona_mapping(PersonaGroupMappingIn(group_id="g-1", group_name=GROUP, persona="a"), session)
    assert exc.value.status_code == 400
    # ...and the DB enforces it too (race-proof).
    session.add(PersonaGroupMapping(group_id="g-1", group_name=GROUP, persona="b"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ─── Per-space overrides + table grants ──────────────────────────────────────


def test_per_space_overrides_and_table_grants_for_stacked_personas(session, ws):
    _sales_hr(session)
    _map(session, "sales_analyst")
    _map(session, "hr_analyst")

    res = apply_permissions("sales_analyst", ws, session, None, "admin@example.com")

    assert res.total_errors == 0, res
    # Genie level per space = highest of the group's personas there.
    assert ws.level(SALES) == CAN_RUN and ws.level(HR) == CAN_RUN
    # Per table (never schema-wide) + USE_SCHEMA + USE_CATALOG.
    for typ, name, priv in [
        ("table", ORDERS, "SELECT"),
        ("table", REVENUE, "SELECT"),
        ("schema", f"{CAT}.sales", "USE_SCHEMA"),
        ("schema", f"{CAT}.finance", "USE_SCHEMA"),
        ("catalog", CAT, "USE_CATALOG"),
        ("table", LOOKUP, "SELECT"),
        ("schema", f"{CAT}.other", "USE_SCHEMA"),
    ]:
        assert ws.has(typ, name, priv), (typ, name, priv)
    assert not any(u[1] == "schema" and u[4] == "SELECT" for u in ws.updates)
    sources = {r.source for r in _rows(session)}
    assert sources == {f"persona:sales_analyst:space:{SALES}", f"persona:hr_analyst:space:{HR}"}
    # 7 distinct UC grants; the shared USE_CATALOG is granted once and the HR
    # source records that it relies on it (8 ledger rows).
    assert res.table_grants_added == 7 and len(_rows(session)) == 8
    assert [u for u in ws.updates if u[2] == CAT] == [("add", "catalog", CAT, GROUP, "USE_CATALOG")]
    # Re-apply is idempotent: no new workspace grants.
    n = len(ws.updates)
    apply_permissions("hr_analyst", ws, session, None, "admin@example.com")
    assert len(ws.updates) == n


def test_override_lower_than_all_spaces_level(session, ws):
    # "All spaces" CAN_RUN, but this persona grants NOTHING on HR.
    _persona(session, "sales_only", {GENIE: CAN_RUN}, {HR: NONE})
    _map(session, "sales_only")
    apply_permissions("sales_only", ws, session, None, "admin")
    assert ws.level(SALES) == CAN_RUN and ws.level(HR) is None
    assert not ws.has("table", LOOKUP, "SELECT")


def test_mapping_removal_recomputes_and_revokes_only_unneeded(session, ws):
    _sales_hr(session)
    # Both personas also see REVENUE via a second override (shared grant).
    session.add(PersonaResourceOverride(persona="hr_analyst", resource_type=GENIE, resource_id=SALES, permission_level=CAN_READ))
    session.commit()
    _map(session, "sales_analyst")
    hr = _map(session, "hr_analyst")
    apply_permissions("sales_analyst", ws, session, None, "admin")
    assert ws.has("table", LOOKUP, "SELECT")

    out = delete_persona_mapping(hr.id or 0, ws, session, "admin")

    assert out.errors == [], out.errors
    assert ws.level(HR) is None  # nothing else grants HR
    assert ws.level(SALES) == CAN_RUN  # still from sales_analyst
    assert not ws.has("table", LOOKUP, "SELECT") and not ws.has("schema", f"{CAT}.other", "USE_SCHEMA")
    # Shared Sales grants are kept (sales_analyst still needs them) ...
    assert ws.has("table", ORDERS, "SELECT") and ws.has("catalog", CAT, "USE_CATALOG")
    assert not any(u[2] in (ORDERS, CAT) for u in ws.removes())
    # ... and no ledger row of the removed persona remains.
    assert not [r for r in _rows(session) if r.persona == "hr_analyst"]


def test_removing_the_only_persona_revokes_app_made_grants_but_not_pre_existing(session, ws):
    _sales_hr(session)
    m = _map(session, "sales_analyst")
    ws.uc[("table", REVENUE)] = {GROUP: {"SELECT"}}  # pre-existing direct grant
    apply_permissions("sales_analyst", ws, session, None, "admin")
    pre = next(r for r in _rows(session) if r.full_name == REVENUE)
    assert pre.app_granted is False

    delete_persona_mapping(m.id or 0, ws, session, "admin")

    assert ws.level(SALES) is None
    assert not ws.has("table", ORDERS, "SELECT")
    assert ws.has("table", REVENUE, "SELECT")  # never revoked: the app didn't make it
    assert _rows(session) == []


def test_deleting_a_persona_recomputes_its_groups(session, ws):
    _sales_hr(session)
    _map(session, "sales_analyst")
    _map(session, "hr_analyst")
    apply_permissions("sales_analyst", ws, session, None, "admin")

    out = delete_persona("hr_analyst", ws, session, "admin")

    assert [g["removed_persona"] for g in out["groups_recomputed"]] == ["hr_analyst"]
    assert ws.level(HR) is None and not ws.has("table", LOOKUP, "SELECT")
    assert ws.level(SALES) == CAN_RUN and ws.has("table", ORDERS, "SELECT")
    assert session.exec(select(PersonaResourceOverride).where(PersonaResourceOverride.persona == "hr_analyst")).all() == []


def test_several_groups_on_one_space_get_their_own_grants(session, ws):
    _sales_hr(session)
    _map(session, "sales_analyst")
    other = _map(session, "sales_analyst", "g-3", HR_GROUP)
    apply_permissions("sales_analyst", ws, session, None, "admin")

    for g in (GROUP, HR_GROUP):
        assert ws.level(SALES, g) == CAN_RUN
        assert ws.has("table", ORDERS, "SELECT", principal=g)
        assert ws.has("catalog", CAT, "USE_CATALOG", principal=g)

    delete_persona_mapping(other.id or 0, ws, session, "admin")

    assert ws.level(SALES, HR_GROUP) is None and ws.level(SALES) == CAN_RUN
    assert not ws.has("table", ORDERS, "SELECT", principal=HR_GROUP)
    assert ws.has("table", ORDERS, "SELECT", principal=GROUP)
    assert {r[3] for r in ws.removes()} == {HR_GROUP}


def test_workspace_local_group_is_refused(session, ws):
    _sales_hr(session)
    _map(session, "sales_analyst", "g-2", "local-team")
    res = apply_permissions("sales_analyst", ws, session, None, "admin")
    assert ws.level(SALES, "local-team") is None
    assert ws.updates == [] and _rows(session) == []
    assert any("workspace-local" in e for r in res.results for e in r.errors)


def test_persona_and_bundle_share_grants_on_one_space(session, ws):
    _sales_hr(session)
    bundle = _bundle(session)  # same group g-1, Sales space
    _apply(ws, session, bundle)
    m = _map(session, "sales_analyst")
    apply_permissions("sales_analyst", ws, session, None, "admin")
    orders_adds = [u for u in ws.updates if u[:3] == ("add", "table", ORDERS)]
    assert len(orders_adds) == 1  # granted once, now needed by two sources
    assert {r.source for r in _rows(session, full_name=ORDERS)} == {
        f"bundle:{bundle.id}",
        f"persona:sales_analyst:space:{SALES}",
    }

    delete_persona_mapping(m.id or 0, ws, session, "admin")

    # The bundle still grants the space and its tables: nothing revoked.
    assert ws.level(SALES) == CAN_RUN
    assert ws.has("table", ORDERS, "SELECT") and ws.removes() == []


# ─── Preview: least-privilege detail, zero writes ────────────────────────────


def test_preview_shows_levels_sources_escalation_and_grants_without_writing(session, ws):
    _sales_hr(session)
    _persona(session, "space_admin", {GENIE: NONE}, {SALES: CAN_MANAGE})
    _map(session, "sales_analyst")
    _map(session, "hr_analyst")
    _map(session, "space_admin")
    engine = session.get_bind().engine
    writes, stop = _record_writes(engine)
    try:
        out = preview_apply_permissions("sales_analyst", ws, session)
        assert not session.new and not session.dirty and not session.deleted
    finally:
        stop()

    assert writes == [], writes
    assert ws.updates == [] and ws.space_sets == []
    rows = {(r.group_name, r.resource_id): r for r in out.group_levels}
    sales = rows[(GROUP, SALES)]
    assert sales.effective_level == CAN_MANAGE and sales.granted_by == ["space_admin"]
    assert sales.source_levels["sales_analyst"] == CAN_RUN
    assert sales.escalation and out.escalation_count >= 1
    assert rows[(GROUP, HR)].granted_by == ["hr_analyst"]
    grants = {(g.full_name, g.privilege, g.source): g for g in out.table_grants}
    rev = grants[(REVENUE, "SELECT", f"persona:space_admin:space:{SALES}")]
    assert rev.action == "add" and rev.has_row_filter
    assert any("row filter" in w for w in out.warnings)


def test_preview_reports_keep_and_revoke(session, ws):
    _sales_hr(session)
    _map(session, "sales_analyst")
    apply_permissions("sales_analyst", ws, session, None, "admin")
    # The matrix changes: this persona no longer grants Sales.
    o = session.exec(select(PersonaResourceOverride).where(PersonaResourceOverride.persona == "sales_analyst")).one()
    o.permission_level = NONE
    session.add(o)
    session.commit()
    n = len(ws.updates)

    out = preview_apply_permissions("sales_analyst", ws, session)

    assert len(ws.updates) == n
    actions = {(g.full_name, g.action) for g in out.table_grants}
    assert (ORDERS, "revoke") in actions and (CAT, "revoke") in actions


# ─── Overrides endpoint ──────────────────────────────────────────────────────


def test_override_endpoint_sets_and_clears_genie_only(session):
    _persona(session, "p", {GENIE: NONE})
    out = set_permission_override(
        PermissionOverrideIn(persona="p", resource_type=ResourceType.GENIE_SPACES, resource_id=SALES,
                             permission_level=PermissionLevel.CAN_RUN),
        session,
    )
    assert [(o.resource_id, o.permission_level) for o in out] == [(SALES, CAN_RUN)]
    out = set_permission_override(
        PermissionOverrideIn(persona="p", resource_type=ResourceType.GENIE_SPACES, resource_id=SALES,
                             permission_level=None),
        session,
    )
    assert out == []
    with pytest.raises(HTTPException) as exc:
        set_permission_override(
            PermissionOverrideIn(persona="p", resource_type=ResourceType.CLUSTERS, resource_id="c1",
                                 permission_level=PermissionLevel.CAN_MANAGE),
            session,
        )
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):  # level must be valid for the type
        set_permission_override(
            PermissionOverrideIn(persona="p", resource_type=ResourceType.GENIE_SPACES, resource_id=SALES,
                                 permission_level=PermissionLevel.CAN_RESTART),
            session,
        )


# ─── Migration: drop one-persona-per-group, keep data ───────────────────────


def _legacy_mapping_table(engine):
    id_col = "SERIAL PRIMARY KEY" if engine.dialect.name == "postgresql" else "INTEGER PRIMARY KEY"
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS persona_group_mapping"))
        conn.execute(text(
            f"CREATE TABLE persona_group_mapping (id {id_col}, group_id VARCHAR NOT NULL,"
            " group_name VARCHAR NOT NULL, persona VARCHAR NOT NULL,"
            f" CONSTRAINT {LEGACY_PERSONA_GROUP_UNIQUE} UNIQUE (group_id))"
        ))
        conn.execute(text("CREATE INDEX ix_persona_group_mapping_group_id ON persona_group_mapping (group_id)"))
        conn.execute(text("CREATE INDEX ix_persona_group_mapping_persona ON persona_group_mapping (persona)"))
        conn.execute(text(
            "INSERT INTO persona_group_mapping (id, group_id, group_name, persona) VALUES"
            " (1, 'g-1', 'sales', 'analyst'), (2, 'g-2', 'hr', 'viewer')"
        ))
        if engine.dialect.name == "postgresql":
            conn.execute(text("SELECT setval('persona_group_mapping_id_seq', 2)"))


@pytest.mark.parametrize("kind", _migration_engines())
def test_persona_stacking_migration_is_idempotent_and_keeps_data(kind, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}") if kind == "sqlite" else create_engine(PG_URL or "")
    SQLModel.metadata.drop_all(engine)
    # A pre-stacking database: every other table exists, the override table
    # does not, and the mapping table still has UNIQUE (group_id).
    SQLModel.metadata.create_all(engine, tables=[
        t for name, t in SQLModel.metadata.tables.items()
        if name not in {"persona_group_mapping", "persona_resource_override"}
    ])
    _legacy_mapping_table(engine)
    for _ in range(3):  # idempotent
        with engine.begin() as conn:
            if kind == "postgres":
                ensure_unique_constraints(conn)
            ensure_persona_stacking_schema(conn)

    insp = sa_inspect(engine)
    assert "persona_resource_override" in insp.get_table_names()
    names = {u["name"] for u in insp.get_unique_constraints("persona_group_mapping")}
    assert LEGACY_PERSONA_GROUP_UNIQUE not in names
    with Session(engine) as s:
        rows = s.exec(select(PersonaGroupMapping).order_by(PersonaGroupMapping.id)).all()  # type: ignore[arg-type]
        assert [(r.id, r.group_id, r.persona) for r in rows] == [(1, "g-1", "analyst"), (2, "g-2", "viewer")]
        s.add(PersonaGroupMapping(group_id="g-1", group_name="sales", persona="viewer"))
        s.commit()  # stacking now allowed
        s.add(PersonaGroupMapping(group_id="g-1", group_name="sales", persona="viewer"))
        with pytest.raises(IntegrityError):
            s.commit()  # ...but each (group, persona) once
    SQLModel.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS persona_group_mapping"))
    engine.dispose()


def test_resources_page_genie_edit_is_space_only(ws):
    from permissions_app.backend.models import SetPermissionIn
    from permissions_app.backend.router import set_resource_permissions

    set_resource_permissions(
        GENIE, SALES, [SetPermissionIn(group_name=GROUP, permission_level=PermissionLevel.CAN_RUN)], ws
    )
    assert ws.level(SALES) == CAN_RUN
    assert ws.updates == []  # no UC table / schema / catalog grants


def test_preview_checks_presence_per_group(session, ws):
    """Two groups on one space: one already holds SELECT on orders, the other
    does not. The preview must not reuse one group's grant read for the other."""
    _sales_hr(session)
    _map(session, "sales_analyst")
    _map(session, "sales_analyst", "g-3", HR_GROUP)
    ws.uc[("table", ORDERS)] = {GROUP: {"SELECT"}}

    out = preview_apply_permissions("sales_analyst", ws, session)

    actions = {(g.group_name, g.full_name, g.privilege): g.action for g in out.table_grants}
    assert actions[(GROUP, ORDERS, "SELECT")] == "present"
    assert actions[(HR_GROUP, ORDERS, "SELECT")] == "add"


# ═══ Review round: removal is retryable, scope is complete, scoped PG lookup ═══


def test_failed_revoke_keeps_last_mapping_and_retry_completes(session, ws):
    _sales_hr(session)
    m = _map(session, "sales_analyst")
    mid = m.id or 0
    apply_permissions("sales_analyst", ws, session, None, "admin")
    ws.deny_update.add(("table", ORDERS))  # this revoke will fail

    out = delete_persona_mapping(mid, ws, session, "admin")

    assert not out.mapping_removed and out.errors
    assert session.get(PersonaGroupMapping, mid) is not None  # kept: retryable
    assert ws.has("table", ORDERS, "SELECT")
    assert [r.full_name for r in _rows(session, persona="sales_analyst")] == [ORDERS]

    ws.deny_update.clear()
    again = delete_persona_mapping(mid, ws, session, "admin")

    assert again.mapping_removed and again.errors == []
    assert session.get(PersonaGroupMapping, mid) is None
    assert not ws.has("table", ORDERS, "SELECT") and _rows(session) == []


def test_failed_cleanup_keeps_persona_and_retry_deletes_it(session, ws):
    _sales_hr(session)
    _map(session, "sales_analyst")
    apply_permissions("sales_analyst", ws, session, None, "admin")
    ws.deny_update.add(("table", ORDERS))

    out = delete_persona("sales_analyst", ws, session, "admin")

    assert out["status"] == "incomplete" and not out["persona_deleted"]
    defn = session.exec(select(PersonaDefinition).where(PersonaDefinition.key == "sales_analyst")).first()
    assert defn is not None
    assert session.exec(select(PersonaGroupMapping)).all() != []
    assert session.exec(select(PersonaResourceOverride)).all() != []  # overrides kept too

    ws.deny_update.clear()
    out = delete_persona("sales_analyst", ws, session, "admin")

    assert out["status"] == "ok" and out["persona_deleted"]
    assert session.exec(select(PersonaGroupMapping)).all() == []
    assert not ws.has("table", ORDERS, "SELECT") and _rows(session) == []


@pytest.mark.parametrize("still_needed_by", ["nothing", "persona", "bundle", "pre-existing"])
def test_removal_releases_old_grants_even_if_cell_is_now_none(session, ws, still_needed_by):
    """The persona was applied with Sales = CAN_RUN, then its cell was lowered to
    NO_PERMISSIONS WITHOUT an Apply. Removing it must still release its old
    table grants, keeping only what something else needs."""
    _sales_hr(session)
    m = _map(session, "sales_analyst")
    if still_needed_by == "pre-existing":
        ws.uc[("table", ORDERS)] = {GROUP: {"SELECT"}}
    apply_permissions("sales_analyst", ws, session, None, "admin")
    if still_needed_by == "persona":
        _persona(session, "other", {GENIE: NONE}, {SALES: CAN_READ})
        _map(session, "other")
        apply_permissions("other", ws, session, None, "admin")
    if still_needed_by == "bundle":
        _apply(ws, session, _bundle(session))
    o = session.exec(select(PersonaResourceOverride).where(PersonaResourceOverride.persona == "sales_analyst")).one()
    o.permission_level = NONE  # lowered, never applied
    session.add(o)
    session.commit()
    assert _rows(session, persona="sales_analyst")  # old rows still there

    out = delete_persona_mapping(m.id or 0, ws, session, "admin")

    assert out.mapping_removed, out.errors
    assert _rows(session, persona="sales_analyst") == []
    kept = still_needed_by != "nothing"
    assert ws.has("table", ORDERS, "SELECT") == kept
    if still_needed_by == "nothing":
        assert ws.level(SALES) is None and not ws.has("catalog", CAT, "USE_CATALOG")
    if still_needed_by == "pre-existing":
        assert not ws.has("table", REVENUE, "SELECT")  # app-made ones still revoked


def test_removal_recomputes_previously_applied_non_genie_types(session, ws):
    _persona(session, "ops", {CLUSTERS: PermissionLevel.CAN_RESTART.value, GENIE: NONE})
    m = _map(session, "ops")
    calls = []
    clusters = [ResourceItemOut(id="c1", name="c1", resource_type=CLUSTERS)]

    def fake_list(_w, rt):
        return clusters if rt == CLUSTERS else (ws.spaces() if rt == GENIE else [])

    with patch("permissions_app.backend.router.list_resources", side_effect=fake_list), patch(
        "permissions_app.backend.router.apply_group_acl",
        side_effect=lambda _w, rt, rid, levels: calls.append((rt, rid, dict(levels))),
    ):
        apply_permissions("ops", ws, session, None, "admin")
        assert (CLUSTERS, "c1", {GROUP: "CAN_RESTART"}) in calls
        # Cell lowered without Apply: the persona "grants nothing" now, but the
        # group still holds CAN_RESTART from the earlier Apply.
        t = session.exec(select(PermissionTemplate).where(PermissionTemplate.persona == "ops")).all()
        for row in t:
            row.permission_level = NONE
            session.add(row)
        session.commit()
        calls.clear()
        out = delete_persona_mapping(m.id or 0, ws, session, "admin")

    assert out.mapping_removed
    assert (CLUSTERS, "c1", {GROUP: NONE}) in calls


@pytest.mark.skipif(not PG_URL, reason="set UCG_TEST_PG_URL to run Postgres tests")
def test_pg_constraint_lookup_is_scoped_to_the_ledger_table():
    """Same-named constraints on an unrelated table (same schema) and in another
    schema must not make the migration skip adding the ledger's constraints."""
    from permissions_app.backend.models import (
        GENIE_LEDGER_GROUP_ID_CHECK,
        GENIE_LEDGER_SOURCE_CHECK,
        GENIE_LEDGER_SOURCE_UNIQUE,
    )
    from permissions_app.backend.seed import ensure_genie_bundle_schema
    from permissions_app.tests.test_genie_bundles import _v1_schema

    engine = create_engine(PG_URL or "")
    SQLModel.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS decoy_local"))
        conn.execute(text("DROP SCHEMA IF EXISTS decoy CASCADE"))
        conn.execute(text(
            "CREATE TABLE decoy_local (id INT, source VARCHAR, group_id VARCHAR,"
            f" CONSTRAINT {GENIE_LEDGER_SOURCE_CHECK} CHECK (source <> ''),"
            f" CONSTRAINT {GENIE_LEDGER_GROUP_ID_CHECK} CHECK (group_id <> ''))"
        ))
        conn.execute(text("CREATE SCHEMA decoy"))
        conn.execute(text(
            "CREATE TABLE decoy.t (a INT, b INT, c INT, d INT, e INT,"
            f" CONSTRAINT {GENIE_LEDGER_SOURCE_UNIQUE} UNIQUE (a, b, c, d, e))"
        ))
    _v1_schema(engine)  # pre-source / pre-group_id ledger
    for _ in range(2):  # idempotent
        with engine.begin() as conn:
            ensure_genie_bundle_schema(conn)

    with engine.connect() as conn:
        on_ledger = set(conn.execute(text(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'genie_grant_ledger'::regclass"
        )).scalars().all())
    assert {GENIE_LEDGER_SOURCE_CHECK, GENIE_LEDGER_SOURCE_UNIQUE, GENIE_LEDGER_GROUP_ID_CHECK} <= on_ledger
    with Session(engine) as s:  # the constraints really are enforced
        s.add(GenieGrantLedger(source="", bundle_id=1, group_id="g", space_id="s", principal="p",
                               securable_type="table", full_name="c.s.t", privilege="SELECT"))
        with pytest.raises(IntegrityError, match=GENIE_LEDGER_SOURCE_CHECK):
            s.commit()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE decoy_local"))
        conn.execute(text("DROP SCHEMA decoy CASCADE"))
    SQLModel.metadata.drop_all(engine)
    engine.dispose()



# ═══ Review round 3: serialized removal, deleted groups, blocked Genie ═══


def test_confirmed_deleted_group_forgets_ledger_rows_without_workspace_calls(session, ws):
    _sales_hr(session)
    m = _map(session, "sales_analyst")
    mid = m.id or 0
    apply_permissions("sales_analyst", ws, session, None, "admin")
    assert _rows(session, persona="sales_analyst")
    del ws.groups_by_id["g-1"]  # by-id lookup now returns NotFound
    n_updates, n_sets = len(ws.updates), len(ws.space_sets)

    out = delete_persona_mapping(mid, ws, session, "admin")

    assert out.mapping_removed and out.errors == []
    assert session.get(PersonaGroupMapping, mid) is None
    assert _rows(session, persona="sales_analyst") == []  # forgotten
    assert (len(ws.updates), len(ws.space_sets)) == (n_updates, n_sets)  # no workspace call
    assert any("forgotten" in w for w in out.warnings)


def test_transient_group_lookup_error_keeps_mapping_and_rows(session, ws):
    _sales_hr(session)
    m = _map(session, "sales_analyst")
    mid = m.id or 0
    apply_permissions("sales_analyst", ws, session, None, "admin")
    real_get = ws.groups.get
    n_rows = len(_rows(session))

    def flaky(id):
        raise RuntimeError("SCIM timed out")

    ws.groups.get = flaky
    out = delete_persona_mapping(mid, ws, session, "admin")
    assert not out.mapping_removed and out.errors
    assert session.get(PersonaGroupMapping, mid) is not None
    assert len(_rows(session)) == n_rows

    ws.groups.get = real_get
    assert delete_persona_mapping(mid, ws, session, "admin").mapping_removed


def test_unreadable_space_blocks_removal_until_it_can_be_recomputed(session, ws):
    """A legacy (pre-ledger) persona ACL: the group holds CAN_MANAGE on Sales
    from space_admin with NO ledger rows. sales_analyst (CAN_RUN on Sales)
    remains. While Sales cannot be read, the group's Sales entry can't be
    recomputed, so removing space_admin must keep the mapping."""
    _sales_hr(session)
    _persona(session, "space_admin", {GENIE: NONE}, {SALES: CAN_MANAGE})
    _map(session, "sales_analyst")
    admin = _map(session, "space_admin")
    ws.space_acl[SALES][("group", GROUP)] = CAN_MANAGE  # legacy, unledgered
    ws.unreadable.add(SALES)

    out = delete_persona_mapping(admin.id or 0, ws, session, "admin")

    assert not out.mapping_removed
    assert any("Mapping kept" in e for e in out.errors), out.errors
    assert session.get(PersonaGroupMapping, admin.id) is not None
    assert ws.level(SALES) == CAN_MANAGE  # untouched, and the mapping says why

    ws.unreadable.clear()
    again = delete_persona_mapping(admin.id or 0, ws, session, "admin")

    assert again.mapping_removed, again.errors
    assert ws.level(SALES) == CAN_RUN  # recomputed from sales_analyst
    assert ws.has("table", ORDERS, "SELECT")


def _pg_setup(engine):
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        _sales_hr(s)
        mid = _map(s, "sales_analyst").id
        _map(s, "sales_analyst", "g-3", HR_GROUP)
        _map(s, "hr_analyst")
    return mid


@pytest.mark.skipif(not PG_URL, reason="set UCG_TEST_PG_URL to run Postgres tests")
def test_removal_waits_for_an_inflight_apply_postgres(ws, monkeypatch):
    """Apply builds its plan, then pauses before granting. A removal of the
    same group's mapping must wait for it (group operation lock) and then
    clean up — instead of finishing first and letting Apply recreate rows and
    grants from its stale plan (orphans with no mapping)."""
    import threading

    from permissions_app.backend import router

    engine = create_engine(PG_URL or "")
    mid = _pg_setup(engine)
    with Session(engine) as s:
        apply_permissions("sales_analyst", ws, s, None, "admin")  # rows exist
    paused, go = threading.Event(), threading.Event()
    real_exec = router.execute_genie_table_grants

    def paused_exec(*a, **k):
        if threading.current_thread().name == "apply":
            paused.set()
            assert go.wait(20)
        return real_exec(*a, **k)

    monkeypatch.setattr(router, "execute_genie_table_grants", paused_exec)
    errors: list[str] = []
    results: dict = {}

    def run(name, fn):
        try:
            with Session(engine) as s:
                results[name] = fn(s)
        except Exception as e:
            errors.append(f"{name}: {e!r}")

    t_apply = threading.Thread(
        name="apply", target=run,
        args=("apply", lambda s: apply_permissions("sales_analyst", ws, s, None, "admin")),
    )
    t_apply.start()
    assert paused.wait(20)
    t_remove = threading.Thread(
        name="remove", target=run,
        args=("remove", lambda s: delete_persona_mapping(mid or 0, ws, s, "admin")),
    )
    t_remove.start()
    t_remove.join(1.0)
    assert t_remove.is_alive(), "removal did not wait for the in-flight Apply"
    go.set()
    t_apply.join(30)
    t_remove.join(30)
    assert errors == [] and results["remove"].mapping_removed, results.get("remove")

    with Session(engine) as s:
        assert s.get(PersonaGroupMapping, mid) is None
        left = [r for r in _rows(s, persona="sales_analyst") if r.group_id == "g-1"]
        assert left == [], left  # no orphan rows for the removed mapping
    # g-1 keeps only what hr_analyst gives; no Sales access / grants left.
    assert ws.level(SALES) is None and not ws.has("table", ORDERS, "SELECT")
    assert ws.has("table", ORDERS, "SELECT", principal=HR_GROUP)  # g-3 untouched
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


@pytest.mark.skipif(not PG_URL, reason="set UCG_TEST_PG_URL to run Postgres tests")
def test_concurrent_apply_remove_and_bundle_ops_do_not_deadlock_postgres(ws, monkeypatch):
    import threading
    import time

    from permissions_app.backend import genie_bundles as gb
    from permissions_app.backend.genie_bundles import delete_bundle

    engine = create_engine(PG_URL or "", pool_size=5, max_overflow=0, pool_timeout=20)
    _pg_setup(engine)
    with Session(engine) as s:
        g3 = s.exec(select(PersonaGroupMapping).where(PersonaGroupMapping.group_id == "g-3")).one()
        g3_id = g3.id
        bundle_id = _bundle(s).id  # g-1, Sales
    monkeypatch.setattr(gb, "_after_pending_commit", lambda key: time.sleep(0.02))
    ops = {
        "apply_sales": lambda s: apply_permissions("sales_analyst", ws, s, None, "admin"),
        "apply_hr": lambda s: apply_permissions("hr_analyst", ws, s, None, "admin"),
        "remove_g3": lambda s: delete_persona_mapping(g3_id or 0, ws, s, "admin"),
        "bundle_apply": lambda s: _apply(ws, s, s.get(gb.GenieAccessBundle, bundle_id)),
    }
    barrier = threading.Barrier(len(ops))
    errors: list[str] = []

    def worker(name, fn):
        try:
            with Session(engine) as s:
                barrier.wait()
                fn(s)
        except Exception as e:
            errors.append(f"{name}: {e!r}")

    threads = [threading.Thread(target=worker, args=item) for item in ops.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads), "deadlock / hang"
    assert errors == [], errors
    # Then a bundle delete racing a final apply: also completes.
    with Session(engine) as s:
        b = s.get(gb.GenieAccessBundle, bundle_id)
        assert b is not None
        delete_bundle(ws, s, b)
    with Session(engine) as s:
        assert s.get(PersonaGroupMapping, g3_id) is None
        assert not [r for r in _rows(s) if r.group_id == "g-3"]  # g-3 fully cleaned
        assert not ws.has("table", ORDERS, "SELECT", principal=HR_GROUP)
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


# ═══ Review round 4: persona delete vs. mapping creation ═══


@pytest.mark.skipif(not PG_URL, reason="set UCG_TEST_PG_URL to run Postgres tests")
def test_mapping_creation_cannot_race_persona_delete_postgres(ws, monkeypatch):
    """Persona delete is paused AFTER its group cleanup and final mapping check,
    right before deleting the persona. A concurrent mapping creation for that
    persona must wait on the persona lock and then be refused — never leave a
    mapping pointing at a deleted persona."""
    import threading

    from permissions_app.backend import router

    engine = create_engine(PG_URL or "")
    _pg_setup(engine)
    with Session(engine) as s:
        apply_permissions("sales_analyst", ws, s, None, "admin")
    paused, go = threading.Event(), threading.Event()

    def pause():
        paused.set()
        assert go.wait(20)

    monkeypatch.setattr(router, "_before_persona_delete_commit", pause)
    results: dict = {}
    errors: list[str] = []

    def run(name, fn):
        try:
            with Session(engine) as s:
                results[name] = fn(s)
        except HTTPException as e:
            results[name] = e
        except Exception as e:
            errors.append(f"{name}: {e!r}")

    t_del = threading.Thread(target=run, args=("delete", lambda s: delete_persona("sales_analyst", ws, s, "admin")))
    t_del.start()
    assert paused.wait(20)
    # g-1 was already processed by the delete (its lock released): create again.
    body = PersonaGroupMappingIn(group_id="g-1", group_name=GROUP, persona="sales_analyst")
    t_create = threading.Thread(target=run, args=("create", lambda s: create_persona_mapping(body, s)))
    t_create.start()
    t_create.join(1.0)
    assert t_create.is_alive(), "mapping creation did not wait for the persona delete"
    go.set()
    t_del.join(30)
    t_create.join(30)

    assert errors == [], errors
    assert results["delete"]["persona_deleted"] is True
    assert isinstance(results["create"], HTTPException) and results["create"].status_code == 400
    with Session(engine) as s:
        orphans = s.exec(select(PersonaGroupMapping).where(PersonaGroupMapping.persona == "sales_analyst")).all()
        assert orphans == []  # no mapping points at the deleted persona
        assert s.exec(select(PersonaDefinition).where(PersonaDefinition.key == "sales_analyst")).first() is None
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


def test_operation_lock_order_is_enforced(session):
    from permissions_app.backend import genie_bundles as gb

    with gb.operation_locks(session, personas=["p"]):
        with gb.operation_locks(session, group_ids=["g-1"]):  # persona -> group: OK
            with gb._grant_lock(session, "g-1", gb.GrantKey("table", ORDERS, "SELECT")):
                with pytest.raises(RuntimeError, match="before any grant lock"):
                    with gb.operation_locks(session, group_ids=["g-2"]):
                        pass
    with gb.operation_locks(session, group_ids=["g-1"]):
        with pytest.raises(RuntimeError, match="global order"):  # group -> persona: refused
            with gb.operation_locks(session, personas=["p"]):
                pass
    assert session.info[gb._HELD_OP_LOCKS] == []
