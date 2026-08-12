import { widgetTypeIds, type AgentResponse, type Message, type Widget } from "@/lib/protocol";

export function applyWidgetUpdates(messages: Message[], updates: AgentResponse["widgetUpdates"]): Message[] {
  if (!updates.length) return messages;
  const replacements = new Map(updates.map((update) => [update.widgetId, update.widget]));
  return messages.map((message) => {
    if (!message.widgets.some((widget) => replacements.has(widget.id))) return message;
    return { ...message, widgets: message.widgets.map((widget) => replacements.get(widget.id) ?? widget) };
  });
}

const embeddedControlWidgets = new Set<Widget["type"]>([
  widgetTypeIds.transaction_edit,
  widgetTypeIds.avoidable_expenses,
  widgetTypeIds.loan_calculator,
  widgetTypeIds.investment_projection,
]);

export function isActionableWidget(widget: Widget) {
  if (widget.type === widgetTypeIds.agent_activity) return false;
  if (widget.actions.length > 0 || embeddedControlWidgets.has(widget.type)) return true;
  if (Array.isArray(widget.data.rowActions) && widget.data.rowActions.length > 0) return true;
  if (widget.type === widgetTypeIds.transaction_list && Array.isArray(widget.data.transactions)) {
    return widget.data.transactions.some((row) => {
      if (!row || typeof row !== "object") return false;
      const actions = (row as Record<string, unknown>).actions;
      return Array.isArray(actions) && actions.length > 0;
    });
  }
  return false;
}

/** Historical responses stored tool lifecycle as a large insight card. New
 * responses keep it in the run trace; hide old persisted cards on hydration. */
export function isLegacyAnalysisLifecycleWidget(widget: Widget) {
  return widget.type === widgetTypeIds.insight_card && widget.data.eyebrow === "Validated analysis capability";
}

/**
 * Interaction belongs only to the final display widget in the final assistant
 * turn. A newer user message immediately retires the previous HITL surface.
 */
export function activeWidgetId(messages: Message[]) {
  const latest = messages.at(-1);
  if (!latest || latest.role !== "assistant") return null;
  const displayWidgets = latest.widgets.filter((widget) => widget.type !== widgetTypeIds.agent_activity && !isLegacyAnalysisLifecycleWidget(widget));
  const finalWidget = displayWidgets.at(-1);
  return finalWidget && isActionableWidget(finalWidget) ? finalWidget.id : null;
}
