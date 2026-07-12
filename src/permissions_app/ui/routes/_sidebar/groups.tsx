import { createFileRoute } from "@tanstack/react-router";
import { Suspense, useState } from "react";
import { QueryErrorResetBoundary, useQueryClient } from "@tanstack/react-query";
import { ErrorBoundary } from "react-error-boundary";
import {
  useListGroupsSuspense,
  useListUsersSuspense,
  useListUnassignedUsersSuspense,
  useAddGroupMember,
  listGroupsKey,
  listUnassignedUsersKey,
  listUsersKey,
  type GroupOut,
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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
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
  Users,
  UserPlus,
  AlertCircle,
  Search,
  UserX,
} from "lucide-react";
import { toast } from "sonner";
import { useAdmin } from "@/hooks/use-admin";

export const Route = createFileRoute("/_sidebar/groups")({
  component: () => <GroupsPage />,
});

function GroupsContent() {
  const { data: groups } = useListGroupsSuspense(selector());
  const { data: users } = useListUsersSuspense(selector());
  const { data: unassigned } = useListUnassignedUsersSuspense(selector());
  const [searchGroups, setSearchGroups] = useState("");
  const [searchUsers, setSearchUsers] = useState("");
  const [searchUnassigned, setSearchUnassigned] = useState("");
  const { isAdmin } = useAdmin();

  const filteredGroups = groups.filter(
    (g) =>
      g.display_name.toLowerCase().includes(searchGroups.toLowerCase()) ||
      g.id.toLowerCase().includes(searchGroups.toLowerCase()),
  );

  const filteredUsers = users.filter(
    (u) =>
      (u.display_name || "")
        .toLowerCase()
        .includes(searchUsers.toLowerCase()) ||
      (u.user_name || "").toLowerCase().includes(searchUsers.toLowerCase()),
  );

  const filteredUnassigned = unassigned.filter(
    (u) =>
      (u.display_name || "")
        .toLowerCase()
        .includes(searchUnassigned.toLowerCase()) ||
      (u.user_name || "")
        .toLowerCase()
        .includes(searchUnassigned.toLowerCase()),
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Users & Groups</h1>
        <p className="text-muted-foreground mt-1">
          Scan workspace groups and users. Assign unassigned users to groups.
        </p>
      </div>

      <Tabs defaultValue="groups">
        <TabsList>
          <TabsTrigger value="groups" className="flex items-center gap-1">
            <Users className="h-4 w-4" />
            Groups ({groups.length})
          </TabsTrigger>
          <TabsTrigger value="users" className="flex items-center gap-1">
            <Users className="h-4 w-4" />
            All Users ({users.length})
          </TabsTrigger>
          <TabsTrigger value="unassigned" className="flex items-center gap-1">
            <UserX className="h-4 w-4" />
            Unassigned ({unassigned.length})
          </TabsTrigger>
        </TabsList>

        {/* Groups Tab */}
        <TabsContent value="groups">
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle>Workspace Groups</CardTitle>
                  <CardDescription>
                    All groups in your Databricks workspace
                  </CardDescription>
                </div>
                <div className="relative w-64">
                  <Search className="absolute left-2 top-2.5 h-4 w-4 text-muted-foreground" />
                  <Input
                    placeholder="Search groups..."
                    className="pl-8"
                    value={searchGroups}
                    onChange={(e) => setSearchGroups(e.target.value)}
                  />
                </div>
              </div>
            </CardHeader>
            <CardContent>
              <ScrollArea className="h-[500px]">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Group Name</TableHead>
                      <TableHead>Group ID</TableHead>
                      <TableHead className="text-center">Members</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {filteredGroups.map((group) => (
                      <GroupRow key={group.id} group={group} />
                    ))}
                    {filteredGroups.length === 0 && (
                      <TableRow>
                        <TableCell
                          colSpan={3}
                          className="text-center text-muted-foreground py-8"
                        >
                          No groups found
                        </TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </ScrollArea>
            </CardContent>
          </Card>
        </TabsContent>

        {/* All Users Tab */}
        <TabsContent value="users">
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle>All Workspace Users</CardTitle>
                  <CardDescription>
                    All users in your Databricks workspace
                  </CardDescription>
                </div>
                <div className="relative w-64">
                  <Search className="absolute left-2 top-2.5 h-4 w-4 text-muted-foreground" />
                  <Input
                    placeholder="Search users..."
                    className="pl-8"
                    value={searchUsers}
                    onChange={(e) => setSearchUsers(e.target.value)}
                  />
                </div>
              </div>
            </CardHeader>
            <CardContent>
              <ScrollArea className="h-[500px]">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Display Name</TableHead>
                      <TableHead>Username</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Groups</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {filteredUsers.map((user) => (
                      <TableRow key={user.id}>
                        <TableCell className="font-medium">
                          {user.display_name || "—"}
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          {user.user_name || "—"}
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant={user.active ? "default" : "secondary"}
                          >
                            {user.active ? "Active" : "Inactive"}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          <div className="flex flex-wrap gap-1">
                            {(user.groups || []).slice(0, 3).map((g, i) => (
                              <Badge
                                key={i}
                                variant="outline"
                                className="text-xs"
                              >
                                {g}
                              </Badge>
                            ))}
                            {(user.groups || []).length > 3 && (
                              <Badge variant="outline" className="text-xs">
                                +{(user.groups || []).length - 3} more
                              </Badge>
                            )}
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                    {filteredUsers.length === 0 && (
                      <TableRow>
                        <TableCell
                          colSpan={4}
                          className="text-center text-muted-foreground py-8"
                        >
                          No users found
                        </TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </ScrollArea>
            </CardContent>
          </Card>
        </TabsContent>

        {/* Unassigned Users Tab */}
        <TabsContent value="unassigned">
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle className="flex items-center gap-2">
                    <UserX className="h-5 w-5" />
                    Unassigned Users
                  </CardTitle>
                  <CardDescription>
                    Users not in any persona-mapped group. Assign them to a group
                    below.
                  </CardDescription>
                </div>
                <div className="relative w-64">
                  <Search className="absolute left-2 top-2.5 h-4 w-4 text-muted-foreground" />
                  <Input
                    placeholder="Search unassigned..."
                    className="pl-8"
                    value={searchUnassigned}
                    onChange={(e) => setSearchUnassigned(e.target.value)}
                  />
                </div>
              </div>
            </CardHeader>
            <CardContent>
              <ScrollArea className="h-[500px]">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Display Name</TableHead>
                      <TableHead>Username</TableHead>
                      <TableHead>Current Groups</TableHead>
                      {isAdmin && (
                        <TableHead className="text-right">Action</TableHead>
                      )}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {filteredUnassigned.map((user) => (
                      <TableRow key={user.id}>
                        <TableCell className="font-medium">
                          {user.display_name || "—"}
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          {user.user_name || "—"}
                        </TableCell>
                        <TableCell>
                          <div className="flex flex-wrap gap-1">
                            {(user.groups || []).map((g, i) => (
                              <Badge
                                key={i}
                                variant="outline"
                                className="text-xs"
                              >
                                {g}
                              </Badge>
                            ))}
                            {(user.groups || []).length === 0 && (
                              <span className="text-muted-foreground text-xs">
                                No groups
                              </span>
                            )}
                          </div>
                        </TableCell>
                        {isAdmin && (
                          <TableCell className="text-right">
                            <AssignUserDialog
                              userId={user.id}
                              userName={
                                user.display_name || user.user_name || user.id
                              }
                              groups={groups}
                            />
                          </TableCell>
                        )}
                      </TableRow>
                    ))}
                    {filteredUnassigned.length === 0 && (
                      <TableRow>
                        <TableCell
                          colSpan={isAdmin ? 4 : 3}
                          className="text-center text-muted-foreground py-8"
                        >
                          {unassigned.length === 0
                            ? "All users are assigned to persona-mapped groups"
                            : "No matching unassigned users"}
                        </TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </ScrollArea>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function GroupRow({ group }: { group: GroupOut }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <>
      <TableRow
        className="cursor-pointer hover:bg-muted/50"
        onClick={() => setExpanded(!expanded)}
      >
        <TableCell className="font-medium">{group.display_name}</TableCell>
        <TableCell className="text-muted-foreground font-mono text-xs">
          {group.id}
        </TableCell>
        <TableCell className="text-center">
          <Badge variant="secondary">{group.member_count}</Badge>
        </TableCell>
      </TableRow>
      {expanded && group.members && group.members.length > 0 && (
        <TableRow>
          <TableCell colSpan={3} className="bg-muted/30 p-4">
            <div className="text-sm font-medium mb-2">
              Members ({group.members.length})
            </div>
            <div className="flex flex-wrap gap-2">
              {group.members.map((m) => (
                <Badge key={m.user_id} variant="outline">
                  {m.display_name || m.user_id}
                </Badge>
              ))}
            </div>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

function AssignUserDialog({
  userId,
  userName,
  groups,
}: {
  userId: string;
  userName: string;
  groups: GroupOut[];
}) {
  const [open, setOpen] = useState(false);
  const [selectedGroup, setSelectedGroup] = useState<string>("");
  const queryClient = useQueryClient();
  const addMember = useAddGroupMember({
    mutation: {
      onSuccess: () => {
        toast.success(`User ${userName} added to group successfully`);
        setOpen(false);
        setSelectedGroup("");
        queryClient.invalidateQueries({ queryKey: listGroupsKey() });
        queryClient.invalidateQueries({ queryKey: listUnassignedUsersKey() });
        queryClient.invalidateQueries({ queryKey: listUsersKey() });
      },
      onError: (error) => {
        toast.error(`Failed to add user: ${error.message}`);
      },
    },
  });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" className="flex items-center gap-1">
          <UserPlus className="h-3 w-3" />
          Assign
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Assign User to Group</DialogTitle>
          <DialogDescription>
            Add <strong>{userName}</strong> to a workspace group.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 pt-4">
          <Select value={selectedGroup} onValueChange={setSelectedGroup}>
            <SelectTrigger>
              <SelectValue placeholder="Select a group..." />
            </SelectTrigger>
            <SelectContent>
              {groups.map((g) => (
                <SelectItem key={g.id} value={g.id}>
                  {g.display_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={!selectedGroup || addMember.isPending}
              onClick={() => {
                if (selectedGroup) {
                  addMember.mutate({
                    params: { group_id: selectedGroup },
                    data: { user_id: userId },
                  });
                }
              }}
            >
              {addMember.isPending ? "Adding..." : "Add to Group"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function GroupsSkeleton() {
  return (
    <div className="space-y-6">
      <div>
        <Skeleton className="h-9 w-48" />
        <Skeleton className="h-4 w-96 mt-2" />
      </div>
      <Skeleton className="h-10 w-80" />
      <Card>
        <CardHeader>
          <Skeleton className="h-6 w-48" />
          <Skeleton className="h-4 w-64" />
        </CardHeader>
        <CardContent>
          <div className="space-y-3">
            {[1, 2, 3, 4, 5, 6, 7, 8].map((i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function GroupsPage() {
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
                  Failed to Load Groups
                </CardTitle>
                <CardDescription>
                  Could not fetch workspace groups and users. Make sure you're
                  authenticated and the backend is running.
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
          <Suspense fallback={<GroupsSkeleton />}>
            <GroupsContent />
          </Suspense>
        </ErrorBoundary>
      )}
    </QueryErrorResetBoundary>
  );
}
