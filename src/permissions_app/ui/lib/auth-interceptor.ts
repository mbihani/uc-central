const AUTH_TOKEN_KEY = "databricks-auth-token";

// Header a caller can set to opt a request out of automatic auth recovery.
// The manual login probe (use-auth) sets this so a bad user-entered token
// surfaces an error in the dialog instead of triggering a page reload.
const SKIP_AUTH_RECOVERY_HEADER = "X-Skip-Auth-Recovery";

// sessionStorage key holding the timestamp of the last auth-driven reload,
// used to prevent reload loops if a fresh token still fails.
const RELOAD_GUARD_KEY = "databricks-auth-reload-at";
const RELOAD_MIN_INTERVAL_MS = 30_000;

const originalFetch = window.fetch.bind(window);

/**
 * Recover from an expired/invalid forwarded token.
 *
 * The Databricks Apps proxy injects a short-lived user OBO token via
 * X-Forwarded-Access-Token. When a long-open tab's token expires, API calls
 * start returning 401. A full page reload re-runs the proxy OAuth flow and
 * yields a freshly minted token, so we clear any stale manually-entered token
 * and reload — guarded so a persistently failing token can't cause a loop.
 */
function recoverFromAuthExpiry(): void {
  localStorage.removeItem(AUTH_TOKEN_KEY);

  const last = Number(sessionStorage.getItem(RELOAD_GUARD_KEY) || 0);
  const now = Date.now();
  if (now - last > RELOAD_MIN_INTERVAL_MS) {
    sessionStorage.setItem(RELOAD_GUARD_KEY, String(now));
    window.location.reload();
  }
}

window.fetch = async function (
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const url =
    typeof input === "string"
      ? input
      : input instanceof URL
        ? input.toString()
        : input.url;

  const isApi = url.startsWith("/api/");
  const skipRecovery = new Headers(init?.headers).has(SKIP_AUTH_RECOVERY_HEADER);

  if (isApi) {
    const token = localStorage.getItem(AUTH_TOKEN_KEY);
    if (token) {
      const headers = new Headers(init?.headers);
      if (!headers.has("X-Forwarded-Access-Token")) {
        headers.set("X-Forwarded-Access-Token", token);
        init = { ...init, headers };
      }
    }
  }

  const response = await originalFetch(input, init);

  if (isApi && !skipRecovery) {
    if (response.status === 401) {
      // Expired/invalid token — clear it and reload to obtain a fresh one.
      recoverFromAuthExpiry();
    } else if (response.ok) {
      // A good response means auth is healthy again; reset the reload guard so
      // a future expiry can recover promptly.
      sessionStorage.removeItem(RELOAD_GUARD_KEY);
    }
  }

  return response;
};

export { AUTH_TOKEN_KEY, SKIP_AUTH_RECOVERY_HEADER };
