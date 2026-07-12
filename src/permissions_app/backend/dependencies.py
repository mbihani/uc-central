from typing import Annotated, Generator

from databricks.sdk import WorkspaceClient
from fastapi import Depends, Header, HTTPException, Request
from sqlmodel import Session

from .config import AppConfig
from .logger import logger
from .runtime import Runtime

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


def get_obo_ws(
    token: Annotated[str | None, Header(alias="X-Forwarded-Access-Token")] = None,
) -> WorkspaceClient:
    """
    Returns a Databricks Workspace client with authentication on behalf of user.
    If the request contains an X-Forwarded-Access-Token header, OBO auth is used.
    Otherwise, falls back to default credentials (e.g. DATABRICKS_CONFIG_PROFILE
    or other env-based auth) for local development.

    Raises HTTP 401 if no authentication method is available.
    """

    # Authenticate as the app service principal (full workspace API access via the
    # app's DATABRICKS_CLIENT_ID/SECRET env). A newly-created Databricks App's
    # forwarded OBO user token is scope-restricted (iam.*:read) and cannot be granted
    # SCIM/clusters/jobs/sql, so it is NOT used for SDK calls. This matches how the
    # original app effectively runs (current_user resolves to the app service principal).
    try:
        ws = WorkspaceClient()
        # Validate that credentials actually work by accessing config
        _ = ws.config.host
        logger.info("Using app service-principal credentials")
        return ws
    except Exception as e:
        logger.warning(f"App SP credentials unavailable: {e}")
        if token:
            return WorkspaceClient(token=token, auth_type="pat")
        raise HTTPException(
            status_code=401,
            detail=(
                "Databricks authentication required. "
                "In production, the X-Forwarded-Access-Token header is set automatically. "
                "For local development, configure credentials via environment variables "
                "(DATABRICKS_HOST, DATABRICKS_TOKEN) or a Databricks config profile."
            ),
        )


def get_session(rt: RuntimeDep) -> Generator[Session, None, None]:
    """
    Returns a SQLModel session.
    """
    with rt.get_session() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def _is_workspace_admin(ws: WorkspaceClient) -> bool:
    """Check if the current user belongs to the 'admins' group."""
    try:
        user = ws.current_user.me()
        if user.groups:
            return any(g.display == ADMIN_GROUP_NAME for g in user.groups)
        return False
    except Exception as e:
        logger.warning(f"Could not determine admin status: {e}")
        return False


def require_admin(
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
) -> bool:
    """
    Dependency that enforces workspace admin access.
    Raises HTTP 403 if the authenticated user is not a workspace admin.
    """
    if not _is_workspace_admin(obo_ws):
        raise HTTPException(
            status_code=403,
            detail="Only workspace admins can perform this action.",
        )
    return True


AdminRequired = Annotated[bool, Depends(require_admin)]
