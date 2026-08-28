import { useInfiniteQuery, useMutation, useQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { ArrowDown, BrainCircuit, Check, CheckCircle2, ChartColumn, Copy, FileText, HandCoins, LayoutDashboard, Loader2, MessageSquareText, Paperclip, ReceiptText, RotateCcw, Route, SendHorizontal, Settings, ShieldCheck, Sparkles, Square, SquarePen, Tags, Trash2, TriangleAlert, Wrench, X } from "lucide-react";
import { createContext, FormEvent, memo, RefObject, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import { useLocation, useMatch, useNavigate } from "react-router";
import { SETTINGS_TOAST_PROBLEM, SETTINGS_TOAST_SAVED, SettingsRailIndex } from "@/components/settings-parts";
import { Button } from "@/components/ui/button";
import { DocumentTitle } from "@/components/document-title";
import { SiteHeader, useAutoHideSiteHeader } from "@/components/ui/site-header";
import { Scratchpad } from "@/components/scratchpad";
import { Textarea } from "@/components/ui/textarea";
import { Toast, ToastAction, ToastClose, ToastContent, ToastDescription, ToastPortal, ToastProvider, ToastTitle, ToastViewport, toast, UNDO_WINDOW_MS, useToastManager } from "@/components/ui/toast";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { WidgetRenderer, type WidgetProps } from "@/components/widget-renderer";
import { MarkdownMessage } from "@/components/widget-library/markdown-message";
import { DayDivider, localDayKey } from "@/components/day-divider";
import { MessageDeliveryTime } from "@/components/message-delivery-time";
import { MessageIdentifier } from "@/components/message-identifier";
import { UserMessage } from "@/components/user-message";
import { environment } from "@/config/environment";
import { bootstrap, cancelAgentRun, createCategory, createConversation, createSubcategory, deleteConversation, flushConversationDeletion, getPrivacyStatus, isUnauthorized, listConversations, loadAgentThreadState, loadConversation, renameConversation, openInterrupts, reconnectAgentRun, resumeAgentInterrupt, sendAgentAction, sendAgentMessage, uploadCsv, waitForAgentRelatedQuestions, type AgentActivity, type AgentRunPhase, type FynInterrupt } from "@/lib/api";
import { AgentRunTelemetry } from "@/lib/agent-telemetry";
import { formatBytes, formatMoney, readComposerEntry } from "@/lib/format";
import { takeSharedText } from "@/lib/share-target";
import { focusedTurnSpacerHeight, transcriptElementOffset } from "@/lib/transcript-scroll";
import { widgetTypeIds, type AgentResponse, type Bootstrap, type CategoryDirectoryOut, type ConversationOut, type ConversationPage, type ConversationSummary, type Message, type Widget, type WidgetActionId } from "@/lib/protocol";
import { useWorkspaceOverlay } from "@/components/ui/overlay";
import { useScrollEdges } from "@/lib/scroll-edges";
import { usePlainKey } from "@/lib/shortcuts";
import { cn } from "@/lib/utils";
import { subscribeToViewport } from "@/lib/viewport";
import { contractLimits } from "@/lib/generated/contracts";
import { activeWidgetId, completedWidgetIds, mergeAgentResponse, mergeMessageWidget, reconcileUsedWidgetIds, shouldAdoptServerTranscript, transcriptRevision } from "@/lib/widget-state";
import { interruptActionResolution, recoverInterruptWidget } from "@/lib/interrupt-widget";
import { appPaths, appRoutePatterns } from "@/routing/paths";

const MAX_UPLOAD_BYTES = contractLimits.csvUploadBytes;
const JUMP_TO_LATEST_VIEWPORT_RATIO = 0.9;
const SCROLL_SETTLE_MS = 150;
/** How close to the scroller's physical bottom still counts as "at the end".
 *  Fractional scroll positions under browser zoom can leave a settled scroll a
 *  pixel or two short of the exact maximum, and treating that as "the reader
 *  left" is what used to switch following off after every animation. */
const AT_END_SLACK_PX = 4;

type Retry =
  | { kind: "chat"; text: string }
  | { kind: "action"; widgetId: string; action: WidgetActionId; payload: Record<string, unknown>; markUsed: boolean }
  | { kind: "upload"; file: File }
  | null;

type CreateCategory = NonNullable<WidgetProps["onCreateCategory"]>;
type CreateSubcategory = NonNullable<WidgetProps["onCreateSubcategory"]>;

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

/** Reveal provider chunks at a short, bounded cadence instead of letting large
 *  network frames snap whole paragraphs onto the page. The first characters
 *  appear immediately and the adaptive catch-up stays under roughly half a
 *  second, so this is visual interpolation rather than artificial latency. */
function useSmoothedStreamingText(rawText: string) {
  const target = useRef(rawText);
  const [visible, setVisible] = useState("");
  useEffect(() => {
    target.current = rawText;
    const frame = window.requestAnimationFrame(() => {
      if (!rawText || prefersReducedMotion()) {
        setVisible(rawText);
        return;
      }
      setVisible((current) => {
        if (rawText.startsWith(current) && current) return current;
        return rawText.slice(0, Math.min(14, rawText.length));
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [rawText]);
  useEffect(() => {
    if (prefersReducedMotion()) return;
    const timer = window.setInterval(() => {
      setVisible((current) => {
        const latest = target.current;
        if (!latest || current === latest) return latest;
        if (!latest.startsWith(current)) return latest.slice(0, Math.min(14, latest.length));
        const remaining = latest.length - current.length;
        const step = Math.max(2, Math.ceil(remaining / 10));
        return latest.slice(0, Math.min(latest.length, current.length + step));
      });
    }, 42);
    return () => window.clearInterval(timer);
  }, []);
  return visible;
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

const RailEntry = memo(function RailEntry({ conversation, active, entryRef, onSelect, onPrefetch, onDelete, onRename }: {
  conversation: ConversationSummary;
  active: boolean;
  entryRef?: RefObject<HTMLButtonElement | null>;
  onSelect: (id: string) => void;
  onPrefetch: (id: string) => void;
  onDelete: (conversation: ConversationSummary) => void;
  onRename: (id: string, title: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(conversation.title);
  const rowRef = useRef<HTMLButtonElement | null>(null);
  // Keyboard exits hand focus back to the row; a pointer exit (blur) has
  // already placed it somewhere else deliberately.
  const refocusRow = () => requestAnimationFrame(() => rowRef.current?.focus());
  function commit() {
    setEditing(false);
    const title = draft.replace(/\s+/g, " ").trim();
    if (title && title !== conversation.title) onRename(conversation.id, title);
  }
  return <div className="ledger-row">
    {editing
      ? <input
        autoFocus
        value={draft}
        maxLength={160}
        onChange={(event) => setDraft(event.target.value)}
        // The old caption arrives selected: typing replaces, arrows amend.
        onFocus={(event) => event.currentTarget.select()}
        onKeyDown={(event) => {
          if (event.key === "Enter") { commit(); refocusRow(); }
          if (event.key === "Escape") { setEditing(false); refocusRow(); }
        }}
        onBlur={commit}
        aria-label={`Rename conversation: ${conversation.title}`}
        className="ledger-entry rename-line bg-surface text-ink-body outline-none"
      />
      : <button
        ref={(node) => { rowRef.current = node; if (entryRef) entryRef.current = node; }}
        type="button"
        aria-current={active ? "page" : undefined}
        onClick={() => onSelect(conversation.id)}
        // Renaming is the double-click, not a control: the title itself is the
        // affordance, the way it is in the thread header. The chat flow
        // ("rename this thread to …") remains the spoken and keyboard path.
        onDoubleClick={() => { setDraft(conversation.title); setEditing(true); }}
        onPointerEnter={() => onPrefetch(conversation.id)}
        onFocus={() => onPrefetch(conversation.id)}
        className="ledger-entry"
      >
        <span aria-hidden className="ledger-mark" />
        <span className="line-clamp-2" title={`${conversation.title}\nDouble-click to rename`}>{conversation.title}</span>
      </button>}
    <button type="button" onClick={() => onDelete(conversation)} aria-label={`Delete conversation: ${conversation.title}`} className="ledger-strike"><Trash2 size={14} /></button>
  </div>;
});

const MONEY_PAGES = [
  { label: "Overview", icon: LayoutDashboard, path: "/overview" },
  { label: "Dashboards", icon: ChartColumn, path: "/dashboards" },
  { label: "Transactions", icon: ReceiptText, path: "/transactions" },
  { label: "Personal lending", icon: HandCoins, path: "/loans" },
  { label: "Categories", icon: Tags, path: "/categories" },
] as const;

const ConversationRail = memo(function ConversationRail({ conversations, activeId, activePage, user, personalLendingAvailable, open, docked, switching, loading, loadingMore, hasMore, settingsOpen, onClose, onOpenPage, onSelect, onPrefetch, onDelete, onRename, onLoadMore, onNew, onLeaveSettings, onOpenSection, onOpenSettings, onOpenProfile }: {
  conversations: ConversationSummary[];
  activeId: string;
  activePage: string | null;
  /** Null until bootstrap answers; the rail draws its own placeholder. */
  user: Bootstrap["user"] | null;
  personalLendingAvailable: boolean;
  open: boolean;
  docked: boolean;
  switching: boolean;
  loading: boolean;
  loadingMore: boolean;
  hasMore: boolean;
  /** Settings borrows the rail: its sections replace the index below the
   *  header, and the way back out is the first row in their place. */
  settingsOpen: boolean;
  onClose: () => void;
  onOpenPage: (path: string) => void;
  onSelect: (id: string) => void;
  /** Warms a thread the pointer is heading for, so opening it is a paint. */
  onPrefetch: (id: string) => void;
  onDelete: (conversation: ConversationSummary) => void;
  onRename: (id: string, title: string) => void;
  onLoadMore: () => void;
  onNew: () => void;
  onLeaveSettings: () => void;
  onOpenSection: () => void;
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
    className={cn("ledger fixed top-[var(--viewport-offset)] left-0 z-40 flex h-[var(--app-height)] min-h-0 w-[min(var(--rail-w),85vw)] flex-col border-r border-line bg-ground transition-transform duration-300 ease-[cubic-bezier(0.32,0.72,0,1)] md:static md:h-full md:w-auto md:translate-x-0 md:transition-none", open ? "translate-x-0 shadow-[var(--shadow-overlay)]" : "-translate-x-full")}
  >
    <RailHeader creating={switching} onNew={onNew} onClose={onClose} />

    {settingsOpen ? <SettingsRailIndex onLeave={onLeaveSettings} onNavigate={onOpenSection} /> : <>
      <nav aria-label="Money pages" className="py-2">
        <p className="ledger-meta px-3 pt-1 pb-2">Money</p>
        {MONEY_PAGES.filter((item) => item.path !== appPaths.loans || personalLendingAvailable).map((item) => {
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
                  onRename={onRename}
                />)}
                <div ref={endRef} aria-hidden className="h-px" />
                {loadingMore ? <p role="status" className="ledger-meta py-4 px-3">Loading earlier</p> : null}
              </nav>}
        </div>
      </div>
    </>}

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

type FynResponseState = "turn" | "routing" | "reasoning" | "tool" | "responding" | "prepared" | "failed";

const responseStateLabels: Record<FynResponseState, string> = {
  turn: "Receiving your turn",
  routing: "Choosing the best path",
  reasoning: "Reasoning through the request",
  tool: "Working with your financial records",
  responding: "Writing the response",
  prepared: "Response prepared",
  failed: "Response needs attention",
};

function FynStateMark({ state, live = false }: { state: FynResponseState; live?: boolean }) {
  const Icon = state === "routing"
    ? Route
    : state === "reasoning"
      ? BrainCircuit
      : state === "tool"
        ? Wrench
        : state === "responding"
          ? MessageSquareText
          : state === "prepared"
            ? Check
            : state === "failed"
              ? TriangleAlert
              : Sparkles;
  return <span
    role="img"
    aria-label={responseStateLabels[state]}
    data-state={state}
    data-live={live || undefined}
    className="fyn-state-mark grid size-6 shrink-0 place-items-center rounded-full"
  >
    <Icon key={state} size={14} className="fyn-state-icon" />
  </span>;
}

function responseStateFor(steps: AgentActivity[], live: boolean, responding = false, failed = false): FynResponseState {
  if (failed || steps.some((step) => step.status === "failed")) return "failed";
  if (!live) return "prepared";
  if (responding) return "responding";
  const latest = steps.at(-1);
  if (!latest) return "turn";
  const stage = `${latest.stageId ?? ""} ${latest.id} ${latest.label}`.toLocaleLowerCase();
  if (latest.tool || /tool|execution|grounding|analysis/.test(stage)) return "tool";
  if (/operator|reason|model|compose/.test(stage)) return "reasoning";
  if (/classif|retriev|rout|select/.test(stage)) return "routing";
  return "turn";
}

/** One stable response header: the mark morphs with execution state, while the
 *  expandable run summary lives on the right instead of occupying a second
 *  full-width row above the answer. */
function AssistantByline({ state = "prepared", live = false, activity }: { state?: FynResponseState; live?: boolean; activity?: ReactNode }) {
  return <div className="assistant-byline mb-2 grid grid-cols-[auto_minmax(0,1fr)] items-start gap-x-3">
    <div className="flex min-h-8 items-center gap-2">
      <FynStateMark state={state} live={live} />
      <span className="text-meta font-semibold tracking-[0.08em] text-ink-muted uppercase">fyn AI</span>
    </div>
    {activity ? <div className="assistant-byline-activity min-w-0 justify-self-stretch">{activity}</div> : null}
  </div>;
}

/** One in-flight run as the stream has described it so far. `failureSummary`
 *  and `modelPassCount` are server-authored aggregates carried by every
 *  activity event — the client renders them, it never re-derives them. */
interface LiveAgentRun {
  steps: AgentActivity[];
  failureSummary: string | null;
  modelPassCount: number;
}

const idleAgentRun: LiveAgentRun = { steps: [], failureSummary: null, modelPassCount: 0 };

/** The reply forming in place: same byline and same run card the finished message
 *  will carry, in the same spot, so landing the answer collapses the run and
 *  fills in the prose rather than moving anything. */
function AgentActivityIndicator({ run, reasoningSummary, responding }: { run: LiveAgentRun; reasoningSummary: string; responding: boolean }) {
  // Rebuilt only when a step actually streams in. A fresh object every render
  // would be a fresh `widget` prop, and the run card would re-render along with
  // whatever else moved on the page.
  const widget: Widget = useMemo(() => {
    const latest = run.steps.at(-1);
    const fallback = latest?.detail || latest?.label || "Preparing a contextual answer";
    const summary = (run.failureSummary || reasoningSummary || fallback).replace(/\s+/g, " ").trim().slice(0, 320);
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
        steps: run.steps,
        modelPassCount: run.modelPassCount,
        // Folded rather than spread: `Math.max(...array)` passes every element as
        // an argument, which throws once a run is long enough. Runs are short, so
        // this is insurance rather than a fix.
        totalMs: run.steps.reduce((longest, activity) => Math.max(longest, activity.cumulativeMs), 0),
        live: true,
      },
      actions: [],
    };
  }, [run, reasoningSummary]);
  const state = responseStateFor(run.steps, true, responding, Boolean(run.failureSummary));
  return <div className="max-w-[680px]">
    <AssistantByline state={state} live activity={<WidgetRenderer widget={widget} disabled onAction={noAction} />} />
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

export { useWorkspaceOverlay };


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
  return toasts.map((slip) => slip.type === SETTINGS_TOAST_SAVED || slip.type === SETTINGS_TOAST_PROBLEM
    // Settings reports through the same stack, but a standing instruction that
    // changed is not an entry that was struck: no undo window, no ruled-out
    // line, just what changed and whether it took.
    ? <Toast key={slip.id} toast={slip} className="rounded-lg border-line bg-surface shadow-[var(--shadow-overlay)]">
      {/* Right padding clears the close, so a message long enough to wrap
          runs under nothing. */}
      <ToastContent className="pr-10">
        {slip.type === SETTINGS_TOAST_PROBLEM
          ? <TriangleAlert size={16} className="shrink-0 text-danger" />
          : <CheckCircle2 size={16} className="shrink-0 text-secondary" />}
        <ToastTitle className="min-w-0 text-control leading-[1.35] font-medium text-ink-body" />
        <ToastClose className="absolute top-2 right-2" />
      </ToastContent>
    </Toast>
    : <Toast
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

function InterruptFallback({ interrupt, busy, locationAllowed, onResolve }: {
  interrupt: FynInterrupt;
  busy: boolean;
  locationAllowed: boolean;
  onResolve: (response: { status: "resolved"; payload: unknown } | { status: "cancelled" }) => void;
}) {
  const recoveredWidget = recoverInterruptWidget(interrupt);
  if (recoveredWidget) return <div role="group" aria-label="Agent response required" className="mx-auto mb-2 w-full max-w-[var(--column-w)]">
    <WidgetRenderer
      widget={recoveredWidget}
      pending={busy}
      locationAllowed={locationAllowed}
      onAction={(widgetId, action, payload) => onResolve(interruptActionResolution(widgetId, action, payload))}
      onCancel={() => onResolve({ status: "cancelled" })}
    />
  </div>;

  return <div role="group" aria-label="Agent response required" className="hitl-card mx-auto mb-2 w-full max-w-[var(--column-w)] overflow-hidden rounded-lg border bg-surface">
    <div className="flex gap-2.5 px-3.5 py-3">
      <ShieldCheck size={17} className="mt-0.5 shrink-0 text-secondary" />
      <div className="min-w-0 flex-1">
        <p className="text-control font-semibold text-ink">Request needs to be restarted</p>
        <p className="mt-0.5 text-note leading-4 text-ink-muted">Its verified interaction surface is unavailable. Cancel it, then send the request again.</p>
      </div>
    </div>
    <div className="hitl-actions border-t border-line">
      <Button type="button" variant="ghost" disabled={busy} onClick={() => onResolve({ status: "cancelled" })}>Cancel</Button>
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
const ActiveWidgetBoundary = memo(function ActiveWidgetBoundary({ active, title, onMount, children }: {
  active: boolean;
  title: string;
  onMount: (target: HTMLElement) => boolean;
  children: ReactNode;
}) {
  const boundaryRef = useRef<HTMLDivElement>(null);
  const focusClaimed = useRef(false);
  useLayoutEffect(() => {
    const target = boundaryRef.current;
    if (!active || !target) {
      focusClaimed.current = false;
      return;
    }
    // A same-thread reconciliation can replace the focused transcript DOM
    // without changing this widget's identity. The browser then falls back to
    // <body>, but a dependency-keyed effect does not run again because the
    // action itself is unchanged. Reclaim only that dropped focus; never pull
    // focus away from another control the reader deliberately chose.
    const focusDropped = focusClaimed.current && document.activeElement === document.body;
    if (!focusClaimed.current || focusDropped) {
      focusClaimed.current = onMount(target);
    }
  });
  return <div
    ref={boundaryRef}
    role={active ? "group" : undefined}
    aria-label={active ? `Action required: ${title}` : undefined}
    data-active-widget={active || undefined}
    tabIndex={active ? -1 : undefined}
    className="scroll-mt-4"
  >{children}</div>;
});

const MessageArticle = memo(function MessageArticle({ message, focusedPrompt = false, focusedResponse = false, showAssistantByline = true, activeWidget, cancelWidget, usedWidgets, pendingWidget, busy, locationAllowed, citationsOpen, onToggleCitations, onAction, onCreateCategory, onCreateSubcategory, onCancelWidget, onActiveWidgetMount, onPostPrompt }: {
  message: Message;
  focusedPrompt?: boolean;
  focusedResponse?: boolean;
  showAssistantByline?: boolean;
  activeWidget: string | null;
  cancelWidget: string | null;
  usedWidgets: Set<string>;
  pendingWidget: string | null;
  busy: boolean;
  locationAllowed: boolean;
  citationsOpen: boolean;
  onToggleCitations: (messageId: string) => void;
  onAction: WidgetAction;
  onCreateCategory: CreateCategory;
  onCreateSubcategory: CreateSubcategory;
  onPostPrompt: (text: string) => void;
  onCancelWidget: (widgetId: string) => void;
  onActiveWidgetMount: (target: HTMLElement) => boolean;
}) {
  // The run trace is how the answer was reached, so it reads above the answer —
  // not appended after the widgets it produced, which is the order the backend
  // stores it in.
  const trace = message.widgets.find((widget) => widget.type === widgetTypeIds.agent_activity);
  const widgets = message.widgets.filter((widget) => widget.id !== trace?.id);
  const traceSteps = Array.isArray(trace?.data.steps) ? trace.data.steps as unknown as AgentActivity[] : [];
  const responseState = responseStateFor(traceSteps, false, false, traceSteps.some((step) => step.status === "failed"));
  return <article data-message-id={message.id} data-message-role={message.role} data-focused-prompt={focusedPrompt || undefined} data-focused-response={focusedResponse || undefined} className={cn("group", message.role === "user" ? "flex justify-end" : "max-w-[680px]")}>
    <div className={cn("min-w-0", message.role === "user" && "max-w-[82%]")}>
      {message.role === "assistant" && showAssistantByline ? <AssistantByline
        state={responseState}
        activity={trace ? <WidgetRenderer widget={trace} disabled onAction={noAction} /> : undefined}
      /> : null}
      {message.content ? message.role === "user"
        ? <UserMessage content={message.content} messageId={message.id} deliveredAt={message.delivered_at} />
        : <div className="transcript-answer break-words"><MarkdownMessage id={message.id}>{message.content}</MarkdownMessage></div>
      : null}
      {message.content && message.role !== "user" ? <div className="mt-1.5 pl-8">
        <div className="flex items-center gap-2 whitespace-nowrap">
          {message.delivered_at ? <>
            <MessageDeliveryTime deliveredAt={message.delivered_at} />
            <span aria-hidden className="text-meta text-ink-muted/70">·</span>
          </> : null}
          <MessageIdentifier messageId={message.id} />
          {message.citations.length ? <>
            <span aria-hidden className="text-meta text-ink-muted/70">·</span>
            <button type="button" data-inline-disclosure="true" aria-expanded={citationsOpen} onClick={() => onToggleCitations(message.id)} className="flex min-h-7 items-center gap-1.5 rounded-lg text-meta font-medium text-ink-muted hover:text-secondary"><FileText size={14} /> {message.citations.length} data source{message.citations.length === 1 ? "" : "s"}</button>
          </> : null}
        </div>
        {message.citations.length && citationsOpen ? <ul className="mt-2 space-y-1 rounded-lg border border-line bg-surface px-4 py-3">{message.citations.map((citation, index) => <li key={index} className="flex gap-2 text-meta leading-5 text-ink-muted"><span aria-hidden className="text-secondary">•</span><span><span className="font-medium text-ink-body">{typeof citation.label === "string" ? citation.label : "Recorded data"}</span>{typeof citation.entity_type === "string" ? ` · ${citation.entity_type.replaceAll("_", " ")}` : ""}</span></li>)}</ul> : null}
      </div> : null}
      {widgets.length ? <div className="mt-3 space-y-3 pl-0 sm:pl-8">{widgets.map((widget) => {
        const active = widget.id === activeWidget;
        const title = typeof widget.data.title === "string" && widget.data.title.trim() ? widget.data.title : "Follow-up";
        return <ActiveWidgetBoundary
          key={widget.id}
          active={active}
          title={title}
          onMount={onActiveWidgetMount}
        >
          <WidgetRenderer widget={widget} disabled={!active || usedWidgets.has(widget.id) || busy} pending={pendingWidget === widget.id} locationAllowed={locationAllowed} onCancel={widget.id === cancelWidget ? () => onCancelWidget(widget.id) : undefined} onAction={onAction} onCreateCategory={onCreateCategory} onCreateSubcategory={onCreateSubcategory} onPostPrompt={onPostPrompt} />
        </ActiveWidgetBoundary>;
      })}</div> : null}
    </div>
  </article>;
});

function buildTranscriptDayMarkers(messages: Message[]) {
  let previous: string | null = null;
  return messages.map((message) => {
    const key = localDayKey(message.created_at);
    const opensDay = key !== null && key !== previous;
    if (key !== null) previous = key;
    return opensDay ? message.created_at : null;
  });
}

/** The thread itself, held behind the same boundary and for the same reason.
 *  Every prop it takes is either state that does not move while you type or a
 *  callback held stable by the workspace, so a keystroke stops here. */
const Transcript = memo(function Transcript({ messages, agentRun, reasoningSummary, streamingText, streaming, busy, locationAllowed, usedWidgets, pendingWidget, openCitations, activeWidget, cancelWidget, activeWidgetFocusKey, error, retry, followEnd, focusedPromptIndex, onAction, onCreateCategory, onCreateSubcategory, onCancelWidget, onActiveWidgetReveal, onFocusedPromptReveal, onListHeightChanged, onToggleCitations, onRetry, onPostPrompt, scrollElement, scrollRef }: {
  messages: Message[];
  agentRun: LiveAgentRun;
  reasoningSummary: string;
  streamingText: string;
  streaming: boolean;
  busy: boolean;
  locationAllowed: boolean;
  usedWidgets: Set<string>;
  pendingWidget: string | null;
  openCitations: Set<string>;
  activeWidget: string | null;
  cancelWidget: string | null;
  activeWidgetFocusKey: string | null;
  error: string | null;
  retry: Retry;
  followEnd: boolean;
  focusedPromptIndex: number | null;
  onAction: WidgetAction;
  onCreateCategory: CreateCategory;
  onCreateSubcategory: CreateSubcategory;
  onPostPrompt: (text: string) => void;
  onCancelWidget: (widgetId: string) => void;
  onActiveWidgetReveal: (target: HTMLElement, focusInPlace?: boolean) => boolean;
  onFocusedPromptReveal: () => void;
  onListHeightChanged: () => void;
  onToggleCitations: (messageId: string) => void;
  onRetry: () => void;
  scrollElement: HTMLDivElement | null;
  scrollRef: RefObject<HTMLDivElement | null>;
}) {
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const turnContentRef = useRef<HTMLDivElement>(null);
  const focusSpacerRef = useRef<HTMLDivElement>(null);
  const focusedPromptPlaced = useRef<number | null>(null);
  const focusedPromptScrollStarted = useRef<number | null>(null);
  const promptFocusFrame = useRef(0);
  const focusedTurnLayout = useRef<(() => void) | null>(null);
  const focusedTurnContentHeight = useRef<{ index: number; height: number } | null>(null);

  // A restored React Query transcript can paint before its background refresh
  // adds turns that arrived after the cache snapshot. Moving only the external
  // scroller cannot reveal those rows until Virtuoso has selected them for its
  // rendered range, so explicitly keep the virtual range on the final item
  // while the workspace says the reader is following. As soon as wheel, touch,
  // keyboard, or scrollbar intent takes ownership `followEnd` becomes false
  // and history is left exactly where the reader put it.
  useLayoutEffect(() => {
    if (!followEnd || focusedPromptIndex !== null || messages.length === 0) return;
    virtuosoRef.current?.scrollToIndex({
      index: messages.length - 1,
      align: "end",
      behavior: "auto",
    });
  }, [focusedPromptIndex, followEnd, messages.length]);

  // A submitted question opens a fresh reading window: the question sits just
  // under the sticky header, the answer starts below it, and the unwritten
  // remainder is real scrollable space. As content arrives the spacer gives up
  // exactly the same height, keeping the viewport still until the answer fills
  // it. From that point the spacer is zero and the normal end follower carries
  // a reader who has not taken control.
  useLayoutEffect(() => {
    cancelAnimationFrame(promptFocusFrame.current);
    const spacer = focusSpacerRef.current;
    if (focusedPromptIndex === null) {
      if (spacer) spacer.style.height = "0px";
      focusedPromptPlaced.current = null;
      focusedPromptScrollStarted.current = null;
      focusedTurnContentHeight.current = null;
      return;
    }
    if (focusedPromptPlaced.current !== focusedPromptIndex && focusedPromptScrollStarted.current !== focusedPromptIndex) {
      focusedPromptPlaced.current = null;
      focusedPromptScrollStarted.current = null;
    }

    let attempts = 0;
    let stableFrames = 0;
    const place = () => {
      const scroller = scrollRef.current;
      const content = turnContentRef.current;
      const promptMessage = messages[focusedPromptIndex];
      const currentSpacer = focusSpacerRef.current;
      if (!scroller || !content || !promptMessage || !currentSpacer) return;
      const prompt = scroller.querySelector<HTMLElement>('[data-focused-prompt="true"]');
      if (!prompt) {
        if (followEnd && attempts === 0) {
          virtuosoRef.current?.scrollToIndex({ index: focusedPromptIndex, align: "start", behavior: "auto" });
        }
        if (++attempts < 12) promptFocusFrame.current = requestAnimationFrame(place);
        return;
      }

      const scrollerRect = scroller.getBoundingClientRect();
      const promptRect = prompt.getBoundingClientRect();
      const header = scroller.querySelector<HTMLElement>("[data-sticky-anchor]");
      const headerIsVisible = header ? !header.hasAttribute("inert") : true;
      const topInset = (headerIsVisible ? (header?.offsetHeight ?? 56) : 0) + 16;
      const desiredTop = scrollerRect.top + topInset;
      const turnContentHeight = Math.max(0, content.getBoundingClientRect().bottom - promptRect.top);
      const spacerRect = currentSpacer.getBoundingClientRect();
      // The transcript column has breathing room after the log. Include that
      // real trailing space in the window equation or the physical end lands
      // the prompt behind the sticky header by exactly the padding amount.
      const spacerEnd = scroller.scrollTop + spacerRect.bottom - scrollerRect.top;
      const trailingSpace = Math.max(0, scroller.scrollHeight - spacerEnd);
      const baseReserve = focusedTurnSpacerHeight(scroller.clientHeight, topInset, turnContentHeight + trailingSpace);
      const distanceFromEnd = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
      const currentReserve = Number.parseFloat(currentSpacer.style.height) || 0;
      const previousMeasurement = focusedTurnContentHeight.current;
      const contentDelta = previousMeasurement?.index === focusedPromptIndex
        ? turnContentHeight - previousMeasurement.height
        : 0;
      let reserve = previousMeasurement?.index === focusedPromptIndex
        ? Math.max(0, currentReserve - contentDelta)
        : baseReserve;
      focusedTurnContentHeight.current = { index: focusedPromptIndex, height: turnContentHeight };
      // When no content changed, displacement at the physical end can only be
      // a late virtual-row measurement. Absorb that exact difference once. A
      // content delta has already been countered one-for-one above; applying
      // both corrections would double it and create an oscillation.
      if (followEnd && distanceFromEnd <= AT_END_SLACK_PX && Math.abs(contentDelta) <= 0.5) {
        reserve = Math.max(0, reserve + promptRect.top - desiredTop);
      }
      const reserveChanged = Math.abs(currentReserve - reserve) > 0.5;
      if (reserveChanged) currentSpacer.style.height = `${reserve}px`;

      const alignmentDelta = Math.abs(promptRect.top - desiredTop);
      if (followEnd && focusedPromptPlaced.current === null) {
        const aligned = alignmentDelta <= 1;
        if (!aligned && focusedPromptScrollStarted.current !== focusedPromptIndex) {
          focusedPromptScrollStarted.current = focusedPromptIndex;
          onFocusedPromptReveal();
        }
        stableFrames = aligned ? stableFrames + 1 : 0;
        if (stableFrames >= 2) focusedPromptPlaced.current = focusedPromptIndex;
      }
      // Re-measure after changing the spacer even once the initial glide has
      // settled: shrinking it clamps the browser to a new maximum scrollTop,
      // and that clamp is the final input needed to converge on the exact top.
      const focusedWindowNeedsSettle = followEnd && reserve > 0 && (reserveChanged || alignmentDelta > 1);
      if (++attempts < 40 && (focusedPromptPlaced.current === null || focusedWindowNeedsSettle)) {
        promptFocusFrame.current = requestAnimationFrame(place);
      }
    };
    focusedTurnLayout.current = place;
    promptFocusFrame.current = requestAnimationFrame(place);
    return () => {
      cancelAnimationFrame(promptFocusFrame.current);
      if (focusedTurnLayout.current === place) focusedTurnLayout.current = null;
    };
  }, [agentRun, busy, error, focusedPromptIndex, followEnd, messages, onFocusedPromptReveal, reasoningSummary, scrollRef, streaming, streamingText]);

  const handleListHeightChanged = useCallback(() => {
    onListHeightChanged();
    cancelAnimationFrame(promptFocusFrame.current);
    promptFocusFrame.current = requestAnimationFrame(() => focusedTurnLayout.current?.());
  }, [onListHeightChanged]);

  useEffect(() => {
    const content = turnContentRef.current;
    if (focusedPromptIndex === null || !content || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(promptFocusFrame.current);
      promptFocusFrame.current = requestAnimationFrame(() => focusedTurnLayout.current?.());
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [focusedPromptIndex]);

  // A HITL response is a hand-off back to the person. Move both the viewport
  // and keyboard/screen-reader focus to that exact widget, even when the prior
  // (possibly very tall) widget left the reader far from the transcript's end.
  // The active turn can initially be outside the virtual window, so first ask
  // the virtualiser to mount its row and then focus the semantic boundary.
  // Start empty so an actionable card present on this transcript's first
  // committed render receives focus too. Initialising from the current key
  // made a remount between run completion and the interrupt silently treat a
  // newly arrived card as already focused.
  const focusedWidgetKey = useRef<string | null>(null);
  const focusFrame = useRef(0);
  useLayoutEffect(() => {
    cancelAnimationFrame(focusFrame.current);
    if (!activeWidgetFocusKey || !activeWidget) {
      focusedWidgetKey.current = null;
      return;
    }
    if (focusedWidgetKey.current === activeWidgetFocusKey) return;

    const rowIndex = messages.length - 1;
    let attempts = 0;
    let stableFrames = 0;
    let revealAuthorized = false;
    const reveal = () => {
      const scroller = scrollRef.current;
      if (!scroller) return;
      const target = scroller.querySelector<HTMLElement>('[data-active-widget="true"]');
      if (!target) {
        if (attempts === 0) virtuosoRef.current?.scrollToIndex({ index: rowIndex, align: "start", behavior: "auto" });
        // A long restored thread can need more than a handful of frames for
        // Virtuoso to replace its estimate and mount the final row. Keep this
        // bounded by roughly two seconds instead of silently abandoning the
        // keyboard hand-off after ~200 ms.
        if (++attempts < 120) focusFrame.current = requestAnimationFrame(reveal);
        return;
      }
      // A short answer is already inside the focused question's reserved
      // viewport. Focus its decision surface without moving the prompt; once
      // the answer is taller than the window, the existing exact alignment is
      // allowed to take over.
      if (focusedPromptIndex !== null) {
        const scrollerRect = scroller.getBoundingClientRect();
        const targetRect = target.getBoundingClientRect();
        const header = scroller.querySelector<HTMLElement>("[data-sticky-anchor]");
        const visibleTop = scrollerRect.top + (header?.hasAttribute("inert") ? 0 : (header?.offsetHeight ?? 56));
        if (targetRect.top >= visibleTop && targetRect.bottom <= scrollerRect.bottom) {
          if (onActiveWidgetReveal(target, true)) {
            focusedWidgetKey.current = activeWidgetFocusKey;
            return;
          }
          if (++attempts < 120) focusFrame.current = requestAnimationFrame(reveal);
          return;
        }
      }

      // `atBottom` can be briefly false while Virtuoso replaces estimates or
      // the focused-turn reserve settles. Reader intent, tracked synchronously
      // by the workspace callback, is the authority for whether focus may
      // move; a transient scroll measurement must not suppress the hand-off.
      if (!revealAuthorized) {
        if (!onActiveWidgetReveal(target)) {
          if (++attempts < 120) focusFrame.current = requestAnimationFrame(reveal);
          return;
        }
        revealAuthorized = true;
        focusedWidgetKey.current = activeWidgetFocusKey;
      }

      // The virtualizer remains the sole writer of this scroller's position.
      // A native smooth `scrollIntoView` captures coordinates before the old
      // HITL card has collapsed and the new row has been measured; its target
      // is stale as soon as either happens. Re-resolve the exact widget offset
      // for two stable frames instead. This also limits movement to the thread
      // scroller rather than every scrollable ancestor.
      // Reader intent can arrive between this animation frame being scheduled
      // and executed. The workspace rejects the reveal synchronously in that
      // case, before the virtualizer writes another scroll position.
      const offset = transcriptElementOffset(scroller, target);
      const aligned = Math.abs(scroller.scrollTop - offset) <= 1;
      if (!aligned) virtuosoRef.current?.scrollTo({ top: offset, behavior: "auto" });
      stableFrames = aligned ? stableFrames + 1 : 0;
      if (++attempts < 12 && stableFrames < 2) focusFrame.current = requestAnimationFrame(reveal);
    };
    focusFrame.current = requestAnimationFrame(reveal);
    return () => cancelAnimationFrame(focusFrame.current);
  }, [activeWidget, activeWidgetFocusKey, focusedPromptIndex, messages.length, onActiveWidgetReveal, scrollRef]);

  // Which rows open a day. A thread is read back long after it was written —
  // often across several sittings — so the transcript is dated: the first entry
  // carries its day, and the marker returns only where the calendar day
  // actually changes. Computed once per transcript rather than per row, because
  // this component re-renders on every streamed token.
  const dayMarkers = useMemo(() => buildTranscriptDayMarkers(messages), [messages]);

  const streamingMessage = useMemo<Message | null>(() => streamingText ? {
    id: "streaming-assistant",
    role: "assistant",
    content: streamingText,
    widgets: [],
    citations: [],
    created_at: new Date().toISOString(),
    delivered_at: "",
  } : null, [streamingText]);
  return <div role="log" aria-busy={streaming}>
    <div ref={turnContentRef}>
    {scrollElement ? <Virtuoso<Message>
      key={messages[0]?.id ?? "empty-transcript"}
      ref={virtuosoRef}
      customScrollParent={scrollElement}
      data={messages}
      computeItemKey={(index, message) => message?.id ?? messages[index]?.id ?? index}
      defaultItemHeight={220}
      initialItemCount={Math.min(messages.length, 8)}
      initialTopMostItemIndex={messages.length ? { index: "LAST", align: "end" } : undefined}
      increaseViewportBy={{ top: 1320, bottom: 1320 }}
      minOverscanItemCount={{ top: 6, bottom: 6 }}
      totalListHeightChanged={handleListHeightChanged}
      itemContent={(index, item) => {
        const message = item ?? messages[index];
        if (!message) return null;
        const opensDay = dayMarkers[index];
        return <div className="pb-6">
          {opensDay ? <DayDivider
            isoTime={opensDay}
            className={cn("mb-5", index > 0 && "mt-2")}
          /> : null}
          <MessageArticle
            message={message}
            focusedPrompt={index === focusedPromptIndex}
            focusedResponse={focusedPromptIndex !== null && index === focusedPromptIndex + 1 && message.role === "assistant"}
            activeWidget={activeWidget}
            cancelWidget={cancelWidget}
            usedWidgets={usedWidgets}
            pendingWidget={pendingWidget}
            busy={busy}
            locationAllowed={locationAllowed}
            citationsOpen={openCitations.has(message.id)}
            onToggleCitations={onToggleCitations}
            onAction={onAction}
            onCreateCategory={onCreateCategory}
            onCreateSubcategory={onCreateSubcategory}
            onPostPrompt={onPostPrompt}
            onCancelWidget={onCancelWidget}
            onActiveWidgetMount={onActiveWidgetReveal}
          />
        </div>;
      }}
    /> : null}
    {streaming ? <div><AgentActivityIndicator run={agentRun} reasoningSummary={reasoningSummary} responding={Boolean(streamingText)} /></div> : null}
    {streamingMessage ? <div className="pb-6"><MessageArticle
      message={streamingMessage}
      focusedResponse={focusedPromptIndex !== null}
      showAssistantByline={false}
      activeWidget={null}
      cancelWidget={null}
      usedWidgets={usedWidgets}
      pendingWidget={null}
      busy
      locationAllowed={locationAllowed}
      citationsOpen={false}
      onToggleCitations={onToggleCitations}
      onAction={onAction}
      onCreateCategory={onCreateCategory}
      onCreateSubcategory={onCreateSubcategory}
      onPostPrompt={onPostPrompt}
      onCancelWidget={onCancelWidget}
      onActiveWidgetMount={onActiveWidgetReveal}
    /></div> : null}
    {busy && !streaming ? <div className="mt-6 flex items-center gap-3 px-1 py-2 text-control text-ink-muted"><FynStateMark state="turn" live /><span className="flex gap-1" aria-hidden><i className="typing-dot" /><i className="typing-dot" /><i className="typing-dot" /></span><span className="sr-only">Working on it</span></div> : null}
    {error ? <div role="alert" className="mt-6 flex flex-wrap items-center gap-3 gap-2 rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note leading-5 text-danger-ink sm:mx-8"><TriangleAlert className="shrink-0" /><span className="min-w-0 flex-1">{error}</span>{retry ? <Button type="button" variant="outline" size="lg" onClick={onRetry} className="rounded-xl border-danger-line text-danger-ink hover:bg-danger-tint"><RotateCcw size={14} /> Try again</Button> : null}</div> : null}
    </div>
    <div ref={focusSpacerRef} data-focused-turn-spacer aria-hidden className="pointer-events-none h-0" />
  </div>;
});

/** What the shell needs to reach inside the thread for: the rail's Saved
 *  analyses sends a prompt, and a file dropped anywhere lands on the importer.
 *  A handle keeps those two reachable without lifting the thread's state. */
type ThreadHandle = { sendPrompt: (text: string) => void; attach: (file: File | undefined) => void };

function ConversationWorkspace({ initialData, loadingThread, navOpen, onOpenNav, switching, dragging, handleRef, onRenameTitle }: {
  initialData: Bootstrap;
  loadingThread?: boolean;
  navOpen: boolean;
  onOpenNav: () => void;
  switching: boolean;
  dragging: boolean;
  handleRef: RefObject<ThreadHandle | null>;
  /** Absent while the thread is still loading, so a placeholder title can never
   *  be committed as the real one. */
  onRenameTitle?: (title: string) => void;
}) {
  const queryClient = useQueryClient();
  const locationAllowed = useQuery({ queryKey: ["privacy"], queryFn: getPrivacyStatus, staleTime: 5 * 60_000 }).data?.locationEnabled ?? false;
  const serverMessages = initialData.active_conversation.messages;
  // React Query can restore a still-fresh transcript from persistent storage
  // before its background request returns. Compute the server revision only
  // when that message-array reference changes; hashing the full transcript on
  // every composer keystroke would make long threads progressively slower.
  const serverTranscriptRevision = useMemo(() => transcriptRevision(serverMessages), [serverMessages]);
  const [messages, setMessages] = useState<Message[]>(serverMessages);
  const conversationId = initialData.active_conversation.id;
  const addConversationCategory = useCallback<CreateCategory>(async (name) => {
    const created = await createCategory(name);
    queryClient.setQueryData<CategoryDirectoryOut[]>(["category-directory"], (current) => {
      if (!current) return current;
      return current.some((item) => item.id === created.id)
        ? current.map((item) => item.id === created.id ? created : item)
        : [...current, created];
    });
    return created;
  }, [queryClient]);
  const addConversationSubcategory = useCallback<CreateSubcategory>(async (categoryId, name) => {
    const created = await createSubcategory(categoryId, name);
    queryClient.setQueryData<CategoryDirectoryOut[]>(["category-directory"], (current) => current?.map((item) => {
      if (item.id !== categoryId || item.subcategories.some((subcategory) => subcategory.id === created.id)) return item;
      return { ...item, subcategories: [...item.subcategories, created] };
    }));
    return created;
  }, [queryClient]);
  const [input, setInput] = useState("");
  // A share from the platform sheet lands as a navigation to "/" carrying the
  // text. Seeded, never sent: the person sees what arrived and decides.
  useEffect(() => {
    // Read inside cancellable scheduled work. Development Strict Mode tears
    // down the first effect before this frame, so the URL is consumed only by
    // the surviving mount; scheduling also avoids a synchronous state cascade
    // inside the effect itself.
    const frame = requestAnimationFrame(() => {
      const shared = takeSharedText();
      if (shared) setInput((current) => current || shared);
    });
    return () => cancelAnimationFrame(frame);
  }, []);
  const [linkCopied, setLinkCopied] = useState(false);
  const switchingConversation = switching;
  const [usedWidgets, setUsedWidgets] = useState<Set<string>>(() => completedWidgetIds(initialData.active_conversation.messages));
  const [pendingWidget, setPendingWidget] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState<Retry>(null);
  const [connectionLost, setConnectionLost] = useState(false);
  const [agentRun, setAgentRun] = useState<LiveAgentRun>(idleAgentRun);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [runPhase, setRunPhase] = useState<AgentRunPhase | null>(null);
  const [stoppingRun, setStoppingRun] = useState(false);
  const [interrupts, setInterrupts] = useState<FynInterrupt[] | null>(null);
  const [reasoningSummary, setReasoningSummary] = useState("");
  const [streamingText, setStreamingText] = useState("");
  const smoothedStreamingText = useSmoothedStreamingText(streamingText);
  // The optimistic UUID is replaced with the server-authored user-message ID
  // when the response lands. Resolving the index from that identity keeps the
  // focus window attached to the same question when a background transcript
  // reconciliation inserts or updates other rows.
  const [focusedPromptId, setFocusedPromptId] = useState<string | null>(null);
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
  // Three things reset it, and they are not the same thing. A different `id` is
  // a different conversation and clears everything, the half-typed draft
  // included. The same `id` going from loading to ready is the transcript
  // arriving for the thread already on screen. Finally, an idle same-thread
  // server revision replaces a restored cache entry; in-flight and optimistic
  // state block that adoption until the local turn has settled. Both same-thread
  // cases deliberately leave the draft alone, because it was typed here.
  const [seeded, setSeeded] = useState({
    id: conversationId,
    loading: Boolean(loadingThread),
    transcriptRevision: serverTranscriptRevision,
  });
  const threadChanged = seeded.id !== conversationId;
  const loadingChanged = seeded.loading !== Boolean(loadingThread);
  const adoptServerTranscript = shouldAdoptServerTranscript({
    messages,
    seededRevision: seeded.transcriptRevision,
    serverRevision: serverTranscriptRevision,
    activeRunId,
    pendingWidget,
    uploading: upload !== null,
  });
  if (threadChanged || loadingChanged || adoptServerTranscript) {
    const changedThread = seeded.id !== conversationId;
    const resetViewport = changedThread || loadingChanged;
    setSeeded({
      id: conversationId,
      loading: Boolean(loadingThread),
      transcriptRevision: serverTranscriptRevision,
    });
    setMessages(serverMessages);
    setUsedWidgets(completedWidgetIds(serverMessages));
    setPendingWidget(null);
    setError(null);
    setRetry(null);
    setConnectionLost(false);
    setAgentRun(idleAgentRun);
    setActiveRunId(null);
    setRunPhase(null);
    setStoppingRun(false);
    setInterrupts(null);
    setReasoningSummary("");
    setStreamingText("");
    if (resetViewport) setFocusedPromptId(null);
    setOpenCitations(new Set());
    setUpload(null);
    // A background refetch of this same transcript is reconciliation, not
    // navigation. Preserve an off-bottom reader's ownership; resetting follow
    // here made every successfully persisted reply jump to the end a second
    // time when the invalidated conversation query returned. A genuinely new
    // or newly loaded thread still opens at its latest turn.
    if (resetViewport) setAtBottom(true);
    setAnnouncement("");
    setLinkCopied(false);
    if (changedThread) setInput("");
  }

  const scrollRef = useRef<HTMLDivElement>(null);
  const [scrollElement, setScrollElement] = useState<HTMLDivElement | null>(null);
  const bindScrollRef = useCallback((node: HTMLDivElement | null) => {
    scrollRef.current = node;
    setScrollElement(node);
  }, []);
  const { headerVisible, updateHeaderForScroll, showHeader, hideHeader } = useAutoHideSiteHeader();
  const textRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  // "/" reaches the composer from anywhere in the thread, matching the
  // search-focus key on the ledger pages.
  usePlainKey("/", useCallback(() => textRef.current?.focus(), []));
  const jumpToLatestRef = useRef<HTMLDivElement>(null);
  // Scheduled work that can outlive the render that started it, so it is held
  // rather than fired and forgotten.
  const copiedTimer = useRef<number | undefined>(undefined);
  const copiedFrame = useRef<number | undefined>(undefined);
  const scrollSettleTimer = useRef<number | undefined>(undefined);
  // True from the first upward gesture until the reader reaches the exact end
  // again. This is deliberately imperative: it closes the frame-sized gap
  // between an input event and the state/effect commit that removes bottom
  // following.
  const readerScrolled = useRef(false);
  // A follow scroll this thread started itself, still travelling toward the
  // end. Its own scroll events must not read as the reader leaving — that
  // misreading is what used to switch following off one frame into every
  // animation — and while a smooth glide is in flight, an instant follow must
  // not land on top of it and snap the movement it exists to smooth.
  const followScroll = useRef<{ smooth: boolean } | null>(null);
  const followSettleTimer = useRef<number | undefined>(undefined);
  // A HITL card is positioned by the transcript list. Its scroll
  // events are programmatic even though the destination is intentionally not
  // the physical bottom, so they must never be mistaken for reader navigation.
  const widgetRevealActive = useRef(false);
  const widgetRevealSettleTimer = useRef<number | undefined>(undefined);
  // A turn that landed while the reader owned the viewport. Unlike the normal
  // distance-based jump affordance, this remains set through row measurements
  // and partial scrolling until the latest turn is actually reached.
  const unseenLatest = useRef(false);
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
    followScroll.current = null;
    const controller = new AbortController();
    inFlight.current = controller;
    return () => {
      window.clearTimeout(copiedTimer.current);
      window.cancelAnimationFrame(copiedFrame.current ?? 0);
      window.clearTimeout(scrollSettleTimer.current);
      window.clearTimeout(followSettleTimer.current);
      window.clearTimeout(widgetRevealSettleTimer.current);
      widgetRevealActive.current = false;
      unseenLatest.current = false;
      controller.abort();
      inFlight.current = null;
      // The next thread is a new transcript, so it is placed at its end rather
      // than smooth-scrolled there as if a reply had just landed.
      arrivals.current = null;
    };
  }, [conversationId, showHeader]);

  const succeeded = useCallback((response: AgentResponse) => {
    setMessages((current) => mergeAgentResponse(current, response));
    if (response.user_message_id) {
      setFocusedPromptId((current) => current?.startsWith("optimistic-") ? response.user_message_id : current);
    }
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
      queryClient.invalidateQueries({ queryKey: ["overview"] }),
    ]);
  }, [queryClient, conversationId]);

  const watchRelatedQuestions = useCallback((runId: string, response: AgentResponse) => {
    if (
      response.pendingAction
      || response.widgets.some((widget) => widget.type === widgetTypeIds.related_questions)
    ) return;
    // This promise is intentionally detached from the mutation. The composer
    // becomes usable as soon as the canonical response resolves; late garnish
    // is merged only if this thread is still mounted.
    void waitForAgentRelatedQuestions(runId, inFlight.current?.signal)
      .then((enrichment) => {
        if (!enrichment) return;
        setMessages((current) => mergeMessageWidget(current, enrichment.messageId, enrichment.widget));
      })
      .catch(() => undefined);
  }, []);

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
    setAgentRun((current) => {
      const index = current.steps.findIndex((item) => item.id === activity.id);
      const steps = index === -1
        ? [...current.steps, activity]
        : current.steps.map((item, itemIndex) => itemIndex === index ? activity : item);
      return {
        steps,
        // Each event restates the run-level aggregates; the newest one is the
        // current truth, and an event without them changes nothing.
        failureSummary: activity.failureSummary ?? current.failureSummary,
        modelPassCount: activity.modelPassCount ?? current.modelPassCount,
      };
    });
  }, []);

  const clientRunTelemetry = useRef<AgentRunTelemetry | null>(null);
  const startClientRunTelemetry = useCallback((replayed = false) => {
    try {
      clientRunTelemetry.current = new AgentRunTelemetry(replayed);
    } catch {
      clientRunTelemetry.current = null;
    }
  }, []);
  const finishClientRunTelemetry = useCallback((composerUnlocked: boolean) => {
    const telemetry = clientRunTelemetry.current;
    if (!telemetry) return;
    telemetry.responseResolved();
    const afterUsablePaint = () => {
      telemetry.answerVisible();
      if (composerUnlocked) telemetry.composerUnlocked();
      telemetry.report();
      if (clientRunTelemetry.current === telemetry) clientRunTelemetry.current = null;
    };
    try {
      if (document.visibilityState === "visible") {
        window.requestAnimationFrame(() => window.setTimeout(afterUsablePaint, 0));
      }
      else window.setTimeout(afterUsablePaint, 0);
    } catch {
      // Dropping a browser sample is always safer than touching run behavior.
    }
  }, []);

  useEffect(() => {
    if (smoothedStreamingText) clientRunTelemetry.current?.answerVisible();
  }, [smoothedStreamingText]);

  const agentRunCallbacks = useMemo(() => ({
    onRunCreated: (runId: string) => {
      if (!clientRunTelemetry.current) startClientRunTelemetry();
      clientRunTelemetry.current?.bindRun(runId);
      setActiveRunId(runId);
      setStoppingRun(false);
      setStreamingText("");
    },
    onPhase: (phase: AgentRunPhase) => setRunPhase(phase),
    onActivity: (activity: AgentActivity) => {
      clientRunTelemetry.current?.activityReceived();
      updateActivity(activity);
    },
    onReasoning: (summary: string) => {
      clientRunTelemetry.current?.reasoningReceived();
      setReasoningSummary(summary);
    },
    onText: (text: string) => {
      clientRunTelemetry.current?.textReceived();
      setStreamingText(text);
    },
  }), [startClientRunTelemetry, updateActivity]);

  const chatMutation = useMutation({
    mutationFn: ({ id, text }: { id: string; text: string }) => sendAgentMessage(id, text, agentRunCallbacks, inFlight.current?.signal),
    onMutate: () => { startClientRunTelemetry(); setAgentRun(idleAgentRun); setReasoningSummary(""); setStreamingText(""); setAnnouncement("fyn AI is working on your message."); },
    onSuccess: (result) => {
      setAgentRun(idleAgentRun);
      setActiveRunId(null);
      setStoppingRun(false);
      setInterrupts(result.interrupts);
      succeeded(result.response);
      finishClientRunTelemetry(result.interrupts.length === 0);
      watchRelatedQuestions(result.runId, result.response);
    },
    // A message that never reached the server should not look delivered: drop
    // the bubble and put the text back where the user can send it again.
    onError: (cause: Error, variables) => {
      setAgentRun(idleAgentRun);
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
    onMutate: () => startClientRunTelemetry(),
    onSuccess: (result, variables) => {
      setPendingWidget(null);
      setActiveRunId(null);
      setStoppingRun(false);
      setInterrupts(result.interrupts);
      // Locking the card belongs to the success path; a failed action has to
      // stay clickable or the user is stuck until a reload.
      setUsedWidgets((current) => reconcileUsedWidgetIds(
        current,
        variables.markUsed ? variables.widgetId : null,
        result.response.widgets,
      ));
      succeeded(result.response);
      finishClientRunTelemetry(result.interrupts.length === 0);
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
    onMutate: () => startClientRunTelemetry(),
    onSuccess: (result) => {
      setActiveRunId(null);
      setStoppingRun(false);
      setInterrupts(result.interrupts);
      succeeded(result.response);
      finishClientRunTelemetry(result.interrupts.length === 0);
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
      // CSV upload is an explicit change of direction. The API retires any
      // older interrupt atomically with the preview, so clear the local copy
      // immediately instead of letting it keep the composer paused until a
      // thread-state refetch happens to finish.
      setInterrupts([]);
      const deliveredAt = new Date().toISOString();
      setMessages((current) => [...current, { id: `upload-${Date.now()}`, role: "user", content: `Uploaded ${file.name}`, widgets: [], citations: [], created_at: deliveredAt, delivered_at: deliveredAt }]);
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
    startClientRunTelemetry(true);
    setAgentRun(idleAgentRun);
    setReasoningSummary("");
    reconnectAgentRun(conversationId, discovered.id, agentRunCallbacks, inFlight.current?.signal)
      .then((result) => {
        setAgentRun(idleAgentRun);
        setActiveRunId(null);
        setStoppingRun(false);
        setInterrupts(result.interrupts);
        succeeded(result.response);
        finishClientRunTelemetry(result.interrupts.length === 0);
      })
      .catch((cause: Error) => {
        setAgentRun(idleAgentRun);
        setActiveRunId(null);
        setStoppingRun(false);
        if (cause.name !== "AbortError") failed(cause, null);
      })
      .finally(() => { reconnectingRun.current = null; });
  }, [activeRunId, actionPending, agentRunCallbacks, agentState.data, chatPending, conversationId, failed, finishClientRunTelemetry, interruptPending, startClientRunTelemetry, succeeded]);

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
    const resolvedVisible = visible || unseenLatest.current;
    control.dataset.visible = String(resolvedVisible);
    control.dataset.scrolling = String(resolvedVisible && scrolling);
    control.dataset.unread = String(unseenLatest.current);
    control.inert = !resolvedVisible;
    control.setAttribute("aria-hidden", String(!resolvedVisible));
  }, []);

  const revealActiveWidget = useCallback((target: HTMLElement, focusInPlace = false) => {
    if (readerScrolled.current) return false;
    const scroller = scrollRef.current;
    const targetRect = target.getBoundingClientRect();
    const scrollerRect = scroller?.getBoundingClientRect();
    const alreadyVisibleInFocusedWindow = Boolean(
      focusedPromptId
      && scrollerRect
      && targetRect.top >= scrollerRect.top
      && targetRect.bottom <= scrollerRect.bottom
    );
    // The focused-turn window already placed this card. Move semantic focus
    // without changing scroll ownership or the spacer equation.
    if (focusInPlace || alreadyVisibleInFocusedWindow) {
      if (!(document.activeElement instanceof HTMLElement) || !target.contains(document.activeElement)) {
        target.focus({ preventScroll: true });
      }
      return document.activeElement instanceof HTMLElement && target.contains(document.activeElement);
    }
    // This hand-off is application navigation, not evidence that the reader
    // took over the scrollbar. Keep it distinct so the scroll events emitted
    // by list alignment cannot permanently disable response following.
    readerScrolled.current = false;
    followScroll.current = null;
    window.clearTimeout(followSettleTimer.current);
    widgetRevealActive.current = true;
    window.clearTimeout(widgetRevealSettleTimer.current);
    widgetRevealSettleTimer.current = window.setTimeout(() => {
      widgetRevealActive.current = false;
    }, SCROLL_SETTLE_MS);
    unseenLatest.current = false;
    setAtBottom(false);
    updateJumpControl(false);
    // Respect a widget that deliberately focused one of its own fields (for
    // example, a newly opened category-name input).
    if (!(document.activeElement instanceof HTMLElement) || !target.contains(document.activeElement)) {
      target.focus({ preventScroll: true });
    }
    return document.activeElement instanceof HTMLElement && target.contains(document.activeElement);
  }, [focusedPromptId, updateJumpControl]);

  const followNextResponse = useCallback(() => {
    // Once the reader answers the active card, their attention transfers to
    // the response it resumes. Re-arm following before the old card compacts,
    // so that height change and the appended row are one anchored transition.
    window.clearTimeout(widgetRevealSettleTimer.current);
    widgetRevealActive.current = false;
    unseenLatest.current = false;
    readerScrolled.current = false;
    followScroll.current = null;
    setAtBottom(true);
    updateJumpControl(false);
  }, [updateJumpControl]);

  useLayoutEffect(() => {
    unseenLatest.current = false;
    updateJumpControl(false);
  }, [conversationId, updateJumpControl]);

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
      window.cancelAnimationFrame(copiedFrame.current ?? 0);
      // Count the confirmation window from a frame in which React has had the
      // chance to paint it. On a very long transcript, starting the timer in
      // the click handler can otherwise expire before the receipt is visible.
      copiedFrame.current = window.requestAnimationFrame(() => {
        copiedFrame.current = undefined;
        copiedTimer.current = window.setTimeout(() => setLinkCopied(false), 1800);
      });
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
    unseenLatest.current = false;
    setAtBottom(true);
    hideHeader();
    updateJumpControl(false);
    const deliveredAt = new Date().toISOString();
    const optimisticId = `optimistic-${Date.now()}`;
    setFocusedPromptId(optimisticId);
    setMessages((current) => [...current, { id: optimisticId, role: "user", content: text, widgets: [], citations: [], created_at: deliveredAt, delivered_at: deliveredAt }]);
    startChat({ id: conversationId, text });
  }, [chatPending, actionPending, uploadPending, agentRunning, pausedForInterrupt, hideHeader, startChat, conversationId, updateJumpControl]);

  function submit(event?: FormEvent) {
    event?.preventDefault();
    sendPrompt(input);
  }

  const attach = useCallback((file: File | undefined) => {
    if (!file || busy) return;
    const problem = csvProblem(file);
    if (problem) { failed(new Error(problem), null); return; }
    setError(null);
    setFocusedPromptId(null);
    readerScrolled.current = false;
    unseenLatest.current = false;
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
    followNextResponse();
    setPendingWidget(widgetId);
    startAction({ widgetId, action, payload, markUsed });
  }, [activeInteractionWidgetId, pendingWidget, usedWidgets, followNextResponse, startAction]);

  const cancelWidgetInterrupt = useCallback((widgetId: string) => {
    if (interruptPending) return;
    const current = availableInterrupts.find((interrupt) => interrupt.widgetId === widgetId);
    if (current) {
      followNextResponse();
      resolveInterrupt({ interrupt: current, response: { status: "cancelled" } });
    }
  }, [availableInterrupts, followNextResponse, interruptPending, resolveInterrupt]);

  const retryLast = useCallback(() => {
    if (!retry) return;
    setError(null);
    if (retry.kind === "chat") sendPrompt(retry.text);
    if (retry.kind === "action") { followNextResponse(); setPendingWidget(retry.widgetId); startAction(retry); }
    if (retry.kind === "upload") startUpload(retry.file);
    setRetry(null);
  }, [retry, followNextResponse, sendPrompt, startAction, startUpload]);

  // Whether the reader is following the conversation, which is not the same as
  // where the scrollbar happens to be. Rows measure themselves after they mount
  // and charts arrive a second late; both move the floor without anybody
  // touching the page. Treating that as "they scrolled up" is what left a
  // refreshed thread parked short of its own latest reply.
  //
  // So the thread only stops following when the reader actually drives it.
  const takeScrollControl = () => {
    readerScrolled.current = true;
    // The reader wins over any follow scroll still travelling: their gesture
    // also cancels the browser's smooth animation, so drop the bookkeeping
    // with it rather than letting the settle check drag them back down.
    followScroll.current = null;
    window.clearTimeout(followSettleTimer.current);
    widgetRevealActive.current = false;
    window.clearTimeout(widgetRevealSettleTimer.current);
    setAtBottom(false);
  };

  // Opening information inside a turn is reader navigation. Capture that
  // intent before the disclosure toggles so the same React commit switches the
  // transcript out of end-follow mode and lets the content grow naturally.
  function stopFollowingForDisclosure(event: ReactMouseEvent<HTMLDivElement>) {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const disclosure = target.closest<HTMLElement>('summary, [data-inline-disclosure="true"]');
    if (!disclosure || !event.currentTarget.contains(disclosure)) return;
    takeScrollControl();
  }

  const noteWheelScroll = (event: React.WheelEvent<HTMLDivElement>) => {
    // A downward wheel at the physical end is a no-op and should not turn off
    // following. Upward intent, however, must win before the scroll event and
    // any pending ResizeObserver callback run.
    if (event.deltaY < 0) takeScrollControl();
  };

  const noteScrollKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowUp" || event.key === "PageUp" || event.key === "Home") takeScrollControl();
  };

  const noteScrollbarPointer = (event: React.PointerEvent<HTMLDivElement>) => {
    const node = event.currentTarget;
    if (event.target !== node) return;
    const rect = node.getBoundingClientRect();
    // Native scrollbar drags do not emit a dedicated intent event. A press in
    // its edge gutter is the one reliable signal available before `scroll`;
    // the small fallback also covers overlay scrollbars whose layout width is
    // reported as zero.
    const gutter = Math.max(node.offsetWidth - node.clientWidth, 16);
    if (event.clientX >= rect.right - gutter) takeScrollControl();
  };

  // Closes the gap a follow scroll leaves when its animation settles short of
  // the end — the target is captured when the scroll starts, and a streaming
  // reply or a late chart can grow the thread underneath it before it lands.
  const followSettle = useCallback(() => {
    const node = scrollRef.current;
    if (!node || !followScroll.current || readerScrolled.current) return;
    if (node.scrollHeight - node.scrollTop - node.clientHeight <= AT_END_SLACK_PX) {
      followScroll.current = null;
      return;
    }
    followScroll.current = { smooth: false };
    // The snap always moves the scroller, and its scroll event re-arms this
    // check from `trackScroll` if the thread has grown yet again.
    node.scrollTo({ top: node.scrollHeight, behavior: "auto" });
  }, []);

  // Follow scrolls target the scroller's physical end, not the list's
  // last row: the live activity card, the streaming reply, and the column's
  // bottom padding all sit below the virtual rows, and a scroll that stops at
  // the last row's edge strands them under the fold — which then read as "the
  // reader scrolled away" and switched following off for the rest of the run.
  // Virtuoso still owns row measurement; the content ResizeObserver below
  // carries a following reader to the physical end after those measurements.
  const scrollToEnd = useCallback((behavior: ScrollBehavior) => {
    const node = scrollRef.current;
    if (!node) return;
    // A smooth glide already travelling to the same place must not be snapped
    // forward by an instant follow; the settle check finishes the remainder.
    if (behavior !== "smooth" && followScroll.current?.smooth) return;
    if (node.scrollHeight - node.scrollTop - node.clientHeight > AT_END_SLACK_PX) {
      followScroll.current = { smooth: behavior === "smooth" };
      window.clearTimeout(followSettleTimer.current);
      followSettleTimer.current = window.setTimeout(followSettle, SCROLL_SETTLE_MS);
    }
    node.scrollTo({ top: node.scrollHeight, behavior });
  }, [followSettle]);

  const revealFocusedPrompt = useCallback(() => {
    readerScrolled.current = false;
    unseenLatest.current = false;
    updateJumpControl(false);
    scrollToEnd(prefersReducedMotion() ? "auto" : "smooth");
  }, [scrollToEnd, updateJumpControl]);

  // Virtuoso first places the last estimated row, then replaces estimates with
  // measured heights. With an external scroll parent that correction does not
  // necessarily resize `contentRef`, so its ResizeObserver cannot close the
  // remaining gap after a reload. Follow Virtuoso's own total-height signal
  // while the reader still owns the latest edge. `readerScrolled` changes on
  // wheel/touch/key intent before React state commits, so this callback cannot
  // pull someone back down while they are reading history.
  const followMeasuredList = useCallback(() => {
    if (!atBottom || readerScrolled.current) return;
    scrollToEnd("auto");
  }, [atBottom, scrollToEnd]);

  function trackScroll(event: React.UIEvent<HTMLDivElement>) {
    const node = event.currentTarget;
    window.clearTimeout(scrollSettleTimer.current);
    window.clearTimeout(followSettleTimer.current);
    updateHeaderForScroll(node.scrollTop, readerScrolled.current);
    const distanceFromBottom = Math.max(0, node.scrollHeight - node.scrollTop - node.clientHeight);
    // The active-widget reveal deliberately stops above the physical bottom.
    // Its list-owned scroll events are not a scrollbar drag and must
    // not flip the thread into reader-owned mode while its row settles.
    if (widgetRevealActive.current) {
      updateJumpControl(false);
      return;
    }
    // Resolve the terminal state before scheduling any visual settle callback.
    // Previously a frame near the end captured `jumpVisible=true`, the exact
    // bottom hid the control, and that older timer showed it again 150ms later
    // without an unread dot.
    if (distanceFromBottom <= AT_END_SLACK_PX) {
      unseenLatest.current = false;
      followScroll.current = null;
      readerScrolled.current = false;
      setAtBottom(true);
      updateJumpControl(false);
      return;
    }
    const viewportHeight = typeof window === "undefined" ? node.clientHeight : window.innerHeight;
    const jumpVisible = Boolean(
      unseenLatest.current
      || (readerScrolled.current && distanceFromBottom > viewportHeight * JUMP_TO_LATEST_VIEWPORT_RATIO)
    );
    updateJumpControl(jumpVisible, true);
    scrollSettleTimer.current = window.setTimeout(
      () => updateJumpControl(jumpVisible),
      SCROLL_SETTLE_MS,
    );
    // Reader intent has priority over the generous follow zone. Previously a
    // turn could remain `atBottom` for the first 120px of an upward scroll, so
    // a row measurement or late chart resize pulled it back toward the dock.
    if (readerScrolled.current) {
      setAtBottom(false);
      return;
    }
    if (followScroll.current) {
      // Our own follow scroll passing by — or ending short because the thread
      // grew underneath it while it travelled. Either way the reader has not
      // left, so following stays on and the settle check closes any remainder
      // once the movement stops.
      followSettleTimer.current = window.setTimeout(followSettle, SCROLL_SETTLE_MS);
      return;
    }
    // A scroll with no announced owner can be a list correction after a
    // row measurement. Inferring reader intent here would disable following
    // just before the next card is focused. Wheel, touch, keyboard and native
    // scrollbar pointer intent are all captured before their scroll events.
  }

  function jumpToLatest() {
    readerScrolled.current = false;
    const node = scrollRef.current;
    if (node && node.scrollHeight - node.scrollTop - node.clientHeight <= AT_END_SLACK_PX) {
      unseenLatest.current = false;
      setAtBottom(true);
      updateJumpControl(false);
      return;
    }
    // Keep an unseen-reply indicator present while the smooth jump travels.
    // `trackScroll` retires it only when the physical latest edge is reached;
    // an active widget reveal retires it when that exact reply is positioned.
    updateJumpControl(true, true);
    scrollToEnd(prefersReducedMotion() ? "auto" : "smooth");
  }

  const toggleCitations = useCallback((messageId: string) => {
    setOpenCitations((current) => {
      const next = new Set(current);
      if (next.has(messageId)) next.delete(messageId); else next.add(messageId);
      return next;
    });
  }, []);

  // A conversation has not started until you have said something. A fresh
  // thread is empty, but a resumed one can hold assistant-only rows, so the
  // opening screen keys off the absence of a question rather than a row count.
  const focusedMode = !loadingThread && !messages.some((message) => message.role === "user");
  const focusedPromptIndex = focusedPromptId === null
    ? null
    : messages.findIndex((message) => message.id === focusedPromptId);
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

  // A keyboard opening takes half the screen from the transcript without
  // changing a single row in it, so nothing above notices: the scroller simply
  // becomes shorter around the same scrollTop and the turn the reader was
  // reading slides out under the keys. Same on the way back. A reader who was
  // following the end is put back on it, instantly — this is the viewport
  // being resized around them, not something arriving to watch.
  useEffect(() => {
    if (focusedMode) return;
    return subscribeToViewport(() => {
      if (!scrollRef.current || !atBottom || readerScrolled.current) return;
      scrollToEnd("auto");
    });
  }, [focusedMode, atBottom, scrollToEnd]);

  const focusedArrival = useRef(activeWidgetFocusKey);
  useLayoutEffect(() => {
    // Only a turn landing earns a glide. Activity ticks and streaming growth
    // re-run this effect too, but they arrive several times a second — easing
    // toward each one restarts the animation mid-flight and reads as stutter,
    // so they follow instantly instead.
    const transcript = `${messages.length}:${messages[messages.length - 1]?.id ?? ""}`;
    const arrived = arrivals.current !== null && arrivals.current !== transcript;
    arrivals.current = transcript;
    const node = scrollRef.current;
    if (!node) return;
    if (!atBottom) {
      // The viewport belongs to the reader. Keep it anchored and announce that
      // a new turn is waiting, even when the gap is smaller than the normal
      // 90vh history-navigation threshold.
      if (arrived) {
        unseenLatest.current = true;
        updateJumpControl(true);
      }
      return;
    }
    const hitlArrived = Boolean(activeWidgetFocusKey && focusedArrival.current !== activeWidgetFocusKey);
    focusedArrival.current = activeWidgetFocusKey;
    // `Transcript` aligns a new HITL widget itself. Following the generic end
    // here as well would win the same layout pass and hide the widget's top.
    if (hitlArrived) return;
    scrollToEnd(arrived && !prefersReducedMotion() ? "smooth" : "auto");
  }, [messages, agentRun, activeWidgetFocusKey, atBottom, loadingThread, scrollToEnd, updateJumpControl]);

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
          ref={bindScrollRef}
          onClickCapture={stopFollowingForDisclosure}
          onScroll={trackScroll}
          onWheel={noteWheelScroll}
          onTouchMove={takeScrollControl}
          onKeyDown={noteScrollKey}
          onPointerDown={noteScrollbarPointer}
          className="conversation-scroll min-h-0 flex-1 overflow-y-auto"
        >
          <SiteHeader
            title={title}
            onRenameTitle={onRenameTitle}
            subtitle={<><span className={cn("size-1 rounded-full", connectionLost ? "bg-danger" : agentRunning || pausedForInterrupt ? "bg-secondary" : "bg-ink-muted")} />{statusLabel}</>}
            subtitleClassName={cn("mt-0.5 flex items-center gap-2", connectionLost && "font-medium text-danger-ink")}
            hidden={!headerVisible}
            navOpen={navOpen}
            onOpenNav={onOpenNav}
            end={<div className="flex items-center gap-1">
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
              agentRun={agentRun}
              reasoningSummary={reasoningSummary}
              streamingText={smoothedStreamingText}
              streaming={agentRunning}
              busy={busy}
              locationAllowed={locationAllowed}
              usedWidgets={usedWidgets}
              pendingWidget={pendingWidget}
              openCitations={openCitations}
              activeWidget={activeInteractionWidgetId}
              cancelWidget={interruptWidgetId}
              activeWidgetFocusKey={activeWidgetFocusKey}
              error={error}
              retry={retry}
              followEnd={atBottom}
              focusedPromptIndex={focusedPromptIndex !== null && focusedPromptIndex >= 0 ? focusedPromptIndex : null}
              onAction={handleWidgetAction}
              onCreateCategory={addConversationCategory}
              onCreateSubcategory={addConversationSubcategory}
              onPostPrompt={sendPrompt}
              onCancelWidget={cancelWidgetInterrupt}
              onActiveWidgetReveal={revealActiveWidget}
              onFocusedPromptReveal={revealFocusedPrompt}
              onListHeightChanged={followMeasuredList}
              onToggleCitations={toggleCitations}
              onRetry={retryLast}
              scrollElement={scrollElement}
              scrollRef={scrollRef}
            />
          </div>}
        </div>

        <p aria-live="polite" aria-atomic className="sr-only">{announcement}</p>

        {!focusedMode ? <>
          {/* Sits just above the composer rather than inside it, so appearing
              cannot change the composer's height and jolt the very scroll
              position it exists to restore. */}
          <div ref={jumpToLatestRef} data-visible="false" data-scrolling="false" data-unread="false" aria-hidden="true" inert style={{ bottom: `calc(var(--dock-h) + 0.75rem)` }} className="jump-to-latest pointer-events-none absolute inset-x-0 z-20 flex justify-center px-3 sm:px-6"><Button type="button" onClick={jumpToLatest} variant="outline" className="pointer-events-auto rounded-full shadow-[var(--shadow-overlay)]"><ArrowDown size={14} className="jump-to-latest-icon" /> Jump to latest<span aria-hidden className="jump-to-latest-unread-dot" /></Button></div>
          <div ref={dockRef} className="entry-dock z-20 shrink-0 px-3 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:px-6 sm:pt-4 sm:pb-4">
            {fallbackInterrupt ? <InterruptFallback interrupt={fallbackInterrupt} busy={interruptPending} locationAllowed={locationAllowed} onResolve={(response) => {
              followNextResponse();
              resolveInterrupt({ interrupt: fallbackInterrupt, response });
            }} /> : null}
            <Composer variant="docked" value={input} onValueChange={setInput} onSubmit={submit} onStop={stopAgent} textRef={textRef} fileRef={fileRef} onAttach={attach} busy={busy} sending={chatPending} running={agentRunning} stopping={stoppingRun} paused={pausedForInterrupt} disabled={switchingConversation} dragging={dragging} upload={upload} />
          </div>
        </> : null}
      </main>
    </>;
}

type ShellValue = { navOpen: boolean; openNav: () => void; switching: boolean; dragging: boolean; handleRef: RefObject<ThreadHandle | null>; conversations: ConversationSummary[]; renameThread: (id: string, title: string) => void };
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
  const openSettings = useCallback(() => { setSidebarOpen(false); navigate(appPaths.settingsAgent); }, [navigate]);
  const openProfile = useCallback(() => { setSidebarOpen(false); navigate(appPaths.settings); }, [navigate]);
  // Settings borrows the rail; leaving hands it back to the workspace it was
  // borrowed from rather than to whatever the history stack happens to hold.
  const leaveSettings = useCallback(() => { setSidebarOpen(false); navigate(appPaths.home); }, [navigate]);
  const settingsOpen = pathname === appPaths.settings || pathname.startsWith(`${appPaths.settings}/`);

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

  const renameThread = useCallback((id: string, title: string) => {
    setNavError(null);
    // The new caption paints immediately everywhere it is read — the rail and
    // the open thread's header — and the server's answer only ever corrects it.
    queryClient.setQueryData<InfiniteData<ConversationPage>>(["conversations"], (data) => data && ({
      ...data,
      pages: data.pages.map((page) => ({ ...page, items: page.items.map((item) => (item.id === id ? { ...item, title } : item)) })),
    }));
    queryClient.setQueryData<ConversationOut>(["conversation", id], (data) => data && ({ ...data, title }));
    void renameConversation(id, title)
      .then(() => Promise.all([
        queryClient.invalidateQueries({ queryKey: ["conversations"] }),
        queryClient.invalidateQueries({ queryKey: ["conversation", id] }),
      ]))
      .catch((cause: unknown) => {
        // Roll the optimistic caption back to server truth before reporting.
        void queryClient.invalidateQueries({ queryKey: ["conversations"] });
        void queryClient.invalidateQueries({ queryKey: ["conversation", id] });
        setNavError(cause instanceof Error ? cause.message : "The conversation could not be renamed.");
      });
  }, [queryClient]);

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
    if (!sidebarOpen) return;
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") setSidebarOpen(false); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [sidebarOpen]);


  if (signedOut) return <AppSkeleton label="Taking you to sign in…" />;
  if (initial.isError) return <WorkspaceUnreachable onRetry={() => initial.refetch()} retrying={initial.isFetching} />;

  return <ToastProvider toastManager={toast} limit={5}>
    <ShellContext.Provider value={{ navOpen: sidebarOpen, openNav, switching, dragging, handleRef: thread, conversations, renameThread }}>
    <div className="app-shell bg-ground text-ink" onDragOver={(event) => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); setDragging(true); } }} onDragLeave={(event) => { if (event.currentTarget === event.target) setDragging(false); }} onDrop={(event) => { if (!event.dataTransfer.files.length) return; event.preventDefault(); setDragging(false); thread.current?.attach(event.dataTransfer.files[0]); }}>
      <div className="relative mx-auto grid h-full max-w-[1600px] md:grid-cols-[var(--rail-w)_1fr]">
        <button type="button" tabIndex={-1} aria-hidden onClick={() => setSidebarOpen(false)} className={cn("fixed inset-0 z-30 bg-scrim/25 backdrop-blur-[2px] transition-opacity duration-300 md:hidden", sidebarOpen ? "opacity-100" : "pointer-events-none opacity-0")} />
        <ConversationRail
          conversations={conversations}
          activeId={conversationId}
          activePage={MONEY_PAGES.find((item) => pathname === item.path || pathname.startsWith(`${item.path}/`))?.path ?? null}
          user={initial.data?.user ?? null}
          personalLendingAvailable={initial.data?.features.personalLending ?? false}
          open={sidebarOpen}
          docked={isDesktop}
          switching={creating}
          loading={history.isPending}
          loadingMore={history.isFetchingNextPage}
          hasMore={Boolean(history.hasNextPage)}
          settingsOpen={settingsOpen}
          onClose={closeNav}
          onOpenPage={openPage}
          onSelect={selectThread}
          onPrefetch={prefetchConversation}
          onDelete={deleteThread}
          onRename={renameThread}
          onLoadMore={loadMore}
          onNew={newConversation}
          onLeaveSettings={leaveSettings}
          onOpenSection={closeNav}
          onOpenSettings={openSettings}
          onOpenProfile={openProfile}
        />
        {/* Rendered here rather than as `children` so it sits above the
            segment boundary and is re-seeded rather than rebuilt. `children`
            is the page, which renders nothing. */}
        {conversationId ? <ConversationThread conversationId={conversationId} /> : null}
        {children}
        {/* The identity key deliberately remounts the tab-scoped draft if an
            auth transition replaces the user without rebuilding this shell. */}
        {/* Development only. `import.meta.env.DEV` is substituted with a literal
            at build time, so the production bundle drops the branch and the
            module with it — the note pad is not merely hidden, it is not
            shipped. A flag read off an object would have hidden it while still
            shipping the code and its listeners. */}
        {import.meta.env.DEV && initial.data ? <Scratchpad key={initial.data.user.id} storageScope={initial.data.user.id} /> : null}
        {navError ? <div role="alert" className="fixed inset-x-0 bottom-4 z-50 mx-auto w-fit max-w-[90vw] rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note text-danger-ink shadow-[var(--shadow-overlay)]">{navError}</div> : null}
        {/* Deleting everything deletes the account itself, so there is nothing
            left to return to — the session is already void server-side. */}
      </div>
    </div>
    </ShellContext.Provider>
    {/* Clear of the composer, which owns the bottom of the column, and of the
        rail down the left: the stack lands in the empty band between them.
        It rides on the measured dock rather than a matching guess, so the two
        cannot drift apart when the box grows. */}
    <ToastPortal>
      <ToastViewport>
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
    // Persisted data makes the first paint instant and preserves offline use,
    // but its freshness timestamp can outlive a just-completed mutation whose
    // invalidation had not finished before the page reloaded. A money record
    // must therefore reconcile on every mount even when the cached transcript
    // is younger than the global stale time. The cache still supplies `data`
    // immediately; this is a background correctness read, not a loading gate.
    refetchOnMount: "always",
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
    onRenameTitle={conversation.data ? (title) => shell.renameThread(conversationId, title) : undefined}
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
