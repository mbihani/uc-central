from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .._metadata import app_name, dist_dir

# Cache-control applied to the SPA HTML shell (index.html). Content-hashed
# assets under /assets/ stay cacheable; only the shell that references those
# hashed names must never be cached, so a stale shell can't request chunk
# names that no longer exist (404) and a recovery reload always fetches the
# current bundle.
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


class NoCacheHTMLStaticFiles(StaticFiles):
    """StaticFiles that marks HTML responses (the SPA shell) as non-cacheable."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if "text/html" in response.headers.get("content-type", ""):
            response.headers.update(_NO_CACHE_HEADERS)
        return response
from .config import AppConfig
from .router import api
from .runtime import Runtime
from .utils import add_databricks_auth_handler, add_not_found_handler
from .logger import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize config and runtime, store in app.state for dependency injection
    config = AppConfig()
    logger.info(f"Starting app with configuration:\n{config}")

    runtime = Runtime(config)
    runtime.validate_db()
    runtime.initialize_models()

    # Store in app.state for access via dependencies
    app.state.config = config
    app.state.runtime = runtime

    yield


app = FastAPI(title=f"{app_name}", lifespan=lifespan)
ui = NoCacheHTMLStaticFiles(directory=dist_dir, html=True)

# note the order of includes and mounts!
app.include_router(api)
app.mount("/", ui)


add_databricks_auth_handler(app)
add_not_found_handler(app)
