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

    def _get_space(self, space_id, include_serialized_space=None):
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
