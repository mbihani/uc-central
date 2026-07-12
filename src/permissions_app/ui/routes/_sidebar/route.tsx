import SidebarLayout from "@/components/apx/sidebar-layout";
import { createFileRoute, Link, useLocation } from "@tanstack/react-router";
import { cn } from "@/lib/utils";
import {
  LayoutDashboard,
  Users,
  UserCog,
  Shield,
  Database,
  User,
} from "lucide-react";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuItem,
} from "@/components/ui/sidebar";

export const Route = createFileRoute("/_sidebar")({
  component: () => <Layout />,
});

function Layout() {
  const location = useLocation();

  const mainNavItems = [
    {
      to: "/dashboard",
      label: "Dashboard",
      icon: <LayoutDashboard size={16} />,
      match: (path: string) => path === "/dashboard",
    },
    {
      to: "/groups",
      label: "Users & Groups",
      icon: <Users size={16} />,
      match: (path: string) => path === "/groups",
    },
    {
      to: "/personas",
      label: "Personas",
      icon: <UserCog size={16} />,
      match: (path: string) => path === "/personas",
    },
    {
      to: "/permissions",
      label: "Permissions Matrix",
      icon: <Shield size={16} />,
      match: (path: string) => path === "/permissions",
    },
    {
      to: "/resources",
      label: "Resources",
      icon: <Database size={16} />,
      match: (path: string) =>
        path === "/resources" || path.startsWith("/resources/"),
    },
  ];

  const accountNavItems = [
    {
      to: "/profile",
      label: "Profile",
      icon: <User size={16} />,
      match: (path: string) => path === "/profile",
    },
  ];

  return (
    <SidebarLayout>
      <SidebarGroup>
        <SidebarGroupLabel>Permissions Management</SidebarGroupLabel>
        <SidebarGroupContent>
          <SidebarMenu>
            {mainNavItems.map((item) => (
              <SidebarMenuItem key={item.to}>
                <Link
                  to={item.to}
                  className={cn(
                    "flex items-center gap-2 p-2 rounded-lg",
                    item.match(location.pathname)
                      ? "bg-sidebar-accent text-sidebar-accent-foreground"
                      : "text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
                  )}
                >
                  {item.icon}
                  <span>{item.label}</span>
                </Link>
              </SidebarMenuItem>
            ))}
          </SidebarMenu>
        </SidebarGroupContent>
      </SidebarGroup>
      <SidebarGroup>
        <SidebarGroupLabel>Account</SidebarGroupLabel>
        <SidebarGroupContent>
          <SidebarMenu>
            {accountNavItems.map((item) => (
              <SidebarMenuItem key={item.to}>
                <Link
                  to={item.to}
                  className={cn(
                    "flex items-center gap-2 p-2 rounded-lg",
                    item.match(location.pathname)
                      ? "bg-sidebar-accent text-sidebar-accent-foreground"
                      : "text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
                  )}
                >
                  {item.icon}
                  <span>{item.label}</span>
                </Link>
              </SidebarMenuItem>
            ))}
          </SidebarMenu>
        </SidebarGroupContent>
      </SidebarGroup>
    </SidebarLayout>
  );
}
