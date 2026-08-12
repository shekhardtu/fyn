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

  it("accepts generated analysis results only through registered widgets", () => {
    const widget = widgetSchema.parse({
      id: "analysis-1",
      type: "analysis_table",
      version: 1,
      data: { title: "Custom analysis", queryResults: [] },
      actions: [],
    });
    expect(widget.type).toBe("analysis_table");
  });

  it("validates a capability-gated dynamic data table", () => {
    const widget = widgetSchema.parse({
      id: "transactions-1",
      type: "data_table",
      version: 1,
      data: {
        title: "Transactions",
        columns: [
          { key: "merchant", label: "Transaction", type: "entity", priority: "primary" },
          { key: "amountMinor", label: "Amount", type: "money", align: "right", priority: "primary", currencyKey: "currency" },
        ],
        rows: [{ id: "txn-1", merchant: "Toit", amountMinor: 77700, currency: "INR", _capabilities: ["transaction.edit"] }],
        rowActions: [{ id: "edit", label: "Edit", action: "edit_saved_transaction", resourceKey: "id", payloadKey: "transactionId", capability: "transaction.edit", icon: "edit" }],
      },
      actions: [],
    });
    expect(widget.type).toBe("data_table");
  });

  it("rejects malformed dynamic table payloads", () => {
    expect(() => widgetSchema.parse({ id: "bad", type: "data_table", version: 1, data: { title: "Missing columns", rows: [] }, actions: [] })).toThrow();
  });

  it("validates the agent output contract", () => {
    expect(agentResponseSchema.parse({
      message: "Ready",
      widgets: [],
      citations: [],
      conversation_id: crypto.randomUUID(),
      message_id: crypto.randomUUID(),
    }).message).toBe("Ready");
  });
});
