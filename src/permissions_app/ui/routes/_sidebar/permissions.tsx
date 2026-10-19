import { createFileRoute, Link } from "@tanstack/react-router";
import { Suspense, useState, useMemo, Fragment } from "react";
import {
  QueryErrorResetBoundary,
  useQueryClient,
  useMutation,
} from "@tanstack/react-query";
import { ErrorBoundary } from "react-error-boundary";
import {
  useGetPermissionMatrixSuspense,
  useListPersonasSuspense,
  useUpdatePermissionMatrix,
  useApplyPermissions,
  useCheckPermissionConflicts,
  useListResourcesByType,
  useSetPermissionOverride,
  previewApplyPermissions,
  checkPermissionConflictsKey,
  getPermissionMatrixKey,
  PermissionLevel,
  type ResourceType,
  type UserPermissionConflict,
  type ApplyPreviewOut,
  type PermissionMatrixOut,
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
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  AlertCircle,
  AlertTriangle,
  Shield,
  Play,
  Loader2,
  ExternalLink,
  ChevronDown,
  ChevronRight,
  Users,
  ArrowUp,
  RefreshCw,
} from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import { useAdmin } from "@/hooks/use-admin";

export const Route = createFileRoute("/_sidebar/permissions")({
  component: () => <PermissionsPage />,
});

// Resource types organized into logical groups
const RESOURCE_GROUPS: { name: string; types: string[] }[] = [
  {
    name: "Compute",
    types: ["clusters", "cluster-policies", "instance-pools"],
  },
  {
    name: "Workflow & Orchestration",
    types: ["jobs", "pipelines"],
  },
  {
    name: "Machine Learning",
    types: ["experiments", "registered-models", "serving-endpoints"],
  },
  {
    name: "Development",
    types: ["repos", "notebooks", "directories"],
  },
  {
    name: "Analytics & BI",
    types: ["warehouses", "dashboards", "alerts", "genie"],
  },
  {
    name: "Apps & Databases",
    types: ["apps", "database-instances"],
  },
  {
    name: "Security",
    types: ["authorization"],
  },
];

// Color coding for permission levels, matching the blog's visual
function getPermissionColor(level: string): string {
  switch (level) {
    case PermissionLevel.CAN_MANAGE:
    case PermissionLevel.IS_OWNER:
    case PermissionLevel.CAN_MANAGE_PERMISSIONS:
      return "bg-red-500/15 text-red-700 dark:text-red-400 border-red-500/30";
    case PermissionLevel.CAN_MANAGE_RUN:
    case PermissionLevel.CAN_MANAGE_PRODUCTION_VERSIONS:
    case PermissionLevel.CAN_MANAGE_STAGING_VERSIONS:
    case PermissionLevel.CAN_EDIT:
    case PermissionLevel.CAN_RUN:
    case PermissionLevel.CAN_USE:
      return "bg-yellow-500/15 text-yellow-700 dark:text-yellow-400 border-yellow-500/30";
    case PermissionLevel.CAN_VIEW:
    case PermissionLevel.CAN_READ:
    case PermissionLevel.CAN_ATTACH_TO:
    case PermissionLevel.CAN_RESTART:
      return "bg-green-500/15 text-green-700 dark:text-green-400 border-green-500/30";
    case PermissionLevel.NO_PERMISSIONS:
      return "bg-muted/50 text-muted-foreground border-muted";
    default:
      return "bg-muted/50 text-muted-foreground";
  }
}

function getPermissionLabel(level: string): string {
  return level.replace(/_/g, " ");
}

// Human-readable reason a resource type is skipped by the apply dry-run plan.
function skipReasonLabel(reason: string): string {
  switch (reason) {
    case "no_permissions":
      return "no permission set";
    case "unsupported":
      return "not supported by Apply";
    case "invalid_level":
      return "invalid level — fix the matrix";
    default:
      return reason;
  }
}

function ConflictUserRow({ conflict }: { conflict: UserPermissionConflict }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="border rounded-lg overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-3 p-3 hover:bg-muted/50 transition-colors text-left"
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
        )}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <Users className="h-4 w-4 text-muted-foreground shrink-0" />
            <span className="font-medium truncate">
              {conflict.display_name || conflict.user_name || conflict.user_id}
            </span>
          </div>
          <div className="flex items-center gap-2 mt-0.5">
            <span className="text-xs text-muted-foreground">
              Personas: {conflict.personas.join(", ")}
            </span>
          </div>
        </div>
        <Badge variant="outline" className="shrink-0 bg-amber-500/15 text-amber-700 dark:text-amber-400 border-amber-500/30">
          {conflict.conflict_count} conflict{conflict.conflict_count !== 1 ? "s" : ""}
        </Badge>
      </button>

      {expanded && (
        <div className="border-t px-3 pb-3">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="text-xs">Resource Type</TableHead>
                {conflict.personas.map((p) => (
                  <TableHead key={p} className="text-xs text-center">
                    {p}
                  </TableHead>
                ))}
                <TableHead className="text-xs text-center">
                  <span className="flex items-center gap-1 justify-center">
                    <ArrowUp className="h-3 w-3" />
                    Effective
                  </span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {conflict.conflicts.map((c) => (
                <TableRow key={c.resource_type}>
                  <TableCell className="text-sm py-1.5 font-medium">
                    {c.resource_type_label}
                  </TableCell>
                  {conflict.personas.map((p) => {
                    const level = c.persona_levels[p] ?? PermissionLevel.NO_PERMISSIONS;
                    const isHighest = level === c.effective_level;
                    return (
                      <TableCell key={p} className="text-center py-1.5">
                        <Badge
                          variant="outline"
                          className={cn(
                            "text-xs",
                            getPermissionColor(level),
                            isHighest && "ring-1 ring-offset-1 ring-primary",
                          )}
                        >
                          {getPermissionLabel(level)}
                        </Badge>
                      </TableCell>
                    );
                  })}
                  <TableCell className="text-center py-1.5">
                    <Tooltip>
                      <TooltipTrigger>
                        <Badge
                          variant="outline"
                          className={cn(
                            "text-xs font-semibold",
                            getPermissionColor(c.effective_level),
                          )}
                        >
                          {getPermissionLabel(c.effective_level)}
                        </Badge>
                      </TooltipTrigger>
                      <TooltipContent>
                        Resolved to highest permission level
                      </TooltipContent>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}

function PermissionsContent() {
  const { data: matrix } = useGetPermissionMatrixSuspense(selector());
  const { data: personas } = useListPersonasSuspense(selector());
  const queryClient = useQueryClient();
  const [applyingPersona, setApplyingPersona] = useState<string | null>(null);
  const [confirmPersona, setConfirmPersona] = useState<string | null>(null);
  const [previewData, setPreviewData] = useState<ApplyPreviewOut | null>(null);
  const [selectedTypes, setSelectedTypes] = useState<Set<string>>(new Set());
  const [showConflictsDialog, setShowConflictsDialog] = useState(false);
  const { isAdmin } = useAdmin();

  // DRY-RUN preview: computes the blast radius (per-type counts) with ZERO ACL
  // writes. Triggered on the Apply click; its result drives the confirmation
  // modal. Only on explicit confirm is the REAL (scoped) apply fired.
  const previewApply = useMutation({
    mutationFn: (persona: string) => previewApplyPermissions({ persona }),
    onSuccess: (result) => {
      const data = result.data;
      setPreviewData(data);
      // Default: every plannable type is selected (full apply). The admin can
      // deselect types to shrink the blast radius before confirming.
      setSelectedTypes(new Set(data.plan.map((p) => p.resource_type)));
    },
    onError: (error) => {
      toast.error(`Failed to compute preview: ${error.message}`);
    },
  });

  // Conflict detection query - not suspense, loads in background
  const conflictsQuery = useCheckPermissionConflicts({
    query: {
      staleTime: 60_000, // cache for 1 minute
      retry: 1,
    },
  });

  const conflicts = conflictsQuery.data?.data;
  const hasConflicts = (conflicts?.users_with_conflicts ?? 0) > 0;

  const updateMatrix = useUpdatePermissionMatrix({
    mutation: {
      onSuccess: () => {
        queryClient.invalidateQueries({
          queryKey: getPermissionMatrixKey(),
        });
        toast.success("Permission updated");
      },
      onError: (error) => {
        toast.error(`Failed to update: ${error.message}`);
      },
    },
  });

  const applyPerms = useApplyPermissions({
    mutation: {
      onSuccess: (result) => {
        const data = result.data;
        setApplyingPersona(null);
        const directPart =
          data.direct_users_synced && data.direct_users_synced > 0
            ? `; ${data.direct_users_synced} direct user${data.direct_users_synced !== 1 ? "s" : ""} also re-synced`
            : "";
        if (data.total_errors > 0) {
          toast.warning(
            `Applied to ${data.total_resources_updated} resources with ${data.total_errors} errors${directPart}`,
          );
        } else {
          toast.success(
            `Applied to ${data.total_resources_updated} resources${directPart}`,
          );
        }
        const added = data.table_grants_added ?? 0;
        const revoked = data.table_grants_revoked ?? 0;
        if (added || revoked) {
          toast.info(`Genie table grants: ${added} added, ${revoked} revoked`);
        }
        for (const w of (data.warnings ?? []).slice(0, 3)) {
          toast.warning(w);
        }
      },
      onError: (error) => {
        setApplyingPersona(null);
        toast.error(`Failed to apply: ${error.message}`);
      },
    },
  });

  // Build a lookup map: persona+resource_type -> permission_level
  const matrixMap = new Map<string, string>();
  for (const entry of matrix.matrix) {
    matrixMap.set(`${entry.persona}::${entry.resource_type}`, entry.permission_level);
  }

  // Build effective resource groups (include ungrouped types in "Other")
  const effectiveGroups = useMemo(() => {
    const allGroupedTypes = new Set(RESOURCE_GROUPS.flatMap((g) => g.types));
    const ungroupedTypes = matrix.resource_types.filter(
      (rt) => !allGroupedTypes.has(rt),
    );
    const groups = [...RESOURCE_GROUPS];
    if (ungroupedTypes.length > 0) {
      groups.push({ name: "Other", types: ungroupedTypes });
    }
    return groups;
  }, [matrix.resource_types]);

  const getPermLevel = (persona: string, resourceType: string): string => {
    return matrixMap.get(`${persona}::${resourceType}`) || PermissionLevel.NO_PERMISSIONS;
  };

  // Build persona has-groups map (gates the Apply button)
  const personaHasGroups = new Map<string, boolean>();
  for (const p of personas) {
    personaHasGroups.set(p.persona, p.groups.length > 0);
  }

  // Count conflicts relevant to the persona being confirmed
  const confirmConflictCount = useMemo(() => {
    if (!confirmPersona || !conflicts) return 0;
    const personaLabel = matrix.persona_labels[confirmPersona] ?? confirmPersona;
    return conflicts.conflicts.filter((c) =>
      c.personas.includes(personaLabel)
    ).length;
  }, [confirmPersona, conflicts, matrix.persona_labels]);

  // How many workspace resources the CURRENTLY-SELECTED types would rewrite.
  const selectedResourceCount = useMemo(() => {
    if (!previewData) return 0;
    return previewData.plan
      .filter((p) => selectedTypes.has(p.resource_type))
      .reduce((sum, p) => sum + p.resource_count, 0);
  }, [previewData, selectedTypes]);

  const closeConfirm = () => {
    setConfirmPersona(null);
    setPreviewData(null);
    setSelectedTypes(new Set());
    previewApply.reset();
  };

  const toggleType = (resourceType: string) => {
    setSelectedTypes((prev) => {
      const next = new Set(prev);
      if (next.has(resourceType)) {
        next.delete(resourceType);
      } else {
        next.add(resourceType);
      }
      return next;
    });
  };

  // Step 1 of the guarded Apply: open the modal and compute the dry-run preview.
  // NOTHING is written here.
  const handleApplyClick = (persona: string) => {
    setConfirmPersona(persona);
    setPreviewData(null);
    setSelectedTypes(new Set());
    previewApply.reset();
    previewApply.mutate(persona);
  };

  // Step 2: only after the admin confirms do we fire the REAL apply, scoped to
  // exactly the resource types still selected in the modal.
  const handleConfirmApply = () => {
    if (!confirmPersona || !previewData || selectedTypes.size === 0) return;
    setApplyingPersona(confirmPersona);
    applyPerms.mutate({
      params: { persona: confirmPersona },
      data: { resource_types: Array.from(selectedTypes) },
    });
    closeConfirm();
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">
          Permissions Matrix
        </h1>
        <p className="text-muted-foreground mt-1">
          Configure default permission levels for each persona across resource
          types. Click a cell to change the permission.
        </p>
      </div>

      {/* Legend */}
      <Card>
        <CardContent className="pt-4">
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-sm font-medium">Legend:</span>
            <Badge
              className={cn(
                "text-xs",
                getPermissionColor(PermissionLevel.CAN_MANAGE),
              )}
              variant="outline"
            >
              High (Manage/Owner)
            </Badge>
            <Badge
              className={cn(
                "text-xs",
                getPermissionColor(PermissionLevel.CAN_RUN),
              )}
              variant="outline"
            >
              Medium (Edit/Run/Use)
            </Badge>
            <Badge
              className={cn(
                "text-xs",
                getPermissionColor(PermissionLevel.CAN_VIEW),
              )}
              variant="outline"
            >
              Low (View/Read)
            </Badge>
            <Badge
              className={cn(
                "text-xs",
                getPermissionColor(PermissionLevel.NO_PERMISSIONS),
              )}
              variant="outline"
            >
              None
            </Badge>
          </div>
        </CardContent>
      </Card>

      {/* Conflict Detection Banner */}
      {hasConflicts && conflicts && (
        <Alert className="border-amber-500/50 bg-amber-500/5">
          <AlertTriangle className="h-4 w-4 text-amber-600 dark:text-amber-400" />
          <AlertTitle className="text-amber-700 dark:text-amber-400">
            Permission Conflicts Detected
          </AlertTitle>
          <AlertDescription className="flex items-center justify-between">
            <span>
              <strong>{conflicts.users_with_conflicts}</strong> user
              {conflicts.users_with_conflicts !== 1 ? "s" : ""} belong
              {conflicts.users_with_conflicts === 1 ? "s" : ""} to groups mapped
              to multiple personas with different permission levels. Conflicts are
              auto-resolved to the highest permission when applying.
            </span>
            <div className="flex items-center gap-2 shrink-0 ml-4">
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs border-amber-500/30 hover:bg-amber-500/10"
                onClick={() =>
                  queryClient.invalidateQueries({
                    queryKey: checkPermissionConflictsKey(),
                  })
                }
                disabled={conflictsQuery.isFetching}
              >
                {conflictsQuery.isFetching ? (
                  <Loader2 className="h-3 w-3 animate-spin mr-1" />
                ) : (
                  <RefreshCw className="h-3 w-3 mr-1" />
                )}
                Refresh
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs border-amber-500/30 hover:bg-amber-500/10"
                onClick={() => setShowConflictsDialog(true)}
              >
                View Details
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}

      {!hasConflicts && !conflictsQuery.isLoading && conflicts && (
        <Alert className="border-green-500/50 bg-green-500/5">
          <Shield className="h-4 w-4 text-green-600 dark:text-green-400" />
          <AlertTitle className="text-green-700 dark:text-green-400">
            No Permission Conflicts
          </AlertTitle>
          <AlertDescription className="flex items-center justify-between">
            <span>
              All {conflicts.total_users_checked} user
              {conflicts.total_users_checked !== 1 ? "s" : ""} checked — no
              conflicting permission levels across personas.
            </span>
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs shrink-0 ml-4 border-green-500/30 hover:bg-green-500/10"
              onClick={() =>
                queryClient.invalidateQueries({
                  queryKey: checkPermissionConflictsKey(),
                })
              }
              disabled={conflictsQuery.isFetching}
            >
              {conflictsQuery.isFetching ? (
                <Loader2 className="h-3 w-3 animate-spin mr-1" />
              ) : (
                <RefreshCw className="h-3 w-3 mr-1" />
              )}
              Re-check
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {/* Matrix Table */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="flex items-center gap-2">
                <Shield className="h-5 w-5" />
                Resource Group x Persona Permissions
              </CardTitle>
              <CardDescription>
                Click any cell to change the permission level. Use "Apply" to
                push permissions to the workspace.
              </CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {/* The shadcn Table wraps its <table> in its OWN `overflow-auto` div,
              so that div is the horizontal scroll container. `min-w` goes on the
              Table (not an outer wrapper) so the first column's `sticky left-0`
              sticks against that same scroller — wrapping this in a ScrollArea
              instead creates a second scroll container and the frozen column
              breaks. */}
          <div className="w-full">
            <Table className="min-w-[900px]">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-52 sticky left-0 bg-background z-20">
                    Resource Group
                  </TableHead>
                    {matrix.personas.map((p) => (
                      <TableHead key={p} className="text-center min-w-[140px]">
                        <div className="flex flex-col items-center gap-1">
                          <span className="text-xs font-medium">
                            {matrix.persona_labels[p]}
                          </span>
                          {isAdmin && (
                            <Button
                              variant="outline"
                              size="sm"
                              className="h-6 text-xs px-2"
                              disabled={
                                applyPerms.isPending ||
                                previewApply.isPending ||
                                !personaHasGroups.get(p)
                              }
                              onClick={() => handleApplyClick(p)}
                            >
                              {applyingPersona === p ? (
                                <>
                                  <Loader2 className="h-3 w-3 animate-spin mr-1" />
                                  Applying...
                                </>
                              ) : previewApply.isPending &&
                                confirmPersona === p ? (
                                <>
                                  <Loader2 className="h-3 w-3 animate-spin mr-1" />
                                  Preview...
                                </>
                              ) : (
                                <>
                                  <Play className="h-3 w-3 mr-1" />
                                  Apply
                                </>
                              )}
                            </Button>
                          )}
                        </div>
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {effectiveGroups.map((group) => {
                    const groupTypes = group.types.filter((t) =>
                      matrix.resource_types.includes(t),
                    );
                    if (groupTypes.length === 0) return null;
                    return (
                      <Fragment key={group.name}>
                        {/* Group header row */}
                        <TableRow className="bg-muted/60 hover:bg-muted/60 border-t-2 border-border">
                          <TableCell
                            colSpan={matrix.personas.length + 1}
                            className="font-semibold text-sm py-2 sticky left-0 text-foreground"
                          >
                            {group.name}
                          </TableCell>
                        </TableRow>
                        {/* Resource type rows within the group */}
                        {groupTypes.map((rt) => (
                          <TableRow key={rt}>
                            <TableCell className="font-medium sticky left-0 bg-background z-10 pl-6">
                              <Link
                                to="/resources"
                                search={{ type: rt }}
                                className="flex items-center gap-1 hover:underline"
                              >
                                {matrix.resource_type_labels[rt]}
                                <ExternalLink className="h-3 w-3 text-muted-foreground" />
                              </Link>
                            </TableCell>
                            {matrix.personas.map((p) => {
                              const level = getPermLevel(p, rt);
                              return (
                                <TableCell
                                  key={p}
                                  className="text-center p-1"
                                >
                                  {isAdmin ? (
                                    <Select
                                      value={level}
                                      onValueChange={(val) => {
                                        updateMatrix.mutate({
                                          params: {},
                                          data: {
                                            persona: p,
                                            resource_type: rt as ResourceType,
                                            permission_level:
                                              val as (typeof PermissionLevel)[keyof typeof PermissionLevel],
                                          },
                                        });
                                      }}
                                    >
                                      <SelectTrigger
                                        className={cn(
                                          "h-8 text-xs border",
                                          getPermissionColor(level),
                                        )}
                                      >
                                        <SelectValue>
                                          {getPermissionLabel(level)}
                                        </SelectValue>
                                      </SelectTrigger>
                                      <SelectContent>
                                        {(
                                          matrix.allowed_permission_levels[rt] ??
                                          Object.values(PermissionLevel)
                                        ).map((pl) => (
                                          <SelectItem key={pl} value={pl}>
                                            <span
                                              className={cn(
                                                "inline-block w-2 h-2 rounded-full mr-2",
                                                pl ===
                                                  PermissionLevel.NO_PERMISSIONS
                                                  ? "bg-muted-foreground"
                                                  : pl ===
                                                        PermissionLevel.CAN_MANAGE ||
                                                      pl ===
                                                        PermissionLevel.IS_OWNER
                                                    ? "bg-red-500"
                                                    : pl ===
                                                          PermissionLevel.CAN_VIEW ||
                                                        pl ===
                                                          PermissionLevel.CAN_READ
                                                      ? "bg-green-500"
                                                      : "bg-yellow-500",
                                              )}
                                            />
                                            {getPermissionLabel(pl)}
                                          </SelectItem>
                                        ))}
                                      </SelectContent>
                                    </Select>
                                  ) : (
                                    <Badge
                                      variant="outline"
                                      className={cn(
                                        "text-xs",
                                        getPermissionColor(level),
                                      )}
                                    >
                                      {getPermissionLabel(level)}
                                    </Badge>
                                  )}
                                  {(matrix.overridable_resource_types ?? []).includes(rt) && (
                                    <PerSpaceOverrides
                                      persona={p}
                                      resourceType={rt}
                                      allLevel={level}
                                      matrix={matrix}
                                      isAdmin={isAdmin}
                                    />
                                  )}
                                </TableCell>
                              );
                            })}
                          </TableRow>
                        ))}
                      </Fragment>
                    );
                  })}
                </TableBody>
              </Table>
          </div>
        </CardContent>
      </Card>

      {/* Confirmation Dialog — dry-run preview + per-type scoping */}
      <AlertDialog
        open={confirmPersona !== null}
        onOpenChange={(open) => {
          if (!open) closeConfirm();
        }}
      >
        <AlertDialogContent className="max-w-4xl max-h-[90vh] overflow-y-auto">
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <Shield className="h-5 w-5" />
              Apply Permissions for{" "}
              {confirmPersona ? matrix.persona_labels[confirmPersona] : ""}?
            </AlertDialogTitle>
            <AlertDialogDescription>
              This <span className="font-medium text-foreground">rewrites ACLs
              across the workspace</span> for the resource types you select
              below. Every matching resource has this persona's group
              {previewData && previewData.group_count !== 1 ? "s" : ""}{" "}
              {previewData ? (
                <span className="font-medium text-foreground">
                  ({previewData.groups.join(", ")})
                </span>
              ) : null}{" "}
              set to its <span className="font-medium text-foreground">effective
              level</span>: personas stack, so each group gets the highest level
              any of its personas (or Genie access bundles) grants on each
              object. Deselect any type you do not want to change. Nothing is
              written until you confirm.
            </AlertDialogDescription>
          </AlertDialogHeader>

          {/* Loading state — dry-run preview in flight (no writes) */}
          {previewApply.isPending && (
            <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Computing blast radius (dry run — no changes made)...
            </div>
          )}

          {/* Error state */}
          {previewApply.isError && !previewApply.isPending && (
            <Alert className="border-destructive/50 bg-destructive/5">
              <AlertCircle className="h-4 w-4 text-destructive" />
              <AlertTitle className="text-sm text-destructive">
                Could not compute preview
              </AlertTitle>
              <AlertDescription className="text-xs">
                {previewApply.error?.message ??
                  "The dry-run preview failed. No changes were made."}
              </AlertDescription>
            </Alert>
          )}

          {previewData && !previewApply.isPending && (
            <>
              {confirmConflictCount > 0 && (
                <Alert className="border-amber-500/50 bg-amber-500/5">
                  <AlertTriangle className="h-4 w-4 text-amber-600" />
                  <AlertTitle className="text-sm text-amber-700 dark:text-amber-400">
                    {confirmConflictCount} user
                    {confirmConflictCount !== 1 ? "s" : ""} with cross-persona
                    conflicts
                  </AlertTitle>
                  <AlertDescription className="text-xs">
                    Some users in this persona's groups also belong to groups
                    with higher permissions from other personas. Databricks
                    resolves a multi-group user to the highest level at access
                    time.
                  </AlertDescription>
                </Alert>
              )}

              {/* Per-type blast radius with checkboxes */}
              {previewData.plan.length > 0 ? (
                <div className="max-h-64 overflow-y-auto rounded-md border">
                  {previewData.plan.map((p) => (
                    <label
                      key={p.resource_type}
                      className="flex items-center gap-3 px-3 py-2 border-b last:border-b-0 hover:bg-muted/50 cursor-pointer"
                    >
                      <input
                        type="checkbox"
                        className="h-4 w-4 shrink-0 accent-primary cursor-pointer"
                        checked={selectedTypes.has(p.resource_type)}
                        onChange={() => toggleType(p.resource_type)}
                      />
                      <span className="flex-1 text-sm font-medium">
                        {p.resource_type_label}
                      </span>
                      <span className="text-xs text-muted-foreground tabular-nums">
                        {p.resource_count} resource
                        {p.resource_count !== 1 ? "s" : ""}
                      </span>
                      <Badge
                        variant="outline"
                        className={cn(
                          "text-xs",
                          getPermissionColor(p.target_level),
                        )}
                      >
                        {getPermissionLabel(p.target_level)}
                      </Badge>
                    </label>
                  ))}
                </div>
              ) : (
                <Alert>
                  <AlertCircle className="h-4 w-4" />
                  <AlertTitle className="text-sm">
                    Nothing to apply
                  </AlertTitle>
                  <AlertDescription className="text-xs">
                    No resource type in this persona's template would be
                    written (all are at No Permissions or unsupported).
                  </AlertDescription>
                </Alert>
              )}

              {/* Skipped types (not written), for transparency */}
              {previewData.skipped.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  <span className="font-medium">Not applied:</span>{" "}
                  {previewData.skipped
                    .map(
                      (s) =>
                        `${s.resource_type_label} (${skipReasonLabel(s.reason)})`,
                    )
                    .join(", ")}
                </p>
              )}

              <StackedPreviewDetail
                preview={previewData}
                selectedTypes={selectedTypes}
              />

              {/* Direct-user re-sync count */}
              {(previewData.direct_user_count ?? 0) > 0 && (
                <p className="text-xs text-muted-foreground flex items-center gap-1">
                  <Users className="h-3 w-3 shrink-0" />
                  Also re-syncing{" "}
                  <strong className="text-foreground">
                    {previewData.direct_user_count}
                  </strong>{" "}
                  directly-assigned user
                  {previewData.direct_user_count !== 1 ? "s" : ""} for this
                  persona.
                </p>
              )}

              {/* Selection summary */}
              {previewData.plan.length > 0 && (
                <p className="text-sm">
                  Will rewrite ACLs on{" "}
                  <strong>{selectedResourceCount}</strong> resource
                  {selectedResourceCount !== 1 ? "s" : ""} across{" "}
                  <strong>{selectedTypes.size}</strong> selected type
                  {selectedTypes.size !== 1 ? "s" : ""}.
                </p>
              )}
            </>
          )}

          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirmApply}
              disabled={
                previewApply.isPending ||
                !previewData ||
                selectedTypes.size === 0
              }
            >
              Apply to {selectedTypes.size} type
              {selectedTypes.size !== 1 ? "s" : ""}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Conflicts Detail Dialog */}
      <Dialog open={showConflictsDialog} onOpenChange={setShowConflictsDialog}>
        <DialogContent className="max-w-3xl max-h-[80vh] overflow-hidden flex flex-col">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-amber-600" />
              Permission Conflicts ({conflicts?.users_with_conflicts ?? 0} user
              {(conflicts?.users_with_conflicts ?? 0) !== 1 ? "s" : ""})
            </DialogTitle>
            <DialogDescription>
              These users belong to groups mapped to multiple personas with
              differing permission levels on the same resource types. When
              permissions are applied, conflicts are automatically resolved by
              assigning the <strong className="text-foreground">highest</strong>{" "}
              permission level.
            </DialogDescription>
          </DialogHeader>

          <ScrollArea className="flex-1 -mx-6 px-6">
            <div className="space-y-3 pb-2">
              {conflicts?.conflicts.map((c) => (
                <ConflictUserRow key={c.user_id} conflict={c} />
              ))}
              {(!conflicts || conflicts.conflicts.length === 0) && (
                <p className="text-sm text-muted-foreground text-center py-8">
                  No conflicts found.
                </p>
              )}
            </div>
          </ScrollArea>
        </DialogContent>
      </Dialog>
    </div>
  );
}

const INHERIT = "__all__";

/** Per-space Genie levels for one persona: each space either follows the
 * persona's "all spaces" cell or overrides it (higher OR lower). Nothing is
 * written to the workspace until the persona is applied. */
function PerSpaceOverrides({
  persona,
  resourceType,
  allLevel,
  matrix,
  isAdmin,
}: {
  persona: string;
  resourceType: string;
  allLevel: string;
  matrix: PermissionMatrixOut;
  isAdmin: boolean;
}) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const overrides = (matrix.overrides ?? []).filter(
    (o) => o.persona === persona && o.resource_type === resourceType,
  );
  const byId = new Map(overrides.map((o) => [o.resource_id, o.permission_level]));
  const spaces = useListResourcesByType({
    params: { resource_type: resourceType },
    query: { enabled: open, staleTime: 60_000 },
  });
  const setOverride = useSetPermissionOverride({
    mutation: {
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: getPermissionMatrixKey() });
      },
      onError: (error) => toast.error(`Failed to set per-space level: ${error.message}`),
    },
  });
  const levels = matrix.allowed_permission_levels[resourceType] ?? [];
  const label = matrix.persona_labels[persona] ?? persona;

  return (
    <>
      <button
        type="button"
        className="mt-1 text-[11px] text-muted-foreground underline-offset-2 hover:underline disabled:no-underline"
        onClick={() => setOpen(true)}
        title="Override this level for individual Genie spaces"
      >
        {overrides.length > 0 ? `${overrides.length} per-space override${overrides.length !== 1 ? "s" : ""}` : "per space…"}
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-2xl max-h-[80vh] overflow-hidden flex flex-col">
          <DialogHeader>
            <DialogTitle>{label}: Genie level per space</DialogTitle>
            <DialogDescription>
              By default every space gets this persona's "all spaces" level (
              <strong>{getPermissionLabel(allLevel)}</strong>). Override it for
              individual spaces — higher or lower; No Permissions means this
              persona grants nothing there. Groups with several personas still
              get the highest level any of them grants. Can View / Can Run or
              higher also grants the group SELECT on the space's tables when the
              persona is applied.
            </DialogDescription>
          </DialogHeader>
          <ScrollArea className="flex-1 -mx-6 px-6">
            {spaces.isLoading && (
              <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading Genie spaces...
              </div>
            )}
            {spaces.isError && (
              <p className="py-6 text-sm text-destructive">Could not list Genie spaces.</p>
            )}
            <div className="space-y-1 pb-2">
              {[
                ...(spaces.data?.data ?? []),
                // Overrides for spaces that are no longer listed stay visible.
                ...overrides
                  .filter((o) => !(spaces.data?.data ?? []).some((sp) => sp.id === o.resource_id))
                  .map((o) => ({ id: o.resource_id, name: `${o.resource_id} (not listed)`, resource_type: resourceType })),
              ].map((sp) => {
                const value = byId.get(sp.id) ?? INHERIT;
                return (
                  <div key={sp.id} className="flex items-center justify-between gap-3 rounded border px-2 py-1.5">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">{sp.name}</p>
                      <p className="truncate text-[11px] text-muted-foreground font-mono">{sp.id}</p>
                    </div>
                    {isAdmin ? (
                      <Select
                        value={value}
                        disabled={setOverride.isPending}
                        onValueChange={(val) =>
                          setOverride.mutate({
                            params: {},
                            data: {
                              persona,
                              resource_type: resourceType as ResourceType,
                              resource_id: sp.id,
                              permission_level:
                                val === INHERIT
                                  ? null
                                  : (val as (typeof PermissionLevel)[keyof typeof PermissionLevel]),
                            },
                          })
                        }
                      >
                        <SelectTrigger className={cn("h-8 w-56 text-xs", value !== INHERIT && getPermissionColor(value))}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value={INHERIT}>
                            All-spaces level ({getPermissionLabel(allLevel)})
                          </SelectItem>
                          {levels.map((pl) => (
                            <SelectItem key={pl} value={pl}>
                              {getPermissionLabel(pl)}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : (
                      <Badge variant="outline" className="text-xs">
                        {value === INHERIT ? `All spaces (${getPermissionLabel(allLevel)})` : getPermissionLabel(value)}
                      </Badge>
                    )}
                  </div>
                );
              })}
            </div>
          </ScrollArea>
        </DialogContent>
      </Dialog>
    </>
  );
}

const GRANT_ACTION_STYLE: Record<string, string> = {
  add: "bg-green-500/15 text-green-700 dark:text-green-400 border-green-500/30",
  present: "bg-muted text-muted-foreground",
  keep: "bg-muted text-muted-foreground",
  revoke: "bg-red-500/15 text-red-700 dark:text-red-400 border-red-500/30",
  release: "bg-muted text-muted-foreground",
  error: "bg-red-500/15 text-red-700 dark:text-red-400 border-red-500/30",
};

/** Least-privilege detail of the dry-run: per group/object effective level and
 * who grants it, escalations, and the Genie table grants (add/keep/revoke). */
function StackedPreviewDetail({
  preview,
  selectedTypes,
}: {
  preview: ApplyPreviewOut;
  selectedTypes: Set<string>;
}) {
  const levels = (preview.group_levels ?? []).filter((l) => selectedTypes.has(l.resource_type));
  const grants = selectedTypes.has("genie") ? preview.table_grants ?? [] : [];
  const escalations = levels.filter((l) => l.escalation);
  const warnings = preview.warnings ?? [];
  if (!levels.length && !grants.length && !warnings.length) return null;
  const counts = grants.reduce<Record<string, number>>((acc, g) => {
    acc[g.action] = (acc[g.action] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="space-y-3">
      {escalations.length > 0 && (
        <Alert className="border-amber-500/50 bg-amber-500/5">
          <ArrowUp className="h-4 w-4 text-amber-600" />
          <AlertTitle className="text-sm text-amber-700 dark:text-amber-400">
            {escalations.length} escalation{escalations.length !== 1 ? "s" : ""} from stacking
          </AlertTitle>
          <AlertDescription className="text-xs space-y-0.5">
            {escalations.map((l) => (
              <p key={`${l.group_id}:${l.resource_type}:${l.resource_id ?? "*"}`}>
                <strong>{l.group_name}</strong> · {l.resource_type_label}
                {l.resource_name ? ` · ${l.resource_name}` : ""}: {l.escalation_detail}
              </p>
            ))}
          </AlertDescription>
        </Alert>
      )}
      {warnings.length > 0 && (
        <div className="space-y-1 rounded-md border border-amber-500/40 bg-amber-500/5 p-2 text-xs">
          {warnings.map((w) => (
            <p key={w} className="flex gap-1.5">
              <AlertTriangle className="h-3 w-3 mt-0.5 shrink-0 text-amber-600" />
              {w}
            </p>
          ))}
        </div>
      )}
      {levels.length > 0 && (
        <details className="rounded-md border" open={levels.length <= 12}>
          <summary className="cursor-pointer px-3 py-2 text-sm font-medium">
            Effective access per group ({levels.length})
          </summary>
          <div className="max-h-64 overflow-auto">
            <table className="w-full text-xs">
              <thead className="bg-muted/50 text-left sticky top-0">
                <tr>
                  <th className="p-1.5">Group</th>
                  <th className="p-1.5">Object</th>
                  <th className="p-1.5">Effective</th>
                  <th className="p-1.5">Given by</th>
                  <th className="p-1.5">All sources</th>
                </tr>
              </thead>
              <tbody>
                {levels.map((l) => (
                  <tr
                    key={`${l.group_id}:${l.resource_type}:${l.resource_id ?? "*"}`}
                    className={cn("border-t", l.escalation && "bg-amber-500/5")}
                  >
                    <td className="p-1.5 font-medium">{l.group_name}</td>
                    <td className="p-1.5">
                      {l.resource_type_label}
                      {l.resource_name ? ` · ${l.resource_name}` : l.resource_id ? "" : " (all)"}
                    </td>
                    <td className="p-1.5">
                      {l.blocked ? (
                        <span className="text-destructive" title={l.blocked}>not changed</span>
                      ) : (
                        <Badge variant="outline" className={cn("text-[10px]", getPermissionColor(l.effective_level))}>
                          {getPermissionLabel(l.effective_level)}
                        </Badge>
                      )}
                    </td>
                    <td className="p-1.5">{(l.granted_by ?? []).join(", ") || "—"}</td>
                    <td className="p-1.5 text-muted-foreground">
                      {Object.entries(l.source_levels ?? {})
                        .map(([src, lvl]) => `${src}: ${getPermissionLabel(lvl)}`)
                        .join("; ")}
                      {l.blocked ? <span className="block text-destructive">{l.blocked}</span> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
      {grants.length > 0 && (
        <details className="rounded-md border" open>
          <summary className="cursor-pointer px-3 py-2 text-sm font-medium">
            Genie table grants — {Object.entries(counts).map(([a, n]) => `${n} ${a}`).join(", ")}
          </summary>
          <div className="max-h-64 overflow-auto">
            <table className="w-full text-xs">
              <thead className="bg-muted/50 text-left sticky top-0">
                <tr>
                  <th className="p-1.5">Group</th>
                  <th className="p-1.5">Space</th>
                  <th className="p-1.5">Grant</th>
                  <th className="p-1.5">Action</th>
                  <th className="p-1.5">Notes</th>
                </tr>
              </thead>
              <tbody>
                {grants.map((g) => (
                  <tr key={`${g.group_id}:${g.source}:${g.securable_type}:${g.full_name}:${g.privilege}`} className="border-t">
                    <td className="p-1.5 font-medium">{g.group_name}</td>
                    <td className="p-1.5">{g.space_title || g.space_id}</td>
                    <td className="p-1.5 font-mono">
                      {g.privilege} on {g.securable_type} {g.full_name}
                    </td>
                    <td className="p-1.5">
                      <Badge variant="outline" className={cn("text-[10px]", GRANT_ACTION_STYLE[g.action])}>
                        {g.action}
                      </Badge>
                    </td>
                    <td className="p-1.5 text-muted-foreground">
                      {g.has_row_filter && <Badge variant="outline" className="mr-1 text-[10px]">row filter</Badge>}
                      {(g.masked_columns ?? []).length > 0 && (
                        <Badge variant="outline" className="mr-1 text-[10px]">
                          masked: {(g.masked_columns ?? []).join(", ")}
                        </Badge>
                      )}
                      {g.detail}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  );
}

function PermissionsSkeleton() {
  return (
    <div className="space-y-6">
      <div>
        <Skeleton className="h-9 w-56" />
        <Skeleton className="h-4 w-96 mt-2" />
      </div>
      <Card>
        <CardContent className="pt-4">
          <Skeleton className="h-8 w-96" />
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <Skeleton className="h-6 w-64" />
          <Skeleton className="h-4 w-96" />
        </CardHeader>
        <CardContent>
          <div className="space-y-2">
            {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function PermissionsPage() {
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
                  Failed to Load Permissions Matrix
                </CardTitle>
                <CardDescription>
                  Could not fetch the permissions matrix. Make sure the backend
                  is running.
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
          <Suspense fallback={<PermissionsSkeleton />}>
            <PermissionsContent />
          </Suspense>
        </ErrorBoundary>
      )}
    </QueryErrorResetBoundary>
  );
}
