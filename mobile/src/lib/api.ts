import {
  AgentCapabilitiesSchema,
  EventSchemas,
  EventType,
  type AgentCapabilities,
  type Interrupt,
  type Message as AgUiMessage,
  type State,
} from "@ag-ui/core";
import Constants from "expo-constants";
import * as Crypto from "expo-crypto";
import { fetch as streamingFetch } from "expo/fetch";
import { applyPatch } from "fast-json-patch";

import { HOLDS_TOKEN, currentSession, saveSession } from "@/lib/session";
import {
  agentActivityEventSchema,
  agentResponseSchema,
  agentThreadStateSchema,
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
  transactionListItemSchema,
  transactionListSchema,
  type AgentActivityEvent,
  type AgentInterruptOut,
  type AgentResponse,
  type AgentThreadStateOut,
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
  const result = conform(bootstrapSchema, await request("/api/bootstrap"), "workspace");
  hydrateNativeAgent(result.active_conversation.id, result.active_conversation.messages);
  return result;
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
  const result = conform(conversationSchema, await request(`/api/conversations/${id}`), "conversation");
  hydrateNativeAgent(result.id, result.messages);
  return result;
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

export type AgentRunPhase = "connecting" | "running" | "interrupted" | "succeeded" | "failed" | "reconnecting";

export interface FynInterrupt {
  id: string;
  runId: string;
  toolCallId?: string;
  widgetId: string;
  reason: string;
  message?: string;
  expiresAt?: string;
  responseSchema?: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

export interface FynAgentRunResult {
  response: AgentResponse;
  runId: string;
  interrupts: FynInterrupt[];
  reasoningSummary: string;
}

export interface AgentRunCallbacks {
  onActivity?: (activity: AgentActivity) => void;
  onRunCreated?: (runId: string) => void;
  onPhase?: (phase: AgentRunPhase) => void;
  onReasoning?: (summary: string) => void;
  onText?: (text: string) => void;
}

type RunCommand = {
  message?: string;
  forwardedProps?: Record<string, unknown>;
  resume?: Array<{ interruptId: string; status: "resolved" | "cancelled"; payload?: unknown }>;
  runId?: string;
  replay?: boolean;
};

type NativeAgentStore = {
  state: State;
  messages: AgUiMessage[];
  pendingInterrupts: Interrupt[];
};

const nativeAgentStores = new Map<string, NativeAgentStore>();
const nativeRunCursors = new Map<string, { safe: number; seen: Set<number> }>();
let capabilitiesRequest: Promise<AgentCapabilities> | undefined;

function nativeAgentStore(threadId: string): NativeAgentStore {
  const existing = nativeAgentStores.get(threadId);
  if (existing) return existing;
  const store: NativeAgentStore = { state: {}, messages: [], pendingInterrupts: [] };
  nativeAgentStores.set(threadId, store);
  return store;
}

function nativeRunCursor(runId: string): { safe: number; seen: Set<number> } {
  const existing = nativeRunCursors.get(runId);
  if (existing) return existing;
  const cursor = { safe: 0, seen: new Set<number>() };
  nativeRunCursors.set(runId, cursor);
  if (nativeRunCursors.size > 20) nativeRunCursors.delete(nativeRunCursors.keys().next().value as string);
  return cursor;
}

function hydrateNativeAgent(threadId: string, messages: ConversationOut["messages"]): void {
  const store = nativeAgentStore(threadId);
  const durable = messages
    .filter((message) => message.role === "user" || message.role === "assistant")
    .map((message) => ({ id: message.id, role: message.role, content: message.content }) as AgUiMessage);
  const local = store.messages.filter((message) => message.role === "activity" || message.role === "reasoning");
  store.messages = [...durable, ...local];
}

async function agentCapabilities(): Promise<AgentCapabilities> {
  capabilitiesRequest ??= request("/api/agent/capabilities")
    .then((payload) => conform(AgentCapabilitiesSchema, payload, "agent capability declaration"))
    .catch((error) => {
      capabilitiesRequest = undefined;
      throw error;
    });
  return capabilitiesRequest;
}

function eventCursor(event: { rawEvent?: unknown }): { sequence: number; replaySafe: boolean } | null {
  const raw = event.rawEvent;
  if (!raw || typeof raw !== "object") return null;
  const fyn = (raw as { fyn?: unknown }).fyn;
  if (!fyn || typeof fyn !== "object") return null;
  const sequence = Number((fyn as { sequence?: unknown }).sequence);
  if (!Number.isInteger(sequence) || sequence < 1) return null;
  return { sequence, replaySafe: (fyn as { replaySafe?: unknown }).replaySafe === true };
}

function reduceNativeEvent(store: NativeAgentStore, event: ReturnType<typeof EventSchemas.parse>): void {
  const messages = store.messages as Array<Record<string, unknown>>;
  if (event.type === EventType.STATE_SNAPSHOT) store.state = structuredClone(event.snapshot);
  if (event.type === EventType.STATE_DELTA) {
    try {
      store.state = applyPatch(structuredClone(store.state), event.delta, true, false).newDocument;
    } catch (error) {
      console.warn("AG-UI state delta could not be applied", error);
    }
  }
  if (event.type === EventType.MESSAGES_SNAPSHOT) {
    const snapshot = structuredClone(event.messages);
    const byId = new Map(snapshot.map((message) => [message.id, message]));
    const snapshotHasReasoning = snapshot.some((message) => message.role === "reasoning");
    const preserveLocal = (message: AgUiMessage) =>
      message.role === "activity" || (message.role === "reasoning" && !snapshotHasReasoning);
    const merged = store.messages
      .filter((message) => preserveLocal(message) || byId.has(message.id))
      .map((message) => preserveLocal(message) ? message : byId.get(message.id) as AgUiMessage);
    const mergedIds = new Set(merged.map((message) => message.id));
    for (const message of snapshot) if (!mergedIds.has(message.id)) merged.push(message);
    store.messages = merged;
  }
  if (event.type === EventType.TEXT_MESSAGE_START && !messages.some((message) => message.id === event.messageId)) {
    messages.push({ id: event.messageId, role: event.role ?? "assistant", content: "" });
  }
  if (event.type === EventType.TEXT_MESSAGE_CONTENT) {
    const message = messages.find((candidate) => candidate.id === event.messageId);
    if (message) message.content = `${typeof message.content === "string" ? message.content : ""}${event.delta}`;
  }
  if (event.type === EventType.TOOL_CALL_START) {
    let parent = messages.find((message) => message.id === event.parentMessageId && message.role === "assistant");
    if (!parent) {
      const parentIdInUse = event.parentMessageId && messages.some((message) => message.id === event.parentMessageId);
      parent = {
        id: parentIdInUse ? event.toolCallId : event.parentMessageId ?? event.toolCallId,
        role: "assistant",
        content: "",
        toolCalls: [],
      };
      messages.push(parent);
    }
    const calls = Array.isArray(parent.toolCalls) ? parent.toolCalls as Array<Record<string, unknown>> : [];
    if (!calls.some((call) => call.id === event.toolCallId)) {
      calls.push({ id: event.toolCallId, type: "function", function: { name: event.toolCallName, arguments: "" } });
    }
    parent.toolCalls = calls;
  }
  if (event.type === EventType.TOOL_CALL_ARGS) {
    for (const message of messages) {
      const calls = Array.isArray(message.toolCalls) ? message.toolCalls as Array<Record<string, unknown>> : [];
      const call = calls.find((candidate) => candidate.id === event.toolCallId);
      const fn = call?.function;
      if (fn && typeof fn === "object") {
        const record = fn as Record<string, unknown>;
        record.arguments = `${typeof record.arguments === "string" ? record.arguments : ""}${event.delta}`;
      }
    }
  }
  if (event.type === EventType.TOOL_CALL_RESULT && !messages.some((message) => message.id === event.messageId)) {
    const result = {
      id: event.messageId,
      role: event.role ?? "tool",
      toolCallId: event.toolCallId,
      content: event.content,
    };
    const assistantIndex = messages.findIndex(
      (message) => message.role === "assistant"
        && Array.isArray(message.toolCalls)
        && message.toolCalls.some((call) => call.id === event.toolCallId),
    );
    if (assistantIndex < 0) messages.push(result);
    else {
      let insertionIndex = assistantIndex + 1;
      while (insertionIndex < messages.length && messages[insertionIndex].role === "tool") insertionIndex += 1;
      messages.splice(insertionIndex, 0, result);
    }
  }
  if (event.type === EventType.ACTIVITY_SNAPSHOT) {
    const index = messages.findIndex((message) => message.id === event.messageId);
    const activity = { id: event.messageId, role: "activity", activityType: event.activityType, content: structuredClone(event.content) };
    if (index < 0) messages.push(activity);
    else if (event.replace !== false) messages[index] = activity;
  }
  if (event.type === EventType.ACTIVITY_DELTA) {
    const activity = messages.find((message) => message.id === event.messageId && message.role === "activity");
    if (activity) {
      try {
        activity.content = applyPatch(structuredClone(activity.content ?? {}), event.patch, true, false).newDocument;
        activity.activityType = event.activityType;
      } catch (error) {
        console.warn(`AG-UI activity delta for ${event.messageId} could not be applied`, error);
      }
    }
  }
  if (event.type === EventType.REASONING_MESSAGE_START && !messages.some((message) => message.id === event.messageId)) {
    messages.push({ id: event.messageId, role: "reasoning", content: "" });
  }
  if (event.type === EventType.REASONING_MESSAGE_CONTENT) {
    const message = messages.find((candidate) => candidate.id === event.messageId);
    if (message) message.content = `${typeof message.content === "string" ? message.content : ""}${event.delta}`;
  }
  if (event.type === EventType.RUN_FINISHED) {
    store.pendingInterrupts = event.outcome?.type === "interrupt" ? [...event.outcome.interrupts] : [];
  }
}

function protocolInterrupt(interrupt: Interrupt, runId: string): FynInterrupt {
  const metadata = interrupt.metadata ?? {};
  return {
    id: interrupt.id,
    runId,
    toolCallId: interrupt.toolCallId,
    widgetId: typeof metadata.widgetId === "string" ? metadata.widgetId : "",
    reason: interrupt.reason,
    message: interrupt.message,
    expiresAt: interrupt.expiresAt,
    responseSchema: interrupt.responseSchema,
    metadata,
  };
}

function storedInterrupt(interrupt: AgentInterruptOut): FynInterrupt {
  return {
    id: interrupt.id,
    runId: interrupt.runId,
    toolCallId: interrupt.toolCallId,
    widgetId: interrupt.widgetId,
    reason: interrupt.reason,
    message: interrupt.message ?? undefined,
    expiresAt: interrupt.expiresAt ?? undefined,
    responseSchema: interrupt.responseSchema,
    metadata: interrupt.metadata,
  };
}

/**
 * Thin native AG-UI adapter.
 *
 * AG-UI does not currently ship a React Native client adapter. Expo's native
 * WinterCG fetch supplies the stream transport; `@ag-ui/core` supplies the
 * canonical event discriminator and validation. Fyn-specific projection is
 * read only from its documented CUSTOM event after every standard event has
 * crossed that protocol boundary.
 */
async function runFynAgent(
  conversationId: string,
  command: RunCommand,
  callbacks: AgentRunCallbacks = {},
  signal?: AbortSignal,
): Promise<FynAgentRunResult> {
  const abandoned = () => {
    const error = new Error("The agent run was detached.");
    error.name = "AbortError";
    return error;
  };
  if (signal?.aborted) throw abandoned();

  const runId = command.runId ?? Crypto.randomUUID();
  callbacks.onRunCreated?.(runId);
  callbacks.onPhase?.(command.replay ? "reconnecting" : "connecting");
  const advertised = await agentCapabilities();
  if (!advertised.transport?.streaming) throw new Error("fyn AI does not currently advertise streaming AG-UI support.");
  if (command.resume && !advertised.humanInTheLoop?.interrupts) {
    throw new Error("fyn AI does not currently advertise interrupt resumption.");
  }
  const store = nativeAgentStore(conversationId);
  const inputMessageId = command.message ? Crypto.randomUUID() : null;
  if (command.message && inputMessageId) store.messages.push({ id: inputMessageId, role: "user", content: command.message });
  const discardRejectedInput = () => {
    if (!inputMessageId) return;
    store.messages = store.messages.filter((message) => message.id !== inputMessageId);
  };
  const input = {
    threadId: conversationId,
    runId,
    state: structuredClone(store.state),
    messages: structuredClone(store.messages.filter((message) => message.role !== "activity")),
    tools: [],
    context: [],
    forwardedProps: command.forwardedProps ?? {},
    ...(command.resume ? { resume: command.resume } : {}),
  };

  let result: AgentResponse | null = null;
  let interrupts: FynInterrupt[] = [];
  let reasoningSummary = "";
  let assistantText = "";
  let assistantMessageId = "";
  const cursorState = nativeRunCursor(runId);
  const runFailure: { message: string | null; code: string | null } = { message: null, code: null };
  let finished = false;
  let replay = Boolean(command.replay);

  function consume(block: string, connection: { started: boolean }) {
    const lines = block.split(/\r?\n/);
    const data = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
    if (!data) return;
    const event = EventSchemas.parse(JSON.parse(data));
    if (!connection.started && event.type !== EventType.RUN_STARTED && event.type !== EventType.RUN_ERROR) {
      throw new Error("The AG-UI stream did not begin with a run lifecycle event.");
    }
    if (event.type === EventType.RUN_STARTED) {
      connection.started = true;
    }
    const cursor = eventCursor(event);
    if (cursor) {
      if (cursorState.seen.has(cursor.sequence)) return;
      cursorState.seen.add(cursor.sequence);
      if (cursor.replaySafe) cursorState.safe = Math.max(cursorState.safe, cursor.sequence);
    }
    reduceNativeEvent(store, event);
    if (event.type === EventType.RUN_STARTED) callbacks.onPhase?.(replay ? "reconnecting" : "running");
    if (event.type === EventType.ACTIVITY_SNAPSHOT && event.activityType === "fyn.agent_activity.v1") {
      callbacks.onActivity?.(conform(agentActivityEventSchema, event.content, "progress update"));
    }
    if (event.type === EventType.REASONING_MESSAGE_CONTENT) {
      const reasoning = store.messages.find((message) => message.id === event.messageId);
      reasoningSummary = typeof reasoning?.content === "string" ? reasoning.content : `${reasoningSummary}${event.delta}`;
      callbacks.onReasoning?.(reasoningSummary);
    }
    if (event.type === EventType.TEXT_MESSAGE_CONTENT) {
      const message = store.messages.find((candidate) => candidate.id === event.messageId);
      assistantMessageId = event.messageId;
      assistantText = typeof message?.content === "string" ? message.content : `${assistantText}${event.delta}`;
      callbacks.onText?.(assistantText);
    }
    if (event.type === EventType.CUSTOM && event.name === "fyn.response.v1" && event.value && typeof event.value === "object") {
      result = conform(agentResponseSchema, (event.value as { response?: unknown }).response, "reply");
    }
    if (event.type === EventType.RUN_FINISHED) {
      finished = true;
      if (event.outcome?.type === "interrupt") {
        interrupts = event.outcome.interrupts.map((interrupt) => protocolInterrupt(interrupt, runId));
        callbacks.onPhase?.("interrupted");
      } else {
        callbacks.onPhase?.("succeeded");
      }
    }
    if (event.type === EventType.RUN_ERROR) {
      finished = true;
      runFailure.message = event.message;
      runFailure.code = event.code ?? null;
      callbacks.onPhase?.("failed");
    }
  }

  for (let attempt = 0; attempt < 2 && !finished; attempt += 1) {
    const replayUrl = `${API_URL}/api/agent/runs/${encodeURIComponent(runId)}/events${cursorState.safe ? `?after=${cursorState.safe}` : ""}`;
    let response: Awaited<ReturnType<typeof streamingFetch>>;
    try {
      response = await streamingFetch(replay ? replayUrl : `${API_URL}/api/agent`, {
        method: replay ? "GET" : "POST",
        credentials: CREDENTIALS,
        headers: authHeaders({ Accept: "text/event-stream" }),
        body: replay ? undefined : JSON.stringify(input),
        signal,
      });
    } catch (cause) {
      if ((cause as Error)?.name === "AbortError") throw cause;
      if (replay || attempt > 0) {
        discardRejectedInput();
        throw new Error(UNREACHABLE);
      }
      replay = true;
      callbacks.onPhase?.("reconnecting");
      continue;
    }
    if (!response.ok || !response.body) {
      const payload = await response.json().catch(() => null);
      discardRejectedInput();
      throw new ApiError(describe(payload, response.status), response.status);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const connection = { started: false };
    let buffer = "";
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (signal?.aborted) throw abandoned();
        buffer += decoder.decode(value, { stream: !done });
        const blocks = buffer.split(/\r?\n\r?\n/);
        buffer = blocks.pop() ?? "";
        blocks.forEach((block) => consume(block, connection));
        if (done) break;
      }
      if (buffer.trim()) consume(buffer, connection);
    } catch (cause) {
      if ((cause as Error)?.name === "AbortError") throw cause;
      if (replay || attempt > 0) {
        discardRejectedInput();
        throw cause;
      }
    } finally {
      reader.releaseLock();
      if (!finished) void response.body?.cancel().catch(() => undefined);
    }
    if (!finished && attempt === 0) {
      replay = true;
      callbacks.onPhase?.("reconnecting");
    }
  }
  if (!result && assistantText) {
    result = {
      message: assistantText,
      widgets: [],
      widgetUpdates: [],
      pendingAction: null,
      citations: [],
      conversation_id: conversationId,
      message_id: assistantMessageId || Crypto.randomUUID(),
    };
  }
  if (!result || !finished) {
    if (runFailure.code === "cancelled") throw abandoned();
    discardRejectedInput();
    throw new Error(runFailure.message ?? "The agent stream ended before returning a verified response.");
  }
  return { response: result, runId, interrupts, reasoningSummary };
}

export function sendAgentMessage(conversationId: string, text: string, callbacks?: AgentRunCallbacks, signal?: AbortSignal) {
  return runFynAgent(conversationId, { message: text }, callbacks, signal);
}

export function sendAgentAction(
  conversationId: string,
  widgetId: string,
  action: WidgetActionId,
  payload: Record<string, unknown>,
  completeWidget = true,
  interrupt?: FynInterrupt,
  callbacks?: AgentRunCallbacks,
  signal?: AbortSignal,
) {
  const validatedPayload = parseActionPayload(action, payload);
  const actionCommand = { widgetId, action, payload: validatedPayload, completeWidget };
  return runFynAgent(
    conversationId,
    interrupt
      ? { resume: [{ interruptId: interrupt.id, status: "resolved", payload: { approved: true, editedArgs: actionCommand } }] }
      : { forwardedProps: { fynAction: actionCommand } },
    callbacks,
    signal,
  );
}

export function resumeAgentInterrupt(
  conversationId: string,
  interrupt: FynInterrupt,
  response: { status: "resolved"; payload: unknown } | { status: "cancelled" },
  callbacks?: AgentRunCallbacks,
  signal?: AbortSignal,
) {
  return runFynAgent(
    conversationId,
    {
      resume: [{
        interruptId: interrupt.id,
        status: response.status,
        ...(response.status === "resolved" ? { payload: response.payload } : {}),
      }],
    },
    callbacks,
    signal,
  );
}

export function reconnectAgentRun(conversationId: string, runId: string, callbacks?: AgentRunCallbacks, signal?: AbortSignal) {
  return runFynAgent(conversationId, { runId, replay: true }, callbacks, signal);
}

export async function loadAgentThreadState(conversationId: string): Promise<AgentThreadStateOut> {
  return conform(agentThreadStateSchema, await request(`/api/agent/threads/${encodeURIComponent(conversationId)}`), "agent state");
}

export async function cancelAgentRun(runId: string): Promise<void> {
  await request(`/api/agent/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST", body: "{}" });
}

export function openInterrupts(state: AgentThreadStateOut): FynInterrupt[] {
  return state.interrupts.map(storedInterrupt);
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
