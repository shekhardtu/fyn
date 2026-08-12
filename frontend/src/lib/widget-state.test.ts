import { describe, expect, it } from "vitest";
import type { Message, Widget } from "@/lib/protocol";
import { activeWidgetId, applyWidgetUpdates, isLegacyAnalysisLifecycleWidget } from "@/lib/widget-state";

function widget(id: string, actionable = true): Widget {
  return {
    id,
    type: actionable ? "confirmation_card" : "insight_card",
    version: 1,
    data: { title: id },
    actions: actionable ? [{ id: "save", label: "Save", action: "save_budget", style: "primary", payload: {} }] : [],
  };
}

function message(id: string, role: Message["role"], widgets: Widget[]): Message {
  return { id, role, content: "", widgets, citations: [], created_at: new Date().toISOString() };
}

describe("activeWidgetId", () => {
  it("keeps only the final actionable widget in the newest assistant turn active", () => {
    expect(activeWidgetId([
      message("a1", "assistant", [widget("first")]),
      message("a2", "assistant", [widget("second"), widget("third")]),
    ])).toBe("third");
  });

  it("retires prior widgets as soon as a new user message appears", () => {
    expect(activeWidgetId([
      message("a1", "assistant", [widget("first")]),
      message("u1", "user", []),
    ])).toBeNull();
  });

  it("does not keep an older action active behind a newer display widget", () => {
    expect(activeWidgetId([message("a1", "assistant", [widget("action"), widget("result", false)])])).toBeNull();
  });

  it("ignores the run trace appended to an actionable result", () => {
    const trace: Widget = { id: "trace", type: "agent_activity", version: 1, data: {}, actions: [] };
    expect(activeWidgetId([message("a1", "assistant", [widget("action"), trace])])).toBe("action");
  });

  it("recognizes historical analysis lifecycle cards for compatibility hiding", () => {
    const lifecycle: Widget = {
      id: "generated-tool-old",
      type: "insight_card",
      version: 1,
      data: { eyebrow: "Validated analysis capability", title: "old_tool" },
      actions: [],
    };
    expect(isLegacyAnalysisLifecycleWidget(lifecycle)).toBe(true);
    expect(activeWidgetId([message("a1", "assistant", [widget("action"), lifecycle])])).toBe("action");
  });
});

describe("applyWidgetUpdates", () => {
  it("replaces the originating persisted widget without changing its message position", () => {
    const original = widget("taxonomy");
    original.type = "taxonomy_editor";
    original.data = { operation: "create_subcategory", name: null, lifecycle: "pending" };
    const resolved: Widget = { ...original, data: { ...original.data, name: "Flights", lifecycle: "completed" }, actions: [] };
    const messages = [message("assistant-1", "assistant", [original])];

    const updated = applyWidgetUpdates(messages, [{ widgetId: original.id, widget: resolved }]);

    expect(updated).toHaveLength(1);
    expect(updated[0].id).toBe("assistant-1");
    expect(updated[0].widgets[0].data).toMatchObject({ name: "Flights", lifecycle: "completed" });
    expect(updated[0].widgets[0].actions).toEqual([]);
  });
});
