import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { createMemoryRouter } from "react-router";
import { RouterProvider } from "react-router/dom";
import { describe, expect, it, vi } from "vitest";

import { LoanInvitationRoute, PersonalLoansRoute } from "@/routes/lending-routes";

const api = vi.hoisted(() => ({ bootstrap: vi.fn(), getAuthStatus: vi.fn() }));

vi.mock("@/lib/api", () => ({ bootstrap: api.bootstrap, getAuthStatus: api.getAuthStatus }));
vi.mock("@/components/workspace", () => ({ WorkspaceShell: ({ children }: { children: ReactNode }) => <div>Workspace shell{children}</div> }));
vi.mock("@/features/lending/personal-lending", () => ({
  PersonalLoansPage: () => <h1>Personal lending page</h1>,
  PersonalLoanDetailPage: () => <h1>Loan detail page</h1>,
  LoanInvitationPage: () => <h1>Loan invitation page</h1>,
}));

function renderRoute(path: string, routes: Parameters<typeof createMemoryRouter>[0]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
}

describe("personal lending feature gate", () => {
  it("redirects authenticated lending routes when production disables the feature", async () => {
    api.bootstrap.mockResolvedValue({ features: { personalLending: false } });
    renderRoute("/loans", [
      { path: "/loans", element: <PersonalLoansRoute /> },
      { path: "/overview", element: <h1>Overview destination</h1> },
    ]);

    expect(await screen.findByRole("heading", { name: "Overview destination" })).toBeInTheDocument();
    expect(screen.queryByText("Personal lending page")).not.toBeInTheDocument();
  });

  it("returns a generic not-found page for disabled invitation links", async () => {
    api.getAuthStatus.mockResolvedValue({ features: { personalLending: false } });
    renderRoute("/loan-invitations/private", [
      { path: "/loan-invitations/:token", element: <LoanInvitationRoute /> },
    ]);

    expect(await screen.findByRole("heading", { name: "This page does not exist" })).toBeInTheDocument();
    expect(screen.queryByText("Loan invitation page")).not.toBeInTheDocument();
  });
});
