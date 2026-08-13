import Constants from "expo-constants";
import { fetch as streamingFetch } from "expo/fetch";

import { HOLDS_TOKEN, currentSession, saveSession } from "@/lib/session";
import {
  agentActivityEventSchema,
  agentResponseSchema,
  authStatusSchema,
  bootstrapSchema,
  categoryDirectorySchema,
  conversationCreatedSchema,
  conversationPageSchema,
  conversationSchema,
  importResultSchema,
  otpSentSchema,
  overviewSchema,
  parseActionPayload,
  privacyStatusSchema,
  profileSchema,
  streamErrorEventSchema,
  transactionListItemSchema,
  transactionListSchema,
  type AgentActivityEvent,
  type AgentResponse,
  type AuthStatusOut,
  type Bootstrap,
  type CategoryDirectoryOut,
  type ConversationCreatedOut,
  type ConversationOut,
  type ConversationPage,
  type ImportResult,
  type OtpSentOut,
  type OverviewOut,
  type PrivacyStatusOut,
  type ProfileOut,
  type TransactionListItemOut,
  type TransactionUpdateIn,
  type WidgetActionId,
} from "@/lib/protocol";

const UNREACHABLE = "We can’t reach your financial data right now. Check your connection and try again.";

/**
 * Where the API lives.
 *
 * The simulator shares the host's loopback, so `localhost` is right there and
 * wrong everywhere else: a physical phone resolves it to itself. Expo already
 * knows the address the packager is being reached on, which is by definition an
 * address this device can route to, so the dev host is derived from that and
 * only the port is assumed. `EXPO_PUBLIC_API_URL` overrides both.
 */
function resolveApiUrl() {
  const configured = process.env.EXPO_PUBLIC_API_URL;
  if (configured) return configured.replace(/\/$/, "");
  const hostUri = Constants.expoConfig?.hostUri ?? Constants.expoGoConfig?.debuggerHost;
  const host = hostUri?.split(":")[0];
  return host ? `http://${host}:8000` : "http://localhost:8000";
}

export const API_URL = resolveApiUrl();

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
  if (status === 409) return "That action isn’t available any more. Pull to refresh to see where things stand.";
  if (status === 404) return "That record no longer exists.";
  if (status === 413) return "That file is too large to import.";
  if (status >= 500) return "fyn AI hit an error on its side. Nothing was changed — try again.";
  return UNREACHABLE;
}

/** How long a request may hang before it is treated as unreachable.
 *
 *  A socket that accepts and then never answers is the failure this exists for.
 *  On a phone it is the common one: walking out of coverage holds the socket
 *  open rather than failing it. Ten seconds is longer than any healthy call and
 *  short enough that nobody wonders whether it is broken.
 *
 *  The agent stream is deliberately exempt: a run legitimately takes tens of
 *  seconds, and it has its own abort signal from the screen. */
const REQUEST_TIMEOUT_MS = 10_000;

/** Every request says who it is and what it is holding.
 *
 *  `X-Client: native` is what earns the bearer token at sign-in. The web app
 *  carries its session in an httpOnly cookie it cannot read; there is no such
 *  thing to inherit here, so the token comes out of the Keychain and is
 *  attached by hand. */
export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = currentSession();
  return {
    "Content-Type": "application/json",
    // Declared only where the app can actually hold what the header earns. On
    // web the browser carries the httpOnly cookie and asking for the token in
    // the body would put a readable copy of the session on the page.
    ...(HOLDS_TOKEN ? { "X-Client": "native" } : {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...extra,
  };
}

/** On web the session is a cookie the request has to be told to send. On
 *  native there is no cookie and the bearer header does the work. */
const CREDENTIALS: RequestCredentials | undefined = HOLDS_TOKEN ? undefined : "include";

/** A thrown fetch means the network or the API is down, not a bad request. */
async function send(path: string, init?: RequestInit) {
  // A caller that brought its own signal — an upload, a run being abandoned —
  // keeps it; everything else gets the deadline.
  const signal = init?.signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS);
  try {
    return await fetch(`${API_URL}${path}`, { credentials: CREDENTIALS, ...init, signal });
  } catch (cause) {
    // An abort raised by our own deadline is unreachability, not a cancelled
    // request; a caller's own abort is passed through so the screen can tell
    // "nobody is listening" from "this failed".
    const name = (cause as Error)?.name;
    if (name === "TimeoutError") throw new Error(UNREACHABLE);
    if (name === "AbortError") throw cause;
    throw new Error(UNREACHABLE);
  }
}

async function request(path: string, init?: RequestInit) {
  const response = await send(path, { ...init, headers: authHeaders(init?.headers as Record<string, string>) });
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
 *  A response that does not match is a version skew between this build and the
 *  server — a real problem, but not one the person typing can act on. The
 *  detail belongs in the log; the banner gets a sentence and a way forward.
 *  Reloading is not the fix on a phone that it is in a browser, so this says
 *  "update" instead. */
function conform<T>(schema: { parse: (value: unknown) => T }, payload: unknown, what: string): T {
  try {
    return schema.parse(payload);
  } catch (cause) {
    console.error(`fyn AI returned a ${what} that does not match this client's contract`, cause, payload);
    throw new Error(`fyn AI sent back a ${what} this version of the app can’t read. Update the app, then try again.`);
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

export type AgentActivity = AgentActivityEvent;

/**
 * The run, streamed.
 *
 * React Native's own `fetch` is a polyfill over `XMLHttpRequest` and has no
 * `response.body` at all, so the web client's reader loop cannot run on it.
 * `expo/fetch` is a real WinterCG implementation with a genuine
 * `ReadableStream`, which is what lets the frame parser below stay the same
 * parser the web app uses rather than a second one that drifts from it.
 *
 * A run can take tens of seconds, which is long enough for the reader to leave.
 * The signal is how the screen says it is no longer listening. It is also
 * checked by hand around every chunk: `expo/fetch` does not document honouring
 * `signal` mid-stream, and a loop that keeps decoding into an unmounted screen
 * is the difference between a cancelled run and a leak.
 */
export async function sendChatStream(
  conversationId: string,
  text: string,
  onActivity: (activity: AgentActivity) => void,
  signal?: AbortSignal,
): Promise<AgentResponse> {
  const abandoned = () => {
    const error = new Error("The run was cancelled.");
    error.name = "AbortError";
    return error;
  };
  if (signal?.aborted) throw abandoned();

  let response: Awaited<ReturnType<typeof streamingFetch>>;
  try {
    response = await streamingFetch(`${API_URL}/api/chat/stream`, {
      method: "POST",
      credentials: CREDENTIALS,
      headers: authHeaders({ Accept: "text/event-stream" }),
      body: JSON.stringify({ conversation_id: conversationId, text }),
      signal,
    });
  } catch (cause) {
    if ((cause as Error)?.name === "AbortError") throw cause;
    throw new Error(UNREACHABLE);
  }

  if (!response.ok || !response.body) {
    const payload = await response.json().catch(() => null);
    throw new ApiError(describe(payload, response.status), response.status);
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
      if (signal?.aborted) throw abandoned();
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
    // `cancel` is what actually stops the socket. Releasing the lock alone
    // leaves the server writing into a body nobody will ever read again.
    if (!result) void response.body?.cancel().catch(() => undefined);
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

export type PickedFile = { uri: string; name: string; mimeType?: string | null; size?: number | null };

/** XHR rather than fetch: a statement can be megabytes, and only XHR reports
 *  upload progress, so the composer can show something real while it climbs.
 *  On React Native, XHR is the native networking stack directly. */
export function uploadCsv(conversationId: string, file: PickedFile, onProgress?: (percent: number) => void, signal?: AbortSignal): Promise<ImportResult> {
  const body = new FormData();
  body.append("conversation_id", conversationId);
  // React Native's FormData takes the file by reference: the bytes are read by
  // the native layer straight from disk, so a large statement is never pulled
  // through JavaScript memory on its way out.
  body.append("file", { uri: file.uri, name: file.name, type: file.mimeType || "text/csv" } as unknown as Blob);

  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", `${API_URL}/api/imports/csv`);
    const token = currentSession();
    if (HOLDS_TOKEN) request.setRequestHeader("X-Client", "native");
    if (token) request.setRequestHeader("Authorization", `Bearer ${token}`);
    // The fetch calls carry the web session through `credentials`; XHR needs
    // the same thing said its own way or the upload arrives signed out.
    if (!HOLDS_TOKEN) request.withCredentials = true;
    // Deliberately no Content-Type: only the runtime knows the multipart
    // boundary it generated, and setting the header by hand loses it.
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
      try { resolve(conform(importResultSchema, payload, "import result")); } catch { reject(new Error("The import finished but the response couldn’t be read. Pull to refresh to see where it landed.")); }
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

export async function deleteAllData(): Promise<void> {
  await request("/api/privacy/data", { method: "DELETE", body: JSON.stringify({ confirmation: "DELETE MY DATA" }) });
}

/** The raw export, for handing to the system share sheet. There is no download
 *  folder to write to on a phone, so the caller decides where it goes. */
export async function fetchDataExport(): Promise<string> {
  const response = await send("/api/privacy/export", { headers: authHeaders() });
  if (!response.ok) throw new Error("Your export couldn’t be prepared. Try again in a moment.");
  const body = await response.text();
  if (!body) throw new Error("Your export came back empty, so nothing was saved.");
  return body;
}

/* ── Signing in ───────────────────────────────────────────────────────────── */

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

/** Persists the session before returning, so the very next request already
 *  carries it — no window where the app believes it is signed in and the
 *  requests it fires do not say so. */
export async function verifySignInCode(challengeId: string, code: string): Promise<AuthStatus> {
  const status = conform(authStatusSchema, await request("/api/auth/otp/verify", {
    method: "POST", body: JSON.stringify({ challengeId, code }),
  }), "sign-in status");
  if (status.sessionToken) await saveSession(status.sessionToken);
  return status;
}

/** Exchanges a Google ID token for a session. The token is verified against
 *  Google's keys on the server; nothing here trusts what it contains. */
export async function signInWithGoogle(credential: string): Promise<AuthStatus> {
  const status = conform(authStatusSchema, await request("/api/auth/google", {
    method: "POST", body: JSON.stringify({ credential }),
  }), "sign-in status");
  if (status.sessionToken) await saveSession(status.sessionToken);
  return status;
}

export async function signOut(): Promise<void> {
  await request("/api/auth/signout", { method: "POST", body: "{}" }).catch(() => null);
}

export async function getProfile(): Promise<Profile> {
  return conform(profileSchema, await request("/api/profile"), "profile");
}
