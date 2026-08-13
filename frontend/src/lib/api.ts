import { agentActivityEventSchema, agentResponseSchema, authStatusSchema, bootstrapSchema, categoryDirectoryEntrySchema, categoryDirectorySchema, categorySubcategorySchema, conversationCreatedSchema, conversationPageSchema, conversationSchema, importResultSchema, otpSentSchema, overviewSchema, parseActionPayload, privacyStatusSchema, profileSchema, streamErrorEventSchema, transactionCategoryHintSchema, transactionListItemSchema, transactionListSchema, type AgentActivityEvent, type AgentResponse, type AuthStatusOut, type Bootstrap, type CategoryDirectoryOut, type CategoryDirectorySubcategoryOut, type ConversationCreatedOut, type ConversationOut, type ConversationPage, type ImportResult, type OtpSentOut, type OverviewOut, type PrivacyStatusOut, type ProfileOut, type TransactionCategoryHintOut, type TransactionListItemOut, type TransactionUpdateIn, type WidgetActionId } from "@/lib/protocol";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const UNREACHABLE = "We can’t reach your financial data right now. Check your connection and try again.";

/** Carries the status alongside the sentence.
 *
 *  A 401 is not an error to show — it means the session is gone and the app has
 *  to route to sign-in — so the caller needs to tell it apart from the failures
 *  that do belong in a banner. */
export class ApiError extends Error {
  readonly status: number;
  readonly retryAfterSeconds: number | null;

  constructor(message: string, status: number, retryAfterSeconds: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

export function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

/** FastAPI returns `detail` as a string, or as a list of validation objects.
 *  Either way the banner has to end up with a sentence, never "[object Object]". */
function describe(payload: unknown, status: number) {
  const detail = payload && typeof payload === "object" ? (payload as { detail?: unknown }).detail : null;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const first = detail.find((item) => item && typeof item === "object" && typeof (item as { msg?: unknown }).msg === "string");
    if (first) return String((first as { msg: string }).msg);
  }
  if (status === 401) return "Your session has ended. Sign in again to continue.";
  if (status === 409) return "That action isn’t available any more. Refresh the conversation to see where things stand.";
  if (status === 404) return "That record no longer exists.";
  if (status === 413) return "That file is too large to import.";
  if (status >= 500) return "fyn AI hit an error on its side. Nothing was changed — try again.";
  return UNREACHABLE;
}

/** How long a request may hang before it is treated as unreachable.
 *
 *  A socket that accepts and then never answers is the failure this exists for.
 *  Without a deadline `fetch` waits indefinitely, react-query never rejects, and
 *  the app sits on a spinner that can only be escaped by reloading — which is
 *  exactly what a stalled backend produced here. Ten seconds is longer than any
 *  healthy call and short enough that nobody wonders whether it is broken.
 *
 *  The agent stream is deliberately exempt: a run legitimately takes tens of
 *  seconds, and it has its own abort signal from the thread. */
const REQUEST_TIMEOUT_MS = 10_000;

/** A thrown fetch means the network or the API is down, not a bad request.
 *
 *  `credentials: "include"` on every call is what carries the session: the
 *  cookie is httpOnly, so there is nothing for this module to read or attach by
 *  hand, and a request that omits it is simply unauthenticated. */
async function send(path: string, init?: RequestInit) {
  // A caller that brought its own signal — an upload, a run being abandoned —
  // keeps it; everything else gets the deadline.
  const signal = init?.signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS);
  try {
    return await fetch(`${API_URL}${path}`, { credentials: "include", ...init, signal });
  } catch (cause) {
    // An abort raised by our own deadline is unreachability, not a cancelled
    // request; a caller's own abort is passed through so the thread can tell
    // "nobody is listening" from "this failed".
    if (cause instanceof DOMException && cause.name === "TimeoutError") throw new Error(UNREACHABLE);
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new Error(UNREACHABLE);
  }
}

async function request(path: string, init?: RequestInit) {
  const response = await send(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const retryAfter = Number(response.headers.get("Retry-After"));
    throw new ApiError(describe(payload, response.status), response.status, Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null);
  }
  return response.status === 204 ? null : response.json().catch(() => null);
}

/** Checks a payload against its contract without letting the failure reach a
 *  reader as a Zod path dump.
 *
 *  A response that does not match is a version skew between this client and the
 *  server — a real problem, but not one the person typing can act on, and
 *  `[{"code":"custom","path":["widgets",0]}]` in a banner is worse than
 *  useless. The detail belongs in the console, where whoever shipped the skew
 *  will look; the banner gets a sentence and a way forward. */
function conform<T>(schema: { parse: (value: unknown) => T }, payload: unknown, what: string): T {
  try {
    return schema.parse(payload);
  } catch (cause) {
    console.error(`fyn AI returned a ${what} that does not match this client's contract`, cause, payload);
    throw new Error(`fyn AI sent back a ${what} this version of the app can’t read. Reload the page to pick up the latest version, then try again.`);
  }
}

export async function bootstrap(): Promise<Bootstrap> {
  return conform(bootstrapSchema, await request("/api/bootstrap"), "workspace");
}

export async function loadOverview(month?: string): Promise<OverviewOut> {
  const query = month ? `?month=${encodeURIComponent(`${month}-01`)}` : "";
  return conform(overviewSchema, await request(`/api/overview${query}`), "overview");
}

export async function loadCategories(): Promise<CategoryDirectoryOut[]> {
  return conform(categoryDirectorySchema, await request("/api/categories"), "category directory");
}

/** Naming an existing category returns that entry — the server owns dedup. */
export async function createCategory(name: string): Promise<CategoryDirectoryOut> {
  return conform(categoryDirectoryEntrySchema, await request("/api/categories", {
    method: "POST",
    body: JSON.stringify({ name }),
  }), "created category");
}

export async function createSubcategory(categoryId: string, name: string): Promise<CategoryDirectorySubcategoryOut> {
  return conform(categorySubcategorySchema, await request(`/api/categories/${encodeURIComponent(categoryId)}/subcategories`, {
    method: "POST",
    body: JSON.stringify({ name }),
  }), "created subcategory");
}

export async function renameCategory(categoryId: string, name: string): Promise<CategoryDirectoryOut> {
  return conform(categoryDirectoryEntrySchema, await request(`/api/categories/${encodeURIComponent(categoryId)}`, {
    method: "PATCH", body: JSON.stringify({ name }),
  }), "updated category");
}

export async function deleteCategory(categoryId: string): Promise<void> {
  await request(`/api/categories/${encodeURIComponent(categoryId)}`, { method: "DELETE" });
}

export async function renameSubcategory(categoryId: string, subcategoryId: string, name: string): Promise<CategoryDirectorySubcategoryOut> {
  return conform(categorySubcategorySchema, await request(`/api/categories/${encodeURIComponent(categoryId)}/subcategories/${encodeURIComponent(subcategoryId)}`, {
    method: "PATCH", body: JSON.stringify({ name }),
  }), "updated subcategory");
}

export async function deleteSubcategory(categoryId: string, subcategoryId: string): Promise<void> {
  await request(`/api/categories/${encodeURIComponent(categoryId)}/subcategories/${encodeURIComponent(subcategoryId)}`, { method: "DELETE" });
}

export async function createTransactionHint(categoryId: string, merchant: string, subcategoryId: string | null): Promise<TransactionCategoryHintOut> {
  return conform(transactionCategoryHintSchema, await request(`/api/categories/${encodeURIComponent(categoryId)}/hints`, {
    method: "POST", body: JSON.stringify({ merchant, subcategoryId }),
  }), "created transaction hint");
}

export async function updateTransactionHint(categoryId: string, hintId: string, merchant: string, subcategoryId: string | null): Promise<TransactionCategoryHintOut> {
  return conform(transactionCategoryHintSchema, await request(`/api/categories/${encodeURIComponent(categoryId)}/hints/${encodeURIComponent(hintId)}`, {
    method: "PATCH", body: JSON.stringify({ merchant, subcategoryId }),
  }), "updated transaction hint");
}

export async function deleteTransactionHint(categoryId: string, hintId: string): Promise<void> {
  await request(`/api/categories/${encodeURIComponent(categoryId)}/hints/${encodeURIComponent(hintId)}`, { method: "DELETE" });
}

export type TransactionPageInput = {
  limit?: number;
  offset?: number;
  search?: string;
  transactionType?: TransactionListItemOut["transactionType"] | null;
};

export async function loadTransactions({ limit = 50, offset = 0, search = "", transactionType = null }: TransactionPageInput = {}): Promise<TransactionListItemOut[]> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (search.trim()) query.set("q", search.trim());
  if (transactionType) query.set("transaction_type", transactionType);
  return conform(transactionListSchema, await request(`/api/transactions?${query}`), "transaction list");
}

export async function createTransactionRecord(payload: TransactionUpdateIn): Promise<TransactionListItemOut> {
  return conform(transactionListItemSchema, await request("/api/transactions", {
    method: "POST",
    body: JSON.stringify(payload),
  }), "created transaction");
}

export async function updateTransaction(id: string, payload: TransactionUpdateIn): Promise<TransactionListItemOut> {
  return conform(transactionListItemSchema, await request(`/api/transactions/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  }), "updated transaction");
}

export async function loadConversation(id: string): Promise<ConversationOut> {
  return conform(conversationSchema, await request(`/api/conversations/${id}`), "conversation");
}

export async function createConversation(): Promise<ConversationCreatedOut> {
  return conform(conversationCreatedSchema, await request("/api/conversations", { method: "POST", body: "{}" }), "new conversation");
}

/** One page of history for the rail. Pass the previous page's `nextCursor` to
 *  continue; a null cursor means there is nothing older to load. */
export async function listConversations(cursor?: string | null): Promise<ConversationPage> {
  return conform(conversationPageSchema, await request(`/api/conversations${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`), "conversation list");
}

/** Erases the thread and everything recorded against it. Transactions it
 *  captured are financial history and are deliberately left alone. */
export async function deleteConversation(id: string): Promise<void> {
  await request(`/api/conversations/${id}`, { method: "DELETE" });
}

/** Fire-and-forget version of the same delete, for the moment the page is going
 *  away with an undo window still open: `keepalive` lets the request outlive the
 *  document, so closing the tab doesn't quietly un-press the delete. */
export function flushConversationDeletion(id: string): void {
  void fetch(`${API_URL}/api/conversations/${id}`, { method: "DELETE", keepalive: true }).catch(() => undefined);
}

export async function sendChat(conversationId: string, text: string): Promise<AgentResponse> {
  return conform(agentResponseSchema, await request("/api/chat", {
    method: "POST", body: JSON.stringify({ conversation_id: conversationId, text }),
  }), "reply");
}

export type AgentActivity = AgentActivityEvent;

/** A run can take ten seconds, which is long enough for the reader to leave.
 *  The signal is how the thread says it is no longer listening: without it the
 *  request, the reader loop and this closure all keep going against a
 *  conversation that has been unmounted. */
export async function sendChatStream(
  conversationId: string,
  text: string,
  onActivity: (activity: AgentActivity) => void,
  signal?: AbortSignal,
): Promise<AgentResponse> {
  const response = await fetch(`${API_URL}/api/chat/stream`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ conversation_id: conversationId, text }),
    signal,
  });
  if (!response.ok || !response.body) {
    const payload = await response.json().catch(() => null);
    throw new ApiError(payload?.detail ?? "fyn AI is unavailable.", response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: AgentResponse | null = null;

  function consume(block: string) {
    const lines = block.split(/\r?\n/);
    const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
    const data = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
    if (!event || !data) return;
    const payload = JSON.parse(data);
    if (event === "activity") onActivity(conform(agentActivityEventSchema, payload, "progress update"));
    if (event === "result") result = conform(agentResponseSchema, payload, "reply");
    if (event === "error") throw new Error(conform(streamErrorEventSchema, payload, "error").message);
  }

  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() ?? "";
      blocks.forEach(consume);
      if (done) break;
    }
  } finally {
    // Releases the lock on the body whichever way the loop ended — a thrown
    // stream error, an abort, or a clean finish — so the response can be
    // collected rather than left half-read.
    reader.releaseLock();
  }
  if (buffer.trim()) consume(buffer);
  if (!result) throw new Error("The agent stream ended before returning a result.");
  return result;
}

export async function sendAction(conversationId: string, widgetId: string, action: WidgetActionId, payload: Record<string, unknown>, completeWidget = true): Promise<AgentResponse> {
  const validatedPayload = parseActionPayload(action, payload);
  return conform(agentResponseSchema, await request("/api/actions", {
    method: "POST", body: JSON.stringify({ conversation_id: conversationId, widget_id: widgetId, action, payload: validatedPayload, completeWidget }),
  }), "reply");
}

/** Carries the same name `fetch` gives an aborted request, so a cancelled
 *  upload and a cancelled run are recognised the same way upstream. */
function cancelled() {
  const error = new Error("The upload was cancelled.");
  error.name = "AbortError";
  return error;
}

/** XHR rather than fetch: a statement can be megabytes, and only XHR reports
 *  upload progress, so the composer can show something real while it climbs. */
export function uploadCsv(conversationId: string, file: File, onProgress?: (percent: number) => void, signal?: AbortSignal): Promise<ImportResult> {
  const body = new FormData();
  body.set("conversation_id", conversationId);
  body.set("file", file);
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", `${API_URL}/api/imports/csv`);
    // The fetch calls carry the session through `credentials: "include"`; XHR
    // needs the same thing said its own way or the upload arrives signed out.
    request.withCredentials = true;
    // A statement can be megabytes; walking away from the conversation should
    // stop pushing them. `abort` already rejects, so there is nothing else to do.
    if (signal) {
      if (signal.aborted) { request.abort(); reject(cancelled()); return; }
      signal.addEventListener("abort", () => request.abort(), { once: true });
    }
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress?.(Math.min(99, Math.round(event.loaded / event.total * 100)));
    });
    request.addEventListener("load", () => {
      let payload: unknown = null;
      try { payload = JSON.parse(request.responseText); } catch { payload = null; }
      if (request.status < 200 || request.status >= 300) { reject(new ApiError(describe(payload, request.status), request.status)); return; }
      onProgress?.(100);
      try { resolve(conform(importResultSchema, payload, "import result")); } catch { reject(new Error("The import finished but the response couldn’t be read. Reload to see where it landed.")); }
    });
    request.addEventListener("error", () => reject(new Error(UNREACHABLE)));
    request.addEventListener("abort", () => reject(cancelled()));
    request.send(body);
  });
}

export type PrivacyStatus = PrivacyStatusOut;

export async function getPrivacyStatus(): Promise<PrivacyStatus> {
  return conform(privacyStatusSchema, await request("/api/privacy"), "privacy setting");
}

export async function setLocationEnabled(enabled: boolean): Promise<void> {
  await request("/api/privacy/location", { method: "PATCH", body: JSON.stringify({ enabled }) });
}

export async function revokeSource(sourceType: string): Promise<void> {
  await request(`/api/privacy/sources/${sourceType}/revoke`, { method: "POST", body: "{}" });
}

/** Returns the filename so the drawer can confirm what was saved. */
export async function downloadDataExport(): Promise<string> {
  const response = await send("/api/privacy/export");
  if (!response.ok) throw new Error("Your export couldn’t be prepared. Try again in a moment.");
  const blob = await response.blob();
  if (!blob.size) throw new Error("Your export came back empty, so nothing was saved.");
  const filename = `fyn-ai-export-${new Date().toISOString().slice(0, 10)}.json`;
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = filename;
  // Safari ignores a click on a detached anchor, and revoking the URL in the
  // same tick can cancel the download before it starts.
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(href), 10_000);
  return filename;
}

export async function deleteAllData(): Promise<void> {
  await request("/api/privacy/data", { method: "DELETE", body: JSON.stringify({ confirmation: "DELETE MY DATA" }) });
}

/* ── Signing in ─────────────────────────────────────────────────────────────
 * The session lives in an httpOnly cookie, so there is no token to store, read
 * or refresh here. "Am I signed in?" is a question only the server can answer,
 * and `/api/auth/session` answers it 200 either way rather than by throwing. */

export type AuthStatus = AuthStatusOut;
export type Profile = ProfileOut;
export type OtpSent = OtpSentOut;
export type OtpChannel = "phone" | "email";

export async function getAuthStatus(): Promise<AuthStatus> {
  return conform(authStatusSchema, await request("/api/auth/session"), "sign-in status");
}

/** Sends a sign-in code. Reveals nothing about whether an account exists. */
export async function startSignInCode(channel: OtpChannel, value: string): Promise<OtpSent> {
  return conform(otpSentSchema, await request("/api/auth/otp/start", {
    method: "POST", body: JSON.stringify({ channel, value }),
  }), "verification code");
}

export async function verifySignInCode(challengeId: string, code: string): Promise<AuthStatus> {
  return conform(authStatusSchema, await request("/api/auth/otp/verify", {
    method: "POST", body: JSON.stringify({ challengeId, code }),
  }), "sign-in status");
}

/** Exchanges the Google ID token for a session. The token is verified against
 *  Google's keys on the server; nothing here trusts what it contains. */
export async function signInWithGoogle(credential: string): Promise<AuthStatus> {
  return conform(authStatusSchema, await request("/api/auth/google", {
    method: "POST", body: JSON.stringify({ credential }),
  }), "sign-in status");
}

export async function signOut(): Promise<void> {
  await request("/api/auth/signout", { method: "POST", body: "{}" });
}

/* ── Profile ─────────────────────────────────────────────────────────────── */

export async function getProfile(): Promise<Profile> {
  return conform(profileSchema, await request("/api/profile"), "profile");
}

/** Sends a code to a number or address this account wants to claim. Throws a
 *  409 before sending when it belongs to somebody else. */
export async function startLinkCode(channel: OtpChannel, value: string): Promise<OtpSent> {
  return conform(otpSentSchema, await request("/api/profile/identities/otp/start", {
    method: "POST", body: JSON.stringify({ channel, value }),
  }), "verification code");
}

export async function verifyLinkCode(challengeId: string, code: string): Promise<Profile> {
  return conform(profileSchema, await request("/api/profile/identities/otp/verify", {
    method: "POST", body: JSON.stringify({ challengeId, code }),
  }), "profile");
}

export async function removeIdentity(identityId: string): Promise<Profile> {
  return conform(profileSchema, await request(`/api/profile/identities/${identityId}`, { method: "DELETE" }), "profile");
}
