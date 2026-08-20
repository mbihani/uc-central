# UC Central — Deployment Guide

Deploying **UC Central** (a Databricks App: FastAPI backend + React/Vite UI, backed
by a Lakebase Postgres database) into a Databricks workspace.

> **What you provision vs what the bundle automates.** The app code is fully
> portable — it connects to whatever workspace it runs in via the Databricks SDK.
> The bundle **declaratively** creates the app and attaches the Lakebase
> `database` resource binding. Three things are **not** captured in the bundle
> and are handled outside `bundle deploy`: (1) **Lakebase Postgres instance
> provisioning** happens *before* deploy (the bundle cannot pin `pg_version`, so
> you pre-create a PG 16 instance — Step 1); then, *after* `bundle deploy`, two
> one-time operator steps: (2) the **Postgres `GRANT USAGE, CREATE ON SCHEMA
> public`** for the app's service principal — Step 5a, and (3) **adding the app
> service principal to the `admins` group** so it can perform SCIM/ACL writes —
> Step 5b. This guide makes all three explicit. See **MANIFESTO.md** for every
> configurable value.

---

## Prerequisites

You will need:

- A **Databricks workspace** with the Lakebase feature enabled.
- The **Databricks CLI** (v1.3.0+ for the `direct` deploy engine). Verify:
  ```bash
  databricks --version    # expect >= 1.3.0
  ```
- A **CLI auth profile** logged into your workspace:
  ```bash
  databricks auth login --host https://<your-workspace>.cloud.databricks.com --profile <your-profile>
  ```
- A **workspace admin** account that is ALSO a **Lakebase/Postgres superuser**
  on the instance (you need both to perform the one-time operator setup — see
  Step 5).

---

## Step 0 — Install dependencies & configure (first clone)

`node_modules/`, `.venv/`, and `.env` are not committed, so a fresh clone must
create them before building:

```bash
git clone <repo-url> uc-central    # use git clone, NOT GitHub "Download ZIP" (no .git → no updates)
cd uc-central
bun install                          # JS/UI deps  -> node_modules/   (project uses bun)
uv sync                              # Python deps -> .venv/
cp .env.example .env                # sets PGAPPNAME for LOCAL builds/dev
```

> These are required even just to *build* the artifact: `apx build` loads the app
> to generate its OpenAPI schema, and the app's config requires `PGAPPNAME` (read
> from `.env`), so a build without `.env` fails with a `DatabaseConfig` /
> `PGAPPNAME` validation error. In production the DB connection env
> (`PGHOST`/`PGUSER`/`PGDATABASE`/`PGPORT`/`PGAPPNAME`) is injected by the Lakebase
> `database` binding and the app's `app.yml` env block — `.env` only affects local
> build/dev.

---

## ⚠️ Gotcha 1 — PG 16, not PG 17 (read before deploying)

The bundle `database_instances` resource **does not expose `pg_version`**
(`additionalProperties:false` in the bundle schema — no version field). An instance
the bundle *creates* would land on the platform default, which cannot be relied
on to be PG 16. **PG 16 is required**: PG 17 instances are not registered in the
Database Instances API, so the app `database` binding silently fails to inject
`PGHOST`/`PGUSER`/`PGDATABASE`/`PGPORT` and the app falls back to no DB.

**Therefore the Lakebase instance is created OUTSIDE the bundle** (Step 1 below)
with an explicit `pg_version=PG_VERSION_16` and referenced by name. Because it is
external, `bundle destroy` does **not** delete it — delete it separately if
desired (see Rollback).

---

## Step 1 — Pre-create the Lakebase PG 16 instance (once, required)

The bundle binds to this instance by name; it must exist before `bundle deploy`.

```bash
databricks api post /api/2.0/database/instances --profile <your-profile> --json '{
  "name": "uc-central", "capacity": "CU_1", "pg_version": "PG_VERSION_16"
}'    # CONFIGURE(capacity): Lakebase compute capacity (CU_1, CU_2, ...). Default CU_1.
```

Poll until the instance is ready:
```bash
databricks api get /api/2.0/database/instances/uc-central --profile <your-profile> \
  | jq '{state, pg_version, read_write_dns}'
# wait until state == "AVAILABLE" and pg_version == "PG_VERSION_16"
```

> Override the instance name (`uc-central`) and capacity (`CU_1`) per your
> needs — see `instance_name` and `capacity` in MANIFESTO.md. The app's
> `database` binding in `databricks.yml` must reference the same name via the
> `instance_name` variable, and `app.yml` must set `PGAPPNAME` to the same
> value (see Gotcha 2 below).

---

## Step 2 — Build the artifact

```bash
node_modules/.bin/vite build                       # React UI -> src/permissions_app/__dist__
.venv/bin/apx build --skip-ui-build               # wheel + .build/ (regenerates _metadata.py)
ls -1 .build                                       # expect: <wheel>.whl, requirements.txt, app.yml
```

`bundle deploy` syncs the pre-built `.build/` directory — it runs no build step,
so build before deploying.

---

## Step 3 — Validate (read-only)

```bash
databricks bundle validate -t dev --profile <your-profile>
```

Requires Databricks CLI v1.3+ with `bundle.engine: direct` (already set in
`databricks.yml`). Must report `Validation OK!`.

---

## Step 4 — Deploy

Creates the UC Central app and binds it to the external PG 16 instance:

```bash
databricks bundle deploy -t dev --profile <your-profile>
```

---

## Step 5 — Post-deploy setup (operator, one-time)

Run **after** `bundle deploy`. The app's service principal only exists once the
app is created (Step 4), so two non-declarative residues must be done by an
operator. Both are safe to re-run (idempotent). Whoever performs them must be
BOTH a **workspace admin** (for the SCIM group PATCH) AND a **Lakebase/Postgres
superuser** on the instance (Databricks superusers are Postgres superusers on
Lakebase — required for the GRANT).

> **Why an operator step and not an in-bundle job?** A `uc-central-setup` job
> was tried on every compute type and never completes: serverless + `psycopg`
> SIGABRTs (OpenSSL double-load); serverless + `pg8000` hangs on
> `pg8000.connect` (serverless egress cannot reach the Lakebase endpoint); a
> classic cluster fails `WorkspaceClient` runtime auth. The operator-run
> equivalents below finish in seconds.

### (a) Grant the app SP schema privileges

PG 15+ no longer grants `CREATE` on the `public` schema to non-owner roles by
default, so the app's `CAN_CONNECT_AND_CREATE` Lakebase role can create
new *schemas* but not objects in the existing `public` schema. Without this grant the app's
first-boot `create_all` (which creates its tables in `public`) crashes with a
permission-denied error — see Gotcha 3.

```bash
APP_SP=$(databricks apps get <APP> --profile <PROFILE> -o json | jq -r .service_principal_client_id)
DNS=$(databricks database get-database-instance <INSTANCE> --profile <PROFILE> -o json | jq -r .read_write_dns)
TOKEN=$(databricks database generate-database-credential --profile <PROFILE> -o json --json '{"instance_names":["<INSTANCE>"]}' | jq -r .token)
PGPASSWORD="$TOKEN" psql "host=$DNS port=5432 dbname=databricks_postgres user=<YOUR_USER> sslmode=require" \
  -c 'GRANT USAGE, CREATE ON SCHEMA public TO "'"$APP_SP"'";'
```

> **UI alternative:** open the instance in the Lakebase SQL editor (as a
> superuser / the instance creator) and run the same
> `GRANT USAGE, CREATE ON SCHEMA public TO "<app_sp_client_id>";` statement.
> Find the app SP's client ID on the app's detail page or via
> `databricks apps get <APP> -o json`.

Replace `<APP>` with the app name (`app_name`, e.g. `uc-central`), `<INSTANCE>`
with the Lakebase instance name (`instance_name`), `<YOUR_USER>` with your
Lakebase/Postgres superuser username, and `<PROFILE>` with your CLI auth profile.

### (b) Add the app SP to the `admins` group

New-app service principals ship with a scope-restricted OBO token (`iam.*:read`
only). UC Central performs SCIM and ACL **writes** as the app SP, so the SP must
be a member of the workspace `admins` group. (End-user authorization is separate
— this grants the *app* the right to write, not end users.)

```bash
SPID=$(databricks service-principals list --profile <PROFILE> -o json | jq -r '.[]|select(.applicationId=="'"$APP_SP"'")|.id')
GID=$(databricks groups list --profile <PROFILE> -o json | jq -r '.[]|select(.displayName=="admins")|.id')
databricks groups patch "$GID" --profile <PROFILE> --json '{"schemas":["urn:ietf:params:scim:api:messages:2.0:PatchOp"],"Operations":[{"op":"add","path":"members","value":[{"value":"'"$SPID"'"}]}]}'
```

> **UI alternative:** Settings → Identity → Groups → `admins` → Add members →
> select the app's service principal.

### After both — redeploy & verify

Once both grants are in place, redeploy the app so its first-boot `create_all`
succeeds (it crashed on first start before the GRANT existed):

```bash
databricks apps deploy <APP> --source-code-path <workspace .build path> --profile <PROFILE>
```

> Re-running `databricks bundle deploy -t dev --profile <PROFILE>` also restarts
> the app from the synced `.build/` and achieves the same effect — use it if you
> don't have the synced workspace path handy.

Then verify (see Step 6 for detail): the app is `RUNNING`, the title reads
**UC Central**, and a data-backed view (e.g. Personas) loads — confirming
Lakebase is connected and first-boot seeding ran.

---

## Step 6 — Verify

```bash
databricks apps get uc-central --profile <your-profile> -o json \
  | jq '{app_status: .app_status.state, compute: .compute_status.state, url}'
# expect app_status RUNNING, compute ACTIVE
```

Then, signed into the workspace in a browser, open the app URL and confirm:
- The brand reads **UC Central** (title + navbar/sidebar logo).
- A data-backed view (e.g. Personas) loads → Lakebase is connected and first-boot
  seeding created the default personas.
- As a **workspace admin**: the permissions matrix / add-user actions work.
- As a **non-admin**: you get **403** (fail-closed authorization).
- `GET /api/version` shows the freshly-built wheel timestamp.

---

## ⚠️ Gotcha 2 — Instance name / PGAPPNAME handling

The app validates the Lakebase instance named by the `PGAPPNAME` environment
variable (read in `config.py` as `db.instance_name`, used to call
`get_database_instance()` and `generate_database_credential()`). The Databricks
Apps platform **defaults `PGAPPNAME` to the APP NAME** — which works only when
the app name and the instance name are the same. When they differ (e.g. a
soft-deleted app-name tombstone forces the instance to be named differently),
the app validates the wrong instance and crashes with
`ValueError: Database instance <app-name> does not exist`.

**The fix is in `app.yml`:** the `env` block sets `PGAPPNAME` to the bundle's
`instance_name` variable value, overriding the platform default:

```yaml
# app.yml (copied verbatim to .build/app.yml by `apx build`)
env:
  - name: PGAPPNAME
    value: "uc-central"   # CONFIGURE(instance-name): MUST match `instance_name` in databricks.yml
```

This survives `apx build` because apx copies the root `app.yml` to `.build/app.yml`
at build time. **If you change `instance_name` in `databricks.yml`, you MUST also
update `PGAPPNAME` in `app.yml` to match.** The `CONFIGURE(instance-name)` marker
in both files makes this coupling greppable:

```bash
grep -rn 'CONFIGURE(instance-name)' app.yml databricks.yml
```

---

## ⚠️ Gotcha 3 — `GRANT USAGE, CREATE ON SCHEMA public` (PG 15+)

Postgres 15+ tightened the default permissions on the `public` schema: users
with `CREATE` on the database can no longer create objects in `schema public`
by default. The app's `CAN_CONNECT_AND_CREATE` Lakebase role can create
new *schemas* but not objects in the existing `public` schema — so the app's first-boot
`create_all` (which creates tables in `public`) fails without the explicit
GRANT.

Step 5a performs this grant (operator-run). If you skip it, or if it runs
before the app SP's Postgres role is enrolled by the `database` binding, the
app will crash on boot with a permission-denied error on `schema public`.
Re-run the Step 5a grant to fix it — the `database` binding enrolls the SP
role shortly after `bundle deploy`, so if the grant races ahead of that
enrollment, wait a moment and re-run.

---

## Rollback / cleanup

```bash
databricks bundle destroy -t dev --profile <your-profile>       # deletes the app only
databricks api delete /api/2.0/database/instances/uc-central --profile <your-profile>
# (optionally remove the app SP from the admins group)
```

The Lakebase instance is external to the bundle, so `bundle destroy` does **not**
delete it — delete it separately as shown above.

---

## Deploying in another (customer) workspace

Add a target (e.g. `prod`) in `databricks.yml` and override the variables (the
workspace host/profile is supplied via `--profile`, same as `dev`):

```yaml
targets:
  prod:
    mode: production
    variables:
      app_name: "uc-central"        # or their chosen app name
      instance_name: "uc-central"   # or their chosen instance name
```

Then `databricks bundle validate -t prod --profile <their-profile>` and
`bundle deploy -t prod --profile <their-profile>`, then perform the two operator
steps from Step 5 (GRANT + SP-admin) against the prod app and instance.

**If the instance name is overridden**, also update `PGAPPNAME` in `app.yml`
to match (see Gotcha 2).

Additional notes for a production target:
- **PG 16 instance**: pre-create one (see Step 1). The bundle cannot pin
  `pg_version` itself.
- **`admins` group**: must exist (the admin gate is keyed to the group literally
  named `admins`); Step 5b adds the app SP to it. Override
  `ADMIN_GROUP_NAME` in `dependencies.py` if your workspace uses a different name.
- **Superuser for the operator steps**: whoever performs the Step 5 grants must
  be BOTH a workspace admin AND a Postgres/Databricks superuser on the instance.
- **External instance lifecycle**: the PG 16 instance is created outside the
  bundle, so `bundle destroy` does not delete it.
- **Front-door IP ACL**: admins reaching the UI are subject to the workspace IP
  access list (workspace config, not app config).

---

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| App crashes on boot, `Database instance <name> does not exist` | `PGAPPNAME` in `app.yml` doesn't match the instance name. Update `app.yml` to match `instance_name` in `databricks.yml`, rebuild, and redeploy. |
| App crashes on boot, `create_all`/permission-denied on `schema public` | Step 5a grant missing or ran before the SP role existed. Re-run the Step 5a grant after confirming the SP role exists on the instance (the `database` binding enrolls it). |
| App boots but DB endpoints 500 / can't connect | `database` resource not attached, or the instance is PG 17 (binding silently fails to inject env). Ensure the instance is PG 16. |
| SCIM/ACL operations 403 as the app | App SP not in `admins` group — re-do Step 5b. |
| Deploy SUCCEEDED but old code runs | Wheel/`requirements.txt` mismatch — re-upload both together, redeploy. |
| `bundle deploy` fails to reconcile | Ensure you are on Databricks CLI v1.3+ and the bundle uses `engine: direct` (already set in `databricks.yml`). |
| Brand shows the old slug after a rebuild | Ensure `pyproject [tool.apx.metadata] app-name = "UC Central"`; `apx build` regenerates `_metadata.py` from it. |
