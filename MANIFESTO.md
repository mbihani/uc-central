# UC Central — Configuration Manifesto

Every environment-specific or customer-configurable value in this repo is tagged
with a greppable `CONFIGURE(<slug>)` marker comment at its source location. This
file catalogues them all, grouped by priority. To find any value in the codebase:

```bash
grep -rn 'CONFIGURE(' .
```

---

## Required — must set or confirm before deploying

These must match your workspace and pre-created resources. Only **`cli-profile`**
and **`workspace-host`** are truly customer-specific (no usable default — you
supply them via the `--profile` CLI flag). The rest have working defaults but
must *exist* in your workspace for the deploy to succeed.

| Slug | Location | What it does | Default |
|---|---|---|---|
| `cli-profile` | `databricks.yml:103` | The `databricks` CLI auth profile for your workspace. Passed via `--profile <your-profile>` on every bundle command; **not stored in the committed bundle** (no internal host/profile is committed). | *(none — customer-supplied)* |
| `workspace-host` | `databricks.yml:104` | Your Databricks workspace URL. Resolved automatically from the `--profile` above; no value is committed in `databricks.yml`. | *(none — resolved from profile)* |
| `instance-name` | `databricks.yml:39` | Name of the **pre-created** Lakebase Postgres 16 instance the app binds to (becomes `PGAPPNAME` at runtime). Must exist before `bundle deploy` — the bundle does not create it. | `uc-central` |
| `admin-group` | `src/permissions_app/backend/dependencies.py:13` | The workspace group that gates all admin actions. Override only if your workspace uses a non-standard admin group name. | `admins` |
| `production-run-as` | `databricks.yml:71` | The principal the setup job runs as in a **production** target (production bundles require an explicit `run_as`). Omitted in `dev` mode (defaults to the deploying user). Uncomment and set for a `prod` target. | *(dev: deploying user; prod: must set)* |

**Provisioning parameters** (set when creating the Lakebase instance, not in code):
- **PG version** — `PG_VERSION_16` (required; PG 17 breaks the app database binding).
- **Region** — determined by your workspace; the instance is created in the same
  region as the workspace.

---

## Customize — optional but commonly changed

| Slug | Location | What it does | Default |
|---|---|---|---|
| `brand-name` | `pyproject.toml:22` | The customer-facing product name. **Single source of truth** — Vite reads it for the navbar text and HTML `<title>`; `apx build` regenerates `_metadata.py` from it for the FastAPI/OpenAPI title. Rename here and rebuild; no other file needs changing. The build **fails** (not falls back) if this cannot be read. | `UC Central` |
| `app-name` | `databricks.yml:28` | The Databricks App resource name (lowercase, hyphenated). Also the app URL slug and the setup-job parameter. | `uc-central` |
| `capacity` | `DEPLOYMENT.md:61` | Lakebase compute capacity (`CU_1`, `CU_2`, …). Set when creating the instance (Step 1 of DEPLOYMENT.md); the bundle does not manage it. | `CU_1` |
| `pg-app-name` | `.env.example:6` | The Lakebase instance name used in **local development** only. In production this is injected by the app `database` binding from the bundle's `instance_name` variable. | `permissions-app` |

---

## Optional — rarely changed

| Slug | Location | What it does | Default |
|---|---|---|---|
| `log-level` | `src/permissions_app/backend/app.py:45` | Application log verbosity via `PERMISSIONS_APP_LOG_LEVEL` env var (`DEBUG`/`INFO`/`WARNING`/`ERROR`). Never `DEBUG` in production (no PII at `INFO`). | `INFO` |
| `database-name` | `databricks.yml:58` + `src/permissions_app/backend/config.py:32` | The Postgres database name on the Lakebase instance. **In production this is injected** by the app `database` binding as `PGDATABASE` (single source: the `database_name` field in `databricks.yml`); `config.py` reads it via `validation_alias="PGDATABASE"`. The literal default in `config.py` is a local-dev fallback only. Changing it requires updating both locations. | `databricks_postgres` |

---

## Fixed — intentionally not renamed (do not change)

The internal technical slug `permissions_app` is deliberately decoupled from the
customer-facing brand. Renaming it is a breaking refactor (Python package paths,
env var prefix, deploy identity, Lakebase binding) and is avoided. This is the
intended design, not a leftover:

- **Package / wheel / dir**: `permissions_app` (`src/permissions_app/`,
  `permissions_app.backend.app:app` entrypoint, wheel name).
- **Env prefix**: `PERMISSIONS_APP_*` (e.g. `PERMISSIONS_APP_LOG_LEVEL`).
- **Lakebase binding**: `PGAPPNAME` in `.env` / app `database` resource.
- **Note in code**: `src/permissions_app/backend/config.py` (near the env prefix).

The brand name (`UC Central`) and the slug (`permissions_app`) are independent —
a customer renames the brand in `pyproject.toml` without touching the slug.
