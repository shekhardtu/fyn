import { HttpAgent, type AgentSubscriber, type Interrupt } from "@ag-ui/client";
import { AgentCapabilitiesSchema, type AgentCapabilities, type Message as AgUiMessage } from "@ag-ui/core";

import { API_MOUNT_PATH } from "@/config/api-path";
import { environment } from "@/config/environment";
import { agentActivityEventSchema, agentEnrichmentSchema, agentResponseSchema, agentSettingsSchema, agentThreadStateSchema, authStatusSchema, bootstrapSchema, categoryDirectoryEntrySchema, categoryDirectorySchema, categorySubcategorySchema, contactSuggestionSchema, conversationCreatedSchema, conversationPageSchema, conversationSchema, conversationSummarySchema, dashboardDetailSchema, dashboardListSchema, documentAssetSchema, documentRevisionListSchema, importResultSchema, invitationPreviewSchema, loanCommandSchema, locationResolveSchema, otpSentSchema, overviewSchema, parseActionPayload, personalLoanDetailSchema, personalLoanListSchema, privacyStatusSchema, profileSchema, reminderSchema, transactionCategoryHintSchema, transactionListItemSchema, transactionListSchema, transactionRevisionListSchema, type AgentActivityEvent, type AgentEnrichmentOut, type AgentInterruptOut, type AgentResponse, type AgentSettingsOut, type AgentThreadStateOut, type AuthStatusOut, type Bootstrap, type CategoryDirectoryOut, type CategoryDirectorySubcategoryOut, type ContactSuggestionOut, type ConversationCreatedOut, type ConversationOut, type ConversationPage, type ConversationSummary, type CreatePersonalLoanIn, type DashboardDetail, type DashboardSummary, type DocumentAssetOut, type DocumentRevisionOut, type FulfillDocumentRequestsIn, type ImportResult, type InvitationPreviewOut, type LoanCommandOut, type LoanTermProposalIn, type OtpSentOut, type OverviewOut, type PersonalLoanDetailOut, type PersonalLoanListOut, type PrivacyStatusOut, type ProfileOut, type RecordLoanFundingIn, type RecordLoanPaymentIn, type ReminderOut, type SendLoanReminderIn, type TransactionCategoryHintOut, type TransactionListItemOut, type TransactionRevisionOut, type TransactionUpdateIn, type Widget, type WidgetActionId } from "@/lib/protocol";
import type { AgentClientTelemetryIn } from "@/lib/generated/contracts";

const API_URL = environment.apiUrl;

/** The URL to call for a service-root resource path.
 *
 * Same-origin requests need the public mount to stay separate from SPA routes.
 * A configured dedicated API origin already provides that namespace, so its
 * service resources remain at the backend root. */
export function apiUrl(resourcePath: string): string {
  return `${API_URL || API_MOUNT_PATH}${resourcePath}`;
}

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
    if (first) {
      const validation = first as { msg: string; loc?: unknown[] };
      if (validation.msg === "Field required") {
        const field = validation.loc?.filter((part) => part !== "body").at(-1);
        const label = typeof field === "string" ? field.replaceAll("_", " ") : "required information";
        return `Complete ${label} before continuing.`;
      }
      return validation.msg;
    }
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
    return await fetch(apiUrl(path), { credentials: "include", ...init, signal });
  } catch (cause) {
    // An abort raised by our own deadline is unreachability, not a cancelled
    // request; a caller's own abort is passed through so the thread can tell
    // "nobody is listening" from "this failed".
    if (cause instanceof DOMException && cause.name === "TimeoutError") throw new Error(UNREACHABLE, { cause });
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new Error(UNREACHABLE, { cause });
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
    throw new Error(`fyn AI sent back a ${what} this version of the app can’t read. Reload the page to pick up the latest version, then try again.`, { cause });
  }
}

export async function bootstrap(): Promise<Bootstrap> {
  const result = conform(bootstrapSchema, await request("/bootstrap"), "workspace");
  hydrateFynAgent(result.active_conversation.id, result.active_conversation.messages);
  return result;
}

export async function loadOverview(month?: string): Promise<OverviewOut> {
  const query = month ? `?month=${encodeURIComponent(`${month}-01`)}` : "";
  const payload = await request(`/overview${query}`);
  const compatiblePayload = payload && typeof payload === "object" && !Array.isArray(payload) && !("budgets" in payload)
    ? { ...payload, budgets: [] }
    : payload;
  return conform(overviewSchema, compatiblePayload, "overview");
}

/* ── Personal lending ──────────────────────────────────────────────────────
 * Lending is a shared aggregate, not a loose collection of transactions. The
 * API owns every transition atomically and the browser supplies a fresh key so
 * retrying a tap can never create a second loan, payment, or reminder. */

function lendingMutation(path: string, method: "POST", payload: unknown, idempotencyKey: string = crypto.randomUUID()) {
  return request(path, {
    method,
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(payload),
  });
}

export async function loadPersonalLoans(): Promise<PersonalLoanListOut> {
  return conform(personalLoanListSchema, await request("/loan-agreements"), "personal loan list");
}

export async function searchContacts(channel: "email" | "phone", query: string, signal?: AbortSignal): Promise<ContactSuggestionOut[]> {
  const path = `/contacts?channel=${encodeURIComponent(channel)}&q=${encodeURIComponent(query)}`;
  return conform(contactSuggestionSchema.array(), await request(path, { signal }), "contact suggestions");
}

export async function loadPersonalLoan(id: string): Promise<PersonalLoanDetailOut> {
  return conform(personalLoanDetailSchema, await request(`/loan-agreements/${encodeURIComponent(id)}`), "personal loan");
}

export async function createPersonalLoan(payload: CreatePersonalLoanIn, idempotencyKey?: string): Promise<LoanCommandOut> {
  return conform(loanCommandSchema, await lendingMutation("/loan-agreements", "POST", payload, idempotencyKey), "created personal loan");
}

export async function uploadDocumentAsset(file: File, classification: string, description?: string): Promise<DocumentAssetOut> {
  const form = new FormData();
  form.append("file", file);
  form.append("classification", classification);
  if (description?.trim()) form.append("description", description.trim());
  const response = await send("/document-assets", { method: "POST", body: form });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new ApiError(describe(payload, response.status), response.status);
  }
  return conform(documentAssetSchema, await response.json(), "supporting document");
}

export async function loadDocumentAssets(): Promise<DocumentAssetOut[]> {
  return conform(documentAssetSchema.array(), await request("/document-assets"), "private document repository");
}

export async function deleteDocumentAsset(assetId: string): Promise<void> {
  await request(`/document-assets/${encodeURIComponent(assetId)}`, { method: "DELETE" });
}

export function documentAssetDownloadUrl(assetId: string): string {
  return apiUrl(`/document-assets/${encodeURIComponent(assetId)}/download`);
}

export function loanAgreementPdfUrl(loanId: string): string {
  return apiUrl(`/loan-agreements/${encodeURIComponent(loanId)}/agreement.pdf`);
}

export function loanEvidenceBundleUrl(loanId: string): string {
  return apiUrl(`/loan-agreements/${encodeURIComponent(loanId)}/evidence-bundle`);
}

export async function loadLoanInvitation(token: string): Promise<InvitationPreviewOut> {
  return conform(invitationPreviewSchema, await request(`/loan-invitations/${encodeURIComponent(token)}`), "loan invitation");
}

export async function redeemLoanInvitation(token: string): Promise<LoanCommandOut> {
  return conform(loanCommandSchema, await request(`/loan-invitations/${encodeURIComponent(token)}/redeem`, {
    method: "POST",
    body: "{}",
  }), "redeemed loan invitation");
}

export async function acceptPersonalLoan(id: string, expectedRowVersion: number, idempotencyKey?: string): Promise<LoanCommandOut> {
  return conform(loanCommandSchema, await lendingMutation(`/loan-agreements/${encodeURIComponent(id)}/accept`, "POST", { expectedRowVersion }, idempotencyKey), "accepted personal loan");
}

export async function fulfillPersonalLoanDocumentRequests(id: string, payload: FulfillDocumentRequestsIn, idempotencyKey?: string): Promise<LoanCommandOut> {
  return conform(loanCommandSchema, await lendingMutation(`/loan-agreements/${encodeURIComponent(id)}/document-requests/fulfill`, "POST", payload, idempotencyKey), "provided loan documents");
}

export async function proposePersonalLoanTerms(id: string, payload: LoanTermProposalIn, idempotencyKey?: string): Promise<LoanCommandOut> {
  return conform(loanCommandSchema, await lendingMutation(`/loan-agreements/${encodeURIComponent(id)}/term-proposals`, "POST", payload, idempotencyKey), "updated repayment plan");
}

export async function recordPersonalLoanPayment(id: string, payload: RecordLoanPaymentIn, idempotencyKey?: string): Promise<LoanCommandOut> {
  return conform(loanCommandSchema, await lendingMutation(`/loan-agreements/${encodeURIComponent(id)}/payments`, "POST", payload, idempotencyKey), "recorded loan payment");
}

export async function recordPersonalLoanFunding(id: string, payload: RecordLoanFundingIn, idempotencyKey?: string): Promise<LoanCommandOut> {
  return conform(loanCommandSchema, await lendingMutation(`/loan-agreements/${encodeURIComponent(id)}/funding`, "POST", payload, idempotencyKey), "recorded loan funding");
}

export async function confirmPersonalLoanPayment(cashflowId: string, expectedRowVersion: number, idempotencyKey?: string): Promise<LoanCommandOut> {
  return conform(loanCommandSchema, await lendingMutation(`/loan-cashflows/${encodeURIComponent(cashflowId)}/confirm`, "POST", { expectedRowVersion }, idempotencyKey), "confirmed loan payment");
}

export async function sendPersonalLoanReminder(id: string, payload: SendLoanReminderIn, idempotencyKey?: string): Promise<ReminderOut> {
  return conform(reminderSchema, await lendingMutation(`/loan-agreements/${encodeURIComponent(id)}/reminders`, "POST", payload, idempotencyKey), "loan reminder");
}

export async function closePersonalLoan(id: string, idempotencyKey?: string): Promise<LoanCommandOut> {
  return conform(loanCommandSchema, await lendingMutation(`/loan-agreements/${encodeURIComponent(id)}/close`, "POST", {}, idempotencyKey), "closed personal loan");
}

export async function loadSharedDocumentRevisions(documentId: string): Promise<DocumentRevisionOut[]> {
  return conform(documentRevisionListSchema, await request(`/shared-documents/${encodeURIComponent(documentId)}/revisions`), "document revision history");
}

/* ── Dashboards ─────────────────────────────────────────────────────────────
 * Saved analysis charts, re-executed on every read so a tile is always live
 * data — the page never caches a stale plot beyond react-query's own window. */

export async function listDashboards(): Promise<DashboardSummary[]> {
  return conform(dashboardListSchema, await request("/dashboards"), "dashboard list").dashboards;
}

export async function loadDashboard(id: string): Promise<DashboardDetail> {
  return conform(dashboardDetailSchema, await request(`/dashboards/${encodeURIComponent(id)}`), "dashboard");
}

export async function deleteDashboardTile(dashboardId: string, tileId: string): Promise<void> {
  await request(`/dashboards/${encodeURIComponent(dashboardId)}/tiles/${encodeURIComponent(tileId)}`, { method: "DELETE" });
}

export async function loadCategories(): Promise<CategoryDirectoryOut[]> {
  return conform(categoryDirectorySchema, await request("/categories"), "category directory");
}

/** Naming an existing category returns that entry — the server owns dedup. */
export async function createCategory(name: string): Promise<CategoryDirectoryOut> {
  return conform(categoryDirectoryEntrySchema, await request("/categories", {
    method: "POST",
    body: JSON.stringify({ name }),
  }), "created category");
}

export async function createSubcategory(categoryId: string, name: string): Promise<CategoryDirectorySubcategoryOut> {
  return conform(categorySubcategorySchema, await request(`/categories/${encodeURIComponent(categoryId)}/subcategories`, {
    method: "POST",
    body: JSON.stringify({ name }),
  }), "created subcategory");
}

export async function renameCategory(categoryId: string, name: string): Promise<CategoryDirectoryOut> {
  return conform(categoryDirectoryEntrySchema, await request(`/categories/${encodeURIComponent(categoryId)}`, {
    method: "PATCH", body: JSON.stringify({ name }),
  }), "updated category");
}

export async function deleteCategory(categoryId: string): Promise<void> {
  await request(`/categories/${encodeURIComponent(categoryId)}`, { method: "DELETE" });
}

export async function renameSubcategory(categoryId: string, subcategoryId: string, name: string): Promise<CategoryDirectorySubcategoryOut> {
  return conform(categorySubcategorySchema, await request(`/categories/${encodeURIComponent(categoryId)}/subcategories/${encodeURIComponent(subcategoryId)}`, {
    method: "PATCH", body: JSON.stringify({ name }),
  }), "updated subcategory");
}

export async function deleteSubcategory(categoryId: string, subcategoryId: string): Promise<void> {
  await request(`/categories/${encodeURIComponent(categoryId)}/subcategories/${encodeURIComponent(subcategoryId)}`, { method: "DELETE" });
}

export async function createTransactionHint(categoryId: string, merchant: string, subcategoryId: string | null): Promise<TransactionCategoryHintOut> {
  return conform(transactionCategoryHintSchema, await request(`/categories/${encodeURIComponent(categoryId)}/hints`, {
    method: "POST", body: JSON.stringify({ merchant, subcategoryId }),
  }), "created transaction hint");
}

export async function updateTransactionHint(categoryId: string, hintId: string, merchant: string, subcategoryId: string | null): Promise<TransactionCategoryHintOut> {
  return conform(transactionCategoryHintSchema, await request(`/categories/${encodeURIComponent(categoryId)}/hints/${encodeURIComponent(hintId)}`, {
    method: "PATCH", body: JSON.stringify({ merchant, subcategoryId }),
  }), "updated transaction hint");
}

export async function deleteTransactionHint(categoryId: string, hintId: string): Promise<void> {
  await request(`/categories/${encodeURIComponent(categoryId)}/hints/${encodeURIComponent(hintId)}`, { method: "DELETE" });
}

export type TransactionPageInput = {
  limit?: number;
  offset?: number;
  search?: string;
  transactionType?: TransactionListItemOut["transactionType"] | null;
  includeRemoved?: boolean;
};

export async function loadTransactions({ limit = 50, offset = 0, search = "", transactionType = null, includeRemoved = true }: TransactionPageInput = {}): Promise<TransactionListItemOut[]> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (search.trim()) query.set("q", search.trim());
  if (transactionType) query.set("transaction_type", transactionType);
  if (!includeRemoved) query.set("include_removed", "false");
  return conform(transactionListSchema, await request(`/transactions?${query}`), "transaction list");
}

/** Soft-deletes one transaction: it stays in the log struck through, leaves
 *  every total, and can be restored. */
export async function removeTransaction(id: string): Promise<TransactionListItemOut> {
  return conform(transactionListItemSchema, await request(`/transactions/${encodeURIComponent(id)}`, { method: "DELETE" }), "removed transaction");
}

/** Clears a removal's tombstone; the record rejoins every total at once. */
export async function restoreTransaction(id: string): Promise<TransactionListItemOut> {
  return conform(transactionListItemSchema, await request(`/transactions/${encodeURIComponent(id)}/restore`, { method: "POST" }), "restored transaction");
}

export async function createTransactionRecord(payload: TransactionUpdateIn): Promise<TransactionListItemOut> {
  return conform(transactionListItemSchema, await request("/transactions", {
    method: "POST",
    body: JSON.stringify(payload),
  }), "created transaction");
}

export async function resolveLocationLabel(latitude: number, longitude: number): Promise<string | null> {
  return conform(locationResolveSchema, await request("/locations/resolve", {
    method: "POST",
    body: JSON.stringify({ latitude, longitude }),
  }), "resolved location").location;
}

export async function updateTransaction(id: string, payload: TransactionUpdateIn): Promise<TransactionListItemOut> {
  return conform(transactionListItemSchema, await request(`/transactions/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  }), "updated transaction");
}

export async function loadTransactionRevisions(id: string): Promise<TransactionRevisionOut[]> {
  return conform(transactionRevisionListSchema, await request(`/transactions/${encodeURIComponent(id)}/revisions`), "transaction history");
}

export async function loadConversation(id: string): Promise<ConversationOut> {
  const result = conform(conversationSchema, await request(`/conversations/${encodeURIComponent(id)}`), "conversation");
  hydrateFynAgent(result.id, result.messages);
  return result;
}

export async function createConversation(): Promise<ConversationCreatedOut> {
  return conform(conversationCreatedSchema, await request("/conversations", { method: "POST", body: "{}" }), "new conversation");
}

/** One page of history for the rail. Pass the previous page's `nextCursor` to
 *  continue; a null cursor means there is nothing older to load. */
export async function listConversations(cursor?: string | null): Promise<ConversationPage> {
  return conform(conversationPageSchema, await request(`/conversations${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`), "conversation list");
}

/** Erases the thread and everything recorded against it. Transactions it
 *  captured are financial history and are deliberately left alone. */
export async function renameConversation(id: string, title: string): Promise<ConversationSummary> {
  return conform(conversationSummarySchema, await request(`/conversations/${encodeURIComponent(id)}`, {
    method: "PATCH", body: JSON.stringify({ title }),
  }), "renamed conversation");
}

export async function deleteConversation(id: string): Promise<void> {
  await request(`/conversations/${id}`, { method: "DELETE" });
}

/** Fire-and-forget version of the same delete, for the moment the page is going
 *  away with an undo window still open: `keepalive` lets the request outlive the
 *  document, so closing the tab doesn't quietly un-press the delete. */
export function flushConversationDeletion(id: string): void {
  void fetch(apiUrl(`/conversations/${id}`), { method: "DELETE", keepalive: true }).catch(() => undefined);
}

/** The same keepalive escape hatch for taxonomy deletes still inside their
 *  undo window when the page goes away. */
export function flushCategoryDeletion(categoryId: string): void {
  void fetch(apiUrl(`/categories/${encodeURIComponent(categoryId)}`), { method: "DELETE", keepalive: true }).catch(() => undefined);
}

export function flushSubcategoryDeletion(categoryId: string, subcategoryId: string): void {
  void fetch(apiUrl(`/categories/${encodeURIComponent(categoryId)}/subcategories/${encodeURIComponent(subcategoryId)}`), { method: "DELETE", keepalive: true }).catch(() => undefined);
}

export function flushHintDeletion(categoryId: string, hintId: string): void {
  void fetch(apiUrl(`/categories/${encodeURIComponent(categoryId)}/hints/${encodeURIComponent(hintId)}`), { method: "DELETE", keepalive: true }).catch(() => undefined);
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


// This budget is detached from the agent mutation: it never keeps the answer,
// composer, or run open. It only lets the mounted thread adopt optional
// suggestions when an independent provider call has a cold start.
const RELATED_QUESTION_POLL_DELAYS_MS = [
  0, 250, 500, 1_000, 2_000, 3_000, 5_000, 5_000, 5_000, 5_000, 5_000,
] as const;

function abortableDelay(milliseconds: number, signal?: AbortSignal): Promise<void> {
  if (!milliseconds) return Promise.resolve();
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("The enrichment request was detached.", "AbortError"));
      return;
    }
    const timeout = window.setTimeout(resolve, milliseconds);
    signal?.addEventListener("abort", () => {
      window.clearTimeout(timeout);
      reject(new DOMException("The enrichment request was detached.", "AbortError"));
    }, { once: true });
  });
}

/** Poll optional post-answer work. Callers deliberately never await this from
 * the agent mutation; a missing widget must have no effect on the answer. */
export async function waitForAgentRelatedQuestions(runId: string, signal?: AbortSignal): Promise<{ messageId: string; widget: Widget } | null> {
  for (const delay of RELATED_QUESTION_POLL_DELAYS_MS) {
    await abortableDelay(delay, signal);
    const response = await fetch(apiUrl(`/agent/runs/${encodeURIComponent(runId)}/related-questions`), {
      credentials: "include",
      signal,
    });
    if (response.status === 404) return null;
    if (!response.ok) throw new ApiError(describe(await response.json().catch(() => null), response.status), response.status);
    const enrichment: AgentEnrichmentOut = conform(agentEnrichmentSchema, await response.json(), "answer enrichment");
    if (enrichment.status === "completed" && enrichment.widget) {
      return { messageId: enrichment.messageId, widget: enrichment.widget };
    }
    if (enrichment.status === "failed" || enrichment.status === "skipped") return null;
  }
  return null;
}

interface RunCommand {
  message?: string;
  forwardedProps?: Record<string, unknown>;
  resume?: Array<{ interruptId: string; status: "resolved" | "cancelled"; payload?: unknown }>;
  runId?: string;
  replay?: boolean;
}

function linkedAbortController(signal?: AbortSignal) {
  const controller = new AbortController();
  if (!signal) return controller;
  if (signal.aborted) controller.abort(signal.reason);
  else signal.addEventListener("abort", () => controller.abort(signal.reason), { once: true });
  return controller;
}

class FynHttpAgent extends HttpAgent {
  replayMode = false;
  private capabilitiesRequest?: Promise<AgentCapabilities>;
  private runCursors = new Map<string, { safe: number; seen: Set<number> }>();

  protected override requestInit(input: Parameters<HttpAgent["run"]>[0]): RequestInit {
    if (!this.replayMode) return super.requestInit(input);
    return {
      method: "GET",
      headers: { Accept: "text/event-stream" },
      signal: this.abortController.signal,
    };
  }

  override async getCapabilities(): Promise<AgentCapabilities> {
    this.capabilitiesRequest ??= fetch(apiUrl(`/agent/capabilities`), { credentials: "include" })
      .then(async (response) => {
        if (!response.ok) throw new ApiError(describe(await response.json().catch(() => null), response.status), response.status);
        return conform(AgentCapabilitiesSchema, await response.json(), "agent capability declaration");
      })
      .catch((error) => {
        this.capabilitiesRequest = undefined;
        throw error;
      });
    return this.capabilitiesRequest;
  }

  cursorFor(runId: string): { safe: number; seen: Set<number> } {
    const existing = this.runCursors.get(runId);
    if (existing) return existing;
    const cursor = { safe: 0, seen: new Set<number>() };
    this.runCursors.set(runId, cursor);
    if (this.runCursors.size > 20) this.runCursors.delete(this.runCursors.keys().next().value as string);
    return cursor;
  }
}

const fynAgents = new Map<string, FynHttpAgent>();

function fynAgent(threadId: string): FynHttpAgent {
  const existing = fynAgents.get(threadId);
  if (existing) return existing;
  const agent = new FynHttpAgent({
    url: apiUrl(`/agent`),
    threadId,
    fetch: (url, init) => fetch(url, { ...init, credentials: "include" }),
  });
  // Warm capability discovery as soon as the transcript creates its agent.
  // This request is advisory for ordinary runs and is never joined to them.
  void agent.getCapabilities().catch(() => undefined);
  fynAgents.set(threadId, agent);
  return agent;
}

function hydrateFynAgent(threadId: string, messages: ConversationOut["messages"]): void {
  const agent = fynAgent(threadId);
  if (agent.isRunning) return;
  const hydrated = messages
    .filter((message) => message.role === "user" || message.role === "assistant")
    .map((message) => ({ id: message.id, role: message.role, content: message.content }) as AgUiMessage);
  const localOnly = agent.messages.filter((message) => message.role === "activity" || message.role === "reasoning");
  agent.setMessages([...hydrated, ...localOnly]);
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

/** Deliver the first visible value immediately, then collapse burst updates
 * to the newest value once per animation frame. ``flush`` preserves the final
 * provider value when a short stream finishes before the queued frame paints. */
function frameLatestDispatcher<T>(deliver: (value: T) => void) {
  let frame: number | null = null;
  let latest!: T;
  let hasLatest = false;
  let deliveredFirst = false;
  const schedule = (callback: FrameRequestCallback) => window.requestAnimationFrame(callback);
  const cancelFrame = (handle: number) => window.cancelAnimationFrame(handle);
  const drain = () => {
    frame = null;
    if (!hasLatest) return;
    const value = latest;
    hasLatest = false;
    deliver(value);
  };
  return {
    push(value: T) {
      if (!deliveredFirst) {
        deliveredFirst = true;
        deliver(value);
        return;
      }
      latest = value;
      hasLatest = true;
      frame ??= schedule(drain);
    },
    flush() {
      if (frame !== null) cancelFrame(frame);
      drain();
    },
    cancel() {
      if (frame !== null) cancelFrame(frame);
      frame = null;
      hasLatest = false;
    },
  };
}

async function runFynAgent(
  conversationId: string,
  command: RunCommand,
  callbacks: AgentRunCallbacks = {},
  signal?: AbortSignal,
): Promise<FynAgentRunResult> {
  const runId = command.runId ?? crypto.randomUUID();
  const controller = linkedAbortController(signal);
  let response: AgentResponse | null = null;
  let interrupts: FynInterrupt[] = [];
  let reasoningSummary = "";
  let assistantText = "";
  let assistantMessageId = "";
  const textUpdates = frameLatestDispatcher((text: string) => callbacks.onText?.(text));
  const runFailure: { message: string | null; code: string | null } = { message: null, code: null };
  callbacks.onRunCreated?.(runId);
  callbacks.onPhase?.(command.replay ? "reconnecting" : "connecting");

  const agent = fynAgent(conversationId);
  const cursorState = agent.cursorFor(runId);
  if (command.resume) {
    // Resume changes durable state through an existing server-authored
    // interrupt, so capability confirmation remains a hard precondition.
    const advertised = await agent.getCapabilities();
    if (!advertised.transport?.streaming) throw new Error("fyn AI does not currently advertise streaming AG-UI support.");
    if (!advertised.humanInTheLoop?.interrupts) {
      throw new Error("fyn AI does not currently advertise interrupt resumption.");
    }
  }
  let inputMessageId: string | null = null;
  if (command.message) {
    inputMessageId = crypto.randomUUID();
    agent.addMessage({ id: inputMessageId, role: "user", content: command.message });
  }
  const discardRejectedInput = () => {
    if (inputMessageId) agent.setMessages(agent.messages.filter((message) => message.id !== inputMessageId));
  };
  let replay = Boolean(command.replay);
  const subscriber: AgentSubscriber = {
    onEvent: ({ event }) => {
      const cursor = eventCursor(event);
      if (!cursor) return;
      if (cursorState.seen.has(cursor.sequence)) return { stopPropagation: true };
      cursorState.seen.add(cursor.sequence);
      if (cursor.replaySafe) cursorState.safe = Math.max(cursorState.safe, cursor.sequence);
    },
    onRunStartedEvent: () => callbacks.onPhase?.(replay ? "reconnecting" : "running"),
    onActivitySnapshotEvent: ({ event }) => {
      if (event.activityType !== "fyn.agent_activity.v1") return;
      callbacks.onActivity?.(conform(agentActivityEventSchema, event.content, "progress update"));
    },
    onReasoningMessageContentEvent: ({ event, reasoningMessageBuffer }) => {
      reasoningSummary = `${reasoningMessageBuffer}${event.delta}`;
      callbacks.onReasoning?.(reasoningSummary);
    },
    onTextMessageContentEvent: ({ event, textMessageBuffer }) => {
      assistantMessageId = event.messageId;
      assistantText = `${textMessageBuffer}${event.delta}`;
      textUpdates.push(assistantText);
    },
    onCustomEvent: ({ event }) => {
      if (event.name !== "fyn.response.v1" || !event.value || typeof event.value !== "object") return;
      response = conform(agentResponseSchema, (event.value as { response?: unknown }).response, "reply");
    },
    onRunFinishedEvent: (finished) => {
      if (finished.outcome === "interrupt") {
        interrupts = finished.interrupts.map((interrupt) => protocolInterrupt(interrupt, runId));
        callbacks.onPhase?.("interrupted");
      } else {
        callbacks.onPhase?.("succeeded");
      }
    },
    onRunErrorEvent: ({ event }) => {
      runFailure.message = event.message;
      runFailure.code = event.code ?? null;
      callbacks.onPhase?.("failed");
    },
  };

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const replayUrl = apiUrl(`/agent/runs/${encodeURIComponent(runId)}/events${cursorState.safe ? `?after=${cursorState.safe}` : ""}`);
    agent.replayMode = replay;
    agent.url = replay ? replayUrl : apiUrl(`/agent`);
    try {
      await agent.runAgent({
        runId,
        forwardedProps: command.forwardedProps ?? {},
        ...(command.resume ? { resume: command.resume } : {}),
        abortController: controller,
      }, subscriber);
      break;
    } catch (cause) {
      if (controller.signal.aborted) {
        textUpdates.cancel();
        throw new DOMException("The agent run was detached.", "AbortError");
      }
      const status = typeof cause === "object" && cause && "status" in cause ? Number((cause as { status: unknown }).status) : 0;
      if (status || replay || attempt > 0) {
        textUpdates.cancel();
        discardRejectedInput();
        if (status) throw new ApiError(describe(typeof cause === "object" && cause && "payload" in cause ? (cause as { payload: unknown }).payload : null, status), status);
        throw cause;
      }
      replay = true;
      callbacks.onPhase?.("reconnecting");
    }
  }
  if (controller.signal.aborted) {
    textUpdates.cancel();
    throw new DOMException("The agent run was detached.", "AbortError");
  }
  // Provider text can cross the wire before the server validates and commits
  // the canonical response. A RUN_ERROR always wins over that provisional
  // text; otherwise a failed resume looks successful while its interrupt
  // remains open and the next message is rejected.
  if (runFailure.message || runFailure.code) {
    textUpdates.cancel();
    discardRejectedInput();
    if (runFailure.code === "cancelled") throw new DOMException("The agent run was stopped.", "AbortError");
    throw new Error(runFailure.message ?? "The agent run failed before its response was verified.");
  }
  textUpdates.flush();
  if (!response && assistantText) {
    response = {
      message: assistantText,
      widgets: [],
      widgetUpdates: [],
      pendingAction: null,
      citations: [],
      conversation_id: conversationId,
      message_id: assistantMessageId || crypto.randomUUID(),
      // Synthesised client-side, so the persisted identity of the question is
      // unknown here; the bubble keeps its provisional ID until a reload.
      user_message_id: null,
      delivered_at: new Date().toISOString(),
    };
  }
  if (!response) {
    textUpdates.cancel();
    discardRejectedInput();
    throw new Error("The agent stream ended before returning a verified response.");
  }
  return { response, runId, interrupts, reasoningSummary };
}

export function sendAgentMessage(
  conversationId: string,
  text: string,
  callbacks?: AgentRunCallbacks,
  signal?: AbortSignal,
) {
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

export function reconnectAgentRun(
  conversationId: string,
  runId: string,
  callbacks?: AgentRunCallbacks,
  signal?: AbortSignal,
) {
  return runFynAgent(conversationId, { runId, replay: true }, callbacks, signal);
}

export async function loadAgentThreadState(conversationId: string): Promise<AgentThreadStateOut> {
  return conform(agentThreadStateSchema, await request(`/agent/threads/${encodeURIComponent(conversationId)}`), "agent state");
}


export async function cancelAgentRun(runId: string): Promise<void> {
  await request(`/agent/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST", body: "{}" });
}

/** Queue optional browser timings after the interaction is already usable.
 *
 * Nothing in the run awaits this request. Serialization and dispatch happen in
 * an idle task, and every failure is contained inside that detached task.
 */
export function reportAgentClientTelemetry(runId: string, telemetry: AgentClientTelemetryIn): void {
  const transmit = () => {
    try {
      void fetch(apiUrl(`/agent/runs/${encodeURIComponent(runId)}/telemetry`), {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(telemetry),
        keepalive: true,
      }).catch(() => undefined);
    } catch {
      // Observability is optional and can never fail the customer interaction.
    }
  };
  try {
    const idle = typeof window === "undefined"
      ? undefined
      : (window as Window & { requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number }).requestIdleCallback;
    if (idle) {
      idle.call(window, transmit, { timeout: 2_000 });
    } else if (typeof window !== "undefined") {
      globalThis.setTimeout(transmit, 0);
    }
  } catch {
    // A browser without either scheduling primitive simply drops the sample.
  }
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

/** XHR rather than fetch: a statement can be megabytes, and only XHR reports
 *  upload progress, so the composer can show something real while it climbs. */
export function uploadCsv(conversationId: string, file: File, onProgress?: (percent: number) => void, signal?: AbortSignal): Promise<ImportResult> {
  const body = new FormData();
  body.set("conversation_id", conversationId);
  body.set("file", file);
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", apiUrl(`/imports/csv`));
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
      let payload: unknown;
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

export type AnswerValidationMode = AgentSettingsOut["answerValidationMode"];
export type AnswerStyle = AgentSettingsOut["answerStyle"];

export async function getAgentSettings(): Promise<AgentSettingsOut> {
  return conform(agentSettingsSchema, await request("/agent-settings"), "agent setting");
}

export async function setAnswerValidationMode(answerValidationMode: AnswerValidationMode): Promise<AgentSettingsOut> {
  return conform(agentSettingsSchema, await request("/agent-settings", {
    method: "PATCH",
    body: JSON.stringify({ answerValidationMode }),
  }), "updated agent setting");
}

export async function setAnswerStyle(answerStyle: AnswerStyle): Promise<AgentSettingsOut> {
  return conform(agentSettingsSchema, await request("/agent-settings", {
    method: "PATCH",
    body: JSON.stringify({ answerStyle }),
  }), "updated agent setting");
}

export async function getPrivacyStatus(): Promise<PrivacyStatus> {
  return conform(privacyStatusSchema, await request("/privacy"), "privacy setting");
}

export async function setLocationEnabled(enabled: boolean): Promise<void> {
  await request("/privacy/location", { method: "PATCH", body: JSON.stringify({ enabled }) });
}

export async function revokeSource(sourceType: string): Promise<void> {
  await request(`/privacy/sources/${sourceType}/revoke`, { method: "POST", body: "{}" });
}

/** Returns the filename so the drawer can confirm what was saved. */
export async function downloadDataExport(): Promise<string> {
  const response = await send("/privacy/export");
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
  await request("/privacy/data", { method: "DELETE", body: JSON.stringify({ confirmation: "DELETE MY DATA" }) });
}

/* ── Signing in ─────────────────────────────────────────────────────────────
 * The session lives in an httpOnly cookie, so there is no token to store, read
 * or refresh here. "Am I signed in?" is a question only the server can answer,
 * and `/auth/session` answers it 200 either way rather than by throwing. */

export type AuthStatus = AuthStatusOut;
export type Profile = ProfileOut;
export type OtpSent = OtpSentOut;
export type OtpChannel = "phone" | "email";

export async function getAuthStatus(): Promise<AuthStatus> {
  return conform(authStatusSchema, await request("/auth/session"), "sign-in status");
}

/** Sends a sign-in code. Reveals nothing about whether an account exists. */
export async function startSignInCode(channel: OtpChannel, value: string): Promise<OtpSent> {
  return conform(otpSentSchema, await request("/auth/otp/start", {
    method: "POST", body: JSON.stringify({ channel, value }),
  }), "verification code");
}

export async function verifySignInCode(challengeId: string, code: string): Promise<AuthStatus> {
  return conform(authStatusSchema, await request("/auth/otp/verify", {
    method: "POST", body: JSON.stringify({ challengeId, code }),
  }), "sign-in status");
}

/** Exchanges the Google ID token for a session. The token is verified against
 *  Google's keys on the server; nothing here trusts what it contains. */
export async function signInWithGoogle(credential: string): Promise<AuthStatus> {
  return conform(authStatusSchema, await request("/auth/google", {
    method: "POST", body: JSON.stringify({ credential }),
  }), "sign-in status");
}

export async function signOut(): Promise<void> {
  await request("/auth/signout", { method: "POST", body: "{}" });
}

/* ── Profile ─────────────────────────────────────────────────────────────── */

export async function getProfile(): Promise<Profile> {
  return conform(profileSchema, await request("/profile"), "profile");
}

export async function updateProfile(changes: { displayName: string; currency?: string; timezone?: string }): Promise<Profile> {
  return conform(profileSchema, await request("/profile", {
    method: "PATCH", body: JSON.stringify(changes),
  }), "profile");
}

/** Sends a code to a number or address this account wants to claim. Throws a
 *  409 before sending when it belongs to somebody else. */
export async function startLinkCode(channel: OtpChannel, value: string): Promise<OtpSent> {
  return conform(otpSentSchema, await request("/profile/identities/otp/start", {
    method: "POST", body: JSON.stringify({ channel, value }),
  }), "verification code");
}

export async function verifyLinkCode(challengeId: string, code: string): Promise<Profile> {
  return conform(profileSchema, await request("/profile/identities/otp/verify", {
    method: "POST", body: JSON.stringify({ challengeId, code }),
  }), "profile");
}

export async function removeIdentity(identityId: string): Promise<Profile> {
  return conform(profileSchema, await request(`/profile/identities/${identityId}`, { method: "DELETE" }), "profile");
}
