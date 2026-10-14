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

from sqlalchemy import Connection, inspect, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, SQLModel

from .defaults import DEFAULT_PERMISSIONS_MATRIX
from .logger import logger
from .models import (
    DEFAULT_PERSONA_DESCRIPTIONS,
    DEFAULT_PERSONA_LABELS,
    GENIE_BUNDLE_SPACE_UNIQUE,
    GENIE_LEDGER_GROUP_ID_CHECK,
    LEDGER_CONFIRMED,
    DefaultPersona,
    GenieAccessBundle,
    GenieGrantLedger,
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


def ensure_genie_bundle_schema(conn: Connection) -> None:
    """Create / upgrade the Genie access bundle + grant ledger tables.

    Idempotent, on a fresh database AND on one created by the first version of
    this feature (which allowed several bundles per space and had no ledger
    ``state`` column). Never drops or rewrites data:

      * ``create_all(checkfirst=True)`` creates missing tables (fresh DB: with
        every column and index already in place).
      * ``genie_grant_ledger.state`` is ADDed if missing; existing rows were all
        written after their grant succeeded, so they default to ``confirmed``.
      * ``genie_grant_ledger.group_id`` (the lock / sharing key) is ADDed if
        missing and backfilled from each row's bundle; its index is created.
        If ANY row still has no group_id (its bundle is missing — there is no
        FK to prevent that) the migration FAILS LOUDLY listing the row and
        bundle ids, deleting nothing. On Postgres the transitional '' default is
        then dropped and a non-empty CHECK added (fresh DBs get both from the
        model).
      * the one-bundle-per-space UNIQUE index is created if missing. If the DB
        already holds several bundles for one space the migration FAILS LOUDLY
        (RuntimeError, aborting startup) naming them, rather than silently
        deleting a bundle whose grants are recorded in the ledger.
    """
    SQLModel.metadata.create_all(
        conn,
        tables=[
            SQLModel.metadata.tables[GenieAccessBundle.__tablename__],
            SQLModel.metadata.tables[GenieGrantLedger.__tablename__],
        ],
        checkfirst=True,
    )
    insp = inspect(conn)
    ledger = GenieGrantLedger.__tablename__
    bundles = GenieAccessBundle.__tablename__
    if "state" not in {c["name"] for c in insp.get_columns(ledger)}:
        conn.execute(
            text(
                f"ALTER TABLE {ledger} ADD COLUMN state VARCHAR NOT NULL "
                f"DEFAULT '{LEDGER_CONFIRMED}'"
            )
        )
        logger.info(f"Added column {ledger}.state")
    if "group_id" not in {c["name"] for c in insp.get_columns(ledger)}:
        conn.execute(
            text(f"ALTER TABLE {ledger} ADD COLUMN group_id VARCHAR NOT NULL DEFAULT ''")
        )
        logger.info(f"Added column {ledger}.group_id")
    # Backfill (idempotent: only rows still missing it) from the owning bundle.
    backfilled = conn.execute(
        text(
            f"UPDATE {ledger} SET group_id = (SELECT b.group_id FROM "
            f"{GenieAccessBundle.__tablename__} b WHERE b.id = {ledger}.bundle_id) "
            f"WHERE (group_id IS NULL OR group_id = '') AND EXISTS (SELECT 1 FROM "
            f"{GenieAccessBundle.__tablename__} b WHERE b.id = {ledger}.bundle_id)"
        )
    ).rowcount
    if backfilled:
        logger.info(f"Backfilled group_id on {backfilled} {ledger} row(s)")
    # Rows that could not be backfilled (their bundle is gone — there is no
    # foreign key preventing that) would lose their identity: they would share
    # one lock / "still needed" key with every other such row and could never
    # be revoked safely. Refuse to start instead of guessing or deleting.
    unkeyed = conn.execute(
        text(
            f"SELECT l.id, l.bundle_id, "
            f"(SELECT COUNT(*) FROM {bundles} b WHERE b.id = l.bundle_id) "
            f"FROM {ledger} l WHERE l.group_id IS NULL OR l.group_id = '' ORDER BY l.id"
        )
    ).all()
    if unkeyed:
        detail = ", ".join(
            f"row {r[0]} (bundle {r[1]}{'' if r[2] else ' — MISSING'})" for r in unkeyed
        )
        raise RuntimeError(
            f"{len(unkeyed)} {ledger} row(s) have no group_id and could not be "
            f"backfilled from their bundle: {detail}. Nothing was changed or deleted. "
            f"These rows record grants the app may have made for a bundle that no "
            f"longer exists; review them (and revoke the grants by hand if "
            f"appropriate), then set group_id or remove the rows, and restart."
        )
    if conn.dialect.name == "postgresql":
        # Existing PG databases: the column was added with a transitional ''
        # default; make it strictly required like a fresh database.
        conn.execute(text(f"ALTER TABLE {ledger} ALTER COLUMN group_id DROP DEFAULT"))
        has_check = conn.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :n"),
            {"n": GENIE_LEDGER_GROUP_ID_CHECK},
        ).first()
        if not has_check:
            conn.execute(
                text(
                    f"ALTER TABLE {ledger} ADD CONSTRAINT {GENIE_LEDGER_GROUP_ID_CHECK} "
                    f"CHECK (group_id <> '')"
                )
            )
    conn.execute(
        text(f"CREATE INDEX IF NOT EXISTS ix_{ledger}_group_id ON {ledger} (group_id)")
    )

    if GENIE_BUNDLE_SPACE_UNIQUE not in {i["name"] for i in insp.get_indexes(bundles)}:
        dups = conn.execute(
            text(
                f"SELECT space_id, COUNT(*) FROM {bundles} "
                f"GROUP BY space_id HAVING COUNT(*) > 1 ORDER BY space_id"
            )
        ).all()
        if dups:
            detail = "; ".join(f"space {d[0]}: {d[1]} bundles" for d in dups)
            raise RuntimeError(
                f"Cannot enforce one Genie access bundle per space: {detail}. "
                f"Delete the extra bundle(s) through the app (DELETE "
                f"/api/genie-bundles/<id>, which revokes their grants safely) on the "
                f"previous version, then restart."
            )
        conn.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {GENIE_BUNDLE_SPACE_UNIQUE} "
                f"ON {bundles} (space_id)"
            )
        )
        logger.info(f"Added UNIQUE index {GENIE_BUNDLE_SPACE_UNIQUE} on {bundles}(space_id)")


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
