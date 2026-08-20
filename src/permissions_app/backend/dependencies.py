import os
from typing import Annotated, Generator

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.iam import User
from fastapi import Depends, Header, HTTPException, Request
from sqlmodel import Session

from .config import AppConfig
from .logger import logger
from .runtime import Runtime

# CONFIGURE(admin-group): the workspace group that gates all admin actions.
# Defaults to "admins" (standard on every workspace). Override only if your
# workspace uses a non-standard admin group name.
ADMIN_GROUP_NAME = "admins"


def get_config(request: Request) -> AppConfig:
    """
    Returns the AppConfig instance from app.state.
    The config is initialized during application lifespan startup.
    """
    if not hasattr(request.app.state, "config"):
        raise RuntimeError(
            "AppConfig not initialized. "
            "Ensure app.state.config is set during application lifespan startup."
        )
    return request.app.state.config


ConfigDep = Annotated[AppConfig, Depends(get_config)]


def get_runtime(request: Request) -> Runtime:
    """
    Returns the Runtime instance from app.state.
    The runtime is initialized during application lifespan startup.
    """
    if not hasattr(request.app.state, "runtime"):
        raise RuntimeError(
            "Runtime not initialized. "
            "Ensure app.state.runtime is set during application lifespan startup."
        )
    return request.app.state.runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


def get_obo_ws() -> WorkspaceClient:
    """Return the APP SERVICE-PRINCIPAL Databricks client used to EXECUTE writes.

    Authenticates as the app service principal via the app's injected
    DATABRICKS_CLIENT_ID/DATABRICKS_CLIENT_SECRET env. The SP holds the workspace
    privileges needed to read groups/users/resources and to mutate ACLs, so it is
    used to perform the actual workspace operations.

    IMPORTANT: this client is NEVER used to make an authorization decision. The
    SP is a workspace admin, so authorizing against it would let every visitor
    pass. Authorization is performed against the *forwarded request user* (see
    ``get_user_ws`` / ``require_admin``); privileged operations only run through
    this SP client *after* the user has passed that check.

    A client-supplied Personal Access Token is intentionally NOT accepted here:
    the Databricks Apps proxy already authenticates same-origin requests and
    injects the end-user identity, so there is no trusted client-provided token.
    """
    try:
        ws = WorkspaceClient()
        # Validate that credentials actually work by accessing config.
        _ = ws.config.host
        return ws
    except Exception as e:
        logger.error(f"App service-principal credentials unavailable: {e}")
        raise HTTPException(
            status_code=500,
            detail="App service-principal credentials are not configured.",
        )


def get_user_ws(
    x_forwarded_access_token: Annotated[
        str | None, Header(alias="X-Forwarded-Access-Token")
    ] = None,
) -> WorkspaceClient:
    """Return a USER-scoped client built from the Apps-proxy forwarded user token.

    Databricks Apps injects the end user's short-lived OBO OAuth access token via
    the ``X-Forwarded-Access-Token`` header (a client cannot spoof it — the proxy
    strips and re-sets it). We bind a client to THAT token so identity and admin
    checks reflect the real request user, never the app service principal.

    Fails closed with HTTP 401 when the header is absent, so an admin-gated route
    can never be silently served as the SP.
    """
    if not x_forwarded_access_token:
        raise HTTPException(
            status_code=401,
            detail="Missing forwarded user identity (X-Forwarded-Access-Token).",
        )
    # Force bearer-token auth so the SP env creds (DATABRICKS_CLIENT_ID/SECRET,
    # also present in the Apps runtime) are not used instead of the user token.
    return WorkspaceClient(
        host=os.environ.get("DATABRICKS_HOST"),
        token=x_forwarded_access_token,
        auth_type="pat",
    )


UserWsDep = Annotated[WorkspaceClient, Depends(get_user_ws)]


def get_current_user(user_ws: UserWsDep) -> User:
    """Resolve the real request user from the forwarded token.

    Fails closed with HTTP 401 if the token is invalid/expired (i.e. identity
    cannot be established). Never falls back to the SP identity.
    """
    try:
        return user_ws.current_user.me()
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Could not resolve forwarded user identity: {e}")
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired user identity token.",
        )


CurrentUserDep = Annotated[User, Depends(get_current_user)]


def get_session(rt: RuntimeDep) -> Generator[Session, None, None]:
    """
    Returns a SQLModel session.
    """
    with rt.get_session() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def _is_workspace_admin(user: User) -> bool:
    """Check whether the resolved user belongs to the workspace 'admins' group."""
    if user.groups:
        return any(g.display == ADMIN_GROUP_NAME for g in user.groups)
    return False


def require_admin(
    user: CurrentUserDep,
) -> bool:
    """Authorize the REAL forwarded request user as a workspace admin.

    Fails closed:
      * 401 when there is no/invalid forwarded user token (raised upstream by
        ``get_user_ws`` / ``get_current_user``);
      * 403 when the resolved user is not a member of the ``admins`` group.

    The app service principal identity is NEVER used for this decision.
    """
    if not _is_workspace_admin(user):
        raise HTTPException(
            status_code=403,
            detail="Only workspace admins can perform this action.",
        )
    return True


AdminRequired = Annotated[bool, Depends(require_admin)]
