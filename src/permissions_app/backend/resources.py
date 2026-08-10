"""
Helper functions to list workspace resources by type using the Databricks SDK.
Each function returns a list of ResourceItemOut for consistent handling.
"""

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import iam

from .logger import logger
from .models import PermissionLevel, ResourceItemOut, ResourceType

# Resource types whose individual resources this app can actually enumerate
# (i.e. `list_resources` has a real lister). Types advertised in the matrix but
# NOT in this set (notebooks, directories, alerts, authorization/tokens) cannot
# be applied by the persona "Apply" path, and the apply endpoint reports an
# explicit error for them instead of a silent, false "success".
SUPPORTED_ACL_RESOURCE_TYPES: set[str] = {
    ResourceType.CLUSTERS.value,
    ResourceType.CLUSTER_POLICIES.value,
    ResourceType.INSTANCE_POOLS.value,
    ResourceType.JOBS.value,
    ResourceType.PIPELINES.value,
    ResourceType.EXPERIMENTS.value,
    ResourceType.REGISTERED_MODELS.value,
    ResourceType.REPOS.value,
    ResourceType.SERVING_ENDPOINTS.value,
    ResourceType.WAREHOUSES.value,
    ResourceType.DASHBOARDS.value,
}

# MLflow experiment-kind tag. Notebook-backed experiments carry
# `mlflow.experimentType == "NOTEBOOK"` and are NOT permissionable via the
# experiments ACL API (they are permissioned through their backing notebook);
# `_list_experiments` filters them out. See that function for details.
_EXPERIMENT_TYPE_TAG = "mlflow.experimentType"
_NOTEBOOK_EXPERIMENT_TYPE = "NOTEBOOK"


def apply_group_acl(
    ws: WorkspaceClient,
    resource_type: str,
    resource_id: str,
    group_levels: dict[str, str],
) -> None:
    """Merge-apply a set of group permission levels onto a single resource.

    This is the single source of truth for pushing ACLs to the workspace, used
    by BOTH the per-persona matrix "Apply" and the per-resource editor. It does a
    read-modify-write MERGE so that only the managed groups are touched:

      1. GET the resource's current permissions.
      2. Carry forward every OTHER principal's *direct* grant untouched — other
         groups, individual users, service principals, and owners (IS_OWNER).
         Inherited-only entries (e.g. the `admins` group) are dropped from the
         write set because they are not direct ACLs; they keep inheriting.
      3. For each managed group: set it to the requested level, or REMOVE it
         entirely when the level is NO_PERMISSIONS (this is how a revoke /
         downgrade-to-none happens).
      4. PUT the merged full ACL (`permissions.set`).

    Using PUT with an explicitly-merged ACL (rather than a blind PATCH that can
    only add/raise) is what makes grant, downgrade AND revoke all work while
    never clobbering existing users, owners, or admins.

    Raises on any SDK error so the caller can surface it per-resource.
    """
    current = ws.permissions.get(
        request_object_type=resource_type, request_object_id=resource_id
    )

    merged: list[iam.AccessControlRequest] = []
    for entry in current.access_control_list or []:
        # The direct (non-inherited) permission level held by this principal.
        direct_level: iam.PermissionLevel | None = None
        for p in entry.all_permissions or []:
            if p.permission_level and not p.inherited:
                direct_level = p.permission_level
                break
        if direct_level is None:
            # Only inherited permissions -> not part of the direct ACL; skip.
            continue

        # Managed groups are (re)applied below, so drop any existing entry here.
        if entry.group_name is not None and entry.group_name in group_levels:
            continue

        if entry.user_name is not None:
            merged.append(
                iam.AccessControlRequest(
                    user_name=entry.user_name, permission_level=direct_level
                )
            )
        elif entry.group_name is not None:
            merged.append(
                iam.AccessControlRequest(
                    group_name=entry.group_name, permission_level=direct_level
                )
            )
        elif entry.service_principal_name is not None:
            merged.append(
                iam.AccessControlRequest(
                    service_principal_name=entry.service_principal_name,
                    permission_level=direct_level,
                )
            )

    # Apply the managed groups. NO_PERMISSIONS => omit => revoke.
    for group_name, level in group_levels.items():
        if level == PermissionLevel.NO_PERMISSIONS.value:
            continue
        merged.append(
            iam.AccessControlRequest(
                group_name=group_name,
                permission_level=iam.PermissionLevel(level),
            )
        )

    ws.permissions.set(
        request_object_type=resource_type,
        request_object_id=resource_id,
        access_control_list=merged,
    )


def apply_user_acl(
    ws: WorkspaceClient,
    resource_type: str,
    resource_id: str,
    user_levels: dict[str, str],
) -> None:
    """Merge-apply per-user ACL entries onto a single resource.

    Mirrors ``apply_group_acl`` but operates on ``user_name`` principals.
    Only the user_names listed in ``user_levels`` are touched; all other
    principals (groups, other users, owners) are preserved unchanged.
    NO_PERMISSIONS => the user's direct entry is removed (revoke).
    """
    current = ws.permissions.get(
        request_object_type=resource_type, request_object_id=resource_id
    )

    merged: list[iam.AccessControlRequest] = []
    for entry in current.access_control_list or []:
        direct_level: iam.PermissionLevel | None = None
        for p in entry.all_permissions or []:
            if p.permission_level and not p.inherited:
                direct_level = p.permission_level
                break
        if direct_level is None:
            continue

        # Managed user_names are (re)applied below; drop their existing entry.
        if entry.user_name is not None and entry.user_name in user_levels:
            continue

        if entry.user_name is not None:
            merged.append(
                iam.AccessControlRequest(
                    user_name=entry.user_name, permission_level=direct_level
                )
            )
        elif entry.group_name is not None:
            merged.append(
                iam.AccessControlRequest(
                    group_name=entry.group_name, permission_level=direct_level
                )
            )
        elif entry.service_principal_name is not None:
            merged.append(
                iam.AccessControlRequest(
                    service_principal_name=entry.service_principal_name,
                    permission_level=direct_level,
                )
            )

    for user_name, level in user_levels.items():
        if level == PermissionLevel.NO_PERMISSIONS.value:
            continue
        merged.append(
            iam.AccessControlRequest(
                user_name=user_name,
                permission_level=iam.PermissionLevel(level),
            )
        )

    ws.permissions.set(
        request_object_type=resource_type,
        request_object_id=resource_id,
        access_control_list=merged,
    )


def list_resources(
    ws: WorkspaceClient, resource_type: str
) -> list[ResourceItemOut]:
    """Route to the correct SDK call based on resource type and return normalized items."""
    try:
        match resource_type:
            case ResourceType.CLUSTERS:
                return _list_clusters(ws)
            case ResourceType.CLUSTER_POLICIES:
                return _list_cluster_policies(ws)
            case ResourceType.INSTANCE_POOLS:
                return _list_instance_pools(ws)
            case ResourceType.JOBS:
                return _list_jobs(ws)
            case ResourceType.PIPELINES:
                return _list_pipelines(ws)
            case ResourceType.EXPERIMENTS:
                return _list_experiments(ws)
            case ResourceType.REGISTERED_MODELS:
                return _list_registered_models(ws)
            case ResourceType.REPOS:
                return _list_repos(ws)
            case ResourceType.SERVING_ENDPOINTS:
                return _list_serving_endpoints(ws)
            case ResourceType.WAREHOUSES:
                return _list_warehouses(ws)
            case ResourceType.DASHBOARDS:
                return _list_dashboards(ws)
            case _:
                logger.warning(f"Unsupported resource type for listing: {resource_type}")
                return []
    except Exception as e:
        logger.error(f"Error listing resources of type {resource_type}: {e}")
        return []


def _list_clusters(ws: WorkspaceClient) -> list[ResourceItemOut]:
    items = []
    for c in ws.clusters.list():
        items.append(
            ResourceItemOut(
                id=str(c.cluster_id),
                name=c.cluster_name or f"Cluster {c.cluster_id}",
                resource_type=ResourceType.CLUSTERS,
            )
        )
    return items


def _list_cluster_policies(ws: WorkspaceClient) -> list[ResourceItemOut]:
    items = []
    for p in ws.cluster_policies.list():
        items.append(
            ResourceItemOut(
                id=str(p.policy_id),
                name=p.name or f"Policy {p.policy_id}",
                resource_type=ResourceType.CLUSTER_POLICIES,
            )
        )
    return items


def _list_instance_pools(ws: WorkspaceClient) -> list[ResourceItemOut]:
    items = []
    for p in ws.instance_pools.list():
        items.append(
            ResourceItemOut(
                id=str(p.instance_pool_id),
                name=p.instance_pool_name or f"Pool {p.instance_pool_id}",
                resource_type=ResourceType.INSTANCE_POOLS,
            )
        )
    return items


def _list_jobs(ws: WorkspaceClient) -> list[ResourceItemOut]:
    items = []
    for j in ws.jobs.list():
        items.append(
            ResourceItemOut(
                id=str(j.job_id),
                name=j.settings.name if j.settings and j.settings.name else f"Job {j.job_id}",
                resource_type=ResourceType.JOBS,
            )
        )
    return items


def _list_pipelines(ws: WorkspaceClient) -> list[ResourceItemOut]:
    items = []
    for p in ws.pipelines.list_pipelines():
        items.append(
            ResourceItemOut(
                id=str(p.pipeline_id),
                name=p.name or f"Pipeline {p.pipeline_id}",
                resource_type=ResourceType.PIPELINES,
            )
        )
    return items


def _list_experiments(ws: WorkspaceClient) -> list[ResourceItemOut]:
    items = []
    for e in ws.experiments.list_experiments():
        # MLflow tags every experiment with its kind. A NOTEBOOK-backed experiment
        # is a workspace notebook object under the hood: its `experiment_id` is a
        # notebook id, and it is permissioned via that notebook, NOT as a workspace
        # experiment object. `permissions.set(request_object_type="experiments", …)`
        # rejects such an id with "Object <id> not a experiment". Only real
        # workspace/standalone experiments (MLFLOW_EXPERIMENT) are permissionable on
        # the experiments ACL API, so notebook-backed ones are excluded here — they
        # would otherwise make every Apply fail on them.
        tags = {t.key: t.value for t in (e.tags or [])}
        if tags.get(_EXPERIMENT_TYPE_TAG) == _NOTEBOOK_EXPERIMENT_TYPE:
            continue
        items.append(
            ResourceItemOut(
                id=str(e.experiment_id),
                name=e.name or f"Experiment {e.experiment_id}",
                resource_type=ResourceType.EXPERIMENTS,
            )
        )
    return items


def _list_registered_models(ws: WorkspaceClient) -> list[ResourceItemOut]:
    items = []
    try:
        for m in ws.model_registry.list_models():
            items.append(
                ResourceItemOut(
                    id=str(m.name),
                    name=m.name or "Unknown Model",
                    resource_type=ResourceType.REGISTERED_MODELS,
                )
            )
    except Exception as e:
        logger.warning(f"Could not list registered models: {e}")
    return items


def _list_repos(ws: WorkspaceClient) -> list[ResourceItemOut]:
    items = []
    for r in ws.repos.list():
        items.append(
            ResourceItemOut(
                id=str(r.id),
                name=r.path or f"Repo {r.id}",
                resource_type=ResourceType.REPOS,
            )
        )
    return items


def _list_serving_endpoints(ws: WorkspaceClient) -> list[ResourceItemOut]:
    items = []
    for s in ws.serving_endpoints.list():
        # The serving-endpoints permissions API keys on the endpoint's opaque ID,
        # NOT its human-readable name — passing the name yields "'<name>' is not a
        # valid Inference Endpoint ID". Use `id` for the id (used by every
        # permissions call) and keep `name` purely as the display label.
        #
        # System-managed Foundation Model (pay-per-token) endpoints — e.g.
        # `databricks-*` — carry no `id` and cannot have per-endpoint ACLs set,
        # so they are skipped rather than emitted with a bogus "None" id that
        # every permissions call would reject.
        if not s.id:
            continue
        items.append(
            ResourceItemOut(
                id=str(s.id),
                name=s.name or "Unknown Endpoint",
                resource_type=ResourceType.SERVING_ENDPOINTS,
            )
        )
    return items


def _list_warehouses(ws: WorkspaceClient) -> list[ResourceItemOut]:
    items = []
    for w_ in ws.warehouses.list():
        items.append(
            ResourceItemOut(
                id=str(w_.id),
                name=w_.name or f"Warehouse {w_.id}",
                resource_type=ResourceType.WAREHOUSES,
            )
        )
    return items


def _list_dashboards(ws: WorkspaceClient) -> list[ResourceItemOut]:
    items = []
    try:
        for d in ws.dashboards.list():
            items.append(
                ResourceItemOut(
                    id=str(d.id),
                    name=d.name or f"Dashboard {d.id}",
                    resource_type=ResourceType.DASHBOARDS,
                )
            )
    except Exception as e:
        logger.warning(f"Could not list dashboards: {e}")
    return items
