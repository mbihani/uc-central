"""Idempotent, concurrency-safe database bootstrap + default seeding.

Startup runs `uvicorn ... --workers 2`, so up to N worker processes initialise
the schema and seed defaults *concurrently*. Everything here is written so that
N concurrent bootstraps converge on exactly ONE clean seed set — no duplicate
rows, no DDL race, no startup crash:

  * ``ensure_unique_constraints`` de-duplicates any pre-existing rows and adds
    the UNIQUE constraints IF they are missing (guarded by ``pg_constraint`` so
    it is a no-op on the second run and on already-migrated databases).
  * seeding uses Postgres ``INSERT ... ON CONFLICT DO NOTHING`` against those
    UNIQUE constraints, so a second (racing or repeated) seed inserts nothing.

The startup path (``runtime.initialize_models``) additionally wraps the whole
bootstrap in a Postgres advisory lock so only one worker performs the DDL at a
time; the ON CONFLICT seeding here is the belt-and-suspenders that keeps the
lazy per-request seed paths (``router._seed_*``) safe as well.
"""

from sqlalchemy import Connection, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session

from .defaults import DEFAULT_PERMISSIONS_MATRIX
from .logger import logger
from .models import (
    DEFAULT_PERSONA_DESCRIPTIONS,
    DEFAULT_PERSONA_LABELS,
    DefaultPersona,
    PermissionTemplate,
    PersonaDefinition,
)

# Stable, arbitrary 63-bit key so every worker/process contends on the SAME
# Postgres advisory lock during startup bootstrap (see runtime.initialize_models).
BOOTSTRAP_LOCK_KEY = 0x7065726D5F696E69  # "perm_ini"

# (table, columns, constraint_name) for every UNIQUE constraint we must ensure
# on databases that were created BEFORE the constraint existed in the model.
# create_all() only creates constraints for NEW tables; it never ALTERs an
# existing table, so the live table needs an explicit, idempotent migration.
_UNIQUE_CONSTRAINTS: list[tuple[str, tuple[str, ...], str]] = [
    (
        "permission_template",
        ("persona", "resource_type"),
        "uq_permission_template_persona_resource_type",
    ),
    (
        "persona_group_mapping",
        ("group_id",),
        "uq_persona_group_mapping_group_id",
    ),
    (
        "persona_user_mapping",
        ("persona", "user_name"),
        "uq_persona_user_mapping_persona_user_name",
    ),
]


def _persona_definition_rows() -> list[dict]:
    return [
        {
            "key": p.value,
            "label": DEFAULT_PERSONA_LABELS.get(p.value, p.value),
            "description": DEFAULT_PERSONA_DESCRIPTIONS.get(p.value, ""),
            "is_default": True,
        }
        for p in DefaultPersona
    ]


def _permission_template_rows() -> list[dict]:
    return [
        {
            "persona": persona.value,
            "resource_type": resource_type.value,
            "permission_level": perm_level.value,
        }
        for persona, resource_map in DEFAULT_PERMISSIONS_MATRIX.items()
        for resource_type, perm_level in resource_map.items()
    ]


def _persona_definition_stmt():
    # ON CONFLICT on the existing UNIQUE index over persona_definition.key.
    return (
        pg_insert(PersonaDefinition.__table__)
        .values(_persona_definition_rows())
        .on_conflict_do_nothing(index_elements=["key"])
    )


def _permission_template_stmt():
    # ON CONFLICT on the (persona, resource_type) UNIQUE constraint.
    return (
        pg_insert(PermissionTemplate.__table__)
        .values(_permission_template_rows())
        .on_conflict_do_nothing(index_elements=["persona", "resource_type"])
    )


def ensure_unique_constraints(conn: Connection) -> None:
    """De-dup + add the UNIQUE constraints IF missing. Idempotent & safe to run
    concurrently only while holding the bootstrap advisory lock (the caller does).

    Table/column/constraint names are internal constants (never user input), so
    interpolating them into the DDL is safe.
    """
    for table, cols, name in _UNIQUE_CONSTRAINTS:
        exists = conn.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
            {"name": name},
        ).first()
        if exists:
            continue

        # Remove any pre-existing duplicates (keep the lowest id) so the UNIQUE
        # constraint can be added. On a healthy DB this deletes nothing.
        match_cols = " AND ".join(f"a.{c} = b.{c}" for c in cols)
        deleted = conn.execute(
            text(
                f"DELETE FROM {table} a USING {table} b "
                f"WHERE a.id > b.id AND {match_cols}"
            )
        ).rowcount
        if deleted:
            logger.warning(
                f"Removed {deleted} duplicate row(s) from {table} before adding "
                f"UNIQUE constraint {name}"
            )

        conn.execute(
            text(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                f"UNIQUE ({', '.join(cols)})"
            )
        )
        logger.info(f"Added UNIQUE constraint {name} on {table}({', '.join(cols)})")


def seed_defaults(conn: Connection) -> None:
    """Seed persona definitions + permission templates on a CONNECTION, without
    committing (the caller's transaction owns the commit). Idempotent."""
    conn.execute(_persona_definition_stmt())
    conn.execute(_permission_template_stmt())


def seed_persona_definitions(session: Session) -> None:
    """Idempotently seed default persona definitions (lazy per-request path)."""
    session.execute(_persona_definition_stmt())
    session.commit()
    logger.info("Seeded default persona definitions")


def seed_permission_templates(session: Session) -> None:
    """Idempotently seed the permission template matrix (lazy per-request path)."""
    session.execute(_permission_template_stmt())
    session.commit()
    logger.info("Seeded default permission templates from blog matrix")
