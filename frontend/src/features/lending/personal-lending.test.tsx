import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { createMemoryRouter } from "react-router";
import { RouterProvider } from "react-router/dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LoanInvitationPage, PersonalLoanDetailPage, PersonalLoansPage } from "@/features/lending/personal-lending";
import type { PersonalLoanDetailOut } from "@/lib/protocol";


const api = vi.hoisted(() => ({
  loadPersonalLoans: vi.fn(),
  loadPersonalLoan: vi.fn(),
  loadLoanInvitation: vi.fn(),
  getAuthStatus: vi.fn(),
  searchContacts: vi.fn(),
}));

vi.mock("@/components/workspace", () => ({
  useWorkspaceShell: () => ({ navOpen: false, openNav: vi.fn() }),
}));

vi.mock("@/components/ui/overlay", async () => {
  const React = await import("react");
  return { useWorkspaceOverlay: () => React.useRef<HTMLElement>(null) };
});

vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  loadPersonalLoans: api.loadPersonalLoans,
  loadPersonalLoan: api.loadPersonalLoan,
  loadLoanInvitation: api.loadLoanInvitation,
  getAuthStatus: api.getAuthStatus,
  searchContacts: api.searchContacts,
}));

function renderRoute(path: string, pattern: string, element: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const router = createMemoryRouter([{ path: pattern, element }], { initialEntries: [path] });
  render(<QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider>);
  return router;
}

const loan: PersonalLoanDetailOut = {
  id: "00000000-0000-4000-8000-000000000010",
  sharedRecordId: "00000000-0000-4000-8000-000000000011",
  direction: "lent",
  counterpartyName: "Rahul",
  counterpartyVerification: "email_verified",
  status: "active",
  fundingStatus: "confirmed",
  principalMinor: 100_000,
  outstandingPrincipalMinor: 100_000,
  accruedInterestMinor: 3_000,
  totalRepayableMinor: 103_000,
  paidMinor: 0,
  currency: "INR",
  moneyDate: "2026-08-24",
  dueDate: "2027-08-24",
  nextDueMinor: 103_000,
  responseNeeded: false,
  rowVersion: 3,
  createdAt: "2026-08-24T10:00:00Z",
  updatedAt: "2026-08-24T10:00:00Z",
  note: "For a laptop",
  annualRateBps: 300,
  currentTerms: {
    id: "00000000-0000-4000-8000-000000000012",
    version: 1,
    principalMinor: 100_000,
    currency: "INR",
    annualRateBps: 300,
    interestMethod: "simple_annual",
    moneyDate: "2026-08-24",
    dueDate: "2027-08-24",
    note: "For a laptop",
    totalInterestMinor: 3_000,
    totalRepayableMinor: 103_000,
    state: "accepted",
    sourceHash: "b".repeat(64),
    documentRevisionId: "00000000-0000-4000-8000-000000000013",
    effectiveAt: "2026-08-24T10:05:00Z",
  },
  participants: [
    { id: "00000000-0000-4000-8000-000000000014", role: "lender", displayName: "Hari", state: "accepted", isCurrentUser: true, verificationChannel: null, verificationClaim: "fyn_account", claimedAt: "2026-08-24T10:00:00Z" },
    { id: "00000000-0000-4000-8000-000000000015", role: "borrower", displayName: "Rahul", state: "accepted", isCurrentUser: false, verificationChannel: "email", verificationClaim: "email_control", claimedAt: "2026-08-24T10:04:00Z" },
  ],
  invitation: null,
  documentRevision: {
    id: "00000000-0000-4000-8000-000000000013",
    documentId: "00000000-0000-4000-8000-000000000016",
    documentTitle: "Shared repayment plan",
    revisionNumber: 1,
    baseRevisionId: null,
    state: "accepted",
    authoredBy: "Hari",
    content: { plainLanguage: "Rahul acknowledges a repayment plan with Hari.", terms: { dueDate: "2027-08-24", annualRateBps: 300, totalRepayableMinor: 103_000 }, assuranceItems: [{ kind: "post_dated_cheque" }] },
    changeSummary: [],
    sourceSnapshotHash: "b".repeat(64),
    contentHash: "a".repeat(64),
    proposedAt: "2026-08-24T10:00:00Z",
    finalizedAt: "2026-08-24T10:05:00Z",
    changes: [],
    acceptances: [
      { participantId: "00000000-0000-4000-8000-000000000014", participantName: "Hari", action: "accepted", contentHash: "a".repeat(64), acceptedAt: "2026-08-24T10:00:00Z" },
      { participantId: "00000000-0000-4000-8000-000000000015", participantName: "Rahul", action: "accepted", contentHash: "a".repeat(64), acceptedAt: "2026-08-24T10:05:00Z" },
    ],
  },
  cashflows: [],
  securityItems: [{
    id: "00000000-0000-4000-8000-000000000017",
    kind: "post_dated_cheque",
    description: "Post-dated cheque for the agreed total",
    maskedIdentifier: "Cheque ••4821",
    statedValueMinor: 103_000,
    currency: "INR",
    providedBy: "Rahul",
    heldBy: "Hari",
    state: "acknowledged",
    returnedAt: null,
    returnConfirmedBy: null,
  }],
  activity: [{ id: "00000000-0000-4000-8000-000000000018", sequence: 1, eventType: "loan.created", actorParticipantId: "00000000-0000-4000-8000-000000000014", actorName: "Hari", payload: {}, eventHash: "c".repeat(64), createdAt: "2026-08-24T10:00:00Z" }],
};

beforeEach(() => {
  vi.clearAllMocks();
  api.loadPersonalLoans.mockResolvedValue({ moneyIGaveMinor: 0, moneyIReceivedMinor: 0, needsResponseCount: 0, items: [] });
  api.searchContacts.mockResolvedValue([]);
});

describe("personal lending UI", () => {
  it("uses calm shared-record language and reveals the assurance fields only when chosen", async () => {
    renderRoute("/loans", "/loans", <PersonalLoansPage />);
    expect(await screen.findByText("A shared record, in plain language")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "New plan" }));
    expect(screen.getByRole("heading", { name: "Create a shared plan" })).toBeInTheDocument();
    expect(screen.getByText("Record first. The other person reviews next.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Assurance item type"), { target: { value: "post_dated_cheque" } });
    expect(screen.getByLabelText("Description")).toBeInTheDocument();
    expect(screen.getByText(/Fyn does not take custody or enforce it/)).toBeInTheDocument();
  });

  it("starts with a login identifier and pulls the profile name for an exact email or phone match", async () => {
    api.searchContacts.mockImplementation(async (channel: "email" | "phone", query: string) => query.includes("@") ? [{
      channel,
      identifier: "rahul@example.test",
      displayName: "Rahul Sharma",
      matchKind: "exact",
    }] : []);
    renderRoute("/loans", "/loans", <PersonalLoansPage />);
    fireEvent.click(await screen.findByRole("button", { name: "New plan" }));

    const email = screen.getByRole("combobox", { name: /Email address/ });
    expect(document.querySelector("form input")).toBe(email);
    expect(screen.getByRole("tab", { name: "Email" })).toHaveAttribute("aria-selected", "true");

    fireEvent.change(email, { target: { value: "rahul@example.test" } });
    expect(await screen.findByText(/Matched to a Fyn account/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Person’s name/)).toHaveValue("Rahul Sharma");

    fireEvent.click(screen.getByRole("tab", { name: "Phone" }));
    expect(screen.getByRole("combobox", { name: /Phone number/ })).toHaveValue("");
  });

  it("keeps the private invitation path through sign-in", async () => {
    api.loadLoanInvitation.mockResolvedValue({ tokenValid: true, senderName: "Hari", recipientName: "Rahul", channel: "email", destinationMasked: "r***@example.com", expiresAt: "2026-09-01T10:00:00Z", canRedeem: false, loan: null });
    api.getAuthStatus.mockResolvedValue({ authenticated: false, profile: null, googleSignInAvailable: false });
    renderRoute("/loan-invitations/private-token", "/loan-invitations/:token", <LoanInvitationPage />);
    const link = await screen.findByRole("link", { name: /Sign in to review/ });
    expect(link).toHaveAttribute("href", "/login?next=%2Floan-invitations%2Fprivate-token");
    expect(screen.getByText(/full financial terms become visible only after/)).toBeInTheDocument();
  });

  it("shows exact revision evidence and the assurance return lifecycle", async () => {
    api.loadPersonalLoan.mockResolvedValue(loan);
    renderRoute(`/loans/${loan.id}`, "/loans/:loanId", <PersonalLoanDetailPage />);
    expect(await screen.findByRole("heading", { name: "Shared repayment plan" })).toBeInTheDocument();
    expect(screen.getByText(/Content fingerprint aaaaaaaaaa/)).toBeInTheDocument();
    expect(screen.getByText(/Post Dated Cheque/)).toBeInTheDocument();
    expect(screen.getByText(/Provided by Rahul, stated as held by Hari/)).toBeInTheDocument();
    expect(screen.getByText("Hari · acknowledged")).toBeInTheDocument();
    expect(screen.getByText("Rahul · acknowledged")).toBeInTheDocument();
  });

  it("does not offer the lender the borrower-only security return confirmation", async () => {
    const settlement: PersonalLoanDetailOut = {
      ...loan,
      status: "settlement_pending",
      outstandingPrincipalMinor: 0,
      accruedInterestMinor: 0,
      nextDueMinor: null,
      securityItems: loan.securityItems.map((item) => ({ ...item, state: "return_pending_confirmation" })),
      activity: [{
        id: "00000000-0000-4000-8000-000000000019",
        sequence: 2,
        eventType: "loan.closure_proposed",
        actorParticipantId: "00000000-0000-4000-8000-000000000014",
        actorName: "Hari",
        payload: {},
        eventHash: "d".repeat(64),
        createdAt: "2026-08-24T10:10:00Z",
      }],
    };
    api.loadPersonalLoan.mockResolvedValue(settlement);
    renderRoute(`/loans/${settlement.id}`, "/loans/:loanId", <PersonalLoanDetailPage />);
    expect(await screen.findByText(/Waiting for Rahul to confirm receipt/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm item returned and close" })).not.toBeInTheDocument();
  });
});
