import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WidgetRenderer } from "@/components/widget-renderer";
import type { FynInterrupt } from "@/lib/api";
import { interruptActionResolution, recoverInterruptWidget } from "@/lib/interrupt-widget";
import type { Widget } from "@/lib/protocol";

const clarificationId = "7ab22bba-3f79-47cc-a545-f1016002a910";

function interrupt(metadata: Record<string, unknown>): FynInterrupt {
  return {
    id: "19b77308-97db-4a8b-83c8-64c6c4d20b78",
    runId: "12eaa6a6-91da-481c-83e0-93cd8d1e8a91",
    widgetId: `clarification-${clarificationId}`,
    reason: "clarification",
    message: "What monthly amount should I use for the Construction budget?",
    metadata,
  };
}

describe("interrupt widget recovery", () => {
  it("uses the validated server-authored widget snapshot as the only UI contract", () => {
    const widget: Widget = {
      id: `clarification-${clarificationId}`,
      type: "clarification",
      version: 1,
      data: {
        clarificationId,
        title: "One detail needs your confirmation",
        question: "What monthly amount should I use for the Construction budget?",
        reason: "An amount is required.",
        conflictFields: ["amount_minor"],
        options: [],
        allowCustom: true,
        customLabel: "Enter monthly amount",
      },
      actions: [
        { id: "custom", label: "Enter monthly amount", action: "resolve_clarification", style: "secondary", payload: { clarificationId, optionId: "custom" } },
        { id: "cancel", label: "Cancel", action: "resolve_clarification", style: "ghost", payload: { clarificationId, optionId: "cancel" } },
      ],
    };

    const recovered = recoverInterruptWidget(interrupt({ widget }));
    expect(recovered).not.toBeNull();

    const onAction = vi.fn();
    render(<WidgetRenderer widget={recovered!} onAction={onAction} />);

    expect(screen.queryByText("Choose an option")).not.toBeInTheDocument();
    const input = screen.getByRole("textbox", { name: "Custom clarification" });
    expect(input).toBeVisible();
    fireEvent.change(input, { target: { value: "₹25,000" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(onAction).toHaveBeenCalledWith(
      `clarification-${clarificationId}`,
      "resolve_clarification",
      { clarificationId, optionId: "custom", customText: "₹25,000" },
    );
  });

  it("fails closed when the interrupt has no matching widget snapshot", () => {
    expect(recoverInterruptWidget(interrupt({
      widgetType: "clarification",
      continuation: { clarificationId, allowCustom: true, customStrategy: "budget_amount" },
    }))).toBeNull();
    expect(recoverInterruptWidget(interrupt({
      widget: {
        id: "a-different-widget",
        type: "clarification",
        version: 1,
        data: {},
        actions: [],
      },
    }))).toBeNull();
  });

  it("wraps a recovered action in the governed interrupt response", () => {
    expect(interruptActionResolution(
      `clarification-${clarificationId}`,
      "resolve_clarification",
      { clarificationId, optionId: "custom", customText: "25000" },
    )).toEqual({
      status: "resolved",
      payload: {
        approved: true,
        editedArgs: {
          widgetId: `clarification-${clarificationId}`,
          action: "resolve_clarification",
          payload: { clarificationId, optionId: "custom", customText: "25000" },
          completeWidget: true,
        },
      },
    });
  });
});
