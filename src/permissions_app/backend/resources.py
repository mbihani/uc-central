"""
Helper functions to list workspace resources by type using the Databricks SDK.
Each function returns a list of ResourceItemOut for consistent handling.
"""

from databricks.sdk import WorkspaceClient

from .logger import logger
from .models import ResourceItemOut, ResourceType


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
        items.append(
            ResourceItemOut(
                id=str(s.name),
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
