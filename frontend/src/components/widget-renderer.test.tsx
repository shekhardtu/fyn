import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

describe("clarification HITL", () => {
  const clarificationId = "7ab22bba-3f79-47cc-a545-f1016002a910";
  const widget: Widget = {
    id: `clarification-${clarificationId}`,
    type: "clarification",
    version: 1,
    data: {
      clarificationId,
      title: "One detail needs your confirmation",
      question: "Which installment assumption should control the chart?",
      reason: "The supplied tenure and payment produce different schedules.",
      conflictFields: ["tenure", "monthly installment"],
      options: [
        { id: "use_tenure", label: "Keep the 2-year tenure", description: "Calculate the required monthly installment." },
        { id: "use_installment", label: "Keep the ₹2,000 installment", description: "Calculate the resulting tenure." },
      ],
      allowCustom: true,
      customLabel: "Use another assumption",
    },
    actions: [
      { id: "use_tenure", label: "Keep the 2-year tenure", action: "resolve_clarification", style: "primary", payload: { clarificationId, optionId: "use_tenure" } },
      { id: "use_installment", label: "Keep the ₹2,000 installment", action: "resolve_clarification", style: "secondary", payload: { clarificationId, optionId: "use_installment" } },
      { id: "custom", label: "Use another assumption", action: "resolve_clarification", style: "secondary", payload: { clarificationId, optionId: "custom" } },
      { id: "cancel", label: "Cancel", action: "resolve_clarification", style: "ghost", payload: { clarificationId, optionId: "cancel" } },
    ],
  };

  it("submits a server-authored suggestion with one click", () => {
    const onAction = vi.fn();
    render(<WidgetRenderer widget={widget} onAction={onAction} />);

    fireEvent.click(screen.getByRole("button", { name: /Keep the 2-year tenure/ }));

    expect(onAction).toHaveBeenCalledWith(widget.id, "resolve_clarification", {
      clarificationId,
      optionId: "use_tenure",
    });
  });

  it("supports a bounded custom clarification", () => {
    const onAction = vi.fn();
    render(<WidgetRenderer widget={widget} onAction={onAction} />);

    expect(screen.queryByRole("textbox", { name: "Custom clarification" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Use another assumption" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Custom clarification" }), {
      target: { value: "Use a 36-month tenure" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(onAction).toHaveBeenCalledWith(widget.id, "resolve_clarification", {
      clarificationId,
      optionId: "custom",
      customText: "Use a 36-month tenure",
    });
  });

  it("keeps a quiet cancellation path beside the decision", () => {
    const onAction = vi.fn();
    render(<WidgetRenderer widget={widget} onAction={onAction} />);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onAction).toHaveBeenCalledWith(widget.id, "resolve_clarification", {
      clarificationId,
      optionId: "cancel",
    });
  });

  it("gives legacy persisted clarifications a protocol-level escape", () => {
    const onCancel = vi.fn();
    const legacy = { ...widget, actions: widget.actions.filter((action) => action.id !== "cancel") };
    render(<WidgetRenderer widget={legacy} onCancel={onCancel} onAction={() => undefined} />);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("keeps protocol fields and repeated question copy out of the decision card", () => {
    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    expect(screen.queryByText(widget.data.question as string)).not.toBeInTheDocument();
    expect(screen.queryByText(/Conflicting or missing inputs/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/monthly installment/, { selector: "p" })).not.toBeInTheDocument();
  });

  it("collapses a cancelled clarification to one quiet receipt", () => {
    const cancelled: Widget = {
      ...widget,
      data: {
        ...widget.data,
        lifecycle: "cancelled",
        completion: { action: "resolve_clarification", values: { optionId: "use_installment" } },
      },
    };

    render(<WidgetRenderer widget={cancelled} onAction={() => undefined} />);

    expect(screen.getByRole("status")).toHaveTextContent("Cancelled");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByText(widget.data.reason as string)).not.toBeInTheDocument();
  });
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

  it("does not submit hidden expense classification after changing Type to income", () => {
    const onAction = vi.fn();
    const categoryId = "42b9db9a-ff04-4ffc-b428-82bb3fb1eb80";
    const subcategoryId = "7d9b7570-1e89-4dcb-b0ad-d9dbbd0c0432";
    const widget: Widget = {
      id: "edit-direction",
      type: "transaction_edit",
      version: 1,
      data: {
        transactionId: "transaction-1",
        amountMinor: 30_000,
        transactionType: "expense",
        spendNature: "discretionary",
        categoryId,
        subcategoryId,
        categories: [{ id: categoryId, label: "Food" }],
        subcategories: [{ id: subcategoryId, categoryId, label: "Dining" }],
        fields: ["amount", "transaction_type", "spend_nature", "category", "subcategory"],
      },
      actions: [{ id: "update", label: "Apply changes", action: "update_saved_transaction", style: "primary", payload: { transactionId: "transaction-1" } }],
    };

    render(<WidgetRenderer widget={widget} onAction={onAction} />);
    fireEvent.click(screen.getByRole("combobox", { name: "Transaction type" }));
    fireEvent.click(screen.getByRole("option", { name: "income" }));
    expect(screen.queryByRole("combobox", { name: "Spend nature" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Transaction category" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));

    expect(onAction).toHaveBeenCalledWith("edit-direction", "update_saved_transaction", {
      transactionId: "transaction-1",
      amountMinor: 30_000,
      transactionType: "income",
    });
  });
});

describe("transaction preview classification", () => {
  it("shows direction and leaf without repeating the income root", () => {
    const widget: Widget = {
      id: "income-preview",
      type: "transaction_preview",
      version: 1,
      data: {
        transactionId: "transaction-1",
        title: "Employer",
        amountMinor: 50_000,
        currency: "INR",
        transactionType: "income",
        category: "Income",
        subcategory: "Salary",
        transactionAt: "2026-08-14T08:30:00Z",
        status: "Saved",
        sourceCount: 1,
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);
    expect(screen.getByText(/Income · Salary/)).toBeInTheDocument();
    expect(screen.queryByText(/Income → Income/)).not.toBeInTheDocument();
  });

  it.each([
    ["essential", "Essential"],
    ["potentially_avoidable", "Potentially avoidable"],
  ])("formats the %s spend nature as %s", (spendNature, expectedLabel) => {
    const widget: Widget = {
      id: `expense-preview-${spendNature}`,
      type: "transaction_preview",
      version: 1,
      data: {
        transactionId: "transaction-1",
        title: "Groceries",
        amountMinor: 30_000,
        currency: "INR",
        transactionType: "expense",
        category: "Food",
        subcategory: "Groceries",
        spendNature: spendNature as "essential" | "potentially_avoidable",
        transactionAt: "2026-08-14T08:30:00Z",
        status: "Saved",
        sourceCount: 1,
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);
    expect(screen.getByText(expectedLabel)).toBeInTheDocument();
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

describe("account selector HITL", () => {
  const draftId = "b85f2065-2cff-40a1-a9d0-9bfabb0a1125";
  const widget: Widget = {
    id: "source-account",
    type: "account_selector",
    version: 1,
    data: {
      draftId,
      role: "source_account",
      title: "Which account did the money leave?",
      body: "Choose a saved account or enter a name.",
      options: [],
    },
    actions: [
      { id: "select", label: "Select account", action: "select_account", style: "primary", payload: { draftId, role: "source_account" } },
      { id: "change-type", label: "Change type", action: "revisit_transaction_step", style: "secondary", payload: { draftId, step: "transaction_type" } },
      { id: "cancel", label: "Cancel transaction", action: "cancel_transaction_draft", style: "ghost", payload: { draftId } },
    ],
  };

  it("accepts the first account inline while keeping back and cancel available", () => {
    const onAction = vi.fn();
    render(<WidgetRenderer widget={widget} onAction={onAction} />);

    expect(screen.queryByText("No saved accounts yet.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Change type" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel transaction" }));
    expect(onAction).toHaveBeenCalledWith(widget.id, "cancel_transaction_draft", { draftId });

    fireEvent.change(screen.getByRole("textbox", { name: "Account name" }), { target: { value: "HDFC" } });
    const continueButton = screen.getByRole("button", { name: "Continue" });
    expect(continueButton).toHaveClass("h-[var(--h-field)]");
    fireEvent.click(continueButton);
    expect(onAction).toHaveBeenCalledWith(widget.id, "select_account", {
      draftId,
      role: "source_account",
      accountName: "HDFC",
    });
  });

  it("repairs an active legacy card with a governed cancel transition", () => {
    render(<WidgetRenderer widget={{ ...widget, actions: widget.actions.slice(0, 1) }} onAction={() => undefined} />);
    expect(screen.getByRole("button", { name: "Cancel transaction" })).toBeInTheDocument();
  });

  it("keeps every action in place while only the submitted transition spins", () => {
    const onAction = vi.fn();
    const view = render(<WidgetRenderer widget={widget} onAction={onAction} />);
    fireEvent.click(screen.getByRole("button", { name: "Change type" }));

    view.rerender(<WidgetRenderer widget={widget} pending disabled onAction={onAction} />);
    expect(screen.getByRole("button", { name: "Change type" }).querySelector(".animate-spin")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel transaction" }).querySelector(".animate-spin")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel transaction" })).toBeDisabled();
  });
});

describe("persisted widget action receipts", () => {
  it("collapses a completed subcategory form to its recorded value", () => {
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

    expect(screen.getByRole("status")).toHaveTextContent(/Added.*Flights/);
    expect(screen.queryByRole("textbox", { name: "New subcategory name" })).not.toBeInTheDocument();
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
    fireEvent.click(screen.getByRole("button", { name: "Save entry" }));

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

  it("does not claim zero spending when a summary simply has no breakdown", () => {
    const widget: Widget = {
      id: "summary-total",
      type: "financial_summary",
      version: 1,
      data: {
        title: "Spending · All time",
        period: "Beginning – Aug 14",
        amountMinor: 1_140_000,
        currency: "INR",
        count: 3,
        breakdown: [],
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    expect(screen.getByText("₹11,400")).toBeInTheDocument();
    expect(screen.queryByText("No spending recorded in this period yet.")).not.toBeInTheDocument();
  });

  it("keeps the empty-state copy for a period with genuinely nothing recorded", () => {
    const widget: Widget = {
      id: "summary-empty",
      type: "financial_summary",
      version: 1,
      data: {
        title: "Spending · This month",
        period: "Aug 01 – Aug 14",
        amountMinor: 0,
        currency: "INR",
        count: 0,
        breakdown: [],
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    expect(screen.getByText("No spending recorded in this period yet.")).toBeInTheDocument();
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
  it("renders one decision line and treats a legacy open step as failed", () => {
    const widget: Widget = {
      id: "stale-run",
      type: "agent_activity",
      version: 1,
      data: {
        title: "Agno agent run",
        engine: "Agno harness",
        model: "gpt-5.6-luna",
        summary: "Kept the previous Housing scope and narrowed it to July.",
        totalMs: 20_000,
        steps: [
          { id: "request", label: "Request received", status: "completed", durationMs: 0, cumulativeMs: 2 },
          { id: "reroute", label: "Rerouting", status: "running", durationMs: 0, cumulativeMs: 20_000 },
        ],
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    expect(screen.getByRole("button", { name: /Agent run failed:/ })).toBeInTheDocument();
    expect(screen.queryByText("Working on it")).not.toBeInTheDocument();
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByTestId("agent-activity-details")).toHaveAttribute("aria-hidden", "true");
  });

  it("starts with one reasoning line and expands to the complete multiline transcript", () => {
    const widget: Widget = {
      id: "finished-run",
      type: "agent_activity",
      version: 1,
      data: {
        summary: "Compared the current question with the previous Housing result.",
        reasoningTrace: "**Compared the current question with the previous Housing result.**\n\n- Kept the July scope.\n- Removed the merchant filter.",
        debugTrace: true,
        totalMs: 7360,
        steps: [
          { id: "request", label: "Request received", status: "completed", durationMs: 0, cumulativeMs: 3 },
          { id: "classification", label: "Unified agent completed the answer", tool: "unified_read_agent", status: "completed", durationMs: 7100, cumulativeMs: 7360 },
        ],
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    expect(screen.getByText("Compared the current question with the previous Housing result.")).toBeInTheDocument();
    const reasoning = screen.getByRole("button");
    expect(within(reasoning).getByText("7.36 s")).toBeInTheDocument();
    expect(within(reasoning).getByText("Single-pass route")).toBeInTheDocument();
    const details = screen.getByTestId("agent-activity-details");
    const transition = details.parentElement;
    expect(details).toHaveAttribute("aria-hidden", "true");
    expect(transition).toHaveClass("grid-rows-[0fr]", "opacity-0", "duration-300");

    fireEvent.click(reasoning);

    expect(reasoning).toHaveAttribute("aria-expanded", "true");
    expect(details).toHaveAttribute("aria-hidden", "false");
    expect(transition).toHaveClass("grid-rows-[1fr]", "opacity-100");
    expect(details).toHaveTextContent("Kept the July scope.");
    expect(details).toHaveTextContent("Removed the merchant filter.");
    expect(within(details).getByText("Execution trace")).toBeInTheDocument();
    expect(within(details).getByText("Request received")).toBeInTheDocument();
    expect(within(details).getByText("Unified agent completed the answer")).toBeInTheDocument();
    expect(details).toHaveTextContent("Stage request");
    expect(details).toHaveTextContent("Tool unified_read_agent");
    expect(details).toHaveTextContent("<1 ms step · 3 ms total elapsed");
    expect(details).toHaveTextContent("7.10 s step · 7.36 s total elapsed");

    fireEvent.click(reasoning);
    expect(details).toHaveAttribute("aria-hidden", "true");
    expect(transition).toHaveClass("grid-rows-[0fr]", "opacity-0");
  });

  it("identifies and expands a multi-pass router trace", () => {
    const widget: Widget = {
      id: "multi-pass-run",
      type: "agent_activity",
      version: 1,
      data: {
        summary: "Rechecked the first route after validation rejected it.",
        reasoningTrace: "The initial route was incomplete, so I revised it and validated the correction.",
        debugTrace: true,
        totalMs: 14_000,
        steps: [
          { id: "router", label: "Router produced a typed decision", tool: "agno_router", status: "completed", durationMs: 5000, cumulativeMs: 5000 },
          { id: "validator", label: "Validator rejected the route", tool: "agno_validator", status: "completed", durationMs: 2500, cumulativeMs: 7500 },
          { id: "reroute", label: "Rerouted with the analysis model", tool: "agno_reroute", status: "completed", durationMs: 4000, cumulativeMs: 11_500 },
          { id: "revalidation", label: "Validated the revised route", tool: "agno_validator", status: "completed", durationMs: 2500, cumulativeMs: 14_000 },
        ],
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    const reasoning = screen.getByRole("button");
    expect(within(reasoning).getByText("4-pass route")).toBeInTheDocument();
    fireEvent.click(reasoning);

    const details = screen.getByTestId("agent-activity-details");
    expect(details).toHaveTextContent("Execution trace");
    expect(details).toHaveTextContent("4-pass route");
    expect(details).toHaveTextContent("Router produced a typed decision");
    expect(details).toHaveTextContent("Stage router · Tool agno_router");
    expect(details).toHaveTextContent("5.00 s step · 5.00 s total elapsed");
    expect(details).toHaveTextContent("Validated the revised route");
    expect(details).toHaveTextContent("Stage revalidation · Tool agno_validator");
    expect(details).toHaveTextContent("2.50 s step · 14.0 s total elapsed");
  });
});
