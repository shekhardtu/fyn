/**
 * The browser suite deliberately shares one durable PostgreSQL conversation.
 * Never create or delete conversations from browser tests; routing every
 * scenario through this one accessor prevents an individual test from silently
 * polluting the rail.
 *
 * The id is resolved by `auth.setup.ts` rather than written here, because the
 * thread belongs to the account that setup signs in as. Hardcoding it survives
 * exactly until the database is rebuilt under a different account, and then
 * every spec lands on "Conversation unavailable" — the app being right about
 * ownership, and the fixture being wrong about it.
 *
 * Read lazily: Playwright imports spec files to collect tests before the setup
 * project has run, so this cannot be a module-level constant.
 */
export function sharedThreadUrl(): string {
  let id: string;
  try {
    id = JSON.parse(readFileSync(SHARED_THREAD_STATE, "utf8")).id;
  } catch {
    throw new Error(`No shared thread recorded at ${SHARED_THREAD_STATE}. Run the setup project first (npm run test:e2e runs it automatically).`);
  }
  if (!id) throw new Error(`${SHARED_THREAD_STATE} does not name a conversation.`);
  return `/c/${id}`;
}

/**
 * The number the browser suite signs in with. It is fixed for the same reason
 * the thread is: the account behind it owns that thread, and a per-run number
 * would create a fresh empty account every time.
 */
export const TEST_PHONE = "+919000000099";
import { readFileSync } from "node:fs";

export const API_URL = process.env.VITE_API_URL ?? "http://localhost:8000";
/** Where the signed-in session is kept between the setup project and the tests. */
export const STORAGE_STATE = "e2e/.auth/session.json";
/** Where the resolved shared conversation is recorded, for the same reason. */
export const SHARED_THREAD_STATE = "e2e/.auth/thread.json";
