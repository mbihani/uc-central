"""Dashboard listing and Lakeview ACL compatibility tests."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from databricks.sdk.service import iam
from sqlalchemy import create_engine, text

from permissions_app.backend.models import (
    PermissionLevel,
    PermissionMatrixCell,
    ResourceType,
    get_permission_rank,
)
from permissions_app.backend.router import (
    _effective_user_level,
    check_permission_conflicts,
    update_permission_matrix,
)
from permissions_app.backend.resources import apply_group_acl, list_resources
from permissions_app.backend.seed import migrate_dashboard_permission_levels


def test_lakeview_dashboard_lister_uses_paginated_iterator_and_maps_fields():
    """The SDK list iterator transparently fetches every Lakeview page."""
    dashboards = iter(
        [
            SimpleNamespace(
                dashboard_id="d1",
                display_name="Dashboard One",
                lifecycle_state="ACTIVE",
            ),
            SimpleNamespace(
                dashboard_id="d2",
                display_name=None,
                lifecycle_state="ACTIVE",
            ),
        ]
    )
    mock_ws = MagicMock()
    mock_ws.lakeview.list.return_value = dashboards

    items = list_resources(mock_ws, ResourceType.DASHBOARDS.value)

    assert [(item.id, item.name, item.resource_type) for item in items] == [
        ("d1", "Dashboard One", "dashboards"),
        ("d2", "Dashboard d2", "dashboards"),
    ]
    mock_ws.lakeview.list.assert_called_once_with()
    mock_ws.dashboards.list.assert_not_called()


def test_lakeview_dashboard_lister_filters_trashed_dashboards():
    mock_ws = MagicMock()
    mock_ws.lakeview.list.return_value = iter(
        [
            SimpleNamespace(
                dashboard_id="active",
                display_name="Active",
                lifecycle_state=SimpleNamespace(value="ACTIVE"),
            ),
            SimpleNamespace(
                dashboard_id="trashed",
                display_name="Trashed",
                lifecycle_state=SimpleNamespace(value="TRASHED"),
            ),
        ]
    )

    items = list_resources(mock_ws, ResourceType.DASHBOARDS.value)

    assert [item.id for item in items] == ["active"]


def test_dashboard_can_view_is_written_as_lakeview_can_read():
    mock_ws = MagicMock()
    mock_ws.permissions.get.return_value = SimpleNamespace(access_control_list=[])

    apply_group_acl(
        mock_ws,
        ResourceType.DASHBOARDS.value,
        "dashboard-id",
        {"analysts": PermissionLevel.CAN_VIEW.value},
    )

    mock_ws.permissions.get.assert_called_once_with(
        request_object_type="dashboards", request_object_id="dashboard-id"
    )
    mock_ws.permissions.set.assert_called_once()
    call = mock_ws.permissions.set.call_args.kwargs
    assert call["request_object_type"] == "dashboards"
    assert call["request_object_id"] == "dashboard-id"
    assert len(call["access_control_list"]) == 1
    assert call["access_control_list"][0].group_name == "analysts"
    assert call["access_control_list"][0].permission_level == iam.PermissionLevel.CAN_READ


def test_legacy_dashboard_can_view_has_same_rank_as_can_read():
    assert get_permission_rank("dashboards", "CAN_VIEW") == get_permission_rank(
        "dashboards", "CAN_READ"
    )


def test_effective_user_level_normalizes_stored_dashboard_can_view():
    mapping = SimpleNamespace(persona="support")
    template = SimpleNamespace(permission_level="CAN_VIEW")
    mock_session = MagicMock()
    mock_session.exec.side_effect = [
        SimpleNamespace(all=lambda: [mapping]),
        SimpleNamespace(first=lambda: template),
    ]

    level = _effective_user_level(mock_session, "user@example.com", "dashboards")

    assert level == "CAN_READ"


def test_conflict_check_treats_dashboard_can_view_and_can_read_as_equal():
    definitions = [
        SimpleNamespace(key="support", label="Support"),
        SimpleNamespace(key="analyst", label="Analyst"),
    ]
    mappings = [
        SimpleNamespace(group_name="support-group", persona="support"),
        SimpleNamespace(group_name="analyst-group", persona="analyst"),
    ]
    templates = [
        SimpleNamespace(
            persona="support",
            resource_type="dashboards",
            permission_level="CAN_VIEW",
        ),
        SimpleNamespace(
            persona="analyst",
            resource_type="dashboards",
            permission_level="CAN_READ",
        ),
    ]
    mock_session = MagicMock()
    mock_session.exec.side_effect = [
        SimpleNamespace(all=lambda: definitions),
        SimpleNamespace(all=lambda: mappings),
        SimpleNamespace(all=lambda: templates),
    ]
    mock_ws = MagicMock()
    mock_ws.users.list.return_value = [
        SimpleNamespace(
            id="u1",
            user_name="user@example.com",
            display_name="User",
            groups=[
                SimpleNamespace(display="support-group"),
                SimpleNamespace(display="analyst-group"),
            ],
        )
    ]

    result = check_permission_conflicts(mock_ws, mock_session)

    assert result.users_with_conflicts == 0
    assert result.conflicts == []


def test_matrix_update_stores_canonical_dashboard_can_read():
    template = SimpleNamespace(
        id=1,
        persona="support",
        resource_type="dashboards",
        permission_level="CAN_VIEW",
    )
    mock_session = MagicMock()
    mock_session.exec.return_value.first.return_value = template
    body = PermissionMatrixCell(
        persona="support",
        resource_type=ResourceType.DASHBOARDS,
        permission_level=PermissionLevel.CAN_VIEW,
    )

    result = update_permission_matrix(body, mock_session)

    assert template.permission_level == "CAN_READ"
    assert result.permission_level == "CAN_READ"


def test_dashboard_permission_migration_is_idempotent():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE permission_template ("
                "id INTEGER PRIMARY KEY, resource_type TEXT, permission_level TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO permission_template "
                "(resource_type, permission_level) VALUES ('dashboards', 'CAN_VIEW')"
            )
        )

        migrate_dashboard_permission_levels(conn)
        migrate_dashboard_permission_levels(conn)

        rows = conn.execute(
            text("SELECT resource_type, permission_level FROM permission_template")
        ).all()
    assert rows == [("dashboards", "CAN_READ")]
