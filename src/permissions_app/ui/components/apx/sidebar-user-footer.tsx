import { Suspense, useMemo } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { SidebarMenuButton } from "@/components/ui/sidebar";
import { useCurrentUserSuspense } from "@/lib/api";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Skeleton } from "@/components/ui/skeleton";
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
  // Identity comes from the Databricks Apps proxy (the forwarded end-user).
  const { data: user } = useCurrentUserSuspense(selector());

  const firstLetters = useMemo(() => {
    const displayName = user.display_name ?? user.user_name ?? "";
    const parts = displayName.split(/[\s.@]+/);
    if (parts.length >= 2) {
      return `${parts[0][0] ?? ""}${parts[1][0] ?? ""}`.toUpperCase();
    }
    return displayName.substring(0, 2).toUpperCase();
  }, [user.display_name, user.user_name]);

  return (
    <SidebarMenuButton size="lg" className="cursor-default">
      <Avatar className="h-8 w-8 rounded-lg grayscale">
        <AvatarFallback className="rounded-lg">{firstLetters}</AvatarFallback>
      </Avatar>
      <div className="grid flex-1 text-left text-sm leading-tight">
        <span className="truncate font-medium">
          {user.display_name || user.user_name}
        </span>
        <span className="text-muted-foreground truncate text-xs">
          {user.user_name}
        </span>
      </div>
    </SidebarMenuButton>
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
