"""Dashboard listing and Lakeview ACL compatibility tests."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from databricks.sdk.service import iam

from permissions_app.backend.models import PermissionLevel, ResourceType
from permissions_app.backend.resources import apply_group_acl, list_resources


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
