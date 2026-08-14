import { useSyncExternalStore } from "react";

/**
 * One shared answer to "are tables drawn wide?".
 *
 * Widening is a reading preference, not a property of one table: a reader who
 * wants more columns wants them everywhere. Every table's width toggle reads
 * and writes this store, so expanding one expands all of them, in every
 * conversation, for the rest of the session. Maximize stays per-table — a
 * modal inspects one table and says nothing about the others.
 */
let wide = false;
const listeners = new Set<() => void>();

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function setTablesWide(next: boolean) {
  if (wide === next) return;
  wide = next;
  for (const listener of [...listeners]) listener();
}

export function useTablesWide(): [boolean, typeof setTablesWide] {
  return [useSyncExternalStore(subscribe, () => wide, () => false), setTablesWide];
}

/** The breakout drawn by a wide table: 80% of the conversation pane — the
 *  `.conversation-scroll` size container, gutters included — but never
 *  narrower than the table's normal place in the transcript. */
export const WIDE_TABLE_BREAKOUT = "relative left-1/2 z-20 w-[max(100%,80cqw)] -translate-x-1/2";
