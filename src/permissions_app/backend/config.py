from importlib import resources
from pathlib import Path
from typing import ClassVar

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .._metadata import app_name, app_slug

# NOTE: the internal technical slug `permissions_app` (package dir, wheel name,
# PERMISSIONS_APP_* env prefix, PGAPPNAME binding) is intentionally kept as-is
# and deliberately decoupled from the customer-facing brand "UC Central" (which
# lives in pyproject `app-name`). Renaming the slug is a breaking refactor
# (package paths, env vars, deploy identity) and is avoided. See MANIFESTO.md.

# project root is the parent of the src folder
project_root = Path(__file__).parent.parent.parent.parent
env_file = project_root / ".env"

if env_file.exists():
    load_dotenv(dotenv_path=env_file)


class DatabaseConfig(BaseSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        extra="ignore",
    )
    port: int = Field(
        description="The port of the database", default=5432, validation_alias="PGPORT"
    )
    # CONFIGURE(database-name): the Postgres database name. In production this is
    # injected by the app `database` binding as PGDATABASE (single source: the
    # `database_name` field in databricks.yml). The default below is a local-dev
    # fallback only — do not rely on it in production.
    database_name: str = Field(
        description="The name of the database",
        default="databricks_postgres",
        validation_alias="PGDATABASE",
    )
    instance_name: str = Field(
        description="The name of the database instance", validation_alias="PGAPPNAME"
    )


class AppConfig(BaseSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=env_file,
        env_prefix=f"{app_slug.upper()}_",
        extra="ignore",
        env_nested_delimiter="__",
    )
    app_name: str = Field(default=app_name)
    db: DatabaseConfig = DatabaseConfig()  # type: ignore

    @property
    def static_assets_path(self) -> Path:
        return Path(str(resources.files(app_slug))).joinpath("__dist__")

    def __hash__(self) -> int:
        return hash(self.app_name)
