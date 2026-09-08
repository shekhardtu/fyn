import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FinanceFormDialog } from "@/components/finance-forms";
import * as api from "@/lib/api";
import type { FinanceFormKind } from "@/routing/paths";

vi.mock("@/lib/api", () => ({ loadBudgets: vi.fn(), loadGoals: vi.fn(), loadCategories: vi.fn(), saveBudget: vi.fn(), saveGoal: vi.fn(), createAccount: vi.fn(), contributeGoal: vi.fn() }));
const budget = { id: "budget-1", name: "Monthly budget", amountMinor: 500_000, currency: "INR", period: "monthly", categoryId: null };
const goal = { id: "goal-1", name: "Emergency fund", targetMinor: 1_000_000, currentMinor: 25_000, targetDate: "2027-01-01", currency: "INR" };

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.loadBudgets).mockResolvedValue([budget]);
  vi.mocked(api.loadGoals).mockResolvedValue([goal]);
  vi.mocked(api.loadCategories).mockResolvedValue([]);
});

function openForm(kind: FinanceFormKind, id?: string) {
  const onClose = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><FinanceFormDialog kind={kind} id={id} onClose={onClose} /></QueryClientProvider>);
  return { onClose };
}

describe("direct finance forms", () => {
  it("prefills and updates an existing overall limit rather than creating another budget", async () => {
    vi.mocked(api.saveBudget).mockResolvedValue({ ...budget, amountMinor: 650_000 });
    const { onClose } = openForm("budget");
    const amount = await screen.findByRole("textbox", { name: "Monthly limit" });
    expect(amount).toHaveValue("5000");
    fireEvent.change(amount, { target: { value: "6500" } });
    fireEvent.click(screen.getByRole("button", { name: "Save budget" }));
    await waitFor(() => expect(api.saveBudget).toHaveBeenCalledWith({ name: "Monthly budget", amountMinor: 650_000, categoryId: null }, "budget-1"));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  });

  it("keeps account details after failure and saves an amount owed in its selected currency", async () => {
    vi.mocked(api.createAccount).mockRejectedValue(new Error("Please try again."));
    const { onClose } = openForm("account");
    fireEvent.change(screen.getByRole("textbox", { name: "Account name" }), { target: { value: "Travel card" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Current balance" }), { target: { value: "45" } });
    fireEvent.click(screen.getByRole("button", { name: "Money owed" }));
    fireEvent.click(screen.getByRole("combobox", { name: "Account currency" }));
    fireEvent.click(screen.getByRole("option", { name: "US dollar (USD)" }));
    fireEvent.click(screen.getByRole("button", { name: "Add account" }));
    await screen.findByRole("alert");
    expect(api.createAccount).toHaveBeenCalledWith(expect.objectContaining({ name: "Travel card", balanceMinor: -4500, currency: "USD" }));
    expect(screen.getByRole("textbox", { name: "Account name" })).toHaveValue("Travel card");
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("alertdialog", { name: "Discard unsaved changes" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByRole("textbox", { name: "Current balance" })).toHaveValue("45");
  });

  it("retries a savings contribution with the same request identity", async () => {
    vi.mocked(api.contributeGoal).mockRejectedValueOnce(new Error("Connection lost.")).mockResolvedValueOnce({ ...goal, currentMinor: 30_000 });
    const { onClose } = openForm("contribution", goal.id);
    fireEvent.change(await screen.findByRole("textbox", { name: "Amount saved" }), { target: { value: "50" } });
    fireEvent.click(screen.getByRole("button", { name: "Add savings" }));
    await screen.findByRole("alert");
    const requestId = vi.mocked(api.contributeGoal).mock.calls[0][2];
    fireEvent.click(screen.getByRole("button", { name: "Add savings" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(api.contributeGoal).toHaveBeenNthCalledWith(1, goal.id, 5000, requestId);
    expect(api.contributeGoal).toHaveBeenNthCalledWith(2, goal.id, 5000, requestId);
  });

  it("keeps a missing goal URL from turning into a new goal", async () => {
    openForm("goal", "missing-goal");
    expect(await screen.findByRole("alert")).toHaveTextContent("no longer available");
    expect(screen.queryByRole("button", { name: "Save goal" })).not.toBeInTheDocument();
    expect(api.saveGoal).not.toHaveBeenCalled();
  });
});
