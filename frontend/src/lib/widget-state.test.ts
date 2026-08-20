import { describe, expect, it } from "vitest";
import type { Message, Widget } from "@/lib/protocol";
import { activeWidgetId, adoptUserMessageIdentity, applyWidgetUpdates, completedWidgetIds, mergeAgentResponse, reconcileUsedWidgetIds, shouldAdoptServerTranscript, transcriptRevision } from "@/lib/widget-state";

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
  const deliveredAt = new Date().toISOString();
  return { id, role, content: "", widgets, citations: [], created_at: deliveredAt, delivered_at: deliveredAt };
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

  it("keeps a HITL card active when related questions are appended after it", () => {
    const related: Widget = {
      id: "related",
      type: "related_questions",
      version: 1,
      data: { questions: ["Review this budget"] },
      actions: [],
    };
    expect(activeWidgetId([message("a1", "assistant", [widget("budget"), related])])).toBe("budget");
  });
});

describe("completedWidgetIds", () => {
  it("lets a newer pending legacy widget recover when an older card reused its id", () => {
    const saved = widget("budget-resource");
    saved.data.lifecycle = "completed";
    const editor = widget("budget-resource");
    editor.data.lifecycle = "pending";

    expect(completedWidgetIds([
      message("saved", "assistant", [saved]),
      message("editor", "assistant", [editor]),
    ])).not.toContain("budget-resource");
  });

  it("unlocks a newly emitted pending interaction even if it repeats a used legacy id", () => {
    const next = widget("budget-resource");

    expect(reconcileUsedWidgetIds(
      new Set(["older-widget"]),
      "budget-resource",
      [next],
    )).toEqual(new Set(["older-widget"]));
  });

  it("keeps a completed response locked", () => {
    const receipt = widget("budget-resource");
    receipt.data.lifecycle = "completed";

    expect(reconcileUsedWidgetIds(
      new Set(),
      "budget-resource",
      [receipt],
    )).toEqual(new Set(["budget-resource"]));
  });
});

describe("adoptUserMessageIdentity", () => {
  const persistedId = "3f7256bb-4c1d-4a08-9f7e-2f65a1c0d9ab";

  it("retires the optimistic bubble id with the persisted one", () => {
    const updated = adoptUserMessageIdentity([
      message("assistant-1", "assistant", []),
      message("optimistic-1755350000000", "user", []),
    ], persistedId);
    expect(updated.map((item) => item.id)).toEqual(["assistant-1", persistedId]);
  });

  it("retires an upload bubble the same way", () => {
    const updated = adoptUserMessageIdentity([message("upload-1755350000000", "user", [])], persistedId);
    expect(updated[0].id).toBe(persistedId);
  });

  it("leaves the transcript alone when the newest user turn is already persisted", () => {
    const messages = [
      message("optimistic-1755340000000", "user", []),
      message(persistedId, "user", []),
    ];
    expect(adoptUserMessageIdentity(messages, "6de0a1b2-8f3c-4d5e-9a7b-1c2d3e4f5a6b")).toBe(messages);
  });

  it("leaves the transcript alone when the persisted id is absent or already present", () => {
    const messages = [message(persistedId, "user", [])];
    expect(adoptUserMessageIdentity(messages, null)).toBe(messages);
    expect(adoptUserMessageIdentity(messages, persistedId)).toBe(messages);
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

describe("mergeAgentResponse", () => {
  it("retires the submitted HITL card before appending its terminal acknowledgement", () => {
    const editor = widget("budget-editor");
    editor.type = "budget_progress";
    editor.data = { title: "Food budget", amountMinor: 2_000_000, lifecycle: "pending" };
    const terminal = widget("budget-saved");
    terminal.type = "budget_progress";
    terminal.data = { title: "Food budget", amountMinor: 2_500_000 };
    terminal.actions = [
      { id: "edit", label: "Update budget", action: "edit_budget", style: "secondary", payload: { budgetId: "budget-1" } },
      { id: "delete", label: "Delete budget", action: "request_delete_budget", style: "danger", payload: { budgetId: "budget-1" } },
    ];
    const resolved = { ...editor, data: { ...editor.data, lifecycle: "completed" }, actions: [] };
    const deliveredAt = new Date().toISOString();

    const merged = mergeAgentResponse(
      [message("editor-message", "assistant", [editor])],
      {
        message: "Set your food budget to ₹25,000 per month.",
        widgets: [terminal],
        widgetUpdates: [{ widgetId: editor.id, widget: resolved }],
        pendingAction: null,
        citations: [],
        conversation_id: "conversation-1",
        message_id: "terminal-message",
        user_message_id: null,
        delivered_at: deliveredAt,
      },
    );

    expect(merged.map((item) => item.id)).toEqual(["editor-message", "terminal-message"]);
    expect(merged[0].widgets[0]).toMatchObject({ data: { lifecycle: "completed" }, actions: [] });
    expect(activeWidgetId(merged)).toBe("budget-saved");
  });
});

describe("server transcript adoption", () => {
  it("adopts a persisted related-question widget added to a cached message", () => {
    const cached = [message("assistant-1", "assistant", [widget("trace", false)])];
    const related: Widget = {
      id: "related-assistant-1",
      type: "related_questions",
      version: 1,
      data: { questions: ["Which August 2026 categories cost the most?"] },
      actions: [],
    };
    const refreshed = [{ ...cached[0], widgets: [...cached[0].widgets, related] }];

    expect(shouldAdoptServerTranscript({
      messages: cached,
      seededRevision: transcriptRevision(cached),
      serverRevision: transcriptRevision(refreshed),
      activeRunId: null,
      pendingWidget: null,
      uploading: false,
    })).toBe(true);
  });

  it("does not overwrite optimistic or in-flight transcript state", () => {
    const cached = [message("assistant-1", "assistant", [])];
    const refreshedRevision = transcriptRevision([...cached, message("assistant-2", "assistant", [])]);
    const base = {
      seededRevision: transcriptRevision(cached),
      serverRevision: refreshedRevision,
      activeRunId: null,
      pendingWidget: null,
      uploading: false,
    };

    expect(shouldAdoptServerTranscript({ ...base, messages: [message("optimistic-1", "user", [])] })).toBe(false);
    expect(shouldAdoptServerTranscript({ ...base, messages: cached, activeRunId: "run-1" })).toBe(false);
    expect(shouldAdoptServerTranscript({ ...base, messages: cached, pendingWidget: "widget-1" })).toBe(false);
    expect(shouldAdoptServerTranscript({ ...base, messages: cached, uploading: true })).toBe(false);
  });
});
