import { createFileRoute, Link } from "@tanstack/react-router";
import { Suspense } from "react";
import { QueryErrorResetBoundary } from "@tanstack/react-query";
import { ErrorBoundary } from "react-error-boundary";
import {
  useGetDashboardStatsSuspense,
  useListPersonasSuspense,
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
  Users,
  Shield,
  UserCog,
  Database,
  AlertCircle,
  ArrowRight,
  UserX,
} from "lucide-react";

export const Route = createFileRoute("/_sidebar/dashboard")({
  component: () => <Dashboard />,
});

function DashboardContent() {
  const { data: stats } = useGetDashboardStatsSuspense(selector());
  const { data: personas } = useListPersonasSuspense(selector());

  const statCards = [
    {
      title: "Total Groups",
      value: stats.total_groups,
      icon: <Users className="h-5 w-5" />,
      description: "Workspace groups scanned",
      link: "/groups",
    },
    {
      title: "Total Users",
      value: stats.total_users,
      icon: <UserCog className="h-5 w-5" />,
      description: "Workspace users found",
      link: "/groups",
    },
    {
      title: "Mapped Groups",
      value: stats.mapped_groups,
      icon: <Shield className="h-5 w-5" />,
      description: `${stats.personas_with_groups} of 6 personas have groups`,
      link: "/personas",
    },
    {
      title: "Unassigned Users",
      value: stats.unassigned_users,
      icon: <UserX className="h-5 w-5" />,
      description: "Users not in persona-mapped groups",
      link: "/groups",
    },
  ];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">
          Permissions Dashboard
        </h1>
        <p className="text-muted-foreground mt-1">
          Overview of your Databricks workspace permissions management
        </p>
      </div>

      {/* Stat Cards */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        {statCards.map((card) => (
          <Link key={card.title} to={card.link}>
            <Card className="hover:border-primary/40 transition-colors cursor-pointer">
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium">
                  {card.title}
                </CardTitle>
                {card.icon}
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{card.value}</div>
                <p className="text-xs text-muted-foreground mt-1">
                  {card.description}
                </p>
              </CardContent>
            </Card>
          </Link>
        ))}
      </div>

      {/* Personas Overview */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Persona Assignments</CardTitle>
              <CardDescription>
                Groups mapped to each persona role
              </CardDescription>
            </div>
            <Button variant="outline" size="sm" asChild>
              <Link to="/personas" className="flex items-center gap-1">
                Manage <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {personas.map((persona) => (
              <div
                key={persona.persona}
                className="flex items-start gap-3 p-3 rounded-lg border"
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <p className="font-medium text-sm">{persona.label}</p>
                    <Badge variant="secondary" className="text-xs">
                      {persona.groups.length} group
                      {persona.groups.length !== 1 ? "s" : ""}
                    </Badge>
                  </div>
                  <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
                    {persona.description}
                  </p>
                  {persona.groups.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-2">
                      {persona.groups.map((g) => (
                        <Badge key={g.id} variant="outline" className="text-xs">
                          {g.group_name}
                        </Badge>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Quick Actions */}
      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm flex items-center gap-2">
              <Database className="h-4 w-4" /> Resources
            </CardTitle>
            <CardDescription>
              Browse workspace resources and manage per-resource permissions
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button variant="outline" size="sm" asChild>
              <Link to="/resources">Browse Resources</Link>
            </Button>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-sm flex items-center gap-2">
              <Shield className="h-4 w-4" /> Permissions Matrix
            </CardTitle>
            <CardDescription>
              View and edit the persona-to-permission mapping
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button variant="outline" size="sm" asChild>
              <Link to="/permissions">View Matrix</Link>
            </Button>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-sm flex items-center gap-2">
              <Users className="h-4 w-4" /> Groups
            </CardTitle>
            <CardDescription>
              Scan workspace groups and assign unassigned users
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button variant="outline" size="sm" asChild>
              <Link to="/groups">Manage Groups</Link>
            </Button>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function DashboardSkeleton() {
  return (
    <div className="space-y-6">
      <div>
        <Skeleton className="h-9 w-64" />
        <Skeleton className="h-4 w-96 mt-2" />
      </div>
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        {[1, 2, 3, 4].map((i) => (
          <Card key={i}>
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <Skeleton className="h-4 w-24" />
              <Skeleton className="h-5 w-5 rounded" />
            </CardHeader>
            <CardContent>
              <Skeleton className="h-8 w-16" />
              <Skeleton className="h-3 w-40 mt-2" />
            </CardContent>
          </Card>
        ))}
      </div>
      <Card>
        <CardHeader>
          <Skeleton className="h-6 w-48" />
          <Skeleton className="h-4 w-64" />
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {[1, 2, 3, 4, 5, 6].map((i) => (
              <Skeleton key={i} className="h-24 w-full rounded-lg" />
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function Dashboard() {
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
                  Failed to Load Dashboard
                </CardTitle>
                <CardDescription>
                  There was an error loading the dashboard. Make sure the backend
                  is running and you're authenticated.
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
          <Suspense fallback={<DashboardSkeleton />}>
            <DashboardContent />
          </Suspense>
        </ErrorBoundary>
      )}
    </QueryErrorResetBoundary>
  );
}
