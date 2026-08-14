import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Activity, ArrowDown, Check, CheckCircle2, Copy, Download, FileText, LayoutDashboard, Loader2, MapPin, MessageSquareText, Paperclip, ReceiptText, RotateCcw, SendHorizontal, Settings, ShieldCheck, Sparkles, Square, SquarePen, Tags, Trash2, TriangleAlert, X } from "lucide-react";
import { createContext, FormEvent, memo, RefObject, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";
import { useLocation, useMatch, useNavigate } from "react-router";
import { Button } from "@/components/ui/button";
import { DocumentTitle } from "@/components/document-title";
import { SiteHeader, useAutoHideSiteHeader } from "@/components/ui/site-header";
import { Textarea } from "@/components/ui/textarea";
import { Toast, ToastAction, ToastContent, ToastDescription, ToastPortal, ToastProvider, ToastTitle, ToastViewport, toast, useToastManager } from "@/components/ui/toast";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { WidgetRenderer } from "@/components/widget-renderer";
import { MarkdownMessage } from "@/components/widget-library/markdown-message";
import { environment } from "@/config/environment";
import { bootstrap, cancelAgentRun, createConversation, deleteAllData, deleteConversation, downloadDataExport, flushConversationDeletion, getPrivacyStatus, isUnauthorized, listConversations, loadAgentThreadMetrics, loadAgentThreadState, loadConversation, openInterrupts, reconnectAgentRun, resumeAgentInterrupt, revokeSource, sendAgentAction, sendAgentMessage, setLocationEnabled, uploadCsv, type AgentActivity, type AgentRunPhase, type FynInterrupt } from "@/lib/api";
import { formatBytes, formatMoney, readComposerEntry } from "@/lib/format";
import { widgetTypeIds, type AgentResponse, type AgentThreadMetricsOut, type Bootstrap, type ConversationSummary, type Message, type Widget, type WidgetActionId } from "@/lib/protocol";
import { cn } from "@/lib/utils";
import { contractLimits } from "@/lib/generated/contracts";
import { activeWidgetId, applyWidgetUpdates, isLegacyAnalysisLifecycleWidget } from "@/lib/widget-state";
import { appPaths, appRoutePatterns } from "@/routing/paths";

const MAX_UPLOAD_BYTES = contractLimits.csvUploadBytes;
const JUMP_TO_LATEST_VIEWPORT_RATIO = 0.9;
const SCROLL_SETTLE_MS = 150;

type Retry =
  | { kind: "chat"; text: string }
  | { kind: "action"; widgetId: string; action: WidgetActionId; payload: Record<string, unknown>; markUsed: boolean }
  | { kind: "upload"; file: File }
  | null;

/** Rejects what the importer can't read before spending a round trip on it. */
function csvProblem(file: File) {
  if (!/\.csv$/i.test(file.name) && file.type !== "text/csv") return `${file.name} isn’t a CSV file. Export the statement as CSV and attach it again.`;
  if (file.size === 0) return `${file.name} is empty. Attach a statement that has rows in it.`;
  if (file.size > MAX_UPLOAD_BYTES) return `${file.name} is ${formatBytes(file.size)}. Attach a statement under ${formatBytes(MAX_UPLOAD_BYTES)}.`;
  return null;
}

function prefersReducedMotion() {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function formatLatency(value: number | null) {
  if (value === null) return "—";
  if (value < 1_000) return `${Math.round(value)} ms`;
  return `${(value / 1_000).toFixed(value < 10_000 ? 1 : 0)} s`;
}

function formatRate(value: number | null) {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

function AgentMetricsPanel({ metrics, loading, failed }: {
  metrics?: AgentThreadMetricsOut;
  loading: boolean;
  failed: boolean;
}) {
  if (loading) return <div className="p-5 text-note text-ink-muted">Loading run measurements…</div>;
  if (failed || !metrics) return <div className="p-5 text-note text-danger-ink">Run measurements could not be loaded.</div>;
  const cards = [
    ["First response · p50", formatLatency(metrics.p50TimeToFirstResponseMs)],
    ["Total time · average", formatLatency(metrics.averageDurationMs)],
    ["Evidence integrity", formatRate(metrics.evidencePassRate)],
    ["Quality heuristic", metrics.averageQualityScore === null ? "—" : `${Math.round(metrics.averageQualityScore)}/100`],
    ["Grounded when needed", formatRate(metrics.groundingRate)],
    ["Depth heuristic", metrics.averageDepthScore === null ? "—" : `${Math.round(metrics.averageDepthScore)}/100`],
    ["Contextual responses", formatRate(metrics.contextualityRate)],
    ["Total time · p95", formatLatency(metrics.p95DurationMs)],
  ];
  return <div>
    <div className="grid grid-cols-2 gap-px bg-line">
      {cards.map(([label, value]) => <div key={label} className="bg-surface px-4 py-3">
        <div className="text-[11px] leading-4 text-ink-muted">{label}</div>
        <div className="mt-1 text-control font-semibold text-ink-body">{value}</div>
      </div>)}
    </div>
    <div className="border-t border-line px-4 py-3">
      <div className="flex items-center justify-between text-meta text-ink-muted"><span>Recent runs</span><span>{metrics.sampleSize} sampled</span></div>
      <div className="mt-2 space-y-2">
        {metrics.recentRuns.slice(0, 5).map(({ run, evaluation }) => <div key={run.id} className="grid grid-cols-[1fr_auto_auto] items-center gap-3 text-meta">
          <span className="min-w-0 truncate text-ink-body">{run.deliveryMode === "model_delta" ? "Live model stream" : "Verified final"}</span>
          <span className="text-ink-muted">{formatLatency(run.timeToFirstResponseMs)}</span>
          <span className={cn("font-medium", evaluation && (!evaluation.evidencePassed || !evaluation.contextual) ? "text-danger-ink" : "text-secondary")}>{evaluation ? `${evaluation.qualityScore}` : "—"}</span>
        </div>)}
        {!metrics.recentRuns.length ? <p className="text-meta text-ink-muted">No agent runs in this thread yet.</p> : null}
      </div>
    </div>
    <p className="border-t border-line bg-canvas px-4 py-3 text-[11px] leading-4 text-ink-muted">Evidence and contextuality are deterministic integrity/repetition checks. Quality and depth are transparent heuristics, not a claim that the model is semantically correct.</p>
  </div>;
}

function responseToMessage(response: AgentResponse): Message {
  return { id: response.message_id, role: "assistant", content: response.message, widgets: response.widgets, citations: response.citations, created_at: new Date().toISOString() };
}

function completedWidgetIds(messages: Message[]) {
  const widgetsByDraft = new Map<string, string[]>();
  const completed = new Set<string>();
  for (const message of messages) {
    for (const widget of message.widgets) {
      if (widget.data.lifecycle === "completed" || widget.data.lifecycle === "cancelled") completed.add(widget.id);
      const resourceId = typeof widget.data.draftId === "string" ? widget.data.draftId : typeof widget.data.transactionId === "string" ? widget.data.transactionId : null;
      if (!resourceId) continue;
      const prior = widgetsByDraft.get(resourceId) ?? [];
      prior.forEach((id) => completed.add(id));
      if (widget.type === widgetTypeIds.transaction_preview && widget.actions.length === 0) completed.add(widget.id);
      widgetsByDraft.set(resourceId, [...prior, widget.id]);
    }
  }
  return completed;
}

/** The rail is a drawer below `md` and a docked column above it; the layout,
 *  the focus behaviour, and the scroll lock all hinge on knowing which.
 *
 *  `useSyncExternalStore` rather than state seeded from `matchMedia`, and the
 *  distinction matters now that the shell renders before its data: the server
 *  has no viewport, so a seeded initial value disagrees with the client's first
 *  render and React reports a hydration mismatch on the one attribute that
 *  depends on it. Giving it an explicit server snapshot means the server and
 *  the hydration pass agree by construction, and the real value arrives in the
 *  commit straight after. The rail's *appearance* never depended on this — that
 *  is carried by `md:` variants in CSS — so there is nothing to flash. */
function useMediaQuery(query: string) {
  const subscribe = useCallback((onChange: () => void) => {
    const media = window.matchMedia(query);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [query]);
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(query).matches,
    // No viewport to measure, so assume the narrow case: a drawer that is shut
    // is the safe thing to render, because it is the one that stays out of the
    // tab order until it is opened.
    () => false,
  );
}

/** Marks whether a scroll container has content hidden above or below, so a
 *  cut-off list can say so with a fade instead of ending on a hard edge.
 *
 *  Written to the DOM rather than to state, deliberately. This fires on every
 *  scroll event, and in React state that is a re-render of the whole rail per
 *  frame of scrolling — for two fades whose only job is to be visible or not.
 *  The flags land as data attributes and CSS does the rest, so scrolling the
 *  rail now costs nothing in React at all. */
function useScrollEdges<T extends HTMLElement>(dependency: unknown) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const node = ref.current;
    const host = node?.parentElement;
    if (!node || !host) return;
    // Held so the attribute is only written when the answer actually changes;
    // a scroll within the same state is the common case.
    let top: boolean | null = null;
    let bottom: boolean | null = null;
    const update = () => {
      const nextTop = node.scrollTop > 4;
      const nextBottom = Math.ceil(node.scrollTop + node.clientHeight) < node.scrollHeight - 4;
      if (nextTop !== top) { top = nextTop; host.dataset.edgeTop = String(nextTop); }
      if (nextBottom !== bottom) { bottom = nextBottom; host.dataset.edgeBottom = String(nextBottom); }
    };
    update();
    node.addEventListener("scroll", update, { passive: true });
    if (typeof ResizeObserver === "undefined") return () => node.removeEventListener("scroll", update);
    const observer = new ResizeObserver(update);
    observer.observe(node);
    if (node.firstElementChild) observer.observe(node.firstElementChild);
    return () => { node.removeEventListener("scroll", update); observer.disconnect(); };
  }, [dependency]);
  return ref;
}

/** Asks for the next page once the end of the list comes into view, so history
 *  arrives as the reader scrolls towards it rather than all at once on load. */
function useEndOfList(root: RefObject<HTMLElement | null>, armed: boolean, onReached: () => void) {
  const sentinel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = sentinel.current;
    if (!node || !armed || typeof IntersectionObserver === "undefined") return;
    // A generous margin so the page lands before the reader hits the bottom.
    const observer = new IntersectionObserver(([entry]) => { if (entry.isIntersecting) onReached(); }, { root: root.current, rootMargin: "200px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, [root, armed, onReached]);
  return sentinel;
}

/** The wordmark and the one action that belongs to the workspace rather than
 *  to a thread.
 *
 *  Starting a conversation used to own a 52px band of its own, directly under
 *  this one. That is a lot of rail for a control that is not the primary action
 *  in this product — the composer is always on screen, so most turns start by
 *  typing into the thread already open, not by making a new one. Folding it up
 *  here returns the space to the list, which is what the rail is actually for.
 *
 *  A compose glyph rather than a bare plus: next to a wordmark, "+" reads as
 *  ambiguously as "add an account". The tooltip explains it to a pointer and
 *  the label explains it to a screen reader; on touch the icon carries it,
 *  which is why it is the conventional one rather than a clever one. */
const RailHeader = memo(function RailHeader({ creating, onNew, onClose }: { creating: boolean; onNew: () => void; onClose: () => void }) {
  return <header className="rail-header">
    <div className="min-w-0 flex-1">
      <p className="truncate font-heading text-title leading-none font-semibold tracking-[-0.03em] text-ink">fyn AI</p>
      <p className="ledger-meta mt-0.5 truncate">Private workspace</p>
    </div>
    <Tooltip>
      <TooltipTrigger render={<Button type="button" variant="ghost" size="icon-lg" disabled={creating} onClick={onNew} aria-label="New conversation" className="shrink-0 text-ink-muted hover:text-secondary" />}><SquarePen className="size-[22px]" /></TooltipTrigger>
      <TooltipContent>New conversation</TooltipContent>
    </Tooltip>
    <Button variant="ghost" size="icon" aria-label="Close navigation" className="shrink-0 md:hidden" onClick={onClose}><X /></Button>
  </header>;
});

/** One band: who you are, and the two things that belong to you rather than to
 *  a thread.
 *
 *  These were three rows of equal weight, which made the account — the anchor
 *  of the whole rail — read as just another menu item. Collapsing the utilities
 *  to icons beside it puts the weight where it belongs and returns a row of
 *  height to the list above.
 *
 *  The account is a sibling of the icons rather than their parent: nesting a
 *  button inside a button is invalid, and the whole block is the target for
 *  opening your profile. */
const RailFooter = memo(function RailFooter({ user, onOpenSettings, onOpenProfile }: {
  user: Bootstrap["user"] | null;
  onOpenSettings: () => void;
  onOpenProfile: () => void;
}) {
  return <footer className="rail-footer">
    <div className="flex items-center gap-1 px-1">
      {/* One line, because that is all the row is for: whose workspace this is
          and a way into it. Currency and timezone are ambient facts you check
          rarely and never act on from here, so they move to the tooltip and
          stop making a two-line block out of a one-line answer. */}
      <Tooltip>
        <TooltipTrigger render={
          <button
            type="button"
            disabled={!user}
            onClick={onOpenProfile}
            aria-label={user ? `${user.name} — profile and sign-in methods` : "Profile and sign-in methods"}
            className="flex h-8 min-w-0 flex-1 items-center gap-2 rounded-lg px-2 text-left transition-colors duration-[110ms] ease-linear hover:bg-surface-sunken active:scale-[.995] disabled:pointer-events-none"
          />
        }>
          <span className="ledger-stamp shrink-0">{user ? user.name.slice(0, 1) : ""}</span>
          {user
            ? <span className="truncate text-control font-medium text-ink-body">{user.name}</span>
            : <span className="h-2.5 w-24 animate-pulse rounded-full bg-line" />}
        </TooltipTrigger>
        {user ? <TooltipContent>{user.currency} · {user.timezone}</TooltipContent> : null}
      </Tooltip>
      <Tooltip>
        <TooltipTrigger render={<Button type="button" variant="ghost" size="icon" onClick={onOpenSettings} aria-label="Settings" className="shrink-0" />}><Settings size={15} /></TooltipTrigger>
        <TooltipContent>Settings</TooltipContent>
      </Tooltip>
    </div>
  </footer>;
});

const RailEntry = memo(function RailEntry({ conversation, active, entryRef, onSelect, onPrefetch, onDelete }: {
  conversation: ConversationSummary;
  active: boolean;
  entryRef?: RefObject<HTMLButtonElement | null>;
  onSelect: (id: string) => void;
  onPrefetch: (id: string) => void;
  onDelete: (conversation: ConversationSummary) => void;
}) {
  return <div className="ledger-row">
    <button
      ref={entryRef}
      type="button"
      aria-current={active ? "page" : undefined}
      onClick={() => onSelect(conversation.id)}
      onPointerEnter={() => onPrefetch(conversation.id)}
      onFocus={() => onPrefetch(conversation.id)}
      className="ledger-entry"
    >
      <span aria-hidden className="ledger-mark" />
      <span className="line-clamp-2">{conversation.title}</span>
    </button>
    <button type="button" onClick={() => onDelete(conversation)} aria-label={`Delete conversation: ${conversation.title}`} className="ledger-strike"><Trash2 size={14} /></button>
  </div>;
});

const MONEY_PAGES = [
  { label: "Overview", icon: LayoutDashboard, path: "/overview" },
  { label: "Transactions", icon: ReceiptText, path: "/transactions" },
  { label: "Categories", icon: Tags, path: "/categories" },
] as const;

const ConversationRail = memo(function ConversationRail({ conversations, activeId, activePage, user, open, docked, switching, loading, loadingMore, hasMore, onClose, onOpenPage, onSelect, onPrefetch, onDelete, onLoadMore, onNew, onOpenSettings, onOpenProfile }: {
  conversations: ConversationSummary[];
  activeId: string;
  activePage: string | null;
  /** Null until bootstrap answers; the rail draws its own placeholder. */
  user: Bootstrap["user"] | null;
  open: boolean;
  docked: boolean;
  switching: boolean;
  loading: boolean;
  loadingMore: boolean;
  hasMore: boolean;
  onClose: () => void;
  onOpenPage: (path: string) => void;
  onSelect: (id: string) => void;
  /** Warms a thread the pointer is heading for, so opening it is a paint. */
  onPrefetch: (id: string) => void;
  onDelete: (conversation: ConversationSummary) => void;
  onLoadMore: () => void;
  onNew: () => void;
  onOpenSettings: () => void;
  onOpenProfile: () => void;
}) {
  const listRef = useScrollEdges<HTMLDivElement>(conversations.length);
  const endRef = useEndOfList(listRef, hasMore && !loadingMore, onLoadMore);
  const activeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => { activeRef.current?.scrollIntoView({ block: "nearest" }); }, [activeId]);

  return <aside
    id="conversation-rail"
    aria-label="Workspace navigation"
    inert={!docked && !open}
    className={cn("ledger fixed inset-y-0 left-0 z-40 flex min-h-0 w-[min(var(--rail-w),85vw)] flex-col border-r border-line bg-ground transition-transform duration-300 ease-[cubic-bezier(0.32,0.72,0,1)] md:static md:h-full md:w-auto md:translate-x-0 md:transition-none", open ? "translate-x-0 shadow-[var(--shadow-overlay)]" : "-translate-x-full")}
  >
    <RailHeader creating={switching} onNew={onNew} onClose={onClose} />

    <nav aria-label="Money pages" className="py-2">
      <p className="ledger-meta px-3 pt-1 pb-2">Money</p>
      {MONEY_PAGES.map((item) => {
        const Icon = item.icon;
        return <div key={item.label} className="ledger-row">
          <button
            type="button"
            aria-label={item.label}
            aria-current={activePage === item.path ? "page" : undefined}
            onClick={() => onOpenPage(item.path)}
            className="ledger-entry money-entry"
          >
            <span aria-hidden className="ledger-mark" />
            <Icon size={16} className="shrink-0" />
            <span>{item.label}</span>
          </button>
        </div>;
      })}
    </nav>

    <div className="rail-body flex min-h-0 flex-col">
      <p className="chat-index-label shrink-0">Recent chats</p>
      <div ref={listRef} className="panel-scroll min-h-0 flex-1 overflow-y-auto">
        {loading
          ? <div role="status" aria-label="Loading your conversations" className="space-y-3 px-4 pt-6">{[0, 1, 2, 3].map((row) => <div key={row} className="h-3 animate-pulse rounded-full bg-line" style={{ width: `${88 - row * 13}%` }} />)}</div>
          : conversations.length === 0
            ? <div className="pt-8 px-3"><p className="text-control font-medium text-ink-body">No conversations yet</p><p className="mt-1 text-note leading-5 text-ink-muted">Start one and it appears here.</p></div>
            : <nav aria-label="Conversation history" className="relative pb-3">
              {conversations.map((conversation) => <RailEntry
                key={conversation.id}
                conversation={conversation}
                active={conversation.id === activeId}
                entryRef={conversation.id === activeId ? activeRef : undefined}
                onSelect={onSelect}
                onPrefetch={onPrefetch}
                onDelete={onDelete}
              />)}
              <div ref={endRef} aria-hidden className="h-px" />
              {loadingMore ? <p role="status" className="ledger-meta py-4 px-3">Loading earlier</p> : null}
            </nav>}
      </div>
    </div>

    <RailFooter user={user} onOpenSettings={onOpenSettings} onOpenProfile={onOpenProfile} />
  </aside>;
});

/** Sends a caller without a session to the sign-in page.
 *
 *  The session cookie is httpOnly, so nothing in the browser can tell whether
 *  one is live — only the server's answer can. A 401 from the first query is
 *  therefore the signal, and it is a redirect rather than an error banner:
 *  "sign in" is a destination, not a failure to report. */
function useSignInGuard(error: unknown) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const signedOut = isUnauthorized(error);
  useEffect(() => {
    if (!signedOut) return;
    // Nothing cached under the ended session may be shown to whoever signs in next.
    queryClient.clear();
    navigate(appPaths.login, { replace: true });
  }, [navigate, signedOut, queryClient]);
  return signedOut;
}

/** A finished run and a legacy trace are read-only, but `WidgetRenderer` still
 *  wants a handler. One shared no-op, so passing it does not hand the renderer a
 *  new prop on every render and defeat its memoisation. */
const noAction = () => undefined;

/** Marks where the copilot's turn starts. Shared so the reply being written and
 *  the reply already written begin at exactly the same pixel. */
function AssistantByline({ thinking = false, live = false }: { thinking?: boolean; live?: boolean }) {
  return <div className="mb-2 flex items-center gap-2">
    <span className="grid size-6 place-items-center rounded-full bg-secondary-tint text-secondary"><Sparkles size={14} /></span>
    <span className="text-meta font-semibold tracking-[0.08em] text-ink-muted uppercase">fyn AI</span>
    {thinking ? <span aria-label={live ? "Thinking" : "Thinking transcript"} className="text-ink-muted"><Activity size={15} className={live ? "animate-pulse" : undefined} /></span> : null}
  </div>;
}

/** The reply forming in place: same byline and same run card the finished message
 *  will carry, in the same spot, so landing the answer collapses the run and
 *  fills in the prose rather than moving anything. */
function AgentActivityIndicator({ activities, reasoningSummary }: { activities: AgentActivity[]; reasoningSummary: string }) {
  // Rebuilt only when a step actually streams in. A fresh object every render
  // would be a fresh `widget` prop, and the run card would re-render along with
  // whatever else moved on the page.
  const widget: Widget = useMemo(() => {
    const latest = activities.at(-1);
    const fallback = latest?.detail || latest?.label || "Preparing a contextual answer";
    const summary = (reasoningSummary || fallback).replace(/\s+/g, " ").trim().slice(0, 320);
    return {
      id: "live-agent-activity",
      type: widgetTypeIds.agent_activity,
      version: 1,
      data: {
        title: "fyn AI is working",
        engine: "AG-UI",
        model: "live run",
        summary,
        reasoningTrace: reasoningSummary || null,
        debugTrace: environment.isDevelopment,
        steps: activities,
        // Folded rather than spread: `Math.max(...array)` passes every element as
        // an argument, which throws once a run is long enough. Runs are short, so
        // this is insurance rather than a fix.
        totalMs: activities.reduce((longest, activity) => Math.max(longest, activity.cumulativeMs), 0),
        live: true,
      },
      actions: [],
    };
  }, [activities, reasoningSummary]);
  return <div className="max-w-[680px]">
    <AssistantByline thinking live />
    <div className="pl-0 sm:pl-8"><WidgetRenderer widget={widget} disabled onAction={noAction} /></div>
  </div>;
}

function AppSkeleton({ label = "Opening your financial conversation…" }: { label?: string }) {
  return <div role="status" className="grid h-dvh place-items-center bg-ground"><div className="flex flex-col items-center gap-3 text-ink-muted"><span className="grid size-12 animate-pulse place-items-center rounded-lg bg-secondary-tint text-secondary"><Sparkles size={20} /></span><p className="text-control">{label}</p></div></div>;
}

function ThreadSkeleton() {
  return <div role="status" aria-label="Loading this conversation" className="space-y-6 pt-2">
    {[0, 1].map((row) => <div key={row} className="space-y-3">
      <div className="h-3 w-24 animate-pulse rounded-full bg-line" />
      <div className="h-4 w-3/4 animate-pulse rounded-full bg-line" />
      <div className="h-24 animate-pulse rounded-lg bg-line/70" />
    </div>)}
  </div>;
}

/** Keeps Tab inside an open overlay, closes it on Escape, and hands focus back
 *  to whatever opened it. */
export function useWorkspaceOverlay(open: boolean, onClose: () => void) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement as HTMLElement | null;
    const node = ref.current;
    const focusable = () => Array.from(node?.querySelectorAll<HTMLElement>('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])') ?? []).filter((element) => element.offsetParent !== null);
    focusable()[0]?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === first || !node?.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      // Whatever opened this may have gone inert behind it — on mobile the rail
      // closes as the drawer opens — so fall back to a control still on screen.
      const reachable = opener?.isConnected && !opener.closest("[inert]") ? opener : document.querySelector<HTMLElement>("header button");
      reachable?.focus?.();
    };
  }, [open, onClose]);
  return ref;
}

/** How long a deleted thread can still be brought back. Long enough to read the
 *  title and reconsider, short enough that "deleted" stays true. */
const UNDO_WINDOW_MS = 7000;

/** Delete asks nothing and takes effect at once; the toast is where it can be
 *  taken back. Each deleted entry lifts out of the rail as its own slip and is
 *  ruled through while its window runs down — the strike is the clock, so what
 *  is going and how long is left to stop it are one mark rather than two. Delete
 *  several in a row and the slips stack, each keeping its own window, so a quick
 *  clear-out is still undoable one thread at a time.
 *
 *  The toast's own timer drives the deletion: the strike finishing, the slip
 *  leaving, and the thread being erased are the same event. */
function UndoToastList() {
  const { toasts } = useToastManager();
  return toasts.map((slip) => <Toast
    key={slip.id}
    toast={slip}
    style={{ "--undo-window": `${UNDO_WINDOW_MS}ms` } as CSSProperties}
    className="rounded-lg border-line bg-surface shadow-[var(--shadow-overlay)]"
  >
    <ToastContent className="flex-col items-stretch gap-2 p-4">
      <div className="flex items-center gap-2">
        <ToastTitle className="ledger-meta" />
        <ToastAction className="strike-slip-undo ml-auto" render={<button type="button" />} />
      </div>
      <ToastDescription className="strike-slip-entry text-control leading-[1.35] font-medium text-ink-body">
        <span className="line-clamp-1">{slip.description}</span>
        <span aria-hidden className="strike-slip-rule" />
      </ToastDescription>
    </ToastContent>
  </Toast>);
}

/** Mounted only while open, so a drawer reopened later is never still armed to
 *  delete everything or mid-way through a revoke confirmation. */
function PrivacyDrawer({ onClose, onDeleted }: { onClose: () => void; onDeleted: () => void }) {
  const queryClient = useQueryClient();
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [confirmingRevoke, setConfirmingRevoke] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busyControl, setBusyControl] = useState<string | null>(null);
  const panelRef = useWorkspaceOverlay(true, onClose);
  const privacy = useQuery({ queryKey: ["privacy"], queryFn: getPrivacyStatus });

  const run = useMutation({
    mutationFn: async ({ kind, value }: { kind: "location" | "revoke" | "export" | "delete"; value?: string | boolean }) => {
      if (kind === "location") return setLocationEnabled(Boolean(value));
      if (kind === "revoke") return revokeSource(String(value));
      if (kind === "export") return downloadDataExport();
      return deleteAllData();
    },
    onMutate: ({ kind, value }) => { setProblem(null); setNotice(null); setBusyControl(kind === "revoke" ? `revoke:${value}` : kind); },
    onSuccess: async (result, variables) => {
      setBusyControl(null);
      if (variables.kind === "delete") { onDeleted(); return; }
      if (variables.kind === "export") setNotice(`Saved ${typeof result === "string" ? result : "your export"} to your downloads.`);
      if (variables.kind === "revoke") { setConfirmingRevoke(null); setNotice(`${String(variables.value).toUpperCase()} can no longer add transactions.`); }
      await queryClient.invalidateQueries({ queryKey: ["privacy"] });
    },
    onError: (cause: Error) => { setBusyControl(null); setProblem(cause.message); },
  });

  const locationEnabled = privacy.data?.locationEnabled ?? false;
  const sources = Object.entries(privacy.data?.sources ?? {});

  return <>
    <button type="button" tabIndex={-1} aria-hidden onClick={onClose} className="scrim-fade fixed inset-0 z-40 bg-ink/25 backdrop-blur-[2px]" />
    <section ref={panelRef} role="dialog" aria-modal="true" aria-labelledby="privacy-title" className="drawer-right fixed inset-y-0 right-0 z-50 flex w-full max-w-md flex-col border-l border-line bg-surface shadow-[var(--shadow-overlay)]">
      <div className="flex shrink-0 items-center border-b border-line px-4 pt-[max(1.25rem,env(safe-area-inset-top))] pb-4 sm:px-6">
        <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-secondary-tint text-secondary"><ShieldCheck size={20} /></span>
        <div className="ml-3 min-w-0"><h2 id="privacy-title" className="font-heading text-title font-semibold text-ink">Privacy &amp; data</h2><p className="text-note text-ink-muted">Nothing is collected until you switch it on.</p></div>
        <Button type="button" variant="ghost" size="icon-lg" aria-label="Close privacy settings" onClick={onClose} className="-mr-1 ml-auto rounded-xl text-ink-muted"><X /></Button>
      </div>

      <div className="panel-scroll min-h-0 flex-1 space-y-6 overflow-y-auto px-4 pt-6 pb-[max(1.75rem,env(safe-area-inset-bottom))] sm:px-6">
        {problem ? <p role="alert" className="flex items-start gap-2 rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note leading-5 text-danger-ink"><TriangleAlert className="mt-0.5 shrink-0" />{problem}</p> : null}
        {notice ? <p role="status" className="flex items-start gap-2 rounded-lg border border-secondary-line bg-secondary-tint px-4 py-3 text-note leading-5 text-secondary-hover"><CheckCircle2 className="mt-0.5 shrink-0" />{notice}</p> : null}
        {privacy.isError ? <p role="alert" className="rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note leading-5 text-danger-ink">Your privacy settings couldn’t be loaded, so they’re hidden rather than shown wrong. <button type="button" onClick={() => privacy.refetch()} className="font-semibold underline">Load them again</button></p> : null}
        {privacy.isLoading ? <div role="status" aria-label="Loading privacy settings" className="space-y-3">{[0, 1, 2].map((row) => <div key={row} className="h-16 animate-pulse rounded-lg bg-line/70" />)}</div> : null}

        {privacy.data ? <>
          <div className="rounded-lg border border-line p-4">
            <div className="flex items-center gap-3">
              <MapPin className="shrink-0 text-secondary" />
              <div className="min-w-0"><p className="text-control font-semibold text-ink-body">Location enrichment</p><p className="mt-0.5 text-note leading-5 text-ink-muted">Adds the place a transaction happened. Precise location is never stored.</p></div>
              <button type="button" role="switch" aria-label="Location enrichment" aria-checked={locationEnabled} disabled={run.isPending} onClick={() => run.mutate({ kind: "location", value: !locationEnabled })} className={cn("ml-auto grid h-11 w-14 shrink-0 place-items-center rounded-full disabled:opacity-60", busyControl === "location" && "opacity-70")}>
                <span className={cn("flex h-6 w-11 items-center rounded-full p-0.5 transition-colors", locationEnabled ? "bg-secondary" : "bg-line-strong")}><span className={cn("block size-5 rounded-full bg-surface ring-1 ring-black/5 transition-transform duration-[110ms] ease-linear", locationEnabled && "translate-x-5")} /></span>
              </button>
            </div>
          </div>

          <div>
            <p className="mb-2 text-meta font-semibold tracking-[0.08em] text-ink-muted uppercase">Where transactions can come from</p>
            <div className="divide-y divide-line rounded-lg border border-line">
              {sources.map(([source, active]) => <div key={source} className="px-4 py-3">
                <div className="flex items-center gap-3">
                  <div className="min-w-0 flex-1"><p className="text-control font-medium uppercase text-ink-body">{source}</p><p className="mt-0.5 text-note leading-5 text-ink-muted">{active ? "Allowed to add transactions" : "Revoked — it can no longer add transactions"}</p></div>
                  {active ? <Button type="button" variant="outline" size="sm" disabled={run.isPending} onClick={() => setConfirmingRevoke(source)}>Revoke</Button> : <span className="shrink-0 text-note font-semibold text-danger-ink">Revoked</span>}
                </div>
                {confirmingRevoke === source ? <div className="mt-3 rounded-lg bg-surface-sunken p-3">
                  <p className="text-note leading-5 text-ink-body">Revoke {source.toUpperCase()}? Transactions already recorded stay; this source just can’t add more.</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button type="button" size="lg" disabled={run.isPending} onClick={() => run.mutate({ kind: "revoke", value: source })} variant="danger">{busyControl === `revoke:${source}` ? <Loader2 size={14} className="animate-spin" /> : null}Revoke {source.toUpperCase()}</Button>
                    <Button type="button" variant="ghost" size="lg" onClick={() => setConfirmingRevoke(null)} >Keep it on</Button>
                  </div>
                </div> : null}
              </div>)}
              {!sources.length ? <p className="px-4 py-4 text-note text-ink-muted">No sources are connected yet.</p> : null}
            </div>
          </div>

          <Button type="button" variant="outline" disabled={run.isPending} onClick={() => run.mutate({ kind: "export" })} size="lg" className="w-full">{busyControl === "export" ? <Loader2 className="animate-spin" /> : <Download />}{busyControl === "export" ? "Preparing your export…" : "Export my data"}</Button>

          <div className="rounded-lg border border-danger-line bg-danger-tint p-4">
            <div className="flex gap-3"><Trash2 className="mt-0.5 shrink-0 text-danger" /><div><p className="text-control font-semibold text-danger-ink">Delete all data</p><p className="mt-1 text-note leading-5 text-danger-ink/85">Permanently removes conversations, transactions, observations, goals, budgets, and preferences. This cannot be undone.</p></div></div>
            <input value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.target.value)} placeholder="Type DELETE MY DATA" aria-label="Deletion confirmation" className="manual-field manual-field-danger mt-4 h-[var(--h-field)] w-full rounded-lg border border-danger-line bg-surface px-3 text-body text-ink outline-none transition-colors duration-[110ms] ease-linear" />
            <Button type="button" disabled={deleteConfirmation !== "DELETE MY DATA" || run.isPending} onClick={() => run.mutate({ kind: "delete" })} variant="danger" size="lg" className="mt-2 w-full">{busyControl === "delete" ? <Loader2 className="animate-spin" /> : null}{busyControl === "delete" ? "Deleting everything…" : "Delete permanently"}</Button>
          </div>
        </> : null}
      </div>
    </section>
  </>;
}

/* Four things you can do, one per line and none of them repeated: the
   placeholder records an expense, these record income, ask, and analyse.
   Pressing one loads it into the box instead of sending it — a tapped demo
   must never file a real transaction. */
const STARTERS = ["Got ₹2 lakh salary today", "How much did I spend this month?", "Where can I cut back?"];

/** One composer, two placements. On a blank conversation it sits in the middle
 *  of the page next to the invitation; once there is a transcript to read it
 *  docks at the bottom. Same element either way, so nothing drifts. */
function Composer({ variant, value, onValueChange, onSubmit, onStop, textRef, fileRef, onAttach, busy, sending, running, stopping, paused, disabled, dragging, upload }: {
  variant: "focused" | "docked";
  value: string;
  onValueChange: (value: string) => void;
  onSubmit: (event?: FormEvent) => void;
  onStop: () => void;
  textRef: RefObject<HTMLTextAreaElement | null>;
  fileRef: RefObject<HTMLInputElement | null>;
  onAttach: (file: File | undefined) => void;
  busy: boolean;
  sending: boolean;
  running: boolean;
  stopping: boolean;
  paused: boolean;
  disabled: boolean;
  dragging: boolean;
  upload: { name: string; percent: number } | null;
}) {
  const focused = variant === "focused";
  // Recomputed per keystroke, which costs one regex over a short string.
  const reading = useMemo(() => readComposerEntry(value), [value]);
  return <form onSubmit={onSubmit} className={cn("pointer-events-auto mx-auto w-full", !focused && "max-w-[var(--column-w)]")}>
    {upload ? <div role="status" className="mb-2 flex items-center gap-3 rounded-lg border border-line bg-surface px-4 py-3 text-note text-ink-body"><Loader2 size={14} className="shrink-0 animate-spin text-secondary" /><span className="min-w-0 flex-1 truncate">Uploading {upload.name}</span><span className="money shrink-0 text-ink-muted">{upload.percent}%</span><span aria-hidden className="h-1 w-20 shrink-0 overflow-hidden rounded-full bg-surface-sunken"><span data-motion="informational" className="block h-full rounded-full bg-secondary transition-[width] duration-[240ms]" style={{ width: `${upload.percent}%` }} /></span></div> : null}
    <div data-dropping={dragging || undefined} className="entry-card p-2">
      {/* 14px of text inset is not arbitrary: it is where a 16px glyph lands
          inside a 44px control, so the first character of what you write sits
          on the same vertical as the paperclip below it. */}
      <Textarea id="composer" ref={textRef} value={value} disabled={disabled || paused} onChange={(event) => onValueChange(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); onSubmit(); } }} placeholder={disabled ? "Opening conversation…" : paused ? "Respond to the card above to continue…" : focused ? "Spent ₹500 on lunch" : "Ask anything about your finances…"} aria-label="Message fyn AI" aria-describedby="composer-hint" rows={1} className="max-h-36 min-h-10 resize-none border-0 bg-transparent px-3 py-2 text-control leading-6 shadow-none placeholder:text-ink-muted" />
      <div className="flex items-center gap-2">
        <input ref={fileRef} type="file" accept=".csv,text/csv" className="sr-only" tabIndex={-1} aria-hidden aria-label="Choose a CSV statement" onChange={(event) => { onAttach(event.target.files?.[0]); event.currentTarget.value = ""; }} />
        <Tooltip><TooltipTrigger render={<Button type="button" variant="ghost" size="icon" disabled={busy} onClick={() => fileRef.current?.click()} className="shrink-0" aria-label="Attach a CSV statement" />}><Paperclip /></TooltipTrigger><TooltipContent>Attach a CSV statement, or drop one anywhere</TooltipContent></Tooltip>
        {/* Two things share this line, and only one is ever on it.
            Ordinarily it is the single piece of small print the composer
            carries, there because filing happens without asking.
            The moment what you have typed reads as an amount, it becomes the
            reading instead — the answer to the only question anyone has while
            a run is going, given before the run starts. It is also the last
            chance to correct the figure without filing it first. */}
        <p id="composer-hint" className="entry-hint -ml-1" aria-live="polite">
          {reading
            ? <span key={`${reading.amountMinor}:${reading.kind}`} className="composer-reading">
              <span className={cn("money font-semibold", reading.kind === "income" ? "text-money-in" : reading.kind === "expense" ? "text-money-out" : "text-ink-body")}>
                {reading.kind === "income" ? "+" : reading.kind === "expense" ? "−" : ""}{formatMoney(reading.amountMinor)}
              </span>
              <span className="truncate text-ink-muted">{reading.kind}</span>
            </span>
            : <><CheckCircle2 size={14} className="shrink-0" /><span className="truncate">Complete entries are added automatically</span></>}
        </p>
        {running
          ? <Button type="button" size="icon" variant="outline" disabled={stopping} onClick={onStop} className="ml-auto shrink-0" aria-label={stopping ? "Stopping fyn AI" : "Stop fyn AI"}>{stopping ? <Loader2 className="animate-spin" /> : <Square size={14} fill="currentColor" />}</Button>
          : <Button type="submit" size="icon" disabled={!value.trim() || busy} className="ml-auto shrink-0" aria-label="Send message">{sending ? <Loader2 className="animate-spin" /> : <SendHorizontal />}</Button>}
      </div>
    </div>
  </form>;
}

function InterruptFallback({ interrupt, busy, onResolve }: {
  interrupt: FynInterrupt;
  busy: boolean;
  onResolve: (response: { status: "resolved"; payload: unknown } | { status: "cancelled" }) => void;
}) {
  const toolApproval = interrupt.reason === "tool_call";
  const clarification = interrupt.reason === "clarification";
  const continuation = clarification && interrupt.metadata.continuation && typeof interrupt.metadata.continuation === "object"
    ? interrupt.metadata.continuation as Record<string, unknown>
    : null;
  const clarificationOptions = continuation?.options && typeof continuation.options === "object"
    ? Object.entries(continuation.options as Record<string, unknown>)
    : [];
  const chooseClarification = (optionId: string) => onResolve({
    status: "resolved",
    payload: {
      approved: true,
      editedArgs: {
        widgetId: interrupt.widgetId,
        action: "resolve_clarification",
        payload: { clarificationId: continuation?.clarificationId, optionId },
        completeWidget: true,
      },
    },
  });
  return <div role="group" aria-label="Agent response required" className="hitl-card mx-auto mb-2 w-full max-w-[var(--column-w)] overflow-hidden rounded-lg border bg-surface">
    <div className="flex gap-2.5 px-3.5 py-3">
      <ShieldCheck size={17} className="mt-0.5 shrink-0 text-secondary" />
      <div className="min-w-0 flex-1">
        <p className="text-control font-semibold text-ink">{toolApproval ? "Approval needed" : "Choose an option"}</p>
        <p className="mt-0.5 text-note leading-4 text-ink-muted">{interrupt.message || "Review this request before the agent continues."}</p>
      </div>
    </div>
    <div className="hitl-actions border-t border-line">
      <Button type="button" variant="ghost" disabled={busy} onClick={() => onResolve({ status: "cancelled" })}>Cancel</Button>
      {toolApproval ? <>
        <Button type="button" variant="outline" disabled={busy} onClick={() => onResolve({ status: "resolved", payload: { approved: false } })}>Don’t approve</Button>
        <Button type="button" disabled={busy} onClick={() => onResolve({ status: "resolved", payload: { approved: true } })}>{busy ? <Loader2 className="animate-spin" /> : null}Approve</Button>
      </> : null}
      {clarificationOptions.map(([optionId, rawOption]) => {
        const option = rawOption && typeof rawOption === "object" ? rawOption as Record<string, unknown> : {};
        return <Button key={optionId} type="button" variant={clarificationOptions[0]?.[0] === optionId ? "default" : "outline"} disabled={busy} onClick={() => chooseClarification(optionId)}>{String(option.label || optionId)}</Button>;
      })}
    </div>
  </div>;
}

/** The dock's height is the transcript's bottom margin, so it is measured
 *  rather than guessed. It is not a constant: the box grows with what you
 *  type, and an upload chip or an error banner can appear above it. A guess
 *  is what was leaving the last line of every answer under the composer.
 *
 *  Published on the document root as well as returned, because the undo slips
 *  anchor to the same edge from inside a portal and cannot read it any other
 *  way. Only one dock is ever mounted, so there is nothing to collide with. */
function useDockHeight(active: boolean, onResize: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  // The height is published and acted on, never stored. Holding it in state
  // meant that growing the box by one line re-rendered the whole thread a
  // second time — on top of the render the keystroke had already caused — and
  // typing past the end of a line cost twice what typing along it did. What the
  // layout actually reads is the custom property, so that is all it is given.
  const latest = useRef(onResize);
  useEffect(() => { latest.current = onResize; });
  useEffect(() => {
    const node = ref.current;
    // Writing a custom property on the root invalidates inherited style for
    // every element under it, so it is written only when the rounded height
    // actually moves — the observer also fires for sub-pixel changes that round
    // to the same value, and for those there is nothing to say.
    let published: number | null = null;
    const publish = (value: number) => {
      if (value === published) return false;
      published = value;
      document.documentElement.style.setProperty("--dock-h", `${value}px`);
      return true;
    };
    if (!active || !node || typeof ResizeObserver === "undefined") { publish(0); return; }
    const observer = new ResizeObserver(([entry]) => {
      // The size arrives with the callback. Asking the element for it instead
      // forces a synchronous layout inside the very observer layout just fired.
      const measured = Math.round(entry.borderBoxSize?.[0]?.blockSize ?? entry.contentRect.height);
      if (publish(measured)) latest.current();
    });
    observer.observe(node);
    // Back to the stylesheet's opening guess, so a thread mounting after this
    // one starts from a sane reservation rather than the last dock's height.
    return () => { observer.disconnect(); document.documentElement.style.removeProperty("--dock-h"); };
  }, [active]);
  return ref;
}

type WidgetAction = (widgetId: string, action: WidgetActionId, payload: Record<string, unknown>, options?: { markUsed?: boolean }) => void;

/** One turn in the transcript.
 *
 *  Memoised, and the reason is the composer: the draft message lives in the
 *  same component that renders the thread, so without a boundary here every
 *  keystroke re-parsed every markdown block and rebuilt every widget on screen
 *  — work that grew with the length of the conversation, which is exactly
 *  backwards. Nothing in a finished turn reads what is being typed, so nothing
 *  in one needs to re-render while it is. The same boundary keeps a streaming
 *  reply from re-rendering the turns above it on every tick. */
const MessageArticle = memo(function MessageArticle({ message, activeWidget, cancelWidget, usedWidgets, pendingWidget, busy, citationsOpen, onToggleCitations, onAction, onCancelWidget }: {
  message: Message;
  activeWidget: string | null;
  cancelWidget: string | null;
  usedWidgets: Set<string>;
  pendingWidget: string | null;
  busy: boolean;
  citationsOpen: boolean;
  onToggleCitations: (messageId: string) => void;
  onAction: WidgetAction;
  onCancelWidget: (widgetId: string) => void;
}) {
  // The run trace is how the answer was reached, so it reads above the answer —
  // not appended after the widgets it produced, which is the order the backend
  // stores it in.
  const trace = message.widgets.find((widget) => widget.type === widgetTypeIds.agent_activity);
  const widgets = message.widgets.filter((widget) => widget.id !== trace?.id && !isLegacyAnalysisLifecycleWidget(widget));
  return <article data-message-id={message.id} className={cn("group", message.role === "user" ? "flex justify-end" : "max-w-[680px]")}>
    <div className={cn("min-w-0", message.role === "user" && "max-w-[82%]")}>
      {message.role === "assistant" ? <AssistantByline thinking={Boolean(trace)} /> : null}
      {trace ? <div className="mb-3 pl-0 sm:pl-8"><WidgetRenderer widget={trace} disabled onAction={noAction} /></div> : null}
      {message.content ? message.role === "user"
        ? <div className="w-fit break-words whitespace-pre-wrap rounded-xl rounded-br-sm bg-secondary px-4 py-3 text-body leading-6 text-on-secondary">{message.content}</div>
        : <div className="break-words pl-8"><MarkdownMessage>{message.content}</MarkdownMessage></div>
      : null}
      {widgets.length ? <div className="mt-3 space-y-3 pl-0 sm:pl-8">{widgets.map((widget) => {
        const active = widget.id === activeWidget;
        const title = typeof widget.data.title === "string" && widget.data.title.trim() ? widget.data.title : "Follow-up";
        return <div
          key={widget.id}
          role={active ? "group" : undefined}
          aria-label={active ? `Action required: ${title}` : undefined}
          data-active-widget={active || undefined}
          tabIndex={active ? -1 : undefined}
          className="scroll-mt-4"
        >
          <WidgetRenderer widget={widget} disabled={!active || usedWidgets.has(widget.id) || busy} pending={pendingWidget === widget.id} onCancel={widget.id === cancelWidget ? () => onCancelWidget(widget.id) : undefined} onAction={onAction} />
        </div>;
      })}</div> : null}
      {message.citations.length ? <div className="mt-2 ml-8">
        <button type="button" aria-expanded={citationsOpen} onClick={() => onToggleCitations(message.id)} className="flex min-h-8 items-center gap-2 rounded-lg text-meta font-medium text-ink-muted hover:text-secondary"><FileText size={14} /> {message.citations.length} data source{message.citations.length === 1 ? "" : "s"}</button>
        {citationsOpen ? <ul className="mt-2 space-y-1 rounded-lg border border-line bg-surface px-4 py-3">{message.citations.map((citation, index) => <li key={index} className="flex gap-2 text-meta leading-5 text-ink-muted"><span aria-hidden className="text-secondary">•</span><span><span className="font-medium text-ink-body">{typeof citation.label === "string" ? citation.label : "Recorded data"}</span>{typeof citation.entity_type === "string" ? ` · ${citation.entity_type.replaceAll("_", " ")}` : ""}</span></li>)}</ul> : null}
      </div> : null}
    </div>
  </article>;
});

/** The thread itself, held behind the same boundary and for the same reason.
 *  Every prop it takes is either state that does not move while you type or a
 *  callback held stable by the workspace, so a keystroke stops here. */
type TranscriptScrollHandle = {
  isAtEnd: (threshold?: number) => boolean;
  scrollToEnd: (behavior: ScrollBehavior) => void;
  scrollToMessage: (messageId: string | undefined, behavior: ScrollBehavior) => void;
};

const Transcript = memo(function Transcript({ messages, agentActivities, reasoningSummary, streamingText, streaming, busy, usedWidgets, pendingWidget, openCitations, activeWidget, cancelWidget, activeWidgetFocusKey, error, retry, onAction, onCancelWidget, onActiveWidgetFocus, onToggleCitations, onRetry, scrollRef, scrollHandleRef }: {
  messages: Message[];
  agentActivities: AgentActivity[];
  reasoningSummary: string;
  streamingText: string;
  streaming: boolean;
  busy: boolean;
  usedWidgets: Set<string>;
  pendingWidget: string | null;
  openCitations: Set<string>;
  activeWidget: string | null;
  cancelWidget: string | null;
  activeWidgetFocusKey: string | null;
  error: string | null;
  retry: Retry;
  onAction: WidgetAction;
  onCancelWidget: (widgetId: string) => void;
  onActiveWidgetFocus: () => void;
  onToggleCitations: (messageId: string) => void;
  onRetry: () => void;
  scrollRef: RefObject<HTMLDivElement | null>;
  scrollHandleRef: RefObject<TranscriptScrollHandle | null>;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  // The list does not start at the top of the scroller — the column above it is
  // padded — and the virtualiser measures offsets from the scroller. Without
  // this margin every row would be positioned that padding too high.
  const [scrollMargin, setScrollMargin] = useState(0);
  useLayoutEffect(() => { setScrollMargin(listRef.current?.offsetTop ?? 0); }, []);

  const virtualizer = useVirtualizer({
    count: messages.length,
    getScrollElement: () => scrollRef.current,
    // A measurement can arrive while React is committing this transcript.
    // TanStack's synchronous mode uses flushSync for that update, which React
    // 19 rejects inside a lifecycle. A normal scheduled render keeps the same
    // measurements without nesting another React flush inside the commit.
    useFlushSync: false,
    // Row measurements can change both scrollTop (to preserve the reader's
    // anchor) and every following row's position. Let the virtualiser write
    // those two DOM changes together, before the browser paints. Going
    // through a deferred React render here produced one frame with the new
    // scrollTop and the old transforms — the visible snap when a turn entered
    // or left the virtual window.
    directDomUpdates: true,
    directDomUpdatesMode: "transform",
    // Only an opening guess. Turns here run from a one-line question to a chart,
    // so every row is measured once it mounts and the estimate stops mattering.
    estimateSize: () => 220,
    getItemKey: (index) => messages[index].id,
    // Chat is end-anchored: measuring a previously estimated row must preserve
    // the visible tail, and a new turn should follow only while the reader is
    // already at the end. These are virtual-list decisions; a DOM selector
    // cannot make them because its target may not be mounted yet.
    anchorTo: "end",
    followOnAppend: true,
    scrollEndThreshold: 1,
    // Generous, because a row that scrolls in unmeasured resizes the moment it
    // does, and doing that at the edge of the viewport is what reads as jitter.
    overscan: 8,
    scrollMargin,
  });

  useLayoutEffect(() => {
    const handle: TranscriptScrollHandle = {
      isAtEnd: (threshold) => virtualizer.isAtEnd(threshold),
      scrollToEnd: (behavior) => virtualizer.scrollToEnd({ behavior }),
      scrollToMessage: (messageId, behavior) => {
        const index = messageId ? messages.findIndex((message) => message.id === messageId) : -1;
        // The latest persisted turn, a live streaming turn, and a target that
        // has just been replaced by its server ID all mean the same thing:
        // follow the physical end. `scrollToEnd` keeps reconciling that target
        // while unmounted rows exchange estimates for their measured heights.
        if (index < 0 || index === messages.length - 1) {
          virtualizer.scrollToEnd({ behavior });
          return;
        }
        virtualizer.scrollToIndex(index, { align: "end", behavior });
      },
    };
    scrollHandleRef.current = handle;
    return () => {
      if (scrollHandleRef.current === handle) scrollHandleRef.current = null;
    };
  }, [messages, scrollHandleRef, virtualizer]);

  const setListRef = useCallback((node: HTMLDivElement | null) => {
    listRef.current = node;
    virtualizer.containerRef(node);
  }, [virtualizer]);

  // A HITL response is a hand-off back to the person. Move both the viewport
  // and keyboard/screen-reader focus to that exact widget, even when the prior
  // (possibly very tall) widget left the reader far from the transcript's end.
  // The active turn can initially be outside the virtual window, so first ask
  // the virtualiser to mount its row and then focus the semantic boundary.
  const previousFocusKey = useRef(activeWidgetFocusKey);
  const focusFrame = useRef(0);
  useLayoutEffect(() => {
    const changed = previousFocusKey.current !== activeWidgetFocusKey;
    previousFocusKey.current = activeWidgetFocusKey;
    cancelAnimationFrame(focusFrame.current);
    if (!changed || !activeWidgetFocusKey || !activeWidget) return;

    const rowIndex = messages.length - 1;
    let attempts = 0;
    const reveal = () => {
      const scroller = scrollRef.current;
      if (!scroller) return;
      const target = scroller.querySelector<HTMLElement>('[data-active-widget="true"]');
      if (!target) {
        if (attempts === 0) virtualizer.scrollToIndex(rowIndex, { align: "start", behavior: "auto" });
        if (++attempts < 12) focusFrame.current = requestAnimationFrame(reveal);
        return;
      }

      // Respect a widget that deliberately focused one of its own fields (for
      // example, a newly opened category-name input).
      onActiveWidgetFocus();
      if (!(document.activeElement instanceof HTMLElement) || !target.contains(document.activeElement)) {
        target.focus({ preventScroll: true });
      }
      target.scrollIntoView({ block: "start", inline: "nearest", behavior: prefersReducedMotion() ? "auto" : "smooth" });
    };
    focusFrame.current = requestAnimationFrame(reveal);
    return () => cancelAnimationFrame(focusFrame.current);
  }, [activeWidget, activeWidgetFocusKey, messages, onActiveWidgetFocus, scrollRef, virtualizer]);

  const rows = virtualizer.getVirtualItems();
  const streamingMessage = useMemo<Message | null>(() => streamingText ? {
    id: "streaming-assistant",
    role: "assistant",
    content: streamingText,
    widgets: [],
    citations: [],
    created_at: new Date().toISOString(),
  } : null, [streamingText]);
  return <div role="log" aria-busy={streaming}>
    <div ref={setListRef} style={{ position: "relative" }}>
      {rows.map((row) => <div
        key={row.key}
        data-index={row.index}
        ref={virtualizer.measureElement}
        style={{ position: "absolute", insetInlineStart: 0, top: 0, width: "100%" }}
      >
        {/* The rhythm the removed `space-y-6` used to hold. It belongs on the
            row rather than between rows now, because absolutely positioned
            siblings have no gap to share. */}
        <div className="pb-6">
          <MessageArticle
            message={messages[row.index]}
            activeWidget={activeWidget}
            cancelWidget={cancelWidget}
            usedWidgets={usedWidgets}
            pendingWidget={pendingWidget}
            busy={busy}
            citationsOpen={openCitations.has(messages[row.index].id)}
            onToggleCitations={onToggleCitations}
            onAction={onAction}
            onCancelWidget={onCancelWidget}
          />
        </div>
      </div>)}
    </div>
    {streaming ? <div aria-hidden><AgentActivityIndicator activities={agentActivities} reasoningSummary={reasoningSummary} /></div> : null}
    {streamingMessage ? <div className="pb-6"><MessageArticle
      message={streamingMessage}
      activeWidget={null}
      cancelWidget={null}
      usedWidgets={usedWidgets}
      pendingWidget={null}
      busy
      citationsOpen={false}
      onToggleCitations={onToggleCitations}
      onAction={onAction}
      onCancelWidget={onCancelWidget}
    /></div> : null}
    {busy && !streaming ? <div className="mt-6 flex items-center gap-3 px-1 py-2 text-control text-ink-muted"><span className="grid size-7 place-items-center rounded-full bg-secondary-tint text-secondary"><Sparkles size={14} /></span><span className="flex gap-1" aria-hidden><i className="typing-dot" /><i className="typing-dot" /><i className="typing-dot" /></span><span className="sr-only">Working on it</span></div> : null}
    {error ? <div role="alert" className="mt-6 flex flex-wrap items-center gap-3 gap-2 rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note leading-5 text-danger-ink sm:mx-8"><TriangleAlert className="shrink-0" /><span className="min-w-0 flex-1">{error}</span>{retry ? <Button type="button" variant="outline" size="lg" onClick={onRetry} className="rounded-xl border-danger-line text-danger-ink hover:bg-danger-tint"><RotateCcw size={14} /> Try again</Button> : null}</div> : null}
  </div>;
});

/** What the shell needs to reach inside the thread for: the rail's Saved
 *  analyses sends a prompt, and a file dropped anywhere lands on the importer.
 *  A handle keeps those two reachable without lifting the thread's state. */
type ThreadHandle = { sendPrompt: (text: string) => void; attach: (file: File | undefined) => void };

function ConversationWorkspace({ initialData, loadingThread, navOpen, onOpenNav, switching, dragging, handleRef }: {
  initialData: Bootstrap;
  loadingThread?: boolean;
  navOpen: boolean;
  onOpenNav: () => void;
  switching: boolean;
  dragging: boolean;
  handleRef: RefObject<ThreadHandle | null>;
}) {
  const queryClient = useQueryClient();
  const [messages, setMessages] = useState<Message[]>(initialData.active_conversation.messages);
  const conversationId = initialData.active_conversation.id;
  const [input, setInput] = useState("");
  const [linkCopied, setLinkCopied] = useState(false);
  const switchingConversation = switching;
  const [usedWidgets, setUsedWidgets] = useState<Set<string>>(() => completedWidgetIds(initialData.active_conversation.messages));
  const [pendingWidget, setPendingWidget] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState<Retry>(null);
  const [connectionLost, setConnectionLost] = useState(false);
  const [agentActivities, setAgentActivities] = useState<AgentActivity[]>([]);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [runPhase, setRunPhase] = useState<AgentRunPhase | null>(null);
  const [stoppingRun, setStoppingRun] = useState(false);
  const [interrupts, setInterrupts] = useState<FynInterrupt[] | null>(null);
  const [reasoningSummary, setReasoningSummary] = useState("");
  const [streamingText, setStreamingText] = useState("");
  const [metricsOpen, setMetricsOpen] = useState(false);
  const [openCitations, setOpenCitations] = useState<Set<string>>(new Set());
  const [upload, setUpload] = useState<{ name: string; percent: number } | null>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [announcement, setAnnouncement] = useState("");
  const agentState = useQuery({
    queryKey: ["agent-state", conversationId],
    queryFn: () => loadAgentThreadState(conversationId),
    enabled: !loadingThread,
    staleTime: 2_000,
    refetchOnWindowFocus: true,
  });
  const agentMetrics = useQuery({
    queryKey: ["agent-metrics", conversationId],
    queryFn: () => loadAgentThreadMetrics(conversationId),
    enabled: !loadingThread && metricsOpen,
    staleTime: 10_000,
  });
  // Switching threads used to replace this whole component — a `key` on it
  // meant the header, the composer, the scroll container and every widget were
  // thrown away and rebuilt for what is really a change of contents. Re-seeding
  // here instead keeps all of that mounted, so a switch swaps the transcript
  // and moves the mark in the rail, and nothing else moves.
  //
  // Adjusted during render rather than in an effect, which is what React
  // recommends for resetting state when a prop changes: the reset lands in the
  // same commit, so the previous thread's messages are never painted under the
  // new thread's title.
  //
  // Two things reset it, and they are not the same thing. A different `id` is a
  // different conversation and clears everything, the half-typed draft
  // included. The same `id` going from loading to ready is the transcript
  // arriving for the thread already on screen — that adopts the messages and
  // deliberately leaves the draft alone, because it was typed into this thread.
  const [seeded, setSeeded] = useState({ id: conversationId, loading: Boolean(loadingThread) });
  if (seeded.id !== conversationId || seeded.loading !== Boolean(loadingThread)) {
    const changedThread = seeded.id !== conversationId;
    setSeeded({ id: conversationId, loading: Boolean(loadingThread) });
    setMessages(initialData.active_conversation.messages);
    setUsedWidgets(completedWidgetIds(initialData.active_conversation.messages));
    setPendingWidget(null);
    setError(null);
    setRetry(null);
    setConnectionLost(false);
    setAgentActivities([]);
    setActiveRunId(null);
    setRunPhase(null);
    setStoppingRun(false);
    setInterrupts(null);
    setReasoningSummary("");
    setStreamingText("");
    setMetricsOpen(false);
    setOpenCitations(new Set());
    setUpload(null);
    setAtBottom(true);
    setAnnouncement("");
    setLinkCopied(false);
    if (changedThread) setInput("");
  }

  const scrollRef = useRef<HTMLDivElement>(null);
  const transcriptScrollRef = useRef<TranscriptScrollHandle | null>(null);
  const { headerVisible, updateHeaderForScroll, showHeader } = useAutoHideSiteHeader();
  const textRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const jumpToLatestRef = useRef<HTMLDivElement>(null);
  // Scheduled work that can outlive the render that started it, so it is held
  // rather than fired and forgotten.
  const copiedTimer = useRef<number | undefined>(undefined);
  const scrollSettleTimer = useRef<number | undefined>(undefined);
  // True from the first upward gesture until the reader reaches the exact end
  // again. This is deliberately imperative: it closes the frame-sized gap
  // between an input event and the state/effect commit that removes bottom
  // following.
  const readerScrolled = useRef(false);
  // Which transcript was last placed on screen; reset when the thread changes.
  const arrivals = useRef<string | null>(null);
  // One controller for everything this thread is currently listening to.
  // Aborting detaches this screen; the durable server run deliberately keeps
  // going and can be replayed when the reader comes back.
  //
  // Keyed on the conversation rather than on the mount, and that is the whole
  // reason this is safe now that the component survives a switch: leaving a
  // thread has to cancel its run, and before it was the unmount that did so.
  //
  // Created inside the effect, deliberately. Holding it in state instead keeps
  // a single controller across React's development remount, so the discarded
  // first mount aborts the one the real mount goes on to use and every request
  // afterwards starts life already cancelled.
  const inFlight = useRef<AbortController | null>(null);
  useEffect(() => {
    showHeader();
    readerScrolled.current = false;
    const controller = new AbortController();
    inFlight.current = controller;
    return () => {
      window.clearTimeout(copiedTimer.current);
      window.clearTimeout(scrollSettleTimer.current);
      controller.abort();
      inFlight.current = null;
      // The next thread is a new transcript, so it is placed at its end rather
      // than smooth-scrolled there as if a reply had just landed.
      arrivals.current = null;
    };
  }, [conversationId, showHeader]);

  const succeeded = useCallback((response: AgentResponse) => {
    setMessages((current) => {
      const updated = applyWidgetUpdates(current, response.widgetUpdates);
      return updated.some((message) => message.id === response.message_id)
        ? updated
        : [...updated, responseToMessage(response)];
    });
    setError(null);
    setRetry(null);
    setConnectionLost(false);
    setAnnouncement("");
    setStreamingText("");
    // Cache refresh is bookkeeping, not part of the financial action. Keeping
    // the mutation pending until an arbitrarily long history refetch completes
    // would disable the next HITL widget even though the backend already
    // returned it successfully.
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: ["conversations"] }),
      queryClient.invalidateQueries({ queryKey: ["conversation", conversationId] }),
      queryClient.invalidateQueries({ queryKey: ["agent-state", conversationId] }),
      queryClient.invalidateQueries({ queryKey: ["agent-metrics", conversationId] }),
    ]);
  }, [queryClient, conversationId]);

  const failed = useCallback((cause: Error, next: Retry) => {
    // An abort is this thread being closed, not something that went wrong with
    // it. There is nobody left to tell, and nothing to offer to retry.
    if (cause.name === "AbortError") return;
    setError(cause.message);
    setRetry(next);
    setConnectionLost(/reach|offline|connection/i.test(cause.message));
    setStreamingText("");
    // A failed stream can leave local interrupt knowledge behind the durable
    // server state. Fall back to a fresh thread-state read so the composer and
    // HITL card cannot remain permanently out of sync.
    setInterrupts(null);
    void queryClient.invalidateQueries({ queryKey: ["agent-state", conversationId] });
    // The banner itself is role="alert"; announcing it again would double up.
    setAnnouncement("");
  }, [conversationId, queryClient]);

  const availableInterrupts = useMemo(
    () => interrupts ?? (agentState.data ? openInterrupts(agentState.data) : []),
    [agentState.data, interrupts],
  );
  const fallbackInterrupt = useMemo(() => {
    const widgetIds = new Set(messages.flatMap((message) => message.widgets.map((widget) => widget.id)));
    return availableInterrupts.find((interrupt) => !interrupt.widgetId || !widgetIds.has(interrupt.widgetId)) ?? null;
  }, [availableInterrupts, messages]);

  const updateActivity = useCallback((activity: AgentActivity) => {
    setAgentActivities((current) => {
      const index = current.findIndex((item) => item.id === activity.id);
      if (index === -1) return [...current, activity];
      return current.map((item, itemIndex) => itemIndex === index ? activity : item);
    });
  }, []);

  const agentRunCallbacks = useMemo(() => ({
    onRunCreated: (runId: string) => {
      setActiveRunId(runId);
      setStoppingRun(false);
      setStreamingText("");
    },
    onPhase: (phase: AgentRunPhase) => setRunPhase(phase),
    onActivity: updateActivity,
    onReasoning: (summary: string) => setReasoningSummary(summary),
    onText: (text: string) => setStreamingText(text),
  }), [updateActivity]);

  const chatMutation = useMutation({
    mutationFn: ({ id, text }: { id: string; text: string }) => sendAgentMessage(id, text, agentRunCallbacks, inFlight.current?.signal),
    onMutate: () => { setAgentActivities([]); setReasoningSummary(""); setStreamingText(""); setAnnouncement("fyn AI is working on your message."); },
    onSuccess: (result) => {
      setAgentActivities([]);
      setActiveRunId(null);
      setStoppingRun(false);
      setInterrupts(result.interrupts);
      succeeded(result.response);
    },
    // A message that never reached the server should not look delivered: drop
    // the bubble and put the text back where the user can send it again.
    onError: (cause: Error, variables) => {
      setAgentActivities([]);
      setActiveRunId(null);
      setStoppingRun(false);
      setMessages((current) => current.filter((message) => !(message.id.startsWith("optimistic-") && message.content === variables.text)));
      if (cause.name === "AbortError") return;
      setInput((current) => current || variables.text);
      failed(cause, { kind: "chat", text: variables.text });
    },
  });
  const actionMutation = useMutation({
    mutationFn: ({ widgetId, action, payload, markUsed }: { widgetId: string; action: WidgetActionId; payload: Record<string, unknown>; markUsed: boolean }) => sendAgentAction(
      conversationId,
      widgetId,
      action,
      payload,
      markUsed,
      availableInterrupts.find((interrupt) => interrupt.widgetId === widgetId),
      agentRunCallbacks,
      inFlight.current?.signal,
    ),
    onSuccess: (result, variables) => {
      setPendingWidget(null);
      setActiveRunId(null);
      setStoppingRun(false);
      setInterrupts(result.interrupts);
      // Locking the card belongs to the success path; a failed action has to
      // stay clickable or the user is stuck until a reload.
      if (variables.markUsed) setUsedWidgets((current) => new Set(current).add(variables.widgetId));
      succeeded(result.response);
    },
    onError: (cause: Error, variables) => {
      setPendingWidget(null);
      setActiveRunId(null);
      setStoppingRun(false);
      failed(cause, { kind: "action", ...variables });
    },
  });
  const interruptMutation = useMutation({
    mutationFn: ({ interrupt, response }: {
      interrupt: FynInterrupt;
      response: { status: "resolved"; payload: unknown } | { status: "cancelled" };
    }) => resumeAgentInterrupt(
      conversationId,
      interrupt,
      response,
      agentRunCallbacks,
      inFlight.current?.signal,
    ),
    onSuccess: (result) => {
      setActiveRunId(null);
      setStoppingRun(false);
      setInterrupts(result.interrupts);
      succeeded(result.response);
    },
    onError: (cause: Error) => {
      setActiveRunId(null);
      setStoppingRun(false);
      failed(cause, null);
    },
  });
  const uploadMutation = useMutation({
    mutationFn: (file: File) => uploadCsv(conversationId, file, (percent) => setUpload({ name: file.name, percent }), inFlight.current?.signal),
    onMutate: (file) => setUpload({ name: file.name, percent: 0 }),
    onSuccess: (result, file) => {
      setUpload(null);
      setMessages((current) => [...current, { id: `upload-${Date.now()}`, role: "user", content: `Uploaded ${file.name}`, widgets: [], citations: [], created_at: new Date().toISOString() }]);
      succeeded(result.agentResponse);
    },
    onError: (cause: Error, file) => { setUpload(null); failed(cause, { kind: "upload", file }); },
  });

  // Bound to the mutations' stable members rather than to the result objects,
  // which react-query rebuilds on every render — the same reason the rail binds
  // to `fetchNextPage` below. Handlers that closed over the whole object would
  // change identity every render and the memoised thread would never bail out.
  const { mutate: startChat, isPending: chatPending } = chatMutation;
  const { mutate: startAction, isPending: actionPending } = actionMutation;
  const { mutate: resolveInterrupt, isPending: interruptPending } = interruptMutation;
  const { mutate: startUpload, isPending: uploadPending } = uploadMutation;

  const reconnectingRun = useRef<string | null>(null);
  useEffect(() => {
    if (!agentState.data) return;
    const discovered = agentState.data.activeRun;
    if (!discovered || chatPending || actionPending || interruptPending || activeRunId === discovered.id || reconnectingRun.current === discovered.id) return;
    reconnectingRun.current = discovered.id;
    setAgentActivities([]);
    setReasoningSummary("");
    reconnectAgentRun(conversationId, discovered.id, agentRunCallbacks, inFlight.current?.signal)
      .then((result) => {
        setAgentActivities([]);
        setActiveRunId(null);
        setStoppingRun(false);
        setInterrupts(result.interrupts);
        succeeded(result.response);
      })
      .catch((cause: Error) => {
        setAgentActivities([]);
        setActiveRunId(null);
        setStoppingRun(false);
        if (cause.name !== "AbortError") failed(cause, null);
      })
      .finally(() => { reconnectingRun.current = null; });
  }, [activeRunId, actionPending, agentRunCallbacks, agentState.data, chatPending, conversationId, failed, interruptPending, succeeded]);

  // Declared above the handlers below rather than beside the render: they are
  // dependencies of those handlers now, and a `const` read from a dependency
  // list has to already exist by the time the list is evaluated.
  const agentRunning = Boolean(activeRunId) && runPhase !== "interrupted" && runPhase !== "succeeded" && runPhase !== "failed";
  const pausedForInterrupt = availableInterrupts.length > 0;
  const busy = switchingConversation || chatPending || actionPending || interruptPending || uploadPending || agentRunning;
  const activeInteractionWidgetId = useMemo(() => activeWidgetId(messages), [messages]);
  const interruptWidgetId = useMemo(
    () => availableInterrupts.find((interrupt) => interrupt.widgetId === activeInteractionWidgetId)?.widgetId ?? null,
    [activeInteractionWidgetId, availableInterrupts],
  );
  const activeWidgetFocusKey = activeInteractionWidgetId && messages.at(-1)?.role === "assistant"
    ? `${messages.at(-1)?.id}:${activeInteractionWidgetId}`
    : null;
  const updateJumpControl = useCallback((visible: boolean, scrolling = false) => {
    const control = jumpToLatestRef.current;
    if (!control) return;
    control.dataset.visible = String(visible);
    control.dataset.scrolling = String(visible && scrolling);
    control.inert = !visible;
    control.setAttribute("aria-hidden", String(!visible));
  }, []);
  const stopFollowingForHitl = useCallback(() => {
    readerScrolled.current = true;
    setAtBottom(false);
    updateJumpControl(false);
  }, [updateJumpControl]);

  useLayoutEffect(() => updateJumpControl(false), [conversationId, updateJumpControl]);

  const stopAgent = useCallback(() => {
    if (!activeRunId || stoppingRun) return;
    setStoppingRun(true);
    setAnnouncement("Stopping fyn AI after the current safe operation.");
    void cancelAgentRun(activeRunId).catch((cause: Error) => {
      setStoppingRun(false);
      failed(cause, null);
    });
  }, [activeRunId, failed, stoppingRun]);

  async function copyConversationLink() {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setLinkCopied(true);
      // Cleared before it is replaced, so pressing twice does not leave an
      // earlier timer to reset the label out from under the later one, and
      // cancelled on unmount so it cannot fire into a thread that has gone.
      window.clearTimeout(copiedTimer.current);
      copiedTimer.current = window.setTimeout(() => setLinkCopied(false), 1800);
    } catch {
      failed(new Error("The link couldn’t be copied. Copy it from the address bar instead."), null);
    }
  }

  // The three handlers the transcript is given are wrapped so their identity
  // survives a keystroke; that is what lets the memoised thread below bail out.
  // The dependencies are the real ones — they change when a widget is used or a
  // request lands, which is precisely when the thread should redraw.
  const sendPrompt = useCallback((value: string) => {
    const text = value.trim();
    if (!text || chatPending || actionPending || uploadPending || agentRunning || pausedForInterrupt) return;
    setInput(""); setError(null); setRetry(null);
    readerScrolled.current = false;
    setAtBottom(true);
    updateJumpControl(false);
    setMessages((current) => [...current, { id: `optimistic-${Date.now()}`, role: "user", content: text, widgets: [], citations: [], created_at: new Date().toISOString() }]);
    startChat({ id: conversationId, text });
  }, [chatPending, actionPending, uploadPending, agentRunning, pausedForInterrupt, startChat, conversationId, updateJumpControl]);

  function submit(event?: FormEvent) {
    event?.preventDefault();
    sendPrompt(input);
  }

  const attach = useCallback((file: File | undefined) => {
    if (!file || busy) return;
    const problem = csvProblem(file);
    if (problem) { failed(new Error(problem), null); return; }
    setError(null);
    readerScrolled.current = false;
    setAtBottom(true);
    updateJumpControl(false);
    startUpload(file);
  }, [busy, failed, startUpload, updateJumpControl]);

  const handleWidgetAction = useCallback<WidgetAction>((widgetId, action, payload, options) => {
    // Per-row widgets (avoidable expenses, the calculators) opt out of the lock
    // so one decision does not retire every other control on the card.
    const markUsed = options?.markUsed !== false;
    if (widgetId !== activeInteractionWidgetId || pendingWidget || (markUsed && usedWidgets.has(widgetId))) return;
    setError(null);
    setPendingWidget(widgetId);
    startAction({ widgetId, action, payload, markUsed });
  }, [activeInteractionWidgetId, pendingWidget, usedWidgets, startAction]);

  const cancelWidgetInterrupt = useCallback((widgetId: string) => {
    if (interruptPending) return;
    const current = availableInterrupts.find((interrupt) => interrupt.widgetId === widgetId);
    if (current) resolveInterrupt({ interrupt: current, response: { status: "cancelled" } });
  }, [availableInterrupts, interruptPending, resolveInterrupt]);

  const retryLast = useCallback(() => {
    if (!retry) return;
    setError(null);
    if (retry.kind === "chat") sendPrompt(retry.text);
    if (retry.kind === "action") { setPendingWidget(retry.widgetId); startAction(retry); }
    if (retry.kind === "upload") startUpload(retry.file);
    setRetry(null);
  }, [retry, sendPrompt, startAction, startUpload]);

  // Whether the reader is following the conversation, which is not the same as
  // where the scrollbar happens to be. Rows measure themselves after they mount
  // and charts arrive a second late; both move the floor without anybody
  // touching the page. Treating that as "they scrolled up" is what left a
  // refreshed thread parked short of its own latest reply.
  //
  // So the thread only stops following when the reader actually drives it.
  const takeScrollControl = () => {
    readerScrolled.current = true;
    setAtBottom(false);
  };

  const noteWheelScroll = (event: React.WheelEvent<HTMLDivElement>) => {
    // A downward wheel at the physical end is a no-op and should not turn off
    // following. Upward intent, however, must win before the scroll event and
    // any pending ResizeObserver callback run.
    if (event.deltaY < 0) takeScrollControl();
  };

  const noteScrollKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowUp" || event.key === "PageUp" || event.key === "Home") takeScrollControl();
  };

  function trackScroll(event: React.UIEvent<HTMLDivElement>) {
    const node = event.currentTarget;
    window.clearTimeout(scrollSettleTimer.current);
    updateHeaderForScroll(node.scrollTop, readerScrolled.current);
    const distanceFromBottom = Math.max(0, node.scrollHeight - node.scrollTop - node.clientHeight);
    const viewportHeight = typeof window === "undefined" ? node.clientHeight : window.innerHeight;
    const jumpVisible = Boolean(
      readerScrolled.current
      && distanceFromBottom > viewportHeight * JUMP_TO_LATEST_VIEWPORT_RATIO
    );
    updateJumpControl(jumpVisible, true);
    scrollSettleTimer.current = window.setTimeout(
      () => updateJumpControl(jumpVisible),
      SCROLL_SETTLE_MS,
    );
    const reachedEnd = transcriptScrollRef.current?.isAtEnd(1) ?? distanceFromBottom <= 1;
    // Reader intent has priority over the generous follow zone. Previously a
    // turn could remain `atBottom` for the first 120px of an upward scroll, so
    // a row measurement or late chart resize pulled it back toward the dock.
    if (readerScrolled.current && !reachedEnd) {
      setAtBottom(false);
      return;
    }
    if (reachedEnd) {
      readerScrolled.current = false;
      setAtBottom(true);
      updateJumpControl(false);
      return;
    }
    // Keep automatic following off until the virtualiser confirms the physical
    // end. Button visibility is intentionally separate and uses the 90vh gap.
    setAtBottom(false);
  }

  // Let the virtualiser own end placement. It knows which estimates are still
  // unresolved and reconciles the target as those rows are measured.
  const scrollToEnd = useCallback((behavior: ScrollBehavior) => {
    transcriptScrollRef.current?.scrollToEnd(behavior);
  }, []);

  function jumpToLatest(event: ReactMouseEvent<HTMLButtonElement>) {
    readerScrolled.current = false;
    updateJumpControl(false);
    transcriptScrollRef.current?.scrollToMessage(
      event.currentTarget.dataset.targetMessageId,
      prefersReducedMotion() ? "auto" : "smooth",
    );
  }

  const toggleCitations = useCallback((messageId: string) => {
    setOpenCitations((current) => {
      const next = new Set(current);
      if (next.has(messageId)) next.delete(messageId); else next.add(messageId);
      return next;
    });
  }, []);

  // A conversation has not started until you have said something — the backend
  // seeds an assistant greeting, so message count alone never reports blank.
  const focusedMode = !loadingThread && !messages.some((message) => message.role === "user");
  // Growing the box pushes the tail of the thread up, and the tail is what you
  // were reading — so the reader is carried with it, but only if they were
  // already at the end. Instantly, not smoothly: the composer changing shape
  // under the thread is placement, not something arriving to watch.
  const dockRef = useDockHeight(!focusedMode, () => {
    if (!scrollRef.current || !atBottom || readerScrolled.current) return;
    scrollToEnd("auto");
  });

  // Follow the conversation only while the reader is already at the end of it,
  // so scrolling back through history is not undone by the next stream tick.
  // The dock's own growth is handled above, by the observer that measures it.
  //
  // There are two ways to arrive at the end of a transcript and only one of
  // them is movement. A reply landing while you watch is worth animating; a
  // thread you just opened is simply already at its end, so it is placed there
  // before the first paint rather than scrolled there in front of you.
  // Content that arrives after the scroll has already settled: a chart embeds a
  // second late, an image decodes, a row is measured taller than it was
  // estimated. Each of those grows the thread underneath a reader who was at
  // the end of it, so while they are still following, the end follows too.
  useEffect(() => {
    const content = contentRef.current;
    // Re-subscribed when the reader stops or starts following rather than
    // reading that through a ref: it changes when they scroll away, which is
    // rare, and an observer is cheap to rebuild.
    if (focusedMode || !atBottom || !content || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const node = scrollRef.current;
      if (!node || readerScrolled.current) return;
      if (node.scrollHeight - node.scrollTop - node.clientHeight > 1) {
        scrollToEnd("auto");
      }
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [focusedMode, atBottom, scrollToEnd]);

  const focusedArrival = useRef(activeWidgetFocusKey);
  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (!node || !atBottom) return;
    const transcript = `${messages.length}:${messages[messages.length - 1]?.id ?? ""}:${agentActivities.length}`;
    const arrived = arrivals.current !== null && arrivals.current !== transcript;
    arrivals.current = transcript;
    const hitlArrived = Boolean(activeWidgetFocusKey && focusedArrival.current !== activeWidgetFocusKey);
    focusedArrival.current = activeWidgetFocusKey;
    // `Transcript` aligns a new HITL widget itself. Following the generic end
    // here as well would win the same layout pass and hide the widget's top.
    if (hitlArrived) return;
    scrollToEnd(arrived && !prefersReducedMotion() ? "smooth" : "auto");
  }, [messages, agentActivities, activeWidgetFocusKey, atBottom, loadingThread, scrollToEnd]);

  function applyStarter(text: string) {
    setInput(text);
    // The textarea is controlled, so the caret can only be placed once the new
    // value has actually rendered.
    requestAnimationFrame(() => {
      const node = textRef.current;
      if (!node) return;
      node.focus();
      node.setSelectionRange(node.value.length, node.value.length);
    });
  }

  const title = initialData.active_conversation.title || "Financial check-in";
  const latestMessageId = streamingText ? "streaming-assistant" : messages.at(-1)?.id;
  const statusLabel = connectionLost
    ? "Can’t reach your financial data"
    : stoppingRun
      ? "Stopping after the current safe operation"
      : runPhase === "reconnecting"
        ? "Reconnecting to fyn AI"
        : agentRunning
          ? "fyn AI is working"
          : pausedForInterrupt
            ? "Waiting for your choice"
            : "Financial data connected";

  // No dependency list: the shell calls through this handle at arbitrary times,
  // so it has to hold this render's closures, not the first render's.
  useEffect(() => {
    handleRef.current = { sendPrompt, attach };
    return () => { handleRef.current = null; };
  });

  return <>
      <DocumentTitle title={title} fallback="Financial check-in" />
      <main id="main-content" className="relative flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-surface">
        <div
          ref={scrollRef}
          onScroll={trackScroll}
          onWheel={noteWheelScroll}
          onTouchMove={takeScrollControl}
          onKeyDown={noteScrollKey}
          className="conversation-scroll min-h-0 flex-1 overflow-y-auto"
        >
          <SiteHeader
            title={title}
            subtitle={<><span className={cn("size-1 rounded-full", connectionLost ? "bg-danger" : agentRunning || pausedForInterrupt ? "bg-secondary" : "bg-ink-muted")} />{statusLabel}</>}
            subtitleClassName={cn("mt-0.5 flex items-center gap-2", connectionLost && "font-medium text-danger-ink")}
            hidden={!headerVisible}
            navOpen={navOpen}
            onOpenNav={onOpenNav}
            end={<div className="flex items-center gap-1">
              <Tooltip><TooltipTrigger render={<Button type="button" variant="ghost" size="icon-lg" onClick={() => setMetricsOpen((open) => !open)} aria-expanded={metricsOpen} aria-label="Agent performance" className={cn("rounded-xl text-ink-muted", metricsOpen && "bg-secondary-tint text-secondary")} />}><LayoutDashboard /></TooltipTrigger><TooltipContent>Agent performance</TooltipContent></Tooltip>
              <Tooltip><TooltipTrigger render={<Button type="button" variant="ghost" size="icon-lg" onClick={copyConversationLink} aria-label={linkCopied ? "Conversation link copied" : "Copy conversation link"} className="rounded-xl text-ink-muted" />}>{linkCopied ? <Check /> : <Copy />}</TooltipTrigger><TooltipContent>{linkCopied ? "Link copied" : "Copy conversation link"}</TooltipContent></Tooltip>
            </div>}
          />
          {loadingThread ? <div className="mx-auto flex min-h-[calc(100%-3.5rem)] w-full max-w-[var(--column-w)] flex-col px-4 pt-8 pb-10 sm:px-6 sm:pt-12"><ThreadSkeleton /></div> : focusedMode ? <div className="leaf mx-auto flex min-h-[calc(100%-3.5rem)] w-full max-w-[34rem] flex-col justify-center px-4 py-12 sm:px-6">
            <h2 className="leaf-title">What happened?</h2>
            <div className="mt-6"><Composer variant="focused" value={input} onValueChange={setInput} onSubmit={submit} onStop={stopAgent} textRef={textRef} fileRef={fileRef} onAttach={attach} busy={busy} sending={chatPending} running={agentRunning} stopping={stoppingRun} paused={pausedForInterrupt} disabled={switchingConversation} dragging={dragging} upload={upload} /></div>
            {error ? <div role="alert" className="mt-3 flex flex-wrap items-center gap-3 gap-2 rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note leading-5 text-danger-ink"><TriangleAlert className="shrink-0" /><span className="min-w-0 flex-1">{error}</span>{retry ? <Button type="button" variant="outline" size="lg" onClick={retryLast} className="rounded-xl border-danger-line text-danger-ink hover:bg-danger-tint"><RotateCcw size={14} /> Try again</Button> : null}</div> : null}
            <p className="leaf-band mt-9">Try</p>
            <div className="mt-1">{STARTERS.map((starter) => <button key={starter} type="button" onClick={() => applyStarter(starter)} className="leaf-example"><span aria-hidden className="ledger-mark" />{starter}</button>)}</div>
          </div> : <div ref={contentRef} className="mx-auto flex min-h-[calc(100%-3.5rem)] w-full max-w-[var(--column-w)] flex-col px-4 pt-8 pb-10 sm:px-6 sm:pt-12">
            <Transcript
              messages={messages}
              agentActivities={agentActivities}
              reasoningSummary={reasoningSummary}
              streamingText={streamingText}
              streaming={agentRunning}
              busy={busy}
              usedWidgets={usedWidgets}
              pendingWidget={pendingWidget}
              openCitations={openCitations}
              activeWidget={activeInteractionWidgetId}
              cancelWidget={interruptWidgetId}
              activeWidgetFocusKey={activeWidgetFocusKey}
              error={error}
              retry={retry}
              onAction={handleWidgetAction}
              onCancelWidget={cancelWidgetInterrupt}
              onActiveWidgetFocus={stopFollowingForHitl}
              onToggleCitations={toggleCitations}
              onRetry={retryLast}
              scrollRef={scrollRef}
              scrollHandleRef={transcriptScrollRef}
            />
          </div>}
        </div>

        {metricsOpen ? <section aria-label="Agent performance" className="absolute top-16 right-3 z-40 w-[min(24rem,calc(100%-1.5rem))] overflow-hidden rounded-2xl border border-line bg-surface shadow-[var(--shadow-overlay)] sm:right-6">
          <div className="flex items-center justify-between border-b border-line px-4 py-3">
            <div><h2 className="text-control font-semibold text-ink-body">Agent performance</h2><p className="text-[11px] text-ink-muted">Measured for this thread</p></div>
            <Button type="button" variant="ghost" size="icon" onClick={() => setMetricsOpen(false)} aria-label="Close agent performance"><X size={16} /></Button>
          </div>
          <AgentMetricsPanel metrics={agentMetrics.data} loading={agentMetrics.isLoading} failed={agentMetrics.isError} />
        </section> : null}

        <p aria-live="polite" aria-atomic className="sr-only">{announcement}</p>

        {!focusedMode ? <>
          {/* Sits just above the composer rather than inside it, so appearing
              cannot change the composer's height and jolt the very scroll
              position it exists to restore. */}
          <div ref={jumpToLatestRef} data-visible="false" data-scrolling="false" aria-hidden="true" inert style={{ bottom: `calc(var(--dock-h) + 0.75rem)` }} className="jump-to-latest pointer-events-none absolute inset-x-0 z-20 flex justify-center px-3 sm:px-6"><Button type="button" data-target-message-id={latestMessageId} onClick={jumpToLatest} variant="outline" className="pointer-events-auto rounded-full shadow-[var(--shadow-overlay)]"><ArrowDown size={14} /> Jump to latest</Button></div>
          <div ref={dockRef} className="entry-dock z-20 shrink-0 px-3 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:px-6 sm:pt-4 sm:pb-4">
            {fallbackInterrupt ? <InterruptFallback interrupt={fallbackInterrupt} busy={interruptPending} onResolve={(response) => resolveInterrupt({ interrupt: fallbackInterrupt, response })} /> : null}
            <Composer variant="docked" value={input} onValueChange={setInput} onSubmit={submit} onStop={stopAgent} textRef={textRef} fileRef={fileRef} onAttach={attach} busy={busy} sending={chatPending} running={agentRunning} stopping={stoppingRun} paused={pausedForInterrupt} disabled={switchingConversation} dragging={dragging} upload={upload} />
          </div>
        </> : null}
      </main>
    </>;
}

type ShellValue = { navOpen: boolean; openNav: () => void; switching: boolean; dragging: boolean; handleRef: RefObject<ThreadHandle | null>; conversations: ConversationSummary[] };
const ShellContext = createContext<ShellValue | null>(null);

export function useWorkspaceShell() {
  const value = useContext(ShellContext);
  if (!value) throw new Error("This view must be rendered inside WorkspaceShell.");
  return value;
}

/** App chrome, and the reason it lives in a layout rather than in the page.
 *
 *  `ConversationWorkspace` deliberately re-seeds thread state from the freshly
 *  loaded conversation. The router keeps this shell in the stable `/c` parent,
 *  so the rail retains its scroll position, transitions, and DOM node while the
 *  selected thread underneath changes. */
export function WorkspaceShell({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const pathname = useLocation().pathname;
  const queryClient = useQueryClient();
  const conversationMatch = useMatch(appRoutePatterns.conversation);
  const conversationId = conversationMatch?.params.conversationId ?? "";
  const initial = useQuery({ queryKey: ["bootstrap"], queryFn: bootstrap });
  const signedOut = useSignInGuard(initial.error);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [switchingFrom, setSwitchingFrom] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [navError, setNavError] = useState<string | null>(null);
  // Reported up by the open thread. The rail needs it because two of its
  // controls send messages, and a message cannot be sent during a run.
  const [creating, setCreating] = useState(false);
  // Threads held back from the rail: those inside an undo window, and those past
  // it and being erased. Withholding covers both, so a row never flickers back
  // between the window closing and the server confirming.
  const [withheld, setWithheld] = useState<string[]>([]);
  const pending = useRef(new Map<string, { conversation: ConversationSummary; wasOpen: boolean; undone: boolean }>());
  const isDesktop = useMediaQuery("(min-width: 768px)");
  const thread = useRef<ThreadHandle | null>(null);
  const switching = switchingFrom === conversationId;
  const closeSettings = useCallback(() => setSettingsOpen(false), []);
  const openNav = useCallback(() => setSidebarOpen(true), []);

  const history = useInfiniteQuery({
    queryKey: ["conversations"],
    queryFn: ({ pageParam }) => listConversations(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextCursor,
    // Bootstrap creates the first conversation when there is none, so asking for
    // page one before it answers can report an empty history that isn't.
    enabled: Boolean(initial.data),
  });
  const conversations = useMemo(() => {
    const held = new Set(withheld);
    return (history.data?.pages.flatMap((page) => page.items) ?? []).filter((item) => !held.has(item.id));
  }, [history.data, withheld]);
  // Bound to the query's stable fetcher rather than the query object, so the
  // end-of-list observer is not torn down and rebuilt on every render.
  const { fetchNextPage } = history;
  const loadMore = useCallback(() => { void fetchNextPage(); }, [fetchNextPage]);

  // Opening a thread you have hovered should be a paint, not a request. The
  // transcript is the largest thing the app fetches, and the pointer travelling
  // to a rail entry is several hundred milliseconds of free warning.
  // `prefetchQuery` is a no-op when the thread is already cached and fresh, so
  // sweeping the pointer down the rail does not stampede the API.
  const prefetchConversation = useCallback((id: string) => {
    if (!id) return;
    void queryClient.prefetchQuery({
      queryKey: ["conversation", id],
      queryFn: () => loadConversation(id),
      staleTime: 15_000,
    });
  }, [queryClient]);

  const erase = useMutation({
    mutationFn: deleteConversation,
    onSuccess: async (_result, id) => {
      // The transcript must not survive in the client cache either.
      queryClient.removeQueries({ queryKey: ["conversation", id] });
      await Promise.all([queryClient.invalidateQueries({ queryKey: ["bootstrap"] }), queryClient.invalidateQueries({ queryKey: ["conversations"] })]);
    },
    // Nothing was deleted, so the row belongs back in the rail — which happens
    // by itself once it stops being withheld.
    onError: (cause: Error) => setNavError(cause.message),
    onSettled: (_result, _cause, id) => setWithheld((current) => current.filter((item) => item !== id)),
  });

  // A slip closing resolves its window: undone puts the thread back, anything
  // else — the timer running out, or the slip being dismissed — erases it. Held
  // in a ref because the toast calls this long after the render that opened it.
  const resolve = useRef<(id: string) => void>(() => {});
  useEffect(() => {
    resolve.current = (id: string) => {
      const removal = pending.current.get(id);
      if (!removal) return;
      pending.current.delete(id);
      if (!removal.undone) { erase.mutate(id); return; }
      setWithheld((current) => current.filter((item) => item !== id));
      if (!removal.wasOpen || id === conversationId) return;
      setSwitchingFrom(conversationId);
      navigate(appPaths.conversation(id), { replace: true, preventScrollReset: true });
    };
  });

  // A delete you walked away from is still a delete: send whatever is still
  // inside its window before the page goes, rather than silently keeping it.
  useEffect(() => {
    const flush = () => pending.current.forEach((removal, id) => { if (!removal.undone) flushConversationDeletion(id); });
    window.addEventListener("pagehide", flush);
    return () => window.removeEventListener("pagehide", flush);
  }, []);

  // Every handler the rail receives is stabilised, because memoising the rail
  // buys nothing while its props are fresh closures on each shell render.
  // Declared in dependency order: `const` bindings do not hoist.
  const closeNav = useCallback(() => setSidebarOpen(false), []);
  const openPage = useCallback((path: string) => {
    setSidebarOpen(false);
    setNavError(null);
    navigate(path, { preventScrollReset: true });
  }, [navigate]);
  const openSettings = useCallback(() => { setSettingsOpen(true); setSidebarOpen(false); }, []);
  const openProfile = useCallback(() => { setSidebarOpen(false); navigate(appPaths.profile); }, [navigate]);

  const openThreadId = useRef(conversationId);
  useEffect(() => { openThreadId.current = conversationId; }, [conversationId]);

  const startConversation = useCallback(async (mode: "push" | "replace" = "push") => {
    setSwitchingFrom(openThreadId.current);
    setCreating(true);
    setNavError(null);
    try {
      const conversation = await createConversation();
      setSidebarOpen(false);
      await Promise.all([queryClient.invalidateQueries({ queryKey: ["bootstrap"] }), queryClient.invalidateQueries({ queryKey: ["conversations"] })]);
      navigate(appPaths.conversation(conversation.id), { replace: mode === "replace", preventScrollReset: true });
    } catch { setSwitchingFrom(null); setNavError("The conversation couldn’t be started. Try again."); }
    finally { setCreating(false); }
  }, [navigate, queryClient]);

  const newConversation = useCallback(() => { void startConversation(); }, [startConversation]);

  const selectThread = useCallback((id: string) => {
    if (id === conversationId) { setSidebarOpen(false); return; }
    setSwitchingFrom(conversationId);
    setNavError(null);
    setSidebarOpen(false);
    navigate(appPaths.conversation(id), { preventScrollReset: true });
  }, [conversationId, navigate]);

  const deleteThread = useCallback((conversation: ConversationSummary) => {
    setNavError(null);
    const wasOpen = conversation.id === conversationId;
    pending.current.set(conversation.id, { conversation, wasOpen, undone: false });
    setWithheld((current) => [...current, conversation.id]);
    toast.add({
      id: conversation.id,
      title: "Deleted",
      description: conversation.title,
      timeout: UNDO_WINDOW_MS,
      actionProps: {
        children: "Undo",
        onClick: () => {
          const removal = pending.current.get(conversation.id);
          if (removal) removal.undone = true;
          toast.close(conversation.id);
        },
      },
      onClose: () => resolve.current(conversation.id),
    });
    if (!wasOpen) return;
    // Step onto the next thread in the rail. `replace`, so neither leaving nor
    // undoing leaves the deleted conversation sitting in the history stack.
    const next = conversations.find((item) => item.id !== conversation.id);
    setSwitchingFrom(conversationId);
    if (next) navigate(appPaths.conversation(next.id), { replace: true, preventScrollReset: true });
    else void startConversation("replace");
  }, [conversationId, conversations, navigate, startConversation]);

  // The rail is permanently visible once it docks, so drop the drawer state
  // rather than let it re-open behind the user on the way back down.
  useEffect(() => {
    const media = window.matchMedia("(min-width: 768px)");
    const closeWhenDocked = () => { if (media.matches) setSidebarOpen(false); };
    media.addEventListener("change", closeWhenDocked);
    return () => media.removeEventListener("change", closeWhenDocked);
  }, []);
  useEffect(() => {
    if (!sidebarOpen && !settingsOpen) return;
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") { setSidebarOpen(false); setSettingsOpen(false); } };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [sidebarOpen, settingsOpen]);


  if (signedOut) return <AppSkeleton label="Taking you to sign in…" />;
  if (initial.isError) return <WorkspaceUnreachable onRetry={() => initial.refetch()} retrying={initial.isFetching} />;

  return <ToastProvider toastManager={toast} limit={5}>
    <ShellContext.Provider value={{ navOpen: sidebarOpen, openNav, switching, dragging, handleRef: thread, conversations }}>
    <div className="h-dvh overflow-hidden bg-ground text-ink" onDragOver={(event) => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); setDragging(true); } }} onDragLeave={(event) => { if (event.currentTarget === event.target) setDragging(false); }} onDrop={(event) => { if (!event.dataTransfer.files.length) return; event.preventDefault(); setDragging(false); thread.current?.attach(event.dataTransfer.files[0]); }}>
      <div className="relative mx-auto grid h-full max-w-[1600px] md:grid-cols-[var(--rail-w)_1fr]">
        <button type="button" tabIndex={-1} aria-hidden onClick={() => setSidebarOpen(false)} className={cn("fixed inset-0 z-30 bg-ink/25 backdrop-blur-[2px] transition-opacity duration-300 md:hidden", sidebarOpen ? "opacity-100" : "pointer-events-none opacity-0")} />
        <ConversationRail
          conversations={conversations}
          activeId={conversationId}
          activePage={MONEY_PAGES.some((item) => item.path === pathname) ? pathname : null}
          user={initial.data?.user ?? null}
          open={sidebarOpen}
          docked={isDesktop}
          switching={creating}
          loading={history.isPending}
          loadingMore={history.isFetchingNextPage}
          hasMore={Boolean(history.hasNextPage)}
          onClose={closeNav}
          onOpenPage={openPage}
          onSelect={selectThread}
          onPrefetch={prefetchConversation}
          onDelete={deleteThread}
          onLoadMore={loadMore}
          onNew={newConversation}
          onOpenSettings={openSettings}
          onOpenProfile={openProfile}
        />
        {/* Rendered here rather than as `children` so it sits above the
            segment boundary and is re-seeded rather than rebuilt. `children`
            is the page, which renders nothing. */}
        {conversationId ? <ConversationThread conversationId={conversationId} /> : null}
        {children}
        {navError ? <div role="alert" className="fixed inset-x-0 bottom-4 z-50 mx-auto w-fit max-w-[90vw] rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note text-danger-ink shadow-[var(--shadow-overlay)]">{navError}</div> : null}
        {/* Deleting everything deletes the account itself, so there is nothing
            left to return to — the session is already void server-side. */}
        {settingsOpen ? <PrivacyDrawer onClose={closeSettings} onDeleted={() => { queryClient.clear(); navigate(appPaths.login, { replace: true }); }} /> : null}
      </div>
    </div>
    </ShellContext.Provider>
    {/* Clear of the composer, which owns the bottom of the column, and of the
        rail down the left: the stack lands in the empty band between them.
        It rides on the measured dock rather than a matching guess, so the two
        cannot drift apart when the box grows. */}
    <ToastPortal>
      <ToastViewport className="inset-x-3 bottom-[calc(var(--dock-h)+0.75rem)] mx-auto w-auto max-w-[20rem] md:right-auto md:left-[max(calc(var(--rail-w)+0.75rem),calc(50vw-var(--column-w)/2))] md:mx-0 md:w-full">
        <UndoToastList />
      </ToastViewport>
    </ToastPortal>
  </ToastProvider>;
}

function ConversationUnavailable({ onOpenLatest }: { onOpenLatest: () => void }) {
  return <div className="grid h-dvh place-items-center bg-ground p-6"><div role="alert" className="max-w-sm rounded-xl border border-line bg-surface p-6 text-center"><span className="mx-auto grid size-11 place-items-center rounded-[17px] bg-secondary-tint text-secondary"><MessageSquareText size={20} /></span><h1 className="mt-4 font-heading text-title font-semibold text-ink">Conversation unavailable</h1><p className="mt-2 text-control leading-6 text-ink-muted">This link is invalid, the conversation was deleted, or it belongs to another account.</p><Button type="button" onClick={onOpenLatest} size="lg" className="mt-4">Open latest conversation</Button></div></div>;
}

function WorkspaceUnreachable({ onRetry, retrying }: { onRetry: () => void; retrying: boolean }) {
  return <div className="grid h-dvh place-items-center bg-ground p-6"><div role="alert" className="max-w-sm rounded-xl border border-danger-line bg-surface p-6 text-center"><span className="mx-auto grid size-11 place-items-center rounded-[17px] bg-danger-tint text-danger"><TriangleAlert size={20} /></span><h1 className="mt-4 font-heading text-title font-semibold text-ink">We couldn’t load your workspace</h1><p className="mt-2 text-control leading-6 text-ink-muted">Nothing was lost. Check your connection and try again.</p><Button type="button" onClick={onRetry} disabled={retrying} size="lg" className="mt-4">{retrying ? <Loader2 className="animate-spin" /> : <RotateCcw />}{retrying ? "Trying again…" : "Try again"}</Button></div></div>;
}

/** The thread for one conversation. */
export function ConversationThread({ conversationId }: { conversationId: string }) {
  const navigate = useNavigate();
  const shell = useWorkspaceShell();
  const initial = useQuery({ queryKey: ["bootstrap"], queryFn: bootstrap });
  const conversation = useQuery({
    queryKey: ["conversation", conversationId],
    queryFn: () => loadConversation(conversationId),
    retry: false,
  });

  if (!initial.data) return <main className="min-h-0 bg-surface" />;
  // A thread being navigated away from — the one just deleted, say — is allowed
  // to stop loading without the shell accusing the link of being broken.
  if (conversation.isError && !shell.switching) return <ConversationUnavailable onOpenLatest={() => navigate(appPaths.conversation(initial.data.active_conversation.id), { replace: true })} />;

  // Keep the shell up while a thread loads instead of blanking the whole app.
  const known = shell.conversations.find((item) => item.id === conversationId);
  const activeConversation = conversation.data ?? { id: conversationId, title: known?.title ?? "Opening conversation", messages: [], updated_at: known?.updatedAt ?? "" };
  const prepared = { ...initial.data, active_conversation: activeConversation };
  return <ConversationWorkspace
    initialData={prepared}
    loadingThread={!conversation.data}
    navOpen={shell.navOpen}
    onOpenNav={shell.openNav}
    switching={shell.switching}
    dragging={shell.dragging}
    handleRef={shell.handleRef}
  />;
}

/** "/" is a redirect, not a place to work: mounting the composer here means
 *  anything typed before the redirect lands is thrown away with the unmount. */
export function FynWorkspace() {
  const navigate = useNavigate();
  const initial = useQuery({ queryKey: ["bootstrap"], queryFn: bootstrap });
  const signedOut = useSignInGuard(initial.error);
  useEffect(() => {
    if (initial.data) navigate(appPaths.overview, { replace: true, preventScrollReset: true });
  }, [initial.data, navigate]);

  if (signedOut) return <AppSkeleton label="Taking you to sign in…" />;
  if (initial.isError) return <WorkspaceUnreachable onRetry={() => initial.refetch()} retrying={initial.isFetching} />;
  return <AppSkeleton label="Opening your overview…" />;
}
