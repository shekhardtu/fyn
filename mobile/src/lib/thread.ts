import { contractLimits } from "@/lib/generated/contracts";
import { widgetTypeIds, type AgentResponse, type Message, type WidgetActionId } from "@/lib/protocol";
import { formatBytes } from "@/lib/format";
import type { PickedFile } from "@/lib/api";

/**
 * The thread's pure logic, kept out of the screen.
 *
 * All of this is lifted from the web app unchanged in behaviour — it is the
 * same protocol, and a native client that decided for itself which HITL control
 * is live would be a second implementation of a rule the server already owns.
 */

export type Retry =
  | { kind: "chat"; text: string }
  | { kind: "action"; widgetId: string; action: WidgetActionId; payload: Record<string, unknown>; markUsed: boolean }
  | { kind: "upload"; file: PickedFile }
  | null;

export const MAX_UPLOAD_BYTES = contractLimits.csvUploadBytes;

/** Rejects what the importer can't read before spending a round trip on it. */
export function csvProblem(file: PickedFile) {
  const size = file.size ?? 0;
  if (!/\.csv$/i.test(file.name) && file.mimeType !== "text/csv") {
    return `${file.name} isn’t a CSV file. Export the statement as CSV and attach it again.`;
  }
  if (size === 0) return `${file.name} is empty. Attach a statement that has rows in it.`;
  if (size > MAX_UPLOAD_BYTES) return `${file.name} is ${formatBytes(size)}. Attach a statement under ${formatBytes(MAX_UPLOAD_BYTES)}.`;
  return null;
}

export function responseToMessage(response: AgentResponse): Message {
  return {
    id: response.message_id,
    role: "assistant",
    content: response.message,
    widgets: response.widgets,
    citations: response.citations,
    created_at: new Date().toISOString(),
  };
}

/**
 * Which controls are already spent.
 *
 * A widget is retired when the server says its lifecycle ended, and also when a
 * later widget speaks for the same draft or transaction: answering a category
 * question supersedes the preview that asked it, and leaving both live would
 * let the same decision be submitted twice.
 */
export function completedWidgetIds(messages: Message[]) {
  const widgetsByDraft = new Map<string, string[]>();
  const completed = new Set<string>();
  for (const message of messages) {
    for (const widget of message.widgets) {
      if (widget.data.lifecycle === "completed" || widget.data.lifecycle === "cancelled") completed.add(widget.id);
      const resourceId = typeof widget.data.draftId === "string"
        ? widget.data.draftId
        : typeof widget.data.transactionId === "string" ? widget.data.transactionId : null;
      if (!resourceId) continue;
      const prior = widgetsByDraft.get(resourceId) ?? [];
      prior.forEach((id) => completed.add(id));
      if (widget.type === widgetTypeIds.transaction_preview && widget.actions.length === 0) completed.add(widget.id);
      widgetsByDraft.set(resourceId, [...prior, widget.id]);
    }
  }
  return completed;
}

/**
 * Appends the turn the server just answered with, or replaces it if we already
 * have it.
 *
 * A widget action does not always create a new message — answering a
 * clarification can re-emit the turn it belongs to, carrying the same
 * `message_id` with its controls now resolved. Appending that blindly puts two
 * rows with one id into the transcript, which React renders as duplicated or
 * silently omitted children and which makes the list's identity tracking wrong
 * on every subsequent update.
 */
export function mergeTurn(messages: Message[], incoming: Message): Message[] {
  const at = messages.findIndex((message) => message.id === incoming.id);
  if (at === -1) return [...messages, incoming];
  return messages.map((message, index) => (index === at ? incoming : message));
}

/** An optimistic bubble, so the message the person just sent is on screen
 *  before the network has agreed that it exists. */
export function optimisticMessage(text: string): Message {
  return {
    id: `optimistic-${Date.now()}`,
    role: "user",
    content: text,
    widgets: [],
    citations: [],
    created_at: new Date().toISOString(),
  };
}
