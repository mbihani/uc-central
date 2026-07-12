import { ThemeProvider } from "@/components/apx/theme-provider";
import { AdminProvider } from "@/hooks/use-admin";
import { AuthProvider } from "@/hooks/use-auth";
import { QueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, Outlet } from "@tanstack/react-router";
import { TanStackRouterDevtools } from "@tanstack/react-router-devtools";
import { Toaster } from "sonner";

export const Route = createRootRouteWithContext<{
  queryClient: QueryClient;
}>()({
  component: () => (
    <ThemeProvider defaultTheme="dark" storageKey="apx-ui-theme">
      <AuthProvider>
        <AdminProvider>
          {import.meta.env.DEV && (
            <>
              <TanStackRouterDevtools position="bottom-right" />
            </>
          )}
          <Outlet />
          <Toaster richColors />
        </AdminProvider>
      </AuthProvider>
    </ThemeProvider>
  ),
});
