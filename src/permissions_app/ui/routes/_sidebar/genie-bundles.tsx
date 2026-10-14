import { createFileRoute } from "@tanstack/react-router";
import { Suspense, useState } from "react";
import { QueryErrorResetBoundary, useQueryClient } from "@tanstack/react-query";
import { ErrorBoundary } from "react-error-boundary";
import {
  ApiError,
  useListGenieBundlesSuspense,
  useListResourcesByType,
  useListUsers,
  useCreateGenieBundle,
  usePreviewGenieBundle,
  useApplyGenieBundle,
  useResyncGenieBundle,
  useDeleteGenieBundle,
  useListGenieBundleMembers,
  useAddGenieBundleMember,
  useRemoveGenieBundleMember,
  listGenieBundlesKey,
  listGenieBundleMembersKey,
  previewGenieBundleKey,
  type GenieBundleOut,
  type GenieBundleActionOut,
  type GenieBundlePreviewOut,
  type GenieGrantOut,
  type GroupMemberOut,
  type ResourceItemOut,
  type UserOut,
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
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  AlertCircle,
  Eye,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
  UserMinus,
  UserPlus,
  Users,
} from "lucide-react";
import { toast } from "sonner";
import { useAdmin } from "@/hooks/use-admin";

export const Route = createFileRoute("/_sidebar/genie-bundles")({
  component: () => <GenieBundlesPage />,
});

/** Surface the backend's human-readable `detail` (e.g. "workspace-local group")
 * instead of the generic "HTTP 400" status text. */
function apiDetail(error: ApiError): string {
  const detail = (error.body as { detail?: unknown } | null)?.detail;
  return typeof detail === "string" ? detail : error.message;
}

const GRANT_STATUS_STYLE: Record<string, string> = {
  missing: "border-blue-500/50 text-blue-700 dark:text-blue-400",
  present: "text-muted-foreground",
  managed: "border-green-500/50 text-green-700 dark:text-green-400",
  stale: "border-amber-500/50 text-amber-700 dark:text-amber-400",
  error: "border-destructive/50 text-destructive",
};

function GrantStatus({ status }: { status: string }) {
  return (
    <Badge variant="outline" className={GRANT_STATUS_STYLE[status] ?? ""}>
      {status}
    </Badge>
  );
}

function GrantsTable({ grants }: { grants: GenieGrantOut[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Securable</TableHead>
          <TableHead>Privilege</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Detail</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {grants.map((g) => (
          <TableRow key={`${g.securable_type}:${g.full_name}:${g.privilege}`}>
            <TableCell className="font-mono text-xs">
              <span className="text-muted-foreground">{g.securable_type}</span>{" "}
              {g.full_name}
            </TableCell>
            <TableCell className="font-mono text-xs">{g.privilege}</TableCell>
            <TableCell>
              <GrantStatus status={g.status} />
            </TableCell>
            <TableCell className="text-xs text-muted-foreground">
              {g.detail ?? ""}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function ActionResult({
  result,
  onDismiss,
}: {
  result: GenieBundleActionOut;
  onDismiss: () => void;
}) {
  const errors = result.errors ?? [];
  const warnings = result.warnings ?? [];
  const failed = result.status !== "ok";
  return (
    <Alert
      className={
        failed
          ? "border-destructive/50 bg-destructive/5"
          : "border-green-500/50 bg-green-500/5"
      }
    >
      <AlertTitle>
        Bundle #{result.bundle_id} {result.action}: {result.status}
      </AlertTitle>
      <AlertDescription className="space-y-2">
        <div className="text-sm">
          {(result.granted ?? []).length} granted ·{" "}
          {(result.adopted ?? []).length} already present ·{" "}
          {(result.revoked ?? []).length} revoked ·{" "}
          {(result.released ?? []).length} released
          {(result.reconciled ?? []).length > 0
            ? ` · ${(result.reconciled ?? []).length} interrupted grant(s) reconciled`
            : ""}
          {result.deleted ? " · bundle deleted" : ""}
        </div>
        {errors.length > 0 && (
          <ul className="list-disc pl-5 text-sm text-destructive">
            {errors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        )}
        {warnings.length > 0 && (
          <ul className="list-disc pl-5 text-sm text-muted-foreground">
            {warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        )}
        <Button variant="outline" size="sm" onClick={onDismiss}>
          Dismiss
        </Button>
      </AlertDescription>
    </Alert>
  );
}

function GenieBundlesContent() {
  const { data: bundles } = useListGenieBundlesSuspense(selector());
  const { isAdmin } = useAdmin();
  const [lastResult, setLastResult] = useState<GenieBundleActionOut | null>(
    null,
  );

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">
            Genie Access Bundles
          </h1>
          <p className="text-muted-foreground mt-1">
            One account group per Genie space. Applying a bundle grants the
            group CAN_RUN on the space plus SELECT on each table it uses (with
            USE_SCHEMA / USE_CATALOG). Onboard people by adding them to the
            group.
          </p>
        </div>
        {isAdmin && <CreateBundleDialog />}
      </div>

      {lastResult && (
        <ActionResult result={lastResult} onDismiss={() => setLastResult(null)} />
      )}

      <Card>
        <CardHeader>
          <CardTitle>Bundles ({bundles.length})</CardTitle>
          <CardDescription>
            Preview is read-only. Resync re-reads the space, grants new tables
            and revokes only grants this app created that no other bundle
            needs.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {bundles.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No bundles yet. Ask the client to create an account-level group
              (e.g. <code>genie-sales-users</code>), then create a bundle for
              it.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Genie space</TableHead>
                  <TableHead>Account group</TableHead>
                  <TableHead>Ledger</TableHead>
                  <TableHead>Last applied</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {bundles.map((b) => (
                  <BundleRow
                    key={b.id}
                    bundle={b}
                    isAdmin={isAdmin}
                    onResult={setLastResult}
                  />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function BundleRow({
  bundle,
  isAdmin,
  onResult,
}: {
  bundle: GenieBundleOut;
  isAdmin: boolean;
  onResult: (r: GenieBundleActionOut) => void;
}) {
  const queryClient = useQueryClient();
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: listGenieBundlesKey() });
    queryClient.invalidateQueries({
      queryKey: previewGenieBundleKey({ bundle_id: bundle.id }),
    });
  };
  const handlers = {
    onSuccess: (res: { data: GenieBundleActionOut }) => {
      onResult(res.data);
      if (res.data.status === "ok") {
        toast.success(`Bundle ${res.data.action} complete`);
      } else {
        toast.error(
          `Bundle ${res.data.action} ${res.data.status}: ${(res.data.errors ?? []).length} error(s)`,
        );
      }
      refresh();
    },
    onError: (error: ApiError) => {
      toast.error(apiDetail(error));
    },
  };
  const apply = useApplyGenieBundle({ mutation: handlers });
  const resync = useResyncGenieBundle({ mutation: handlers });
  const remove = useDeleteGenieBundle({
    mutation: {
      onSuccess: handlers.onSuccess,
      onError: (error: ApiError) => {
        const detail = apiDetail(error);
        // The group was deleted: offer the record-only escape hatch (the
        // backend re-checks that the group id is really gone).
        if (detail.includes("forget orphaned")) {
          toast.error(detail, {
            duration: 20000,
            action: {
              label: "Forget orphaned bundle",
              onClick: () =>
                remove.mutate({
                  params: { bundle_id: bundle.id, forget_orphaned: true },
                }),
            },
          });
        } else {
          toast.error(detail);
        }
      },
    },
  });
  const busy = apply.isPending || resync.isPending || remove.isPending;

  return (
    <TableRow>
      <TableCell>
        <div className="font-medium">{bundle.space_title}</div>
        <div className="text-xs text-muted-foreground font-mono">
          {bundle.space_id}
        </div>
      </TableCell>
      <TableCell className="font-mono text-sm">{bundle.group_name}</TableCell>
      <TableCell>
        <Badge variant="secondary">{bundle.ledger_count ?? 0} grants</Badge>
      </TableCell>
      <TableCell className="text-sm text-muted-foreground">
        {bundle.last_applied_at
          ? `${new Date(bundle.last_applied_at).toLocaleString()} by ${bundle.last_applied_by ?? ""}`
          : "never"}
      </TableCell>
      <TableCell>
        <div className="flex justify-end gap-2">
          <PreviewDialog
            bundle={bundle}
            isAdmin={isAdmin}
            applying={apply.isPending}
            onApply={() => apply.mutate({ params: { bundle_id: bundle.id } })}
          />
          {isAdmin && (
            <>
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => resync.mutate({ params: { bundle_id: bundle.id } })}
              >
                <RefreshCw className="h-3 w-3 mr-1" />
                {resync.isPending ? "Resyncing..." : "Resync"}
              </Button>
              <MembersDialog bundle={bundle} />
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button variant="outline" size="sm" disabled={busy}>
                    <Trash2 className="h-3 w-3" />
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Delete this bundle?</AlertDialogTitle>
                    <AlertDialogDescription>
                      Revokes the grants this app created for{" "}
                      <strong>{bundle.group_name}</strong> on{" "}
                      <strong>{bundle.space_title}</strong>, except grants
                      another bundle still needs. Grants the app did not create
                      are left in place. The account group itself is not
                      deleted.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancel</AlertDialogCancel>
                    <AlertDialogAction
                      onClick={() =>
                        remove.mutate({ params: { bundle_id: bundle.id } })
                      }
                    >
                      Delete bundle
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            </>
          )}
        </div>
      </TableCell>
    </TableRow>
  );
}

function PreviewDialog({
  bundle,
  isAdmin,
  applying,
  onApply,
}: {
  bundle: GenieBundleOut;
  isAdmin: boolean;
  applying: boolean;
  onApply: () => void;
}) {
  const [open, setOpen] = useState(false);
  const preview = usePreviewGenieBundle({
    params: { bundle_id: bundle.id },
    query: { enabled: open, ...selector<GenieBundlePreviewOut>().query },
  });
  const data = preview.data;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Eye className="h-3 w-3 mr-1" />
          Preview
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-4xl">
        <DialogHeader>
          <DialogTitle>Preview: {bundle.space_title}</DialogTitle>
          <DialogDescription>
            Read-only. Nothing is changed until you click Apply.
          </DialogDescription>
        </DialogHeader>
        {preview.isLoading && <Skeleton className="h-48 w-full" />}
        {preview.error && (
          <Alert className="border-destructive/50 bg-destructive/5">
            <AlertTitle>Preview failed</AlertTitle>
            <AlertDescription>{apiDetail(preview.error)}</AlertDescription>
          </Alert>
        )}
        {data && (
          <ScrollArea className="max-h-[65vh] pr-4">
            <div className="space-y-4">
              <div className="text-sm">
                Group <code>{data.group.group_name}</code>:{" "}
                {data.group.is_account_group ? (
                  <Badge variant="outline">account group</Badge>
                ) : (
                  <Badge variant="destructive">not usable</Badge>
                )}
              </div>
              {(data.blockers ?? []).length > 0 && (
                <Alert className="border-destructive/50 bg-destructive/5">
                  <AlertTitle className="text-destructive">Blockers</AlertTitle>
                  <AlertDescription>
                    <ul className="list-disc pl-5 text-sm">
                      {(data.blockers ?? []).map((b) => (
                        <li key={b}>{b}</li>
                      ))}
                    </ul>
                  </AlertDescription>
                </Alert>
              )}
              {(data.warnings ?? []).length > 0 && (
                <Alert className="border-amber-500/50 bg-amber-500/5">
                  <AlertTitle className="text-amber-700 dark:text-amber-400">
                    Warnings
                  </AlertTitle>
                  <AlertDescription>
                    <ul className="list-disc pl-5 text-sm">
                      {(data.warnings ?? []).map((w) => (
                        <li key={w}>{w}</li>
                      ))}
                    </ul>
                  </AlertDescription>
                </Alert>
              )}

              <div>
                <h3 className="font-medium mb-2">
                  Data sources ({data.objects.length})
                </h3>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Object</TableHead>
                      <TableHead>Type</TableHead>
                      <TableHead>Row filter</TableHead>
                      <TableHead>Column masks</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.objects.map((o) => (
                      <TableRow key={o.identifier}>
                        <TableCell className="font-mono text-xs">
                          {o.full_name ?? o.identifier}
                          {!o.resolved && (
                            <div className="text-destructive font-sans">
                              Unresolved: {o.reason}
                            </div>
                          )}
                        </TableCell>
                        <TableCell className="text-xs">
                          {o.table_type ?? o.kind}
                        </TableCell>
                        <TableCell className="text-xs">
                          {o.has_row_filter ? (
                            <Badge variant="outline">
                              {o.row_filter_function ?? "yes"}
                            </Badge>
                          ) : (
                            "none"
                          )}
                        </TableCell>
                        <TableCell className="text-xs">
                          {(o.masked_columns ?? []).length > 0
                            ? (o.masked_columns ?? []).join(", ")
                            : "none"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>

              <div>
                <h3 className="font-medium mb-2">
                  Required grants ({data.grants.length})
                </h3>
                <GrantsTable grants={data.grants} />
              </div>

              {(data.stale_grants ?? []).length > 0 && (
                <div>
                  <h3 className="font-medium mb-2">
                    No longer needed (released on Resync)
                  </h3>
                  <GrantsTable grants={data.stale_grants ?? []} />
                </div>
              )}
            </div>
          </ScrollArea>
        )}
        {isAdmin && data && (
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="outline" onClick={() => setOpen(false)}>
              Close
            </Button>
            <Button
              disabled={!data.can_apply || applying}
              onClick={() => {
                onApply();
                setOpen(false);
              }}
            >
              <Sparkles className="h-4 w-4 mr-1" />
              {applying ? "Applying..." : "Apply"}
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function CreateBundleDialog() {
  const [open, setOpen] = useState(false);
  const [spaceId, setSpaceId] = useState("");
  const [groupName, setGroupName] = useState("");
  const queryClient = useQueryClient();
  const spaces = useListResourcesByType({
    params: { resource_type: "genie" },
    query: { enabled: open, ...selector<ResourceItemOut[]>().query },
  });
  const create = useCreateGenieBundle({
    mutation: {
      onSuccess: (res) => {
        toast.success(
          `Bundle created for ${res.data.group_name}. Preview it before applying.`,
        );
        setOpen(false);
        setSpaceId("");
        setGroupName("");
        queryClient.invalidateQueries({ queryKey: listGenieBundlesKey() });
      },
      onError: (error) => {
        toast.error(apiDetail(error));
      },
    },
  });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <Plus className="h-4 w-4 mr-1" />
          New bundle
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New Genie access bundle</DialogTitle>
          <DialogDescription>
            The group must already exist as an ACCOUNT-level group assigned to
            this workspace. UC Central does not create groups, and
            workspace-local groups are refused (Unity Catalog cannot grant to
            them).
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 pt-2">
          <div className="space-y-2">
            <Label>Genie space</Label>
            <Select value={spaceId} onValueChange={setSpaceId}>
              <SelectTrigger>
                <SelectValue
                  placeholder={spaces.isLoading ? "Loading..." : "Select a space..."}
                />
              </SelectTrigger>
              <SelectContent>
                {(spaces.data ?? []).map((s) => (
                  <SelectItem key={s.id} value={s.id}>
                    {s.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label>Account group name</Label>
            <Input
              placeholder="genie-sales-users"
              value={groupName}
              onChange={(e) => setGroupName(e.target.value)}
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={!spaceId || !groupName.trim() || create.isPending}
              onClick={() =>
                create.mutate({
                  params: {},
                  data: { space_id: spaceId, group_name: groupName.trim() },
                })
              }
            >
              {create.isPending ? "Creating..." : "Create bundle"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function MembersDialog({ bundle }: { bundle: GenieBundleOut }) {
  const [open, setOpen] = useState(false);
  const [userId, setUserId] = useState("");
  const queryClient = useQueryClient();
  const membersKey = listGenieBundleMembersKey({ bundle_id: bundle.id });
  const members = useListGenieBundleMembers({
    params: { bundle_id: bundle.id },
    query: { enabled: open, ...selector<GroupMemberOut[]>().query },
  });
  const users = useListUsers({
    query: { enabled: open, ...selector<UserOut[]>().query },
  });
  const onDone = (verb: string) => () => {
    toast.success(`Member ${verb}`);
    setUserId("");
    queryClient.invalidateQueries({ queryKey: membersKey });
  };
  const add = useAddGenieBundleMember({
    mutation: {
      onSuccess: onDone("added"),
      onError: (error) => toast.error(apiDetail(error)),
    },
  });
  const remove = useRemoveGenieBundleMember({
    mutation: {
      onSuccess: onDone("removed"),
      onError: (error) => toast.error(apiDetail(error)),
    },
  });
  const memberIds = new Set((members.data ?? []).map((m) => m.user_id));

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Users className="h-3 w-3 mr-1" />
          Members
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Members of {bundle.group_name}</DialogTitle>
          <DialogDescription>
            Adding someone here gives them this Genie space and its data.
            If the workspace refuses changes to this account group, manage
            membership in the account console (User management → Groups →{" "}
            <code>{bundle.group_name}</code>).
          </DialogDescription>
        </DialogHeader>
        {members.error && (
          <Alert className="border-destructive/50 bg-destructive/5">
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>{apiDetail(members.error)}</AlertDescription>
          </Alert>
        )}
        <div className="flex gap-2">
          <Select value={userId} onValueChange={setUserId}>
            <SelectTrigger>
              <SelectValue placeholder="Select a user..." />
            </SelectTrigger>
            <SelectContent>
              {(users.data ?? [])
                .filter((u) => !memberIds.has(u.id))
                .map((u) => (
                  <SelectItem key={u.id} value={u.id}>
                    {u.display_name || u.user_name || u.id}
                  </SelectItem>
                ))}
            </SelectContent>
          </Select>
          <Button
            disabled={!userId || add.isPending}
            onClick={() =>
              add.mutate({
                params: { bundle_id: bundle.id },
                data: { user_id: userId },
              })
            }
          >
            <UserPlus className="h-4 w-4" />
          </Button>
        </div>
        <ScrollArea className="max-h-72">
          {members.isLoading && <Skeleton className="h-24 w-full" />}
          {(members.data ?? []).length === 0 && !members.isLoading && (
            <p className="text-sm text-muted-foreground">No members yet.</p>
          )}
          {(members.data ?? []).map((m) => (
            <div
              key={m.user_id}
              className="flex items-center justify-between py-1"
            >
              <span className="text-sm">{m.display_name || m.user_id}</span>
              <Button
                variant="ghost"
                size="sm"
                disabled={remove.isPending}
                onClick={() =>
                  remove.mutate({
                    params: { bundle_id: bundle.id, user_id: m.user_id },
                  })
                }
              >
                <UserMinus className="h-3 w-3" />
              </Button>
            </div>
          ))}
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}

function GenieBundlesSkeleton() {
  return (
    <div className="space-y-6">
      <div>
        <Skeleton className="h-9 w-64" />
        <Skeleton className="h-4 w-96 mt-2" />
      </div>
      <Card>
        <CardHeader>
          <Skeleton className="h-6 w-48" />
        </CardHeader>
        <CardContent>
          <div className="space-y-3">
            {[1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function GenieBundlesPage() {
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
                  Failed to Load Genie Access Bundles
                </CardTitle>
                <CardDescription>
                  Could not fetch bundles. Make sure you're authenticated and
                  the backend is running.
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
          <Suspense fallback={<GenieBundlesSkeleton />}>
            <GenieBundlesContent />
          </Suspense>
        </ErrorBoundary>
      )}
    </QueryErrorResetBoundary>
  );
}
