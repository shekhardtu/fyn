import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TransactionEditor, TransactionRow } from "@/components/money-pages";
import * as api from "@/lib/api";
import type { CategoryDirectoryOut, TransactionListItemOut } from "@/lib/protocol";
import { groupTransactionsByDay } from "@/lib/transaction-groups";

/** The dropdowns are Base UI comboboxes now: open the trigger, press the row. */
function chooseOption(label: string, option: string | RegExp) {
  fireEvent.click(screen.getByRole("combobox", { name: label }));
  fireEvent.click(screen.getByRole("option", { name: option }));
}

const categories: CategoryDirectoryOut[] = [{
  id: "42b9db9a-ff04-4ffc-b428-82bb3fb1eb80",
  slug: "transport",
  label: "Transport",
  icon: "car",
  editable: false,
  hints: [],
  subcategories: [{ id: "7d9b7570-1e89-4dcb-b0ad-d9dbbd0c0432", slug: "cab", label: "Cab", editable: false }],
}];

const transaction: TransactionListItemOut = {
  id: "16a3ff79-4035-427b-a538-6ce4bf2b608b",
  rowVersion: 1,
  transactionType: "expense",
  amountMinor: 54_000,
  currency: "INR",
  merchant: "Swiggy",
  transactionAt: "2026-08-13T08:30:00Z",
  status: "confirmed",
  categoryId: null,
  category: null,
  subcategoryId: null,
  subcategory: null,
  spendNature: "discretionary",
  location: null,
  sourceCount: 1,
  deletedAt: null,
};

afterEach(() => {
  vi.restoreAllMocks();
  Reflect.deleteProperty(navigator, "geolocation");
});

describe("TransactionRow", () => {
  it("keeps the edit affordance on an active row", () => {
    const onEdit = vi.fn();
    render(<TransactionRow transaction={transaction} onEdit={onEdit} />);

    expect(screen.queryByText("Removed")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit Swiggy transaction" }));
    expect(onEdit).toHaveBeenCalledWith(transaction);
  });

  it("strikes a removed row through, stamps the removal time, and drops the edit affordance", () => {
    render(<TransactionRow transaction={{ ...transaction, deletedAt: "2026-08-14T10:15:00Z" }} onEdit={vi.fn()} />);

    expect(screen.getByText("Removed")).toBeInTheDocument();
    expect(screen.getByText("Swiggy").className).toContain("line-through");
    expect(screen.getByText(/Removed \d{1,2} Aug 2026/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit Swiggy transaction" })).not.toBeInTheDocument();
  });

  it("offers removal on an active row when the page can remove", () => {
    const onRemove = vi.fn();
    render(<TransactionRow transaction={transaction} onEdit={vi.fn()} onRemove={onRemove} />);

    fireEvent.click(screen.getByRole("button", { name: "Remove Swiggy transaction" }));
    expect(onRemove).toHaveBeenCalledWith(transaction);
  });

  it("offers restore on a removed row when the page can restore", () => {
    const onRestore = vi.fn();
    const removed = { ...transaction, deletedAt: "2026-08-14T10:15:00Z" };
    render(<TransactionRow transaction={removed} onEdit={vi.fn()} onRestore={onRestore} />);

    fireEvent.click(screen.getByRole("button", { name: "Restore Swiggy transaction" }));
    expect(onRestore).toHaveBeenCalledWith(removed);
  });
});

describe("transaction day groups", () => {
  it("preserves every stable transaction row while producing contiguous group counts", () => {
    const items = [
      transaction,
      { ...transaction, id: "97975fca-938d-493b-a163-7dcf393849fa", merchant: "Auto rides", transactionAt: "2026-08-13T07:30:00Z" },
      { ...transaction, id: "d7e7b57f-48a7-4ed0-a839-dba471aff18e", merchant: "School fees", transactionAt: "2026-08-12T08:30:00Z" },
    ];

    const groups = groupTransactionsByDay(items);

    expect(groups.map((group) => group.transactions.length)).toEqual([2, 1]);
    expect(groups.flatMap((group) => group.transactions).map((item) => item.id)).toEqual(items.map((item) => item.id));
  });

  it("groups midnight-adjacent entries using the profile timezone", () => {
    const items = [
      { ...transaction, id: "97975fca-938d-493b-a163-7dcf393849fa", transactionAt: "2026-08-19T20:00:00Z" },
      { ...transaction, id: "d7e7b57f-48a7-4ed0-a839-dba471aff18e", transactionAt: "2026-08-19T17:00:00Z" },
    ];

    expect(groupTransactionsByDay(items, "Asia/Kolkata").map((group) => group.id)).toEqual(["2026-08-20", "2026-08-19"]);
    expect(groupTransactionsByDay(items, "America/New_York").map((group) => group.id)).toEqual(["2026-08-19"]);
  });
});

describe("TransactionEditor", () => {
  it("shows the same canonical transaction reference and current version as conversation cards", () => {
    render(<TransactionEditor transaction={{ ...transaction, rowVersion: 3 }} categories={categories} saving={false} problem={null} onClose={() => undefined} onSave={() => undefined} />);

    expect(screen.getByRole("button", { name: `Copy Transaction ID ${transaction.id}` })).toHaveTextContent("TXN 16A3FF79…BF2B608B");
    expect(screen.getByText("· Version 3")).toBeInTheDocument();
  });

  it("shows and saves a browser fix while prefilling its resolved place", async () => {
    const onSave = vi.fn();
    const getCurrentPosition = vi.fn((success: PositionCallback) => success({
      coords: { latitude: 12.971599, longitude: 77.594566, accuracy: 18 },
      timestamp: Date.now(),
    } as GeolocationPosition));
    Object.defineProperty(navigator, "geolocation", { configurable: true, value: { getCurrentPosition } });
    vi.spyOn(api, "resolveLocationLabel").mockResolvedValue("Bengaluru, Karnataka");

    render(<TransactionEditor transaction={null} categories={categories} saving={false} problem={null} locationAllowed onClose={() => undefined} onSave={onSave} />);

    expect(await screen.findByText("Coordinates 12.971599, 77.594566 · accuracy ±18 m")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Transaction location")).toHaveValue("Bengaluru, Karnataka"));
    expect(api.resolveLocationLabel).toHaveBeenCalledWith(12.971599, 77.594566);

    fireEvent.change(screen.getByLabelText("Transaction amount"), { target: { value: "42" } });
    fireEvent.change(screen.getByLabelText("Transaction date and time"), { target: { value: "2026-08-01T09:30" } });
    fireEvent.click(screen.getByRole("button", { name: "Add transaction" }));

    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      location: "Bengaluru, Karnataka",
      latitude: 12.971599,
      longitude: 77.594566,
      locationAccuracy: 18,
    }));
  });

  it("only lists taxonomy categories in the category picker", () => {
    const pickerCategories = [...categories, {
      id: "f2cd0b08-71d3-4142-b3c2-55bcf8f8ba9e",
      slug: "other",
      label: "Other",
      icon: "circle-ellipsis",
      editable: false,
      hints: [],
      subcategories: [],
    }];
    render(<TransactionEditor transaction={transaction} categories={pickerCategories} saving={false} problem={null} onClose={() => undefined} onSave={() => undefined} />);

    fireEvent.click(screen.getByRole("combobox", { name: "Transaction category" }));

    expect(screen.getByRole("option", { name: "Transport" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Other" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Uncategorized" })).not.toBeInTheDocument();
  });

  it("questions a close once the form is dirty, and only discards on the explicit button", () => {
    const onClose = vi.fn();
    render(<TransactionEditor transaction={transaction} categories={categories} saving={false} problem={null} onClose={onClose} onSave={() => undefined} />);

    // Pristine: Cancel closes straight away.
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText("Merchant"), { target: { value: "Zomato" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alertdialog", { name: "Discard unsaved changes" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Merchant")).toHaveValue("Zomato");

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("submits a page edit through the generated transaction contract", () => {
    const onSave = vi.fn();
    render(<TransactionEditor transaction={transaction} categories={categories} saving={false} problem={null} onClose={() => undefined} onSave={onSave} />);

    fireEvent.change(screen.getByLabelText("Transaction amount"), { target: { value: "725" } });
    fireEvent.change(screen.getByLabelText("Merchant"), { target: { value: "Uber" } });
    chooseOption("Transaction category", "Transport");
    chooseOption("Transaction subcategory", "Cab");
    fireEvent.change(screen.getByLabelText("Transaction location"), { target: { value: "Bengaluru" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      expectedVersion: 1,
      amountMinor: 72_500,
      merchant: "Uber",
      categoryId: categories[0].id,
      subcategoryId: categories[0].subcategories[0].id,
      location: "Bengaluru",
    }));
  });

  it("shows immutable amendment history and does not offer an accountless transfer conversion", () => {
    render(<TransactionEditor
      transaction={transaction}
      categories={categories}
      revisions={[{
        revisionNumber: 2,
        source: "conversation_edit",
        reason: null,
        changes: { amount_minor: { before: 25_000, after: 54_000 } },
        createdAt: "2026-08-14T10:15:00Z",
      }]}
      saving={false}
      problem={null}
      onClose={() => undefined}
      onSave={() => undefined}
    />);

    fireEvent.click(screen.getByText("Amendment history"));
    expect(screen.getByText("Version 2")).toBeInTheDocument();
    expect(screen.getByText(/Conversation edit/)).toBeInTheDocument();
    expect(screen.getByText(/₹250.*₹540/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("combobox", { name: "Transaction type" }));
    expect(screen.queryByRole("option", { name: "Transfer" })).not.toBeInTheDocument();
  });

  it("creates a subcategory from inside the dropdown and saves with it", async () => {
    const onSave = vi.fn();
    const created = { id: "3f7a1c9e-8d2b-4e5f-9a6c-1b2d3e4f5a6b", slug: "custom-rickshaw", label: "Rickshaw" };
    const onCreateSubcategory = vi.fn().mockResolvedValue(created);
    render(<TransactionEditor transaction={{ ...transaction, categoryId: categories[0].id, category: "Transport" }} categories={categories} saving={false} problem={null} onClose={() => undefined} onSave={onSave} onCreateSubcategory={onCreateSubcategory} />);

    fireEvent.click(screen.getByRole("combobox", { name: "Transaction subcategory" }));
    fireEvent.change(screen.getByPlaceholderText("Search or add new"), { target: { value: "Rickshaw" } });
    fireEvent.click(screen.getByRole("option", { name: /Add “Rickshaw”/ }));
    await waitFor(() => expect(onCreateSubcategory).toHaveBeenCalledWith(categories[0].id, "Rickshaw"));

    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ categoryId: categories[0].id, subcategoryId: created.id }));
  });

  it("keeps invalid amounts in the drawer and explains the correction", () => {
    const onSave = vi.fn();
    render(<TransactionEditor transaction={transaction} categories={categories} saving={false} problem={null} onClose={() => undefined} onSave={onSave} />);
    fireEvent.change(screen.getByLabelText("Transaction amount"), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(screen.getByRole("alert")).toHaveTextContent("greater than zero");
    expect(onSave).not.toHaveBeenCalled();
  });

  it("reuses the editor contract to add a transaction", () => {
    const onSave = vi.fn();
    render(<TransactionEditor transaction={null} categories={categories} saving={false} problem={null} onClose={() => undefined} onSave={onSave} />);

    expect(screen.getByRole("heading", { name: "Add transaction" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Transaction amount"), { target: { value: "32.50" } });
    fireEvent.change(screen.getByLabelText("Merchant"), { target: { value: "Namma Metro" } });
    fireEvent.click(screen.getByRole("button", { name: "Add transaction" }));

    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      amountMinor: 3_250,
      merchant: "Namma Metro",
      transactionType: "expense",
    }));
  });
});
