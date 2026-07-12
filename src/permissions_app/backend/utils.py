from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from databricks.sdk.errors import DatabricksError

from .._metadata import api_prefix, dist_dir
from .logger import logger

# Substrings that indicate the caller's Databricks access token is expired or
# otherwise invalid (as opposed to a genuine authorization failure). The Apps
# proxy forwards a short-lived user OBO token via X-Forwarded-Access-Token; once
# it expires the SDK raises PermissionDenied("Invalid access token"), which we
# translate to a 401 so the client can transparently re-authenticate.
_TOKEN_EXPIRY_MARKERS = (
    "invalid access token",
    "token expired",
    "expired token",
    "default auth: cannot configure default credentials",
    "unauthenticated",
)


def _is_token_expiry(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _TOKEN_EXPIRY_MARKERS)


def add_databricks_auth_handler(app: FastAPI):
    """Translate Databricks SDK auth failures into clean HTTP responses.

    An expired/invalid forwarded token becomes a 401 with a machine-readable
    ``code`` so the SPA can clear its stale token and re-auth, instead of a 500
    that both breaks the UI and leaks the full SDK config dump to the client.
    """

    async def databricks_error_handler(request: Request, exc: DatabricksError):
        if _is_token_expiry(exc):
            logger.warning(
                f"Expired/invalid Databricks token on {request.url.path}; "
                f"returning 401 for client re-auth"
            )
            return JSONResponse(
                {
                    "detail": "Your Databricks session has expired. Please reload to sign in again.",
                    "code": "TOKEN_EXPIRED",
                },
                status_code=401,
            )
        # Any other SDK error: log server-side, return a generic 502 without the
        # verbose config dump the SDK puts in the exception message.
        logger.error(f"Databricks SDK error on {request.url.path}: {exc}")
        return JSONResponse(
            {"detail": "Databricks API request failed.", "code": "DATABRICKS_ERROR"},
            status_code=502,
        )

    app.exception_handler(DatabricksError)(databricks_error_handler)


def add_not_found_handler(app: FastAPI):
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        logger.info(
            f"HTTP exception handler called for request {request.url.path} with status code {exc.status_code}"
        )
        if exc.status_code == 404:
            path = request.url.path
            accept = request.headers.get("accept", "")

            is_api = path.startswith(api_prefix)
            is_get_page_nav = request.method == "GET" and "text/html" in accept

            # Heuristic: if the last path segment looks like a file (has a dot), don't SPA-fallback
            looks_like_asset = "." in path.split("/")[-1]

            if (not is_api) and is_get_page_nav and (not looks_like_asset):
                # Let the SPA router handle it. Never cache the shell so a
                # stale copy can't reference chunk names that no longer exist.
                return FileResponse(
                    dist_dir / "index.html",
                    headers={
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0",
                    },
                )
        # Default: return the original HTTP error (JSON 404 for API, etc.)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    app.exception_handler(StarletteHTTPException)(http_exception_handler)
