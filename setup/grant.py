# Databricks notebook source
# === UC Central — one-shot setup (GRANT schema + SP-admin) =====================
#
# Run AFTER `databricks bundle deploy -t dev` via:
#     databricks bundle run uc-central-setup -t dev
#
# This notebook automates the two non-declarative residues of a fresh UC Central
# deploy that the DAB cannot express:
#   (a) GRANT USAGE, CREATE ON SCHEMA public TO "<app_sp_client_id>"
#       on the fresh Lakebase instance (the app's first-boot create_all needs
#       this; the SP's CAN_CONNECT_AND_CREATE role can create DATABASES but not
#       objects in `schema public`).
#   (b) Add the app's service principal (by numeric service_principal_id) to the
#       workspace `admins` group — new-app SPs ship with a restricted OBO token
#       (iam.*:read only); UC Central performs SCIM/ACL WRITES as the SP.
#
# Idempotent: the GRANT is a no-op if already held, and the group add checks
# membership before patching. Safe to re-run.
#
# Retry-hardened: eventual-consistency races after `bundle deploy` are bounded
# with backoff — (0) the app's SP client_id/id populating, (1) the instance
# reaching AVAILABLE with a non-empty read_write_dns, (2) the GRANT itself while
# the app SP's Postgres role (enrolled by the `database` binding) lags behind,
# (3) the SCIM group PATCH while the freshly-populated SP id propagates to the
# groups service. Permanent errors (auth/permission/config) fail FAST instead of
# burning the whole retry window — via an optional retryable-error predicate.
#
# PREREQUISITES for whoever runs `bundle run uc-central-setup` (the job's run_as
# — the deploying user in dev mode):
#   - workspace ADMIN (to PATCH the `admins` group via SCIM), AND
#   - Postgres/DATABRICKS SUPERUSER on the instance (to issue the GRANT;
#     Databricks superusers are Postgres superusers on Lakebase).
# On serverless compute, `WorkspaceClient()` authenticates as that run-as user.

# --- Parameters (injected by the bundle from ${var.app_name} / ${var.instance_name}) ---
app_name = dbutils.widgets.get("app_name")        # e.g. "uc-central"
instance_name = dbutils.widgets.get("instance_name")  # e.g. "uc-central"

DATABASE_NAME = "databricks_postgres"  # fixed — the app hardcodes this
PGPORT = 5432

# Bounded retry windows (seconds). Generous enough to absorb the worst-case
# post-deploy propagation lag without hanging the job indefinitely.
SP_READY_TIMEOUT = 120        # app SP client_id/id to populate
INSTANCE_READY_TIMEOUT = 600  # instance AVAILABLE + read_write_dns (cold start)
GRANT_READY_TIMEOUT = 180     # app SP's Postgres role to be enrolled by the binding
SCIM_READY_TIMEOUT = 120      # SP id to be resolvable by SCIM group membership
POLL_INTERVAL = 5             # initial backoff; grows via _backoff()

print(f"[setup] app_name={app_name!r} instance_name={instance_name!r}")

import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied, ResourceConflict

w = WorkspaceClient()  # authenticates as the job's run-as identity (deploying user)


def _backoff(elapsed, timeout, interval):
    """Linear backoff capped at 15s; never exceeds the remaining window."""
    return min(interval + elapsed // 10, 15.0, max(0.1, timeout - elapsed))


def _retry(label, fn, timeout, retryable=None):
    """Poll ``fn`` with backoff until ``timeout`` or ``fn`` returns non-None.

    ``retryable(exc) -> bool`` decides whether a raised exception is transient
    (retry) or permanent (re-raise immediately). Default: retry everything,
    preserving the original catch-all behavior for the simple poll sites.

    On timeout, raises a clear, explicit RuntimeError naming what we waited for.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            result = fn()
            if result is not None:
                return result
            last = "not-ready"
        except Exception as e:  # noqa: BLE001 — surface final error below
            if retryable is not None and not retryable(e):
                raise  # permanent error — fail fast, don't burn the window
            last = repr(e)
        elapsed = time.time() - (deadline - timeout)
        wait = _backoff(elapsed, timeout, POLL_INTERVAL)
        print(f"[setup] {label}: not ready ({last}); retrying in {wait:.0f}s...")
        time.sleep(wait)
    raise RuntimeError(
        f"[setup] {label}: timed out after {timeout}s "
        f"(last status: {last!r}). Re-run `databricks bundle run "
        f"uc-central-setup -t dev` once the resource is ready."
    )


# --- (0) Resolve the app's service principal at runtime ----------------------
# Do NOT hardcode: service_principal_client_id / service_principal_id only exist
# AFTER the app is created by `bundle deploy`, and they may LAG the deploy call.


def _app_sp_ready():
    a = w.apps.get(app_name)
    if a.service_principal_client_id and a.service_principal_id:
        return a
    return None


app = w.apps.get(app_name)
sp_client_id = app.service_principal_client_id
sp_id = app.service_principal_id
if not sp_client_id or not sp_id:
    print("[setup] app SP not yet populated — waiting (eventual consistency)...")
    app = _retry("app service principal to populate", _app_sp_ready, SP_READY_TIMEOUT)
    sp_client_id = app.service_principal_client_id
    sp_id = app.service_principal_id
print(f"[setup] app SP: client_id={sp_client_id!r} id={sp_id!r}")


# --- (b) Add the app SP to the workspace `admins` group (SCIM, idempotent) ----
# Raw SCIM via api_client.do. NOTE: the SDK ApiClient.do signature is
#   do(method, path=, url=, query=, headers=, body=, raw=, files=, data=, ...)
# where `body` is serialized as the JSON body (requests json=) and `data` is
# form/stream data (requests data=). The SCIM PatchOp MUST go via `body=` —
# passing it via `data=` would form-encode it and the PATCH silently fails.
# `query=` is the query-string param (correct for the GET filter). Verified
# against databricks-sdk 0.82.0 (_BaseClient._perform: json=body, data=data).
#
# The SP id may populate before it's resolvable by the SCIM groups service, so
# the whole discover+membership-check+PATCH is wrapped in a bounded retry that
# re-checks membership each attempt (idempotent) and only retries transient
# not-found/propagation errors — auth/permission errors fail fast.

def _scim_retryable(e):
    """Transient SCIM errors worth retrying; everything else fails fast."""
    if isinstance(e, (NotFound, ResourceConflict)):
        return True
    msg = str(e).lower()
    # SP/group not yet resolvable by SCIM, or transient propagation/5xx.
    return any(s in msg for s in (
        "not found", "does not exist", "no such", "resource not found",
        "temporarily unavailable", "service unavailable", "503", "504",
    ))


def _ensure_admin_membership():
    """Idempotently add the app SP to the `admins` group.

    Returns the admins group id on success (truthy). Re-checks membership every
    attempt, so a PATCH that succeeded on a prior attempt is a no-op on retry.
    """
    admins = w.api_client.do(
        "GET",
        "/api/2.0/preview/scim/v2/Groups",
        query={"filter": "displayName eq 'admins'"},
    )
    admins_group_id = None
    existing_members = set()
    for g in admins.get("Resources", []):
        if g.get("displayName") == "admins":
            admins_group_id = g.get("id")
            existing_members = {
                str(m.get("value")) for m in g.get("members", []) or []
            }
            break
    if not admins_group_id:
        raise RuntimeError(
            "Workspace `admins` group not found (displayName eq 'admins')."
        )
    if str(sp_id) in existing_members:
        print(f"[setup] SP {sp_id} already in admins (id={admins_group_id!r}) — no-op.")
        return admins_group_id
    print(f"[setup] admins group id={admins_group_id!r} members={len(existing_members)}; adding SP {sp_id}...")
    w.api_client.do(
        "PATCH",
        f"/api/2.0/preview/scim/v2/Groups/{admins_group_id}",
        headers={"Content-Type": "application/scim+json"},
        body={  # body= → JSON body (NOT data=, which would form-encode it)
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "add",
                    "path": "members",
                    "value": [{"value": str(sp_id)}],
                }
            ],
        },
    )
    print(f"[setup] added SP {sp_id} to admins group.")
    return admins_group_id


_retry(
    "SP added to admins group (SCIM propagation)",
    _ensure_admin_membership,
    SCIM_READY_TIMEOUT,
    retryable=_scim_retryable,
)

# --- (a) GRANT USAGE, CREATE ON SCHEMA public TO "<sp_client_id>" ------------
# Connect as the deploying (super)user over TLS, using a short-lived Lakebase
# credential token. The GRANT is idempotent (re-granting is a no-op).
#
# Retry the whole connect+GRANT for a bounded window: the app SP's Postgres role
# is enrolled by the app `database` binding and may LAG `bundle deploy`, so the
# first GRANT can fail with `role "<sp_client_id>" does not exist`. The role
# appearing is eventually consistent; we backoff and retry ONLY on that transient
# error — any other psycopg.Error (auth/permission/config) fails fast.
import psycopg

me = w.current_user.me().user_name
grant_sql = f'GRANT USAGE, CREATE ON SCHEMA public TO "{sp_client_id}";'


def _instance_available():
    inst = w.database.get_database_instance(instance_name)
    if inst.state == "AVAILABLE" and inst.read_write_dns:
        return inst
    return None


def _grant_retryable(e):
    """Only the SP-role-not-yet-enrolled race is retryable; all else fails fast."""
    if isinstance(e, psycopg.Error):
        msg = str(e).lower()
        return "does not exist" in msg and "role" in msg
    # Don't retry arbitrary non-psycopg errors at the GRANT site.
    return False


def _connect_and_grant():
    """Connect over TLS with a fresh credential token and run the GRANT.

    Returns True on success. Raises (for the outer retry's predicate) on the
    transient 'role does not exist' race; re-raises any OTHER error immediately
    so auth/permission/config failures surface instead of burning the window.
    """
    instance = _instance_available()
    host = instance.read_write_dns
    cred = w.database.generate_database_credential(instance_names=[instance_name])
    token = cred.token
    print(f"[setup] connecting to {host}:{PGPORT} as {me!r} (TLS, token-auth)")
    conn = psycopg.connect(
        host=host,
        port=PGPORT,
        dbname=DATABASE_NAME,
        user=me,
        password=token,
        sslmode="require",
    )
    conn.autocommit = True
    try:
        conn.execute(grant_sql)
    finally:
        conn.close()
    return True


# (1) wait for the instance to be AVAILABLE with a read_write_dns (cold start
# after a fresh create / redeploy can take several minutes).
instance = _retry(
    "instance AVAILABLE with read_write_dns", _instance_available, INSTANCE_READY_TIMEOUT
)
print(f"[setup] instance ready: state={instance.state!r} dns={instance.read_write_dns!r}")

# (2) connect + GRANT with bounded retry on the SP-role-not-yet-enrolled race.
# Permanent psycopg errors (permission denied, wrong host, config) fail fast via
# _grant_retryable returning False for anything other than 'role does not exist'.
_retry(
    "GRANT (app SP role enrolled)",
    _connect_and_grant,
    GRANT_READY_TIMEOUT,
    retryable=_grant_retryable,
)
print(f"[setup] GRANT OK: {grant_sql}")

print("[setup] DONE — app SP is an admin and can create objects in schema public.")
