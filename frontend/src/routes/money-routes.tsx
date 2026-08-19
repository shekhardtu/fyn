import { DashboardsPage } from "@/components/dashboards";
import { DocumentTitle } from "@/components/document-title";
import { CategoriesPage, TransactionsPage } from "@/components/money-pages";
import { OverviewPage } from "@/components/overview";
import { WorkspaceShell } from "@/components/workspace";

export function OverviewRoute() {
  return <WorkspaceShell><DocumentTitle title="Overview" /><OverviewPage /></WorkspaceShell>;
}

export function DashboardsRoute() {
  return <WorkspaceShell><DocumentTitle title="Dashboards" /><DashboardsPage /></WorkspaceShell>;
}

export function TransactionsRoute() {
  return <WorkspaceShell><DocumentTitle title="Transactions" /><TransactionsPage /></WorkspaceShell>;
}

export function CategoriesRoute() {
  return <WorkspaceShell><DocumentTitle title="Categories" /><CategoriesPage /></WorkspaceShell>;
}
