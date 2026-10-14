# Genie access bundles

Give people a Genie space (Genie agent) without granting table access to every
individual user or group.

## Why a bundle

Genie always runs a question's SQL **with the asking user's own Unity Catalog
permissions**. There is no "run as owner / service credential" option. So
CAN_RUN on a space only works if the user can also read every table the space
uses.

A **bundle** ties one Genie space to one **account-level group**, for example
`genie-sales-users`. UC Central grants that group, once:

| What | Where |
|---|---|
| `CAN_RUN` | the Genie space (workspace ACL) |
| `SELECT` | each table / view / materialized view / metric view the space uses |
| `READ_VOLUME` | each volume the space uses |
| `USE_SCHEMA` | each schema that contains one of those objects |
| `USE_CATALOG` | each catalog that contains one of those objects |

Grants are per table. The bundle never grants schema-wide `SELECT`.

After that, **onboarding means adding a person to the group, and offboarding
means removing them.** No per-user grants exist.

## Design

### Lifecycle

```
create ─▶ preview ─▶ apply ─▶ (add members) ─▶ resync … ─▶ delete
```

* **Create** (`POST /api/genie-bundles`): takes a space id and an existing
  group name. Checks that the space exists and that the group exists in the
  workspace as an **account** group, then stores the group's immutable SCIM
  **id**. Writes nothing to the workspace. **Each space has exactly one
  bundle (one group).** A second bundle for the same space gets a 409; the
  database enforces this with a unique index.
* **Preview** (`GET /api/genie-bundles/{id}/preview`): read-only. It shows:
  * the space's data sources, each with its UC `table_type`;
  * any data source that can't be resolved, and why;
  * whether each table already has a **row filter**, and which columns are
    **masked**;
  * every required grant, with one of these statuses:
    * `missing`: the bundle would grant it.
    * `present`: already effective, either direct, inherited from a parent, or
      via a group such as `account users`. The app didn't create it.
    * `managed`: already effective and recorded in the ledger as app-granted.
    * `error`: the grants couldn't be read.
  * ledger rows that are no longer needed (`stale`, released on resync);
  * the group check, plus any **blockers** and warnings.

  Preview makes **no workspace writes and no database writes**. If the group
  was renamed, the preview reports it (`group.renamed_from` plus a warning)
  and evaluates grants under the current name. The rename is only *persisted*
  by apply, resync, delete or a membership change.
* **Apply** (`POST …/apply`): grants `CAN_RUN` on the space, then the missing
  UC privileges (parents first). Apply is idempotent: a second apply makes no
  API calls.
* **Resync** (`POST …/resync`): re-reads the space definition, grants objects
  that were added since the last apply, and **releases** ledger rows the space
  no longer needs. Releasing follows the rules under "Safe revoke" below.
* **Delete** (`DELETE …/{id}`): releases every ledger row under the same
  rules, then deletes the bundle. The account group itself is never deleted.
  If any revoke fails, the bundle and the failed rows are **kept** so the
  delete can be retried. Grants are never orphaned without a ledger record.
* Every one of these operations, including membership changes, first
  **re-resolves the group by its stored id** (see "Groups" below).

### Discovery

`GET /api/2.0/genie/spaces/{id}?include_serialized_space=true` returns
`serialized_space`, which is a JSON document stored as a string. These shapes
were confirmed against a live workspace:

```json
{"version": 2,
 "data_sources": {
   "tables":  [{"identifier": "catalog.schema.table", "column_configs": [...]}],
   "volumes": [{"path": "/Volumes/catalog/schema/volume/"}]},
 "instructions": {"join_specs": [{"left": {"identifier": "..."}, ...}], ...},
 ...}
```

* Tables, views, materialized views and streaming tables all appear under
  `data_sources.tables`. Metric views are UC table securables, so we assume
  they appear there too: no metric view existed in the workspace to confirm
  this. Any other list under `data_sources` whose entries have an
  `identifier` is also treated as table-like.
* Each identifier is resolved with the UC tables API (`tables.get`). That call
  confirms the object exists and returns its type, row filter and column
  masks. Back-ticked parts are handled.
* Some references can't be resolved: an object that was dropped, a name that
  isn't three-part, or one the app SP can't see. These are **listed in the
  preview with a reason** and are not granted.
* **Nothing to grant blocks apply.** This happens when the definition isn't
  returned, `data_sources` is absent or empty, the format isn't recognized
  (no entries with an `identifier` / `path`), or none of the data sources
  resolves. The preview shows a blocker that starts *"Nothing to grant"*, and
  apply/resync return 400. A CAN_RUN-only grant would be useless.
* SQL text in instructions or example queries is **not** parsed. Only declared
  data sources are granted.

### Grant ledger

Two tables are stored in Lakebase. They are created or upgraded at startup by
`ensure_genie_bundle_schema` in `backend/seed.py`, inside the existing
advisory-locked bootstrap transaction. The migration is idempotent and never
drops or rewrites data:

* `create_all(checkfirst)` creates any missing table. On a fresh database,
  every column and index is created in place.
* On a database created by an earlier version of this feature, these
  upgrades run:
  * `genie_grant_ledger.state` is added with default `confirmed`. Those rows
    were all written after their grant succeeded.
  * `genie_grant_ledger.group_id` is added, **backfilled from each row's
    bundle**, and indexed. It is the lock and sharing key.
  * The unique index `uq_genie_access_bundle_space_id` is created. **If that
    database already has several bundles for one space, startup fails loudly**
    with a `RuntimeError` naming each space and its bundle count. Nothing is
    silently deleted, because those bundles' grants are in the ledger. Delete
    the extras through the app (safe revoke) on the previous version, then
    restart.

The two tables:

* `genie_access_bundle` has one row per space (unique index on `space_id`).
  `group_id` is the group's identity; `group_name` is its current display name.
* `genie_grant_ledger` has one row per privilege a bundle relies on. Its
  columns are: `bundle_id`, `group_id` (immutable; the lock and sharing key),
  `space_id`, `principal` (the group's display name as last seen; informational
  only), `securable_type` (`genie_space` / `catalog` / `schema` / `table` /
  `volume`), `full_name`, `privilege`, `app_granted`, `state`
  (`pending` / `confirmed`), `granted_at`, `granted_by`. A UNIQUE constraint
  covers (bundle, principal, securable, privilege). Only *that* uniqueness
  conflict is treated as "already recorded"; any other integrity error is
  raised.

`app_granted = true` means UC Central issued the grant.
`app_granted = false` means the privilege already existed when the bundle was
applied. The bundle still records it so that **another bundle never revokes it
out from under this one**, but the app never revokes it itself.

### Grant-before-ledger: pending rows

Every grant the app makes runs inside **one continuous per-grant lock span**:

1. Fresh presence check.
2. A durable **`pending`** ledger row is committed *before* the workspace call.
   A new row is written with `app_granted = false`.
3. The workspace grant call.
4. On success, the row becomes **`confirmed`** with `app_granted = true`; this
   process provably issued the grant. On failure, the row is restored to its
   previous state, or removed.

Delete, resync and reconciliation take the **same lock**, so none of them can
act on a pending row while its grant is in flight. A pending row that one of
them does see therefore means the applying process **crashed** (or lost its
DB connection) inside that span. That is ambiguous: the grant may or may not
have been issued, and someone else may have granted the same privilege
meanwhile.

**Reconciliation rule (conservative).** At the start of every apply, resync
and delete, each pending row of the bundle is checked against the
workspace's effective grants, under the lock:

* **Privilege in effect:** the row is confirmed, and its `app_granted` flag is
  left **unchanged**. A row first created by the interrupted attempt stays
  `app_granted = false`: it is recorded as *needed* but is **never revoked
  automatically**. The response lists it under `reconciled` with a warning so
  an operator can review it and revoke it by hand if appropriate. A row that
  was already app-granted before the attempt (proven earlier, then revoked
  externally and re-granted) keeps `app_granted = true`.
* **Privilege not in effect:** the grant never happened, so the row is
  **dropped** and apply grants it again normally.
* **Workspace unreadable:** the row stays pending and an error is reported.

The preview warns whenever pending rows exist.

### Safe revoke (resync / delete)

Every decision about one grant runs under a **per-grant lock** keyed on a
stable 64-bit hash of (**group id**, securable type, full name, privilege).
The key uses the group's **immutable SCIM id**, never its mutable display
name, so a rename can't let a second operation take a "different" lock for
the same grant.

On Postgres the lock is a **session-level `pg_advisory_lock`** taken on a
dedicated pooled connection. **While it is held, the ORM session is bound to
that same connection**, so each operation uses exactly one pooled connection
and concurrent operations can't exhaust the pool waiting for a second one.
The lock stays held across the commits and workspace calls inside the span,
and is released with `pg_advisory_unlock` in a `finally`. If the unlock
itself fails, the connection is invalidated, so its backend closes and the
lock is freed rather than returned to the pool still held. On other databases
(the SQLite used in tests), a process-local lock is used.

Inside each lock span, the group's **current display name is re-resolved by
id**, right before every UC or ACL call. Grants and revokes therefore always
target the group the bundle was created for, never another group that now
carries an old name. If the id is gone or is no longer an account group, the
call is skipped and an error is reported.

Within the lock, the app re-reads the other bundles' rows **with the same
group id** (not the same stored name), drops its own row, and revokes if it
was the last user. Two bundles sharing a table, schema or
catalog grant can therefore be deleted or resynced concurrently without both
concluding "the other one still needs it" and orphaning the grant.

An operation holds **one grant lock at a time**. If code ever nests them, the
keys must be taken in ascending order; out-of-order nesting raises an error,
which rules out lock-order deadlocks.

To release one ledger row:

1. If **another bundle** has a row for the same principal, securable and
   privilege, the privilege is still needed. Only this bundle's row is
   dropped. If this row was the app-granted one, ownership passes to a
   remaining row.
2. Otherwise, if the row is `app_granted`, the app makes a **targeted** revoke:
   `grants.update(remove=[privilege])` for that one principal. For the space,
   it removes the group's ACL entry, but only if the group still holds exactly
   `CAN_RUN`. If someone raised the level afterwards, the entry is left in
   place and a warning is returned.
3. Otherwise (a pre-existing grant), the row is dropped and nothing is revoked.

Revokes run children first (tables, then schemas, then catalogs). If the
securable no longer exists, its row is dropped without an API call.

## Required privileges for the app service principal

| Needed for | Privilege |
|---|---|
| Grant / revoke on tables, views, volumes, schemas, catalogs | **`MANAGE`** (or ownership) on each **target catalog**, or on each target schema. `MANAGE` on a catalog covers its schemas and tables. |
| Resolve tables (type, row filter, masks) | Metadata visibility. `MANAGE` implies it; otherwise `BROWSE` or `USE_CATALOG` + `USE_SCHEMA`. |
| Read the space definition and set its ACL | `CAN_MANAGE` on each Genie space |
| Look up groups and change membership | Workspace admin. This is already a deployment prerequisite (DEPLOYMENT.md). |

If the SP lacks `MANAGE` or ownership, the failure is **not swallowed**. The
apply/resync/delete response has `status: "partial"` (or `"failed"`) and an
`errors` entry that names the exact securable, for example: *Permission denied
while trying to grant SELECT on table 'cat.sales.orders': the app service
principal needs MANAGE (or ownership) on this table or a parent
catalog/schema.* The UI shows these errors in a result banner. A grant that
failed is **not** written to the ledger.

Grant once per catalog:

```sql
GRANT MANAGE ON CATALOG <catalog> TO `<app-service-principal-application-id>`;
```

## Groups

* **The client creates the groups.** UC Central never creates groups.
* The group must be an **account-level** group that is **assigned to the
  workspace**. On the workspace SCIM API, account groups report
  `meta.resourceType = "Group"` and workspace-local groups report
  `"WorkspaceGroup"` (confirmed live). Workspace-local groups are refused with
  a clear error, because Unity Catalog can't grant to them.
* **Identity is the group id, not the name.** Before every preview, apply,
  resync, delete and membership operation, the app looks up the stored SCIM
  id:
  * **Renamed** (same id, new name): UC grants follow the principal. Apply,
    resync, delete and membership changes update the bundle's `group_name`,
    and update each ledger row's `principal` while holding **that row's
    per-grant lock**, so propagation can't interleave with an in-flight grant.
    Preview only reports the rename. Correctness doesn't depend on
    propagation: locks and sharing use `group_id`, and every UC call
    re-resolves the current name.
  * **No longer account-level**: the operation is blocked (400).
  * **Deleted**: the bundle is **blocked**, even if a new group with the same
    name exists. That is a different principal, and granting to or revoking
    from it would act on someone else's access. The only allowed action is
    `DELETE /api/genie-bundles/{id}?forget_orphaned=true` (the UI offers
    "Forget orphaned bundle"). It is accepted only when a by-id lookup returns
    *not found*. It drops the bundle and its ledger rows **without any
    workspace call** and lists them in the response for audit. You can then
    create a new bundle for the new group on that space.
* Use a **dedicated** group per bundle, and don't map it to a persona. A
  persona Apply rewrites the Genie-space ACL of every group mapped to that
  persona. The preview warns if the group is persona-mapped.

### Membership

The bundle UI's **Members** dialog reuses the app's existing SCIM membership
code (`_set_group_membership`). That code calls `PATCH
/api/2.0/preview/scim/v2/Groups/{id}` on the workspace API.

**Not verified live:** whether the workspace SCIM API accepts membership
changes on an **account** group. This depends on the workspace and its
identity settings, and may require the SP to be a manager of the group. If the
workspace refuses, the API returns a 403 telling the operator to manage
membership in the **account console**. The dialog always shows the group name
along with those instructions.

## Persona templates: Genie default changed

New persona templates now default the **Genie spaces** cell to
`NO_PERMISSIONS` for Data Engineer, Data Scientist, Analyst and Support.
Previously they defaulted to `CAN_RUN` / `CAN_READ` on every space, but that
access carried no data access, so it didn't work anyway. Admin and Deployer
keep `CAN_MANAGE`, because that is space management rather than consumption.
Genie consumption is granted only through bundles.

**Existing saved templates are not changed.** Seeding is `INSERT … ON CONFLICT
DO NOTHING`, so a database that already has the old cells keeps them. To move
an existing deployment to bundle-only Genie access, set those cells to "No
permission" in the Permissions Matrix and re-apply the personas. Applying a
`NO_PERMISSIONS` cell **removes** that persona's groups from every space's
ACL, so first make sure the affected users are in the right bundle groups.

## Operator runbook

1. **Client:** create an account group per Genie space in the account console
   (for example `genie-sales-users`) and assign it to the workspace.
2. **Once:** grant the app SP `MANAGE` on each target catalog and `CAN_MANAGE`
   on each Genie space (see above).
3. **UC Central → Genie Access → New bundle:** pick the space and enter the
   group name. A workspace-local or missing group is rejected at this step.
4. **Preview:** check the data sources, any unresolved references, the row
   filter and mask columns, and which grants are `missing` vs `present`.
   Resolve any blockers.
5. **Apply:** check the result banner, which shows granted / already present /
   errors. Fix any permission-denied errors (missing `MANAGE`) and apply again.
   Apply is idempotent.
6. **Members:** add people to the group. If the workspace refuses, add them in
   the account console instead.
7. **When the space changes** (tables added or removed): **Resync**. New tables
   are granted. Removed tables are revoked only if the app granted them and no
   other bundle needs them.
8. **Retire:** **Delete** the bundle. This revokes app-made grants that are no
   longer needed elsewhere, then the client can delete the group.

Warehouse access isn't managed by the bundle. The preview names the space's
warehouse so it can be checked separately.

## Follow-up: different data per person (row / column restrictions)

This is **not in this PR**. Today every member of a bundle group sees the same
rows and columns, limited by any filters or masks that already exist and that
the preview reports. The data model leaves room for a restriction step:

* `genie_access_bundle.restriction_mode` (currently always `"none"`) is
  reserved for a later `"row_filter"` / `"abac"` mode.
* A space has **one** bundle group (the "who can use this agent" group).
  Different data per person comes from **additional** account groups
  referenced by the filter or mask policy, for example `genie-sales-emea` and
  `genie-sales-apac`, whose members are also in `genie-sales-users`. It does
  not come from extra bundles. A later step can attach a list of such
  restriction groups to the bundle without changing the one-group-per-space
  rule.
* The ledger already records securables by type and full name. Policy objects
  (functions, tag policies) can be added as new `securable_type` values and
  reuse the same safe-revoke rule.

### Option A: row filters and column masks keyed on group membership

For each restricted table, attach a SQL UDF row filter or column mask that
checks `is_account_group_member()`. That function evaluates the **asking
user's** account groups, which matches Genie's execution model:

```sql
CREATE FUNCTION gov.sales_region_filter(region STRING)
RETURN is_account_group_member('admins')
    OR (region = 'EMEA' AND is_account_group_member('genie-sales-emea'))
    OR (region = 'APAC' AND is_account_group_member('genie-sales-apac'));
ALTER TABLE sales.orders SET ROW FILTER gov.sales_region_filter ON (region);

CREATE FUNCTION gov.mask_email(email STRING)
RETURN CASE WHEN is_account_group_member('genie-sales-pii') THEN email
            ELSE '***' END;
ALTER TABLE sales.customers ALTER COLUMN email SET MASK gov.mask_email;
```

In UC Central, a bundle would carry a restriction spec, for example "column
`region` = `EMEA`". Apply would then generate or own the filter function and
grant `EXECUTE` on it. The ledger would record the function and the
`SET ROW FILTER` attachment, so resync and delete can remove them. A table
**already** has a filter or mask if the preview shows one; UC allows one row
filter per table, so a later step must refuse or merge instead of overwriting.

### Option B: ABAC with governed tags

Tag columns and tables with **governed tags** (for example `region`, `pii`)
and define catalog- or schema-level **ABAC policies**. Those policies apply a
row filter or column mask function to every tagged object, `TO` or `EXCEPT`
specific account groups. A bundle would then only need group membership plus
the existing per-table `SELECT`. New tables pick up policies through their
tags, with no per-table filter management. ABAC `EXCEPT` resolves **account
groups only**, which matches this design. Prefer B when several tables share
the same restriction dimension; use A for one-off tables.

Either way, the restriction step runs **before apply**. The preview gains a
"restriction" column, and the grant ledger remains the only record that
drives revocation.

## Running the tests

```bash
PGAPPNAME=test PYTHONPATH=src python -m pytest src/permissions_app/tests
```

The repo has no CI. The 9 concurrency, pool and migration tests that need
**real Postgres** semantics (READ COMMITTED, advisory locks, a real
connection pool) are skipped unless `UCG_TEST_PG_URL` is set. To run them against a throwaway local Postgres:

```bash
initdb -D /tmp/ucg-pg -U postgres --auth=trust
pg_ctl -D /tmp/ucg-pg -o "-p 55432 -k /tmp -c listen_addresses=''" -l /tmp/ucg-pg.log start
psql -h /tmp -p 55432 -U postgres -c "create database ucg_test"
UCG_TEST_PG_URL="postgresql+psycopg://postgres@/ucg_test?host=/tmp&port=55432" \
  PGAPPNAME=test PYTHONPATH=src python -m pytest src/permissions_app/tests
pg_ctl -D /tmp/ucg-pg stop
```

The Postgres tests drop and recreate the app's tables in that database, so
point `UCG_TEST_PG_URL` only at a throwaway database.

## Assumptions and limitations

* Metric views are assumed to appear in `data_sources.tables` (see Discovery).
* Querying a view or metric view needs only `SELECT` on it (the owner's rights
  cover the underlying tables, which is standard UC view semantics). Granting
  the underlying tables is out of scope.
* The SQL warehouse `CAN_USE` permission isn't managed.
* If a group held `CAN_READ` on the space before apply, it is upgraded to
  `CAN_RUN`. On revoke the entry is removed, not restored to `CAN_READ`.
