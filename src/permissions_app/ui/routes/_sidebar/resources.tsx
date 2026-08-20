import { createFileRoute } from "@tanstack/react-router";
import { Suspense, useState } from "react";
import { QueryErrorResetBoundary, useQueryClient } from "@tanstack/react-query";
import { ErrorBoundary } from "react-error-boundary";
import {
  useListResourcesByTypeSuspense,
  useGetResourcePermissions,
  useSetResourcePermissions,
  useListGroupsSuspense,
  getResourcePermissionsKey,
  ResourceType,
  PermissionLevel,
  type ResourceItemOut,
} from "@/lib/api";
import selector from "@/lib/selector";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertCircle,
  Search,
  Database,
  ChevronRight,
  Loader2,
  Shield,
  Settings,
} from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import { useAdmin } from "@/hooks/use-admin";

// Resource type labels for display
const RESOURCE_TYPE_LABELS: Record<string, string> = {
  clusters: "Clusters",
  "cluster-policies": "Cluster Policies",
  "instance-pools": "Instance Pools",
  jobs: "Jobs",
  pipelines: "DLT Pipelines",
  experiments: "MLflow Experiments",
  "registered-models": "MLflow Registered Models",
  repos: "Repos",
  "serving-endpoints": "Serving Endpoints",
  warehouses: "SQL Warehouses",
  notebooks: "Notebooks",
  directories: "Directories",
  dashboards: "Dashboards",
  alerts: "Alerts",
  genie: "Genie Spaces",
  authorization: "Tokens",
};

// Allowed permission levels per resource type (matches Databricks ACL definitions)
const ALLOWED_PERMISSION_LEVELS: Record<string, string[]> = {
  clusters: [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_ATTACH_TO, PermissionLevel.CAN_RESTART, PermissionLevel.CAN_MANAGE],
  "cluster-policies": [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_USE],
  "instance-pools": [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_ATTACH_TO, PermissionLevel.CAN_MANAGE],
  jobs: [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_VIEW, PermissionLevel.CAN_MANAGE_RUN, PermissionLevel.CAN_MANAGE, PermissionLevel.IS_OWNER],
  pipelines: [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_VIEW, PermissionLevel.CAN_RUN, PermissionLevel.CAN_MANAGE, PermissionLevel.IS_OWNER],
  experiments: [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_READ, PermissionLevel.CAN_EDIT, PermissionLevel.CAN_MANAGE],
  "registered-models": [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_READ, PermissionLevel.CAN_EDIT, PermissionLevel.CAN_MANAGE_STAGING_VERSIONS, PermissionLevel.CAN_MANAGE_PRODUCTION_VERSIONS, PermissionLevel.CAN_MANAGE],
  repos: [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_READ, PermissionLevel.CAN_RUN, PermissionLevel.CAN_EDIT, PermissionLevel.CAN_MANAGE],
  "serving-endpoints": [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_VIEW, PermissionLevel.CAN_MANAGE],
  warehouses: [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_USE, PermissionLevel.CAN_MANAGE, PermissionLevel.IS_OWNER],
  notebooks: [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_READ, PermissionLevel.CAN_RUN, PermissionLevel.CAN_EDIT, PermissionLevel.CAN_MANAGE],
  directories: [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_READ, PermissionLevel.CAN_RUN, PermissionLevel.CAN_EDIT, PermissionLevel.CAN_MANAGE],
  dashboards: [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_VIEW, PermissionLevel.CAN_RUN, PermissionLevel.CAN_EDIT, PermissionLevel.CAN_MANAGE],
  alerts: [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_VIEW, PermissionLevel.CAN_RUN, PermissionLevel.CAN_EDIT, PermissionLevel.CAN_MANAGE],
  genie: [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_READ, PermissionLevel.CAN_RUN, PermissionLevel.CAN_EDIT, PermissionLevel.CAN_MANAGE],
  authorization: [PermissionLevel.NO_PERMISSIONS, PermissionLevel.CAN_USE],
};

// Only list resource types that can be browsed (have individual resources)
const BROWSABLE_RESOURCE_TYPES = [
  ResourceType.clusters,
  ResourceType["cluster-policies"],
  ResourceType["instance-pools"],
  ResourceType.jobs,
  ResourceType.pipelines,
  ResourceType.experiments,
  ResourceType["registered-models"],
  ResourceType.repos,
  ResourceType["serving-endpoints"],
  ResourceType.warehouses,
  ResourceType.dashboards,
  ResourceType.genie,
];

interface ResourcesSearchParams {
  type?: string;
}

export const Route = createFileRoute("/_sidebar/resources")({
  component: () => <ResourcesPage />,
  validateSearch: (search: Record<string, unknown>): ResourcesSearchParams => ({
    type: (search.type as string) || undefined,
  }),
});

function ResourcesContent() {
  const { type: initialType } = Route.useSearch();
  const [selectedType, setSelectedType] = useState<string>(
    initialType || ResourceType.clusters,
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">
          Resource Browser
        </h1>
        <p className="text-muted-foreground mt-1">
          Browse individual workspace resources and manage per-resource
          permissions for each group.
        </p>
      </div>

      <Tabs value={selectedType} onValueChange={setSelectedType}>
        <ScrollArea className="w-full">
          <TabsList className="flex w-max">
            {BROWSABLE_RESOURCE_TYPES.map((rt) => (
              <TabsTrigger key={rt} value={rt} className="text-xs px-3">
                {RESOURCE_TYPE_LABELS[rt] || rt}
              </TabsTrigger>
            ))}
          </TabsList>
        </ScrollArea>

        {BROWSABLE_RESOURCE_TYPES.map((rt) => (
          <TabsContent key={rt} value={rt}>
            <QueryErrorResetBoundary>
              {({ reset }) => (
                <ErrorBoundary
                  onReset={reset}
                  fallbackRender={({ resetErrorBoundary }) => (
                    <Card className="border-destructive/50">
                      <CardHeader>
                        <CardTitle className="flex items-center gap-2 text-destructive">
                          <AlertCircle className="h-5 w-5" />
                          Failed to Load Resources
                        </CardTitle>
                      </CardHeader>
                      <CardContent>
                        <Button
                          variant="outline"
                          onClick={resetErrorBoundary}
                        >
                          Try Again
                        </Button>
                      </CardContent>
                    </Card>
                  )}
                >
                  <Suspense fallback={<ResourceListSkeleton />}>
                    <ResourceList resourceType={rt} />
                  </Suspense>
                </ErrorBoundary>
              )}
            </QueryErrorResetBoundary>
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}

function ResourceList({ resourceType }: { resourceType: string }) {
  const { data: resources } = useListResourcesByTypeSuspense({
    params: { resource_type: resourceType },
    query: {
      select: (d: { data: ResourceItemOut[] }) => d.data,
    },
  });
  const [search, setSearch] = useState("");
  const [selectedResource, setSelectedResource] =
    useState<ResourceItemOut | null>(null);

  const filtered = resources.filter(
    (r: ResourceItemOut) =>
      r.name.toLowerCase().includes(search.toLowerCase()) ||
      r.id.toLowerCase().includes(search.toLowerCase()),
  );

  return (
    <>
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="flex items-center gap-2">
                <Database className="h-5 w-5" />
                {RESOURCE_TYPE_LABELS[resourceType] || resourceType}
              </CardTitle>
              <CardDescription>
                {resources.length} resource
                {resources.length !== 1 ? "s" : ""} found. Click a resource to
                manage its permissions.
              </CardDescription>
            </div>
            <div className="relative w-64">
              <Search className="absolute left-2 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search resources..."
                className="pl-8"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {resources.length === 0 ? (
            <div className="text-center py-12 text-muted-foreground">
              <Database className="h-12 w-12 mx-auto mb-3 opacity-50" />
              <p className="text-lg font-medium">No resources found</p>
              <p className="text-sm">
                There are no {RESOURCE_TYPE_LABELS[resourceType]?.toLowerCase()}{" "}
                in your workspace.
              </p>
            </div>
          ) : (
            <ScrollArea className="h-[500px]">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>ID</TableHead>
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filtered.map((resource: ResourceItemOut) => (
                    <TableRow
                      key={resource.id}
                      className="cursor-pointer hover:bg-muted/50"
                      onClick={() => setSelectedResource(resource)}
                    >
                      <TableCell className="font-medium">
                        {resource.name}
                      </TableCell>
                      <TableCell className="text-muted-foreground font-mono text-xs">
                        {resource.id}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button variant="ghost" size="sm">
                          <Settings className="h-4 w-4 mr-1" />
                          Permissions
                          <ChevronRight className="h-4 w-4 ml-1" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                  {filtered.length === 0 && resources.length > 0 && (
                    <TableRow>
                      <TableCell
                        colSpan={3}
                        className="text-center text-muted-foreground py-8"
                      >
                        No matching resources
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </ScrollArea>
          )}
        </CardContent>
      </Card>

      {/* Resource Permission Dialog */}
      {selectedResource && (
        <ResourcePermissionDialog
          resource={selectedResource}
          open={!!selectedResource}
          onOpenChange={(open: boolean) => {
            if (!open) setSelectedResource(null);
          }}
        />
      )}
    </>
  );
}

function ResourcePermissionDialog({
  resource,
  open,
  onOpenChange,
}: {
  resource: ResourceItemOut;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { data: groups } = useListGroupsSuspense(selector());
  const queryClient = useQueryClient();
  const { isAdmin } = useAdmin();

  const permissionsQuery = useGetResourcePermissions({
    params: {
      resource_type: resource.resource_type,
      resource_id: resource.id,
    },
  });

  const setPermissions = useSetResourcePermissions({
    mutation: {
      onSuccess: () => {
        toast.success(`Permissions updated for ${resource.name}`);
        queryClient.invalidateQueries({
          queryKey: getResourcePermissionsKey({
            resource_type: resource.resource_type,
            resource_id: resource.id,
          }),
        });
      },
      onError: (error) => {
        toast.error(`Failed to update: ${error.message}`);
      },
    },
  });

  // Build current permissions map: group_name -> permission_level
  const currentPerms = new Map<string, string>();
  if (permissionsQuery.data?.data.access_control_list) {
    for (const entry of permissionsQuery.data.data.access_control_list) {
      if (entry.group_name && entry.all_permissions && entry.all_permissions.length > 0) {
        // Get the direct (non-inherited) permission
        const directPerm = entry.all_permissions.find(
          (p: Record<string, unknown>) => !p.inherited,
        );
        if (directPerm && directPerm.permission_level) {
          currentPerms.set(
            entry.group_name,
            directPerm.permission_level as string,
          );
        }
      }
    }
  }

  const [editedPerms, setEditedPerms] = useState<Map<string, string>>(
    new Map(),
  );

  const getEffectivePerm = (groupName: string): string => {
    return (
      editedPerms.get(groupName) ||
      currentPerms.get(groupName) ||
      PermissionLevel.NO_PERMISSIONS
    );
  };

  const handleSave = () => {
    const permList: { group_name: string; permission_level: string }[] = [];
    const allGroups = new Set([
      ...currentPerms.keys(),
      ...editedPerms.keys(),
    ]);

    for (const groupName of allGroups) {
      const level = getEffectivePerm(groupName);
      permList.push({
        group_name: groupName,
        permission_level: level,
      });
    }

    setPermissions.mutate({
      params: {
        resource_type: resource.resource_type,
        resource_id: resource.id,
      },
      data: permList.map((p) => ({
        group_name: p.group_name,
        permission_level:
          p.permission_level as (typeof PermissionLevel)[keyof typeof PermissionLevel],
      })),
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Shield className="h-5 w-5" />
            Permissions for {resource.name}
          </DialogTitle>
          <DialogDescription>
            {RESOURCE_TYPE_LABELS[resource.resource_type]} &middot; ID:{" "}
            {resource.id}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {permissionsQuery.isLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-6 w-6 animate-spin" />
              <span className="ml-2 text-sm text-muted-foreground">
                Loading permissions...
              </span>
            </div>
          ) : permissionsQuery.isError ? (
            <div className="text-center py-8 text-destructive">
              <AlertCircle className="h-6 w-6 mx-auto mb-2" />
              <p className="text-sm">
                Failed to load permissions. You may not have access to view
                permissions on this resource.
              </p>
            </div>
          ) : (
            <>
              {/* Current ACL */}
              {permissionsQuery.data?.data.access_control_list &&
                permissionsQuery.data.data.access_control_list.length > 0 && (
                  <div>
                    <h3 className="text-sm font-medium mb-2">
                      Current Permissions
                    </h3>
                    <ScrollArea className="h-[200px]">
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>Identity</TableHead>
                            <TableHead>Permissions</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {permissionsQuery.data.data.access_control_list.map(
                            (entry, idx: number) => (
                              <TableRow key={idx}>
                                <TableCell className="font-medium">
                                  {entry.group_name || entry.user_name || "—"}
                                  <span className="text-xs text-muted-foreground ml-1">
                                    ({entry.group_name ? "group" : "user"})
                                  </span>
                                </TableCell>
                                <TableCell>
                                  <div className="flex flex-wrap gap-1">
                                    {(entry.all_permissions || []).map(
                                      (p: Record<string, unknown>, i: number) => (
                                        <Badge
                                          key={i}
                                          variant="outline"
                                          className={cn(
                                            "text-xs",
                                            p.inherited
                                              ? "opacity-50"
                                              : "",
                                          )}
                                        >
                                          {String(
                                            p.permission_level || "—",
                                          ).replace(/_/g, " ")}
                                          {p.inherited ? " (inherited)" : ""}
                                        </Badge>
                                      ),
                                    )}
                                  </div>
                                </TableCell>
                              </TableRow>
                            ),
                          )}
                        </TableBody>
                      </Table>
                    </ScrollArea>
                  </div>
                )}

              {isAdmin && (
                <>
                  {/* Edit Permissions by Group */}
                  <div>
                    <h3 className="text-sm font-medium mb-2">
                      Edit Group Permissions
                    </h3>
                    <ScrollArea className="h-[200px]">
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>Group</TableHead>
                            <TableHead>Permission</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {groups.map((group) => (
                            <TableRow key={group.id}>
                              <TableCell className="font-medium text-sm">
                                {group.display_name}
                              </TableCell>
                              <TableCell>
                                <Select
                                  value={getEffectivePerm(group.display_name)}
                                  onValueChange={(val: string) => {
                                    setEditedPerms((prev) => {
                                      const next = new Map(prev);
                                      next.set(group.display_name, val);
                                      return next;
                                    });
                                  }}
                                >
                                  <SelectTrigger className="h-8 text-xs w-48">
                                    <SelectValue />
                                  </SelectTrigger>
                                  <SelectContent>
                                    {(ALLOWED_PERMISSION_LEVELS[resource.resource_type] ?? Object.values(PermissionLevel)).map((pl) => (
                                      <SelectItem key={pl} value={pl}>
                                        {pl.replace(/_/g, " ")}
                                      </SelectItem>
                                    ))}
                                  </SelectContent>
                                </Select>
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </ScrollArea>
                  </div>

                  <div className="flex justify-end gap-2 pt-2">
                    <Button
                      variant="outline"
                      onClick={() => onOpenChange(false)}
                    >
                      Cancel
                    </Button>
                    <Button
                      disabled={editedPerms.size === 0 || setPermissions.isPending}
                      onClick={handleSave}
                    >
                      {setPermissions.isPending ? (
                        <>
                          <Loader2 className="h-4 w-4 animate-spin mr-1" />
                          Saving...
                        </>
                      ) : (
                        "Save Permissions"
                      )}
                    </Button>
                  </div>
                </>
              )}

              {!isAdmin && (
                <div className="flex justify-end pt-2">
                  <Button
                    variant="outline"
                    onClick={() => onOpenChange(false)}
                  >
                    Close
                  </Button>
                </div>
              )}
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function ResourceListSkeleton() {
  return (
    <Card>
      <CardHeader>
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-4 w-64" />
      </CardHeader>
      <CardContent>
        <div className="space-y-3">
          {[1, 2, 3, 4, 5].map((i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function ResourcesPage() {
  return (
    <QueryErrorResetBoundary>
      {({ reset }) => (
        <ErrorBoundary
          onReset={reset}
          fallbackRender={({ resetErrorBoundary }) => (
            <Card className="border-destructive/50">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-destructive">
                  <AlertCircle className="h-5 w-5" />
                  Failed to Load Resources
                </CardTitle>
                <CardDescription>
                  Could not load resource data. Make sure the backend is
                  running and you're authenticated.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <Button variant="outline" onClick={resetErrorBoundary}>
                  Try Again
                </Button>
              </CardContent>
            </Card>
          )}
        >
          <Suspense fallback={<ResourceListSkeleton />}>
            <ResourcesContent />
          </Suspense>
        </ErrorBoundary>
      )}
    </QueryErrorResetBoundary>
  );
}
