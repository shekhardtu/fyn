import { Popover } from "@base-ui/react/popover";
import { Check, Clock3, Copy, Ellipsis, Hash, Loader2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { MessageDeliveryTime } from "@/components/message-delivery-time";
import { Button } from "@/components/ui/button";

type CopyTarget = "message" | "id";
type CopyState = { target: CopyTarget; result: "copied" | "failed" } | null;
const PERSISTED_MESSAGE_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/**
 * A person's message is a reading surface first. The text stays native and
 * selectable; copy and delivery metadata live in explicit adjacent controls
 * so dragging, double-clicking, or long-pressing the bubble can never trigger
 * an unrelated action.
 */
export function UserMessage({ content, messageId, deliveredAt }: {
  content: string;
  messageId: string;
  deliveredAt: string;
}) {
  const [copyState, setCopyState] = useState<CopyState>(null);
  const revertTimer = useRef<number | undefined>(undefined);
  const persisted = PERSISTED_MESSAGE_ID.test(messageId);

  useEffect(() => () => window.clearTimeout(revertTimer.current), []);

  async function copy(target: CopyTarget, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopyState({ target, result: "copied" });
    } catch {
      setCopyState({ target, result: "failed" });
    }
    window.clearTimeout(revertTimer.current);
    revertTimer.current = window.setTimeout(() => setCopyState(null), 1800);
  }

  function actionLabel(target: CopyTarget) {
    if (copyState?.target !== target) return target === "message" ? "Copy message" : "Copy message ID";
    const subject = target === "message" ? "Message" : "Message ID";
    return copyState.result === "copied" ? `${subject} copied` : `${subject} could not be copied`;
  }

  function actionText(target: CopyTarget) {
    if (copyState?.target !== target) return target === "message" ? "Copy message" : "Copy message ID";
    return copyState.result === "copied" ? "Copied" : "Couldn’t copy";
  }

  return <div className="user-message relative ml-auto w-fit max-w-full">
    <p
      data-message-content
      className="cursor-text select-text break-words whitespace-pre-wrap rounded-xl rounded-br-sm bg-secondary px-4 py-3 text-left text-body leading-6 text-on-secondary"
    >{content}</p>

    <Popover.Root>
      <Popover.Trigger
        render={<Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Message options"
          className="user-message-options-trigger absolute top-1/2 right-full mr-1 -translate-y-1/2 rounded-full border border-line bg-surface/95 text-ink-muted shadow-sm hover:text-secondary"
        />}
      >
        <Ellipsis aria-hidden />
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Positioner side="top" align="end" sideOffset={8} className="z-50 outline-none">
          <Popover.Popup className="w-64 max-w-[calc(100vw-2rem)] origin-[var(--transform-origin)] rounded-xl border border-line-strong bg-surface p-1.5 text-ink shadow-[var(--shadow-overlay)] outline-none transition-[transform,opacity] duration-[var(--m-state)] ease-out data-starting-style:scale-[.98] data-starting-style:opacity-0 data-ending-style:scale-[.98] data-ending-style:opacity-0 motion-reduce:transition-none">
            <Popover.Title className="sr-only">Message information and actions</Popover.Title>
            <section aria-label="Message information" className="rounded-lg bg-surface-sunken px-3 py-2.5">
              <p className="flex items-center gap-1.5 text-meta font-semibold text-ink-body"><Clock3 size={13} aria-hidden />Delivery</p>
              {deliveredAt
                ? <MessageDeliveryTime deliveredAt={deliveredAt} className="mt-1 block" />
                : <span className="mt-1 inline-flex items-center gap-1 text-meta text-ink-muted"><Loader2 size={10} aria-hidden className="animate-spin" /> Sending…</span>}
              <p className="mt-2 flex items-center gap-1.5 text-meta text-ink-muted"><Hash size={12} aria-hidden /><span>Message ID</span><code className="ml-auto font-mono text-[11px] text-ink-body">{persisted ? messageId.slice(0, 8) : "Pending"}</code></p>
            </section>

            <div aria-label="Message actions" className="mt-1 space-y-0.5">
              <Button
                type="button"
                variant="ghost"
                size="lg"
                aria-label={actionLabel("message")}
                onClick={() => void copy("message", content)}
                className="w-full justify-start px-2.5 text-ink-body"
              >
                {copyState?.target === "message" && copyState.result === "copied" ? <Check aria-hidden /> : <Copy aria-hidden />}
                <span aria-live="polite">{actionText("message")}</span>
              </Button>
              {persisted ? <Button
                type="button"
                variant="ghost"
                size="lg"
                aria-label={actionLabel("id")}
                onClick={() => void copy("id", messageId)}
                className="w-full justify-start px-2.5 text-ink-body"
              >
                {copyState?.target === "id" && copyState.result === "copied" ? <Check aria-hidden /> : <Hash aria-hidden />}
                <span aria-live="polite">{actionText("id")}</span>
              </Button> : null}
            </div>
          </Popover.Popup>
        </Popover.Positioner>
      </Popover.Portal>
    </Popover.Root>
  </div>;
}
