import { createFileRoute } from "@tanstack/react-router";
import { Suspense, useState } from "react";
import { QueryErrorResetBoundary, useQueryClient } from "@tanstack/react-query";
import { ErrorBoundary } from "react-error-boundary";
import {
  useListPersonasSuspense,
  useListGroupsSuspense,
  useListUsersSuspense,
  useCreatePersonaMapping,
  useDeletePersonaMapping,
  useCreatePersonaUserMapping,
  useDeletePersonaUserMapping,
  useCreatePersona,
  useUpdatePersona,
  useDeletePersona,
  listPersonasKey,
  getPermissionMatrixKey,
  type PersonaOut,
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
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
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
} from "@/components/ui/alert-dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  AlertCircle,
  Plus,
  Trash2,
  UserCog,
  User,
  Users,
  Shield,
  Code2,
  FlaskConical,
  BarChart3,
  Rocket,
  Headphones,
  Pencil,
} from "lucide-react";
import { toast } from "sonner";
import { useAdmin } from "@/hooks/use-admin";

export const Route = createFileRoute("/_sidebar/personas")({
  component: () => <PersonasPage />,
});

const PERSONA_ICONS: Record<string, React.ReactNode> = {
  admin: <Shield className="h-5 w-5 text-red-500" />,
  data_engineer: <Code2 className="h-5 w-5 text-blue-500" />,
  data_scientist: <FlaskConical className="h-5 w-5 text-purple-500" />,
  analyst: <BarChart3 className="h-5 w-5 text-green-500" />,
  deployer: <Rocket className="h-5 w-5 text-orange-500" />,
  support: <Headphones className="h-5 w-5 text-cyan-500" />,
};

function PersonasContent() {
  const { data: personas } = useListPersonasSuspense(selector());
  const { data: groups } = useListGroupsSuspense(selector());
  const { data: users } = useListUsersSuspense(selector());
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const { isAdmin } = useAdmin();

  // Get list of already-mapped group IDs
  const mappedGroupIds = new Set(
    personas.flatMap((p) => p.groups.map((g) => g.group_id)),
  );
  const availableGroups = groups.filter((g) => !mappedGroupIds.has(g.id));

  // Get list of already-mapped user IDs (direct persona-user mappings)
  const mappedUserIds = new Set(
    personas.flatMap((p) => (p.users ?? []).map((u) => u.user_id)),
  );
  const availableUsers = users.filter((u) => !mappedUserIds.has(u.id));

  const invalidateAll = () => {
    queryClient.invalidateQueries({ queryKey: listPersonasKey() });
    queryClient.invalidateQueries({ queryKey: getPermissionMatrixKey() });
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">
            Persona Mapping
          </h1>
          <p className="text-muted-foreground mt-1">
            Map Databricks workspace groups and users to personas. Each persona
            defines a set of default permissions.
          </p>
        </div>
        {isAdmin && (
          <Button onClick={() => setCreateOpen(true)} className="gap-1">
            <Plus className="h-4 w-4" />
            New Persona
          </Button>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {personas.map((persona) => (
          <PersonaCard
            key={persona.persona}
            persona={persona}
            availableGroups={availableGroups}
            availableUsers={availableUsers}
            onChanged={invalidateAll}
            isAdmin={isAdmin}
          />
        ))}
      </div>

      {isAdmin && (
        <CreatePersonaDialog
          open={createOpen}
          onOpenChange={setCreateOpen}
          onCreated={invalidateAll}
        />
      )}
    </div>
  );
}

function PersonaCard({
  persona,
  availableGroups,
  availableUsers,
  onChanged,
  isAdmin,
}: {
  persona: PersonaOut;
  availableGroups: { id: string; display_name: string }[];
  availableUsers: UserOut[];
  onChanged: () => void;
  isAdmin: boolean;
}) {
  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  const personaUsers = persona.users ?? [];

  return (
    <Card className="flex flex-col">
      <CardHeader>
        <div className="flex items-start gap-3">
          {PERSONA_ICONS[persona.persona] || (
            <UserCog className="h-5 w-5 text-muted-foreground" />
          )}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <CardTitle className="text-lg">{persona.label}</CardTitle>
              {persona.is_default && (
                <Badge variant="secondary" className="text-xs">
                  Default
                </Badge>
              )}
            </div>
            <CardDescription className="text-xs mt-1">
              {persona.description}
            </CardDescription>
          </div>
          {isAdmin && (
            <div className="flex gap-1 shrink-0">
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted-foreground hover:text-foreground"
                onClick={() => setEditOpen(true)}
                title="Edit persona"
              >
                <Pencil className="h-3.5 w-3.5" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted-foreground hover:text-destructive"
                onClick={() => setDeleteOpen(true)}
                title="Delete persona"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent className="flex-1 flex flex-col">
        <div className="flex-1 space-y-4">
          {/* Groups section */}
          <div>
            <div className="flex items-center gap-1.5 text-sm font-medium mb-2">
              <Users className="h-3.5 w-3.5 text-muted-foreground" />
              Mapped Groups
            </div>
            {persona.groups.length === 0 ? (
              <p className="text-sm text-muted-foreground italic">
                No groups mapped yet
              </p>
            ) : (
              <div className="space-y-2">
                {persona.groups.map((g) => (
                  <GroupMappingBadge
                    key={g.id}
                    mappingId={g.id}
                    groupName={g.group_name}
                    onDeleted={onChanged}
                    isAdmin={isAdmin}
                  />
                ))}
              </div>
            )}
          </div>

          {/* Users section */}
          <div>
            <div className="flex items-center gap-1.5 text-sm font-medium mb-2">
              <User className="h-3.5 w-3.5 text-muted-foreground" />
              Mapped Users
            </div>
            {personaUsers.length === 0 ? (
              <p className="text-sm text-muted-foreground italic">
                No users mapped yet
              </p>
            ) : (
              <div className="space-y-2">
                {personaUsers.map((u) => (
                  <UserMappingBadge
                    key={u.id}
                    mappingId={u.id}
                    userName={u.user_name}
                    displayName={u.display_name}
                    onDeleted={onChanged}
                    isAdmin={isAdmin}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
        {isAdmin && (
          <div className="mt-4 flex gap-2">
            <AddGroupDialog
              persona={persona.persona}
              personaLabel={persona.label}
              availableGroups={availableGroups}
              onCreated={onChanged}
            />
            <AddUserDialog
              persona={persona.persona}
              personaLabel={persona.label}
              availableUsers={availableUsers}
              onCreated={onChanged}
            />
          </div>
        )}
      </CardContent>

      {isAdmin && (
        <>
          <EditPersonaDialog
            open={editOpen}
            onOpenChange={setEditOpen}
            persona={persona}
            onUpdated={onChanged}
          />

          <DeletePersonaDialog
            open={deleteOpen}
            onOpenChange={setDeleteOpen}
            persona={persona}
            onDeleted={onChanged}
          />
        </>
      )}
    </Card>
  );
}

function GroupMappingBadge({
  mappingId,
  groupName,
  onDeleted,
  isAdmin,
}: {
  mappingId: number;
  groupName: string;
  onDeleted: () => void;
  isAdmin: boolean;
}) {
  const deleteMapping = useDeletePersonaMapping({
    mutation: {
      onSuccess: () => {
        toast.success(`Removed ${groupName} from persona`);
        onDeleted();
      },
      onError: (error) => {
        toast.error(`Failed to remove mapping: ${error.message}`);
      },
    },
  });

  return (
    <div className="flex items-center justify-between gap-2 p-2 rounded-md border bg-muted/30">
      <span className="text-sm truncate">{groupName}</span>
      {isAdmin && (
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 text-muted-foreground hover:text-destructive"
          disabled={deleteMapping.isPending}
          onClick={() => {
            deleteMapping.mutate({ params: { mapping_id: mappingId } });
          }}
        >
          <Trash2 className="h-3 w-3" />
        </Button>
      )}
    </div>
  );
}

function UserMappingBadge({
  mappingId,
  userName,
  displayName,
  onDeleted,
  isAdmin,
}: {
  mappingId: number;
  userName: string;
  displayName: string;
  onDeleted: () => void;
  isAdmin: boolean;
}) {
  const deleteMapping = useDeletePersonaUserMapping({
    mutation: {
      onSuccess: () => {
        toast.success(`Removed ${displayName || userName} from persona`);
        onDeleted();
      },
      onError: (error) => {
        toast.error(`Failed to remove user mapping: ${error.message}`);
      },
    },
  });

  return (
    <div className="flex items-center justify-between gap-2 p-2 rounded-md border bg-muted/30">
      <div className="min-w-0">
        <span className="text-sm truncate block">
          {displayName || userName}
        </span>
        {displayName && userName && (
          <span className="text-xs text-muted-foreground truncate block">
            {userName}
          </span>
        )}
      </div>
      {isAdmin && (
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 shrink-0 text-muted-foreground hover:text-destructive"
          disabled={deleteMapping.isPending}
          onClick={() => {
            deleteMapping.mutate({ params: { mapping_id: mappingId } });
          }}
        >
          <Trash2 className="h-3 w-3" />
        </Button>
      )}
    </div>
  );
}

function AddGroupDialog({
  persona,
  personaLabel,
  availableGroups,
  onCreated,
}: {
  persona: string;
  personaLabel: string;
  availableGroups: { id: string; display_name: string }[];
  onCreated: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [selectedGroup, setSelectedGroup] = useState<string>("");

  const createMapping = useCreatePersonaMapping({
    mutation: {
      onSuccess: () => {
        toast.success("Group mapped to persona successfully");
        setOpen(false);
        setSelectedGroup("");
        onCreated();
      },
      onError: (error) => {
        toast.error(`Failed to map group: ${error.message}`);
      },
    },
  });

  const selectedGroupObj = availableGroups.find((g) => g.id === selectedGroup);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="w-full flex items-center gap-1"
        >
          <Plus className="h-3 w-3" />
          Add Group
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Map Group to {personaLabel}</DialogTitle>
          <DialogDescription>
            Select a workspace group to assign the{" "}
            <strong>{personaLabel}</strong> persona permissions.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 pt-4">
          {availableGroups.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              All groups are already mapped to personas.
            </p>
          ) : (
            <Select value={selectedGroup} onValueChange={setSelectedGroup}>
              <SelectTrigger>
                <SelectValue placeholder="Select a group..." />
              </SelectTrigger>
              <SelectContent>
                {availableGroups.map((g) => (
                  <SelectItem key={g.id} value={g.id}>
                    {g.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={
                !selectedGroup ||
                createMapping.isPending ||
                availableGroups.length === 0
              }
              onClick={() => {
                if (selectedGroupObj) {
                  createMapping.mutate({
                    params: {},
                    data: {
                      group_id: selectedGroupObj.id,
                      group_name: selectedGroupObj.display_name,
                      persona: persona,
                    },
                  });
                }
              }}
            >
              {createMapping.isPending ? "Mapping..." : "Map Group"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function AddUserDialog({
  persona,
  personaLabel,
  availableUsers,
  onCreated,
}: {
  persona: string;
  personaLabel: string;
  availableUsers: UserOut[];
  onCreated: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [selectedUser, setSelectedUser] = useState<string>("");

  const createMapping = useCreatePersonaUserMapping({
    mutation: {
      onSuccess: () => {
        toast.success("User mapped to persona successfully");
        setOpen(false);
        setSelectedUser("");
        onCreated();
      },
      onError: (error) => {
        toast.error(`Failed to map user: ${error.message}`);
      },
    },
  });

  const selectedUserObj = availableUsers.find((u) => u.id === selectedUser);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="w-full flex items-center gap-1"
        >
          <Plus className="h-3 w-3" />
          Add User
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Map User to {personaLabel}</DialogTitle>
          <DialogDescription>
            Select a workspace user to directly assign the{" "}
            <strong>{personaLabel}</strong> persona permissions.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 pt-4">
          {availableUsers.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              All users are already directly mapped to personas.
            </p>
          ) : (
            <Select value={selectedUser} onValueChange={setSelectedUser}>
              <SelectTrigger>
                <SelectValue placeholder="Select a user..." />
              </SelectTrigger>
              <SelectContent>
                {availableUsers.map((u) => (
                  <SelectItem key={u.id} value={u.id}>
                    {u.display_name || u.user_name || u.id}
                    {u.display_name && u.user_name && (
                      <span className="text-muted-foreground ml-1">
                        ({u.user_name})
                      </span>
                    )}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={
                !selectedUser ||
                createMapping.isPending ||
                availableUsers.length === 0
              }
              onClick={() => {
                if (selectedUserObj) {
                  createMapping.mutate({
                    params: {},
                    data: {
                      user_id: selectedUserObj.id,
                      user_name: selectedUserObj.user_name ?? "",
                      display_name: selectedUserObj.display_name ?? "",
                      persona: persona,
                    },
                  });
                }
              }}
            >
              {createMapping.isPending ? "Mapping..." : "Map User"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function CreatePersonaDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: () => void;
}) {
  const [key, setKey] = useState("");
  const [label, setLabel] = useState("");
  const [description, setDescription] = useState("");

  const createPersona = useCreatePersona({
    mutation: {
      onSuccess: () => {
        toast.success("Persona created successfully");
        setKey("");
        setLabel("");
        setDescription("");
        onOpenChange(false);
        onCreated();
      },
      onError: (error) => {
        toast.error(`Failed to create persona: ${error.message}`);
      },
    },
  });

  // Auto-generate key from label
  const handleLabelChange = (value: string) => {
    setLabel(value);
    // Only auto-generate key if it hasn't been manually edited
    const autoKey = value
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_|_$/g, "");
    setKey(autoKey);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create New Persona</DialogTitle>
          <DialogDescription>
            Define a new persona with a unique key, display name, and
            description. The persona will start with no permissions assigned.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 pt-2">
          <div className="space-y-2">
            <Label htmlFor="persona-label">Display Name</Label>
            <Input
              id="persona-label"
              placeholder="e.g. Platform Engineer"
              value={label}
              onChange={(e) => handleLabelChange(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="persona-key">
              Key{" "}
              <span className="text-muted-foreground font-normal">
                (lowercase, underscores only)
              </span>
            </Label>
            <Input
              id="persona-key"
              placeholder="e.g. platform_engineer"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              pattern="^[a-z][a-z0-9_]*$"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="persona-desc">Description</Label>
            <Textarea
              id="persona-desc"
              placeholder="Describe the role and typical permissions..."
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              disabled={!key || !label || createPersona.isPending}
              onClick={() => {
                createPersona.mutate({ params: {}, data: { key, label, description } });
              }}
            >
              {createPersona.isPending ? "Creating..." : "Create Persona"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function EditPersonaDialog({
  open,
  onOpenChange,
  persona,
  onUpdated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  persona: PersonaOut;
  onUpdated: () => void;
}) {
  const [label, setLabel] = useState(persona.label);
  const [description, setDescription] = useState(persona.description);

  const updatePersona = useUpdatePersona({
    mutation: {
      onSuccess: () => {
        toast.success("Persona updated successfully");
        onOpenChange(false);
        onUpdated();
      },
      onError: (error) => {
        toast.error(`Failed to update persona: ${error.message}`);
      },
    },
  });

  // Reset form when dialog opens with new persona data
  const handleOpenChange = (isOpen: boolean) => {
    if (isOpen) {
      setLabel(persona.label);
      setDescription(persona.description);
    }
    onOpenChange(isOpen);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit Persona</DialogTitle>
          <DialogDescription>
            Update the display name and description for{" "}
            <strong>{persona.label}</strong>.
            {persona.is_default && (
              <span className="block mt-1 text-xs">
                This is a built-in default persona. You can customize its label
                and description.
              </span>
            )}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 pt-2">
          <div className="space-y-2">
            <Label>
              Key{" "}
              <span className="text-muted-foreground font-normal">
                (read-only)
              </span>
            </Label>
            <Input value={persona.persona} disabled />
          </div>
          <div className="space-y-2">
            <Label htmlFor="edit-label">Display Name</Label>
            <Input
              id="edit-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="edit-desc">Description</Label>
            <Textarea
              id="edit-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              disabled={!label || updatePersona.isPending}
              onClick={() => {
                updatePersona.mutate({
                  params: { persona_key: persona.persona },
                  data: { label, description },
                });
              }}
            >
              {updatePersona.isPending ? "Saving..." : "Save Changes"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function DeletePersonaDialog({
  open,
  onOpenChange,
  persona,
  onDeleted,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  persona: PersonaOut;
  onDeleted: () => void;
}) {
  const deletePersonaMutation = useDeletePersona({
    mutation: {
      onSuccess: () => {
        toast.success(`Persona "${persona.label}" deleted`);
        onOpenChange(false);
        onDeleted();
      },
      onError: (error) => {
        toast.error(`Failed to delete persona: ${error.message}`);
      },
    },
  });

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete Persona: {persona.label}?</AlertDialogTitle>
          <AlertDialogDescription>
            This will permanently delete the <strong>{persona.label}</strong>{" "}
            persona along with:
            <ul className="list-disc list-inside mt-2 space-y-1">
              {persona.groups.length > 0 && (
                <li>
                  {persona.groups.length} group mapping
                  {persona.groups.length !== 1 ? "s" : ""}
                </li>
              )}
              <li>All associated permission templates</li>
            </ul>
            <span className="block mt-2">This action cannot be undone.</span>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            disabled={deletePersonaMutation.isPending}
            onClick={(e) => {
              e.preventDefault();
              deletePersonaMutation.mutate({
                params: { persona_key: persona.persona },
              });
            }}
          >
            {deletePersonaMutation.isPending ? "Deleting..." : "Delete Persona"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function PersonasSkeleton() {
  return (
    <div className="space-y-6">
      <div>
        <Skeleton className="h-9 w-56" />
        <Skeleton className="h-4 w-96 mt-2" />
      </div>
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {[1, 2, 3, 4, 5, 6].map((i) => (
          <Card key={i}>
            <CardHeader>
              <div className="flex items-center gap-3">
                <Skeleton className="h-5 w-5 rounded" />
                <div>
                  <Skeleton className="h-5 w-32" />
                  <Skeleton className="h-3 w-48 mt-1" />
                </div>
              </div>
            </CardHeader>
            <CardContent>
              <Skeleton className="h-4 w-24 mb-2" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full mt-4" />
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

function PersonasPage() {
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
                  Failed to Load Personas
                </CardTitle>
                <CardDescription>
                  Could not fetch persona data. Make sure the backend is running.
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
          <Suspense fallback={<PersonasSkeleton />}>
            <PersonasContent />
          </Suspense>
        </ErrorBoundary>
      )}
    </QueryErrorResetBoundary>
  );
}
