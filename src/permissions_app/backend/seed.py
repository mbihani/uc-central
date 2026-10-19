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

from sqlalchemy import Connection, Table, inspect, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, SQLModel

from .defaults import DEFAULT_PERMISSIONS_MATRIX
from .logger import logger
from .models import (
    DEFAULT_PERSONA_DESCRIPTIONS,
    DEFAULT_PERSONA_LABELS,
    GENIE_BUNDLE_SPACE_UNIQUE,
    GENIE_LEDGER_GROUP_ID_CHECK,
    GENIE_LEDGER_SOURCE_CHECK,
    GENIE_LEDGER_SOURCE_UNIQUE,
    LEDGER_CONFIRMED,
    LEGACY_LEDGER_UNIQUE,
    LEGACY_PERSONA_GROUP_UNIQUE,
    PERSONA_GROUP_UNIQUE,
    PersonaGroupMapping,
    PersonaResourceOverride,
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
        # Personas STACK: a group may map to several personas, but each
        # (group, persona) pair once. The pre-stacking UNIQUE (group_id) is
        # dropped by ensure_persona_stacking_schema.
        "persona_group_mapping",
        ("group_id", "persona"),
        PERSONA_GROUP_UNIQUE,
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


def default_permission_template_rows() -> list[dict]:
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
        .values(default_permission_template_rows())
        .on_conflict_do_nothing(index_elements=["persona", "resource_type"])
    )


def _pg_constraints(conn: Connection, table: str) -> set[str]:
    """Names of the constraints ON ``table`` (resolved through the active
    search_path, i.e. the app's schema). Postgres constraint names are only
    unique per table, so a global ``conname`` lookup could match a same-named
    constraint on another table or schema and wrongly skip adding ours."""
    return set(
        conn.execute(
            text("SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(:t)"),
            {"t": table},
        ).scalars().all()
    )


def ensure_unique_constraints(conn: Connection) -> None:
    """De-dup + add the UNIQUE constraints IF missing. Idempotent & safe to run
    concurrently only while holding the bootstrap advisory lock (the caller does).

    Table/column/constraint names are internal constants (never user input), so
    interpolating them into the DDL is safe.
    """
    for table, cols, name in _UNIQUE_CONSTRAINTS:
        if name in _pg_constraints(conn, table):
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


def _sqlite_table_sql(conn: Connection, table: str) -> str:
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :t"),
        {"t": table},
    ).first()
    return (row[0] or "") if row else ""


def _sqlite_rebuild(conn: Connection, table: Table) -> None:
    """Recreate ``table`` from the current model and copy its rows (SQLite
    cannot drop or alter constraints in place). Every column the model and the
    old table share is copied unchanged; nothing is dropped. Callers run their
    validations BEFORE calling this.

    SQLite is the test / local-dev backend only; production runs Postgres,
    whose path uses in-place ALTERs inside the bootstrap transaction.
    """
    name = table.name
    tmp = f"{name}__pre_migration"
    old_cols = {c["name"] for c in inspect(conn).get_columns(name)}
    # Index names are database-global in SQLite: drop the old table's explicit
    # indexes so the new table can recreate them (auto-indexes go with it).
    for (idx,) in conn.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :t "
            "AND sql IS NOT NULL"
        ),
        {"t": name},
    ).all():
        conn.execute(text(f'DROP INDEX "{idx}"'))
    conn.execute(text(f'ALTER TABLE "{name}" RENAME TO "{tmp}"'))
    table.create(conn)
    cols = ", ".join(f'"{c.name}"' for c in table.columns if c.name in old_cols)
    conn.execute(text(f'INSERT INTO "{name}" ({cols}) SELECT {cols} FROM "{tmp}"'))
    conn.execute(text(f'DROP TABLE "{tmp}"'))
    logger.info(f"Rebuilt SQLite table {name} with the current constraints")


def ensure_persona_stacking_schema(conn: Connection) -> None:
    """Allow a group to map to SEVERAL personas, and add per-resource overrides.

    Idempotent on a fresh database and on one created before stacking:

      * the per-resource override table is created if missing;
      * the pre-stacking ``UNIQUE (group_id)`` on ``persona_group_mapping`` is
        dropped; ``UNIQUE (group_id, persona)`` remains (Postgres: added by
        ``ensure_unique_constraints``; fresh/rebuilt tables: from the model).
        No mapping row is changed or deleted.
    """
    SQLModel.metadata.create_all(
        conn,
        tables=[SQLModel.metadata.tables[PersonaResourceOverride.__tablename__]],
        checkfirst=True,
    )
    table = PersonaGroupMapping.__tablename__
    if conn.dialect.name == "postgresql":
        conn.execute(
            text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {LEGACY_PERSONA_GROUP_UNIQUE}")
        )
        return
    if LEGACY_PERSONA_GROUP_UNIQUE in _sqlite_table_sql(conn, table):
        _sqlite_rebuild(conn, SQLModel.metadata.tables[table])


def _upgrade_ledger_sources(conn: Connection) -> None:
    """Give every ledger row a SOURCE ("bundle:<id>" or "persona:<k>:space:<id>").

    Refcounting compares sources for the same group id, so bundles and personas
    can rely on the same UC grant and only the last one releases it. Rows from
    earlier versions all came from bundles and are backfilled to
    ``bundle:<bundle_id>``. The uniqueness key moves from (bundle, principal,
    grant) to (source, group_id, grant); ``bundle_id`` becomes optional
    (persona rows have none). Fails loudly, changing nothing, if a row cannot
    be given a source or the new key would be violated.
    """
    ledger = GenieGrantLedger.__tablename__
    cols = {c["name"] for c in inspect(conn).get_columns(ledger)}
    if "source" not in cols:
        conn.execute(text(f"ALTER TABLE {ledger} ADD COLUMN source VARCHAR NOT NULL DEFAULT ''"))
        logger.info(f"Added column {ledger}.source")
    if "persona" not in cols:
        conn.execute(text(f"ALTER TABLE {ledger} ADD COLUMN persona VARCHAR"))
    conn.execute(
        text(
            f"UPDATE {ledger} SET source = 'bundle:' || CAST(bundle_id AS VARCHAR) "
            f"WHERE (source IS NULL OR source = '') AND bundle_id IS NOT NULL"
        )
    )
    unsourced = conn.execute(
        text(f"SELECT id FROM {ledger} WHERE source IS NULL OR source = '' ORDER BY id")
    ).scalars().all()
    if unsourced:
        raise RuntimeError(
            f"{len(unsourced)} {ledger} row(s) have neither a source nor a bundle id: "
            f"rows {list(unsourced)}. Nothing was changed or deleted; set their source "
            f"and restart."
        )
    dups = conn.execute(
        text(
            f"SELECT source, group_id, securable_type, full_name, privilege, COUNT(*) "
            f"FROM {ledger} GROUP BY source, group_id, securable_type, full_name, privilege "
            f"HAVING COUNT(*) > 1"
        )
    ).all()
    if dups:
        raise RuntimeError(
            f"{len(dups)} duplicate {ledger} grant(s) per (source, group): "
            f"{[tuple(d[:5]) for d in dups]}. Nothing was changed or deleted."
        )

    if conn.dialect.name == "postgresql":
        conn.execute(text(f"ALTER TABLE {ledger} ALTER COLUMN source DROP DEFAULT"))
        conn.execute(text(f"ALTER TABLE {ledger} ALTER COLUMN bundle_id DROP NOT NULL"))
        conn.execute(text(f"ALTER TABLE {ledger} DROP CONSTRAINT IF EXISTS {LEGACY_LEDGER_UNIQUE}"))
        existing = _pg_constraints(conn, ledger)
        if GENIE_LEDGER_SOURCE_CHECK not in existing:
            conn.execute(
                text(
                    f"ALTER TABLE {ledger} ADD CONSTRAINT {GENIE_LEDGER_SOURCE_CHECK} "
                    f"CHECK (source <> '')"
                )
            )
        if GENIE_LEDGER_SOURCE_UNIQUE not in existing:
            conn.execute(
                text(
                    f"ALTER TABLE {ledger} ADD CONSTRAINT {GENIE_LEDGER_SOURCE_UNIQUE} "
                    f"UNIQUE (source, group_id, securable_type, full_name, privilege)"
                )
            )
    elif GENIE_LEDGER_SOURCE_UNIQUE not in _sqlite_table_sql(conn, ledger):
        _sqlite_rebuild(conn, SQLModel.metadata.tables[ledger])
    for col_name in ("source", "persona"):
        conn.execute(
            text(f"CREATE INDEX IF NOT EXISTS ix_{ledger}_{col_name} ON {ledger} ({col_name})")
        )


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
      * every row gets a ``source`` (see ``_upgrade_ledger_sources``).
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
        if GENIE_LEDGER_GROUP_ID_CHECK not in _pg_constraints(conn, ledger):
            conn.execute(
                text(
                    f"ALTER TABLE {ledger} ADD CONSTRAINT {GENIE_LEDGER_GROUP_ID_CHECK} "
                    f"CHECK (group_id <> '')"
                )
            )
    conn.execute(
        text(f"CREATE INDEX IF NOT EXISTS ix_{ledger}_group_id ON {ledger} (group_id)")
    )
    _upgrade_ledger_sources(conn)

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
