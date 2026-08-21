import { useSyncExternalStore } from "react";

/**
 * One shared answer to "are tables drawn wide?".
 *
 * Widening is a reading preference, not a property of one table: a reader who
 * wants more columns wants them everywhere. Every table's width toggle reads
 * and writes this store, so expanding one expands all of them, in every
 * conversation — and it outlives the tab, because a preference you have to
 * re-state after every reload is not a preference, it is a chore.
 */
const STORAGE_KEY = "fyn.tables-wide";
const listeners = new Set<() => void>();

function stored() {
  try {
    return localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    // Private browsing, or storage switched off. The session still works.
    return false;
  }
}

let wide = typeof window === "undefined" ? false : stored();

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function tablesWide() {
  return wide;
}

export function setTablesWide(next: boolean) {
  if (wide === next) return;
  wide = next;
  try {
    if (next) localStorage.setItem(STORAGE_KEY, "1");
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    // The choice simply won't survive a reload.
  }
  for (const listener of [...listeners]) listener();
}

export function useTablesWide(): [boolean, typeof setTablesWide] {
  return [useSyncExternalStore(subscribe, tablesWide, () => false), setTablesWide];
}

/** The breakout drawn by a wide table: 80% of the conversation pane — the
 *  `.conversation-scroll` size container, gutters included — but never
 *  narrower than the table's normal place in the transcript.
 *
 *  It starts at `sm` because below that a table is already edge-to-edge with
 *  the screen, and 80% of a phone is narrower than the place it is leaving.
 *
 *  It lifts over its neighbours but stays under the site header's 20: a
 *  streaming reply renders outside the virtual list, with no transform to put
 *  it in its own stacking context, so a tie there would paint table rows over
 *  the header. `.ledger-table` is already positioned, so no `relative` here. */
export const WIDE_TABLE_BREAKOUT = "sm:left-1/2 sm:z-10 sm:w-[max(100%,80cqw)] sm:-translate-x-1/2";
