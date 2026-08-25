import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Navigate } from "react-router";

import { DocumentTitle } from "@/components/document-title";
import { WorkspaceShell } from "@/components/workspace";
import { LoanInvitationPage, PersonalLoanDetailPage, PersonalLoansPage } from "@/features/lending/personal-lending";
import { bootstrap, getAuthStatus } from "@/lib/api";
import { appPaths } from "@/routing/paths";
import { InitialRouteFallback, NotFoundRoute } from "@/routes/fallback-routes";

function LendingWorkspaceGate({ title, children }: { title: string; children: ReactNode }) {
  const availability = useQuery({ queryKey: ["bootstrap"], queryFn: bootstrap, retry: false });
  if (availability.data && !availability.data.features.personalLending) return <Navigate to={appPaths.overview} replace />;
  return <WorkspaceShell>{availability.data?.features.personalLending ? <><DocumentTitle title={title} />{children}</> : null}</WorkspaceShell>;
}

export function PersonalLoansRoute() {
  return <LendingWorkspaceGate title="Personal lending"><PersonalLoansPage /></LendingWorkspaceGate>;
}

export function PersonalLoanDetailRoute() {
  return <LendingWorkspaceGate title="Shared repayment plan"><PersonalLoanDetailPage /></LendingWorkspaceGate>;
}

export function LoanInvitationRoute() {
  const availability = useQuery({ queryKey: ["auth-status"], queryFn: getAuthStatus, retry: false });
  if (availability.isPending) return <InitialRouteFallback />;
  if (availability.isError || !availability.data.features.personalLending) return <NotFoundRoute />;
  return <><DocumentTitle title="Loan invitation" /><LoanInvitationPage /></>;
}
