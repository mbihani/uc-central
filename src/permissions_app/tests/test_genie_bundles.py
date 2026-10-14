"""Tests for Genie access bundles.

A stateful fake WorkspaceClient (UC grants, Genie-space ACLs, tables, groups)
stands in for the workspace, and a real SQLite database backs the bundle +
grant-ledger tables, so apply / resync / delete are exercised end-to-end
against the actual ledger logic.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service import catalog, iam
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from permissions_app.backend.defaults import DEFAULT_PERMISSIONS_MATRIX
from permissions_app.backend.genie_bundle_routes import (
    _run,
    create_genie_bundle,
    delete_genie_bundle,
)
from permissions_app.backend.genie_bundles import (
    GENIE_SPACE,
    GrantKey,
    apply_plan,
    build_plan,
    check_group,
    delete_bundle,
    parse_serialized_space,
    required_grants,
    resolve_objects,
    split_name,
    to_preview,
)
from permissions_app.backend.models import (
    AddMemberIn,
    GenieAccessBundle,
    GenieBundleIn,
    GenieGrantLedger,
    PermissionLevel,
    ResourceType,
)
from permissions_app.backend.seed import ensure_genie_bundle_schema

FIXTURE = Path(__file__).parent / "fixtures" / "genie_space_get_serialized.json"
SPACE_ID = "01f0000000000000000000000000abcd"
GROUP = "genie-sales-users"
CAT = "acme_catalog"


# ─── Fake workspace ──────────────────────────────────────────────────────────


def _table(full_name, table_type, row_filter=None, masked=()):
    cols = [catalog.ColumnInfo(name="region"), catalog.ColumnInfo(name="email")]
    for c in cols:
        if c.name in masked:
            c.mask = catalog.ColumnMask(function_name=f"{CAT}.gov.mask_{c.name}")
    return catalog.TableInfo(
        full_name=full_name,
        table_type=catalog.TableType(table_type),
        row_filter=(
            catalog.TableRowFilter(function_name=row_filter, input_column_names=["region"])
            if row_filter
            else None
        ),
        columns=cols,
    )


class FakeWorkspace:
    def __init__(self, serialized_space: str, *, group_type="Group"):
        self.serialized_space = serialized_space
        self.tables_by_name = {
            f"{CAT}.sales.orders": _table(f"{CAT}.sales.orders", "MANAGED"),
            f"{CAT}.sales.v_daily_revenue": _table(f"{CAT}.sales.v_daily_revenue", "VIEW"),
            f"{CAT}.sales.customer_gold": _table(
                f"{CAT}.sales.customer_gold", "MATERIALIZED_VIEW", masked=("email",)
            ),
            f"{CAT}.finance.revenue_metrics": _table(
                f"{CAT}.finance.revenue_metrics",
                "METRIC_VIEW",
                row_filter=f"{CAT}.gov.region_filter",
            ),
            f"{CAT}.sales.returns": _table(f"{CAT}.sales.returns", "MANAGED"),
            f"{CAT}.other.lookup": _table(f"{CAT}.other.lookup", "MANAGED"),
        }
        self.volumes_by_name = {f"{CAT}.sales.attachments": f"{CAT}.sales.attachments"}
        # Direct UC grants: (securable_type, full_name) -> principal -> {privs}
        self.uc: dict[tuple[str, str], dict[str, set[str]]] = {}
        # Inherited/other-principal effective grants, read-only:
        # (securable_type, full_name) -> list[(principal, privilege, inherited_from)]
        self.uc_inherited: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
        self.deny_update: set[tuple[str, str]] = set()
        self.updates: list[tuple[str, str, str, str, str]] = []  # (op, type, name, principal, priv)
        # Genie space ACL: space_id -> {("group"|"user", name): level}
        self.space_acl: dict[str, dict[tuple[str, str], str]] = {
            SPACE_ID: {("user", "owner@example.com"): "CAN_MANAGE"}
        }
        self.space_sets: list[str] = []
        self.group_type = group_type

        self.genie = SimpleNamespace(get_space=self._get_space)
        self.tables = SimpleNamespace(get=self._get_table)
        self.volumes = SimpleNamespace(read=self._read_volume)
        self.grants = SimpleNamespace(get_effective=self._get_effective, update=self._update)
        self.permissions = SimpleNamespace(get=self._perm_get, set=self._perm_set)
        self.groups = SimpleNamespace(list=self._groups_list)

    # genie
    def _get_space(self, space_id, include_serialized_space=None):
        if space_id not in self.space_acl:
            raise NotFound("no such space")
        return SimpleNamespace(
            space_id=space_id,
            title="Sales Insights",
            warehouse_id="0000000000000000",
            serialized_space=self.serialized_space if include_serialized_space else None,
        )

    # tables / volumes
    def _get_table(self, full_name):
        if full_name not in self.tables_by_name:
            raise NotFound(f"Table '{full_name}' does not exist.")
        return self.tables_by_name[full_name]

    def _read_volume(self, name):
        if name not in self.volumes_by_name:
            raise NotFound("no volume")
        return catalog.VolumeInfo(full_name=name)

    # grants
    def _get_effective(self, securable_type, full_name, principal=None):
        assignments = []
        direct = self.uc.get((securable_type, full_name), {}).get(principal or "", set())
        if direct:
            assignments.append(
                catalog.EffectivePrivilegeAssignment(
                    principal=principal,
                    privileges=[
                        catalog.EffectivePrivilege(privilege=catalog.Privilege(p))
                        for p in sorted(direct)
                    ],
                )
            )
        for who, priv, src in self.uc_inherited.get((securable_type, full_name), []):
            assignments.append(
                catalog.EffectivePrivilegeAssignment(
                    principal=who,
                    privileges=[
                        catalog.EffectivePrivilege(
                            privilege=catalog.Privilege(priv),
                            inherited_from_name=src,
                            inherited_from_type=catalog.SecurableType.SCHEMA,
                        )
                    ],
                )
            )
        return catalog.EffectivePermissionsList(privilege_assignments=assignments)

    def _update(self, securable_type, full_name, changes=None):
        if (securable_type, full_name) in self.deny_update:
            raise PermissionDenied(f"User does not have MANAGE on {securable_type} '{full_name}'.")
        for ch in changes or []:
            held = self.uc.setdefault((securable_type, full_name), {}).setdefault(ch.principal, set())
            for p in ch.add or []:
                held.add(p.value)
                self.updates.append(("add", securable_type, full_name, ch.principal, p.value))
            for p in ch.remove or []:
                held.discard(p.value)
                self.updates.append(("remove", securable_type, full_name, ch.principal, p.value))
        return catalog.UpdatePermissionsResponse()

    # workspace ACLs (Genie space)
    def _perm_get(self, request_object_type, request_object_id):
        assert request_object_type == ResourceType.GENIE_SPACES.value
        if request_object_id not in self.space_acl:
            raise NotFound("no such space")
        acl = []
        for (kind, name), level in self.space_acl[request_object_id].items():
            perm = iam.Permission(permission_level=iam.PermissionLevel(level), inherited=False)
            acl.append(
                iam.AccessControlResponse(
                    group_name=name if kind == "group" else None,
                    user_name=name if kind == "user" else None,
                    all_permissions=[perm],
                )
            )
        # Workspace admins inherit CAN_MANAGE — must be ignored by the merge.
        acl.append(
            iam.AccessControlResponse(
                group_name="admins",
                all_permissions=[
                    iam.Permission(permission_level=iam.PermissionLevel.CAN_MANAGE, inherited=True)
                ],
            )
        )
        return iam.ObjectPermissions(access_control_list=acl)

    def _perm_set(self, request_object_type, request_object_id, access_control_list):
        new = {}
        for r in access_control_list:
            if r.group_name:
                new[("group", r.group_name)] = r.permission_level.value
            elif r.user_name:
                new[("user", r.user_name)] = r.permission_level.value
        self.space_acl[request_object_id] = new
        self.space_sets.append(request_object_id)

    # groups
    def _groups_list(self, filter=None, attributes=None):
        known = {
            GROUP: ("g-1", self.group_type),
            "local-team": ("g-2", "WorkspaceGroup"),
        }
        out = []
        for name, (gid, rtype) in known.items():
            if filter == f'displayName eq "{name}"':
                out.append(
                    iam.Group(id=gid, display_name=name, meta=iam.ResourceMeta(resource_type=rtype))
                )
        return out

    # helpers for assertions
    def has(self, securable_type, full_name, priv, principal=GROUP):
        return priv in self.uc.get((securable_type, full_name), {}).get(principal, set())

    def removes(self):
        return [u for u in self.updates if u[0] == "remove"]


def make_ws(serialized_space: str, **kw) -> Any:
    # Typed Any: the fake duck-types the WorkspaceClient surface the code uses.
    return FakeWorkspace(serialized_space, **kw)


def _serialized(tables, volumes=()):
    return json.dumps(
        {
            "version": 2,
            "data_sources": {
                "tables": [{"identifier": t} for t in tables],
                **({"volumes": [{"path": v} for v in volumes]} if volumes else {}),
            },
        }
    )


@pytest.fixture
def fixture_space():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _bundle(session, space_id=SPACE_ID, group=GROUP):
    b = GenieAccessBundle(space_id=space_id, space_title="Sales Insights", group_name=group, group_id="g-1")
    session.add(b)
    session.commit()
    session.refresh(b)
    return b


def _apply(ws, session, bundle, *, resync=False):
    plan = build_plan(ws, session, bundle)
    assert not plan.blockers, plan.blockers
    return apply_plan(ws, session, bundle, plan, "admin@example.com", revoke_stale=resync)


def _ledger(session, bundle_id=None):
    q = select(GenieGrantLedger)
    if bundle_id is not None:
        q = q.where(GenieGrantLedger.bundle_id == bundle_id)
    return session.exec(q).all()


# ─── Discovery ───────────────────────────────────────────────────────────────


def test_parse_fixture_discovers_tables_views_metric_views_and_volumes(fixture_space):
    refs = parse_serialized_space(fixture_space["serialized_space"])
    assert refs == [
        ("table", f"{CAT}.sales.orders"),
        ("table", f"{CAT}.sales.v_daily_revenue"),
        ("table", f"{CAT}.sales.customer_gold"),
        ("table", f"{CAT}.finance.revenue_metrics"),
        ("table", f"{CAT}.sales.dropped_table"),
        ("volume", f"/Volumes/{CAT}/sales/attachments/"),
    ]


def test_parse_picks_up_unknown_identifier_lists_and_rejects_bad_json():
    s = json.dumps({"data_sources": {"metric_views": [{"identifier": "c.s.mv"}], "tables": []}})
    assert parse_serialized_space(s) == [("table", "c.s.mv")]
    assert parse_serialized_space(None) == []
    with pytest.raises(ValueError):
        parse_serialized_space("{not json")


def test_split_name_handles_backticks():
    assert split_name("`my cat`.`sch.ema`.t") == ["my cat", "sch.ema", "t"]
    assert split_name("a.b.c") == ["a", "b", "c"]


def test_resolve_objects_types_filters_masks_and_unresolved(fixture_space):
    ws = make_ws(fixture_space["serialized_space"])
    refs = parse_serialized_space(fixture_space["serialized_space"]) + [("table", "sales.no_catalog")]
    objs = {o.identifier: o for o in resolve_objects(ws, refs)}

    assert objs[f"{CAT}.sales.orders"].table_type == "MANAGED"
    assert objs[f"{CAT}.sales.v_daily_revenue"].table_type == "VIEW"
    mv = objs[f"{CAT}.sales.customer_gold"]
    assert mv.table_type == "MATERIALIZED_VIEW" and mv.masked_columns == ["email"]
    metric = objs[f"{CAT}.finance.revenue_metrics"]
    assert metric.table_type == "METRIC_VIEW"
    assert metric.has_row_filter and metric.row_filter_function == f"{CAT}.gov.region_filter"
    vol = objs[f"/Volumes/{CAT}/sales/attachments/"]
    assert vol.resolved and vol.full_name == f"{CAT}.sales.attachments"

    dropped = objs[f"{CAT}.sales.dropped_table"]
    assert not dropped.resolved and "Does not exist" in (dropped.reason or "")
    two_part = objs["sales.no_catalog"]
    assert not two_part.resolved and "fully-qualified" in (two_part.reason or "")


# ─── Grant plan ──────────────────────────────────────────────────────────────


def test_required_grants_per_table_with_parents_deduped(fixture_space):
    ws = make_ws(fixture_space["serialized_space"])
    objs = resolve_objects(ws, parse_serialized_space(fixture_space["serialized_space"]))
    keys = required_grants(SPACE_ID, objs)

    assert keys[0] == GrantKey(GENIE_SPACE, SPACE_ID, "CAN_RUN")
    assert keys.count(GrantKey("catalog", CAT, "USE_CATALOG")) == 1
    assert [k for k in keys if k.securable_type == "schema"] == [
        GrantKey("schema", f"{CAT}.sales", "USE_SCHEMA"),
        GrantKey("schema", f"{CAT}.finance", "USE_SCHEMA"),
    ]
    assert {k.full_name for k in keys if k.privilege == "SELECT"} == {
        f"{CAT}.sales.orders",
        f"{CAT}.sales.v_daily_revenue",
        f"{CAT}.sales.customer_gold",
        f"{CAT}.finance.revenue_metrics",
    }
    assert GrantKey("volume", f"{CAT}.sales.attachments", "READ_VOLUME") in keys
    # Never schema-wide SELECT; the unresolved table is not granted.
    assert not any(k.securable_type == "schema" and k.privilege == "SELECT" for k in keys)
    assert not any("dropped_table" in k.full_name for k in keys)
    assert len(keys) == len(set(keys))


def test_preview_is_read_only_and_reports_present_grants(fixture_space, session):
    ws = make_ws(fixture_space["serialized_space"])
    ws.uc[("table", f"{CAT}.sales.orders")] = {GROUP: {"SELECT"}}  # client-made
    ws.uc_inherited[("catalog", CAT)] = [("account users", "USE_CATALOG", "metastore")]
    bundle = _bundle(session)

    preview = to_preview(bundle, build_plan(ws, session, bundle))

    assert ws.updates == [] and ws.space_sets == [] and _ledger(session) == []
    status = {(g.securable_type, g.full_name, g.privilege): g for g in preview.grants}
    assert status[("table", f"{CAT}.sales.orders", "SELECT")].status == "present"
    cat = status[("catalog", CAT, "USE_CATALOG")]
    assert cat.status == "present" and "account users" in (cat.detail or "")
    assert status[("table", f"{CAT}.sales.v_daily_revenue", "SELECT")].status == "missing"
    assert status[(GENIE_SPACE, SPACE_ID, "CAN_RUN")].status == "missing"
    assert preview.can_apply and preview.group.is_account_group
    assert any("dropped_table" in w for w in preview.warnings)
    assert any("row filter or column mask" in w for w in preview.warnings)
    restricted = {o.full_name for o in preview.objects if o.has_row_filter or o.masked_columns}
    assert restricted == {f"{CAT}.sales.customer_gold", f"{CAT}.finance.revenue_metrics"}


# ─── Apply ───────────────────────────────────────────────────────────────────


def test_apply_grants_missing_and_is_idempotent(fixture_space, session):
    ws = make_ws(fixture_space["serialized_space"])
    ws.uc[("table", f"{CAT}.sales.orders")] = {GROUP: {"SELECT"}}  # pre-existing
    bundle = _bundle(session)

    first = _apply(ws, session, bundle)

    assert first.status == "ok" and first.errors == []
    assert ws.space_acl[SPACE_ID][("group", GROUP)] == "CAN_RUN"
    assert ws.space_acl[SPACE_ID][("user", "owner@example.com")] == "CAN_MANAGE"  # preserved
    assert ("group", "admins") not in ws.space_acl[SPACE_ID]  # inherited, not written
    assert ws.has("catalog", CAT, "USE_CATALOG")
    assert ws.has("schema", f"{CAT}.finance", "USE_SCHEMA")
    assert ws.has("table", f"{CAT}.finance.revenue_metrics", "SELECT")
    assert ws.has("volume", f"{CAT}.sales.attachments", "READ_VOLUME")
    # The pre-existing SELECT was not re-granted, and is ledgered as not app-made.
    assert ("add", "table", f"{CAT}.sales.orders", GROUP, "SELECT") not in ws.updates
    rows = {(r.securable_type, r.full_name): r for r in _ledger(session, bundle.id)}
    assert rows[("table", f"{CAT}.sales.orders")].app_granted is False
    assert rows[("table", f"{CAT}.sales.v_daily_revenue")].app_granted is True
    assert rows[(GENIE_SPACE, SPACE_ID)].granted_by == "admin@example.com"
    n_rows, n_updates, n_sets = len(rows), len(ws.updates), len(ws.space_sets)

    second = _apply(ws, session, bundle)

    assert second.status == "ok" and second.granted == [] and second.adopted == []
    assert len(ws.updates) == n_updates and len(ws.space_sets) == n_sets
    assert len(_ledger(session, bundle.id)) == n_rows


def test_permission_denied_is_surfaced_not_swallowed(fixture_space, session):
    ws = make_ws(fixture_space["serialized_space"])
    ws.deny_update.add(("table", f"{CAT}.finance.revenue_metrics"))
    bundle = _bundle(session)

    result = _apply(ws, session, bundle)

    assert result.status == "partial"
    assert len(result.errors) == 1
    msg = result.errors[0]
    assert "Permission denied" in msg and "MANAGE" in msg and "revenue_metrics" in msg
    assert not any(r.full_name.endswith("revenue_metrics") for r in _ledger(session, bundle.id))
    assert ws.has("table", f"{CAT}.sales.orders", "SELECT")  # the rest still applied


# ─── Resync ──────────────────────────────────────────────────────────────────


def test_resync_adds_new_tables_and_revokes_only_ledger_owned_unshared(session):
    sales = [f"{CAT}.sales.orders", f"{CAT}.sales.v_daily_revenue", f"{CAT}.other.lookup"]
    ws = make_ws(_serialized(sales))
    ws.space_acl["space-2"] = {}
    ws.uc[("table", f"{CAT}.other.lookup")] = {GROUP: {"SELECT"}}  # client-made
    ws.uc[("schema", f"{CAT}.other")] = {GROUP: {"USE_SCHEMA"}}  # client-made
    a = _bundle(session)
    b = _bundle(session, space_id="space-2")
    _apply(ws, session, a)
    # Bundle B (same group, other space) also relies on orders.
    ws.serialized_space = _serialized([f"{CAT}.sales.orders"])
    _apply(ws, session, b)
    assert ws.removes() == []

    # Space A drops v_daily_revenue, orders and lookup; gains returns.
    ws.serialized_space = _serialized([f"{CAT}.sales.returns"])
    result = _run_resync(ws, session, a)

    assert result.status == "ok", result.errors
    assert ws.has("table", f"{CAT}.sales.returns", "SELECT")
    assert {g.full_name for g in result.granted} == {f"{CAT}.sales.returns"}
    # Only the app-made, no-longer-needed, unshared grant is revoked.
    assert ws.removes() == [("remove", "table", f"{CAT}.sales.v_daily_revenue", GROUP, "SELECT")]
    assert ws.has("table", f"{CAT}.sales.orders", "SELECT")  # B still needs it
    assert ws.has("table", f"{CAT}.other.lookup", "SELECT")  # not app-made
    assert ws.has("schema", f"{CAT}.other", "USE_SCHEMA")  # not app-made
    released = {g.full_name for g in result.released}
    assert {f"{CAT}.sales.orders", f"{CAT}.other.lookup", f"{CAT}.other"} <= released
    # A no longer has ledger rows for the dropped tables; B inherited ownership.
    assert not any(r.full_name.endswith("orders") for r in _ledger(session, a.id))
    b_orders = [r for r in _ledger(session, b.id) if r.full_name.endswith("orders")]
    assert len(b_orders) == 1 and b_orders[0].app_granted is True


def _run_resync(ws, session, bundle):
    return _apply(ws, session, bundle, resync=True)


# ─── Delete ──────────────────────────────────────────────────────────────────


def test_delete_safe_revoke_respects_other_bundles_and_preexisting(session):
    ws = make_ws(_serialized([f"{CAT}.sales.orders", f"{CAT}.sales.v_daily_revenue"]))
    ws.space_acl["space-2"] = {}
    ws.uc[("table", f"{CAT}.sales.v_daily_revenue")] = {GROUP: {"SELECT"}}  # client-made
    a = _bundle(session)
    b = _bundle(session, space_id="space-2")
    _apply(ws, session, a)
    ws.serialized_space = _serialized([f"{CAT}.sales.orders"])
    _apply(ws, session, b)

    res_a = delete_bundle(ws, session, a)

    assert res_a.deleted and res_a.status == "ok"
    # Shared catalog/schema/orders grants stay for B; only A's space ACL goes.
    assert ws.removes() == []
    assert ("group", GROUP) not in ws.space_acl[SPACE_ID]
    assert ws.has("table", f"{CAT}.sales.orders", "SELECT")
    assert ws.has("table", f"{CAT}.sales.v_daily_revenue", "SELECT")  # never app-made
    assert session.get(GenieAccessBundle, a.id) is None

    res_b = delete_bundle(ws, session, b)

    assert res_b.deleted
    assert set(ws.removes()) == {
        ("remove", "table", f"{CAT}.sales.orders", GROUP, "SELECT"),
        ("remove", "schema", f"{CAT}.sales", GROUP, "USE_SCHEMA"),
        ("remove", "catalog", CAT, GROUP, "USE_CATALOG"),
    }
    assert ws.has("table", f"{CAT}.sales.v_daily_revenue", "SELECT")  # still untouched
    assert _ledger(session) == []


def test_delete_keeps_bundle_and_rows_when_revoke_fails(session):
    ws = make_ws(_serialized([f"{CAT}.sales.orders"]))
    bundle = _bundle(session)
    _apply(ws, session, bundle)
    ws.deny_update.add(("table", f"{CAT}.sales.orders"))

    result = delete_bundle(ws, session, bundle)

    assert not result.deleted and result.status == "partial"
    assert "MANAGE" in result.errors[0]
    remaining = _ledger(session, bundle.id)
    assert [(r.securable_type, r.full_name) for r in remaining] == [("table", f"{CAT}.sales.orders")]
    assert session.get(GenieAccessBundle, bundle.id) is not None


def test_space_acl_left_alone_if_level_was_changed_after_grant(session):
    ws = make_ws(_serialized([f"{CAT}.sales.orders"]))
    bundle = _bundle(session)
    _apply(ws, session, bundle)
    ws.space_acl[SPACE_ID][("group", GROUP)] = "CAN_EDIT"  # someone upgraded it

    result = delete_bundle(ws, session, bundle)

    assert ws.space_acl[SPACE_ID][("group", GROUP)] == "CAN_EDIT"
    assert any("left in place" in w for w in result.warnings)


# ─── Groups ──────────────────────────────────────────────────────────────────


def test_workspace_local_group_is_refused(session):
    ws = make_ws(_serialized([f"{CAT}.sales.orders"]))

    local = check_group(ws, "local-team")
    assert local.exists and not local.is_account_group
    assert "workspace-local" in (local.error or "")
    missing = check_group(ws, "nobody")
    assert not missing.exists and "ACCOUNT-level" in (missing.error or "")
    assert check_group(ws, GROUP).is_account_group
    assert check_group(ws, 'x" or "1"="1').error  # filter injection refused

    user = iam.User(user_name="admin@example.com")
    body = GenieBundleIn(space_id=SPACE_ID, group_name="local-team")
    with pytest.raises(HTTPException) as exc:
        create_genie_bundle(body, ws, session, user)
    assert exc.value.status_code == 400 and "workspace-local" in exc.value.detail
    assert session.exec(select(GenieAccessBundle)).all() == []


def test_apply_blocked_when_group_became_workspace_local(session):
    ws = make_ws(_serialized([f"{CAT}.sales.orders"]), group_type="WorkspaceGroup")
    bundle = _bundle(session)

    with pytest.raises(HTTPException) as exc:
        _run(bundle.id or 0, ws, session, "admin@example.com", revoke_stale=False)

    assert exc.value.status_code == 400 and "workspace-local" in exc.value.detail
    assert ws.updates == [] and ws.space_sets == []


def test_create_then_delete_endpoints_round_trip(session):
    ws = make_ws(_serialized([f"{CAT}.sales.orders"]))
    user = iam.User(user_name="admin@example.com")
    body = GenieBundleIn(space_id=SPACE_ID, group_name=GROUP)

    out = create_genie_bundle(body, ws, session, user)
    assert out.group_id == "g-1" and out.created_by == "admin@example.com"
    with pytest.raises(HTTPException) as dup:
        create_genie_bundle(body, ws, session, user)
    assert dup.value.status_code == 409

    res = delete_genie_bundle(out.id, ws, session)
    assert res.deleted


# ─── Persona template default ────────────────────────────────────────────────


def test_persona_template_genie_default_is_no_permission():
    consumer = {"data_engineer", "data_scientist", "analyst", "support"}
    for persona, cells in DEFAULT_PERMISSIONS_MATRIX.items():
        level = cells[ResourceType.GENIE_SPACES]
        if persona.value in consumer:
            assert level == PermissionLevel.NO_PERMISSIONS, persona
        # No persona hands out consumer Genie access (CAN_READ / CAN_RUN) any more.
        assert level not in (PermissionLevel.CAN_RUN, PermissionLevel.CAN_READ), persona


# ─── Ledger migration ────────────────────────────────────────────────────────


def test_ledger_migration_is_idempotent_on_fresh_db():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        ensure_genie_bundle_schema(conn)
        ensure_genie_bundle_schema(conn)
    with Session(engine) as s:
        row = dict(
            bundle_id=1,
            space_id="s",
            principal=GROUP,
            securable_type="table",
            full_name="c.s.t",
            privilege="SELECT",
        )
        s.add(GenieGrantLedger(**row))
        s.commit()
        with engine.begin() as conn:
            ensure_genie_bundle_schema(conn)  # re-run must keep data
        assert len(s.exec(select(GenieGrantLedger)).all()) == 1
        s.add(GenieGrantLedger(**row))
        with pytest.raises(IntegrityError):
            s.commit()  # (bundle, principal, securable, privilege) is UNIQUE
    engine.dispose()


def test_membership_errors_are_surfaced(session):
    from permissions_app.backend.genie_bundle_routes import add_genie_bundle_member

    bundle = _bundle(session)
    ws = MagicMock()
    ws.groups.get.return_value = iam.Group(id="g-1", members=[])
    ws.groups.patch.side_effect = PermissionDenied("account group")

    with pytest.raises(HTTPException) as exc:
        add_genie_bundle_member(bundle.id or 0, AddMemberIn(user_id="123"), ws, session)
    assert exc.value.status_code == 403 and "account console" in exc.value.detail
