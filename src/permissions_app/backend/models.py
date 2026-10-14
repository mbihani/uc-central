from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from .. import __version__


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    # AI/BI Genie spaces. The permissions API object type is literally "genie"
    # (verified against get_permission_levels), so the enum value doubles as the
    # `request_object_type` passed straight to the ACL API.
    GENIE_SPACES = "genie"
    # Databricks Apps. Permissions API object type "apps"; the ACL API keys on the
    # app NAME (its id/uuid is rejected by permissions.get), verified live.
    APPS = "apps"
    # Lakebase database instances. Permissions API object type "database-instances";
    # like apps, the ACL API keys on the instance NAME (uid rejected), verified live.
    DATABASE_INSTANCES = "database-instances"
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
    ResourceType.GENIE_SPACES: "Genie Spaces",
    ResourceType.APPS: "Apps",
    ResourceType.DATABASE_INSTANCES: "Lakebase Instances",
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
    # Genie spaces expose CAN_READ / CAN_RUN / CAN_EDIT / CAN_MANAGE (no CAN_VIEW);
    # confirmed via permissions.get_permission_levels(request_object_type="genie").
    ResourceType.GENIE_SPACES: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_READ,
        PermissionLevel.CAN_RUN,
        PermissionLevel.CAN_EDIT,
        PermissionLevel.CAN_MANAGE,
    ],
    # Apps and Lakebase instances both expose only CAN_USE / CAN_MANAGE
    # (confirmed via permissions.get_permission_levels), plus NO_PERMISSIONS for revoke.
    ResourceType.APPS: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_USE,
        PermissionLevel.CAN_MANAGE,
    ],
    ResourceType.DATABASE_INSTANCES: [
        PermissionLevel.NO_PERMISSIONS,
        PermissionLevel.CAN_USE,
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


# ─── Genie access bundles ────────────────────────────────────────────────────
#
# A bundle ties ONE Genie space to ONE pre-existing ACCOUNT-level group. Applying
# it grants the group CAN_RUN on the space plus the per-table UC privileges the
# space needs (SELECT on each table/view, USE_SCHEMA / USE_CATALOG on each
# parent). Every privilege the bundle relies on is recorded in the grant ledger;
# revocation is driven ONLY by the ledger, so the app never revokes a grant it
# did not create. See docs/genie-access-bundles.md.


GENIE_BUNDLE_SPACE_UNIQUE = "uq_genie_access_bundle_space_id"
GENIE_LEDGER_GROUP_ID_CHECK = "ck_genie_grant_ledger_group_id_nonempty"

# Grant-ledger row states. A PENDING row is written durably BEFORE the workspace
# grant call and flipped to CONFIRMED after it, so a crash in between can be
# reconciled instead of leaving an untracked app-created grant.
LEDGER_PENDING = "pending"
LEDGER_CONFIRMED = "confirmed"


class GenieAccessBundle(SQLModel, table=True):
    """One Genie space <-> one account group."""

    __tablename__ = "genie_access_bundle"
    # ONE group per space. A unique INDEX (not a table constraint) so the same
    # DDL — CREATE UNIQUE INDEX IF NOT EXISTS — upgrades databases created before
    # this rule existed (see seed.ensure_genie_bundle_schema).
    __table_args__ = (
        Index(GENIE_BUNDLE_SPACE_UNIQUE, "space_id", unique=True),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    space_id: str = Field(index=True)
    space_title: str = ""
    # group_id (immutable SCIM id) is the identity; group_name is the CURRENT
    # display name, refreshed from group_id before every operation.
    group_name: str = Field(index=True)
    group_id: str = ""
    created_by: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    last_applied_at: Optional[datetime] = None
    last_applied_by: Optional[str] = None
    # Reserved for the row/column restriction follow-up (docs/genie-access-
    # bundles.md): "none" today; later e.g. "row_filter" or "abac". Nothing in
    # this PR reads it beyond echoing it back.
    restriction_mode: str = "none"


class GenieGrantLedger(SQLModel, table=True):
    """One privilege a bundle relies on, on one securable, for its group.

    ``app_granted`` is True when THIS app issued the grant (or inherited
    ownership of an app-issued grant from a deleted bundle). Rows with
    ``app_granted=False`` record a privilege that already existed when the
    bundle was applied: they mark the privilege as "needed" (so another bundle
    never revokes it out from under this one) but are never revoked themselves.
    """

    __tablename__ = "genie_grant_ledger"
    __table_args__ = (
        UniqueConstraint(
            "bundle_id",
            "principal",
            "securable_type",
            "full_name",
            "privilege",
            name="uq_genie_grant_ledger_bundle_grant",
        ),
        # group_id is the identity / lock / sharing key: never empty. (NOT NULL
        # comes from the required field below.) There is NO foreign key to
        # genie_access_bundle, so seed.ensure_genie_bundle_schema also refuses
        # to start if any row lacks it.
        CheckConstraint("group_id <> ''", name=GENIE_LEDGER_GROUP_ID_CHECK),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    bundle_id: int = Field(index=True)
    # Immutable SCIM id of the bundle's group: the key for per-grant locks and
    # for "does another bundle still need this grant?". ``principal`` is the
    # group's display name as last seen (informational; UC calls re-resolve it).
    group_id: str = Field(index=True, nullable=False)  # REQUIRED: no default
    space_id: str = Field(index=True)
    principal: str = Field(index=True)
    # "genie_space" (ACL CAN_RUN) or a UC securable type: catalog/schema/table/volume.
    securable_type: str
    full_name: str = Field(index=True)
    privilege: str
    app_granted: bool = Field(default=True)
    state: str = Field(default=LEDGER_CONFIRMED)  # LEDGER_PENDING | LEDGER_CONFIRMED
    granted_at: datetime = Field(default_factory=_utcnow)
    granted_by: str = ""


class GenieBundleIn(BaseModel):
    space_id: str
    group_name: str


class GenieBundleOut(BaseModel):
    id: int
    space_id: str
    space_title: str
    group_name: str
    group_id: str
    created_by: str
    created_at: datetime
    last_applied_at: Optional[datetime] = None
    last_applied_by: Optional[str] = None
    restriction_mode: str = "none"
    ledger_count: int = 0


class GenieGroupCheckOut(BaseModel):
    group_name: str
    group_id: Optional[str] = None
    exists: bool
    is_account_group: bool
    # SCIM meta.resourceType: "Group" (account group) or "WorkspaceGroup".
    resource_type: Optional[str] = None
    error: Optional[str] = None
    # Set when the group (same SCIM id) now has a different display name than
    # the bundle recorded. Preview only reports it; mutating paths persist it.
    renamed_from: Optional[str] = None


class GenieSpaceObjectOut(BaseModel):
    """A data source referenced by the space's serialized definition."""

    kind: str  # "table" (tables, views, MVs, metric views) or "volume"
    identifier: str  # as written in the serialized space
    full_name: Optional[str] = None  # resolved catalog.schema.name
    table_type: Optional[str] = None  # MANAGED / VIEW / METRIC_VIEW / ...
    resolved: bool
    reason: Optional[str] = None  # why it could not be resolved
    has_row_filter: bool = False
    row_filter_function: Optional[str] = None
    masked_columns: list[str] = []


class GenieGrantOut(BaseModel):
    securable_type: str
    full_name: str
    privilege: str
    # missing      — would be / was granted by this bundle
    # present      — already effective, not created by the app (never revoked)
    # managed      — already effective AND owned by the app's ledger
    # stale        — in this bundle's ledger but no longer needed (resync drops it)
    status: str
    detail: Optional[str] = None


class GenieBundlePreviewOut(BaseModel):
    bundle_id: int
    space_id: str
    space_title: str
    warehouse_id: Optional[str] = None
    group: GenieGroupCheckOut
    objects: list[GenieSpaceObjectOut]
    grants: list[GenieGrantOut]
    stale_grants: list[GenieGrantOut] = []
    blockers: list[str] = []
    warnings: list[str] = []
    can_apply: bool


class GenieBundleActionOut(BaseModel):
    bundle_id: int
    action: str  # apply | resync | delete
    status: str  # ok | partial | failed
    granted: list[GenieGrantOut] = []
    adopted: list[GenieGrantOut] = []
    revoked: list[GenieGrantOut] = []
    released: list[GenieGrantOut] = []
    # PENDING rows from an interrupted run, confirmed or dropped this run.
    reconciled: list[GenieGrantOut] = []
    errors: list[str] = []
    warnings: list[str] = []
    deleted: bool = False
