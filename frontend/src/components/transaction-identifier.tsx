import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

const TRANSACTION_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isPersistedTransactionId(value: unknown): value is string {
  return typeof value === "string" && TRANSACTION_UUID.test(value);
}

/**
 * A transaction keeps its UUID for its whole lifetime; amendments only advance
 * the row version. The compact stamp is therefore useful for visually joining
 * a saved card, a later correction card, and the ledger editor. The complete
 * canonical UUID remains in the accessible name, tooltip, and clipboard.
 */
export function TransactionIdentifier({ transactionId, rowVersion, className }: {
  transactionId: unknown;
  rowVersion?: unknown;
  className?: string;
}) {
  const [copyState, setCopyState] = useState<"copied" | "failed" | null>(null);
  const revertTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(revertTimer.current), []);

  if (!isPersistedTransactionId(transactionId)) return null;
  const persistedTransactionId = transactionId;
  const normalized = persistedTransactionId.toUpperCase();
  const compact = normalized.replaceAll("-", "");
  const reference = `TXN ${compact.slice(0, 8)}…${compact.slice(-8)}`;
  const version = typeof rowVersion === "number" && Number.isSafeInteger(rowVersion) && rowVersion > 0 ? rowVersion : null;

  async function copyId() {
    try {
      await navigator.clipboard.writeText(persistedTransactionId);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
    window.clearTimeout(revertTimer.current);
    revertTimer.current = window.setTimeout(() => setCopyState(null), 1800);
  }

  const fullLabel = `Transaction ID ${persistedTransactionId}`;
  return <p className={cn("flex flex-wrap items-center gap-x-1.5 text-meta text-ink-muted opacity-70 transition-opacity hover:opacity-100 focus-within:opacity-100", className)}>
    <button
      type="button"
      title={`${fullLabel} — click to copy`}
      aria-label={copyState === "copied" ? `Copied ${fullLabel}` : `Copy ${fullLabel}`}
      onClick={() => void copyId()}
      className="font-mono tracking-[0.02em] text-inherit hover:text-secondary"
    >
      {copyState === "copied" ? "Copied" : copyState === "failed" ? "Couldn’t copy" : reference}
    </button>
    {version ? <span>· Version {version}</span> : null}
  </p>;
}
