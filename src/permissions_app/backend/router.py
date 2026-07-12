from typing import Annotated

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import iam
from databricks.sdk.service.iam import User as DatabricksUserOut
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlmodel import Session, select

from .._metadata import api_prefix
from .defaults import DEFAULT_PERMISSIONS_MATRIX
from .dependencies import AdminRequired, SessionDep, _is_workspace_admin, get_obo_ws, get_session
from .logger import logger
from .models import (
    AddMemberIn,
    ApplyAllResultOut,
    ApplyResultOut,
    DashboardStatsOut,
    GroupMemberOut,
    GroupOut,
    IsAdminOut,
    PersonaDefinition,
    PersonaDefinitionIn,
    PersonaDefinitionOut,
    PersonaDefinitionUpdateIn,
    PersonaGroupMapping,
    PersonaGroupMappingIn,
    PersonaGroupMappingOut,
    PersonaOut,
    PersonaUserMapping,
    PersonaUserMappingIn,
    PersonaUserMappingOut,
    PermissionConflictDetail,
    PermissionConflictsOut,
    PermissionLevel,
    PermissionLevelOut,
    PermissionMatrixCell,
    PermissionMatrixOut,
    PermissionTemplate,
    PermissionTemplateOut,
    DEFAULT_PERSONA_DESCRIPTIONS,
    DEFAULT_PERSONA_LABELS,
    RESOURCE_PERMISSION_LEVELS,
    RESOURCE_TYPE_LABELS,
    ResourceItemOut,
    ResourcePermissionOut,
    ResourcePermissionsOut,
    ResourceType,
    SetPermissionIn,
    UserOut,
    UserPermissionConflict,
    VersionOut,
    get_higher_permission,
    get_permission_rank,
)
from .resources import list_resources

api = APIRouter(prefix=api_prefix)


# ─── Version ──────────────────────────────────────────────────────────────────


@api.get("/version", response_model=VersionOut, operation_id="version")
async def version():
    return VersionOut.from_metadata()


@api.get("/current-user", response_model=DatabricksUserOut, operation_id="currentUser")
def me(obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)]):
    return obo_ws.current_user.me()


@api.get("/current-user/is-admin", response_model=IsAdminOut, operation_id="checkIsAdmin")
def check_is_admin(obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)]):
    """Check if the current user is a workspace admin."""
    return IsAdminOut(is_admin=_is_workspace_admin(obo_ws))


# ─── Dashboard Stats ──────────────────────────────────────────────────────────


@api.get(
    "/dashboard/stats",
    response_model=DashboardStatsOut,
    operation_id="getDashboardStats",
)
def get_dashboard_stats(
    session: SessionDep,
    token: Annotated[str | None, Header(alias="X-Forwarded-Access-Token")] = None,
):
    """Get overview stats for the dashboard.

    Gracefully degrades when Databricks credentials are unavailable:
    SDK-dependent stats (groups, users) return 0, DB-backed stats still work.
    """
    total_groups = 0
    total_users = 0
    unassigned = 0
    users: list = []

    # Try to get SDK-dependent stats, but don't fail the whole endpoint
    try:
        # Use app service-principal creds (see get_obo_ws rationale); the forwarded
        # OBO user token is scope-restricted and cannot list groups/users.
        ws = WorkspaceClient()

        groups = list(ws.groups.list())
        total_groups = len(groups)

        users = list(ws.users.list(attributes="id,userName,displayName,groups"))
        total_users = len(users)
    except Exception as e:
        logger.warning(f"Could not fetch workspace groups/users for dashboard: {e}")
        # SDK not available — zero out SDK-dependent stats and continue

    # Count persona-mapped groups from DB (always works)
    mappings = session.exec(select(PersonaGroupMapping)).all()
    mapped_group_ids = {m.group_id for m in mappings}
    mapped_groups = len(mapped_group_ids)

    # Count personas with at least one group
    personas_with_groups = len({m.persona for m in mappings})

    # Count unassigned users (only if SDK fetched users successfully)
    if total_users > 0:
        try:
            for u in users:
                user_group_ids = set()
                if u.groups:
                    for g in u.groups:
                        if g.value:
                            user_group_ids.add(g.value)
                if not user_group_ids.intersection(mapped_group_ids):
                    unassigned += 1
        except Exception:
            pass

    return DashboardStatsOut(
        total_groups=total_groups,
        total_users=total_users,
        mapped_groups=mapped_groups,
        unassigned_users=unassigned,
        personas_with_groups=personas_with_groups,
    )


# ─── Groups (from Databricks workspace) ──────────────────────────────────────


@api.get("/groups", response_model=list[GroupOut], operation_id="listGroups")
def list_workspace_groups(
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
):
    """List all workspace groups with their members."""
    results = []
    for g in obo_ws.groups.list():
        members = []
        if g.members:
            for m in g.members:
                members.append(
                    GroupMemberOut(
                        user_id=m.value or "",
                        display_name=m.display or None,
                    )
                )
        results.append(
            GroupOut(
                id=g.id or "",
                display_name=g.display_name or "",
                member_count=len(members),
                members=members,
            )
        )
    return results


@api.get(
    "/groups/{group_id}",
    response_model=GroupOut,
    operation_id="getGroup",
)
def get_group(
    group_id: str,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
):
    """Get a single group with members."""
    g = obo_ws.groups.get(id=group_id)
    members = []
    if g.members:
        for m in g.members:
            members.append(
                GroupMemberOut(
                    user_id=m.value or "",
                    display_name=m.display or None,
                )
            )
    return GroupOut(
        id=g.id or "",
        display_name=g.display_name or "",
        member_count=len(members),
        members=members,
    )


@api.post(
    "/groups/{group_id}/members",
    response_model=dict,
    operation_id="addGroupMember",
)
def add_group_member(
    group_id: str,
    body: AddMemberIn,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    _admin: AdminRequired = True,
):
    """Add a user to a group."""
    obo_ws.groups.patch(
        id=group_id,
        operations=[
            iam.Patch(
                op=iam.PatchOp.ADD,
                value={"members": [{"value": body.user_id}]},
            )
        ],
        schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
    )
    return {"status": "ok", "message": f"User {body.user_id} added to group {group_id}"}


# ─── Users (from Databricks workspace) ───────────────────────────────────────


@api.get("/users", response_model=list[UserOut], operation_id="listUsers")
def list_workspace_users(
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
):
    """List all workspace users."""
    results = []
    for u in obo_ws.users.list(attributes="id,userName,displayName,active,groups"):
        group_names = []
        if u.groups:
            for g in u.groups:
                if g.display:
                    group_names.append(g.display)
        results.append(
            UserOut(
                id=u.id or "",
                user_name=u.user_name,
                display_name=u.display_name,
                active=u.active,
                groups=group_names,
            )
        )
    return results


@api.get(
    "/users/unassigned",
    response_model=list[UserOut],
    operation_id="listUnassignedUsers",
)
def list_unassigned_users(
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
):
    """List users who are not in any persona-mapped group."""
    # Get mapped group names
    mappings = session.exec(select(PersonaGroupMapping)).all()
    mapped_group_names = {m.group_name for m in mappings}

    results = []
    for u in obo_ws.users.list(attributes="id,userName,displayName,active,groups"):
        user_group_names = set()
        group_display_names = []
        if u.groups:
            for g in u.groups:
                if g.display:
                    user_group_names.add(g.display)
                    group_display_names.append(g.display)

        # User is unassigned if none of their groups is mapped to a persona
        if not user_group_names.intersection(mapped_group_names):
            results.append(
                UserOut(
                    id=u.id or "",
                    user_name=u.user_name,
                    display_name=u.display_name,
                    active=u.active,
                    groups=group_display_names,
                )
            )
    return results


# ─── Personas (DB-backed) ────────────────────────────────────────────────────


def _seed_persona_definitions(session: Session):
    """Seed default persona definitions if the table is empty."""
    from .models import DefaultPersona

    for p in DefaultPersona:
        existing = session.exec(
            select(PersonaDefinition).where(PersonaDefinition.key == p.value)
        ).first()
        if not existing:
            session.add(
                PersonaDefinition(
                    key=p.value,
                    label=DEFAULT_PERSONA_LABELS.get(p.value, p.value),
                    description=DEFAULT_PERSONA_DESCRIPTIONS.get(p.value, ""),
                    is_default=True,
                )
            )
    session.commit()
    logger.info("Seeded default persona definitions")


@api.get(
    "/personas",
    response_model=list[PersonaOut],
    operation_id="listPersonas",
)
def list_personas(session: SessionDep):
    """List all personas (both default and custom) with their mapped groups and users."""
    # Seed defaults if empty
    definitions = session.exec(select(PersonaDefinition)).all()
    if not definitions:
        _seed_persona_definitions(session)
        definitions = session.exec(select(PersonaDefinition)).all()

    mappings = session.exec(select(PersonaGroupMapping)).all()
    user_mappings = session.exec(select(PersonaUserMapping)).all()

    # Group mappings by persona key
    persona_groups: dict[str, list[PersonaGroupMappingOut]] = {}
    for m in mappings:
        if m.persona not in persona_groups:
            persona_groups[m.persona] = []
        persona_groups[m.persona].append(
            PersonaGroupMappingOut(
                id=m.id,  # type: ignore
                group_id=m.group_id,
                group_name=m.group_name,
                persona=m.persona,
            )
        )

    # User mappings by persona key
    persona_users: dict[str, list[PersonaUserMappingOut]] = {}
    for um in user_mappings:
        if um.persona not in persona_users:
            persona_users[um.persona] = []
        persona_users[um.persona].append(
            PersonaUserMappingOut(
                id=um.id,  # type: ignore
                user_id=um.user_id,
                user_name=um.user_name,
                display_name=um.display_name,
                persona=um.persona,
            )
        )

    results = []
    for defn in definitions:
        results.append(
            PersonaOut(
                persona=defn.key,
                label=defn.label,
                description=defn.description,
                is_default=defn.is_default,
                groups=persona_groups.get(defn.key, []),
                users=persona_users.get(defn.key, []),
            )
        )
    return results


@api.post(
    "/personas",
    response_model=PersonaDefinitionOut,
    operation_id="createPersona",
)
def create_persona(body: PersonaDefinitionIn, session: SessionDep, _admin: AdminRequired = True):
    """Create a new custom persona."""
    # Validate key format (lowercase, underscores, no spaces)
    import re

    if not re.match(r"^[a-z][a-z0-9_]*$", body.key):
        raise HTTPException(
            status_code=400,
            detail="Persona key must start with a letter and contain only lowercase letters, numbers, and underscores.",
        )

    # Check uniqueness
    existing = session.exec(
        select(PersonaDefinition).where(PersonaDefinition.key == body.key)
    ).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"A persona with key '{body.key}' already exists.",
        )

    defn = PersonaDefinition(
        key=body.key,
        label=body.label,
        description=body.description,
        is_default=False,
    )
    session.add(defn)
    session.commit()
    session.refresh(defn)

    # Seed NO_PERMISSIONS for all resource types so the persona shows in the matrix
    for rt in ResourceType:
        session.add(
            PermissionTemplate(
                persona=defn.key,
                resource_type=rt.value,
                permission_level=PermissionLevel.NO_PERMISSIONS.value,
            )
        )
    session.commit()

    return PersonaDefinitionOut(
        id=defn.id,  # type: ignore
        key=defn.key,
        label=defn.label,
        description=defn.description,
        is_default=defn.is_default,
    )


@api.put(
    "/personas/{persona_key}",
    response_model=PersonaDefinitionOut,
    operation_id="updatePersona",
)
def update_persona(persona_key: str, body: PersonaDefinitionUpdateIn, session: SessionDep, _admin: AdminRequired = True):
    """Update a persona's label and/or description."""
    defn = session.exec(
        select(PersonaDefinition).where(PersonaDefinition.key == persona_key)
    ).first()
    if not defn:
        raise HTTPException(status_code=404, detail="Persona not found")

    if body.label is not None:
        defn.label = body.label
    if body.description is not None:
        defn.description = body.description

    session.add(defn)
    session.commit()
    session.refresh(defn)

    return PersonaDefinitionOut(
        id=defn.id,  # type: ignore
        key=defn.key,
        label=defn.label,
        description=defn.description,
        is_default=defn.is_default,
    )


@api.delete(
    "/personas/{persona_key}",
    response_model=dict,
    operation_id="deletePersona",
)
def delete_persona(persona_key: str, session: SessionDep, _admin: AdminRequired = True):
    """Delete a persona and all its associated mappings and permission templates."""
    defn = session.exec(
        select(PersonaDefinition).where(PersonaDefinition.key == persona_key)
    ).first()
    if not defn:
        raise HTTPException(status_code=404, detail="Persona not found")

    # Delete associated group mappings
    mappings = session.exec(
        select(PersonaGroupMapping).where(PersonaGroupMapping.persona == persona_key)
    ).all()
    for m in mappings:
        session.delete(m)

    # Delete associated user mappings
    user_maps = session.exec(
        select(PersonaUserMapping).where(PersonaUserMapping.persona == persona_key)
    ).all()
    for um in user_maps:
        session.delete(um)

    # Delete associated permission templates
    templates = session.exec(
        select(PermissionTemplate).where(PermissionTemplate.persona == persona_key)
    ).all()
    for t in templates:
        session.delete(t)

    session.delete(defn)
    session.commit()

    return {"status": "ok", "message": f"Persona '{persona_key}' deleted"}


@api.post(
    "/personas/mappings",
    response_model=PersonaGroupMappingOut,
    operation_id="createPersonaMapping",
)
def create_persona_mapping(
    body: PersonaGroupMappingIn,
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """Map a Databricks group to a persona."""
    # Validate persona exists
    persona_def = session.exec(
        select(PersonaDefinition).where(PersonaDefinition.key == body.persona)
    ).first()
    if not persona_def:
        raise HTTPException(
            status_code=400,
            detail=f"Persona '{body.persona}' does not exist.",
        )

    # Check if this group is already mapped
    existing = session.exec(
        select(PersonaGroupMapping).where(
            PersonaGroupMapping.group_id == body.group_id
        )
    ).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Group {body.group_name} is already mapped to persona {existing.persona}",
        )

    mapping = PersonaGroupMapping(
        group_id=body.group_id,
        group_name=body.group_name,
        persona=body.persona,
    )
    session.add(mapping)
    session.commit()
    session.refresh(mapping)

    return PersonaGroupMappingOut(
        id=mapping.id,  # type: ignore
        group_id=mapping.group_id,
        group_name=mapping.group_name,
        persona=mapping.persona,
    )


@api.delete(
    "/personas/mappings/{mapping_id}",
    response_model=dict,
    operation_id="deletePersonaMapping",
)
def delete_persona_mapping(mapping_id: int, session: SessionDep, _admin: AdminRequired = True):
    """Remove a persona-group mapping."""
    mapping = session.get(PersonaGroupMapping, mapping_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")
    session.delete(mapping)
    session.commit()
    return {"status": "ok", "message": "Mapping deleted"}


# ─── Persona User Mappings ────────────────────────────────────────────────────


@api.post(
    "/personas/user-mappings",
    response_model=PersonaUserMappingOut,
    operation_id="createPersonaUserMapping",
)
def create_persona_user_mapping(
    body: PersonaUserMappingIn,
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """Map a Databricks user directly to a persona."""
    # Validate persona exists
    persona_def = session.exec(
        select(PersonaDefinition).where(PersonaDefinition.key == body.persona)
    ).first()
    if not persona_def:
        raise HTTPException(
            status_code=400,
            detail=f"Persona '{body.persona}' does not exist.",
        )

    # Check if this user is already directly mapped to any persona
    existing = session.exec(
        select(PersonaUserMapping).where(
            PersonaUserMapping.user_id == body.user_id
        )
    ).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"User {body.user_name or body.user_id} is already mapped to persona '{existing.persona}'.",
        )

    mapping = PersonaUserMapping(
        user_id=body.user_id,
        user_name=body.user_name,
        display_name=body.display_name,
        persona=body.persona,
    )
    session.add(mapping)
    session.commit()
    session.refresh(mapping)

    return PersonaUserMappingOut(
        id=mapping.id,  # type: ignore
        user_id=mapping.user_id,
        user_name=mapping.user_name,
        display_name=mapping.display_name,
        persona=mapping.persona,
    )


@api.delete(
    "/personas/user-mappings/{mapping_id}",
    response_model=dict,
    operation_id="deletePersonaUserMapping",
)
def delete_persona_user_mapping(mapping_id: int, session: SessionDep, _admin: AdminRequired = True):
    """Remove a persona-user mapping."""
    mapping = session.get(PersonaUserMapping, mapping_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="User mapping not found")
    session.delete(mapping)
    session.commit()
    return {"status": "ok", "message": "User mapping deleted"}


# ─── Permissions Matrix (DB-backed) ──────────────────────────────────────────


@api.get(
    "/permissions/matrix",
    response_model=PermissionMatrixOut,
    operation_id="getPermissionMatrix",
)
def get_permission_matrix(session: SessionDep):
    """Get the full permissions matrix (seeded from defaults on first call)."""
    # Ensure persona definitions are seeded
    definitions = session.exec(select(PersonaDefinition)).all()
    if not definitions:
        _seed_persona_definitions(session)
        definitions = session.exec(select(PersonaDefinition)).all()

    templates = session.exec(select(PermissionTemplate)).all()

    # Seed defaults if empty
    if not templates:
        _seed_permission_templates(session)
        templates = session.exec(select(PermissionTemplate)).all()

    matrix = [
        PermissionTemplateOut(
            id=t.id,  # type: ignore
            persona=t.persona,
            resource_type=t.resource_type,
            permission_level=t.permission_level,
        )
        for t in templates
    ]

    # Build persona list and labels from DB definitions
    persona_keys = [d.key for d in definitions]
    persona_labels = {d.key: d.label for d in definitions}

    return PermissionMatrixOut(
        matrix=matrix,
        personas=persona_keys,
        resource_types=[r.value for r in ResourceType],
        persona_labels=persona_labels,
        resource_type_labels={r.value: RESOURCE_TYPE_LABELS[r] for r in ResourceType},
        allowed_permission_levels={
            r.value: [pl.value for pl in RESOURCE_PERMISSION_LEVELS[r]]
            for r in ResourceType
        },
    )


@api.put(
    "/permissions/matrix",
    response_model=PermissionTemplateOut,
    operation_id="updatePermissionMatrix",
)
def update_permission_matrix(body: PermissionMatrixCell, session: SessionDep, _admin: AdminRequired = True):
    """Update a single cell in the permissions matrix."""
    template = session.exec(
        select(PermissionTemplate).where(
            PermissionTemplate.persona == body.persona,
            PermissionTemplate.resource_type == body.resource_type.value,
        )
    ).first()

    if not template:
        # Create if it doesn't exist
        template = PermissionTemplate(
            persona=body.persona,
            resource_type=body.resource_type.value,
            permission_level=body.permission_level.value,
        )
        session.add(template)
    else:
        template.permission_level = body.permission_level.value

    session.commit()
    session.refresh(template)

    return PermissionTemplateOut(
        id=template.id,  # type: ignore
        persona=template.persona,
        resource_type=template.resource_type,
        permission_level=template.permission_level,
    )


@api.get(
    "/permissions/conflicts",
    response_model=PermissionConflictsOut,
    operation_id="checkPermissionConflicts",
)
def check_permission_conflicts(
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
):
    """Detect permission conflicts for users who belong to multiple persona groups.

    A conflict occurs when a user is in groups mapped to different personas,
    and those personas have different permission levels for the same resource type.
    Conflicts are resolved by taking the highest permission level.
    """
    # Build persona key -> label lookup from DB
    definitions = session.exec(select(PersonaDefinition)).all()
    persona_label_map: dict[str, str] = {d.key: d.label for d in definitions}

    # 1. Get all persona-group mappings
    mappings = session.exec(select(PersonaGroupMapping)).all()
    if not mappings:
        return PermissionConflictsOut(
            total_users_checked=0, users_with_conflicts=0, conflicts=[]
        )

    # Build group_name -> persona lookup
    group_name_to_persona: dict[str, str] = {}
    for m in mappings:
        group_name_to_persona[m.group_name] = m.persona

    mapped_group_names = set(group_name_to_persona.keys())

    # 2. Get the full permission template matrix
    templates = session.exec(select(PermissionTemplate)).all()
    if not templates:
        return PermissionConflictsOut(
            total_users_checked=0, users_with_conflicts=0, conflicts=[]
        )

    # Build persona+resource_type -> permission_level lookup
    template_map: dict[str, str] = {}
    for t in templates:
        template_map[f"{t.persona}::{t.resource_type}"] = t.permission_level

    # 3. Fetch all workspace users with group memberships
    users_with_multiple_personas: list[UserPermissionConflict] = []
    total_users_checked = 0

    try:
        all_users = list(
            obo_ws.users.list(attributes="id,userName,displayName,groups")
        )
    except Exception as e:
        logger.warning(f"Could not fetch users for conflict check: {e}")
        return PermissionConflictsOut(
            total_users_checked=0, users_with_conflicts=0, conflicts=[]
        )

    for user in all_users:
        # Find which mapped groups this user belongs to
        user_group_names: set[str] = set()
        if user.groups:
            for g in user.groups:
                if g.display:
                    user_group_names.add(g.display)

        user_mapped_groups = user_group_names.intersection(mapped_group_names)
        if not user_mapped_groups:
            continue

        total_users_checked += 1

        # Find which personas this user has through their groups
        user_personas: set[str] = set()
        for gname in user_mapped_groups:
            persona = group_name_to_persona.get(gname)
            if persona:
                user_personas.add(persona)

        if len(user_personas) < 2:
            continue  # No conflict possible with a single persona

        # 4. Check for conflicts across resource types
        conflicts: list[PermissionConflictDetail] = []
        for rt in ResourceType:
            # Gather permission levels from each persona for this resource type
            persona_levels: dict[str, str] = {}
            levels_set: set[str] = set()
            for persona_val in user_personas:
                level = template_map.get(
                    f"{persona_val}::{rt.value}",
                    PermissionLevel.NO_PERMISSIONS.value,
                )
                persona_label = persona_label_map.get(persona_val, persona_val)
                persona_levels[persona_label] = level
                levels_set.add(level)

            # If there are different permission levels, it's a conflict
            if len(levels_set) > 1:
                effective = get_higher_permission(
                    rt.value, *list(levels_set)
                )
                conflicts.append(
                    PermissionConflictDetail(
                        resource_type=rt.value,
                        resource_type_label=RESOURCE_TYPE_LABELS.get(
                            rt, rt.value
                        ),
                        persona_levels=persona_levels,
                        effective_level=effective,
                    )
                )

        if conflicts:
            persona_labels_list = [
                persona_label_map.get(p, p) for p in user_personas
            ]
            users_with_multiple_personas.append(
                UserPermissionConflict(
                    user_id=user.id or "",
                    user_name=user.user_name,
                    display_name=user.display_name,
                    personas=sorted(persona_labels_list),
                    conflict_count=len(conflicts),
                    conflicts=conflicts,
                )
            )

    return PermissionConflictsOut(
        total_users_checked=total_users_checked,
        users_with_conflicts=len(users_with_multiple_personas),
        conflicts=users_with_multiple_personas,
    )


@api.post(
    "/permissions/apply/{persona}",
    response_model=ApplyAllResultOut,
    operation_id="applyPermissions",
)
def apply_permissions(
    persona: str,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    session: SessionDep,
    _admin: AdminRequired = True,
):
    """Apply the persona's template permissions to all resources in the workspace."""
    # Validate persona exists
    persona_def = session.exec(
        select(PersonaDefinition).where(PersonaDefinition.key == persona)
    ).first()
    if not persona_def:
        raise HTTPException(status_code=404, detail=f"Persona '{persona}' not found")

    # Get groups mapped to this persona
    mappings = session.exec(
        select(PersonaGroupMapping).where(
            PersonaGroupMapping.persona == persona
        )
    ).all()

    if not mappings:
        raise HTTPException(
            status_code=400,
            detail=f"No groups are mapped to persona {persona}. Map groups first.",
        )

    # Get the permission template for this persona
    templates = session.exec(
        select(PermissionTemplate).where(
            PermissionTemplate.persona == persona
        )
    ).all()

    if not templates:
        raise HTTPException(
            status_code=400,
            detail="Permission template not found. Load the matrix first.",
        )

    # Get ALL persona-group mappings to detect cross-persona conflicts
    all_mappings = session.exec(select(PersonaGroupMapping)).all()
    all_templates = session.exec(select(PermissionTemplate)).all()

    # Build a lookup: persona -> resource_type -> permission_level
    all_template_map: dict[str, dict[str, str]] = {}
    for t in all_templates:
        if t.persona not in all_template_map:
            all_template_map[t.persona] = {}
        all_template_map[t.persona][t.resource_type] = t.permission_level

    # Build group_name -> persona lookup for all mappings
    group_to_persona: dict[str, str] = {}
    for m in all_mappings:
        group_to_persona[m.group_name] = m.persona

    # Fetch members of each group mapped to this persona to detect
    # cross-persona membership and resolve to highest permission
    group_members: dict[str, set[str]] = {}  # group_name -> set of user group names
    try:
        for mapping in mappings:
            try:
                g = obo_ws.groups.get(id=mapping.group_id)
                if g.members:
                    for member in g.members:
                        if member.value:
                            if mapping.group_name not in group_members:
                                group_members[mapping.group_name] = set()
                            group_members[mapping.group_name].add(member.value)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Could not fetch group members for conflict resolution: {e}")

    # For each user in this persona's groups, find all their other persona memberships
    # to compute the effective (highest) permission per resource type per group
    user_other_personas: dict[str, set[str]] = {}  # user_id -> set of other persona values
    if group_members:
        try:
            # Get all member user IDs from our persona's groups
            all_member_ids: set[str] = set()
            for members in group_members.values():
                all_member_ids.update(members)

            # For each user, check if they're in groups mapped to other personas
            for user_id in all_member_ids:
                try:
                    user_detail = obo_ws.users.get(id=user_id)
                    if user_detail.groups:
                        for ug in user_detail.groups:
                            if ug.display and ug.display in group_to_persona:
                                other_persona = group_to_persona[ug.display]
                                if other_persona != persona:
                                    if user_id not in user_other_personas:
                                        user_other_personas[user_id] = set()
                                    user_other_personas[user_id].add(
                                        other_persona
                                    )
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Could not resolve cross-persona conflicts: {e}")

    # Determine all personas that overlap with this persona's users
    overlapping_personas: set[str] = set()
    for other_personas in user_other_personas.values():
        overlapping_personas.update(other_personas)

    results = []
    total_updated = 0
    total_errors = 0

    for template in templates:
        if template.permission_level == PermissionLevel.NO_PERMISSIONS.value:
            results.append(
                ApplyResultOut(
                    persona=persona,
                    resource_type=template.resource_type,
                    resources_updated=0,
                    errors=[],
                )
            )
            continue

        # Resolve the effective permission level for this resource type
        # by taking the max across this persona and all overlapping personas
        effective_level = template.permission_level
        if overlapping_personas:
            candidate_levels = [template.permission_level]
            for other_persona in overlapping_personas:
                other_level = all_template_map.get(other_persona, {}).get(
                    template.resource_type,
                    PermissionLevel.NO_PERMISSIONS.value,
                )
                candidate_levels.append(other_level)
            effective_level = get_higher_permission(
                template.resource_type, *candidate_levels
            )

        if effective_level != template.permission_level:
            logger.info(
                f"Conflict resolved for {persona}/{template.resource_type}: "
                f"{template.permission_level} -> {effective_level} "
                f"(higher level from overlapping personas: {overlapping_personas})"
            )

        # List resources of this type
        resources = list_resources(obo_ws, template.resource_type)
        resource_errors: list[str] = []
        updated = 0

        for resource in resources:
            try:
                # Build ACL for all groups mapped to this persona
                # using the conflict-resolved effective permission level
                acl = []
                for mapping in mappings:
                    acl.append(
                        iam.AccessControlRequest(
                            group_name=mapping.group_name,
                            permission_level=iam.PermissionLevel(
                                effective_level
                            ),
                        )
                    )

                obo_ws.permissions.update(
                    request_object_type=template.resource_type,
                    request_object_id=resource.id,
                    access_control_list=acl,
                )
                updated += 1
            except Exception as e:
                error_msg = f"Failed to set permissions on {resource.name} ({resource.id}): {str(e)}"
                logger.error(error_msg)
                resource_errors.append(error_msg)

        total_updated += updated
        total_errors += len(resource_errors)
        results.append(
            ApplyResultOut(
                persona=persona,
                resource_type=template.resource_type,
                resources_updated=updated,
                errors=resource_errors,
            )
        )

    return ApplyAllResultOut(
        persona=persona,
        results=results,
        total_resources_updated=total_updated,
        total_errors=total_errors,
    )


# ─── Resources (from Databricks workspace) ───────────────────────────────────


@api.get(
    "/resources/{resource_type}",
    response_model=list[ResourceItemOut],
    operation_id="listResourcesByType",
)
def list_resources_by_type(
    resource_type: str,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
):
    """List all resources of a given type."""
    return list_resources(obo_ws, resource_type)


@api.get(
    "/resources/{resource_type}/{resource_id}/permissions",
    response_model=ResourcePermissionsOut,
    operation_id="getResourcePermissions",
)
def get_resource_permissions(
    resource_type: str,
    resource_id: str,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
):
    """Get current permissions on a specific resource."""
    try:
        perms = obo_ws.permissions.get(
            request_object_type=resource_type,
            request_object_id=resource_id,
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not get permissions: {str(e)}",
        )

    acl = []
    if perms.access_control_list:
        for entry in perms.access_control_list:
            all_permissions = []
            if entry.all_permissions:
                for p in entry.all_permissions:
                    all_permissions.append(
                        {
                            "permission_level": p.permission_level.value if p.permission_level else None,
                            "inherited": p.inherited or False,
                            "inherited_from_object": (
                                list(p.inherited_from_object)
                                if p.inherited_from_object
                                else []
                            ),
                        }
                    )
            acl.append(
                ResourcePermissionOut(
                    group_name=entry.group_name,
                    user_name=entry.user_name,
                    all_permissions=all_permissions,
                )
            )

    return ResourcePermissionsOut(
        resource_id=resource_id,
        resource_type=resource_type,
        access_control_list=acl,
    )


@api.get(
    "/resources/{resource_type}/{resource_id}/permission-levels",
    response_model=list[PermissionLevelOut],
    operation_id="getPermissionLevels",
)
def get_permission_levels(
    resource_type: str,
    resource_id: str,
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
):
    """Get available permission levels for a resource."""
    try:
        levels = obo_ws.permissions.get_permission_levels(
            request_object_type=resource_type,
            request_object_id=resource_id,
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not get permission levels: {str(e)}",
        )

    results = []
    if levels.permission_levels:
        for lvl in levels.permission_levels:
            results.append(
                PermissionLevelOut(
                    permission_level=lvl.permission_level.value if lvl.permission_level else "",
                    description=lvl.description,
                )
            )
    return results


@api.put(
    "/resources/{resource_type}/{resource_id}/permissions",
    response_model=dict,
    operation_id="setResourcePermissions",
)
def set_resource_permissions(
    resource_type: str,
    resource_id: str,
    body: list[SetPermissionIn],
    obo_ws: Annotated[WorkspaceClient, Depends(get_obo_ws)],
    _admin: AdminRequired = True,
):
    """Set permissions on a specific resource for given groups."""
    acl = []
    for item in body:
        if item.permission_level == PermissionLevel.NO_PERMISSIONS:
            continue
        acl.append(
            iam.AccessControlRequest(
                group_name=item.group_name,
                permission_level=iam.PermissionLevel(item.permission_level.value),
            )
        )

    try:
        obo_ws.permissions.update(
            request_object_type=resource_type,
            request_object_id=resource_id,
            access_control_list=acl,
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to update permissions: {str(e)}",
        )

    return {"status": "ok", "message": "Permissions updated"}


# ─── Internal helpers ─────────────────────────────────────────────────────────


def _seed_permission_templates(session: Session):
    """Seed the permission template table with defaults from the blog."""
    for persona, resource_map in DEFAULT_PERMISSIONS_MATRIX.items():
        for resource_type, perm_level in resource_map.items():
            template = PermissionTemplate(
                persona=persona.value,
                resource_type=resource_type.value,
                permission_level=perm_level.value,
            )
            session.add(template)
    session.commit()
    logger.info("Seeded default permission templates from blog matrix")
