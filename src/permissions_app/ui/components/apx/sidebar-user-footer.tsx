import { Suspense, useMemo, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { SidebarMenuButton } from "@/components/ui/sidebar";
import { useCurrentUserSuspense } from "@/lib/api";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAuth } from "@/hooks/use-auth";
import { LoginDialog } from "@/components/login-dialog";
import { ChevronsUpDown, LogIn, LogOut, UserRoundCog } from "lucide-react";
import selector from "@/lib/selector";

function SidebarUserFooterSkeleton() {
  return (
    <SidebarMenuButton size="lg">
      <Skeleton className="h-8 w-8 rounded-lg" />
      <div className="grid flex-1 text-left text-sm leading-tight gap-1">
        <Skeleton className="h-4 w-24 rounded" />
        <Skeleton className="h-3 w-46 rounded" />
      </div>
    </SidebarMenuButton>
  );
}

function SidebarUserFooterContent() {
  const { data: user } = useCurrentUserSuspense(selector());
  const { isCustomAuth, logout } = useAuth();
  const [loginOpen, setLoginOpen] = useState(false);

  const isServicePrincipal = useMemo(() => {
    const userName = user.user_name ?? "";
    return !userName.includes("@");
  }, [user.user_name]);

  const firstLetters = useMemo(() => {
    const displayName = user.display_name ?? user.user_name ?? "";
    const parts = displayName.split(/[\s.@]+/);
    if (parts.length >= 2) {
      return `${parts[0][0] ?? ""}${parts[1][0] ?? ""}`.toUpperCase();
    }
    return displayName.substring(0, 2).toUpperCase();
  }, [user.display_name, user.user_name]);

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <SidebarMenuButton
            size="lg"
            className="data-[state=open]:bg-sidebar-accent data-[state=open]:text-sidebar-accent-foreground cursor-pointer"
          >
            <Avatar className="h-8 w-8 rounded-lg grayscale">
              <AvatarFallback className="rounded-lg">
                {firstLetters}
              </AvatarFallback>
            </Avatar>
            <div className="grid flex-1 text-left text-sm leading-tight">
              <span className="truncate font-medium flex items-center gap-1.5">
                {user.display_name || user.user_name}
                {isServicePrincipal && (
                  <Badge
                    variant="outline"
                    className="text-[10px] px-1 py-0 font-normal text-amber-500 border-amber-500/50"
                  >
                    SP
                  </Badge>
                )}
              </span>
              <span className="text-muted-foreground truncate text-xs">
                {user.user_name}
              </span>
            </div>
            <ChevronsUpDown className="ml-auto h-4 w-4 text-muted-foreground" />
          </SidebarMenuButton>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          className="w-[--radix-dropdown-menu-trigger-width] min-w-56"
          side="top"
          align="start"
          sideOffset={4}
        >
          <DropdownMenuLabel className="font-normal">
            <div className="flex flex-col space-y-1">
              <p className="text-sm font-medium leading-none">
                {user.display_name || user.user_name}
              </p>
              <p className="text-xs leading-none text-muted-foreground">
                {user.user_name}
              </p>
              {isServicePrincipal && !isCustomAuth && (
                <p className="text-xs text-amber-500 pt-1">
                  Using default service principal credentials
                </p>
              )}
            </div>
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuItem onClick={() => setLoginOpen(true)}>
            {isCustomAuth ? (
              <>
                <UserRoundCog className="mr-2 h-4 w-4" />
                Switch User
              </>
            ) : (
              <>
                <LogIn className="mr-2 h-4 w-4" />
                Sign In with PAT
              </>
            )}
          </DropdownMenuItem>
          {isCustomAuth && (
            <DropdownMenuItem onClick={logout}>
              <LogOut className="mr-2 h-4 w-4" />
              Sign Out
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      <LoginDialog open={loginOpen} onOpenChange={setLoginOpen} />
    </>
  );
}

function SidebarUserFooterFallback() {
  return (
    <SidebarMenuButton size="lg">
      <Avatar className="h-8 w-8 rounded-lg grayscale">
        <AvatarFallback className="rounded-lg">?</AvatarFallback>
      </Avatar>
      <div className="grid flex-1 text-left text-sm leading-tight">
        <span className="truncate font-medium">Account</span>
        <span className="text-muted-foreground truncate text-xs">
          Sign-in unavailable
        </span>
      </div>
    </SidebarMenuButton>
  );
}

export default function SidebarUserFooter() {
  // Isolate the footer's user lookup: if /current-user transiently fails, show a
  // graceful fallback instead of letting the thrown error bubble to the root
  // router boundary (which renders a non-recoverable "Something went wrong").
  return (
    <ErrorBoundary fallbackRender={() => <SidebarUserFooterFallback />}>
      <Suspense fallback={<SidebarUserFooterSkeleton />}>
        <SidebarUserFooterContent />
      </Suspense>
    </ErrorBoundary>
  );
}
