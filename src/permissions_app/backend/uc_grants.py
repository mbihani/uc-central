"""Unity Catalog grant helpers (``ws.grants``) for Genie access bundles.

Only three operations are needed, and all of them are TARGETED at a single
principal + privilege so nothing else on a securable is ever disturbed:

  * ``effective_privileges`` — what a principal can already do on a securable
    (direct OR inherited from a parent / a group it belongs to), via
    ``grants.get_effective``. Used to skip grants that are already in force.
  * ``add_privileges`` — ``grants.update(add=[...])``. UC's add is itself
    idempotent (re-adding an existing privilege is a no-op).
  * ``remove_privileges`` — ``grants.update(remove=[...])``. Removes ONLY the
    named privileges for the named principal.

Permission failures (the app service principal lacks MANAGE / ownership on the
securable) are raised as ``UCGrantError`` with ``permission_denied=True`` and a
message that says exactly which securable needs which privilege, so callers can
surface it instead of swallowing it.
"""

from dataclasses import dataclass

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service import catalog

# UC securable types used by bundles (the grants API path segment).
CATALOG = "catalog"
SCHEMA = "schema"
TABLE = "table"
VOLUME = "volume"

# Privileges that imply every other privilege on the securable.
_ALL = catalog.Privilege.ALL_PRIVILEGES.value


@dataclass(frozen=True)
class EffectivePrivilege:
    privilege: str
    # None for a direct grant; otherwise the securable it is inherited from.
    inherited_from: str | None
    # The principal the privilege is attached to (the group itself, or a group
    # it is a member of, e.g. "account users").
    via_principal: str


class UCGrantError(Exception):
    def __init__(
        self, message: str, *, permission_denied: bool = False, not_found: bool = False
    ):
        super().__init__(message)
        self.permission_denied = permission_denied
        self.not_found = not_found


def _describe(exc: Exception, action: str, securable_type: str, full_name: str) -> UCGrantError:
    if isinstance(exc, PermissionDenied):
        return UCGrantError(
            f"Permission denied while trying to {action} on {securable_type} "
            f"'{full_name}': the app service principal needs MANAGE (or "
            f"ownership) on this {securable_type} or a parent catalog/schema.",
            permission_denied=True,
        )
    if isinstance(exc, NotFound):
        return UCGrantError(
            f"{securable_type} '{full_name}' was not found (or is not visible to "
            f"the app service principal) while trying to {action}.",
            not_found=True,
        )
    return UCGrantError(
        f"Failed to {action} on {securable_type} '{full_name}' "
        f"({type(exc).__name__})."
    )


def effective_privileges(
    ws: WorkspaceClient, securable_type: str, full_name: str, principal: str
) -> list[EffectivePrivilege]:
    """Every privilege ``principal`` effectively holds on the securable."""
    try:
        resp = ws.grants.get_effective(
            securable_type=securable_type, full_name=full_name, principal=principal
        )
    except Exception as e:
        raise _describe(e, "read grants", securable_type, full_name) from e
    out: list[EffectivePrivilege] = []
    for assignment in resp.privilege_assignments or []:
        for p in assignment.privileges or []:
            if p.privilege is None:
                continue
            out.append(
                EffectivePrivilege(
                    privilege=p.privilege.value,
                    inherited_from=p.inherited_from_name,
                    via_principal=assignment.principal or "",
                )
            )
    return out


def find_effective(
    privileges: list[EffectivePrivilege], wanted: str
) -> EffectivePrivilege | None:
    """The effective privilege that satisfies ``wanted`` (exact or ALL_PRIVILEGES)."""
    for p in privileges:
        if p.privilege == wanted:
            return p
    for p in privileges:
        if p.privilege == _ALL:
            return p
    return None


def add_privileges(
    ws: WorkspaceClient,
    securable_type: str,
    full_name: str,
    principal: str,
    privileges: list[str],
) -> None:
    try:
        ws.grants.update(
            securable_type=securable_type,
            full_name=full_name,
            changes=[
                catalog.PermissionsChange(
                    principal=principal,
                    add=[catalog.Privilege(p) for p in privileges],
                )
            ],
        )
    except Exception as e:
        raise _describe(e, "grant " + ", ".join(privileges), securable_type, full_name) from e


def remove_privileges(
    ws: WorkspaceClient,
    securable_type: str,
    full_name: str,
    principal: str,
    privileges: list[str],
) -> None:
    try:
        ws.grants.update(
            securable_type=securable_type,
            full_name=full_name,
            changes=[
                catalog.PermissionsChange(
                    principal=principal,
                    remove=[catalog.Privilege(p) for p in privileges],
                )
            ],
        )
    except Exception as e:
        raise _describe(e, "revoke " + ", ".join(privileges), securable_type, full_name) from e
