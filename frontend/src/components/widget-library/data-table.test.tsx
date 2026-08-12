import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DataTableView } from "@/components/widget-library/data-table";
import type { DataTableData } from "@/lib/protocol";

const data: DataTableData = {
  title: "Open invoices",
  body: "Generated from governed invoice rows.",
  columns: [
    { key: "customer", label: "Customer", type: "entity", align: "left", priority: "primary", currencyKey: null, secondaryKeys: ["status"] },
    { key: "dueDate", label: "Due date", type: "date", align: "left", priority: "secondary", currencyKey: null, secondaryKeys: [] },
    { key: "amountMinor", label: "Amount", type: "money", align: "right", priority: "primary", currencyKey: "currency", secondaryKeys: [] },
  ],
  rows: [{ id: "invoice-1", customer: "Acme", status: "Overdue", dueDate: "2026-08-30", amountMinor: 125_000, currency: "INR", _capabilities: ["invoice.view"] }],
  rowIdKey: "id",
  rowActions: [
    { id: "view", label: "View", action: "edit_saved_transaction", style: "secondary", resourceKey: "id", payloadKey: "invoiceId", icon: "view", capability: "invoice.view" },
    { id: "remove", label: "Remove", action: "request_remove_transaction", style: "danger", resourceKey: "id", payloadKey: "invoiceId", icon: "remove", capability: "invoice.remove" },
  ],
  capabilitiesKey: "_capabilities",
  emptyMessage: "No invoices.",
};

describe("DataTableView", () => {
  it("renders generated columns and only backend-authorized actions", () => {
    const onAction = vi.fn();
    render(<DataTableView data={data} onAction={onAction} />);

    expect(screen.getByRole("columnheader", { name: "Customer" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /Open invoices table/ })).toHaveAttribute("tabindex", "0");
    expect(screen.getByText("Acme")).toBeInTheDocument();
    expect(screen.getByText("₹1,250")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "View" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "View" }));
    expect(onAction).toHaveBeenCalledWith("edit_saved_transaction", { invoiceId: "invoice-1" });
  });

  it("shows ten rows first, reports the remainder, and opens a full-width view", () => {
    const rows = Array.from({ length: 12 }, (_, index) => ({
      id: `invoice-${index + 1}`,
      customer: `Customer ${index + 1}`,
      status: "Open",
      dueDate: "2026-08-30",
      amountMinor: 10_000 + index,
      currency: "INR",
      _capabilities: [],
    }));
    render(<DataTableView data={{ ...data, rows, rowActions: [] }} />);

    expect(screen.getByText("Showing", { exact: false })).toHaveTextContent("Showing 10 of 12 · 2 remaining");
    expect(screen.queryByText("Customer 11")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show all 12" }));
    expect(screen.getByText("Customer 11")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Use full conversation width" }));
    expect(screen.getByRole("button", { name: "Use normal table width" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Maximize table" }));
    expect(screen.getByRole("dialog", { name: "Open invoices maximized table" })).toBeInTheDocument();
  });
});
