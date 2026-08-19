import { render, screen } from "@testing-library/react";
import { cloneElement, type ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { ChartView } from "@/components/widget-library/chart";
import { formatMoney } from "@/lib/format";
import type { DataChartData } from "@/lib/protocol";

// jsdom has no layout, so ResponsiveContainer would measure 0×0 and render
// nothing. Handing the chart a fixed size exercises the real Recharts tree —
// axes, legend, marks — without faking any of it.
vi.mock("recharts", async (importOriginal) => {
  const original = await importOriginal<typeof import("recharts")>();
  return {
    ...original,
    ResponsiveContainer: ({ children }: { children: ReactElement }) => (
      <div style={{ width: 600, height: 300 }}>{cloneElement(children, { width: 600, height: 300 } as never)}</div>
    ),
  };
});

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= ResizeObserverStub;

const emptyEncoding = { x: null, y: null, color: null, size: null, theta: null, row: null, column: null, tooltip: [] };
const lineage = { origin: "analysis", manifestHash: "abc123", executedAt: "2026-08-18T10:00:00+00:00" };

const barData: DataChartData = {
  view: {
    id: "view-1",
    title: "Monthly spend",
    description: "Spend by month and category.",
    dataset: "monthly_spend",
    mark: "bar",
    encoding: {
      ...emptyEncoding,
      x: { field: "month", type: "ordinal", title: "Month", valueType: "string", sort: "ascending" },
      y: { field: "value_minor", type: "quantitative", title: "Spend", valueType: "money_minor", sort: null },
      color: { field: "category", type: "nominal", title: "Category", valueType: "category", sort: null },
    },
    height: 280,
  },
  rows: [
    { month: "2026-07", category: "Food", value_minor: 120_000 },
    { month: "2026-07", category: "Travel", value_minor: 80_000 },
    { month: "2026-08", category: "Food", value_minor: 50_000 },
  ],
  currency: "INR",
  lineage,
};

const arcData: DataChartData = {
  view: {
    id: "view-2",
    title: "Where it went",
    description: null,
    dataset: "category_share",
    mark: "arc",
    encoding: {
      ...emptyEncoding,
      theta: { field: "amount", type: "quantitative", title: "Amount", valueType: "money_minor", sort: null },
      color: { field: "label", type: "nominal", title: "Category", valueType: "category", sort: null },
    },
    height: 260,
  },
  rows: [
    { label: "Food", amount: 150_000 },
    { label: "Rent", amount: 350_000 },
  ],
  currency: "INR",
  lineage,
};

describe("ChartView", () => {
  it("renders a grouped bar chart with a legend and a money-formatted summary", () => {
    const { container } = render(<ChartView data={barData} />);

    expect(container.querySelector("svg")).toBeTruthy();
    // Two series → a legend is mandatory, and its entries carry the series names.
    expect(container.querySelector(".recharts-legend-wrapper")).toBeTruthy();
    expect(screen.getByText("Food")).toBeInTheDocument();
    expect(screen.getByText("Travel")).toBeInTheDocument();
    // Minor units divide by 100 on display: 250,000 minor → ₹2,500 in the
    // plot's full-sentence accessible name.
    const img = screen.getByRole("img");
    expect(img.getAttribute("aria-label")).toContain(formatMoney(250_000, "INR"));
    expect(img.getAttribute("aria-label")).toMatch(/Bar chart titled/);
  });

  it("omits the legend for a single series", () => {
    const single: DataChartData = { ...barData, view: { ...barData.view, encoding: { ...barData.view.encoding, color: null } } };
    const { container } = render(<ChartView data={single} />);

    expect(container.querySelector("svg")).toBeTruthy();
    expect(container.querySelector(".recharts-legend-wrapper")).toBeNull();
  });

  it("renders an arc as a pie with one sector per slice and a legend", async () => {
    const { container } = render(<ChartView data={arcData} />);

    expect(container.querySelector("svg")).toBeTruthy();
    expect(container.querySelectorAll(".recharts-pie-sector").length).toBe(2);
    expect(container.querySelector(".recharts-legend-wrapper")).toBeTruthy();
    // The pie registers its legend payload a tick after the sectors mount.
    expect(await screen.findByText("Rent")).toBeInTheDocument();
    expect(screen.getByText("Food")).toBeInTheDocument();
    expect(screen.getByRole("img").getAttribute("aria-label")).toContain(formatMoney(500_000, "INR"));
  });
});
