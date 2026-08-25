import { useInfiniteQuery, useMutation, useQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { ArrowDownLeft, ArrowUpRight, CheckCircle2, Eye, EyeOff, History, Loader2, PencilLine, Plus, ReceiptText, RotateCcw, Search, Trash2, X } from "lucide-react";
import { type CSSProperties, type UIEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { GroupedVirtuoso, type Components, type ContextProp, type GroupProps } from "react-virtuoso";
import { useSearchParams } from "react-router";
import { CategoryManager, type CategoryUsage } from "@/components/category-manager";
import { TransactionForm } from "@/components/transaction-form";
import { TransactionIdentifier } from "@/components/transaction-identifier";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { SITE_HEADER_HEIGHT, SiteHeader, useAutoHideSiteHeader } from "@/components/ui/site-header";
import { toast, UNDO_WINDOW_MS } from "@/components/ui/toast";
import { useWorkspaceOverlay, useWorkspaceShell } from "@/components/workspace";
import { createCategory, createSubcategory, createTransactionHint, createTransactionRecord, deleteCategory, deleteSubcategory, deleteTransactionHint, getPrivacyStatus, loadCategories, loadOverview, loadTransactionRevisions, loadTransactions, removeTransaction, renameCategory, renameSubcategory, restoreTransaction, updateTransaction, updateTransactionHint } from "@/lib/api";
import { formatInstant, formatMoney, formatTransactionClassification } from "@/lib/format";
import { usePlainKey } from "@/lib/shortcuts";
import { groupTransactionsByDay } from "@/lib/transaction-groups";
import { editableTransactionTypes, type CategoryDirectoryOut, type CategoryDirectorySubcategoryOut, type TransactionListItemOut, type TransactionRevisionOut, type TransactionUpdateIn } from "@/lib/protocol";
import { cn } from "@/lib/utils";

const dayFormatter = new Intl.DateTimeFormat("en-IN", { weekday: "short", day: "numeric", month: "long", year: "numeric" });
const timeFormatter = new Intl.DateTimeFormat("en-IN", { hour: "numeric", minute: "2-digit" });

function titleCase(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function MoneyPageHeader({ title, subtitle, hidden = false }: { title: string; subtitle: string; hidden?: boolean }) {
  const shell = useWorkspaceShell();
  return <SiteHeader title={title} subtitle={subtitle} subtitleClassName="hidden sm:block" hidden={hidden} navOpen={shell.navOpen} onOpenNav={shell.openNav} />;
}

function QueryFailure({ title, onRetry }: { title: string; onRetry: () => void }) {
  return <div role="alert" className="rounded-xl border border-danger-line bg-surface px-6 py-10 text-center">
    <h2 className="font-heading text-title font-semibold text-ink">{title}</h2>
    <p className="mt-2 text-control text-ink-muted">Nothing was changed. Try loading this page again.</p>
    <Button type="button" variant="outline" className="mt-5" onClick={onRetry}><RotateCcw /> Try again</Button>
  </div>;
}

function PageSkeleton({ rows = 6 }: { rows?: number }) {
  return <div role="status" aria-label="Loading finance records" className="overflow-hidden rounded-xl border border-line bg-surface">
    {Array.from({ length: rows }, (_, index) => <div key={index} className="flex h-20 items-center gap-4 border-b border-line px-5 last:border-0">
      <div className="size-9 animate-pulse rounded-lg bg-line" />
      <div className="min-w-0 flex-1"><div className="h-3 w-36 animate-pulse rounded-full bg-line" /><div className="mt-2 h-2.5 w-52 max-w-[70%] animate-pulse rounded-full bg-line" /></div>
      <div className="h-3 w-20 animate-pulse rounded-full bg-line" />
    </div>)}
  </div>;
}

function transactionTone(transaction: TransactionListItemOut) {
  if (["income", "refund", "reimbursement", "cash_deposit"].includes(transaction.transactionType)) return { className: "text-money-in", prefix: "+", icon: ArrowDownLeft };
  if (["expense", "investment", "loan_payment", "cash_withdrawal"].includes(transaction.transactionType)) return { className: "text-money-out", prefix: "−", icon: ArrowUpRight };
  return { className: "text-ink", prefix: "", icon: ReceiptText };
}

function revisionFieldLabel(field: string) {
  return ({
    amount_minor: "Amount",
    merchant_name: "Merchant",
    transaction_at: "Date and time",
    transaction_type: "Type",
    category: "Category",
    subcategory: "Subcategory",
    location_label: "Location",
    spend_nature: "Spend nature",
    tags: "Tags",
    deleted_at: "Record status",
  } as Record<string, string>)[field] ?? titleCase(field);
}

function revisionValue(field: string, value: unknown, currency: string) {
  if (value == null || value === "") return "Not set";
  if (field === "amount_minor" && typeof value === "number") return formatMoney(value, currency);
  if (field === "transaction_at" && typeof value === "string") return formatInstant(value);
  if (field === "deleted_at") return typeof value === "string" ? `Removed ${formatInstant(value)}` : "Active";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "None";
  return titleCase(String(value));
}

function TransactionHistory({ revisions, loading, currency }: { revisions: TransactionRevisionOut[]; loading: boolean; currency: string }) {
  const visibleFields = new Set(["amount_minor", "merchant_name", "transaction_at", "transaction_type", "category", "subcategory", "location_label", "spend_nature", "tags", "deleted_at"]);
  return <details className="mt-6 rounded-lg border border-line bg-ground/60">
    <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-note font-semibold text-ink-body"><History size={16} />Amendment history<span className="ml-auto font-normal text-ink-muted">{loading ? "Loading…" : `${revisions.length} version${revisions.length === 1 ? "" : "s"}`}</span></summary>
    <div className="border-t border-line px-4 py-3">
      {loading ? <p className="text-note text-ink-muted">Loading transaction versions…</p> : revisions.length ? <ol className="space-y-4">{revisions.map((revision) => {
        const changes = Object.entries(revision.changes).filter(([field]) => visibleFields.has(field));
        return <li key={revision.revisionNumber} className="text-note">
          <p className="font-medium text-ink">Version {revision.revisionNumber}<span className="ml-2 font-normal text-ink-muted">{formatInstant(revision.createdAt)} · {revision.source === "conversation_edit" ? "Conversation edit" : revision.source === "ledger_edit" ? "Ledger edit" : revision.source === "remove" ? "Removed" : revision.source === "restore" ? "Restored" : "Created"}</span></p>
          {changes.length ? <ul className="mt-1.5 space-y-1 text-ink-muted">{changes.map(([field, change]) => <li key={field}><span className="font-medium text-ink-body">{revisionFieldLabel(field)}:</span> {revisionValue(field, change.before, currency)} → {revisionValue(field, change.after, currency)}</li>)}</ul> : <p className="mt-1 text-ink-muted">Initial saved version.</p>}
        </li>;
      })}</ol> : <p className="text-note text-ink-muted">No amendment history is available for this older transaction yet.</p>}
    </div>
  </details>;
}

export function TransactionEditor({ transaction, categories, revisions = [], historyLoading = false, saving, problem, locationAllowed = false, onClose, onSave, onCreateCategory, onCreateSubcategory }: {
  transaction: TransactionListItemOut | null;
  categories: CategoryDirectoryOut[];
  revisions?: TransactionRevisionOut[];
  historyLoading?: boolean;
  saving: boolean;
  problem: string | null;
  locationAllowed?: boolean;
  onClose: () => void;
  onSave: (payload: TransactionUpdateIn) => void;
  onCreateCategory?: (name: string) => Promise<CategoryDirectoryOut>;
  onCreateSubcategory?: (categoryId: string, name: string) => Promise<CategoryDirectorySubcategoryOut>;
}) {
  const creating = transaction === null;
  const [confirmingDiscard, setConfirmingDiscard] = useState(false);
  const [dirty, setDirty] = useState(false);

  // Stable identity: the overlay hook re-arms (and re-focuses the first
  // field) whenever its close callback changes, so the changing closure
  // lives behind a ref.
  const closeBehavior = useRef(() => {});
  useEffect(() => {
    closeBehavior.current = () => {
      if (saving) return;
      if (confirmingDiscard) { setConfirmingDiscard(false); return; }
      if (dirty) { setConfirmingDiscard(true); return; }
      onClose();
    };
  });
  const requestClose = useCallback(() => closeBehavior.current(), []);

  const panelRef = useWorkspaceOverlay(true, requestClose);
  return <>
    <button type="button" tabIndex={-1} aria-hidden onClick={saving ? undefined : requestClose} className="scrim-fade fixed inset-0 z-40 bg-scrim/25 backdrop-blur-[2px]" />
    <section ref={panelRef} role="dialog" aria-modal="true" aria-labelledby="transaction-editor-title" className="drawer-right fixed inset-y-0 right-0 z-50 flex w-full max-w-lg flex-col border-l border-line bg-surface shadow-[var(--shadow-overlay)]">
      <div className="flex shrink-0 items-center border-b border-line px-4 pt-[max(1.25rem,env(safe-area-inset-top))] pb-4 sm:px-6">
        <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-secondary-tint text-secondary">{creating ? <Plus size={19} /> : <PencilLine size={19} />}</span>
        <div className="ml-3 min-w-0"><h2 id="transaction-editor-title" className="font-heading text-title font-semibold text-ink">{creating ? "Add transaction" : "Edit transaction"}</h2><p className="truncate text-note text-ink-muted">{creating ? "Record a confirmed entry" : transaction.merchant ?? titleCase(transaction.transactionType)}</p></div>
        <Button type="button" variant="ghost" size="icon-lg" aria-label="Close transaction editor" disabled={saving} onClick={requestClose} className="-mr-1 ml-auto rounded-xl text-ink-muted"><X /></Button>
      </div>
      {transaction ? <div className="shrink-0 border-b border-line bg-ground/40 px-6 py-2"><TransactionIdentifier transactionId={transaction.id} rowVersion={transaction.rowVersion} /></div> : null}

      <TransactionForm
        initialValues={{
          amountMinor: transaction?.amountMinor,
          merchant: transaction?.merchant,
          transactionAt: transaction?.transactionAt,
          transactionType: transaction?.transactionType,
          categoryId: transaction?.categoryId,
          subcategoryId: transaction?.subcategoryId,
          spendNature: transaction?.spendNature,
          location: transaction?.location,
        }}
        categories={categories}
        transactionTypes={editableTransactionTypes.filter((type) => type !== "transfer" || transaction?.transactionType === "transfer")}
        disabled={saving}
        locationAllowed={locationAllowed}
        captureDeviceLocation={creating}
        problem={problem}
        onDirtyChange={setDirty}
        onCreateCategory={onCreateCategory}
        onCreateSubcategory={onCreateSubcategory}
        onSubmit={(values) => {
          if (!values.transactionAt) return;
          onSave({
            expectedVersion: transaction?.rowVersion ?? null,
            amountMinor: values.amountMinor,
            merchant: values.merchant,
            transactionAt: values.transactionAt,
            transactionType: values.transactionType,
            categoryId: values.categoryId,
            subcategoryId: values.subcategoryId,
            spendNature: values.spendNature,
            location: values.location,
            latitude: values.latitude,
            longitude: values.longitude,
            locationAccuracy: values.locationAccuracy,
          });
        }}
        className="panel-scroll min-h-0 flex-1 overflow-y-auto px-4 pt-6 pb-[max(1.75rem,env(safe-area-inset-bottom))] sm:px-6"
        afterFields={transaction ? <TransactionHistory revisions={revisions} loading={historyLoading} currency={transaction.currency} /> : null}
        banner={confirmingDiscard ? <div role="alertdialog" aria-label="Discard unsaved changes" className="mb-4 rounded-lg border border-attention/40 bg-attention-tint px-4 py-3 text-note text-ink-body">
          You have unsaved changes. Throw them away?
          <div className="mt-2 flex gap-2">
            <Button type="button" variant="destructive" size="sm" onClick={onClose}>Discard</Button>
            <Button type="button" variant="ghost" size="sm" onClick={() => setConfirmingDiscard(false)}>Keep editing</Button>
          </div>
        </div> : null}
        renderActions={() => <div className="mt-6 flex gap-2 border-t border-line pt-5">
          <Button type="submit" size="lg" disabled={saving}>{saving ? <Loader2 className="animate-spin" /> : null}{saving ? (creating ? "Adding…" : "Saving…") : (creating ? "Add transaction" : "Save changes")}</Button>
          <Button type="button" size="lg" variant="ghost" disabled={saving} onClick={requestClose}>Cancel</Button>
        </div>}
      />
    </section>
  </>;
}

const TRANSACTION_PAGE_SIZE = 50;

export function TransactionRow({ transaction, style, onEdit, onRestore, onRemove }: {
  transaction: TransactionListItemOut;
  style?: CSSProperties;
  onEdit: (transaction: TransactionListItemOut) => void;
  onRestore?: (transaction: TransactionListItemOut) => void;
  onRemove?: (transaction: TransactionListItemOut) => void;
}) {
  const removed = Boolean(transaction.deletedAt);
  const tone = transactionTone(transaction);
  const Icon = removed ? Trash2 : tone.icon;
  const classification = formatTransactionClassification(transaction.transactionType, transaction.category, transaction.subcategory);
  return <article className="grid min-h-[68px] grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 border-b border-line px-4 sm:gap-4 sm:px-5" style={style}>
    <span className={cn("grid size-9 place-items-center rounded-lg bg-ground", removed ? "text-ink-muted" : tone.className)}><Icon size={17} /></span>
    <div className="min-w-0"><p className={cn("truncate text-control font-semibold", removed ? "text-ink-muted line-through" : "text-ink")} title={transaction.merchant ?? undefined}>{transaction.merchant ?? titleCase(transaction.transactionType)}</p><p className="mt-0.5 truncate text-note text-ink-muted">{[classification, timeFormatter.format(new Date(transaction.transactionAt)), removed ? `Removed ${formatInstant(transaction.deletedAt)}` : null].filter(Boolean).join(" · ")}</p></div>
    <div className="flex items-center gap-2 sm:gap-4">
      {removed ? <span className="rounded-full bg-danger-tint px-2.5 py-1 text-meta font-semibold text-danger-ink">Removed</span> : null}
      <p className={cn("font-heading text-control font-semibold tabular-nums", removed ? "text-ink-muted line-through" : tone.className)}>{tone.prefix}{formatMoney(transaction.amountMinor, transaction.currency)}</p>
      {removed
        ? (onRestore ? <Button type="button" variant="ghost" size="icon" aria-label={`Restore ${transaction.merchant ?? titleCase(transaction.transactionType)} transaction`} title="Restore — this record rejoins your totals" onClick={() => onRestore(transaction)}><RotateCcw /></Button> : null)
        : <>
          <Button type="button" variant="ghost" size="icon" aria-label={`Edit ${transaction.merchant ?? titleCase(transaction.transactionType)} transaction`} onClick={() => onEdit(transaction)}><PencilLine /></Button>
          {onRemove ? <Button type="button" variant="ghost" size="icon" aria-label={`Remove ${transaction.merchant ?? titleCase(transaction.transactionType)} transaction`} title="Remove — kept in the log, out of your totals" onClick={() => onRemove(transaction)} className="text-ink-muted hover:text-danger"><Trash2 /></Button> : null}
        </>}
    </div>
  </article>;
}

type TransactionListContext = {
  fetchingNextPage: boolean;
  hasNextPage: boolean;
  stickyTop: number;
};

function TransactionGroup({ children, context, style, ...props }: GroupProps & ContextProp<TransactionListContext>) {
  return <div {...props} style={{ ...style, top: context.stickyTop, zIndex: 10 }}>{children}</div>;
}

function TransactionFooter({ context }: ContextProp<TransactionListContext>) {
  if (!context.hasNextPage) return null;
  return <div className="flex h-16 items-center justify-center gap-2 text-note text-ink-muted">
    <Loader2 className={context.fetchingNextPage ? "animate-spin" : undefined} />
    {context.fetchingNextPage ? "Loading more transactions…" : "Scroll for more transactions"}
  </div>;
}

const transactionListComponents: Components<void, TransactionListContext> = {
  Footer: TransactionFooter,
  Group: TransactionGroup,
};

function VirtualTransactionList({ items, scrollElement, headerVisible, hasNextPage, fetchingNextPage, onLoadMore, onEdit, onRestore, onRemove }: {
  items: TransactionListItemOut[];
  scrollElement: HTMLElement | null;
  headerVisible: boolean;
  hasNextPage: boolean;
  fetchingNextPage: boolean;
  onLoadMore: () => void;
  onEdit: (transaction: TransactionListItemOut) => void;
  onRestore: (transaction: TransactionListItemOut) => void;
  onRemove: (transaction: TransactionListItemOut) => void;
}) {
  const groups = useMemo(() => groupTransactionsByDay(items), [items]);
  const groupCounts = useMemo(() => groups.map((group) => group.transactions.length), [groups]);
  const itemKeys = useMemo(() => groups.flatMap((group) => [`transaction-day-${group.id}`, ...group.transactions.map((transaction) => transaction.id)]), [groups]);
  const context = useMemo<TransactionListContext>(() => ({
    fetchingNextPage,
    hasNextPage,
    stickyTop: headerVisible ? SITE_HEADER_HEIGHT : 0,
  }), [fetchingNextPage, hasNextPage, headerVisible]);

  if (!scrollElement) return <div className="h-20 rounded-t-xl border border-line bg-surface" />;

  return <section aria-label="Transaction history" className="relative rounded-t-xl border border-line bg-surface">
    <GroupedVirtuoso<void, TransactionListContext>
      customScrollParent={scrollElement}
      groupCounts={groupCounts}
      context={context}
      components={transactionListComponents}
      computeItemKey={(index) => itemKeys[index] ?? index}
      fixedItemHeight={68}
      fixedGroupHeight={35}
      initialItemCount={Math.min(items.length, 18)}
      increaseViewportBy={{ top: 544, bottom: 816 }}
      endReached={() => {
        if (hasNextPage && !fetchingNextPage) onLoadMore();
      }}
      groupContent={(groupIndex) => <h3 className="ledger-meta flex h-[35px] items-center border-b border-line bg-ground px-4 shadow-[0_1px_0_var(--line)] sm:px-5">{groups[groupIndex] ? dayFormatter.format(new Date(groups[groupIndex].transactions[0].transactionAt)) : null}</h3>}
      itemContent={(index) => {
        const transaction = items[index];
        return transaction ? <TransactionRow transaction={transaction} onEdit={onEdit} onRestore={onRestore} onRemove={onRemove} /> : null;
      }}
    />
  </section>;
}

export function TransactionsPage() {
  const queryClient = useQueryClient();
  const categories = useQuery({ queryKey: ["category-directory"], queryFn: loadCategories });
  // The URL owns the filters: ?q= and ?type= make a filtered view something
  // you can bookmark, share, and undo with Back; a refresh lands on the same
  // view instead of silently resetting.
  const [params, setParams] = useSearchParams();
  const urlQuery = params.get("q") ?? "";
  const typeParam = params.get("type") ?? "all";
  const kind = (editableTransactionTypes as readonly string[]).includes(typeParam) ? typeParam : "all";
  const hideRemoved = params.get("removed") === "hidden";
  const [search, setSearch] = useState(urlQuery);
  const [settledSearch, setSettledSearch] = useState(urlQuery);
  // The add drawer is URL-owned. App shortcuts, pasted links, and the page's
  // own Add transaction control all use the same ?new=1 entry point.
  const adding = params.get("new") === "1";
  const [editingTransaction, setEditingTransaction] = useState<TransactionListItemOut | null>(null);
  const editing: TransactionListItemOut | "new" | null = adding ? "new" : editingTransaction;
  const editingId = editing && editing !== "new" ? editing.id : null;
  const revisionHistory = useQuery({
    queryKey: ["transaction-revisions", editingId],
    queryFn: () => loadTransactionRevisions(editingId as string),
    enabled: editingId !== null,
  });
  const [notice, setNotice] = useState<string | null>(null);
  const { headerVisible, updateHeaderForScroll } = useAutoHideSiteHeader();
  const [mainElement, setMainElement] = useState<HTMLElement | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const filterType = kind === "all" ? null : kind as TransactionListItemOut["transactionType"];

  usePlainKey("/", useCallback(() => searchRef.current?.focus(), []));

  const closeEditor = useCallback(() => {
    setEditingTransaction(null);
    setParams((previous) => {
      const merged = new URLSearchParams(previous);
      merged.delete("new");
      return merged;
    }, { replace: true });
  }, [setParams]);

  const openNewEditor = useCallback(() => {
    setNotice(null);
    setEditingTransaction(null);
    const merged = new URLSearchParams(params);
    merged.set("new", "1");
    setParams(merged);
  }, [params, setParams]);

  function setKind(next: string) {
    setParams((previous) => {
      const merged = new URLSearchParams(previous);
      if (next === "all") merged.delete("type"); else merged.set("type", next);
      return merged;
    });
  }

  function setHideRemoved(next: boolean) {
    setParams((previous) => {
      const merged = new URLSearchParams(previous);
      if (next) merged.set("removed", "hidden"); else merged.delete("removed");
      return merged;
    });
  }

  useEffect(() => {
    const query = search.trim();
    // An unchanged input has nothing to synchronize. Skipping the write also
    // prevents an older debounce closure from replaying a stale query-string
    // snapshot over a newer owner such as ?new=1.
    if (query === urlQuery) return;
    const timer = window.setTimeout(() => {
      setSettledSearch(search);
      // Typing rewrites the entry in place; only the type filter earns a
      // history entry, so Back steps through filters rather than keystrokes.
      setParams((previous) => {
        const merged = new URLSearchParams(previous);
        if (query) merged.set("q", query); else merged.delete("q");
        return merged;
      }, { replace: true });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [search, setParams, urlQuery]);

  // Back/forward (or a pasted link) changes ?q= underneath the input.
  if (urlQuery !== settledSearch.trim()) {
    setSearch(urlQuery);
    setSettledSearch(urlQuery);
  }

  const transactions = useInfiniteQuery({
    queryKey: ["transactions", "pages", settledSearch.trim(), filterType, hideRemoved],
    queryFn: ({ pageParam }) => loadTransactions({ limit: TRANSACTION_PAGE_SIZE, offset: pageParam, search: settledSearch, transactionType: filterType, includeRemoved: !hideRemoved }),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => lastPage.length === TRANSACTION_PAGE_SIZE ? pages.reduce((total, page) => total + page.length, 0) : undefined,
  });
  const items = useMemo(() => transactions.data?.pages.flat() ?? [], [transactions.data]);
  const { fetchNextPage } = transactions;
  const loadMore = useCallback(() => { void fetchNextPage(); }, [fetchNextPage]);

  function trackScroll(event: UIEvent<HTMLElement>) {
    updateHeaderForScroll(event.currentTarget.scrollTop);
  }

  // Shares the settings page's cache entry, so opening the drawer costs no
  // extra request. Read here rather than inside the editor: the drawer is a
  // presentational component that takes what it needs, which is also what
  // keeps it testable without a query client.
  const locationAllowed = useQuery({ queryKey: ["privacy"], queryFn: getPrivacyStatus, staleTime: 5 * 60_000 }).data?.locationEnabled ?? false;

  const save = useMutation({
    mutationFn: ({ id, payload }: { id: string | null; payload: TransactionUpdateIn }) => id ? updateTransaction(id, payload) : createTransactionRecord(payload),
    onSuccess: (updated, variables) => {
      if (variables.id) {
        // Patch the edited row in place across every loaded page — a user ten
        // pages deep should not watch five hundred rows refetch for one fix.
        // Only a changed timestamp can reorder the ledger, so only that case
        // asks the server for a fresh ordering, quietly, behind the patch.
        let moved = false;
        queryClient.setQueriesData<InfiniteData<TransactionListItemOut[]>>({ queryKey: ["transactions", "pages"] }, (data) => data && {
          ...data,
          pages: data.pages.map((page) => page.map((item) => {
            if (item.id !== updated.id) return item;
            moved ||= item.transactionAt !== updated.transactionAt;
            return updated;
          })),
        });
        if (moved) void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      } else {
        void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      }
      void queryClient.invalidateQueries({ queryKey: ["overview"] });
      if (variables.id) void queryClient.invalidateQueries({ queryKey: ["transaction-revisions", variables.id] });
      closeEditor();
      setNotice(`${updated.merchant ?? titleCase(updated.transactionType)} was ${variables.id ? "updated" : "added"}.`);
    },
  });

  // A success line from three edits ago sitting on the page reads as the
  // result of the current one; it leaves on its own once it has been seen.
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  /** Both directions of the tombstone commit at once and confirm with the
   *  same slip: "Removed · Swiggy — Undo" and "Restored · Swiggy — Undo".
   *  Undo is a real API call the other way, not a cancelled timer. A slip
   *  born from pressing Undo confirms without offering another Undo, so
   *  changing your mind once doesn't spawn an infinite ping-pong of slips. */
  type StrikeVariables = { transaction: TransactionListItemOut; viaUndo?: boolean };

  function patchLedgerRow(next: TransactionListItemOut) {
    queryClient.setQueriesData<InfiniteData<TransactionListItemOut[]>>({ queryKey: ["transactions", "pages"] }, (data) => data && {
      ...data,
      pages: data.pages.map((page) => page.map((item) => item.id === next.id ? next : item)),
    });
    void queryClient.invalidateQueries({ queryKey: ["overview"] });
  }

  function confirmStrike(kind: "Removed" | "Restored", item: TransactionListItemOut, viaUndo: boolean, undo: () => void) {
    const id = `${kind.toLowerCase()}-${item.id}`;
    toast.add({
      id,
      title: kind,
      description: item.merchant ?? titleCase(item.transactionType),
      timeout: UNDO_WINDOW_MS,
      ...(viaUndo ? {} : {
        actionProps: {
          children: "Undo",
          onClick: () => {
            toast.close(id);
            undo();
          },
        },
      }),
    });
  }

  const restore = useMutation({
    mutationFn: ({ transaction }: StrikeVariables) => restoreTransaction(transaction.id),
    onSuccess: (restored, { viaUndo = false }) => {
      // The row un-strikes in place; the totals refresh quietly behind it.
      patchLedgerRow(restored);
      confirmStrike("Restored", restored, viaUndo, () => remove.mutate({ transaction: restored, viaUndo: true }));
    },
    onError: (cause: Error) => setNotice(cause.message),
  });

  const remove = useMutation({
    mutationFn: ({ transaction }: StrikeVariables) => removeTransaction(transaction.id),
    onSuccess: (removed, { viaUndo = false }) => {
      patchLedgerRow(removed);
      confirmStrike("Removed", removed, viaUndo, () => restore.mutate({ transaction: removed, viaUndo: true }));
    },
    onError: (cause: Error) => setNotice(cause.message),
  });

  const { reset: resetSave } = save;
  const categoriesReady = Boolean(categories.data);
  usePlainKey("n", useCallback(() => {
    if (!categoriesReady) return;
    resetSave();
    if (!editing) openNewEditor();
  }, [categoriesReady, editing, openNewEditor, resetSave]));

  // Taxonomy created from inside the editor lands in the shared directory
  // cache, so the new entry is selectable everywhere without a refetch.
  async function addCategory(name: string) {
    const created = await createCategory(name);
    queryClient.setQueryData<CategoryDirectoryOut[]>(["category-directory"], (current) => {
      if (!current) return current;
      return current.some((item) => item.id === created.id) ? current.map((item) => item.id === created.id ? created : item) : [...current, created];
    });
    return created;
  }

  async function addSubcategory(categoryId: string, name: string) {
    const created = await createSubcategory(categoryId, name);
    queryClient.setQueryData<CategoryDirectoryOut[]>(["category-directory"], (current) => current?.map((item) => {
      if (item.id !== categoryId || item.subcategories.some((subcategory) => subcategory.id === created.id)) return item;
      return { ...item, subcategories: [...item.subcategories, created] };
    }));
    return created;
  }

  return <main ref={setMainElement} id="main-content" onScroll={trackScroll} className="min-h-0 min-w-0 overflow-y-auto bg-ground">
    <MoneyPageHeader title="Transactions" subtitle="Recent activity and corrections" hidden={!headerVisible} />
    <div className="mx-auto w-full max-w-[70rem] px-4 py-7 sm:px-6 sm:py-10 lg:px-8">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div><p className="ledger-meta">Ledger</p><h2 className="mt-2 font-heading text-[clamp(1.7rem,4vw,2.25rem)] leading-tight font-semibold tracking-[-0.04em] text-ink">Recent transactions</h2><p className="mt-2 text-body text-ink-muted">Review the latest records and correct anything that needs attention.</p></div>
        <div className="flex shrink-0 items-center gap-3">
          {transactions.data ? <p aria-live="polite" className="text-note text-ink-muted">{items.length} {transactions.hasNextPage ? "loaded" : items.length === 1 ? "transaction" : "transactions"}</p> : null}
          <Button type="button" disabled={!categories.data} title={categories.data ? undefined : "Loading your categories first…"} onClick={() => {
            save.reset();
            openNewEditor();
          }}><Plus /> Add transaction</Button>
        </div>
      </div>

      {notice ? <p role="status" className="mt-5 flex items-center gap-2 rounded-lg border border-secondary-line bg-secondary-tint px-4 py-3 text-note text-secondary-hover"><CheckCircle2 />{notice}</p> : null}
      <div className="mt-6 flex flex-col gap-3 rounded-xl border border-line bg-surface p-3 sm:flex-row">
        <label className="relative min-w-0 flex-1"><Search aria-hidden className="absolute top-1/2 left-3 -translate-y-1/2 text-ink-muted" /><span className="sr-only">Search transactions</span><input ref={searchRef} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search merchant or category ( / )" className="manual-field h-10 w-full rounded-lg border border-line-strong bg-ground pr-10 pl-9 text-control text-ink outline-none" />{transactions.isFetching && !transactions.isFetchingNextPage ? <Loader2 aria-label="Searching" className="absolute top-1/2 right-3 -translate-y-1/2 animate-spin text-ink-muted" /> : null}</label>
        <div className="sm:w-52"><Combobox aria-label="Filter by transaction type" value={kind} onValueChange={setKind} options={[{ value: "all", label: "All types" }, ...editableTransactionTypes.map((type) => ({ value: type, label: titleCase(type) }))]} searchable={false} triggerClassName="h-10 bg-ground font-medium text-ink-body" /></div>
        <Button type="button" variant="outline" aria-pressed={hideRemoved} onClick={() => setHideRemoved(!hideRemoved)} className={cn("h-10 shrink-0", hideRemoved && "border-secondary-line bg-secondary-tint text-secondary-hover")}>{hideRemoved ? <EyeOff /> : <Eye />}{hideRemoved ? "Removed hidden" : "Hide removed"}</Button>
      </div>

      <div className="mt-5">
        {transactions.isPending || categories.isPending ? <PageSkeleton /> : (transactions.isError && !transactions.data) || categories.isError ? <QueryFailure title="We couldn’t load your transactions" onRetry={() => { void transactions.refetch(); void categories.refetch(); }} /> : items.length ? <VirtualTransactionList items={items} scrollElement={mainElement} headerVisible={headerVisible} hasNextPage={transactions.hasNextPage} fetchingNextPage={transactions.isFetchingNextPage} onLoadMore={loadMore} onEdit={(transaction) => { setNotice(null); save.reset(); closeEditor(); setEditingTransaction(transaction); }} onRestore={(transaction) => { setNotice(null); restore.mutate({ transaction }); }} onRemove={(transaction) => { setNotice(null); remove.mutate({ transaction }); }} /> : <div className="rounded-xl border border-line bg-surface px-6 py-12 text-center"><ReceiptText className="mx-auto text-secondary" /><h2 className="mt-4 font-heading text-title font-semibold text-ink">{search.trim() || kind !== "all" ? "No matching transactions" : "No transactions yet"}</h2><p className="mt-2 text-control text-ink-muted">{search.trim() || kind !== "all" ? "Try a different search or transaction type." : "Add your first transaction to start the ledger."}</p></div>}
      </div>
    </div>
    {editing && categories.data ? <TransactionEditor key={editing === "new" ? "new" : editing.id} transaction={editing === "new" ? null : editing} categories={categories.data} revisions={revisionHistory.data} historyLoading={revisionHistory.isPending && editing !== "new"} saving={save.isPending} problem={save.error?.message ?? null} locationAllowed={locationAllowed} onClose={() => { if (!save.isPending) closeEditor(); }} onSave={(payload) => save.mutate({ id: editing === "new" ? null : editing.id, payload })} onCreateCategory={addCategory} onCreateSubcategory={addSubcategory} /> : null}
  </main>;
}

export function CategoriesPage() {
  const queryClient = useQueryClient();
  const categories = useQuery({ queryKey: ["category-directory"], queryFn: loadCategories });
  const overview = useQuery({ queryKey: ["overview", "current"], queryFn: () => loadOverview() });
  const [search, setSearch] = useState("");
  const { headerVisible, updateHeaderForScroll } = useAutoHideSiteHeader();
  const searchRef = useRef<HTMLInputElement>(null);
  usePlainKey("/", useCallback(() => searchRef.current?.focus(), []));
  const visibleCategories = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return (categories.data ?? []).filter((category) => !needle || category.label.toLocaleLowerCase().includes(needle) || category.subcategories.some((subcategory) => subcategory.label.toLocaleLowerCase().includes(needle)) || category.hints.some((hint) => hint.merchant.toLocaleLowerCase().includes(needle)));
  }, [categories.data, search]);
  const categoryUsage = useMemo(() => {
    const metrics = new Map((overview.data?.categories ?? []).map((category) => [category.id, category]));
    return new Map<string, CategoryUsage>((categories.data ?? []).map((category) => {
        const metric = metrics.get(category.slug);
        const metricSubcategories = new Map((metric?.subcategories ?? []).map((subcategory) => [subcategory.id, subcategory]));
        return [category.id, {
          amountMinor: metric?.amountMinor ?? 0,
          count: metric?.count ?? 0,
          sharePercent: metric?.sharePercent ?? 0,
          subcategories: new Map(category.subcategories.map((subcategory) => {
            const subMetric = metricSubcategories.get(subcategory.slug);
            return [subcategory.id, { amountMinor: subMetric?.amountMinor ?? 0, count: subMetric?.count ?? 0 }];
          })),
        }];
      }));
  }, [categories.data, overview.data]);
  const subcategoryCount = categories.data?.reduce((sum, category) => sum + category.subcategories.length, 0) ?? 0;

  /** The edit resolves when the server confirms it; the refetches that keep
   *  every surface honest run behind it. Blocking a one-word rename on the
   *  app's largest query (the transactions list) is what made Save spin. */
  async function refreshDirectory<T>(work: Promise<T>): Promise<T> {
    const result = await work;
    // The directory is the surface being edited, so it alone is awaited —
    // the manager re-renders from truth before the spinner stops.
    await queryClient.invalidateQueries({ queryKey: ["category-directory"] });
    void queryClient.invalidateQueries({ queryKey: ["overview"] });
    void queryClient.invalidateQueries({ queryKey: ["transactions"] });
    return result;
  }

  return <main id="main-content" onScroll={(event) => updateHeaderForScroll(event.currentTarget.scrollTop)} className="min-h-0 min-w-0 overflow-y-auto bg-ground">
    <MoneyPageHeader title="Categories" subtitle="Your expense taxonomy" hidden={!headerVisible} />
    <div className="mx-auto w-full max-w-[70rem] px-4 py-7 sm:px-6 sm:py-10 lg:px-8">
      <div><p className="ledger-meta">Organization</p><h2 className="mt-2 font-heading text-[clamp(1.7rem,4vw,2.25rem)] leading-tight font-semibold tracking-[-0.04em] text-ink">Your category system</h2><p className="mt-2 max-w-xl text-body leading-6 text-ink-muted">See every expense category, what sits inside it, and how it is being used this month.</p></div>

      <div className="mt-6 grid gap-px overflow-hidden rounded-xl border border-line bg-line sm:grid-cols-3">
        <div className="bg-surface px-5 py-4"><p className="ledger-meta">Categories</p><p className="mt-2 font-heading text-title font-semibold text-ink tabular-nums">{categories.data?.length ?? "—"}</p></div>
        <div className="bg-surface px-5 py-4"><p className="ledger-meta">Subcategories</p><p className="mt-2 font-heading text-title font-semibold text-ink tabular-nums">{categories.data ? subcategoryCount : "—"}</p></div>
        <div className="bg-surface px-5 py-4"><p className="ledger-meta">{overview.data?.period.label ?? "Current month"}</p><p className="mt-2 font-heading text-title font-semibold text-money-out tabular-nums">{overview.data ? formatMoney(overview.data.summary.spentMinor, overview.data.summary.currency) : "—"}</p></div>
      </div>

      <label className="relative mt-5 block"><Search aria-hidden className="absolute top-1/2 left-3 -translate-y-1/2 text-ink-muted" /><span className="sr-only">Search categories</span><input ref={searchRef} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search categories or subcategories ( / )" className="manual-field h-10 w-full rounded-lg border border-line-strong bg-surface pr-3 pl-9 text-control text-ink outline-none" /></label>
      {/* Filtering happens silently for sighted users too — the list just
          shrinks — so the count is spoken rather than displayed twice. */}
      <p aria-live="polite" className="sr-only">{search.trim() ? `${visibleCategories.length} matching ${visibleCategories.length === 1 ? "category" : "categories"}` : ""}</p>

      <div className="mt-5">
        {categories.isPending || overview.isPending ? <PageSkeleton rows={7} /> : categories.isError || overview.isError ? <QueryFailure title="We couldn’t load your categories" onRetry={() => { void categories.refetch(); void overview.refetch(); }} /> : visibleCategories.length ? <CategoryManager
          categories={visibleCategories}
          usage={categoryUsage}
          currency={overview.data?.summary.currency ?? "INR"}
          onCreateCategory={async (name) => { const created = await refreshDirectory(createCategory(name)); setSearch(""); return created; }}
          onRenameCategory={(id, name) => refreshDirectory(renameCategory(id, name))}
          onDeleteCategory={(id) => refreshDirectory(deleteCategory(id))}
          onCreateSubcategory={(categoryId, name) => refreshDirectory(createSubcategory(categoryId, name))}
          onRenameSubcategory={(categoryId, id, name) => refreshDirectory(renameSubcategory(categoryId, id, name))}
          onDeleteSubcategory={(categoryId, id) => refreshDirectory(deleteSubcategory(categoryId, id))}
          onCreateHint={(categoryId, merchant, subcategoryId) => refreshDirectory(createTransactionHint(categoryId, merchant, subcategoryId))}
          onUpdateHint={(categoryId, id, merchant, subcategoryId) => refreshDirectory(updateTransactionHint(categoryId, id, merchant, subcategoryId))}
          onDeleteHint={(categoryId, id) => refreshDirectory(deleteTransactionHint(categoryId, id))}
        /> : <div className="rounded-xl border border-line bg-surface px-6 py-12 text-center"><Search className="mx-auto text-secondary" /><h2 className="mt-4 font-heading text-title font-semibold text-ink">No matching categories</h2><p className="mt-2 text-control text-ink-muted">Try another category, subcategory or merchant hint.</p></div>}
      </div>
    </div>
  </main>;
}
