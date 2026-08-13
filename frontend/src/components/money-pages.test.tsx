import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TransactionEditor } from "@/components/money-pages";
import type { CategoryDirectoryOut, TransactionListItemOut } from "@/lib/protocol";

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
};

describe("TransactionEditor", () => {
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
      amountMinor: 72_500,
      merchant: "Uber",
      categoryId: categories[0].id,
      subcategoryId: categories[0].subcategories[0].id,
      location: "Bengaluru",
    }));
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
