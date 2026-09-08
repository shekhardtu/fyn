import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DeleteAccountButton } from "@/components/delete-account-button";
import { deleteAccount } from "@/lib/api";

vi.mock("@/lib/api", () => ({ deleteAccount: vi.fn() }));
const account = { id: "account-1", name: "ICICI", accountType: "bank", balanceMinor: 0, currency: "INR", institution: null, mask: null };

beforeEach(() => vi.resetAllMocks());

function renderButton() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  client.setQueryData(["overview", "2026-09"], { accounts: [account, { ...account, id: "account-2", name: "Savings" }] });
  client.setQueryData(["transactions"], []);
  render(<QueryClientProvider client={client}><DeleteAccountButton account={account} /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Delete account ICICI" }));
  return { client };
}

describe("account deletion", () => {
  it("names the account and lets the user cancel without deleting it", async () => {
    renderButton();
    const dialog = await screen.findByRole("alertdialog", { name: "Delete account?" });
    expect(dialog).toHaveTextContent("ICICI");
    expect(dialog).toHaveTextContent("Your transactions will stay");
    const cancel = within(dialog).getByRole("button", { name: "Cancel" });
    await waitFor(() => expect(cancel).toHaveFocus());
    fireEvent.click(cancel);
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(deleteAccount).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole("button", { name: "Delete account ICICI" })).toHaveFocus());
  });

  it("waits for deletion and refreshes the account and ledger views", async () => {
    let resolve!: () => void;
    vi.mocked(deleteAccount).mockImplementation(() => new Promise<void>((done) => { resolve = done; }));
    const { client } = renderButton();
    fireEvent.click(await screen.findByRole("button", { name: "Delete account" }));
    expect(await screen.findByRole("button", { name: "Deleting…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    fireEvent.keyDown(screen.getByRole("alertdialog"), { key: "Escape" });
    expect(screen.getByRole("alertdialog")).toBeVisible();
    expect(deleteAccount).toHaveBeenCalledExactlyOnceWith(account.id);
    resolve();
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(client.getQueryData(["overview", "2026-09"])).toMatchObject({ accounts: [{ id: "account-2" }] });
    expect(client.getQueryState(["overview", "2026-09"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["transactions"])?.isInvalidated).toBe(true);
  });

  it("keeps the account on failure and allows retrying the deletion", async () => {
    vi.mocked(deleteAccount).mockRejectedValueOnce(new Error("Please try again.")).mockResolvedValueOnce(undefined);
    const { client } = renderButton();
    fireEvent.click(await screen.findByRole("button", { name: "Delete account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Please try again.");
    expect(client.getQueryData(["overview", "2026-09"])).toMatchObject({ accounts: [account, { id: "account-2" }] });
    fireEvent.click(screen.getByRole("button", { name: "Delete account" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(deleteAccount).toHaveBeenCalledTimes(2);
  });
});
