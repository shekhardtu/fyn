import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter } from "react-router";
import { RouterProvider } from "react-router/dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TransactionsPage } from "@/components/money-pages";

const api = vi.hoisted(() => ({
  loadCategories: vi.fn(),
  loadTransactions: vi.fn(),
  getPrivacyStatus: vi.fn(),
}));

vi.mock("@/components/workspace", async () => {
  const React = await import("react");
  return {
    useWorkspaceShell: () => ({ navOpen: false, openNav: vi.fn() }),
    useWorkspaceOverlay: () => React.useRef<HTMLElement>(null),
  };
});

vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  loadCategories: api.loadCategories,
  loadTransactions: api.loadTransactions,
  getPrivacyStatus: api.getPrivacyStatus,
}));

function renderPage(entry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([{ path: "/transactions", element: <TransactionsPage /> }], { initialEntries: [entry] });
  render(<QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider>);
  return router;
}

beforeEach(() => {
  api.loadCategories.mockResolvedValue([]);
  api.loadTransactions.mockResolvedValue([]);
  api.getPrivacyStatus.mockResolvedValue({ locationEnabled: false, sources: {} });
});

describe("Add transaction URL state", () => {
  it("opens from ?new=1 and keeps the parameter as the drawer source of truth", async () => {
    const router = renderPage("/transactions?new=1");

    expect(await screen.findByRole("heading", { name: "Add transaction" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?new=1");
  });

  it("sets ?new=1 when the Add transaction button opens the drawer", async () => {
    const router = renderPage("/transactions");
    const searches: string[] = [];
    router.subscribe((state) => searches.push(state.location.search));
    const add = await screen.findByRole("button", { name: "Add transaction" });
    await waitFor(() => expect(add).toBeEnabled());

    await act(async () => { fireEvent.click(add); });

    expect(searches).toContain("?new=1");
    await waitFor(() => expect(router.state.location.search).toBe("?new=1"));
    expect(await screen.findByRole("heading", { name: "Add transaction" })).toBeInTheDocument();

    // The search-box URL sync is debounced. Keep the assertion beyond that
    // boundary so an older closure cannot silently erase the new owner.
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 300)); });
    expect(router.state.location.search).toBe("?new=1");
  });
});
