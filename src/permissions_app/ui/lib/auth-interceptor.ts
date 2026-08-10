// sessionStorage key holding the timestamp of the last auth-driven reload,
// used to prevent reload loops if a fresh token still fails.
const RELOAD_GUARD_KEY = "databricks-auth-reload-at";
const RELOAD_MIN_INTERVAL_MS = 30_000;

const originalFetch = window.fetch.bind(window);

/**
 * Recover from an expired/invalid forwarded token.
 *
 * In a Databricks App the Apps proxy authenticates every same-origin request
 * and injects the end-user's short-lived OBO token (X-Forwarded-Access-Token)
 * itself — the SPA never stores or sends a token. When a long-open tab's
 * forwarded token expires, API calls start returning 401. A full page reload
 * re-runs the proxy OAuth flow and yields a freshly minted token, so we reload —
 * guarded so a persistently failing token can't cause a reload loop.
 */
function recoverFromAuthExpiry(): void {
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

  // Intentionally no client-side auth header is attached: the Apps proxy
  // authenticates the request and injects the forwarded user identity.
  const response = await originalFetch(input, init);

  if (isApi) {
    if (response.status === 401) {
      // Expired/invalid forwarded token — reload to obtain a fresh one.
      recoverFromAuthExpiry();
    } else if (response.ok) {
      // A good response means auth is healthy again; reset the reload guard so
      // a future expiry can recover promptly.
      sessionStorage.removeItem(RELOAD_GUARD_KEY);
    }
  }

  return response;
};
