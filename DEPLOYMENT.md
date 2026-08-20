# UC Central — Deployment Guide

Deploying **UC Central** (a Databricks App: FastAPI backend + React/Vite UI, backed
by a Lakebase Postgres database) into a Databricks workspace.

> **What you provision vs what the bundle automates.** The app code is fully
> portable — it connects to whatever workspace it runs in via the Databricks SDK.
> The bundle **declaratively** creates the app and attaches the Lakebase
> `database` resource binding. Two things are **not** captured in the bundle and
> must be done by hand on every fresh deploy: (1) **Lakebase Postgres instance
> provisioning** (the bundle cannot pin `pg_version`, so you pre-create a PG 16
> instance), and (2) the **Postgres `CREATE ON SCHEMA public` grant** for the app's
> service principal. This guide makes both explicit. See **MANIFESTO.md** for
> every configurable value.

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
- A **workspace admin** account (you need admin + Postgres superuser to run the
  one-shot setup job — see below).

---

## ⚠️ The PG 16 requirement (read before deploying)

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

## Procedure

### 1. Pre-create the Lakebase PG 16 instance (once, required)

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
> `instance_name` variable.

### 2. Build the artifact

```bash
cd uc-central
node_modules/.bin/vite build                       # React UI -> src/permissions_app/__dist__
.venv/bin/apx build --skip-ui-build               # wheel + .build/ (regenerates _metadata.py)
ls -1 .build                                       # expect: <wheel>.whl, requirements.txt, app.yml
```

`bundle deploy` syncs the pre-built `.build/` directory — it runs no build step,
so build before deploying.

### 3. Validate (read-only)

```bash
databricks bundle validate -t dev --profile <your-profile>
```

Requires Databricks CLI v1.3+ with `bundle.engine: direct` (already set in
`databricks.yml`). Must report `Validation OK!`.

### 4. Deploy

Creates the UC Central app and binds it to the external PG 16 instance:

```bash
databricks bundle deploy -t dev --profile <your-profile>
```

### 5. One-shot setup: GRANT schema + SP-admin

Run **after** `bundle deploy`. The setup job (`setup/grant.py`) resolves the
app's service principal at runtime (no hardcoded IDs — they only exist after the
app is created) and performs the two things the DAB cannot express declaratively:

1. **GRANT** `USAGE, CREATE ON SCHEMA public TO "<app_sp_client_id>"` on the
   instance — the SP's `CAN_CONNECT_AND_CREATE` role can create *databases* but
   not objects in `schema public`; without this the app's first-boot `create_all`
   fails.
2. **ADMIN**: adds the app SP (by numeric `service_principal_id`) to the workspace
   `admins` group via SCIM — new-app SPs ship with a restricted OBO token
   (`iam.*:read` only); UC Central performs SCIM/ACL **writes** as the SP.

Both are no-ops if already done, so the job is safe to re-run. It also retries with
bounded backoff for three eventual-consistency races after `bundle deploy`: the app
SP's client_id/id populating, the instance reaching AVAILABLE, and the GRANT itself
while the app SP's Postgres role lags behind the deploy.

```bash
databricks bundle run uc-central-setup -t dev --profile <your-profile>
```

> **Whoever runs this must be BOTH:**
> - a **workspace admin** (required for the SCIM PATCH to the `admins` group), AND
> - a **Postgres/Databricks superuser** on the instance (required for the GRANT;
>   Databricks superusers are Postgres superusers on Lakebase).

### 6. Verify

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

## Direct-engine footgun

With `engine: direct`, **removing a field from `databricks.yml` reverts the
resource to that field's default** (the direct engine reconciles desired-state vs
current-state; an absent field means "default it"). To unset a setting, you must
explicitly set it back to its default — you cannot simply delete the line.

---

## Rollback / cleanup

```bash
databricks bundle destroy -t dev --profile <your-profile>       # deletes the app + setup job only
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

Then `databricks bundle validate -t prod --profile <their-profile>`,
`bundle deploy -t prod --profile <their-profile>`,
`bundle run uc-central-setup -t prod --profile <their-profile>`.

Additional notes for a production target:
- **PG 16 instance**: pre-create one (see the PG 16 requirement above). The bundle
  cannot pin `pg_version` itself.
- **`admins` group**: must exist (the admin gate is keyed to the group literally
  named `admins`); the setup job adds the app SP to it. Override
  `ADMIN_GROUP_NAME` in `dependencies.py` if your workspace uses a different name.
- **Superuser for `bundle run`**: whoever runs the setup job must be BOTH a
  workspace admin AND a Postgres/Databricks superuser on the instance.
- **External instance lifecycle**: the PG 16 instance is created outside the
  bundle, so `bundle destroy` does not delete it.
- **Production `run_as`**: in `production` mode, set an explicit `run_as` for the
  setup job (`run_as: { service_principal_name: ... }` or `user_name: ...`).
- **Front-door IP ACL**: admins reaching the UI are subject to the workspace IP
  access list (workspace config, not app config).

---

## Fallback: manual deploy (`apps deploy`)

Use this if the turnkey DAB path is blocked (e.g. your CLI is < v1.3.0). It
replicates what the bundle does, step by step:

1. Build the artifact (see Step 2 above).
2. Create the Lakebase PG 16 instance (see Step 1 above).
3. Create the app (auto-provisions its service principal):
   ```bash
   databricks apps create uc-central --description "UC Central — permissions management" --profile <your-profile>
   ```
4. Attach the Lakebase `database` resource via the raw REST PATCH (`apps update
   --json` drops the `database` field):
   ```bash
   databricks api patch /api/2.0/apps/uc-central --profile <your-profile> --json '{
     "resources": [
       { "name": "db",
         "database": {
           "instance_name": "uc-central",
           "database_name": "databricks_postgres",
           "permission": "CAN_CONNECT_AND_CREATE"
         } } ] }'
   ```
5. Add the app SP to the `admins` group (SCIM):
   ```bash
   # find the admins group id
   databricks api get "/api/2.0/preview/scim/v2/Groups?filter=displayName+eq+admins" --profile <your-profile> \
     | jq '.Resources[] | {id, displayName}'
   # add the app SP by its numeric service_principal_id
   databricks api patch /api/2.0/preview/scim/v2/Groups/<admins_group_id> --profile <your-profile> --json '{
     "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
     "Operations": [ { "op": "add", "path": "members", "value": [ { "value": "<sp_id>" } ] } ] }'
   ```
6. Grant schema-create to the app SP (a Databricks superuser must run this):
   ```bash
   # get a short-lived DB credential token
   TOKEN=$(databricks api post /api/2.0/database/credentials --profile <your-profile> --json '{
     "instance_names": ["uc-central"]
   }' | jq -r '.token')
   PGPASSWORD="$TOKEN" psql \
     "host=<read_write_dns> port=5432 dbname=databricks_postgres user=<your-user> sslmode=require" \
     -c 'GRANT USAGE, CREATE ON SCHEMA public TO "<sp_client_id>";'
   ```
7. Upload the artifact and deploy:
   ```bash
   REMOTE=/Workspace/Users/<your-user>/uc-central/.build
   for f in $(ls .build); do
     databricks workspace import "$REMOTE/$f" --file ".build/$f" --format RAW --overwrite --profile <your-profile>
   done
   databricks apps deploy uc-central --source-code-path "$REMOTE" --mode SNAPSHOT --profile <your-profile>
   ```
8. Verify (see Step 6 above).

> Upload the wheel **and** `requirements.txt` together. A wheel-only re-upload
> leaves `requirements.txt` pointing at a deleted wheel, yet `apps deploy` still
> reports SUCCEEDED while the app silently runs the old wheel.

---

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| App crashes on boot, `create_all`/permission-denied on `schema public` | Step 5 grant missing or ran before the SP role existed. Re-run `bundle run uc-central-setup` after confirming the SP role exists on the instance (the `database` binding enrolls it). |
| App boots but DB endpoints 500 / can't connect | `database` resource not attached, or attached with `apps update --json` (which drops it) — re-attach via `api patch`. |
| SCIM/ACL operations 403 as the app | App SP not in `admins` group — re-run the setup job. |
| Deploy SUCCEEDED but old code runs | Wheel/`requirements.txt` mismatch — re-upload both together, redeploy. |
| `bundle deploy` fails to reconcile | Ensure you are on Databricks CLI v1.3+ and the bundle uses `engine: direct` (already set in `databricks.yml`). |
| Brand shows the old slug after a rebuild | Ensure `pyproject [tool.apx.metadata] app-name = "UC Central"`; `apx build` regenerates `_metadata.py` from it. |
