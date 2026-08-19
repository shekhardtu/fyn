import { describe, expect, it } from "vitest";
import { agentResponseSchema, widgetSchema } from "@/lib/protocol";

describe("widget protocol", () => {
  it("accepts a typed category selector", () => {
    const widget = widgetSchema.parse({
      id: "category-1",
      type: "category_selector",
      version: 1,
      data: { title: "Choose", options: [{ id: "food", label: "Food" }] },
      actions: [{ id: "select", label: "Select", action: "select_category", payload: {} }],
    });
    expect(widget.type).toBe("category_selector");
  });

  it("rejects arbitrary component types", () => {
    expect(() => widgetSchema.parse({ id: "unsafe", type: "custom_html", version: 1, data: { html: "<script />" } })).toThrow();
  });

  it("rejects retired display widget types", () => {
    expect(() => widgetSchema.parse({
      id: "analysis-1",
      type: "analysis_table",
      version: 1,
      data: { title: "Custom analysis", queryResults: [] },
      actions: [],
    })).toThrow();
  });

  it("no longer accepts a typed record table", () => {
    // Records are narrated as Markdown the agent writes; the only widgets left
    // on the protocol are the interactive HITL surfaces.
    expect(() => widgetSchema.parse({
      id: "transactions-1",
      type: "data_table",
      version: 1,
      data: { title: "Transactions", columns: [], rows: [] },
      actions: [],
    })).toThrow();
  });

  it("validates the agent output contract", () => {
    expect(agentResponseSchema.parse({
      message: "Ready",
      widgets: [],
      citations: [],
      conversation_id: crypto.randomUUID(),
      message_id: crypto.randomUUID(),
      delivered_at: new Date().toISOString(),
    }).message).toBe("Ready");
  });
});
