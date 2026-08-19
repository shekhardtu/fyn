import { widgetTypeIds, type AgentResponse, type Message, type Widget } from "@/lib/protocol";

export function transcriptRevision(messages: Message[]) {
  return JSON.stringify(messages);
}

export function shouldAdoptServerTranscript({
  messages,
  seededRevision,
  serverRevision,
  activeRunId,
  pendingWidget,
  uploading,
}: {
  messages: Message[];
  seededRevision: string;
  serverRevision: string;
  activeRunId: string | null;
  pendingWidget: string | null;
  uploading: boolean;
}) {
  if (seededRevision === serverRevision || activeRunId !== null || pendingWidget !== null || uploading) return false;
  return !messages.some((message) => /^(optimistic|upload)-/.test(message.id));
}

export function applyWidgetUpdates(messages: Message[], updates: AgentResponse["widgetUpdates"]): Message[] {
  if (!updates.length) return messages;
  const replacements = new Map(updates.map((update) => [update.widgetId, update.widget]));
  return messages.map((message) => {
    if (!message.widgets.some((widget) => replacements.has(widget.id))) return message;
    return { ...message, widgets: message.widgets.map((widget) => replacements.get(widget.id) ?? widget) };
  });
}

export function completedWidgetIds(messages: Message[]) {
  const widgetsByDraft = new Map<string, string[]>();
  const completed = new Set<string>();
  for (const message of messages) {
    for (const widget of message.widgets) {
      // Latest occurrence wins for legacy threads that reused a resource id as
      // the widget id. New cards use unique event ids, but this lets an already
      // persisted pending editor recover after a reload.
      if (widget.data.lifecycle === "completed" || widget.data.lifecycle === "cancelled") completed.add(widget.id);
      else completed.delete(widget.id);
      const resourceId = typeof widget.data.draftId === "string" ? widget.data.draftId : typeof widget.data.transactionId === "string" ? widget.data.transactionId : null;
      if (!resourceId) continue;
      const prior = widgetsByDraft.get(resourceId) ?? [];
      prior.forEach((id) => completed.add(id));
      if (widget.type === widgetTypeIds.transaction_preview && widget.actions.length === 0) completed.add(widget.id);
      widgetsByDraft.set(resourceId, [...prior, widget.id]);
    }
  }
  return completed;
}

export function reconcileUsedWidgetIds(
  current: Set<string>,
  completedWidgetId: string | null,
  emittedWidgets: Widget[],
) {
  const next = new Set(current);
  if (completedWidgetId) next.add(completedWidgetId);
  // Event ids are server-unique, but recover in place if a legacy or future
  // producer emits a new pending interaction with an older id. The backend
  // resolves the newest occurrence, so the client must make that occurrence
  // interactive in the same live response rather than requiring a reload.
  for (const widget of emittedWidgets) {
    const lifecycle = widget.data.lifecycle;
    if (
      lifecycle !== "completed"
      && lifecycle !== "cancelled"
      && isActionableWidget(widget)
    ) next.delete(widget.id);
  }
  return next;
}

/** IDs the workspace mints for messages it renders before the server has
 *  stored them. They read as "pending" until the reply confirms the stored ID. */
const LOCAL_MESSAGE_ID = /^(optimistic|upload)-/;

/**
 * The reply names the persisted row the just-sent question became. Folding that
 * identity onto the local bubble retires its provisional ID in place, so the
 * transcript matches what a reload would show without waiting for one.
 */
export function adoptUserMessageIdentity(messages: Message[], persistedId: string | null | undefined): Message[] {
  if (!persistedId || messages.some((message) => message.id === persistedId)) return messages;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role !== "user") continue;
    // The newest user turn is the one this reply answers. If it already carries
    // a server identity there is nothing provisional left to retire.
    if (!LOCAL_MESSAGE_ID.test(message.id)) return messages;
    return messages.map((item, itemIndex) => (itemIndex === index ? { ...item, id: persistedId } : item));
  }
  return messages;
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
  return false;
}

/**
 * Interaction belongs only to the final display widget in the final assistant
 * turn. A newer user message immediately retires the previous HITL surface.
 */
export function activeWidgetId(messages: Message[]) {
  const latest = messages.at(-1);
  if (!latest || latest.role !== "assistant") return null;
  // Traces and follow-up suggestions decorate the answer; neither supersedes
  // the HITL decision directly above it. Treating a trailing related-questions
  // band as the "final widget" deadlocked the thread: the composer correctly
  // paused for the interrupt while the actual action card was made read-only.
  const displayWidgets = latest.widgets.filter((widget) => (
    widget.type !== widgetTypeIds.agent_activity
    && widget.type !== widgetTypeIds.related_questions
  ));
  const finalWidget = displayWidgets.at(-1);
  return finalWidget && isActionableWidget(finalWidget) ? finalWidget.id : null;
}
