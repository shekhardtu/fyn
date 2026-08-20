import type { FynInterrupt } from "@/lib/api";
import { widgetSchema, type Widget, type WidgetActionId } from "@/lib/protocol";

type InterruptResolution = {
  status: "resolved";
  payload: {
    approved: true;
    editedArgs: {
      widgetId: string;
      action: WidgetActionId;
      payload: Record<string, unknown>;
      completeWidget: true;
    };
  };
};

/**
 * Recover the exact server-authored HITL surface when the durable interrupt
 * arrives before the transcript row that normally owns it. The client does
 * not infer presentation from actions or continuation state: an absent,
 * malformed, or mismatched snapshot fails closed.
 */
export function recoverInterruptWidget(interrupt: FynInterrupt): Widget | null {
  const snapshot = widgetSchema.safeParse(interrupt.metadata.widget);
  if (snapshot.success && snapshot.data.id === interrupt.widgetId) return snapshot.data;
  return null;
}

export function interruptActionResolution(
  widgetId: string,
  action: WidgetActionId,
  payload: Record<string, unknown>,
): InterruptResolution {
  return {
    status: "resolved",
    payload: {
      approved: true,
      editedArgs: { widgetId, action, payload, completeWidget: true },
    },
  };
}
