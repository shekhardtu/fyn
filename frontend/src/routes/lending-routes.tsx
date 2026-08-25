import { DocumentTitle } from "@/components/document-title";
import { WorkspaceShell } from "@/components/workspace";
import { LoanInvitationPage, PersonalLoanDetailPage, PersonalLoansPage } from "@/features/lending/personal-lending";

export function PersonalLoansRoute() {
  return <WorkspaceShell><DocumentTitle title="Personal lending" /><PersonalLoansPage /></WorkspaceShell>;
}

export function PersonalLoanDetailRoute() {
  return <WorkspaceShell><DocumentTitle title="Shared repayment plan" /><PersonalLoanDetailPage /></WorkspaceShell>;
}

export function LoanInvitationRoute() {
  return <><DocumentTitle title="Loan invitation" /><LoanInvitationPage /></>;
}
