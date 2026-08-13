import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ExpenseBreakdown } from "@/components/overview";
import type { OverviewCategoryOut } from "@/lib/generated/contracts";

const categories: OverviewCategoryOut[] = [
  {
    id: "food",
    label: "Food",
    amountMinor: 1_140_000,
    count: 2,
    sharePercent: 64.8,
    subcategories: [
      { id: "delivery", label: "Delivery", amountMinor: 700_000, count: 1, sharePercent: 61.4 },
      { id: "groceries", label: "Groceries", amountMinor: 440_000, count: 1, sharePercent: 38.6 },
    ],
  },
  {
    id: "transport",
    label: "Transport",
    amountMinor: 620_000,
    count: 1,
    sharePercent: 35.2,
    subcategories: [
      { id: "cab", label: "Cab", amountMinor: 620_000, count: 1, sharePercent: 100 },
    ],
  },
];

describe("ExpenseBreakdown", () => {
  it("reveals the selected category's subcategories without leaving the overview", () => {
    render(<ExpenseBreakdown categories={categories} currency="INR" />);

    const food = screen.getByRole("button", { name: /^Food/ });
    const transport = screen.getByRole("button", { name: /^Transport/ });
    expect(food).toHaveAttribute("aria-pressed", "true");
    expect(within(screen.getByLabelText("Food subcategories")).getByText("Delivery")).toBeVisible();

    fireEvent.pointerEnter(transport, { pointerType: "mouse" });

    expect(transport).toHaveAttribute("aria-pressed", "true");
    expect(food).toHaveAttribute("aria-pressed", "false");
    expect(within(screen.getByLabelText("Transport subcategories")).getByText("Cab")).toBeVisible();
    expect(screen.queryByLabelText("Food subcategories")).not.toBeInTheDocument();

    fireEvent.click(food);
    expect(food).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Food subcategories")).toBeVisible();
  });

  it("shows a useful empty state when the month has no expenses", () => {
    render(<ExpenseBreakdown categories={[]} currency="INR" />);
    expect(screen.getByRole("heading", { name: "No expenses recorded yet" })).toBeVisible();
  });
});
