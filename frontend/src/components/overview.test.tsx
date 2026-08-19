import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { cumulativeTrend, ExpenseBreakdown, SpendingLimit, visibleTrend } from "@/components/overview";
import type { OverviewCategoryOut, OverviewOut, OverviewTrendPointOut } from "@/lib/generated/contracts";

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

describe("overview trend", () => {
  it("builds cumulative cash flow and applies the selected time window", () => {
    const points: OverviewTrendPointOut[] = Array.from({ length: 31 }, (_, index) => ({
      day: index + 1,
      date: `2026-08-${String(index + 1).padStart(2, "0")}`,
      incomeMinor: index === 0 ? 100_000 : 0,
      spentMinor: 1_000,
      previousIncomeMinor: 0,
      previousSpentMinor: 800,
    }));

    const cumulative = cumulativeTrend(points);

    expect(cumulative[0]).toMatchObject({ income: 100_000, expenses: 1_000, balance: 99_000, previousExpenses: 800 });
    expect(cumulative[30]).toMatchObject({ income: 100_000, expenses: 31_000, balance: 69_000, previousExpenses: 24_800 });
    expect(visibleTrend(cumulative, "7")).toHaveLength(7);
    expect(visibleTrend(cumulative, "30")).toHaveLength(30);
    expect(visibleTrend(cumulative, "max")).toHaveLength(31);
    expect(visibleTrend(cumulative, "7")[0].day).toBe(25);
  });
});

describe("monthly budgets", () => {
  const overview: OverviewOut = {
    period: { start: "2026-08-01", end: "2026-08-15", previousStart: "2026-07-01", previousEnd: "2026-07-15", label: "August 2026", isCurrent: true },
    summary: { currency: "INR", incomeMinor: 35_000_000, spentMinor: 4_726_500, netMinor: 30_273_500, expenseCount: 40, previousSpentMinor: 0, changeMinor: 4_726_500, changePercent: null },
    categories: [],
    budgets: [
      { id: "overall", name: "Monthly spending budget", categoryId: null, categorySlug: null, category: null, amountMinor: 3_000_000, spentMinor: 4_726_500, remainingMinor: 0, overMinor: 1_726_500, percentUsed: 157.6, currency: "INR", period: "monthly" },
      { id: "food", name: "Food budget", categoryId: "food-id", categorySlug: "food", category: "Food", amountMinor: 1_200_000, spentMinor: 900_000, remainingMinor: 300_000, overMinor: 0, percentUsed: 75, currency: "INR", period: "monthly" },
    ],
    trend: [
      { day: 13, date: "2026-08-13", incomeMinor: 0, spentMinor: 1_000_000, previousIncomeMinor: 0, previousSpentMinor: 0 },
      { day: 14, date: "2026-08-14", incomeMinor: 0, spentMinor: 3_726_500, previousIncomeMinor: 0, previousSpentMinor: 0 },
    ],
    recentTransactions: [],
    accounts: [],
  };

  it("uses the overall budget and keeps category limits independent", () => {
    const onPlan = vi.fn();
    render(<SpendingLimit overview={overview} onPlan={onPlan} />);

    expect(screen.getByRole("heading", { name: "₹30,000 limit" })).toBeVisible();
    expect(screen.getByText("₹17,265 over")).toBeVisible();
    expect(screen.getByRole("group", { name: "Spending pace" })).toBeVisible();
    expect(screen.getByText("₹3,151")).toBeVisible();
    expect(screen.getByText("₹968")).toBeVisible();
    expect(screen.getByText("₹97,681")).toBeVisible();
    expect(within(screen.getByLabelText("Budget status")).getByText(/Budget crossed on 14 Aug/)).toBeVisible();
    expect(screen.getByRole("img", { name: "Daily spending bars with the daily budget pace marker" })).toBeVisible();
    expect(screen.queryByText("Food")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Category" }));

    expect(screen.getByRole("heading", { name: "1 category budget" })).toBeVisible();
    expect(screen.getByText("Food")).toBeVisible();
    expect(screen.getByText("₹3,000 left")).toBeVisible();
    expect(screen.getByText(/stay independent from the overall cap/)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Add category" }));
    expect(onPlan).toHaveBeenCalledTimes(2);
  });

  it("offers overall and category setup without inventing a second limit", () => {
    const onPlan = vi.fn();
    render(<SpendingLimit overview={{ ...overview, budgets: [] }} onPlan={onPlan} />);

    expect(screen.getByRole("heading", { name: "No overall limit" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Set overall budget/ }));

    fireEvent.click(screen.getByRole("button", { name: "Category" }));

    expect(screen.getByRole("heading", { name: "No category budgets" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Add category budget/ }));
    expect(onPlan).toHaveBeenCalledTimes(2);
  });

  it("keeps a cached overview without the new budgets field renderable", () => {
    const cachedOverview = { ...overview } as Partial<OverviewOut>;
    delete cachedOverview.budgets;

    render(<SpendingLimit overview={cachedOverview as OverviewOut} onPlan={() => undefined} />);

    expect(screen.getByRole("heading", { name: "No overall limit" })).toBeVisible();
    expect(screen.getByRole("button", { name: /Set overall budget/ })).toBeVisible();
  });
});
