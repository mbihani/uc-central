import { useQuery, useSuspenseQuery, useMutation } from "@tanstack/react-query";
import type { UseQueryOptions, UseSuspenseQueryOptions, UseMutationOptions } from "@tanstack/react-query";

export interface AddMemberIn {
  user_id: string;
}

export interface ApplyAllResultOut {
  direct_users_synced?: number;
  persona: string;
  results: ApplyResultOut[];
  total_errors: number;
  total_resources_updated: number;
}

export interface ApplyPermissionsIn {
  resource_types?: string[] | null;
}

export interface ApplyPlanItemOut {
  resource_count: number;
  resource_type: string;
  resource_type_label: string;
  target_level: string;
}

export interface ApplyPlanSkippedOut {
  reason: string;
  resource_type: string;
  resource_type_label: string;
  target_level: string;
}

export interface ApplyPreviewOut {
  direct_user_count?: number;
  group_count: number;
  groups: string[];
  persona: string;
  plan: ApplyPlanItemOut[];
  skipped: ApplyPlanSkippedOut[];
  total_resources_affected: number;
}

export interface ApplyResultOut {
  errors?: string[];
  persona: string;
  resource_type: string;
  resources_updated: number;
}

export interface ComplexValue {
  display?: string | null;
  primary?: boolean | null;
  ref?: string | null;
  type?: string | null;
  value?: string | null;
}

export interface DashboardStatsOut {
  mapped_groups: number;
  personas_with_groups: number;
  total_groups: number;
  total_users: number;
  unassigned_users: number;
}

export interface GroupMemberOut {
  display_name?: string | null;
  user_id: string;
  user_name?: string | null;
}

export interface GroupOut {
  display_name: string;
  id: string;
  member_count?: number;
  members?: GroupMemberOut[];
}

export interface HTTPValidationError {
  detail?: ValidationError[];
}

export interface IsAdminOut {
  is_admin: boolean;
}

export interface Name {
  family_name?: string | null;
  given_name?: string | null;
}

export interface PermissionConflictDetail {
  effective_level: string;
  persona_levels: Record<string, string>;
  resource_type: string;
  resource_type_label: string;
}

export interface PermissionConflictsOut {
  conflicts: UserPermissionConflict[];
  total_users_checked: number;
  users_with_conflicts: number;
}

export const PermissionLevel = {
  NO_PERMISSIONS: "NO_PERMISSIONS",
  CAN_VIEW: "CAN_VIEW",
  CAN_READ: "CAN_READ",
  CAN_ATTACH_TO: "CAN_ATTACH_TO",
  CAN_RESTART: "CAN_RESTART",
  CAN_RUN: "CAN_RUN",
  CAN_EDIT: "CAN_EDIT",
  CAN_MANAGE: "CAN_MANAGE",
  CAN_MANAGE_RUN: "CAN_MANAGE_RUN",
  CAN_MANAGE_STAGING_VERSIONS: "CAN_MANAGE_STAGING_VERSIONS",
  CAN_MANAGE_PRODUCTION_VERSIONS: "CAN_MANAGE_PRODUCTION_VERSIONS",
  IS_OWNER: "IS_OWNER",
  CAN_USE: "CAN_USE",
  CAN_MANAGE_PERMISSIONS: "CAN_MANAGE_PERMISSIONS",
} as const;

export type PermissionLevel = (typeof PermissionLevel)[keyof typeof PermissionLevel];

export interface PermissionLevelOut {
  description?: string | null;
  permission_level: string;
}

export interface PermissionMatrixCell {
  permission_level: PermissionLevel;
  persona: string;
  resource_type: ResourceType;
}

export interface PermissionMatrixOut {
  allowed_permission_levels: Record<string, string[]>;
  matrix: PermissionTemplateOut[];
  persona_labels: Record<string, string>;
  personas: string[];
  resource_type_labels: Record<string, string>;
  resource_types: string[];
}

export interface PermissionTemplateOut {
  id: number;
  permission_level: string;
  persona: string;
  resource_type: string;
}

export interface PersonaDefinitionIn {
  description?: string;
  key: string;
  label: string;
}

export interface PersonaDefinitionOut {
  description: string;
  id: number;
  is_default: boolean;
  key: string;
  label: string;
}

export interface PersonaDefinitionUpdateIn {
  description?: string | null;
  label?: string | null;
}

export interface PersonaGroupMappingIn {
  group_id: string;
  group_name: string;
  persona: string;
}

export interface PersonaGroupMappingOut {
  group_id: string;
  group_name: string;
  id: number;
  persona: string;
}

export interface PersonaMemberOut {
  assignment_type?: string;
  display_name?: string | null;
  groups?: string[];
  persona: string;
  user_id: string;
  user_name?: string | null;
}

export interface PersonaOut {
  description: string;
  groups: PersonaGroupMappingOut[];
  is_default?: boolean;
  label: string;
  persona: string;
  users?: PersonaMemberOut[];
}

export interface PersonaUserMappingIn {
  display_name?: string;
  persona: string;
  user_id: string;
  user_name: string;
}

export interface ResourceItemOut {
  id: string;
  name: string;
  resource_type: string;
}

export interface ResourcePermissionOut {
  all_permissions?: Record<string, unknown>[];
  group_name?: string | null;
  user_name?: string | null;
}

export interface ResourcePermissionsOut {
  access_control_list?: ResourcePermissionOut[];
  resource_id: string;
  resource_type: string;
}

export const ResourceType = {
  clusters: "clusters",
  "cluster-policies": "cluster-policies",
  "instance-pools": "instance-pools",
  jobs: "jobs",
  pipelines: "pipelines",
  experiments: "experiments",
  "registered-models": "registered-models",
  repos: "repos",
  "serving-endpoints": "serving-endpoints",
  warehouses: "warehouses",
  notebooks: "notebooks",
  directories: "directories",
  dashboards: "dashboards",
  alerts: "alerts",
  authorization: "authorization",
} as const;

export type ResourceType = (typeof ResourceType)[keyof typeof ResourceType];

export interface SetPermissionIn {
  group_name: string;
  permission_level: PermissionLevel;
}

export interface User {
  active?: boolean | null;
  display_name?: string | null;
  emails?: ComplexValue[] | null;
  entitlements?: ComplexValue[] | null;
  external_id?: string | null;
  groups?: ComplexValue[] | null;
  id?: string | null;
  name?: Name | null;
  roles?: ComplexValue[] | null;
  schemas?: UserSchema[] | null;
  user_name?: string | null;
}

export interface UserOut {
  active?: boolean | null;
  display_name?: string | null;
  groups?: string[];
  id: string;
  user_name?: string | null;
}

export interface UserPermissionConflict {
  conflict_count: number;
  conflicts: PermissionConflictDetail[];
  display_name?: string | null;
  personas: string[];
  user_id: string;
  user_name?: string | null;
}

export const UserSchema = {
  "urn:ietf:params:scim:schemas:core:2.0:User": "urn:ietf:params:scim:schemas:core:2.0:User",
  "urn:ietf:params:scim:schemas:extension:workspace:2.0:User": "urn:ietf:params:scim:schemas:extension:workspace:2.0:User",
} as const;

export type UserSchema = (typeof UserSchema)[keyof typeof UserSchema];

export interface ValidationError {
  ctx?: Record<string, unknown>;
  input?: unknown;
  loc: (string | number)[];
  msg: string;
  type: string;
}

export interface VersionOut {
  version: string;
}

export interface CurrentUserParams {
  "X-Forwarded-Access-Token"?: string | null;
}

export interface CheckIsAdminParams {
  "X-Forwarded-Access-Token"?: string | null;
}

export interface GetDashboardStatsParams {
  "X-Forwarded-Access-Token"?: string | null;
}

export interface ListGroupsParams {
  "X-Forwarded-Access-Token"?: string | null;
}

export interface GetGroupParams {
  group_id: string;
  "X-Forwarded-Access-Token"?: string | null;
}

export interface AddGroupMemberParams {
  group_id: string;
  "X-Forwarded-Access-Token"?: string | null;
}

export interface ApplyPermissionsParams {
  persona: string;
  "X-Forwarded-Access-Token"?: string | null;
}

export interface PreviewApplyPermissionsParams {
  persona: string;
  "X-Forwarded-Access-Token"?: string | null;
}

export interface CheckPermissionConflictsParams {
  "X-Forwarded-Access-Token"?: string | null;
}

export interface GetPermissionMatrixParams {
  "X-Forwarded-Access-Token"?: string | null;
}

export interface UpdatePermissionMatrixParams {
  "X-Forwarded-Access-Token"?: string | null;
}

export interface ListPersonasParams {
  "X-Forwarded-Access-Token"?: string | null;
}

export interface CreatePersonaParams {
  "X-Forwarded-Access-Token"?: string | null;
}

export interface CreatePersonaMappingParams {
  "X-Forwarded-Access-Token"?: string | null;
}

export interface DeletePersonaMappingParams {
  mapping_id: number;
  "X-Forwarded-Access-Token"?: string | null;
}

export interface CreatePersonaUserMappingParams {
  "X-Forwarded-Access-Token"?: string | null;
}

export interface UpdatePersonaParams {
  persona_key: string;
  "X-Forwarded-Access-Token"?: string | null;
}

export interface DeletePersonaParams {
  persona_key: string;
  "X-Forwarded-Access-Token"?: string | null;
}

export interface RemovePersonaMemberParams {
  persona: string;
  user_name: string;
  "X-Forwarded-Access-Token"?: string | null;
}

export interface ListResourcesByTypeParams {
  resource_type: string;
  "X-Forwarded-Access-Token"?: string | null;
}

export interface GetPermissionLevelsParams {
  resource_type: string;
  resource_id: string;
  "X-Forwarded-Access-Token"?: string | null;
}

export interface GetResourcePermissionsParams {
  resource_type: string;
  resource_id: string;
  "X-Forwarded-Access-Token"?: string | null;
}

export interface SetResourcePermissionsParams {
  resource_type: string;
  resource_id: string;
  "X-Forwarded-Access-Token"?: string | null;
}

export interface ListUsersParams {
  "X-Forwarded-Access-Token"?: string | null;
}

export interface ListUnassignedUsersParams {
  "X-Forwarded-Access-Token"?: string | null;
}

export class ApiError extends Error {
  status: number;
  statusText: string;
  body: unknown;

  constructor(status: number, statusText: string, body: unknown) {
    super(`HTTP ${status}: ${statusText}`);
    this.name = "ApiError";
    this.status = status;
    this.statusText = statusText;
    this.body = body;
  }
}

export const currentUser = async (params?: CurrentUserParams, options?: RequestInit): Promise<{ data: User }> => {
  const res = await fetch("/api/current-user", { ...options, method: "GET", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const currentUserKey = (params?: CurrentUserParams) => {
  return ["/api/current-user", params] as const;
};

export function useCurrentUser<TData = { data: User }>(options?: { params?: CurrentUserParams; query?: Omit<UseQueryOptions<{ data: User }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: currentUserKey(options?.params), queryFn: () => currentUser(options?.params), ...options?.query });
}

export function useCurrentUserSuspense<TData = { data: User }>(options?: { params?: CurrentUserParams; query?: Omit<UseSuspenseQueryOptions<{ data: User }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: currentUserKey(options?.params), queryFn: () => currentUser(options?.params), ...options?.query });
}

export const checkIsAdmin = async (params?: CheckIsAdminParams, options?: RequestInit): Promise<{ data: IsAdminOut }> => {
  const res = await fetch("/api/current-user/is-admin", { ...options, method: "GET", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const checkIsAdminKey = (params?: CheckIsAdminParams) => {
  return ["/api/current-user/is-admin", params] as const;
};

export function useCheckIsAdmin<TData = { data: IsAdminOut }>(options?: { params?: CheckIsAdminParams; query?: Omit<UseQueryOptions<{ data: IsAdminOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: checkIsAdminKey(options?.params), queryFn: () => checkIsAdmin(options?.params), ...options?.query });
}

export function useCheckIsAdminSuspense<TData = { data: IsAdminOut }>(options?: { params?: CheckIsAdminParams; query?: Omit<UseSuspenseQueryOptions<{ data: IsAdminOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: checkIsAdminKey(options?.params), queryFn: () => checkIsAdmin(options?.params), ...options?.query });
}

export const getDashboardStats = async (params?: GetDashboardStatsParams, options?: RequestInit): Promise<{ data: DashboardStatsOut }> => {
  const res = await fetch("/api/dashboard/stats", { ...options, method: "GET", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const getDashboardStatsKey = (params?: GetDashboardStatsParams) => {
  return ["/api/dashboard/stats", params] as const;
};

export function useGetDashboardStats<TData = { data: DashboardStatsOut }>(options?: { params?: GetDashboardStatsParams; query?: Omit<UseQueryOptions<{ data: DashboardStatsOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: getDashboardStatsKey(options?.params), queryFn: () => getDashboardStats(options?.params), ...options?.query });
}

export function useGetDashboardStatsSuspense<TData = { data: DashboardStatsOut }>(options?: { params?: GetDashboardStatsParams; query?: Omit<UseSuspenseQueryOptions<{ data: DashboardStatsOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: getDashboardStatsKey(options?.params), queryFn: () => getDashboardStats(options?.params), ...options?.query });
}

export const listGroups = async (params?: ListGroupsParams, options?: RequestInit): Promise<{ data: GroupOut[] }> => {
  const res = await fetch("/api/groups", { ...options, method: "GET", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const listGroupsKey = (params?: ListGroupsParams) => {
  return ["/api/groups", params] as const;
};

export function useListGroups<TData = { data: GroupOut[] }>(options?: { params?: ListGroupsParams; query?: Omit<UseQueryOptions<{ data: GroupOut[] }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: listGroupsKey(options?.params), queryFn: () => listGroups(options?.params), ...options?.query });
}

export function useListGroupsSuspense<TData = { data: GroupOut[] }>(options?: { params?: ListGroupsParams; query?: Omit<UseSuspenseQueryOptions<{ data: GroupOut[] }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: listGroupsKey(options?.params), queryFn: () => listGroups(options?.params), ...options?.query });
}

export const getGroup = async (params: GetGroupParams, options?: RequestInit): Promise<{ data: GroupOut }> => {
  const res = await fetch(`/api/groups/${params.group_id}`, { ...options, method: "GET", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const getGroupKey = (params?: GetGroupParams) => {
  return ["/api/groups/{group_id}", params] as const;
};

export function useGetGroup<TData = { data: GroupOut }>(options: { params: GetGroupParams; query?: Omit<UseQueryOptions<{ data: GroupOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: getGroupKey(options.params), queryFn: () => getGroup(options.params), ...options?.query });
}

export function useGetGroupSuspense<TData = { data: GroupOut }>(options: { params: GetGroupParams; query?: Omit<UseSuspenseQueryOptions<{ data: GroupOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: getGroupKey(options.params), queryFn: () => getGroup(options.params), ...options?.query });
}

export const addGroupMember = async (params: AddGroupMemberParams, data: AddMemberIn, options?: RequestInit): Promise<{ data: Record<string, unknown> }> => {
  const res = await fetch(`/api/groups/${params.group_id}/members`, { ...options, method: "POST", headers: { "Content-Type": "application/json", ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers }, body: JSON.stringify(data) });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export function useAddGroupMember(options?: { mutation?: UseMutationOptions<{ data: Record<string, unknown> }, ApiError, { params: AddGroupMemberParams; data: AddMemberIn }> }) {
  return useMutation({ mutationFn: (vars) => addGroupMember(vars.params, vars.data), ...options?.mutation });
}

export const applyPermissions = async (params: ApplyPermissionsParams, data: ApplyPermissionsIn | null, options?: RequestInit): Promise<{ data: ApplyAllResultOut }> => {
  const res = await fetch(`/api/permissions/apply/${params.persona}`, { ...options, method: "POST", headers: { "Content-Type": "application/json", ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers }, body: JSON.stringify(data) });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export function useApplyPermissions(options?: { mutation?: UseMutationOptions<{ data: ApplyAllResultOut }, ApiError, { params: ApplyPermissionsParams; data: ApplyPermissionsIn | null }> }) {
  return useMutation({ mutationFn: (vars) => applyPermissions(vars.params, vars.data), ...options?.mutation });
}

export const previewApplyPermissions = async (params: PreviewApplyPermissionsParams, options?: RequestInit): Promise<{ data: ApplyPreviewOut }> => {
  const res = await fetch(`/api/permissions/apply/${params.persona}/preview`, { ...options, method: "GET", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const previewApplyPermissionsKey = (params?: PreviewApplyPermissionsParams) => {
  return ["/api/permissions/apply/{persona}/preview", params] as const;
};

export function usePreviewApplyPermissions<TData = { data: ApplyPreviewOut }>(options: { params: PreviewApplyPermissionsParams; query?: Omit<UseQueryOptions<{ data: ApplyPreviewOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: previewApplyPermissionsKey(options.params), queryFn: () => previewApplyPermissions(options.params), ...options?.query });
}

export function usePreviewApplyPermissionsSuspense<TData = { data: ApplyPreviewOut }>(options: { params: PreviewApplyPermissionsParams; query?: Omit<UseSuspenseQueryOptions<{ data: ApplyPreviewOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: previewApplyPermissionsKey(options.params), queryFn: () => previewApplyPermissions(options.params), ...options?.query });
}

export const checkPermissionConflicts = async (params?: CheckPermissionConflictsParams, options?: RequestInit): Promise<{ data: PermissionConflictsOut }> => {
  const res = await fetch("/api/permissions/conflicts", { ...options, method: "GET", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const checkPermissionConflictsKey = (params?: CheckPermissionConflictsParams) => {
  return ["/api/permissions/conflicts", params] as const;
};

export function useCheckPermissionConflicts<TData = { data: PermissionConflictsOut }>(options?: { params?: CheckPermissionConflictsParams; query?: Omit<UseQueryOptions<{ data: PermissionConflictsOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: checkPermissionConflictsKey(options?.params), queryFn: () => checkPermissionConflicts(options?.params), ...options?.query });
}

export function useCheckPermissionConflictsSuspense<TData = { data: PermissionConflictsOut }>(options?: { params?: CheckPermissionConflictsParams; query?: Omit<UseSuspenseQueryOptions<{ data: PermissionConflictsOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: checkPermissionConflictsKey(options?.params), queryFn: () => checkPermissionConflicts(options?.params), ...options?.query });
}

export const getPermissionMatrix = async (params?: GetPermissionMatrixParams, options?: RequestInit): Promise<{ data: PermissionMatrixOut }> => {
  const res = await fetch("/api/permissions/matrix", { ...options, method: "GET", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const getPermissionMatrixKey = (params?: GetPermissionMatrixParams) => {
  return ["/api/permissions/matrix", params] as const;
};

export function useGetPermissionMatrix<TData = { data: PermissionMatrixOut }>(options?: { params?: GetPermissionMatrixParams; query?: Omit<UseQueryOptions<{ data: PermissionMatrixOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: getPermissionMatrixKey(options?.params), queryFn: () => getPermissionMatrix(options?.params), ...options?.query });
}

export function useGetPermissionMatrixSuspense<TData = { data: PermissionMatrixOut }>(options?: { params?: GetPermissionMatrixParams; query?: Omit<UseSuspenseQueryOptions<{ data: PermissionMatrixOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: getPermissionMatrixKey(options?.params), queryFn: () => getPermissionMatrix(options?.params), ...options?.query });
}

export const updatePermissionMatrix = async (data: PermissionMatrixCell, params?: UpdatePermissionMatrixParams, options?: RequestInit): Promise<{ data: PermissionTemplateOut }> => {
  const res = await fetch("/api/permissions/matrix", { ...options, method: "PUT", headers: { "Content-Type": "application/json", ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers }, body: JSON.stringify(data) });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export function useUpdatePermissionMatrix(options?: { mutation?: UseMutationOptions<{ data: PermissionTemplateOut }, ApiError, { params: UpdatePermissionMatrixParams; data: PermissionMatrixCell }> }) {
  return useMutation({ mutationFn: (vars) => updatePermissionMatrix(vars.data, vars.params), ...options?.mutation });
}

export const listPersonas = async (params?: ListPersonasParams, options?: RequestInit): Promise<{ data: PersonaOut[] }> => {
  const res = await fetch("/api/personas", { ...options, method: "GET", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const listPersonasKey = (params?: ListPersonasParams) => {
  return ["/api/personas", params] as const;
};

export function useListPersonas<TData = { data: PersonaOut[] }>(options?: { params?: ListPersonasParams; query?: Omit<UseQueryOptions<{ data: PersonaOut[] }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: listPersonasKey(options?.params), queryFn: () => listPersonas(options?.params), ...options?.query });
}

export function useListPersonasSuspense<TData = { data: PersonaOut[] }>(options?: { params?: ListPersonasParams; query?: Omit<UseSuspenseQueryOptions<{ data: PersonaOut[] }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: listPersonasKey(options?.params), queryFn: () => listPersonas(options?.params), ...options?.query });
}

export const createPersona = async (data: PersonaDefinitionIn, params?: CreatePersonaParams, options?: RequestInit): Promise<{ data: PersonaDefinitionOut }> => {
  const res = await fetch("/api/personas", { ...options, method: "POST", headers: { "Content-Type": "application/json", ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers }, body: JSON.stringify(data) });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export function useCreatePersona(options?: { mutation?: UseMutationOptions<{ data: PersonaDefinitionOut }, ApiError, { params: CreatePersonaParams; data: PersonaDefinitionIn }> }) {
  return useMutation({ mutationFn: (vars) => createPersona(vars.data, vars.params), ...options?.mutation });
}

export const createPersonaMapping = async (data: PersonaGroupMappingIn, params?: CreatePersonaMappingParams, options?: RequestInit): Promise<{ data: PersonaGroupMappingOut }> => {
  const res = await fetch("/api/personas/mappings", { ...options, method: "POST", headers: { "Content-Type": "application/json", ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers }, body: JSON.stringify(data) });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export function useCreatePersonaMapping(options?: { mutation?: UseMutationOptions<{ data: PersonaGroupMappingOut }, ApiError, { params: CreatePersonaMappingParams; data: PersonaGroupMappingIn }> }) {
  return useMutation({ mutationFn: (vars) => createPersonaMapping(vars.data, vars.params), ...options?.mutation });
}

export const deletePersonaMapping = async (params: DeletePersonaMappingParams, options?: RequestInit): Promise<{ data: Record<string, unknown> }> => {
  const res = await fetch(`/api/personas/mappings/${params.mapping_id}`, { ...options, method: "DELETE", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export function useDeletePersonaMapping(options?: { mutation?: UseMutationOptions<{ data: Record<string, unknown> }, ApiError, { params: DeletePersonaMappingParams }> }) {
  return useMutation({ mutationFn: (vars) => deletePersonaMapping(vars.params), ...options?.mutation });
}

export const createPersonaUserMapping = async (data: PersonaUserMappingIn, params?: CreatePersonaUserMappingParams, options?: RequestInit): Promise<{ data: Record<string, unknown> }> => {
  const res = await fetch("/api/personas/user-mappings", { ...options, method: "POST", headers: { "Content-Type": "application/json", ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers }, body: JSON.stringify(data) });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export function useCreatePersonaUserMapping(options?: { mutation?: UseMutationOptions<{ data: Record<string, unknown> }, ApiError, { params: CreatePersonaUserMappingParams; data: PersonaUserMappingIn }> }) {
  return useMutation({ mutationFn: (vars) => createPersonaUserMapping(vars.data, vars.params), ...options?.mutation });
}

export const updatePersona = async (params: UpdatePersonaParams, data: PersonaDefinitionUpdateIn, options?: RequestInit): Promise<{ data: PersonaDefinitionOut }> => {
  const res = await fetch(`/api/personas/${params.persona_key}`, { ...options, method: "PUT", headers: { "Content-Type": "application/json", ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers }, body: JSON.stringify(data) });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export function useUpdatePersona(options?: { mutation?: UseMutationOptions<{ data: PersonaDefinitionOut }, ApiError, { params: UpdatePersonaParams; data: PersonaDefinitionUpdateIn }> }) {
  return useMutation({ mutationFn: (vars) => updatePersona(vars.params, vars.data), ...options?.mutation });
}

export const deletePersona = async (params: DeletePersonaParams, options?: RequestInit): Promise<{ data: Record<string, unknown> }> => {
  const res = await fetch(`/api/personas/${params.persona_key}`, { ...options, method: "DELETE", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export function useDeletePersona(options?: { mutation?: UseMutationOptions<{ data: Record<string, unknown> }, ApiError, { params: DeletePersonaParams }> }) {
  return useMutation({ mutationFn: (vars) => deletePersona(vars.params), ...options?.mutation });
}

export const removePersonaMember = async (params: RemovePersonaMemberParams, options?: RequestInit): Promise<{ data: Record<string, unknown> }> => {
  const res = await fetch(`/api/personas/${params.persona}/members/${params.user_name}`, { ...options, method: "DELETE", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export function useRemovePersonaMember(options?: { mutation?: UseMutationOptions<{ data: Record<string, unknown> }, ApiError, { params: RemovePersonaMemberParams }> }) {
  return useMutation({ mutationFn: (vars) => removePersonaMember(vars.params), ...options?.mutation });
}

export const listResourcesByType = async (params: ListResourcesByTypeParams, options?: RequestInit): Promise<{ data: ResourceItemOut[] }> => {
  const res = await fetch(`/api/resources/${params.resource_type}`, { ...options, method: "GET", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const listResourcesByTypeKey = (params?: ListResourcesByTypeParams) => {
  return ["/api/resources/{resource_type}", params] as const;
};

export function useListResourcesByType<TData = { data: ResourceItemOut[] }>(options: { params: ListResourcesByTypeParams; query?: Omit<UseQueryOptions<{ data: ResourceItemOut[] }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: listResourcesByTypeKey(options.params), queryFn: () => listResourcesByType(options.params), ...options?.query });
}

export function useListResourcesByTypeSuspense<TData = { data: ResourceItemOut[] }>(options: { params: ListResourcesByTypeParams; query?: Omit<UseSuspenseQueryOptions<{ data: ResourceItemOut[] }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: listResourcesByTypeKey(options.params), queryFn: () => listResourcesByType(options.params), ...options?.query });
}

export const getPermissionLevels = async (params: GetPermissionLevelsParams, options?: RequestInit): Promise<{ data: PermissionLevelOut[] }> => {
  const res = await fetch(`/api/resources/${params.resource_type}/${params.resource_id}/permission-levels`, { ...options, method: "GET", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const getPermissionLevelsKey = (params?: GetPermissionLevelsParams) => {
  return ["/api/resources/{resource_type}/{resource_id}/permission-levels", params] as const;
};

export function useGetPermissionLevels<TData = { data: PermissionLevelOut[] }>(options: { params: GetPermissionLevelsParams; query?: Omit<UseQueryOptions<{ data: PermissionLevelOut[] }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: getPermissionLevelsKey(options.params), queryFn: () => getPermissionLevels(options.params), ...options?.query });
}

export function useGetPermissionLevelsSuspense<TData = { data: PermissionLevelOut[] }>(options: { params: GetPermissionLevelsParams; query?: Omit<UseSuspenseQueryOptions<{ data: PermissionLevelOut[] }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: getPermissionLevelsKey(options.params), queryFn: () => getPermissionLevels(options.params), ...options?.query });
}

export const getResourcePermissions = async (params: GetResourcePermissionsParams, options?: RequestInit): Promise<{ data: ResourcePermissionsOut }> => {
  const res = await fetch(`/api/resources/${params.resource_type}/${params.resource_id}/permissions`, { ...options, method: "GET", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const getResourcePermissionsKey = (params?: GetResourcePermissionsParams) => {
  return ["/api/resources/{resource_type}/{resource_id}/permissions", params] as const;
};

export function useGetResourcePermissions<TData = { data: ResourcePermissionsOut }>(options: { params: GetResourcePermissionsParams; query?: Omit<UseQueryOptions<{ data: ResourcePermissionsOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: getResourcePermissionsKey(options.params), queryFn: () => getResourcePermissions(options.params), ...options?.query });
}

export function useGetResourcePermissionsSuspense<TData = { data: ResourcePermissionsOut }>(options: { params: GetResourcePermissionsParams; query?: Omit<UseSuspenseQueryOptions<{ data: ResourcePermissionsOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: getResourcePermissionsKey(options.params), queryFn: () => getResourcePermissions(options.params), ...options?.query });
}

export const setResourcePermissions = async (params: SetResourcePermissionsParams, data: SetPermissionIn[], options?: RequestInit): Promise<{ data: Record<string, unknown> }> => {
  const res = await fetch(`/api/resources/${params.resource_type}/${params.resource_id}/permissions`, { ...options, method: "PUT", headers: { "Content-Type": "application/json", ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers }, body: JSON.stringify(data) });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export function useSetResourcePermissions(options?: { mutation?: UseMutationOptions<{ data: Record<string, unknown> }, ApiError, { params: SetResourcePermissionsParams; data: SetPermissionIn[] }> }) {
  return useMutation({ mutationFn: (vars) => setResourcePermissions(vars.params, vars.data), ...options?.mutation });
}

export const listUsers = async (params?: ListUsersParams, options?: RequestInit): Promise<{ data: UserOut[] }> => {
  const res = await fetch("/api/users", { ...options, method: "GET", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const listUsersKey = (params?: ListUsersParams) => {
  return ["/api/users", params] as const;
};

export function useListUsers<TData = { data: UserOut[] }>(options?: { params?: ListUsersParams; query?: Omit<UseQueryOptions<{ data: UserOut[] }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: listUsersKey(options?.params), queryFn: () => listUsers(options?.params), ...options?.query });
}

export function useListUsersSuspense<TData = { data: UserOut[] }>(options?: { params?: ListUsersParams; query?: Omit<UseSuspenseQueryOptions<{ data: UserOut[] }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: listUsersKey(options?.params), queryFn: () => listUsers(options?.params), ...options?.query });
}

export const listUnassignedUsers = async (params?: ListUnassignedUsersParams, options?: RequestInit): Promise<{ data: UserOut[] }> => {
  const res = await fetch("/api/users/unassigned", { ...options, method: "GET", headers: { ...(params?.["X-Forwarded-Access-Token"] != null && { "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"] }), ...options?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const listUnassignedUsersKey = (params?: ListUnassignedUsersParams) => {
  return ["/api/users/unassigned", params] as const;
};

export function useListUnassignedUsers<TData = { data: UserOut[] }>(options?: { params?: ListUnassignedUsersParams; query?: Omit<UseQueryOptions<{ data: UserOut[] }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: listUnassignedUsersKey(options?.params), queryFn: () => listUnassignedUsers(options?.params), ...options?.query });
}

export function useListUnassignedUsersSuspense<TData = { data: UserOut[] }>(options?: { params?: ListUnassignedUsersParams; query?: Omit<UseSuspenseQueryOptions<{ data: UserOut[] }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: listUnassignedUsersKey(options?.params), queryFn: () => listUnassignedUsers(options?.params), ...options?.query });
}

export const version = async (options?: RequestInit): Promise<{ data: VersionOut }> => {
  const res = await fetch("/api/version", { ...options, method: "GET" });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown;
    try { parsed = JSON.parse(body); } catch { parsed = body; }
    throw new ApiError(res.status, res.statusText, parsed);
  }
  return { data: await res.json() };
};

export const versionKey = () => {
  return ["/api/version"] as const;
};

export function useVersion<TData = { data: VersionOut }>(options?: { query?: Omit<UseQueryOptions<{ data: VersionOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useQuery({ queryKey: versionKey(), queryFn: () => version(), ...options?.query });
}

export function useVersionSuspense<TData = { data: VersionOut }>(options?: { query?: Omit<UseSuspenseQueryOptions<{ data: VersionOut }, ApiError, TData>, "queryKey" | "queryFn"> }) {
  return useSuspenseQuery({ queryKey: versionKey(), queryFn: () => version(), ...options?.query });
}

