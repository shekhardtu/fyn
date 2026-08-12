/**
 * The browser suite deliberately shares one durable PostgreSQL conversation.
 * Never create or delete conversations from browser tests; keeping this ID in
 * one file prevents an individual scenario from silently polluting the rail.
 */
export const TEST_CONVERSATION_ID = "6aa484c7-c64c-4a6f-ae11-b031c75b77b5";
export const TEST_CONVERSATION_URL = `/c/${TEST_CONVERSATION_ID}`;

/**
 * The number the browser suite signs in with. It is fixed for the same reason
 * the thread is: the account behind it owns that thread, and a per-run number
 * would create a fresh empty account every time.
 */
export const TEST_PHONE = "+919000000099";
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
/** Where the signed-in session is kept between the setup project and the tests. */
export const STORAGE_STATE = "e2e/.auth/session.json";
