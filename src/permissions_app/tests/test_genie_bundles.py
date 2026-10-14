"""Tests for Genie access bundles.

A stateful fake WorkspaceClient (UC grants, Genie-space ACLs, tables, groups)
stands in for the workspace, and a real SQLite database backs the bundle +
grant-ledger tables, so apply / resync / delete are exercised end-to-end
against the actual ledger logic.
"""

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service import catalog, iam
from fastapi import HTTPException
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from permissions_app.backend import genie_bundles as gb
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
    GENIE_BUNDLE_SPACE_UNIQUE,
    LEDGER_CONFIRMED,
    LEDGER_PENDING,
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
        # SCIM groups by immutable id: id -> (display_name, resourceType)
        self.groups_by_id: dict[str, tuple[str, str]] = {
            "g-1": (GROUP, group_type),
            "g-2": ("local-team", "WorkspaceGroup"),
        }

        self.genie = SimpleNamespace(get_space=self._get_space)
        self.tables = SimpleNamespace(get=self._get_table)
        self.volumes = SimpleNamespace(read=self._read_volume)
        self.grants = SimpleNamespace(get_effective=self._get_effective, update=self._update)
        self.permissions = SimpleNamespace(get=self._perm_get, set=self._perm_set)
        self.groups = SimpleNamespace(list=self._groups_list, get=self._groups_get)

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
        return [
            iam.Group(id=gid, display_name=name, meta=iam.ResourceMeta(resource_type=rtype))
            for gid, (name, rtype) in self.groups_by_id.items()
            if filter == f'displayName eq "{name}"'
        ]

    def _groups_get(self, id):
        if id not in self.groups_by_id:
            raise NotFound(f"Group with id {id} not found.")
        name, rtype = self.groups_by_id[id]
        return iam.Group(id=id, display_name=name, meta=iam.ResourceMeta(resource_type=rtype), members=[])

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
    ws.groups.get.return_value = iam.Group(
        id="g-1", display_name=GROUP, members=[], meta=iam.ResourceMeta(resource_type="Group")
    )
    ws.groups.patch.side_effect = PermissionDenied("account group")

    with pytest.raises(HTTPException) as exc:
        add_genie_bundle_member(bundle.id or 0, AddMemberIn(user_id="123"), ws, session)
    assert exc.value.status_code == 403 and "account console" in exc.value.detail


# ═══ Review fixes: concurrency, pending rows, group identity, one-per-space ═══

PG_URL = os.environ.get("UCG_TEST_PG_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set UCG_TEST_PG_URL=postgresql+psycopg://... to run Postgres tests"
)
ORDERS = f"{CAT}.sales.orders"
ORDERS_SELECT = GrantKey("table", ORDERS, "SELECT")


def _shared_setup(ws, session):
    """Bundles A (SPACE_ID) and B (space-2), same group, both relying on
    orders SELECT + sales USE_SCHEMA + catalog USE_CATALOG (app-granted by A)."""
    ws.space_acl["space-2"] = {}
    a = _bundle(session)
    b = _bundle(session, space_id="space-2")
    _apply(ws, session, a)
    _apply(ws, session, b)
    return a, b


# ─── 1. Safe-revoke serialization ────────────────────────────────────────────


def test_release_decision_holds_the_per_grant_lock(session, monkeypatch):
    """Deterministic interleaving: while bundle A is inside its release
    decision for the shared orders grant (after reading the other bundles'
    rows, before deleting its own / revoking), a contender for the SAME grant
    lock must block, while a different grant's lock stays free."""
    ws = make_ws(_serialized([ORDERS]))
    a, _ = _shared_setup(ws, session)
    probe: dict[str, Any] = {}
    real = gb._ledger_for_grant

    def contend(principal, key):
        entered = threading.Event()

        def run():
            with Session(create_engine("sqlite://")) as s2:
                with gb._grant_lock(s2, principal, key):
                    entered.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return entered, t

    def hooked(sess, principal, key):
        rows = real(sess, principal, key)
        if key == ORDERS_SELECT and "same" not in probe:
            same, t_same = contend(principal, key)
            other, _ = contend(principal, GrantKey("table", f"{CAT}.x.y", "SELECT"))
            probe["same"] = same.wait(0.3)  # must NOT get in
            probe["other"] = other.wait(1.0)  # unrelated grant: free
            probe["thread"], probe["event"] = t_same, same
        return rows

    monkeypatch.setattr(gb, "_ledger_for_grant", hooked)
    delete_bundle(ws, session, a)

    assert probe["same"] is False, "same-grant contender entered the critical section"
    assert probe["other"] is True
    probe["thread"].join(2)
    assert probe["event"].is_set()  # released once A's decision committed


def test_grant_lock_key_is_stable_and_distinct():
    k1 = gb.grant_lock_key(GROUP, ORDERS_SELECT)
    assert k1 == gb.grant_lock_key(GROUP, GrantKey("table", ORDERS, "SELECT"))
    assert k1 != gb.grant_lock_key("other-group", ORDERS_SELECT)
    assert k1 != gb.grant_lock_key(GROUP, GrantKey("schema", ORDERS, "SELECT"))
    assert -(2**63) <= k1 < 2**63


@pytest.fixture
def pg_engine():
    assert PG_URL
    engine = create_engine(PG_URL)
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


@requires_pg
@pytest.mark.parametrize("action", ["delete", "resync"])
def test_concurrent_release_of_shared_grants_postgres(pg_engine, action, monkeypatch):
    """Real Postgres (READ COMMITTED + pg_advisory_xact_lock): A and B share the
    orders table, sales schema and catalog grants. Releasing both concurrently
    (race window widened) must revoke each shared app-granted privilege exactly
    once and leave no orphan — without the lock both would see the other's row
    and nobody would revoke."""
    ws = make_ws(_serialized([ORDERS]))
    with Session(pg_engine) as s:
        a, b = _shared_setup(ws, s)
        ids = [a.id, b.id]
    real = gb._ledger_for_grant

    def slow(sess, principal, key):
        rows = real(sess, principal, key)
        time.sleep(0.2)
        return rows

    monkeypatch.setattr(gb, "_ledger_for_grant", slow)
    if action == "resync":
        # Both spaces drop orders (and the sales schema); the catalog stays needed.
        ws.serialized_space = _serialized([f"{CAT}.other.lookup"])
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def worker(bundle_id):
        try:
            with Session(pg_engine) as s:
                bundle = s.get(GenieAccessBundle, bundle_id)
                assert bundle is not None
                barrier.wait()
                if action == "delete":
                    res = delete_bundle(ws, s, bundle)
                    assert res.deleted, res
                else:
                    res = _apply(ws, s, bundle, resync=True)
                assert res.errors == [], res.errors
        except Exception as e:  # surfaced below
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []

    removes = ws.removes()
    expected = [("table", ORDERS, "SELECT"), ("schema", f"{CAT}.sales", "USE_SCHEMA")]
    if action == "delete":
        expected.append(("catalog", CAT, "USE_CATALOG"))
    for typ, name, priv in expected:
        assert removes.count(("remove", typ, name, GROUP, priv)) == 1, (typ, removes)
        assert not ws.has(typ, name, priv)
    with Session(pg_engine) as s:
        rows = s.exec(select(GenieGrantLedger)).all()
        if action == "delete":
            assert rows == []
        else:
            assert ws.has("catalog", CAT, "USE_CATALOG")
            assert ws.has("table", f"{CAT}.other.lookup", "SELECT")
            names = {r.full_name for r in rows}
            assert ORDERS not in names and f"{CAT}.sales" not in names
            assert all(r.state == LEDGER_CONFIRMED for r in rows)


# ─── 2. Grant-before-ledger: pending rows ────────────────────────────────────


def test_crash_after_grant_leaves_pending_row_that_retry_confirms(session, monkeypatch):
    ws = make_ws(_serialized([ORDERS]))
    bundle = _bundle(session)
    real_grant = gb._grant

    def grant_then_crash(w, principal, key):
        real_grant(w, principal, key)
        if key == ORDERS_SELECT:
            raise RuntimeError("process died / DB unreachable after the grant call")

    monkeypatch.setattr(gb, "_grant", grant_then_crash)
    with pytest.raises(RuntimeError):
        _apply(ws, session, bundle)
    session.rollback()

    assert ws.has("table", ORDERS, "SELECT")  # the workspace grant happened
    row = next(r for r in _ledger(session, bundle.id) if r.full_name == ORDERS)
    assert row.state == LEDGER_PENDING and row.app_granted  # ...and is tracked

    monkeypatch.setattr(gb, "_grant", real_grant)
    adds_before = len([u for u in ws.updates if u[0] == "add"])
    retry = _apply(ws, session, bundle)

    assert retry.status == "ok", retry.errors
    assert [(g.full_name, g.status) for g in retry.reconciled] == [(ORDERS, "confirmed")]
    assert len([u for u in ws.updates if u[0] == "add"]) == adds_before  # no re-grant
    rows = _ledger(session, bundle.id)
    assert all(r.state == LEDGER_CONFIRMED for r in rows)
    # Tracked => revocable: delete removes the app-created grant.
    delete_bundle(ws, session, bundle)
    assert not ws.has("table", ORDERS, "SELECT")


def test_crash_before_grant_pending_row_is_dropped_then_regranted(session, monkeypatch):
    ws = make_ws(_serialized([ORDERS]))
    bundle = _bundle(session)
    real_grant = gb._grant

    def crash_before(w, principal, key):
        if key == ORDERS_SELECT:
            raise RuntimeError("died before the grant call")
        real_grant(w, principal, key)

    monkeypatch.setattr(gb, "_grant", crash_before)
    with pytest.raises(RuntimeError):
        _apply(ws, session, bundle)
    session.rollback()
    assert not ws.has("table", ORDERS, "SELECT")
    assert [r.state for r in _ledger(session, bundle.id) if r.full_name == ORDERS] == [LEDGER_PENDING]

    # A pending row counts as "still needed" for other bundles and is surfaced.
    preview = to_preview(bundle, build_plan(ws, session, bundle))
    assert any("PENDING" in w for w in preview.warnings)

    monkeypatch.setattr(gb, "_grant", real_grant)
    retry = _apply(ws, session, bundle)
    assert [(g.full_name, g.status) for g in retry.reconciled] == [(ORDERS, "dropped")]
    assert ws.has("table", ORDERS, "SELECT")
    row = next(r for r in _ledger(session, bundle.id) if r.full_name == ORDERS)
    assert row.state == LEDGER_CONFIRMED and row.app_granted


def test_failed_grant_does_not_leave_a_pending_row(fixture_space, session):
    ws = make_ws(fixture_space["serialized_space"])
    ws.deny_update.add(("table", f"{CAT}.finance.revenue_metrics"))
    bundle = _bundle(session)
    _apply(ws, session, bundle)
    assert all(r.state == LEDGER_CONFIRMED for r in _ledger(session, bundle.id))
    assert not any(r.full_name.endswith("revenue_metrics") for r in _ledger(session, bundle.id))


def test_only_the_expected_unique_conflict_is_tolerated(session):
    bundle = _bundle(session)
    row = dict(
        bundle_id=bundle.id,
        space_id=SPACE_ID,
        principal=GROUP,
        securable_type="table",
        full_name=ORDERS,
        privilege="SELECT",
    )
    session.add(GenieGrantLedger(**row))
    session.commit()
    session.add(GenieGrantLedger(**row))
    with pytest.raises(IntegrityError) as dup:
        session.commit()
    session.rollback()
    assert gb.is_unique_violation(dup.value, gb.LEDGER_UNIQUE, "genie_grant_ledger")
    assert not gb.is_unique_violation(dup.value, gb.LEDGER_UNIQUE, "genie_access_bundle")

    session.add(GenieGrantLedger(**{**row, "full_name": "c.s.other", "principal": None}))
    with pytest.raises(IntegrityError) as notnull:
        session.commit()
    session.rollback()
    assert not gb.is_unique_violation(notnull.value, gb.LEDGER_UNIQUE, "genie_grant_ledger")
    with pytest.raises(IntegrityError):
        session.add(GenieGrantLedger(**{**row, "full_name": "c.s.other", "principal": None}))
        gb._commit(session)  # a non-uniqueness integrity error is NOT swallowed
    session.rollback()


# ─── 3. Principal identity (group id) ────────────────────────────────────────


def _rename(ws, gid, new_name):
    """UC/ACL grants follow the principal id, so a rename re-labels them."""
    old, rtype = ws.groups_by_id[gid]
    ws.groups_by_id[gid] = (new_name, rtype)
    for holders in ws.uc.values():
        if old in holders:
            holders[new_name] = holders.pop(old)
    for acl in ws.space_acl.values():
        if ("group", old) in acl:
            acl[("group", new_name)] = acl.pop(("group", old))


def test_group_rename_is_followed_by_id(session):
    ws = make_ws(_serialized([ORDERS]))
    bundle = _bundle(session)
    _apply(ws, session, bundle)
    _rename(ws, "g-1", "genie-sales-renamed")
    ws.serialized_space = _serialized([ORDERS, f"{CAT}.sales.returns"])
    n_updates = len(ws.updates)

    result = _apply(ws, session, bundle, resync=True)

    session.refresh(bundle)
    assert bundle.group_name == "genie-sales-renamed"
    assert {r.principal for r in _ledger(session, bundle.id)} == {"genie-sales-renamed"}
    # Only the new table is granted — to the CURRENT name; nothing to the old name.
    assert ws.updates[n_updates:] == [
        ("add", "table", f"{CAT}.sales.returns", "genie-sales-renamed", "SELECT")
    ]
    assert result.revoked == [] and result.errors == []
    delete_bundle(ws, session, bundle)
    assert not ws.has("table", ORDERS, "SELECT", principal="genie-sales-renamed")


def test_deleted_group_recreated_with_same_name_is_blocked(session):
    ws = make_ws(_serialized([ORDERS]))
    bundle = _bundle(session)
    _apply(ws, session, bundle)
    del ws.groups_by_id["g-1"]
    ws.groups_by_id["g-9"] = (GROUP, "Group")  # same name, different principal
    n_updates, n_sets = len(ws.updates), len(ws.space_sets)

    preview = to_preview(bundle, build_plan(ws, session, bundle))
    assert not preview.can_apply
    assert any("no longer exists" in b for b in preview.blockers)
    with pytest.raises(HTTPException) as apply_exc:
        _run(bundle.id or 0, ws, session, "admin@example.com", revoke_stale=True)
    assert apply_exc.value.status_code == 400 and "same name" in apply_exc.value.detail
    with pytest.raises(gb.BundleBlocked):
        delete_bundle(ws, session, bundle)
    with pytest.raises(HTTPException) as del_exc:
        delete_genie_bundle(bundle.id or 0, ws, session)
    assert del_exc.value.status_code == 400
    from permissions_app.backend.genie_bundle_routes import add_genie_bundle_member

    with pytest.raises(HTTPException) as mem_exc:
        add_genie_bundle_member(bundle.id or 0, AddMemberIn(user_id="123"), ws, session)
    assert mem_exc.value.status_code == 400

    assert len(ws.updates) == n_updates and len(ws.space_sets) == n_sets
    assert session.get(GenieAccessBundle, bundle.id) is not None

    # Escape hatch: forget the orphaned records (no workspace calls), which
    # frees the space for a new bundle on the new group.
    forgot = delete_genie_bundle(bundle.id or 0, ws, session, forget_orphaned=True)
    assert forgot.deleted and len(ws.updates) == n_updates and len(ws.space_sets) == n_sets
    assert _ledger(session) == []
    user = iam.User(user_name="admin@example.com")
    new = create_genie_bundle(GenieBundleIn(space_id=SPACE_ID, group_name=GROUP), ws, session, user)
    assert new.group_id == "g-9"


def test_forget_orphaned_refused_while_group_exists(session):
    ws = make_ws(_serialized([ORDERS]))
    bundle = _bundle(session)
    _apply(ws, session, bundle)
    res = delete_bundle(ws, session, bundle, forget_orphaned=True)  # group fine: normal delete
    assert res.deleted and not ws.has("table", ORDERS, "SELECT")

    ws2 = make_ws(_serialized([ORDERS]), group_type="WorkspaceGroup")
    b2 = _bundle(session)
    with pytest.raises(gb.BundleBlocked, match="only allowed"):
        delete_bundle(ws2, session, b2, forget_orphaned=True)  # exists, just not account-level


# ─── 4. One group per space ──────────────────────────────────────────────────


def test_one_bundle_per_space(session):
    ws = make_ws(_serialized([ORDERS]))
    ws.groups_by_id["g-3"] = ("genie-other", "Group")
    user = iam.User(user_name="admin@example.com")
    create_genie_bundle(GenieBundleIn(space_id=SPACE_ID, group_name=GROUP), ws, session, user)

    with pytest.raises(HTTPException) as exc:
        create_genie_bundle(GenieBundleIn(space_id=SPACE_ID, group_name="genie-other"), ws, session, user)
    assert exc.value.status_code == 409 and "already has a bundle" in exc.value.detail

    session.add(GenieAccessBundle(space_id=SPACE_ID, group_name="genie-other", group_id="g-3"))
    with pytest.raises(IntegrityError) as db_exc:
        session.commit()  # enforced by the DB, not just the route
    session.rollback()
    assert gb.is_unique_violation(db_exc.value, GENIE_BUNDLE_SPACE_UNIQUE, "genie_access_bundle")


def _v1_schema(engine):
    """The tables exactly as the first version of this PR created them."""
    md = MetaData()
    Table(
        "genie_access_bundle", md,
        Column("id", Integer, primary_key=True),
        Column("space_id", String, nullable=False, index=True),
        Column("space_title", String, nullable=False),
        Column("group_name", String, nullable=False, index=True),
        Column("group_id", String, nullable=False),
        Column("created_by", String, nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("last_applied_at", DateTime),
        Column("last_applied_by", String),
        Column("restriction_mode", String, nullable=False),
        UniqueConstraint("space_id", "group_name", name="uq_genie_access_bundle_space_group"),
    )
    Table(
        "genie_grant_ledger", md,
        Column("id", Integer, primary_key=True),
        Column("bundle_id", Integer, nullable=False, index=True),
        Column("space_id", String, nullable=False),
        Column("principal", String, nullable=False),
        Column("securable_type", String, nullable=False),
        Column("full_name", String, nullable=False),
        Column("privilege", String, nullable=False),
        Column("app_granted", Boolean, nullable=False),
        Column("granted_at", DateTime, nullable=False),
        Column("granted_by", String, nullable=False),
        UniqueConstraint(
            "bundle_id", "principal", "securable_type", "full_name", "privilege",
            name="uq_genie_grant_ledger_bundle_grant",
        ),
    )
    md.drop_all(engine)
    md.create_all(engine)


def _v1_bundle(conn, bid, space, group):
    conn.execute(
        text(
            "INSERT INTO genie_access_bundle (id, space_id, space_title, group_name, group_id,"
            " created_by, created_at, restriction_mode) VALUES (:i, :s, 't', :g, 'g', 'a',"
            " CURRENT_TIMESTAMP, 'none')"
        ),
        {"i": bid, "s": space, "g": group},
    )


def _migration_engines():
    engines = [pytest.param("sqlite", id="sqlite")]
    engines.append(pytest.param("postgres", id="postgres", marks=requires_pg))
    return engines


def _engine_for(kind, tmp_path):
    if kind == "sqlite":
        return create_engine(f"sqlite:///{tmp_path / 'v1.db'}")
    engine = create_engine(PG_URL or "")
    SQLModel.metadata.drop_all(engine)
    return engine


@pytest.mark.parametrize("kind", _migration_engines())
def test_migration_upgrades_v1_db_idempotently(kind, tmp_path):
    engine = _engine_for(kind, tmp_path)
    _v1_schema(engine)
    with engine.begin() as conn:
        _v1_bundle(conn, 1, "space-a", GROUP)
        conn.execute(
            text(
                "INSERT INTO genie_grant_ledger (bundle_id, space_id, principal, securable_type,"
                " full_name, privilege, app_granted, granted_at, granted_by) VALUES"
                " (1, 'space-a', :g, 'table', 'c.s.t', 'SELECT', :t, CURRENT_TIMESTAMP, 'a')"
            ),
            {"g": GROUP, "t": True},
        )
    for _ in range(2):  # idempotent
        with engine.begin() as conn:
            ensure_genie_bundle_schema(conn)

    insp = sa_inspect(engine)
    assert "state" in {c["name"] for c in insp.get_columns("genie_grant_ledger")}
    assert GENIE_BUNDLE_SPACE_UNIQUE in {i["name"] for i in insp.get_indexes("genie_access_bundle")}
    with Session(engine) as s:
        (row,) = s.exec(select(GenieGrantLedger)).all()
        assert row.state == LEDGER_CONFIRMED and row.app_granted  # data preserved
        s.add(GenieAccessBundle(space_id="space-a", group_name="x", group_id="g-x"))
        with pytest.raises(IntegrityError):
            s.commit()
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


@pytest.mark.parametrize("kind", _migration_engines())
def test_migration_fails_loudly_on_duplicate_bundles_per_space(kind, tmp_path):
    engine = _engine_for(kind, tmp_path)
    _v1_schema(engine)
    with engine.begin() as conn:
        _v1_bundle(conn, 1, "space-a", GROUP)
        _v1_bundle(conn, 2, "space-a", "genie-other")
        _v1_bundle(conn, 3, "space-b", GROUP)

    with pytest.raises(RuntimeError, match="space space-a: 2 bundles"):
        with engine.begin() as conn:
            ensure_genie_bundle_schema(conn)

    with engine.connect() as conn:  # nothing was silently dropped
        assert conn.execute(text("SELECT COUNT(*) FROM genie_access_bundle")).scalar() == 3
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


def test_migration_on_fresh_db_creates_unique_space_index():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        ensure_genie_bundle_schema(conn)
        ensure_genie_bundle_schema(conn)
    insp = sa_inspect(engine)
    assert GENIE_BUNDLE_SPACE_UNIQUE in {i["name"] for i in insp.get_indexes("genie_access_bundle")}
    assert "state" in {c["name"] for c in insp.get_columns("genie_grant_ledger")}
    engine.dispose()


# ─── 5. Empty discovery ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "serialized",
    [
        pytest.param(None, id="no-serialized-space"),
        pytest.param(json.dumps({"version": 2}), id="data_sources-absent"),
        pytest.param(json.dumps({"version": 2, "data_sources": {}}), id="data_sources-empty"),
        pytest.param(json.dumps({"version": 2, "data_sources": {"tables": []}}), id="tables-empty"),
        pytest.param(json.dumps({"version": 9, "sources": [{"name": "c.s.t"}]}), id="unrecognized-format"),
        pytest.param(json.dumps({"data_sources": {"tables": [{"table": "c.s.t"}]}}), id="entries-without-identifier"),
        pytest.param(_serialized([f"{CAT}.sales.dropped_table"]), id="nothing-resolves"),
    ],
)
def test_empty_discovery_blocks_apply(session, serialized):
    ws = make_ws(serialized)
    ws.serialized_space = serialized
    bundle = _bundle(session)

    preview = to_preview(bundle, build_plan(ws, session, bundle))
    assert not preview.can_apply
    assert any(b.startswith("Nothing to grant") for b in preview.blockers), preview.blockers
    with pytest.raises(HTTPException) as exc:
        _run(bundle.id or 0, ws, session, "admin@example.com", revoke_stale=False)
    assert exc.value.status_code == 400 and "Nothing to grant" in exc.value.detail
    assert ws.updates == [] and ws.space_sets == [] and _ledger(session) == []
