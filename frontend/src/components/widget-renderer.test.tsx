import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WidgetRenderer } from "@/components/widget-renderer";
import type { Widget } from "@/lib/protocol";

const { embedMock } = vi.hoisted(() => ({ embedMock: vi.fn() }));

vi.mock("vega-embed", () => ({
  default: embedMock,
}));

afterEach(() => {
  vi.restoreAllMocks();
  embedMock.mockReset();
  embedMock.mockImplementation(async (target: HTMLElement) => {
    target.setAttribute("role", "img");
    return { finalize: () => undefined };
  });
});

embedMock.mockImplementation(async (target: HTMLElement) => {
  target.setAttribute("role", "img");
  return { finalize: () => undefined };
});

describe("embedded analysis table width", () => {
  it("notifies its parent from the click event without a render-phase update", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const widget: Widget = {
      id: "analysis",
      type: "analysis_table",
      version: 1,
      data: {
        title: "Analysis",
        currency: "INR",
        queryResults: [{ name: "By category", metric: "gross_spend", start: "2026-08-01", end: "2026-08-11", rows: [{ category: "Food", value: 50_000 }] }],
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Use full conversation width" }));

    expect(screen.getByRole("button", { name: "Use normal table width" })).toBeInTheDocument();
    expect(consoleError).not.toHaveBeenCalled();
  });
});

describe("saved transaction editor", () => {
  it("offers a backend-owned cancel action without submitting changes", () => {
    const onAction = vi.fn();
    const widget: Widget = {
      id: "edit-saved",
      type: "transaction_edit",
      version: 1,
      data: {
        transactionId: "transaction-1",
        title: "Edit saved transaction",
        amountMinor: 30_000,
        currency: "INR",
        transactionAt: "2026-08-11T10:30:00Z",
        transactionType: "expense",
        spendNature: "essential",
        fields: ["amount", "transaction_at", "transaction_type", "spend_nature"],
      },
      actions: [
        { id: "update", label: "Apply changes", action: "update_saved_transaction", style: "primary", payload: { transactionId: "transaction-1" } },
      ],
    };

    render(<WidgetRenderer widget={widget} onAction={onAction} />);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onAction).toHaveBeenCalledWith("edit-saved", "cancel_saved_transaction_edit", { transactionId: "transaction-1" });
    expect(onAction).not.toHaveBeenCalledWith(expect.anything(), "update_saved_transaction", expect.anything());
  });
});

describe("subcategory selector", () => {
  it("uses one choice mark instead of adding a duplicate generic circle icon", () => {
    const widget: Widget = {
      id: "subcategory-transport",
      type: "subcategory_selector",
      version: 1,
      data: {
        title: "What type of transport expense?",
        category: "Transport",
        options: [{ id: "cab", slug: "cab", label: "Cab" }],
      },
      actions: [{ id: "select", label: "Select", action: "select_subcategory", style: "secondary", payload: { draftId: "draft" } }],
    };
    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);
    const cab = screen.getByRole("button", { name: "Cab" });
    expect(cab.querySelectorAll("svg")).toHaveLength(0);
  });
});

describe("persisted widget action receipts", () => {
  it("shows a completed subcategory name as a disabled, unfocused value", () => {
    const widget: Widget = {
      id: "taxonomy-flights",
      type: "taxonomy_editor",
      version: 1,
      data: {
        operation: "create_subcategory",
        name: "Flights",
        parentCategory: "Travelling",
        appliesToDraft: true,
        lifecycle: "completed",
        completion: { action: "create_subcategory", values: { name: "Flights" } },
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    const input = screen.getByRole("textbox", { name: "New subcategory name" });
    expect(input).toHaveValue("Flights");
    expect(input).toBeDisabled();
    expect(input).not.toHaveFocus();
    expect(screen.getByText("Flights was added under Travelling.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add subcategory" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });

  it("fully disables inputs when a form widget is retired by a newer turn", () => {
    const widget: Widget = {
      id: "retired-edit",
      type: "transaction_edit",
      version: 1,
      data: {
        transactionId: "transaction-1",
        title: "Edit saved transaction",
        amountMinor: 30_000,
        currency: "INR",
        transactionAt: "2026-08-11T10:30:00Z",
        transactionType: "expense",
        fields: ["amount", "transaction_at", "transaction_type"],
      },
      actions: [{ id: "update", label: "Apply changes", action: "update_saved_transaction", style: "primary", payload: { transactionId: "transaction-1" } }],
    };

    render(<WidgetRenderer widget={widget} disabled onAction={() => undefined} />);

    expect(screen.getByRole("textbox", { name: "Transaction amount" })).toBeDisabled();
    expect(screen.getByLabelText("Transaction date and time")).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Transaction type" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Apply changes" })).toBeDisabled();
  });
});

describe("amount-only transaction editor", () => {
  it("submits only the field declared by the backend", () => {
    const onAction = vi.fn();
    const widget: Widget = {
      id: "amount-only",
      type: "transaction_edit",
      version: 1,
      data: {
        draftId: "b85f2065-2cff-40a1-a9d0-9bfabb0a1125",
        title: "Add the missing amount",
        fields: ["amount"],
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={onAction} />);
    fireEvent.change(screen.getByRole("textbox", { name: "Transaction amount" }), { target: { value: "500" } });
    fireEvent.click(screen.getByRole("button", { name: "Save this entry" }));

    expect(onAction).toHaveBeenCalledWith(
      "amount-only",
      "update_transaction_draft",
      { draftId: "b85f2065-2cff-40a1-a9d0-9bfabb0a1125", amountMinor: 50_000 },
    );
  });
});

describe("governed chart renderer", () => {
  it("renders analyzer-selected chart JSON through the generic data-chart component", () => {
    const widget: Widget = {
      id: "entertainment-chart",
      type: "data_chart",
      version: 1,
      data: {
        title: "Entertainment transactions by amount",
        body: "Individual transactions are ordered by recorded amount.",
        chartType: "bar",
        rows: [
          { transaction: "tx-1", merchant: "Cinema", transaction_date: "2026-08-11", value: 50_000 },
          { transaction: "tx-2", merchant: "Toit", transaction_date: "2026-08-10", value: 10_000 },
        ],
        xAxis: { key: "transaction", label: "Transaction", type: "category" },
        series: [{ key: "value", label: "Gross Spend", valueType: "money", currency: "INR" }],
        labelKeys: ["merchant", "transaction_date"],
        emptyMessage: "No matching transactions.",
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    expect(screen.getAllByText("Entertainment transactions by amount")).toHaveLength(1);
    expect(screen.getByRole("img", { name: /2 plotted data points/ })).toBeInTheDocument();
  });

  it("renders a generic subcategory breakdown with guidance instead of a blank Vega surface", () => {
    const widget: Widget = {
      id: "food-breakdown",
      type: "data_visualization",
      version: 1,
      data: {
        title: "Financial analysis",
        body: "Composed from governed semantic query results.",
        datasets: {
          breakdown: [
            { label: "Delivery", basis_points: 4_954, amount_minor: 5_159_000 },
            { label: "Dining", basis_points: 2_788, amount_minor: 2_904_000 },
            { label: "Coffee", basis_points: 1_150, amount_minor: 1_197_000 },
          ],
        },
        views: [{
          id: "food-by-subcategory",
          title: "Food amount by subcategory",
          dataset: "breakdown",
          mark: "arc",
          height: 320,
          encoding: {
            color: { field: "label", type: "nominal", title: "Subcategory", valueType: "category" },
            theta: { field: "basis_points", type: "quantitative", title: "Share", valueType: "percentage" },
            tooltip: [
              { field: "label", type: "nominal", title: "Subcategory", valueType: "category" },
              { field: "amount_minor", type: "quantitative", title: "Amount", valueType: "money_minor" },
            ],
          },
        }],
        layout: { columns: 1 },
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    expect(screen.getByRole("img", { name: /3 plotted data points/ })).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Subcategory legend" })).toHaveTextContent("Delivery49.5%");
    expect(screen.getByText("₹92,600")).toBeInTheDocument();
    expect(screen.getByText(/How to read this:/).closest("p")).toHaveTextContent("Segments represent Subcategory; their size represents Share.");
    expect(embedMock).not.toHaveBeenCalled();
  });

  it("keeps financial summaries chart-free", () => {
    const widget: Widget = {
      id: "summary",
      type: "financial_summary",
      version: 1,
      data: {
        title: "Coffee spending · This month",
        period: "Aug 01 – Aug 11",
        amountMinor: 20_000,
        currency: "INR",
        count: 1,
        breakdown: [{ label: "Food → Coffee", amount_minor: 20_000 }],
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText("Food → Coffee")).toBeInTheDocument();
  });

  it("renders a governed temporal heatmap with valid localized money formatting and facet width", async () => {
    const widget: Widget = {
      id: "time-heatmap",
      type: "data_visualization",
      version: 1,
      data: {
        title: "Transaction activity",
        datasets: { hourly: [
          { transaction_type: "expense", time_bucket: "2026-08-10", time_segment: "09", value: 50_000 },
          { transaction_type: "expense", time_bucket: "2026-08-11", time_segment: "10", value: 10_000 },
        ] },
        views: [{
          id: "heatmap", title: "Transaction amount by day and hour", dataset: "hourly", mark: "rect", height: 320,
          encoding: {
            x: { field: "time_segment", type: "ordinal", title: "Hour", valueType: "category" },
            y: { field: "time_bucket", type: "temporal", title: "Day", valueType: "datetime" },
            color: { field: "value", type: "quantitative", title: "Amount", valueType: "money_minor" },
            row: { field: "transaction_type", type: "nominal", title: "Type", valueType: "category" },
            tooltip: [],
          },
        }],
        layout: { columns: 1 },
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    expect(screen.getByText("Transaction amount by day and hour")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /2 plotted data points/ })).toBeInTheDocument();
    await waitFor(() => expect(embedMock).toHaveBeenCalled());
    const [, spec, options] = embedMock.mock.calls.at(-1)!;
    expect(spec.encoding.color.format).toBe("$,.2f");
    expect(JSON.stringify(spec)).not.toContain("₹,.2f");
    expect(typeof spec.width).toBe("number");
    expect(options.formatLocale.currency).toEqual(["₹", ""]);
  });

  it("surfaces a retryable message when the specialized renderer rejects", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    embedMock.mockRejectedValueOnce(new Error("renderer failed"));
    const widget: Widget = {
      id: "failed-heatmap",
      type: "data_visualization",
      version: 1,
      data: {
        title: "Transaction activity",
        datasets: { hourly: [{ day: "2026-08-11", hour: "10", value: 10_000 }] },
        views: [{
          id: "heatmap", title: "Amount by hour", dataset: "hourly", mark: "rect", height: 240,
          encoding: {
            x: { field: "hour", type: "ordinal", title: "Hour", valueType: "category" },
            y: { field: "day", type: "temporal", title: "Day", valueType: "datetime" },
            color: { field: "value", type: "quantitative", title: "Amount", valueType: "money_minor" },
            tooltip: [],
          },
        }],
        layout: { columns: 1 },
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("The chart renderer hit a problem.");
    expect(screen.getByRole("button", { name: "Retry chart" })).toBeInTheDocument();
    expect(consoleError).toHaveBeenCalledWith("Governed chart renderer failed", expect.any(Error));
  });
});

describe("persisted agent activity", () => {
  it("treats a legacy open step as a failed terminal run, not live work", () => {
    const widget: Widget = {
      id: "stale-run",
      type: "agent_activity",
      version: 1,
      data: {
        title: "Agno agent run",
        engine: "Agno harness",
        model: "gpt-5.6-luna",
        totalMs: 20_000,
        steps: [{ id: "reroute", label: "Rerouting", status: "running", durationMs: 0, cumulativeMs: 20_000 }],
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    expect(screen.getByRole("button", { name: /This run hit a problem/ })).toBeInTheDocument();
    expect(screen.queryByText("Working on it")).not.toBeInTheDocument();
  });
});
