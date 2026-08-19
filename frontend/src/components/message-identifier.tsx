import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";

const PERSISTED_MESSAGE_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/** The message's place in the book. While the server has not yet confirmed the
 *  stored row the line reads "Posting…" beside a small spinner — honest about
 *  being in flight without surfacing system vocabulary. The moment the reply
 *  names the persisted row, the ID stamps in; clicking the stamp copies the
 *  complete UUID, since the eight visible characters exist to be compact, not
 *  to be the identifier. */
export function MessageIdentifier({ messageId }: { messageId: string }) {
  const [copyState, setCopyState] = useState<"copied" | "failed" | null>(null);
  const revertTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(revertTimer.current), []);

  const persisted = PERSISTED_MESSAGE_ID.test(messageId);
  if (!persisted) {
    return <span
      aria-label="Message ID pending until the message is persisted"
      className="inline-flex items-center gap-1 text-meta text-ink-muted"
    >
      <Loader2 size={10} aria-hidden className="shrink-0 animate-spin" />
      Posting…
    </span>;
  }

  async function copyId() {
    try {
      await navigator.clipboard.writeText(messageId);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
    // Cleared before it is replaced, so clicking twice does not leave an
    // earlier timer to reset the label out from under the later one, and
    // cancelled on unmount so it cannot fire into a message that has gone.
    window.clearTimeout(revertTimer.current);
    revertTimer.current = window.setTimeout(() => setCopyState(null), 1800);
  }

  const fullLabel = `Message ID ${messageId}`;
  return <button
    type="button"
    title={`${fullLabel} — click to copy`}
    aria-label={copyState === "copied" ? `Copied ${fullLabel}` : `Copy ${fullLabel}`}
    onClick={() => void copyId()}
    className="entry-stamp text-meta text-ink-muted hover:text-secondary"
  >
    {copyState === "copied"
      ? "Copied"
      : copyState === "failed"
        ? "Couldn’t copy"
        : <>ID <span className="font-mono">{messageId.slice(0, 8)}</span></>}
  </button>;
}
