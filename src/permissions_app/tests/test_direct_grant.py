"""Tests for direct-grant edge cases (B1–B3) and concurrent add.

B1–B3 use mocked Databricks SDK and a mocked DB session (no live workspace).
The concurrent-add test uses a real SQLite engine to verify the uniqueness
constraint and ON CONFLICT DO NOTHING RETURNING id pattern end-to-end.
"""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from permissions_app.backend.models import (
    PermissionLevel,
    PermissionTemplate,
    PersonaDefinition,
    PersonaGroupMapping,
    PersonaUserMapping,
    ResourceType,
)
from permissions_app.backend.router import (
    apply_permissions,
    remove_persona_member,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_resource(rid, name="res"):
    return SimpleNamespace(id=rid, name=name)


def _template(persona, rt, level):
    t = MagicMock(spec=PermissionTemplate)
    t.persona = persona
    t.resource_type = rt
    t.permission_level = level
    return t


def _persona_def(key, label="Label"):
    d = MagicMock(spec=PersonaDefinition)
    d.key = key
    d.label = label
    d.description = ""
    d.is_default = False
    return d


def _group_mapping(persona, group_id="g1", group_name="Group1"):
    m = MagicMock(spec=PersonaGroupMapping)
    m.persona = persona
    m.group_id = group_id
    m.group_name = group_name
    return m


def _user_mapping(persona, user_name, user_id="uid1", display_name="User"):
    um = MagicMock(spec=PersonaUserMapping)
    um.persona = persona
    um.user_name = user_name
    um.user_id = user_id
    um.display_name = display_name
    return um


# ─── B1: Persona with direct users but NO mapped groups ──────────────────────

def test_b1_direct_users_no_groups_acls_are_synced():
    """Apply on a persona with direct users but NO mapped groups still
    calls apply_user_acl for each direct user — does not short-circuit.
    total_resources_updated must be exactly 1 (1 user × 1 resource)."""
    persona = "analyst"
    rt = ResourceType.WAREHOUSES.value
    level = PermissionLevel.CAN_USE.value

    templates = [_template(persona, rt, level)]
    direct_user = _user_mapping(persona, "alice@example.com")
    persona_def = _persona_def(persona)
    group_mappings: list = []

    def fake_session_exec(stmt):
        result = MagicMock()
        compiled = str(stmt)
        if "persona_definition" in compiled:
            result.first.return_value = persona_def
            result.all.return_value = [persona_def]
        elif "persona_group_mapping" in compiled:
            result.all.return_value = group_mappings
        elif "permission_template" in compiled:
            result.all.return_value = templates
        elif "persona_user_mapping" in compiled:
            result.all.return_value = [direct_user]
        else:
            result.all.return_value = []
            result.first.return_value = None
        return result

    mock_session = MagicMock()
    mock_session.exec.side_effect = fake_session_exec
    mock_ws = MagicMock()

    with (
        patch("permissions_app.backend.router.list_resources", return_value=[_make_resource("wh1", "My Warehouse")]),
        patch("permissions_app.backend.router.apply_group_acl") as mock_group_acl,
        patch("permissions_app.backend.router.apply_user_acl") as mock_user_acl,
        patch("permissions_app.backend.router._effective_user_level", return_value=level),
    ):
        result = apply_permissions(
            persona=persona,
            obo_ws=mock_ws,
            session=mock_session,
        )

    mock_group_acl.assert_not_called()
    mock_user_acl.assert_called_once_with(
        mock_ws, rt, "wh1", {"alice@example.com": level}
    )
    assert result.total_resources_updated == 1, (
        f"Expected total_resources_updated == 1, got {result.total_resources_updated}"
    )


# ─── B2: ACL revoke fails → DB row NOT deleted ───────────────────────────────

def test_b2_acl_revoke_failure_db_row_preserved():
    """If apply_user_acl raises during Remove, the PersonaUserMapping row
    must NOT be deleted (row integrity preserved for retry)."""
    persona = "analyst"
    rt = ResourceType.WAREHOUSES.value
    level = PermissionLevel.CAN_USE.value
    user_name = "bob@example.com"

    templates = [_template(persona, rt, level)]
    persona_def = _persona_def(persona)
    mapping_row = _user_mapping(persona, user_name)

    def fake_exec(stmt):
        result = MagicMock()
        compiled = str(stmt)
        if "persona_definition" in compiled:
            result.first.return_value = persona_def
        elif "persona_user_mapping" in compiled:
            result.first.return_value = mapping_row
        elif "permission_template" in compiled:
            result.all.return_value = templates
        else:
            result.all.return_value = []
            result.first.return_value = None
        return result

    mock_session = MagicMock()
    mock_session.exec.side_effect = fake_exec
    mock_ws = MagicMock()

    with (
        patch("permissions_app.backend.router.list_resources", return_value=[_make_resource("wh1")]),
        patch("permissions_app.backend.router.apply_user_acl", side_effect=Exception("ACL write failed")),
        patch("permissions_app.backend.router._effective_user_level", return_value=PermissionLevel.NO_PERMISSIONS.value),
    ):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            remove_persona_member(
                persona=persona,
                user_name=user_name,
                obo_ws=mock_ws,
                session=mock_session,
            )

    assert exc_info.value.status_code == 502
    mock_session.delete.assert_not_called()


# ─── B3: assignment_type == "both" ───────────────────────────────────────────

def test_b3_both_assignment_type_when_group_and_direct():
    """Members list returns assignment_type='both' when a user is both a direct
    assignee AND a group member — including when SCIM enrichment falls back to
    user_name keying (enrichment failed path)."""
    from permissions_app.backend.router import list_personas

    persona_key = "analyst"
    user_name = "carol@example.com"
    user_id = "9001"

    persona_def = _persona_def(persona_key, "Analyst")
    group_mapping = _group_mapping(persona_key, "gid1", "AnalystGroup")
    user_map = _user_mapping(persona_key, user_name, user_id)
    scim_member = SimpleNamespace(value=user_id, display="Carol")

    def fake_exec(stmt):
        result = MagicMock()
        compiled = str(stmt)
        if "persona_definition" in compiled:
            result.all.return_value = [persona_def]
            result.first.return_value = persona_def
        elif "persona_group_mapping" in compiled:
            result.all.return_value = [group_mapping]
        elif "persona_user_mapping" in compiled:
            result.all.return_value = [user_map]
        else:
            result.all.return_value = []
        return result

    mock_session = MagicMock()
    mock_session.exec.side_effect = fake_exec

    mock_group = MagicMock()
    mock_group.members = [scim_member]

    mock_ws = MagicMock()
    mock_ws.users.list.return_value = []  # enrichment falls back to scim id keying
    mock_ws.groups.get.return_value = mock_group

    personas = list_personas(obo_ws=mock_ws, session=mock_session)

    assert len(personas) == 1
    users = personas[0].users
    assert len(users) == 1, f"Expected 1 user, got {len(users)}: {users}"

    member = users[0]
    assert member.assignment_type == "both", (
        f"Expected assignment_type='both', got '{member.assignment_type}'"
    )
    assert member.user_name == user_name


# ─── Concurrent add: real DB — exercises ON CONFLICT DO NOTHING RETURNING id ──

@pytest.fixture
def sqlite_engine(tmp_path):
    """File-based SQLite engine shared across threads. WAL mode enables concurrent
    writers. All PersonaUserMapping tables are created fresh for each test."""
    db_file = tmp_path / "test_direct_grant.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def set_wal(conn, _rec):
        conn.execute("PRAGMA journal_mode=WAL")

    PersonaUserMapping.__table__.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_concurrent_add_results_in_one_row_and_both_succeed(sqlite_engine):
    """Two simultaneous Add User calls for the same (persona, user) pair must
    result in exactly one DB row with no errors raised by either call.

    This test exercises ON CONFLICT DO NOTHING RETURNING id against a REAL
    SQLite database so the uniqueness constraint is enforced end-to-end.
    Removing ON CONFLICT DO NOTHING from the statement causes an IntegrityError
    on the second insert, which would add to ``errors`` and fail the assertion.

    The test uses engine.begin() context managers (auto-commit on exit) so that
    fetchone() is called inside the transaction — before the implicit commit —
    avoiding SQLite's "SQL statements in progress" limitation with RETURNING.
    """
    errors: list[str] = []
    results: list[bool] = []  # True = inserted new row, False = conflict (no-op)
    barrier = threading.Barrier(2)

    def do_insert():
        try:
            barrier.wait()  # both threads reach the insert at the same time
            with sqlite_engine.begin() as conn:
                stmt = (
                    sqlite_insert(PersonaUserMapping.__table__)
                    .values(
                        user_id="uid42",
                        user_name="dave@example.com",
                        display_name="Dave",
                        persona="analyst",
                    )
                    .on_conflict_do_nothing(index_elements=["persona", "user_name"])
                    .returning(PersonaUserMapping.__table__.c.id)
                )
                result = conn.execute(stmt)
                row = result.fetchone()  # fetchone inside transaction (before commit)
            results.append(row is not None)
        except Exception as e:
            errors.append(str(e))

    t1 = threading.Thread(target=do_insert)
    t2 = threading.Thread(target=do_insert)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"One or both inserts raised an error: {errors}"
    assert len(results) == 2, f"Expected 2 results, got {len(results)}"

    with sqlite_engine.connect() as conn:
        row_count = conn.execute(
            text("SELECT COUNT(*) FROM persona_user_mapping WHERE user_name = 'dave@example.com'")
        ).scalar()
    assert row_count == 1, f"Expected exactly 1 DB row, found {row_count}"

    assert results.count(True) == 1, "Exactly one thread should have inserted a new row"
    assert results.count(False) == 1, "Exactly one thread should have hit the conflict no-op"
