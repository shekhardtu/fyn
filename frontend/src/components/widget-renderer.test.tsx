import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WidgetRenderer, widgetRegistry } from "@/components/widget-renderer";
import { widgetTypeIds, type Widget } from "@/lib/protocol";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("widget registry", () => {
  it("has an explicit renderer for every backend-authored widget type", () => {
    expect(Object.keys(widgetRegistry).sort()).toEqual(Object.values(widgetTypeIds).sort());
  });
});

describe("budget HITL", () => {
  it("shows recorded category spend and submits an edited monthly limit", () => {
    const onAction = vi.fn();
    const widget: Widget = {
      id: "budget-draft",
      type: "budget_progress",
      version: 1,
      data: {
        budgetId: "draft",
        title: "Travel budget",
        body: "Monthly budget",
        amountMinor: 2_000_000,
        spentMinor: 500_000,
        remainingMinor: 1_500_000,
        percentUsed: 25,
        currency: "INR",
        categorySlug: "travel",
      },
      actions: [
        { id: "save", label: "Set budget", action: "save_budget", style: "primary", payload: { name: "Travel budget", amountMinor: 2_000_000 } },
        { id: "cancel", label: "Cancel", action: "cancel_pending_action", style: "ghost", payload: { resourceId: "draft" } },
      ],
    };

    render(<WidgetRenderer widget={widget} onAction={onAction} />);

    expect(screen.getByText("₹5,000")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Monthly budget amount" }), { target: { value: "15000" } });
    fireEvent.click(screen.getByRole("button", { name: "Set budget" }));

    expect(onAction).toHaveBeenCalledWith(widget.id, "save_budget", {
      name: "Travel budget",
      amountMinor: 1_500_000,
    });
  });
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

  it("opens the value field immediately when custom input is the only valid response", () => {
    const onAction = vi.fn();
    const amountOnly: Widget = {
      ...widget,
      data: {
        ...widget.data,
        question: "What monthly amount should I use for the Construction budget?",
        options: [],
        customLabel: "Enter monthly amount",
      },
      actions: widget.actions.filter((action) => ["custom", "cancel"].includes(action.id)),
    };

    render(<WidgetRenderer widget={amountOnly} onAction={onAction} />);

    const input = screen.getByRole("textbox", { name: "Custom clarification" });
    expect(input).toBeVisible();
    fireEvent.change(input, { target: { value: "₹25,000" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(onAction).toHaveBeenCalledWith(amountOnly.id, "resolve_clarification", {
      clarificationId,
      optionId: "custom",
      customText: "₹25,000",
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

describe("compound taxonomy approval", () => {
  it("submits the reviewed category and all child names as one action", () => {
    const onAction = vi.fn();
    const widget: Widget = {
      id: "taxonomy-pet-care",
      type: "taxonomy_editor",
      version: 1,
      data: {
        operation: "create_taxonomy_path",
        name: "Pet Care",
        subcategories: ["Vet"],
        appliesToDraft: false,
        lifecycle: "pending",
      },
      actions: [
        {
          id: "confirm-taxonomy",
          label: "Add category and subcategories",
          action: "create_taxonomy_path",
          style: "primary",
          payload: { name: "Pet Care", subcategories: ["Vet"] },
        },
        {
          id: "cancel-taxonomy",
          label: "Cancel",
          action: "cancel_taxonomy_change",
          style: "secondary",
          payload: {},
        },
      ],
    };

    render(<WidgetRenderer widget={widget} onAction={onAction} />);
    expect(screen.getByRole("textbox", { name: "New category name" })).toHaveValue("Pet Care");
    fireEvent.change(screen.getByRole("textbox", { name: "New subcategory names" }), {
      target: { value: "Vet, Food" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add category and subcategories" }));

    expect(onAction).toHaveBeenCalledWith(
      widget.id,
      "create_taxonomy_path",
      { name: "Pet Care", subcategories: ["Vet", "Food"] },
    );
  });

  it("records the private taxonomy path as one completed receipt", () => {
    const widget: Widget = {
      id: "taxonomy-pet-care-completed",
      type: "taxonomy_editor",
      version: 1,
      data: {
        operation: "create_taxonomy_path",
        name: "Pet Care",
        subcategories: ["Vet"],
        appliesToDraft: false,
        lifecycle: "completed",
        completion: {
          action: "create_taxonomy_path",
          values: { name: "Pet Care", subcategories: ["Vet"] },
        },
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    expect(screen.getByRole("status")).toHaveTextContent(/Added.*Pet Care → Vet/);
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

describe("persisted agent activity", () => {
  it("renders one decision line and collapses a failed run by default", () => {
    // The shape the server stores (and migration 0027 upgraded old threads
    // to): a run that ended mid-stage is at rest with the stage failed and
    // the failure line already written into `summary`.
    const widget: Widget = {
      id: "stale-run",
      type: "agent_activity",
      version: 1,
      data: {
        title: "Governed agent run",
        engine: "Governed agent pipeline",
        model: "gpt-5.6-luna",
        summary: "This stage ended before producing a valid terminal result.",
        totalMs: 20_000,
        steps: [
          { id: "request", label: "Request received", status: "completed", durationMs: 0, cumulativeMs: 2 },
          { id: "operator_repair", label: "Repairing the typed contract", tool: "operator", status: "failed", detail: "This stage ended before producing a valid terminal result.", durationMs: 20_000, cumulativeMs: 20_000 },
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

  it("shows the server-authored failure reason instead of a generic problem banner", () => {
    // A degraded-but-delivered run: the backend prefers the failed stage over
    // the reasoning summary when it writes `summary`, so the card renders it
    // verbatim rather than re-deriving it from the steps.
    const widget: Widget = {
      id: "rejected-analysis",
      type: "agent_activity",
      version: 1,
      data: {
        summary: "query_presence: 0 governed semantic queries supplied.",
        reasoningTrace: "The Validator approved the presentation-only plan.",
        totalMs: 20_000,
        steps: [
          {
            id: "classification",
            label: "Analysis plan approved",
            status: "completed",
            detail: "Preserve the existing table presentation.",
            durationMs: 10_000,
            cumulativeMs: 10_000,
          },
          {
            id: "tool_validation",
            label: "Generated tool was rejected",
            status: "failed",
            detail: "query_presence: 0 governed semantic queries supplied.",
            durationMs: 100,
            cumulativeMs: 20_000,
          },
        ],
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    const traceButton = screen.getByRole("button", {
      name: /Agent run failed: query_presence: 0 governed semantic queries supplied\./,
    });
    expect(within(traceButton).getByText("query_presence: 0 governed semantic queries supplied.")).toBeInTheDocument();
    expect(screen.queryByText("This run hit a problem")).not.toBeInTheDocument();

    fireEvent.click(traceButton);
    const details = screen.getByTestId("agent-activity-details");
    expect(details).toHaveTextContent("query_presence: 0 governed semantic queries supplied.");
    expect(details).toHaveTextContent("Generated tool was rejected");
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
        modelPassCount: 1,
        metrics: {
          source: "agno_run_output",
          modelPasses: 1,
          inputTokens: 160,
          outputTokens: 30,
          totalTokens: 190,
          cacheReadTokens: 4,
          cacheWriteTokens: 0,
          reasoningTokens: 2,
          modelDurationMs: 2000,
          firstModelTimeToFirstTokenMs: 300,
          costUsd: null,
          costCoverage: 0,
          passes: [{
            stage: "operator_decision",
            model: "gpt-test",
            provider: "OpenAI",
            inputTokens: 160,
            outputTokens: 30,
            totalTokens: 190,
            cacheReadTokens: 4,
            cacheWriteTokens: 0,
            reasoningTokens: 2,
            durationMs: 2000,
            timeToFirstTokenMs: 300,
            costUsd: null,
          }],
        },
        totalMs: 7360,
        steps: [
          {
            id: "request",
            label: "Request received",
            status: "completed",
            input: { text: "What about the other ones?", conversationId: "conversation-1" },
            output: { accepted: true, replyReserved: true },
            durationMs: 0,
            cumulativeMs: 3,
          },
          { id: "operator", label: "Operator completed the answer", tool: "operator", status: "completed", durationMs: 7100, cumulativeMs: 7360 },
        ],
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    expect(screen.getByText("Compared the current question with the previous Housing result.")).toBeInTheDocument();
    const reasoning = screen.getByRole("button");
    expect(reasoning).toHaveAttribute("data-inline-disclosure", "true");
    expect(within(reasoning).getByText("7.36 s")).toBeInTheDocument();
    expect(within(reasoning).getByText("Single model pass")).toBeInTheDocument();
    const details = screen.getByTestId("agent-activity-details");
    const transition = details.parentElement;
    expect(details).toHaveAttribute("aria-hidden", "true");
    expect(transition).toHaveClass("grid-rows-[0fr]", "opacity-0", "duration-[var(--m-enter)]");

    fireEvent.click(reasoning);

    expect(reasoning).toHaveAttribute("aria-expanded", "true");
    expect(details).toHaveAttribute("aria-hidden", "false");
    expect(screen.getByTestId("agent-run-metrics")).toHaveTextContent("190 tokens (160 in / 30 out)");
    expect(screen.getByTestId("agent-run-metrics")).toHaveTextContent("2.00 s model time");
    expect(screen.getByTestId("agent-run-metrics")).toHaveTextContent("provider cost unavailable (0% coverage)");
    expect(transition).toHaveClass("grid-rows-[1fr]", "opacity-100");
    expect(details).toHaveTextContent("Kept the July scope.");
    expect(details).toHaveTextContent("Removed the merchant filter.");
    expect(within(details).getByText("Execution trace")).toBeInTheDocument();
    expect(within(details).getByText("Request received")).toBeInTheDocument();
    expect(within(details).getByText("Operator completed the answer")).toBeInTheDocument();
    expect(details).toHaveTextContent("Stage request");
    expect(details).toHaveTextContent("Tool operator");
    const inputs = within(details).getAllByRole("button", { name: "Input" });
    const outputs = within(details).getAllByRole("button", { name: "Output" });
    expect(inputs).toHaveLength(1);
    expect(outputs).toHaveLength(1);
    expect(inputs[0]).toHaveAttribute("data-inline-disclosure", "true");
    expect(outputs[0]).toHaveAttribute("data-inline-disclosure", "true");
    fireEvent.click(inputs[0]);
    expect(inputs[0]).toHaveAttribute("aria-expanded", "true");
    const inputTranscript = within(details).getAllByTitle("Click to collapse; drag to select text")[0];
    const selection = vi.spyOn(window, "getSelection").mockReturnValue({ isCollapsed: false } as Selection);
    fireEvent.click(inputTranscript);
    expect(inputs[0]).toHaveAttribute("aria-expanded", "true");
    selection.mockRestore();
    fireEvent.click(inputTranscript);
    expect(inputs[0]).toHaveAttribute("aria-expanded", "false");
    expect(details).toHaveTextContent('"text": "What about the other ones?"');
    expect(details).toHaveTextContent('"replyReserved": true');
    expect(details).toHaveTextContent("<1 ms step · 3 ms total elapsed");
    expect(details).toHaveTextContent("7.10 s step · 7.36 s total elapsed");

    fireEvent.click(reasoning);
    expect(details).toHaveAttribute("aria-hidden", "true");
    expect(transition).toHaveClass("grid-rows-[0fr]", "opacity-0");
  });

  it("identifies and expands a governed multi-pass trace", () => {
    const widget: Widget = {
      id: "multi-pass-run",
      type: "agent_activity",
      version: 1,
      data: {
        summary: "Rechecked the first contract after validation rejected it.",
        reasoningTrace: "The initial contract was incomplete, so I revised it and validated the correction.",
        debugTrace: true,
        modelPassCount: 4,
        totalMs: 14_000,
        steps: [
          { id: "operator", label: "Operator produced a typed contract", tool: "operator", status: "completed", durationMs: 5000, cumulativeMs: 5000 },
          { id: "validator", label: "Validator rejected the contract", tool: "validator", status: "completed", durationMs: 2500, cumulativeMs: 7500 },
          { id: "repair-model", stageId: "model_pass_operator_repair", label: "Operator repair model pass", tool: "gpt-5.6-terra", status: "completed", durationMs: 4000, cumulativeMs: 11_500 },
          { id: "operator_repair", label: "Operator repaired the contract", tool: "operator", status: "completed", durationMs: 4000, cumulativeMs: 11_500 },
          { id: "revalidation", label: "Validated the revised route", tool: "validator", status: "completed", durationMs: 2500, cumulativeMs: 14_000 },
        ],
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    const reasoning = screen.getByRole("button");
    expect(within(reasoning).getByText("4 model passes")).toBeInTheDocument();
    fireEvent.click(reasoning);

    const details = screen.getByTestId("agent-activity-details");
    expect(details).toHaveTextContent("Execution trace");
    expect(details).toHaveTextContent("4 model passes");
    expect(details).toHaveTextContent("Operator produced a typed contract");
    expect(details).toHaveTextContent("Stage operator · Tool operator");
    expect(details).toHaveTextContent("5.00 s step · 5.00 s total elapsed");
    expect(details).toHaveTextContent("Validated the revised route");
    expect(details).toHaveTextContent("Stage revalidation · Tool validator");
    expect(details).toHaveTextContent("2.50 s step · 14.0 s total elapsed");
  });

  it("renders the server-authored model pass count instead of counting stages", () => {
    const widget: Widget = {
      id: "provider-pass-run",
      type: "agent_activity",
      version: 1,
      data: {
        summary: "Recorded the clarified transaction.",
        debugTrace: true,
        modelPassCount: 3,
        totalMs: 29_000,
        steps: [
          { id: "operator", stageId: "operator", label: "Operator handoff", tool: "operator", status: "completed", durationMs: 10_000, cumulativeMs: 10_000 },
          { id: "planner-model", stageId: "model_pass_planner", label: "Planner model pass", tool: "gpt-5.6-terra", status: "completed", durationMs: 3_000, cumulativeMs: 13_000 },
          { id: "planner", stageId: "planner", label: "Planner wrapper", tool: "planner", status: "completed", durationMs: 3_000, cumulativeMs: 13_000 },
          { id: "validator", stageId: "validator", label: "Validator", tool: "validator", status: "completed", durationMs: 3_000, cumulativeMs: 19_000 },
          { id: "execution", stageId: "execution", label: "Governed execution", tool: "analysis_harness", status: "completed", durationMs: 2_000, cumulativeMs: 21_000 },
        ],
        live: false,
      },
      actions: [],
    };

    render(<WidgetRenderer widget={widget} onAction={() => undefined} />);

    expect(screen.getByRole("button", { name: /3 model passes/ })).toBeInTheDocument();
  });
});

describe("related questions", () => {
  it("posts a tapped suggestion as a new prompt and stays tappable outside the active-widget gate", () => {
    const onAction = vi.fn();
    const onPostPrompt = vi.fn();
    const widget = {
      id: "related-questions-1",
      type: "related_questions",
      data: { questions: ["What did I spend on food in August 2026?", "Compare July and August 2026 expenses"] },
      actions: [],
    } as unknown as Widget;

    render(<WidgetRenderer widget={widget} disabled onAction={onAction} onPostPrompt={onPostPrompt} />);

    const chip = screen.getByRole("button", { name: "What did I spend on food in August 2026?" });
    fireEvent.click(chip);

    expect(onPostPrompt).toHaveBeenCalledWith("What did I spend on food in August 2026?");
    expect(onAction).not.toHaveBeenCalled();
    // jsdom cannot enforce the CSS pointer-events retirement that
    // `.widget-readonly` applies in a real browser, so pin the opt-out
    // attribute that keeps chips tappable after the turn completes.
    expect(chip).toHaveAttribute("data-readonly-keep", "true");
  });

  it("renders nothing without a post callback", () => {
    const widget = {
      id: "related-questions-2",
      type: "related_questions",
      data: { questions: ["Anything"] },
      actions: [],
    } as unknown as Widget;

    const { container } = render(<WidgetRenderer widget={widget} onAction={vi.fn()} />);

    expect(container.querySelector("button")).toBeNull();
  });
});

describe("filesystem operation widgets", () => {
  const checksum = "a".repeat(64);

  it("renders a schema-driven form and submits typed inputs through one generic action", () => {
    const onAction = vi.fn();
    const widget: Widget = {
      id: "operation-form-1",
      type: "operation_form",
      version: 1,
      data: {
        title: "Create category path",
        body: "Name the category and its subcategories.",
        operationId: "ops.taxonomy.create_path",
        operationVersion: 1,
        operationChecksum: checksum,
        inputs: {},
        inputSchema: {
          type: "object",
          additionalProperties: false,
          required: ["category", "subcategories"],
          properties: {
            category: { type: "string", title: "Category" },
            subcategories: { type: "array", title: "Subcategories", maxItems: 10, items: { type: "string" } },
          },
        },
        missingFields: ["category", "subcategories"],
        submitLabel: "Review",
      },
      actions: [
        { id: "submit", label: "Review", action: "submit_operation", style: "primary", payload: { operationId: "ops.taxonomy.create_path", operationVersion: 1, operationChecksum: checksum, inputs: {} } },
        { id: "cancel", label: "Cancel", action: "cancel_operation", style: "ghost", payload: { operationId: "ops.taxonomy.create_path", operationVersion: 1, operationChecksum: checksum, inputs: {} } },
      ],
    };
    render(<WidgetRenderer widget={widget} onAction={onAction} />);

    fireEvent.change(screen.getByRole("textbox", { name: "Category" }), { target: { value: "Pet Care" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Subcategories" }), { target: { value: "Vet, Food" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));

    expect(onAction).toHaveBeenCalledWith("operation-form-1", "submit_operation", {
      operationId: "ops.taxonomy.create_path",
      operationVersion: 1,
      operationChecksum: checksum,
      inputs: { category: "Pet Care", subcategories: ["Vet", "Food"] },
    });
  });

  it("renders the server-bound approval values without operation-specific UI", () => {
    const onAction = vi.fn();
    const payload = { operationId: "ops.taxonomy.create_path", operationVersion: 1, operationChecksum: checksum, inputs: { category: "Pet Care", subcategories: ["Vet"] } };
    const widget: Widget = {
      id: "operation-approval-1",
      type: "operation_approval",
      version: 1,
      data: { title: "Create category?", body: "Review this change.", ...payload, effect: "mutation", summary: "Create Pet Care and Vet." },
      actions: [
        { id: "approve", label: "Approve", action: "approve_operation", style: "primary", payload },
        { id: "cancel", label: "Cancel", action: "cancel_operation", style: "ghost", payload },
      ],
    };
    render(<WidgetRenderer widget={widget} onAction={onAction} />);

    expect(screen.getByText("Pet Care")).toBeInTheDocument();
    expect(screen.getByText("Vet")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onAction).toHaveBeenCalledWith("operation-approval-1", "approve_operation", payload);
  });
});
