"use client";

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Archive, ArrowDown, Check, CheckCircle2, Copy, Download, FileText, Loader2, MapPin, Menu, MessageSquareText, Paperclip, Plus, RotateCcw, SendHorizontal, Settings, ShieldCheck, Sparkles, Trash2, TriangleAlert, UserRound, X } from "lucide-react";
import { useParams, useRouter } from "next/navigation";
import { createContext, FormEvent, memo, RefObject, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Toast, ToastAction, ToastContent, ToastDescription, ToastPortal, ToastProvider, ToastTitle, ToastViewport, toast, useToastManager } from "@/components/ui/toast";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { WidgetRenderer } from "@/components/widget-renderer";
import { MarkdownMessage } from "@/components/widget-library/markdown-message";
import { bootstrap, createConversation, deleteAllData, deleteConversation, downloadDataExport, flushConversationDeletion, getPrivacyStatus, isUnauthorized, listConversations, loadConversation, revokeSource, sendAction, sendChatStream, setLocationEnabled, uploadCsv, type AgentActivity } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { widgetTypeIds, type AgentResponse, type Bootstrap, type ConversationSummary, type Message, type Widget, type WidgetActionId } from "@/lib/protocol";
import { cn } from "@/lib/utils";
import { contractLimits } from "@/lib/generated/contracts";
import { activeWidgetId, applyWidgetUpdates, isLegacyAnalysisLifecycleWidget } from "@/lib/widget-state";

const MAX_UPLOAD_BYTES = contractLimits.csvUploadBytes;

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
 *  the focus behaviour, and the scroll lock all hinge on knowing which. */
function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() => typeof window !== "undefined" && window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
}

/** Reports whether a scroll container has content hidden above or below, so a
 *  cut-off list can say so with a fade instead of ending on a hard edge. */
function useScrollEdges<T extends HTMLElement>(dependency: unknown) {
  const ref = useRef<T>(null);
  const [edges, setEdges] = useState({ top: false, bottom: false });
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const update = () => setEdges({ top: node.scrollTop > 4, bottom: Math.ceil(node.scrollTop + node.clientHeight) < node.scrollHeight - 4 });
    update();
    node.addEventListener("scroll", update, { passive: true });
    if (typeof ResizeObserver === "undefined") return () => node.removeEventListener("scroll", update);
    const observer = new ResizeObserver(update);
    observer.observe(node);
    if (node.firstElementChild) observer.observe(node.firstElementChild);
    return () => { node.removeEventListener("scroll", update); observer.disconnect(); };
  }, [dependency]);
  return [ref, edges] as const;
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

function conversationTime(value: string) {
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) ? 0 : parsed;
}

/** Buckets the history by calendar day so a long rail reads as a timeline
 *  rather than one undifferentiated stack of titles. */
function groupConversations(conversations: ConversationSummary[]) {
  const today = new Date().setHours(0, 0, 0, 0);
  const groups = new Map<string, ConversationSummary[]>();
  for (const conversation of [...conversations].sort((a, b) => conversationTime(b.updatedAt) - conversationTime(a.updatedAt))) {
    const updated = conversationTime(conversation.updatedAt);
    const days = updated === 0 ? Infinity : Math.round((today - new Date(updated).setHours(0, 0, 0, 0)) / 86_400_000);
    const label = days <= 0 ? "Today" : days === 1 ? "Yesterday" : days <= 7 ? "Previous 7 days" : days <= 30 ? "Previous 30 days" : "Earlier";
    groups.set(label, [...(groups.get(label) ?? []), conversation]);
  }
  return [...groups];
}

function ConversationRail({ conversations, activeId, user, open, docked, switching, loading, loadingMore, hasMore, onClose, onSelect, onDelete, onLoadMore, onNew, onSavedAnalyses, onOpenSettings, onOpenProfile }: {
  conversations: ConversationSummary[];
  activeId: string;
  user: Bootstrap["user"];
  open: boolean;
  docked: boolean;
  switching: boolean;
  loading: boolean;
  loadingMore: boolean;
  hasMore: boolean;
  onClose: () => void;
  onSelect: (id: string) => void;
  onDelete: (conversation: ConversationSummary) => void;
  onLoadMore: () => void;
  onNew: () => void;
  onSavedAnalyses: () => void;
  onOpenSettings: () => void;
  onOpenProfile: () => void;
}) {
  const groups = useMemo(() => groupConversations(conversations), [conversations]);
  const [listRef, edges] = useScrollEdges<HTMLDivElement>(conversations.length);
  const endRef = useEndOfList(listRef, hasMore && !loadingMore, onLoadMore);
  const activeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => { activeRef.current?.scrollIntoView({ block: "nearest" }); }, [activeId]);

  return <aside
    id="conversation-rail"
    aria-label="Conversations"
    inert={!docked && !open}
    className={cn("ledger fixed inset-y-0 left-0 z-40 flex min-h-0 w-[min(17.5rem,85vw)] flex-col border-r border-line bg-rail pt-[max(0.75rem,env(safe-area-inset-top))] pb-[max(0.75rem,env(safe-area-inset-bottom))] transition-transform duration-300 ease-[cubic-bezier(0.32,0.72,0,1)] md:static md:h-full md:w-auto md:translate-x-0 md:py-3 md:shadow-none md:transition-none", open ? "translate-x-0 shadow-[10px_0_44px_rgba(23,35,31,0.2)]" : "-translate-x-full")}
  >
    <div className="flex shrink-0 items-center pr-2 pl-3">
      <span className="ledger-seal shrink-0">₹</span>
      <div className="min-w-0 pl-2">
        <p className="truncate font-heading text-[13.5px] leading-tight font-semibold tracking-[-0.015em] text-ink">fyn AI</p>
        <p className="ledger-meta mt-1 truncate">Private workspace</p>
      </div>
      <Button variant="ghost" size="icon" aria-label="Close navigation" className="ml-auto shrink-0 rounded-xl text-ink-muted md:hidden" onClick={onClose}><X size={17} /></Button>
    </div>

    <Button onClick={onNew} disabled={switching} className="ledger-new mx-3 mt-4 h-10 shrink-0"><Plus size={15} className="ledger-axis-mark" /> New conversation</Button>

    <div className="relative mt-1 min-h-0 flex-1">
      <div ref={listRef} className="panel-scroll h-full overflow-y-auto">
        {loading
          ? <div role="status" aria-label="Loading your conversations" className="space-y-3 px-4 pt-6">{[0, 1, 2, 3].map((row) => <div key={row} className="h-3 animate-pulse rounded-full bg-line-soft" style={{ width: `${88 - row * 13}%` }} />)}</div>
          : conversations.length === 0
            ? <div className="pt-8 pr-4 pl-[var(--column)]"><p className="text-[12.5px] font-medium text-ink-body">No conversations yet</p><p className="mt-1 text-[11.5px] leading-5 text-ink-muted">Start one and it appears here.</p></div>
            : <nav aria-label="Conversation history" className="relative pb-3">
              <span aria-hidden className="ledger-margin" />
              {groups.map(([label, items]) => <div key={label}>
                <p className="ledger-band">{label}</p>
                {items.map((conversation) => {
                  const posted = conversation.id === activeId;
                  return <div key={conversation.id} className="ledger-row">
                    <button ref={posted ? activeRef : undefined} type="button" aria-current={posted ? "page" : undefined} onClick={() => onSelect(conversation.id)} className="ledger-entry">
                      <span aria-hidden className="ledger-mark" />
                      <span className="line-clamp-2">{conversation.title}</span>
                    </button>
                    <button type="button" onClick={() => onDelete(conversation)} aria-label={`Delete conversation: ${conversation.title}`} className="ledger-strike"><Trash2 size={13} /></button>
                  </div>;
                })}
              </div>)}
              <div ref={endRef} aria-hidden className="h-px" />
              {loadingMore ? <p role="status" className="ledger-meta py-4 pr-4 pl-[var(--column)]">Loading earlier</p> : null}
            </nav>}
      </div>
      <div aria-hidden className={cn("pointer-events-none absolute inset-x-0 top-0 h-5 bg-[linear-gradient(to_bottom,var(--rail),transparent)] transition-opacity duration-200", edges.top ? "opacity-100" : "opacity-0")} />
      <div aria-hidden className={cn("pointer-events-none absolute inset-x-0 bottom-0 h-7 bg-[linear-gradient(to_top,var(--rail),transparent)] transition-opacity duration-200", edges.bottom ? "opacity-100" : "opacity-0")} />
    </div>

    <div className="ledger-close mt-2 shrink-0 pt-2">
      <button type="button" onClick={onSavedAnalyses} className="ledger-link"><Archive size={15} className="ledger-axis-mark" /> Saved analyses</button>
      <button type="button" onClick={onOpenSettings} className="ledger-link"><Settings size={15} className="ledger-axis-mark" /> Settings</button>
      {/* The account block is the button: where you go to see how you sign in is
          where your name already is, rather than a second entry beside it. */}
      <button type="button" onClick={onOpenProfile} className="mt-2 flex w-full items-center rounded-xl px-3 pt-1 pb-1 text-left hover:bg-line-soft/60">
        <span className="ledger-stamp shrink-0">{user.name.slice(0, 1)}</span>
        <div className="min-w-0 pl-2">
          <p className="truncate text-[12.5px] font-medium text-ink-body">{user.name}</p>
          <p className="ledger-meta mt-1 truncate">{user.currency} · {user.timezone}</p>
        </div>
        <UserRound size={15} aria-hidden className="ml-auto shrink-0 text-ink-muted" />
        <span className="sr-only">Profile and sign-in methods</span>
      </button>
    </div>
  </aside>;
}

/** Sends a caller without a session to the sign-in page.
 *
 *  The session cookie is httpOnly, so nothing in the browser can tell whether
 *  one is live — only the server's answer can. A 401 from the first query is
 *  therefore the signal, and it is a redirect rather than an error banner:
 *  "sign in" is a destination, not a failure to report. */
function useSignInGuard(error: unknown) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const signedOut = isUnauthorized(error);
  useEffect(() => {
    if (!signedOut) return;
    // Nothing cached under the ended session may be shown to whoever signs in next.
    queryClient.clear();
    router.replace("/login");
  }, [signedOut, queryClient, router]);
  return signedOut;
}

/** A finished run and a legacy trace are read-only, but `WidgetRenderer` still
 *  wants a handler. One shared no-op, so passing it does not hand the renderer a
 *  new prop on every render and defeat its memoisation. */
const noAction = () => undefined;

/** Marks where the copilot's turn starts. Shared so the reply being written and
 *  the reply already written begin at exactly the same pixel. */
function AssistantByline() {
  return <div className="mb-2 flex items-center gap-2"><span className="grid size-6 place-items-center rounded-full bg-evergreen-tint text-evergreen-ink"><Sparkles size={12} /></span><span className="text-[11px] font-semibold tracking-[0.1em] text-ink-muted uppercase">Copilot</span></div>;
}

/** The reply forming in place: same byline and same run card the finished message
 *  will carry, in the same spot, so landing the answer collapses the run and
 *  fills in the prose rather than moving anything. */
function AgentActivityIndicator({ activities }: { activities: AgentActivity[] }) {
  // Rebuilt only when a step actually streams in. A fresh object every render
  // would be a fresh `widget` prop, and the run card would re-render along with
  // whatever else moved on the page.
  const widget: Widget = useMemo(() => ({
    id: "live-agent-activity",
    type: widgetTypeIds.agent_activity,
    version: 1,
    data: {
      title: "Copilot is working",
      engine: "Copilot",
      model: "live run",
      steps: activities,
      // Folded rather than spread: `Math.max(...array)` passes every element as
      // an argument, which throws once a run is long enough. Runs are short, so
      // this is insurance rather than a fix.
      totalMs: activities.reduce((longest, activity) => Math.max(longest, activity.cumulativeMs), 0),
      live: true,
    },
    actions: [],
  }), [activities]);
  return <div className="max-w-[680px]">
    <AssistantByline />
    <div className="pl-0 sm:pl-8"><WidgetRenderer widget={widget} disabled onAction={noAction} /></div>
  </div>;
}

function AppSkeleton({ label = "Opening your financial conversation…" }: { label?: string }) {
  return <div role="status" className="grid h-dvh place-items-center bg-paper"><div className="flex flex-col items-center gap-3 text-ink-muted"><span className="grid size-12 animate-pulse place-items-center rounded-[18px] bg-evergreen-tint text-evergreen-ink"><Sparkles size={20} /></span><p className="text-sm">{label}</p></div></div>;
}

function ThreadSkeleton() {
  return <div role="status" aria-label="Loading this conversation" className="space-y-7 pt-2">
    {[0, 1].map((row) => <div key={row} className="space-y-2.5">
      <div className="h-3 w-24 animate-pulse rounded-full bg-line-soft" />
      <div className="h-4 w-3/4 animate-pulse rounded-full bg-line-soft" />
      <div className="h-24 animate-pulse rounded-[22px] bg-line-soft/70" />
    </div>)}
  </div>;
}

/** Keeps Tab inside an open overlay, closes it on Escape, and hands focus back
 *  to whatever opened it. */
function useOverlay(open: boolean, onClose: () => void) {
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
    className="rounded-[18px] border-line bg-surface shadow-[0_16px_44px_rgba(26,48,40,0.18)]"
  >
    <ToastContent className="flex-col items-stretch gap-1.5 p-3.5">
      <div className="flex items-center gap-2">
        <ToastTitle className="ledger-meta" />
        <ToastAction className="strike-slip-undo ml-auto" render={<button type="button" />} />
      </div>
      <ToastDescription className="strike-slip-entry text-[12.5px] leading-[1.35] font-medium text-ink-body">
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
  const panelRef = useOverlay(true, onClose);
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
    <button type="button" tabIndex={-1} aria-hidden onClick={onClose} className="scrim-fade fixed inset-0 z-40 bg-[#17231f]/30 backdrop-blur-[2px]" />
    <section ref={panelRef} role="dialog" aria-modal="true" aria-labelledby="privacy-title" className="drawer-right fixed inset-y-0 right-0 z-50 flex w-full max-w-md flex-col border-l border-line bg-surface shadow-[-24px_0_60px_rgba(31,49,42,0.16)]">
      <div className="flex shrink-0 items-center border-b border-line-soft px-5 pt-[max(1.25rem,env(safe-area-inset-top))] pb-4 sm:px-7">
        <span className="grid size-10 shrink-0 place-items-center rounded-2xl bg-evergreen-tint text-evergreen-ink"><ShieldCheck size={19} /></span>
        <div className="ml-3 min-w-0"><h2 id="privacy-title" className="font-heading text-base font-semibold text-ink">Privacy &amp; data</h2><p className="text-xs text-ink-muted">Nothing is collected until you switch it on.</p></div>
        <Button type="button" variant="ghost" size="icon-lg" aria-label="Close privacy settings" onClick={onClose} className="-mr-1 ml-auto rounded-xl text-ink-muted"><X size={17} /></Button>
      </div>

      <div className="panel-scroll min-h-0 flex-1 space-y-6 overflow-y-auto px-5 pt-6 pb-[max(1.75rem,env(safe-area-inset-bottom))] sm:px-7">
        {problem ? <p role="alert" className="flex items-start gap-2 rounded-2xl border border-clay-line bg-clay-tint px-3.5 py-3 text-xs leading-5 text-clay-ink"><TriangleAlert size={15} className="mt-0.5 shrink-0" />{problem}</p> : null}
        {notice ? <p role="status" className="flex items-start gap-2 rounded-2xl border border-evergreen-line bg-evergreen-tint/60 px-3.5 py-3 text-xs leading-5 text-evergreen-ink"><CheckCircle2 size={15} className="mt-0.5 shrink-0" />{notice}</p> : null}
        {privacy.isError ? <p role="alert" className="rounded-2xl border border-clay-line bg-clay-tint px-3.5 py-3 text-xs leading-5 text-clay-ink">Your privacy settings couldn’t be loaded, so they’re hidden rather than shown wrong. <button type="button" onClick={() => privacy.refetch()} className="font-semibold underline">Load them again</button></p> : null}
        {privacy.isLoading ? <div role="status" aria-label="Loading privacy settings" className="space-y-3">{[0, 1, 2].map((row) => <div key={row} className="h-16 animate-pulse rounded-[20px] bg-line-soft/70" />)}</div> : null}

        {privacy.data ? <>
          <div className="rounded-[20px] border border-line p-4">
            <div className="flex items-center gap-3">
              <MapPin size={17} className="shrink-0 text-evergreen-ink" />
              <div className="min-w-0"><p className="text-sm font-semibold text-ink-body">Location enrichment</p><p className="mt-0.5 text-xs leading-5 text-ink-muted">Adds the place a transaction happened. Precise location is never stored.</p></div>
              <button type="button" role="switch" aria-label="Location enrichment" aria-checked={locationEnabled} disabled={run.isPending} onClick={() => run.mutate({ kind: "location", value: !locationEnabled })} className={cn("ml-auto grid h-11 w-14 shrink-0 place-items-center rounded-full disabled:opacity-60", busyControl === "location" && "opacity-70")}>
                <span className={cn("flex h-6 w-11 items-center rounded-full p-0.5 transition-colors", locationEnabled ? "bg-evergreen" : "bg-[#c6cfc9]")}><span className={cn("block size-5 rounded-full bg-white shadow-sm transition-transform", locationEnabled && "translate-x-5")} /></span>
              </button>
            </div>
          </div>

          <div>
            <p className="mb-2 text-[11px] font-semibold tracking-[0.13em] text-ink-muted uppercase">Where transactions can come from</p>
            <div className="divide-y divide-line-soft rounded-[20px] border border-line">
              {sources.map(([source, active]) => <div key={source} className="px-4 py-3">
                <div className="flex items-center gap-3">
                  <div className="min-w-0 flex-1"><p className="text-sm font-medium uppercase text-ink-body">{source}</p><p className="mt-0.5 text-xs leading-5 text-ink-muted">{active ? "Allowed to add transactions" : "Revoked — it can no longer add transactions"}</p></div>
                  {active ? <Button type="button" variant="outline" size="lg" disabled={run.isPending} onClick={() => setConfirmingRevoke(source)} className="shrink-0 rounded-xl px-3 text-xs">Revoke</Button> : <span className="shrink-0 text-xs font-semibold text-clay-ink">Revoked</span>}
                </div>
                {confirmingRevoke === source ? <div className="mt-3 rounded-2xl bg-surface-sunken p-3">
                  <p className="text-xs leading-5 text-ink-body">Revoke {source.toUpperCase()}? Transactions already recorded stay; this source just can’t add more.</p>
                  <div className="mt-2.5 flex flex-wrap gap-2">
                    <Button type="button" size="lg" disabled={run.isPending} onClick={() => run.mutate({ kind: "revoke", value: source })} className="rounded-xl bg-clay px-3 text-xs text-white hover:bg-clay-ink">{busyControl === `revoke:${source}` ? <Loader2 size={14} className="animate-spin" /> : null}Revoke {source.toUpperCase()}</Button>
                    <Button type="button" variant="ghost" size="lg" onClick={() => setConfirmingRevoke(null)} className="rounded-xl px-3 text-xs">Keep it on</Button>
                  </div>
                </div> : null}
              </div>)}
              {!sources.length ? <p className="px-4 py-5 text-xs text-ink-muted">No sources are connected yet.</p> : null}
            </div>
          </div>

          <Button type="button" variant="outline" disabled={run.isPending} onClick={() => run.mutate({ kind: "export" })} className="h-11 w-full rounded-xl">{busyControl === "export" ? <Loader2 size={15} className="animate-spin" /> : <Download size={15} />}{busyControl === "export" ? "Preparing your export…" : "Export my data"}</Button>

          <div className="rounded-[20px] border border-clay-line bg-clay-tint p-4">
            <div className="flex gap-3"><Trash2 size={17} className="mt-0.5 shrink-0 text-clay" /><div><p className="text-sm font-semibold text-clay-ink">Delete all data</p><p className="mt-1 text-xs leading-5 text-clay-ink/85">Permanently removes conversations, transactions, observations, goals, budgets, and preferences. This cannot be undone.</p></div></div>
            <input value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.target.value)} placeholder="Type DELETE MY DATA" aria-label="Deletion confirmation" className="mt-4 h-11 w-full rounded-xl border border-clay-line bg-white px-3 text-sm outline-none focus:border-clay" />
            <Button type="button" disabled={deleteConfirmation !== "DELETE MY DATA" || run.isPending} onClick={() => run.mutate({ kind: "delete" })} className="mt-2 h-11 w-full rounded-xl bg-clay text-white hover:bg-clay-ink disabled:bg-[#e0d3cc] disabled:text-[#8c7b73]">{busyControl === "delete" ? <Loader2 size={15} className="animate-spin" /> : null}{busyControl === "delete" ? "Deleting everything…" : "Delete permanently"}</Button>
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
function Composer({ variant, value, onValueChange, onSubmit, textRef, fileRef, onAttach, busy, sending, disabled, dragging, upload }: {
  variant: "focused" | "docked";
  value: string;
  onValueChange: (value: string) => void;
  onSubmit: (event?: FormEvent) => void;
  textRef: RefObject<HTMLTextAreaElement | null>;
  fileRef: RefObject<HTMLInputElement | null>;
  onAttach: (file: File | undefined) => void;
  busy: boolean;
  sending: boolean;
  disabled: boolean;
  dragging: boolean;
  upload: { name: string; percent: number } | null;
}) {
  const focused = variant === "focused";
  return <form onSubmit={onSubmit} className={cn("pointer-events-auto mx-auto w-full", !focused && "max-w-[790px]")}>
    {upload ? <div role="status" className="mb-2 flex items-center gap-3 rounded-2xl border border-line bg-surface px-3.5 py-2.5 text-xs text-ink-body shadow-sm"><Loader2 size={14} className="shrink-0 animate-spin text-evergreen-ink" /><span className="min-w-0 flex-1 truncate">Uploading {upload.name}</span><span className="money shrink-0 text-ink-muted">{upload.percent}%</span><span aria-hidden className="h-1 w-20 shrink-0 overflow-hidden rounded-full bg-line-soft"><span className="block h-full rounded-full bg-evergreen transition-[width]" style={{ width: `${upload.percent}%` }} /></span></div> : null}
    <div data-dropping={dragging || undefined} className="entry-card p-1.5">
      {/* 14px of text inset is not arbitrary: it is where a 16px glyph lands
          inside a 44px control, so the first character of what you write sits
          on the same vertical as the paperclip below it. */}
      <Textarea id="composer" ref={textRef} value={value} disabled={disabled} onChange={(event) => onValueChange(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); onSubmit(); } }} placeholder={disabled ? "Opening conversation…" : focused ? "Spent ₹500 on lunch" : "Ask anything about your finances…"} aria-label="Message fyn AI" aria-describedby="composer-hint" rows={1} className="max-h-36 min-h-11 resize-none border-0 bg-transparent px-3.5 py-2.5 text-[15px] leading-6 shadow-none placeholder:text-ink-muted focus-visible:ring-0" />
      <div className="flex items-center gap-2">
        <input ref={fileRef} type="file" accept=".csv,text/csv" className="sr-only" tabIndex={-1} aria-hidden aria-label="Choose a CSV statement" onChange={(event) => { onAttach(event.target.files?.[0]); event.currentTarget.value = ""; }} />
        <Tooltip><TooltipTrigger render={<Button type="button" variant="ghost" size="icon-lg" disabled={busy} onClick={() => fileRef.current?.click()} className="size-11 shrink-0 rounded-[13px] text-ink-muted hover:bg-surface-sunken hover:text-evergreen-ink" aria-label="Attach a CSV statement" />}><Paperclip size={16} /></TooltipTrigger><TooltipContent>Attach a CSV statement, or drop one anywhere</TooltipContent></Tooltip>
        {/* The one piece of small print the composer carries, and only because
            filing happens without asking. That it can be undone is not said
            here: every filed entry carries its own Edit and Remove. Nor is
            whose data this is — the header already says so. */}
        <p id="composer-hint" className="entry-hint -ml-1"><CheckCircle2 size={12} className="shrink-0" /><span className="truncate">Complete entries are added automatically</span></p>
        <Button type="submit" size="icon-lg" disabled={!value.trim() || busy} className="ml-auto size-11 shrink-0 rounded-[13px] bg-evergreen text-white hover:bg-evergreen-deep disabled:bg-[#e4e8e3] disabled:text-[#9aa49e]" aria-label="Send message">{sending ? <Loader2 size={16} className="animate-spin" /> : <SendHorizontal size={16} />}</Button>
      </div>
    </div>
  </form>;
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
const MessageArticle = memo(function MessageArticle({ message, activeWidget, usedWidgets, pendingWidget, busy, citationsOpen, onToggleCitations, onAction }: {
  message: Message;
  activeWidget: string | null;
  usedWidgets: Set<string>;
  pendingWidget: string | null;
  busy: boolean;
  citationsOpen: boolean;
  onToggleCitations: (messageId: string) => void;
  onAction: WidgetAction;
}) {
  // The run trace is how the answer was reached, so it reads above the answer —
  // not appended after the widgets it produced, which is the order the backend
  // stores it in.
  const trace = message.widgets.find((widget) => widget.type === widgetTypeIds.agent_activity);
  const widgets = message.widgets.filter((widget) => widget.id !== trace?.id && !isLegacyAnalysisLifecycleWidget(widget));
  return <article className={cn("group", message.role === "user" ? "flex justify-end" : "max-w-[680px]")}>
    <div className={cn("min-w-0", message.role === "user" && "max-w-[82%]")}>
      {message.role === "assistant" ? <AssistantByline /> : null}
      {trace ? <div className="mb-2.5 pl-0 sm:pl-8"><WidgetRenderer widget={trace} disabled onAction={noAction} /></div> : null}
      {message.content ? message.role === "user"
        ? <div className="w-fit break-words whitespace-pre-wrap rounded-[20px_20px_5px_20px] bg-[#234f44] px-4 py-2.5 text-[15px] leading-6 text-[#f5faf7] shadow-sm">{message.content}</div>
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
          <WidgetRenderer widget={widget} disabled={!active || usedWidgets.has(widget.id) || busy} pending={pendingWidget === widget.id} onAction={onAction} />
        </div>;
      })}</div> : null}
      {message.citations.length ? <div className="mt-2 ml-8">
        <button type="button" aria-expanded={citationsOpen} onClick={() => onToggleCitations(message.id)} className="flex min-h-8 items-center gap-1.5 rounded-lg text-[11px] font-medium text-ink-muted hover:text-evergreen-ink"><FileText size={12} /> {message.citations.length} data source{message.citations.length === 1 ? "" : "s"}</button>
        {citationsOpen ? <ul className="mt-1.5 space-y-1 rounded-2xl border border-line-soft bg-surface px-3.5 py-3">{message.citations.map((citation, index) => <li key={index} className="flex gap-2 text-[11px] leading-5 text-ink-muted"><span aria-hidden className="text-evergreen-ink">•</span><span><span className="font-medium text-ink-body">{typeof citation.label === "string" ? citation.label : "Recorded data"}</span>{typeof citation.entity_type === "string" ? ` · ${citation.entity_type.replaceAll("_", " ")}` : ""}</span></li>)}</ul> : null}
      </div> : null}
    </div>
  </article>;
});

/** The thread itself, held behind the same boundary and for the same reason.
 *  Every prop it takes is either state that does not move while you type or a
 *  callback held stable by the workspace, so a keystroke stops here. */
const Transcript = memo(function Transcript({ messages, agentActivities, streaming, busy, usedWidgets, pendingWidget, openCitations, activeWidget, activeWidgetFocusKey, error, retry, onAction, onActiveWidgetFocus, onToggleCitations, onRetry, scrollRef }: {
  messages: Message[];
  agentActivities: AgentActivity[];
  streaming: boolean;
  busy: boolean;
  usedWidgets: Set<string>;
  pendingWidget: string | null;
  openCitations: Set<string>;
  activeWidget: string | null;
  activeWidgetFocusKey: string | null;
  error: string | null;
  retry: Retry;
  onAction: WidgetAction;
  onActiveWidgetFocus: () => void;
  onToggleCitations: (messageId: string) => void;
  onRetry: () => void;
  scrollRef: RefObject<HTMLDivElement | null>;
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
    // Only an opening guess. Turns here run from a one-line question to a chart,
    // so every row is measured once it mounts and the estimate stops mattering.
    estimateSize: () => 220,
    getItemKey: (index) => messages[index].id,
    // Generous, because a row that scrolls in unmeasured resizes the moment it
    // does, and doing that at the edge of the viewport is what reads as jitter.
    overscan: 8,
    scrollMargin,
  });

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
    reveal();
    return () => cancelAnimationFrame(focusFrame.current);
  }, [activeWidget, activeWidgetFocusKey, messages, onActiveWidgetFocus, scrollRef, virtualizer]);

  const rows = virtualizer.getVirtualItems();
  return <div role="log" aria-busy={streaming}>
    <div ref={listRef} style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
      {rows.map((row) => <div
        key={row.key}
        data-index={row.index}
        ref={virtualizer.measureElement}
        style={{ position: "absolute", insetInlineStart: 0, top: 0, width: "100%", transform: `translateY(${row.start - scrollMargin}px)` }}
      >
        {/* The rhythm the removed `space-y-7` used to hold. It belongs on the
            row rather than between rows now, because absolutely positioned
            siblings have no gap to share. */}
        <div className="pb-7">
          <MessageArticle
            message={messages[row.index]}
            activeWidget={activeWidget}
            usedWidgets={usedWidgets}
            pendingWidget={pendingWidget}
            busy={busy}
            citationsOpen={openCitations.has(messages[row.index].id)}
            onToggleCitations={onToggleCitations}
            onAction={onAction}
          />
        </div>
      </div>)}
    </div>
    {streaming ? <div aria-hidden><AgentActivityIndicator activities={agentActivities} /></div> : null}
    {busy && !streaming ? <div className="mt-7 flex items-center gap-3 px-1 py-2 text-sm text-ink-muted"><span className="grid size-7 place-items-center rounded-full bg-evergreen-tint text-evergreen-ink"><Sparkles size={13} /></span><span className="flex gap-1" aria-hidden><i className="typing-dot" /><i className="typing-dot" /><i className="typing-dot" /></span><span className="sr-only">Working on it</span></div> : null}
    {error ? <div role="alert" className="mt-7 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-2xl border border-clay-line bg-clay-tint px-4 py-3 text-xs leading-5 text-clay-ink sm:mx-8"><TriangleAlert size={15} className="shrink-0" /><span className="min-w-0 flex-1">{error}</span>{retry ? <Button type="button" variant="outline" size="lg" onClick={onRetry} className="rounded-xl border-clay-line bg-white text-xs text-clay-ink hover:bg-clay-tint"><RotateCcw size={14} /> Try again</Button> : null}</div> : null}
  </div>;
});

/** What the shell needs to reach inside the thread for: the rail's Saved
 *  analyses sends a prompt, and a file dropped anywhere lands on the importer.
 *  A handle keeps those two reachable without lifting the thread's state. */
type ThreadHandle = { sendPrompt: (text: string) => void; attach: (file: File | undefined) => void };

function CopilotWorkspace({ initialData, loadingThread, navOpen, onOpenNav, switching, dragging, handleRef }: {
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
  const [openCitations, setOpenCitations] = useState<Set<string>>(new Set());
  const [upload, setUpload] = useState<{ name: string; percent: number } | null>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [announcement, setAnnouncement] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  // Two pieces of scheduled work that can outlive the render that started them,
  // so they are held rather than fired and forgotten.
  const copiedTimer = useRef<number | undefined>(undefined);
  const settleFrame = useRef(0);
  // One controller for everything this thread has in flight. A conversation is
  // torn down and rebuilt whenever you switch to another one, so aborting here
  // is what tells a run nobody is watching it any more — a reply takes seconds
  // to arrive, which is long enough to walk away from.
  //
  // Created inside the effect, deliberately. Holding it in state instead keeps
  // a single controller across React's development remount, so the discarded
  // first mount aborts the one the real mount goes on to use and every request
  // afterwards starts life already cancelled.
  const inFlight = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    inFlight.current = controller;
    return () => {
      window.clearTimeout(copiedTimer.current);
      cancelAnimationFrame(settleFrame.current);
      controller.abort();
      inFlight.current = null;
    };
  }, []);

  const succeeded = useCallback((response: AgentResponse) => {
    setMessages((current) => [...applyWidgetUpdates(current, response.widgetUpdates), responseToMessage(response)]);
    setError(null);
    setRetry(null);
    setConnectionLost(false);
    setAnnouncement("");
    // Cache refresh is bookkeeping, not part of the financial action. Keeping
    // the mutation pending until an arbitrarily long history refetch completes
    // would disable the next HITL widget even though the backend already
    // returned it successfully.
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: ["conversations"] }),
      queryClient.invalidateQueries({ queryKey: ["conversation", conversationId] }),
    ]);
  }, [queryClient, conversationId]);

  const failed = useCallback((cause: Error, next: Retry) => {
    // An abort is this thread being closed, not something that went wrong with
    // it. There is nobody left to tell, and nothing to offer to retry.
    if (cause.name === "AbortError") return;
    setError(cause.message);
    setRetry(next);
    setConnectionLost(/reach|offline|connection/i.test(cause.message));
    // The banner itself is role="alert"; announcing it again would double up.
    setAnnouncement("");
  }, []);

  const chatMutation = useMutation({
    mutationFn: ({ id, text }: { id: string; text: string }) => sendChatStream(id, text, (activity) => {
      setAgentActivities((current) => {
        const index = current.findIndex((item) => item.id === activity.id);
        if (index === -1) return [...current, activity];
        return current.map((item, itemIndex) => itemIndex === index ? activity : item);
      });
    }, inFlight.current?.signal),
    onMutate: () => { setAgentActivities([]); setAnnouncement("Copilot is working on your message."); },
    onSuccess: (response) => { setAgentActivities([]); succeeded(response); },
    // A message that never reached the server should not look delivered: drop
    // the bubble and put the text back where the user can send it again.
    onError: (cause: Error, variables) => {
      setAgentActivities([]);
      setMessages((current) => current.filter((message) => !(message.id.startsWith("optimistic-") && message.content === variables.text)));
      setInput((current) => current || variables.text);
      failed(cause, { kind: "chat", text: variables.text });
    },
  });
  const actionMutation = useMutation({
    mutationFn: ({ widgetId, action, payload, markUsed }: { widgetId: string; action: WidgetActionId; payload: Record<string, unknown>; markUsed: boolean }) => sendAction(conversationId, widgetId, action, payload, markUsed),
    onSuccess: (response, variables) => {
      setPendingWidget(null);
      // Locking the card belongs to the success path; a failed action has to
      // stay clickable or the user is stuck until a reload.
      if (variables.markUsed) setUsedWidgets((current) => new Set(current).add(variables.widgetId));
      succeeded(response);
    },
    onError: (cause: Error, variables) => { setPendingWidget(null); failed(cause, { kind: "action", ...variables }); },
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
  const { mutate: startUpload, isPending: uploadPending } = uploadMutation;

  // Declared above the handlers below rather than beside the render: they are
  // dependencies of those handlers now, and a `const` read from a dependency
  // list has to already exist by the time the list is evaluated.
  const busy = switchingConversation || chatPending || actionPending || uploadPending;
  const activeInteractionWidgetId = useMemo(() => activeWidgetId(messages), [messages]);
  const activeWidgetFocusKey = activeInteractionWidgetId && messages.at(-1)?.role === "assistant"
    ? `${messages.at(-1)?.id}:${activeInteractionWidgetId}`
    : null;
  const stopFollowingForHitl = useCallback(() => {
    cancelAnimationFrame(settleFrame.current);
    setAtBottom(false);
  }, []);

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
    if (!text || chatPending || actionPending || uploadPending) return;
    setInput(""); setError(null); setRetry(null);
    setAtBottom(true);
    setMessages((current) => [...current, { id: `optimistic-${Date.now()}`, role: "user", content: text, widgets: [], citations: [], created_at: new Date().toISOString() }]);
    startChat({ id: conversationId, text });
  }, [chatPending, actionPending, uploadPending, startChat, conversationId]);

  function submit(event?: FormEvent) {
    event?.preventDefault();
    sendPrompt(input);
  }

  const attach = useCallback((file: File | undefined) => {
    if (!file || busy) return;
    const problem = csvProblem(file);
    if (problem) { failed(new Error(problem), null); return; }
    setError(null);
    setAtBottom(true);
    startUpload(file);
  }, [busy, failed, startUpload]);

  const handleWidgetAction = useCallback<WidgetAction>((widgetId, action, payload, options) => {
    // Per-row widgets (avoidable expenses, the calculators) opt out of the lock
    // so one decision does not retire every other control on the card.
    const markUsed = options?.markUsed !== false;
    if (widgetId !== activeInteractionWidgetId || pendingWidget || (markUsed && usedWidgets.has(widgetId))) return;
    setError(null);
    setPendingWidget(widgetId);
    startAction({ widgetId, action, payload, markUsed });
  }, [activeInteractionWidgetId, pendingWidget, usedWidgets, startAction]);

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
  const readerScrolled = useRef(false);
  const noteReaderScroll = () => { readerScrolled.current = true; };

  function trackScroll(event: React.UIEvent<HTMLDivElement>) {
    const node = event.currentTarget;
    const nearBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 120;
    if (nearBottom) {
      readerScrolled.current = false;
      setAtBottom(true);
      return;
    }
    if (readerScrolled.current) setAtBottom(false);
  }

  // By index, not by pixel. Only the turns near the viewport are mounted, so
  // the height of everything above the last one is estimated until it has been
  // scrolled through — `scrollHeight` is a guess and landing on it lands short.
  // Asking for the final row instead lets the virtualiser converge as the rows
  // it passes report their real heights.
  // The end of the thread is not a position the virtualiser can be asked for.
  // `scrollToIndex` aligns the last turn's own edge with the bottom of the
  // viewport, which is underneath the composer — the column reserves room for
  // the dock precisely so the last turn clears it, and that reserved space is
  // below the final row. It also keeps steering back to its own target, so it
  // cannot simply be corrected afterwards.
  //
  // So: scroll to the bottom, let the rows that mount there report their real
  // heights, and scroll again now that the floor has moved. Two or three frames
  // is usually enough; it stops as soon as the height holds still.
  const scrollToEnd = useCallback((behavior: ScrollBehavior) => {
    const node = scrollRef.current;
    if (!node) return;
    let frames = 0;
    const settle = () => {
      node.scrollTo({ top: node.scrollHeight, behavior: frames === 0 ? behavior : "auto" });
      // Judged by where it landed, not by whether the height looked stable.
      // The rows measure themselves in a layout effect that runs after this
      // frame's scroll, so a height read before scrolling can be unchanged and
      // then grow immediately afterwards — which is exactly how a refreshed
      // thread used to stop 800px short of its own last reply.
      const short = node.scrollHeight - node.scrollTop - node.clientHeight > 1;
      if (short && ++frames < 30) {
        // Held so a thread that unmounts mid-settle does not leave frames
        // queued against a scroll container that is no longer on the page.
        settleFrame.current = requestAnimationFrame(settle);
      }
    };
    cancelAnimationFrame(settleFrame.current);
    settle();
  }, []);

  function jumpToLatest() {
    setAtBottom(true);
    scrollToEnd(prefersReducedMotion() ? "auto" : "smooth");
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
    if (!scrollRef.current || !atBottom) return;
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
      if (!node) return;
      if (node.scrollHeight - node.scrollTop - node.clientHeight > 1) {
        node.scrollTo({ top: node.scrollHeight, behavior: "auto" });
      }
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [focusedMode, atBottom]);

  const arrivals = useRef<string | null>(null);
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

  // No dependency list: the shell calls through this handle at arbitrary times,
  // so it has to hold this render's closures, not the first render's.
  useEffect(() => {
    handleRef.current = { sendPrompt, attach };
    return () => { handleRef.current = null; };
  });

  return <main className="relative flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-paper-raised">
        <header className="z-20 flex h-16 shrink-0 items-center gap-2 border-b border-line-soft bg-paper-raised/90 px-3 backdrop-blur-xl sm:px-6">
          <Button variant="ghost" size="icon-lg" aria-label="Open navigation" aria-expanded={navOpen} aria-controls="conversation-rail" className="rounded-xl md:hidden" onClick={onOpenNav}><Menu size={18} /></Button>
          <div className="min-w-0">
            <h1 className="truncate font-heading text-sm font-semibold text-ink">{title}</h1>
            <p className={cn("mt-0.5 flex items-center gap-1.5 text-[11px] font-medium", connectionLost ? "text-clay-ink" : "text-ink-muted")}><span className={cn("size-1.5 rounded-full", connectionLost ? "bg-clay" : "bg-[#3f8a68]")} />{connectionLost ? "Can’t reach your financial data" : "Financial data connected"}</p>
          </div>
          <Tooltip><TooltipTrigger render={<Button type="button" variant="ghost" size="icon-lg" onClick={copyConversationLink} aria-label={linkCopied ? "Conversation link copied" : "Copy conversation link"} className="ml-auto rounded-xl text-ink-muted" />}>{linkCopied ? <Check size={17} /> : <Copy size={16} />}</TooltipTrigger><TooltipContent>{linkCopied ? "Link copied" : "Copy conversation link"}</TooltipContent></Tooltip>
        </header>

        <div
          ref={scrollRef}
          onScroll={trackScroll}
          onWheel={noteReaderScroll}
          onTouchMove={noteReaderScroll}
          onKeyDown={noteReaderScroll}
          data-docked={!focusedMode}
          className="conversation-scroll flex-1 overflow-y-auto"
        >
          {loadingThread ? <div className="mx-auto flex min-h-full w-full max-w-[790px] flex-col px-4 pt-8 pb-[calc(var(--dock-h)+2.5rem)] sm:px-6 sm:pt-12"><ThreadSkeleton /></div> : focusedMode ? <div className="leaf mx-auto flex min-h-full w-full max-w-[34rem] flex-col justify-center px-4 py-12 sm:px-6">
            <span aria-hidden className="leaf-seal">₹</span>
            <h2 className="leaf-title mt-5">What happened?</h2>
            <div className="mt-6"><Composer variant="focused" value={input} onValueChange={setInput} onSubmit={submit} textRef={textRef} fileRef={fileRef} onAttach={attach} busy={busy} sending={chatPending} disabled={switchingConversation} dragging={dragging} upload={upload} /></div>
            {error ? <div role="alert" className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-2xl border border-clay-line bg-clay-tint px-4 py-3 text-xs leading-5 text-clay-ink"><TriangleAlert size={15} className="shrink-0" /><span className="min-w-0 flex-1">{error}</span>{retry ? <Button type="button" variant="outline" size="lg" onClick={retryLast} className="rounded-xl border-clay-line bg-white text-xs text-clay-ink hover:bg-clay-tint"><RotateCcw size={14} /> Try again</Button> : null}</div> : null}
            <p className="leaf-band mt-9">Try</p>
            <div className="mt-1">{STARTERS.map((starter) => <button key={starter} type="button" onClick={() => applyStarter(starter)} className="leaf-example"><span aria-hidden className="ledger-mark" />{starter}</button>)}</div>
          </div> : <div ref={contentRef} className="mx-auto flex min-h-full w-full max-w-[790px] flex-col px-4 pt-8 pb-[calc(var(--dock-h)+2.5rem)] sm:px-6 sm:pt-12">
            <Transcript
              messages={messages}
              agentActivities={agentActivities}
              streaming={chatPending}
              busy={busy}
              usedWidgets={usedWidgets}
              pendingWidget={pendingWidget}
              openCitations={openCitations}
              activeWidget={activeInteractionWidgetId}
              activeWidgetFocusKey={activeWidgetFocusKey}
              error={error}
              retry={retry}
              onAction={handleWidgetAction}
              onActiveWidgetFocus={stopFollowingForHitl}
              onToggleCitations={toggleCitations}
              onRetry={retryLast}
              scrollRef={scrollRef}
            />
          </div>}
        </div>

        <p aria-live="polite" aria-atomic className="sr-only">{announcement}</p>

        {!focusedMode ? <>
          {/* Rides above the dock rather than inside it: were it measured with
              the dock, appearing would enlarge the transcript's bottom margin
              and jolt the very scroll position it exists to restore. */}
          {!atBottom ? <div style={{ bottom: `calc(var(--dock-h) + 0.75rem)` }} className="pointer-events-none absolute inset-x-0 z-20 flex justify-center px-3 sm:px-6"><Button type="button" onClick={jumpToLatest} className="pointer-events-auto h-10 rounded-full bg-surface px-4 text-xs text-ink-body shadow-[0_6px_20px_rgba(26,48,40,0.16)] ring-1 ring-line hover:bg-surface"><ArrowDown size={14} /> Jump to latest</Button></div> : null}
          <div ref={dockRef} className="entry-dock pointer-events-auto absolute inset-x-0 bottom-0 z-20 px-3 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:px-6 sm:pt-3.5 sm:pb-4">
            <Composer variant="docked" value={input} onValueChange={setInput} onSubmit={submit} textRef={textRef} fileRef={fileRef} onAttach={attach} busy={busy} sending={chatPending} disabled={switchingConversation} dragging={dragging} upload={upload} />
          </div>
        </> : null}
      </main>;
}

type ShellValue = { navOpen: boolean; openNav: () => void; switching: boolean; dragging: boolean; handleRef: RefObject<ThreadHandle | null>; conversations: ConversationSummary[] };
const ShellContext = createContext<ShellValue | null>(null);

function useShell() {
  const value = useContext(ShellContext);
  if (!value) throw new Error("The conversation thread must be rendered inside WorkspaceShell.");
  return value;
}

/** App chrome, and the reason it lives in a layout rather than in the page.
 *
 *  Two separate things tear the thread down on every conversation click: the
 *  `key` on `CopilotWorkspace`, which is deliberate — it re-seeds thread state
 *  from the freshly loaded conversation — and Next remounting the page subtree
 *  whenever the `[conversationId]` segment changes, which is not something a
 *  component inside the page can opt out of. Rendered from `app/c/layout.tsx`
 *  this shell sits above both, so the rail keeps its scroll position, its
 *  transitions and its DOM node while the thread underneath is replaced. */
export function WorkspaceShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const params = useParams<{ conversationId?: string }>();
  const conversationId = params?.conversationId ?? "";
  const initial = useQuery({ queryKey: ["bootstrap"], queryFn: bootstrap });
  const signedOut = useSignInGuard(initial.error);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [switchingFrom, setSwitchingFrom] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [navError, setNavError] = useState<string | null>(null);
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
      router.replace(`/c/${encodeURIComponent(id)}`, { scroll: false });
    };
  });

  // A delete you walked away from is still a delete: send whatever is still
  // inside its window before the page goes, rather than silently keeping it.
  useEffect(() => {
    const flush = () => pending.current.forEach((removal, id) => { if (!removal.undone) flushConversationDeletion(id); });
    window.addEventListener("pagehide", flush);
    return () => window.removeEventListener("pagehide", flush);
  }, []);

  function removeConversation(conversation: ConversationSummary) {
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
    if (next) router.replace(`/c/${encodeURIComponent(next.id)}`, { scroll: false });
    else void startConversation("replace");
  }

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

  function selectConversation(id: string) {
    if (id === conversationId) { setSidebarOpen(false); return; }
    setSwitchingFrom(conversationId);
    setNavError(null);
    setSidebarOpen(false);
    router.push(`/c/${encodeURIComponent(id)}`, { scroll: false });
  }

  async function startConversation(mode: "push" | "replace" = "push") {
    setSwitchingFrom(conversationId);
    setNavError(null);
    try {
      const conversation = await createConversation();
      setSidebarOpen(false);
      await Promise.all([queryClient.invalidateQueries({ queryKey: ["bootstrap"] }), queryClient.invalidateQueries({ queryKey: ["conversations"] })]);
      router[mode](`/c/${encodeURIComponent(conversation.id)}`, { scroll: false });
    } catch { setSwitchingFrom(null); setNavError("The conversation couldn’t be started. Try again."); }
  }

  if (signedOut) return <AppSkeleton label="Taking you to sign in…" />;
  if (initial.isError) return <WorkspaceUnreachable onRetry={() => initial.refetch()} retrying={initial.isFetching} />;
  if (initial.isLoading || !initial.data) return <AppSkeleton />;

  return <ToastProvider toastManager={toast} limit={5}>
    <ShellContext.Provider value={{ navOpen: sidebarOpen, openNav, switching, dragging, handleRef: thread, conversations }}>
    <div className="h-dvh overflow-hidden bg-paper text-ink" onDragOver={(event) => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); setDragging(true); } }} onDragLeave={(event) => { if (event.currentTarget === event.target) setDragging(false); }} onDrop={(event) => { if (!event.dataTransfer.files.length) return; event.preventDefault(); setDragging(false); thread.current?.attach(event.dataTransfer.files[0]); }}>
      <div className="relative mx-auto grid h-full max-w-[1600px] md:grid-cols-[280px_1fr]">
        <button type="button" tabIndex={-1} aria-hidden onClick={() => setSidebarOpen(false)} className={cn("fixed inset-0 z-30 bg-[#17231f]/30 backdrop-blur-[2px] transition-opacity duration-300 md:hidden", sidebarOpen ? "opacity-100" : "pointer-events-none opacity-0")} />
        <ConversationRail
          conversations={conversations}
          activeId={conversationId}
          user={initial.data.user}
          open={sidebarOpen}
          docked={isDesktop}
          switching={switching}
          loading={history.isPending}
          loadingMore={history.isFetchingNextPage}
          hasMore={Boolean(history.hasNextPage)}
          onClose={() => setSidebarOpen(false)}
          onSelect={selectConversation}
          onDelete={removeConversation}
          onLoadMore={loadMore}
          onNew={() => startConversation()}
          onSavedAnalyses={() => { setSidebarOpen(false); thread.current?.sendPrompt("Show my saved analyses"); }}
          onOpenSettings={() => { setSettingsOpen(true); setSidebarOpen(false); }}
          onOpenProfile={() => { setSidebarOpen(false); router.push("/profile"); }}
        />
        {children}
        {navError ? <div role="alert" className="fixed inset-x-0 bottom-4 z-50 mx-auto w-fit max-w-[90vw] rounded-2xl border border-clay-line bg-clay-tint px-4 py-2.5 text-xs text-clay-ink shadow-[0_8px_28px_rgba(31,49,42,0.14)]">{navError}</div> : null}
        {/* Deleting everything deletes the account itself, so there is nothing
            left to return to — the session is already void server-side. */}
        {settingsOpen ? <PrivacyDrawer onClose={closeSettings} onDeleted={() => { queryClient.clear(); router.replace("/login"); }} /> : null}
      </div>
    </div>
    </ShellContext.Provider>
    {/* Clear of the composer, which owns the bottom of the column, and of the
        rail down the left: the stack lands in the empty band between them.
        It rides on the measured dock rather than a matching guess, so the two
        cannot drift apart when the box grows. */}
    <ToastPortal>
      <ToastViewport className="inset-x-3 bottom-[calc(var(--dock-h)+0.75rem)] mx-auto w-auto max-w-[20rem] md:right-auto md:left-[max(292px,calc(50vw-508px))] md:mx-0 md:w-full">
        <UndoToastList />
      </ToastViewport>
    </ToastPortal>
  </ToastProvider>;
}

function ConversationUnavailable({ onOpenLatest }: { onOpenLatest: () => void }) {
  return <div className="grid h-dvh place-items-center bg-paper p-6"><div role="alert" className="max-w-sm rounded-[24px] border border-line bg-surface p-7 text-center shadow-[0_16px_50px_rgba(26,48,40,0.1)]"><span className="mx-auto grid size-11 place-items-center rounded-[17px] bg-evergreen-tint text-evergreen-ink"><MessageSquareText size={19} /></span><h1 className="mt-4 font-heading text-lg font-semibold text-ink">Conversation unavailable</h1><p className="mt-2 text-sm leading-6 text-ink-muted">This link is invalid, the conversation was deleted, or it belongs to another account.</p><Button type="button" onClick={onOpenLatest} className="mt-5 h-11 rounded-xl bg-evergreen px-4 text-white hover:bg-evergreen-deep">Open latest conversation</Button></div></div>;
}

function WorkspaceUnreachable({ onRetry, retrying }: { onRetry: () => void; retrying: boolean }) {
  return <div className="grid h-dvh place-items-center bg-paper p-6"><div role="alert" className="max-w-sm rounded-[24px] border border-clay-line bg-surface p-7 text-center shadow-[0_16px_50px_rgba(26,48,40,0.1)]"><span className="mx-auto grid size-11 place-items-center rounded-[17px] bg-clay-tint text-clay"><TriangleAlert size={19} /></span><h1 className="mt-4 font-heading text-lg font-semibold text-ink">We couldn’t load your workspace</h1><p className="mt-2 text-sm leading-6 text-ink-muted">Nothing was lost. Check your connection and try again.</p><Button type="button" onClick={onRetry} disabled={retrying} className="mt-5 h-11 rounded-xl bg-evergreen px-4 text-white hover:bg-evergreen-deep">{retrying ? <Loader2 size={15} className="animate-spin" /> : <RotateCcw size={15} />}{retrying ? "Trying again…" : "Try again"}</Button></div></div>;
}

/** The thread for one conversation. Rendered as the shell's child, so this is
 *  the only part Next replaces when the `[conversationId]` segment changes. */
export function ConversationThread({ conversationId }: { conversationId: string }) {
  const router = useRouter();
  const shell = useShell();
  const initial = useQuery({ queryKey: ["bootstrap"], queryFn: bootstrap });
  const conversation = useQuery({
    queryKey: ["conversation", conversationId],
    queryFn: () => loadConversation(conversationId),
    retry: false,
  });

  if (!initial.data) return <main className="min-h-0 bg-paper-raised" />;
  // A thread being navigated away from — the one just deleted, say — is allowed
  // to stop loading without the shell accusing the link of being broken.
  if (conversation.isError && !shell.switching) return <ConversationUnavailable onOpenLatest={() => router.replace(`/c/${encodeURIComponent(initial.data.active_conversation.id)}`)} />;

  // Keep the shell up while a thread loads instead of blanking the whole app.
  const known = shell.conversations.find((item) => item.id === conversationId);
  const activeConversation = conversation.data ?? { id: conversationId, title: known?.title ?? "Opening conversation", messages: [], updated_at: known?.updatedAt ?? "" };
  const prepared = { ...initial.data, active_conversation: activeConversation };
  return <CopilotWorkspace
    key={`${activeConversation.id}:${conversation.data ? "ready" : "loading"}`}
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
  const router = useRouter();
  const initial = useQuery({ queryKey: ["bootstrap"], queryFn: bootstrap });
  const signedOut = useSignInGuard(initial.error);
  useEffect(() => {
    if (initial.data) router.replace(`/c/${encodeURIComponent(initial.data.active_conversation.id)}`, { scroll: false });
  }, [initial.data, router]);

  if (signedOut) return <AppSkeleton label="Taking you to sign in…" />;
  if (initial.isError) return <WorkspaceUnreachable onRetry={() => initial.refetch()} retrying={initial.isFetching} />;
  return <AppSkeleton label="Opening your latest conversation…" />;
}
