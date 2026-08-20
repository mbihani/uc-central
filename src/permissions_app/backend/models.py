from enum import Enum
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from .. import __version__


# ─── Enums ────────────────────────────────────────────────────────────────────


class DefaultPersona(str, Enum):
    """Built-in persona keys used for seeding defaults. Not used for validation."""

    ADMIN = "admin"
    DATA_ENGINEER = "data_engineer"
    DATA_SCIENTIST = "data_scientist"
    ANALYST = "analyst"
    DEPLOYER = "deployer"
    SUPPORT = "support"


# Keep backward compat alias for defaults.py
Persona = DefaultPersona


DEFAULT_PERSONA_LABELS: dict[str, str] = {
    DefaultPersona.ADMIN: "Admin",
    DefaultPersona.DATA_ENGINEER: "Data Engineer",
    DefaultPersona.DATA_SCIENTIST: "Data Scientist / ML Engineer",
    DefaultPersona.ANALYST: "Analyst",
    DefaultPersona.DEPLOYER: "Deployer (DevOps)",
    DefaultPersona.SUPPORT: "Support",
}

# Keep backward compat alias
PERSONA_LABELS = DEFAULT_PERSONA_LABELS


class ResourceType(str, Enum):
    CLUSTERS = "clusters"
    CLUSTER_POLICIES = "cluster-policies"
    INSTANCE_POOLS = "instance-pools"
    JOBS = "jobs"
    PIPELINES = "pipelines"
    EXPERIMENTS = "experiments"
    REGISTERED_MODELS = "registered-models"
    REPOS = "repos"
    SERVING_ENDPOINTS = "serving-endpoints"
    WAREHOUSES = "warehouses"
    NOTEBOOKS = "notebooks"
    DIRECTORIES = "directories"
    DASHBOARDS = "dashboards"
    ALERTS = "alerts"
    TOKENS = "authorization"


RESOURCE_TYPE_LABELS: dict[ResourceType, str] = {
    ResourceType.CLUSTERS: "Clusters",
    ResourceType.CLUSTER_POLICIES: "Cluster Policies",
    ResourceType.INSTANCE_POOLS: "Instance Pools",
    ResourceType.JOBS: "Jobs",
    ResourceType.PIPELINES: "DLT Pipelines",
    ResourceType.EXPERIMENTS: "MLflow Experiments",
    ResourceType.REGISTERED_MODELS: "MLflow Registered Models",
    ResourceType.REPOS: "Repos",
    ResourceType.SERVING_ENDPOINTS: "Serving Endpoints",
    ResourceType.WAREHOUSES: "SQL Warehouses",
    ResourceType.NOTEBOOKS: "Notebooks",
    ResourceType.DIRECTORIES: "Directories",
    ResourceType.DASHBOARDS: "Dashboards",
    ResourceType.ALERTS: "Alerts",
    ResourceType.TOKENS: "Tokens",
}


class PermissionLevel(str, Enum):
    NO_PERMISSIONS = "NO_PERMISSIONS"
    CAN_VIEW = "CAN_VIEW"
    CAN_READ = "CAN_READ"
    CAN_ATTACH_TO = "CAN_ATTACH_TO"
    CAN_RESTART = "CAN_RESTART"
    CAN_RUN = "CAN_RUN"
    CAN_EDIT = "CAN_EDIT"
    CAN_MANAGE = "CAN_MANAGE"
    CAN_MANAGE_RUN = "CAN_MANAGE_RUN"
    CAN_MANAGE_STAGING_VERSIONS = "CAN_MANAGE_STAGING_VERSIONS"
    CAN_MANAGE_PRODUCTION_VERSIONS = "CAN_MANAGE_PRODUCTION_VERSIONS"
    IS_OWNER = "IS_OWNER"
    CAN_USE = "CAN_USE"
    CAN_MANAGE_PERMISSIONS = "CAN_MANAGE_PERMISSIONS"


# Allowed permission levels per resource type (based on Databricks ACL definitions).
# Each resource type only supports a specific subset of permission levels.
RESOURCE_PERMISSION_LEVELS: dict[ResourceType, list[PermissionLevel]] = {
    ResourceType.CLUSTERS: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_ATTACH_TO,
        PermissionLevel.CAN_RESTART,
        PermissionLevel.CAN_MANAGE,
    ],
    ResourceType.CLUSTER_POLICIES: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_USE,
    ],
    ResourceType.INSTANCE_POOLS: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_ATTACH_TO,
        PermissionLevel.CAN_MANAGE,
    ],
    ResourceType.JOBS: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_VIEW,
        PermissionLevel.CAN_MANAGE_RUN,
        PermissionLevel.CAN_MANAGE,
        PermissionLevel.IS_OWNER,
    ],
    ResourceType.PIPELINES: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_VIEW,
        PermissionLevel.CAN_RUN,
        PermissionLevel.CAN_MANAGE,
        PermissionLevel.IS_OWNER,
    ],
    ResourceType.EXPERIMENTS: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_READ,
        PermissionLevel.CAN_EDIT,
        PermissionLevel.CAN_MANAGE,
    ],
    ResourceType.REGISTERED_MODELS: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_READ,
        PermissionLevel.CAN_EDIT,
        PermissionLevel.CAN_MANAGE_STAGING_VERSIONS,
        PermissionLevel.CAN_MANAGE_PRODUCTION_VERSIONS,
        PermissionLevel.CAN_MANAGE,
    ],
    ResourceType.REPOS: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_READ,
        PermissionLevel.CAN_RUN,
        PermissionLevel.CAN_EDIT,
        PermissionLevel.CAN_MANAGE,
    ],
    ResourceType.SERVING_ENDPOINTS: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_VIEW,
        PermissionLevel.CAN_MANAGE,
    ],
    ResourceType.WAREHOUSES: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_USE,
        PermissionLevel.CAN_MANAGE,
        PermissionLevel.IS_OWNER,
    ],
    ResourceType.NOTEBOOKS: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_READ,
        PermissionLevel.CAN_RUN,
        PermissionLevel.CAN_EDIT,
        PermissionLevel.CAN_MANAGE,
    ],
    ResourceType.DIRECTORIES: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_READ,
        PermissionLevel.CAN_RUN,
        PermissionLevel.CAN_EDIT,
        PermissionLevel.CAN_MANAGE,
    ],
    ResourceType.DASHBOARDS: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_VIEW,
        PermissionLevel.CAN_RUN,
        PermissionLevel.CAN_EDIT,
        PermissionLevel.CAN_MANAGE,
    ],
    ResourceType.ALERTS: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_VIEW,
        PermissionLevel.CAN_RUN,
        PermissionLevel.CAN_EDIT,
        PermissionLevel.CAN_MANAGE,
    ],
    ResourceType.TOKENS: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_USE,
    ],
}


def is_permission_level_allowed(resource_type: str, permission_level: str) -> bool:
    """Whether ``permission_level`` is valid for ``resource_type``.

    Enforces the per-resource-type allow-lists in ``RESOURCE_PERMISSION_LEVELS``
    so a level a resource type does not support (e.g. ``CAN_MANAGE`` on a
    cluster-policy, which only supports ``CAN_USE``) is rejected before it ever
    reaches the Databricks ACL API.

    - ``NO_PERMISSIONS`` (revoke) is ALWAYS allowed, for every type.
    - An UNKNOWN resource type (not a ``ResourceType`` member) returns ``True``:
      those have no allow-list to validate against and are intentionally left to
      the existing "unsupported resource type" error path, so this predicate
      never masks that clearer, pre-existing error.
    """
    if permission_level == PermissionLevel.NO_PERMISSIONS.value:
        return True
    try:
        rt = ResourceType(resource_type)
    except ValueError:
        return True
    try:
        pl = PermissionLevel(permission_level)
    except ValueError:
        return False
    return pl in RESOURCE_PERMISSION_LEVELS.get(rt, [])


# ─── Database Models (SQLModel) ──────────────────────────────────────────────


class PersonaDefinition(SQLModel, table=True):
    """Stores persona definitions (both built-in defaults and custom ones)."""

    __tablename__ = "persona_definition"

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True)  # slug, e.g. "admin", "my_custom_role"
    label: str  # human-readable name
    description: str = ""
    is_default: bool = Field(default=False)  # True for built-in personas


class PersonaGroupMapping(SQLModel, table=True):
    """Maps a Databricks workspace group to a persona."""

    __tablename__ = "persona_group_mapping"
    # A workspace group maps to AT MOST ONE persona — the app already enforces
    # this in ``create_persona_mapping`` (check-then-insert). The DB-level UNIQUE
    # makes it race-proof: two concurrent creates for the same group can never
    # both land, and the invariant holds even under the multi-worker startup.
    __table_args__ = (
        UniqueConstraint("group_id", name="uq_persona_group_mapping_group_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: str = Field(index=True)
    group_name: str
    persona: str = Field(index=True)  # references PersonaDefinition.key


class PersonaUserMapping(SQLModel, table=True):
    """Maps a Databricks workspace user directly to a persona."""

    __tablename__ = "persona_user_mapping"
    # One direct-assignment row per (persona, user_name). Enforced both here
    # and via ensure_unique_constraints in seed.py (migration bootstrap) so
    # pre-existing databases without the constraint are upgraded idempotently.
    __table_args__ = (
        UniqueConstraint(
            "persona",
            "user_name",
            name="uq_persona_user_mapping_persona_user_name",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    user_name: str  # email / userName from Databricks
    display_name: str = ""
    persona: str = Field(index=True)  # references PersonaDefinition.key


class PermissionTemplate(SQLModel, table=True):
    """Stores the default permission level per persona per resource type."""

    __tablename__ = "permission_template"
    # Exactly ONE row per (persona, resource_type). This is the seed table that
    # previously had NO uniqueness, so a concurrent (multi-worker) seed could
    # insert duplicate cells. The UNIQUE constraint makes the seed an idempotent
    # upsert (INSERT ... ON CONFLICT DO NOTHING) and guarantees a single clean
    # matrix cell per (persona, type).
    __table_args__ = (
        UniqueConstraint(
            "persona",
            "resource_type",
            name="uq_permission_template_persona_resource_type",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    persona: str = Field(index=True)  # references PersonaDefinition.key
    resource_type: str = Field(index=True)
    permission_level: str


# ─── Pydantic Input/Output Models ────────────────────────────────────────────


class VersionOut(BaseModel):
    version: str

    @classmethod
    def from_metadata(cls):
        return cls(version=__version__)


class IsAdminOut(BaseModel):
    is_admin: bool


# Persona Definition models


class PersonaDefinitionIn(BaseModel):
    """Input model for creating a new persona."""

    key: str  # slug identifier
    label: str
    description: str = ""


class PersonaDefinitionUpdateIn(BaseModel):
    """Input model for updating a persona."""

    label: Optional[str] = None
    description: Optional[str] = None


class PersonaDefinitionOut(BaseModel):
    id: int
    key: str
    label: str
    description: str
    is_default: bool


# Persona Group Mapping models


class PersonaGroupMappingIn(BaseModel):
    group_id: str
    group_name: str
    persona: str  # persona key (string, validated against DB)


class PersonaGroupMappingOut(BaseModel):
    id: int
    group_id: str
    group_name: str
    persona: str


# Persona User Mapping models


class PersonaUserMappingIn(BaseModel):
    user_id: str
    user_name: str  # email / userName from Databricks
    display_name: str = ""
    persona: str  # persona key (string, validated against DB)


class PersonaMemberOut(BaseModel):
    """A member of a persona — either via group membership or direct assignment.

    ``assignment_type`` is one of:
      * ``"group"``  — the user is a member of a mapped workspace group;
                       ``groups`` lists which mapped group(s).
      * ``"direct"`` — the user was added directly via the persona user-mapping
                       (per-user ACL entry); not a member of any persona group.
      * ``"both"``   — appears in both paths (group AND direct).
    """

    user_id: str  # SCIM principal id (as it appears in group membership)
    user_name: Optional[str] = None
    display_name: Optional[str] = None
    persona: str
    groups: list[str] = []  # mapped-group display names this member belongs to
    assignment_type: str = "group"  # "group" | "direct" | "both"


class PersonaOut(BaseModel):
    persona: str  # persona key
    label: str
    description: str
    is_default: bool = False
    groups: list[PersonaGroupMappingOut]
    users: list[PersonaMemberOut] = []


# Permission Template models


class PermissionTemplateOut(BaseModel):
    id: int
    persona: str
    resource_type: str
    permission_level: str


class PermissionMatrixCell(BaseModel):
    persona: str  # persona key (string, validated against DB)
    resource_type: ResourceType
    permission_level: PermissionLevel


class PermissionMatrixOut(BaseModel):
    matrix: list[PermissionTemplateOut]
    personas: list[str]
    resource_types: list[str]
    persona_labels: dict[str, str]
    resource_type_labels: dict[str, str]
    allowed_permission_levels: dict[str, list[str]]


# Group / User models (for workspace scanning)


class GroupMemberOut(BaseModel):
    user_id: str
    display_name: Optional[str] = None
    user_name: Optional[str] = None


class GroupOut(BaseModel):
    id: str
    display_name: str
    member_count: int = 0
    members: list[GroupMemberOut] = []


class UserOut(BaseModel):
    id: str
    user_name: Optional[str] = None
    display_name: Optional[str] = None
    active: Optional[bool] = None
    groups: list[str] = []


class AddMemberIn(BaseModel):
    user_id: str


# Resource models


class ResourceItemOut(BaseModel):
    id: str
    name: str
    resource_type: str


class ResourcePermissionOut(BaseModel):
    group_name: Optional[str] = None
    user_name: Optional[str] = None
    all_permissions: list[dict] = []


class ResourcePermissionsOut(BaseModel):
    resource_id: str
    resource_type: str
    access_control_list: list[ResourcePermissionOut] = []


class SetPermissionIn(BaseModel):
    group_name: str
    permission_level: PermissionLevel


class PermissionLevelOut(BaseModel):
    permission_level: str
    description: Optional[str] = None


class ApplyResultOut(BaseModel):
    persona: str
    resource_type: str
    resources_updated: int
    errors: list[str] = []


class ApplyAllResultOut(BaseModel):
    persona: str
    results: list[ApplyResultOut]
    total_resources_updated: int
    total_errors: int
    direct_users_synced: int = 0


# ─── Apply preview (dry-run) + type scoping models ───────────────────────────


class ApplyPermissionsIn(BaseModel):
    """OPTIONAL request body for the persona Apply (POST /permissions/apply/{persona}).

    ``resource_types`` scopes the blast radius: when a non-null list is supplied,
    the apply touches ONLY those resource types — each of the persona's mapped
    groups is still set to THIS persona's own template level for that type;
    no cross-persona resolution. When omitted / null, the apply covers
    ALL resource types in the persona's template (the original behaviour, fully
    backward compatible). Requested types are validated against the persona's
    template on the server.
    """

    resource_types: Optional[list[str]] = None


class ApplyPlanItemOut(BaseModel):
    """One resource type that a REAL apply WOULD write to (dry-run row).

    Only emitted for types that are supported by the apply path AND carry a
    concrete (non-``NO_PERMISSIONS``, type-valid) level in the persona's
    template. ``resource_count`` is how many live workspace resources of this
    type would have the persona's groups (re)set to ``target_level``.
    """

    resource_type: str
    resource_type_label: str
    target_level: str
    resource_count: int


class ApplyPlanSkippedOut(BaseModel):
    """A resource type the dry-run plan skips, with the reason it would NOT be
    written by a real apply.

    ``reason`` is one of:
      * ``no_permissions`` — the template level is ``NO_PERMISSIONS`` (nothing to
        grant; a revoke of a group not present is a no-op);
      * ``unsupported`` — the app has no lister for this type, so apply cannot
        enumerate/write it (it reports an explicit error at apply time);
      * ``invalid_level`` — the template level is not valid for this type (a stale
        matrix cell); apply skips it per-type.
    """

    resource_type: str
    resource_type_label: str
    target_level: str
    reason: str


class ApplyPreviewOut(BaseModel):
    """Dry-run PLAN for applying a persona — computed with ZERO ACL writes.

    Mirrors the real apply's per-type branching so an admin can see the blast
    radius (how many resources of each type would change, and to what level)
    BEFORE any ACL is rewritten, and choose which types to include.
    """

    persona: str
    group_count: int
    groups: list[str]
    plan: list[ApplyPlanItemOut]
    skipped: list[ApplyPlanSkippedOut]
    total_resources_affected: int
    direct_user_count: int = 0


# Dashboard stats


class DashboardStatsOut(BaseModel):
    total_groups: int
    total_users: int
    mapped_groups: int
    unassigned_users: int
    personas_with_groups: int


DEFAULT_PERSONA_DESCRIPTIONS: dict[str, str] = {
    DefaultPersona.ADMIN: "Highest point of escalation. Can access admin console, manage users/groups, configure cluster policies, and manage workspace settings.",
    DefaultPersona.DATA_ENGINEER: "Builds data pipelines and ETL flows. Has access to their own assets, visibility of shared assets, and view access to jobs and DLT.",
    DefaultPersona.DATA_SCIENTIST: "Develops ML models and experiments. Has access to their own assets and read access to ML models and experiments.",
    DefaultPersona.ANALYST: "Focuses on SQL analysis and BI. Has access to SQL warehouses and can author queries, dashboards, and alerts.",
    DefaultPersona.DEPLOYER: "Responsible for CI/CD and deploying artifacts. Can manage most workspace objects. Recommended to be a service principal.",
    DefaultPersona.SUPPORT: "Maintains and troubleshoots production systems. Can view jobs, pipelines, models, and dashboards for debugging.",
}

# Keep backward compat alias
PERSONA_DESCRIPTIONS = DEFAULT_PERSONA_DESCRIPTIONS


# ─── Permission Hierarchy Helpers ────────────────────────────────────────────


def get_permission_rank(resource_type: str, permission_level: str) -> int:
    """Get the rank of a permission level for a given resource type.

    Uses the index in RESOURCE_PERMISSION_LEVELS for the resource type.
    Higher rank = higher permission. NO_PERMISSIONS = 0.
    """
    try:
        rt = ResourceType(resource_type)
        pl = PermissionLevel(permission_level)
        levels = RESOURCE_PERMISSION_LEVELS.get(rt, [])
        if pl in levels:
            return levels.index(pl)
        return 0
    except (ValueError, KeyError):
        return 0


def get_higher_permission(resource_type: str, *levels: str) -> str:
    """Return the highest permission level from the given levels for a resource type."""
    if not levels:
        return PermissionLevel.NO_PERMISSIONS.value
    best = levels[0]
    best_rank = get_permission_rank(resource_type, best)
    for lvl in levels[1:]:
        rank = get_permission_rank(resource_type, lvl)
        if rank > best_rank:
            best = lvl
            best_rank = rank
    return best


# ─── Conflict Detection Models ───────────────────────────────────────────────


class PermissionConflictDetail(BaseModel):
    """A single resource-type conflict for a user across personas."""

    resource_type: str
    resource_type_label: str
    persona_levels: dict[str, str]  # persona_label -> permission_level
    effective_level: str  # highest permission (resolved)


class UserPermissionConflict(BaseModel):
    """Conflicts for a single user who spans multiple personas."""

    user_id: str
    user_name: Optional[str] = None
    display_name: Optional[str] = None
    personas: list[str]  # persona labels
    conflict_count: int
    conflicts: list[PermissionConflictDetail]


class PermissionConflictsOut(BaseModel):
    """Full conflict check response."""

    total_users_checked: int
    users_with_conflicts: int
    conflicts: list[UserPermissionConflict]
