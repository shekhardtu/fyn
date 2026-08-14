import { CalendarDays, Check, ChevronDown, CircleEllipsis, Info, Landmark, Loader2, LoaderCircle, PencilLine, Plus, ReceiptText, RotateCcw, Search, Target, Trash2, TrendingUp, TriangleAlert, Utensils, WalletCards, X } from "lucide-react";
import { FormEvent, memo, useEffect, useId, useMemo, useRef, useState, type ComponentType } from "react";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, Pie, PieChart, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis } from "recharts";
import type { TopLevelSpec } from "vega-lite";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Progress } from "@/components/ui/progress";
import { DataTableView } from "@/components/widget-library/data-table";
import { formatCount, formatDay, formatDimension, formatDuration, formatInstant, formatMoney, formatTransactionClassification, parseAmountToMinor, parseNumber, timestampInputToUtc, timestampInputValue } from "@/lib/format";
import { dataChartDataSchema, dataTableDataSchema, dataVisualizationDataSchema, editableTransactionTypes, widgetActionIds, widgetActions, widgetTypeIds, type DataChartData, type DataTableData, type DataVisualizationData, type Widget, type WidgetActionId } from "@/lib/protocol";
import { cn } from "@/lib/utils";
import { environment } from "@/config/environment";
import { useTablesWide, WIDE_TABLE_BREAKOUT } from "@/lib/wide-tables";

type Primitive = string | number | boolean | null | undefined;
type Data = Record<string, unknown>;

/** Chart series read left to right in the same order as the legend beside them. */
const palette = ["#4340e0", "#0891b2", "#b45309", "#7c3aed", "#155e75", "#64748b", "#a16207", "#db2777"];

/** Chart furniture. Recharts and Vega take colours as props rather than as
 *  CSS, so the tokens are mirrored here and nowhere else. */
const chartInk = { label: "#6c727c", title: "#3c4048", grid: "#e5e7ec", domain: "#d0d4dc" };

export { formatMoney };

function str(value: unknown, fallback = "") { return typeof value === "string" ? value : fallback; }
function num(value: unknown) { const parsed = typeof value === "number" ? value : Number(value ?? 0); return Number.isFinite(parsed) ? parsed : 0; }
function formatEnumLabel(value: string) {
  const label = formatDimension(value);
  return label.charAt(0).toUpperCase() + label.slice(1);
}
function plainLine(value: unknown) { return str(value).replace(/[*_`#>]/g, "").replace(/\s+/g, " ").trim(); }
function plainTranscript(value: unknown) {
  return str(value)
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^\s*[-*]\s+/gm, "")
    .replace(/`([^`]+)`/g, "$1")
    .trim();
}
function options(data: Data) { return Array.isArray(data.options) ? data.options as Array<Record<string, Primitive>> : []; }
function isWidgetActionId(value: unknown): value is WidgetActionId {
  return typeof value === "string" && (widgetActions as readonly string[]).includes(value);
}
function completionValues(widget: Widget): Data {
  const completion = widget.data.completion;
  if (!completion || typeof completion !== "object") return {};
  const values = (completion as Data).values;
  return values && typeof values === "object" ? values as Data : {};
}

/** The signature detail: every rupee figure in the product speaks with one
 *  voice — lining, tabular figures so columns of money align down the page. */
function Money({ value, currency = "INR", className }: { value: unknown; currency?: string; className?: string }) {
  return <span className={cn("money", className)}>{formatMoney(value, currency)}</span>;
}

function Card({ children, className }: { children: React.ReactNode; className?: string }) {
  return <section className={cn("widget-enter overflow-hidden rounded-lg border border-line bg-surface", className)}>{children}</section>;
}

function CardHeader({ eyebrow, title, body, tone = "neutral", trailing }: { eyebrow?: string; title: string; body?: string; tone?: "neutral" | "caution"; trailing?: React.ReactNode }) {
  return <div className="flex items-start gap-2.5 border-b border-line px-3.5 py-3">
    <div className="min-w-0 flex-1">
      {eyebrow ? <p className={cn("text-meta font-semibold tracking-[0.08em] uppercase", tone === "caution" ? "text-danger-ink" : "text-ink-muted")}>{eyebrow}</p> : null}
      <h3 className={cn("font-heading text-body font-semibold leading-5 text-ink", eyebrow && "mt-0.5")}>{title}</h3>
      {body ? <p className="mt-0.5 text-note leading-4 text-ink-muted">{body}</p> : null}
    </div>
    {trailing}
  </div>;
}

function EmptyNote({ children }: { children: React.ReactNode }) {
  return <p className="px-4 py-6 text-center text-note leading-5 text-ink-muted">{children}</p>;
}

function FieldLabel({ children, hint }: { children: React.ReactNode; hint?: string }) {
  return <span className="mb-2 block text-note font-medium text-ink-muted">{children}{hint ? <span className="ml-1 font-normal text-ink-muted/80">{hint}</span> : null}</span>;
}

const inputClass = "manual-field block h-[var(--h-field)] w-full rounded-lg border border-line-strong bg-surface px-3 text-body text-ink outline-none transition-colors duration-[110ms] ease-linear disabled:opacity-50";
const invalidClass = "manual-field-danger border-danger-line";

function FieldError({ children }: { children: React.ReactNode }) {
  return <span className="mt-1 flex items-center gap-1 text-meta font-medium text-danger-ink"><TriangleAlert size={14} />{children}</span>;
}

/** What this card believes you chose, before the server has agreed.
 *
 *  The round trip is seconds long, and for all of it the confirmed value is
 *  still empty — so a card that only trusts the server has nothing to show for
 *  your tap. This holds the choice locally and hands it back immediately.
 *
 *  Rollback is the interesting half: if the request ends without the server
 *  confirming anything, the tap did not take, and the tile must stop claiming
 *  it did. `pending` falling back to false with `confirmed` still empty is
 *  exactly that case, and it covers a failure the card is never told about
 *  directly — the error banner belongs to the transcript, not to this widget. */
function useOptimisticChoice(confirmed: string, pending?: boolean): [string, (id: string) => void] {
  const [chosen, setChosen] = useState<string | null>(null);
  const wasPending = useRef(false);
  useEffect(() => {
    if (wasPending.current && !pending && !confirmed) setChosen(null);
    wasPending.current = Boolean(pending);
  }, [pending, confirmed]);
  return [chosen ?? confirmed, setChosen];
}

/** Competing actions disable together, but only the submitted one owns the
 *  progress indicator. */
function usePendingAction(pending?: boolean): [string | null, (id: string) => void] {
  const [submitted, setSubmitted] = useState<string | null>(null);
  const wasPending = useRef(false);
  useEffect(() => {
    if (wasPending.current && !pending) setSubmitted(null);
    wasPending.current = Boolean(pending);
  }, [pending]);
  return [submitted, setSubmitted];
}

/** One option, and every state it can be in. */
function OptionTile({ label, detail, icon, selected, pending, dimmed, disabled, onSelect }: {
  label: string;
  detail?: string;
  icon?: React.ReactNode;
  selected: boolean;
  pending?: boolean;
  dimmed?: boolean;
  disabled?: boolean;
  onSelect: () => void;
}) {
  return <button
    type="button"
    aria-pressed={selected}
    disabled={disabled}
    onClick={onSelect}
    data-selected={selected || undefined}
    data-pending={pending || undefined}
    data-dimmed={dimmed || undefined}
    className="option-tile"
  >
    <span aria-hidden className="option-mark">
      {pending ? <Loader2 size={12} className="animate-spin" /> : selected ? <Check size={11} strokeWidth={3} /> : null}
    </span>
    {icon && !selected ? <span aria-hidden className="shrink-0 text-ink-muted">{icon}</span> : null}
    <span className="min-w-0 flex-1">
      <span className="block truncate">{label}</span>
      {detail ? <span className="mt-0.5 block text-meta font-normal leading-4 text-ink-muted">{detail}</span> : null}
    </span>
  </button>;
}

export type WidgetProps = {
  widget: Widget;
  disabled?: boolean;
  /** True while this widget's own action is in flight. */
  pending?: boolean;
  /** Framework escape for a persisted interrupt whose older widget contract
   *  did not yet declare its own cancellation action. */
  onCancel?: () => void;
  onAction: (widgetId: string, action: WidgetActionId, payload: Record<string, unknown>, options?: { markUsed?: boolean }) => void;
};

/** Action buttons render their own progress so the click has an obvious effect. */
function ActionButton({ action, pending, disabled, onClick, icon }: { action: Widget["actions"][number]; pending?: boolean; disabled?: boolean; onClick: () => void; icon?: React.ReactNode }) {
  const destructive = /remove|delete|separate/.test(action.action) || action.style === "danger";
  return <Button type="button" disabled={disabled || pending} variant={destructive ? "destructive" : action.style === "primary" ? "default" : action.style === "ghost" ? "ghost" : "outline"} onClick={onClick}>
    {pending ? <Loader2 size={14} className="animate-spin" /> : icon}{action.label}
  </Button>;
}

function orderedActions(actions: Widget["actions"]) {
  const priority = { ghost: 0, secondary: 1, primary: 2, danger: 3 } as const;
  return actions.map((action, index) => ({ action, index })).sort((left, right) => priority[left.action.style] - priority[right.action.style] || left.index - right.index).map(({ action }) => action);
}

function isEscapeAction(action: Widget["actions"][number]) {
  return action.id === "cancel" || action.action.startsWith("cancel_");
}

/** Keep older, already-persisted draft widgets escapable after the action
 *  contract gains a server-declared cancel transition. New widgets receive
 *  this action from the backend; this fallback only repairs active legacy
 *  cards, and still calls the same governed backend transition. */
function ensureDraftCancel(widget: Widget, actions: Widget["actions"], disabled?: boolean) {
  const draftId = str(widget.data.draftId);
  if (disabled || !draftId || actions.some((action) => action.action === widgetActionIds.cancel_transaction_draft)) return actions;
  return [...actions, {
    id: "cancel-draft",
    label: "Cancel transaction",
    action: widgetActionIds.cancel_transaction_draft,
    style: "ghost" as const,
    payload: { draftId },
  }];
}

function HitlActions({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("hitl-actions", className)}>{children}</div>;
}

function ActionRow({ widget, disabled, pending, onAction, onCancel, icons, actions = widget.actions }: WidgetProps & { icons?: Record<string, React.ReactNode>; actions?: Widget["actions"] }) {
  const fallbackCancel = onCancel && !actions.some(isEscapeAction);
  const [submitted, submit] = usePendingAction(pending);
  if (!actions.length && !fallbackCancel) return null;
  return <HitlActions className="border-t border-line">
    {fallbackCancel ? <Button type="button" variant="ghost" disabled={disabled || pending} onClick={() => { submit("protocol-cancel"); onCancel(); }}>Cancel</Button> : null}
    {orderedActions(actions).map((action) => <ActionButton key={action.id} action={action} pending={pending && submitted === action.id} disabled={disabled || pending} icon={icons?.[action.action]} onClick={() => { submit(action.id); onAction(widget.id, action.action, action.payload); }} />)}
  </HitlActions>;
}

function Clarification({ widget, onAction, onCancel, disabled, pending }: WidgetProps) {
  const listed = Array.isArray(widget.data.options) ? widget.data.options as Array<Record<string, unknown>> : [];
  const completedValues = completionValues(widget);
  const [selectedId, choose] = useOptimisticChoice(str(completedValues.optionId), pending);
  const [customText, setCustomText] = useState(str(completedValues.customText));
  const [customOpen, setCustomOpen] = useState(Boolean(completedValues.customText));
  const customInput = useRef<HTMLInputElement>(null);
  const byId = new Map(widget.actions.map((action) => [action.id, action]));
  const chooseOption = (optionId: string) => {
    const action = byId.get(optionId);
    if (!action) return;
    choose(optionId);
    onAction(widget.id, action.action, action.payload);
  };
  const customAction = byId.get("custom");
  const choiceIds = new Set(listed.map((option) => str(option.id)));
  const navigationActions = widget.actions.filter((action) => !choiceIds.has(action.id) && action.id !== "custom");
  useEffect(() => {
    if (customOpen && !disabled) customInput.current?.focus();
  }, [customOpen, disabled]);
  function submitCustom(event: FormEvent) {
    event.preventDefault();
    const value = customText.trim();
    if (!customAction || !value) return;
    choose("custom");
    onAction(widget.id, customAction.action, { ...customAction.payload, customText: value });
  }
  return <Card className="hitl-card">
    {str(widget.data.reason) ? <div className="flex items-start gap-2 border-b border-line px-3 py-2.5 text-note leading-4 text-ink-muted">
      <TriangleAlert size={15} className="mt-px shrink-0 text-attention" />
      <p>{str(widget.data.reason)}</p>
    </div> : null}
    <div className="hitl-options">
      {listed.map((option) => {
        const id = str(option.id);
        const selected = selectedId === id;
        return <OptionTile
          key={id}
          label={str(option.label)}
          detail={str(option.description) || undefined}
          selected={selected}
          pending={pending && selected}
          dimmed={pending && !selected}
          disabled={disabled || pending || !byId.has(id)}
          onSelect={() => chooseOption(id)}
        />;
      })}
    </div>
    {widget.data.allowCustom && customAction ? <div className="border-t border-line">
      <button type="button" aria-expanded={customOpen} disabled={disabled || pending} onClick={() => setCustomOpen((open) => !open)} className="hitl-disclosure">
        <PencilLine size={14} />{str(widget.data.customLabel, "Something else")}<ChevronDown size={14} className={cn("ml-auto transition-transform duration-[var(--m-state)]", customOpen && "rotate-180")} />
      </button>
      {customOpen ? <form onSubmit={submitCustom} className="hitl-reveal flex flex-col gap-2 border-t border-line-soft p-3 sm:flex-row">
        <input
          ref={customInput}
          value={customText}
          disabled={disabled || pending}
          maxLength={1000}
          onChange={(event) => setCustomText(event.target.value)}
          className={inputClass}
          placeholder="Type your answer"
          aria-label="Custom clarification"
        />
        <Button type="submit" disabled={disabled || pending || !customText.trim()}>{pending && selectedId === "custom" ? <Loader2 size={14} className="animate-spin" /> : null}Continue</Button>
      </form> : null}
    </div> : null}
    {navigationActions.length || onCancel ? <ActionRow widget={widget} actions={navigationActions} disabled={disabled} pending={pending} onAction={onAction} onCancel={onCancel} /> : null}
  </Card>;
}

function Selector({ widget, onAction, disabled, pending }: WidgetProps) {
  const icons: Record<string, React.ReactNode> = { food: <Utensils />, bills: <ReceiptText /> };
  const list = options(widget.data);
  const completedValues = completionValues(widget);
  const declaredAction = widget.actions[0];
  const startCreateAction = widget.actions.find((item) => item.action === widgetActionIds.start_add_subcategory);
  const basePayload = declaredAction?.payload ?? {};
  const suggestions = Array.isArray(widget.data.suggestions) ? widget.data.suggestions as Array<Record<string, unknown>> : [];
  const suggestedIds = new Set(suggestions.map((item) => str(item.id)));
  const remaining = list.filter((option) => !suggestedIds.has(str(option.id)));
  const accountSelector = widget.type === widgetTypeIds.account_selector;
  const field = widget.type === widgetTypeIds.category_selector ? "categoryId" : widget.type === widgetTypeIds.subcategory_selector ? "subcategoryId" : "optionId";
  const [selectedId, choose] = useOptimisticChoice(str(completedValues[field]), pending);
  const [accountName, setAccountName] = useState(str(completedValues.accountName));
  const [accountOpen, setAccountOpen] = useState(accountSelector && list.length === 0);
  const action = declaredAction?.action;
  const navigationActions = ensureDraftCancel(
    widget,
    widget.actions.filter((item) => item.id !== declaredAction?.id && item.id !== startCreateAction?.id),
    disabled && !pending,
  );
  // The card is locked while its action runs, but the tile you pressed is not
  // the thing being waited on — it is the answer. So `disabled` retires the
  // controls and `dimmed` recedes the ones you did not choose, and the chosen
  // tile carries the spinner itself.
  const pick = (id: string) => {
    if (!action) return;
    choose(id);
    onAction(widget.id, action, { ...basePayload, [field]: id });
  };
  function submitAccount(event: FormEvent) {
    event.preventDefault();
    const name = accountName.trim();
    if (!accountSelector || action !== widgetActionIds.select_account || !name) return;
    choose("custom-account");
    onAction(widget.id, action, { ...basePayload, accountName: name });
  }
  return <Card className="hitl-card">
    {widget.type === widgetTypeIds.subcategory_selector && str(widget.data.category) ? <p className="border-b border-line px-3 py-2 text-note text-ink-muted">Under <span className="font-medium text-ink-body">{str(widget.data.category)}</span></p> : null}
    {suggestions.length ? <div className="border-b border-line p-2.5">
      <p className="hitl-section-label">Suggested</p>
      <div className="grid gap-1.5 sm:grid-cols-3">{suggestions.map((suggestion) => {
        const id = str(suggestion.id);
        const selected = selectedId === id;
        const reasons = Array.isArray(suggestion.reasons) ? suggestion.reasons.map((reason) => str(reason)).filter(Boolean) : [];
        return <OptionTile key={id} label={str(suggestion.label)} detail={reasons.join(" · ") || undefined} selected={selected} pending={pending && selected} dimmed={pending && !selected} disabled={disabled || pending || !action} onSelect={() => pick(id)} />;
      })}</div>
    </div> : null}
    {remaining.length ? <div className="grid grid-cols-1 gap-1.5 p-2.5 sm:grid-cols-2 lg:grid-cols-3">
      {remaining.map((option) => {
        const id = str(option.id); const slug = str(option.slug, id);
        const selected = selectedId === id;
        return <OptionTile
          key={id}
          label={str(option.label)}
          // Subcategories already carry the radio-style choice mark. A second
          // generic circle read as another control rather than an icon.
          icon={widget.type === widgetTypeIds.category_selector ? icons[slug] ?? <CircleEllipsis size={14} /> : undefined}
          selected={selected}
          pending={pending && selected}
          dimmed={pending && !selected}
          disabled={disabled || pending || !action}
          onSelect={() => pick(id)}
        />;
      })}
    </div> : list.length || accountSelector ? null : <EmptyNote>Nothing to choose from yet.</EmptyNote>}
    {accountSelector ? <div className={cn(list.length && "border-t border-line")}>
      {list.length ? <button type="button" aria-expanded={accountOpen} disabled={disabled || pending} onClick={() => setAccountOpen((open) => !open)} className="hitl-disclosure"><PencilLine size={14} />Use another account<ChevronDown size={14} className={cn("ml-auto transition-transform duration-[var(--m-state)]", accountOpen && "rotate-180")} /></button> : null}
      {accountOpen ? <form onSubmit={submitAccount} className="hitl-reveal flex flex-col items-stretch gap-2 p-3 sm:flex-row">
        <input autoFocus={!disabled} value={accountName} disabled={disabled || pending} maxLength={120} onChange={(event) => setAccountName(event.target.value)} className={inputClass} placeholder="Account name" aria-label="Account name" />
        <Button type="submit" size="lg" disabled={disabled || pending || !accountName.trim()} className="h-[var(--h-field)] px-4">{pending && selectedId === "custom-account" ? <Loader2 size={14} className="animate-spin" /> : null}Continue</Button>
      </form> : null}
    </div> : null}
    {widget.type === widgetTypeIds.subcategory_selector && widget.data.allowCreate && startCreateAction ? <button type="button" disabled={disabled || pending} onClick={() => onAction(widget.id, startCreateAction.action, startCreateAction.payload)} className="hitl-disclosure"><Plus size={14} /> {startCreateAction.label}</button> : null}
    {navigationActions.length ? <ActionRow widget={widget} actions={navigationActions} disabled={disabled} pending={pending} onAction={onAction} /> : null}
  </Card>;
}

function CategorySelector({ widget, onAction, disabled, pending }: WidgetProps) {
  const [query, setQuery] = useState("");
  const completedValues = completionValues(widget);
  const [newCategory, setNewCategory] = useState(str(completedValues.name));
  const selectedCategoryId = str(completedValues.categoryId);
  const allOptions = options(widget.data);
  const suggestions = Array.isArray(widget.data.suggestions) ? widget.data.suggestions as Array<Record<string, unknown>> : [];
  const normalizedQuery = query.trim().toLowerCase();
  const suggestedIds = new Set(suggestions.map((item) => str(item.id)));
  const filtered = allOptions.filter((option) => str(option.label).toLowerCase().includes(normalizedQuery) && (Boolean(normalizedQuery) || !suggestedIds.has(str(option.id))));
  const declaredAction = widget.actions[0];
  const startCreateAction = widget.actions.find((action) => action.action === widgetActionIds.start_add_category);
  const basePayload = declaredAction?.payload ?? {};
  const navigationActions = ensureDraftCancel(
    widget,
    widget.actions.filter((action) => action.id !== declaredAction?.id && action.id !== startCreateAction?.id),
    disabled && !pending,
  );
  const [chosenId, choose] = useOptimisticChoice(selectedCategoryId, pending);
  const [submittedAction, markSubmitted] = usePendingAction(pending);
  const select = (categoryId: string) => {
    choose(categoryId);
    onAction(widget.id, widgetActionIds.select_category, { ...basePayload, categoryId });
  };

  if (widget.data.mode === "create") {
    function submit(event: FormEvent) {
      event.preventDefault();
      const name = newCategory.trim();
      if (name) {
        markSubmitted(declaredAction?.id ?? "create");
        onAction(widget.id, widgetActionIds.create_category, { ...basePayload, name });
      }
    }
    return <Card className="hitl-card"><form onSubmit={submit} className="space-y-3 p-3">
      <label className="block"><FieldLabel>Category name</FieldLabel><input autoFocus={!disabled} disabled={disabled || pending} aria-label="New category name" value={newCategory} onChange={(event) => setNewCategory(event.target.value)} placeholder="e.g. Pets" maxLength={80} className={inputClass} /></label>
      <HitlActions className="-mx-3 -mb-3 border-t border-line">
        {orderedActions(navigationActions).map((action) => <ActionButton key={action.id} action={action} pending={pending && submittedAction === action.id} disabled={disabled || pending} onClick={() => { markSubmitted(action.id); onAction(widget.id, action.action, action.payload); }} />)}
        <Button type="submit" disabled={disabled || pending || !newCategory.trim()}>{pending && submittedAction === (declaredAction?.id ?? "create") ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />} Add category</Button>
      </HitlActions>
    </form></Card>;
  }

  return <Card className="hitl-card">
    <div className="border-b border-line p-2.5">
      <label className="relative block"><Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-muted" /><input disabled={disabled || pending} aria-label="Search categories" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search categories" className={cn(inputClass, "pl-9")} /></label>
    </div>
    {!normalizedQuery && suggestions.length ? <div className="border-b border-line p-2.5">
      <p className="hitl-section-label">Suggested</p>
      <div className="grid gap-1.5 sm:grid-cols-3">{suggestions.map((suggestion) => { const selected = chosenId === str(suggestion.id); return <OptionTile key={str(suggestion.id)} label={str(suggestion.label)} detail={Array.isArray(suggestion.reasons) && suggestion.reasons.length ? suggestion.reasons.join(" · ") : undefined} selected={selected} pending={pending && selected} dimmed={pending && !selected} disabled={disabled || pending} onSelect={() => select(str(suggestion.id))} />; })}</div>
    </div> : null}
    <div className="p-2.5">
      <p className="hitl-section-label">{normalizedQuery ? "Search results" : "All categories"}</p>
      <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2 lg:grid-cols-3">{filtered.map((option) => { const id = str(option.id); const selected = chosenId === id; return <OptionTile key={id} label={str(option.label)} selected={selected} pending={pending && selected} dimmed={pending && !selected} disabled={disabled || pending} onSelect={() => select(id)} />; })}</div>
      {filtered.length === 0 && startCreateAction ? <div className="px-1 py-3 text-center"><p className="text-note text-ink-muted">No match for “{query.trim()}”.</p><Button type="button" variant="outline" disabled={disabled || pending} onClick={() => onAction(widget.id, startCreateAction.action, startCreateAction.payload)} className="mt-2.5"><Plus size={14} /> Create “{query.trim()}”</Button></div> : null}
    </div>
    {startCreateAction ? <button type="button" disabled={disabled || pending} onClick={() => onAction(widget.id, startCreateAction.action, startCreateAction.payload)} className="hitl-disclosure border-t border-line"><Plus size={14} /> {startCreateAction.label}</button> : null}
    {navigationActions.length ? <ActionRow widget={widget} actions={navigationActions} disabled={disabled} pending={pending} onAction={onAction} /> : null}
  </Card>;
}

function TaxonomyEditor({ widget, onAction, disabled, pending }: WidgetProps) {
  const [name, setName] = useState(str(widget.data.name));
  const [submittedAction, markSubmitted] = usePendingAction(pending);
  const operation = str(widget.data.operation);
  const isSubcategory = operation === widgetActionIds.create_subcategory;
  const lifecycle = str(widget.data.lifecycle, "pending");
  const resolved = lifecycle === "completed" || lifecycle === "cancelled";
  const submitAction = isSubcategory ? widgetActionIds.create_subcategory : widgetActionIds.create_category;
  const declaredAction = widget.actions[0];
  const basePayload = declaredAction?.payload ?? {};
  const navigationActions = ensureDraftCancel(
    widget,
    widget.actions.filter((action) => action.id !== declaredAction?.id),
    disabled && !pending,
  );
  function submit(event: FormEvent) {
    event.preventDefault();
    if (name.trim()) {
      markSubmitted(declaredAction?.id ?? "create");
      onAction(widget.id, submitAction, { ...basePayload, name: name.trim() });
    }
  }
  return <Card className="hitl-card"><form onSubmit={submit} className="space-y-3 p-3">
    <label className="block"><FieldLabel hint={isSubcategory && widget.data.parentCategory ? `under ${str(widget.data.parentCategory)}` : undefined}>{isSubcategory ? "Subcategory name" : "Category name"}</FieldLabel><input autoFocus={!disabled && !resolved} disabled={disabled || pending || resolved} aria-label={isSubcategory ? "New subcategory name" : "New category name"} value={name} onChange={(event) => setName(event.target.value)} placeholder={isSubcategory ? "e.g. Materials" : "e.g. Pets"} maxLength={80} className={inputClass} /></label>
    {!resolved ? <HitlActions className="-mx-3 -mb-3 border-t border-line">
      {orderedActions(navigationActions).map((action) => <ActionButton key={action.id} action={action} pending={pending && submittedAction === action.id} disabled={disabled || pending} onClick={() => { markSubmitted(action.id); onAction(widget.id, action.action, action.payload); }} />)}
      <Button type="submit" disabled={disabled || pending || !name.trim()}>{pending && submittedAction === (declaredAction?.id ?? "create") ? <Loader2 className="animate-spin" /> : <Plus />} {isSubcategory ? "Add subcategory" : "Add category"}</Button>
    </HitlActions> : null}
  </form></Card>;
}

function Confirmation({ widget, onAction, disabled, pending }: WidgetProps) {
  const data = widget.data;
  const inferred = Array.isArray(data.inferredFields) ? data.inferredFields as string[] : [];
  // A confirmation that destroys a record must not wear the same green as one
  // that saves it.
  const destructive = widget.actions.some((action) => /remove|delete/.test(action.action));
  const rows: Array<[string, React.ReactNode]> = [];
  if (data.sourceAccount || data.destinationAccount) rows.push(["Accounts", `${str(data.sourceAccount, "—")} → ${str(data.destinationAccount, "—")}`]);
  if (data.category) rows.push(["Category", `${String(data.category)}${data.subcategory ? ` → ${String(data.subcategory)}` : ""}`]);
  if (data.location) rows.push(["Location", str(data.location)]);

  return <Card className={cn("hitl-card", destructive && "border-danger-line")}>
    {/* The amount is the fact being confirmed, so it is set in ink at display
        size and the chip beside it carries the state. Filling this panel with
        colour — as the two gradients here used to — made every save look like
        an alert and every removal look like the same alert in another hue. */}
    <div className="border-b border-line px-3.5 py-3">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="money text-title font-semibold text-ink">{formatMoney(data.amountMinor, str(data.currency, "INR"))}</p>
          <p className="mt-0.5 truncate text-note text-ink-muted">{[data.merchant, data.subcategory, data.transactionType].filter(Boolean).map(String).join(" · ")}</p>
        </div>
        <span className={cn(
          "inline-flex items-center rounded-xs px-2 py-1 text-meta font-semibold tracking-[0.06em] uppercase",
          destructive ? "bg-danger-tint text-danger-ink" : "bg-secondary-tint text-secondary-hover",
        )}>{str(data.status, destructive ? "Confirm removal" : "Ready to save")}</span>
      </div>
    </div>
    <div className="space-y-2 px-3.5 py-3">
      <div className="flex items-center gap-2 text-note"><CalendarDays size={14} className="text-ink-muted" /><span className="text-ink-muted">Time</span><span className="ml-auto font-medium text-ink">{formatInstant(data.transactionAt) || "—"}</span></div>
      {rows.map(([label, value]) => <div key={label} className="flex flex-wrap items-baseline gap-3 border-t border-line-soft pt-2 text-note"><span className="text-ink-muted">{label}</span><span className="ml-auto text-right font-medium text-ink">{value}</span></div>)}
      {Array.isArray(data.tags) && data.tags.length ? <p className="text-note text-ink-muted">{data.tags.map(String).map((tag) => `#${tag}`).join(" · ")}</p> : null}
      {inferred.length ? <p className="flex items-start gap-2 border-t border-line-soft pt-2 text-meta leading-4 text-ink-muted"><Info size={13} className="mt-px shrink-0" />Inferred: {inferred.map((field) => field.replaceAll("_", " ")).join(", ")}. Edit if needed.</p> : null}
    </div>
    <ActionRow widget={widget} actions={ensureDraftCancel(widget, widget.actions, disabled && !pending)} disabled={disabled} pending={pending} onAction={onAction} icons={{ [widgetActionIds.edit_transaction]: <PencilLine />, [widgetActionIds.commit_transaction]: <Check />, [widgetActionIds.confirm_remove_transaction]: <Trash2 /> }} />
  </Card>;
}

function TransactionPreview({ widget, onAction, disabled, pending }: WidgetProps) {
  const removed = widget.data.status === "Removed";
  const classification = formatTransactionClassification(widget.data.transactionType, widget.data.category, widget.data.subcategory);
  const tags = Array.isArray(widget.data.tags) ? widget.data.tags.map(String) : [];
  const sourceCount = Math.max(1, num(widget.data.sourceCount));
  const spendNature = str(widget.data.spendNature);
  const metadata = [widget.data.location, spendNature && spendNature !== "unknown" ? formatEnumLabel(spendNature) : null].filter(Boolean).map(String);
  return <Card className={cn("border-secondary-line", removed && "border-danger-line")}>
    <div className="flex items-center gap-3 p-4">
      <span className={cn("grid size-10 shrink-0 place-items-center rounded-full bg-secondary-tint text-secondary", removed && "bg-danger-tint text-danger-ink")}>{removed ? <Trash2 /> : <Check size={20} strokeWidth={2.5} />}</span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-control font-semibold text-ink">{str(widget.data.title, "Transaction saved")}</p>
        <p className="mt-0.5 text-note text-ink-muted">{[classification, formatInstant(widget.data.transactionAt), str(widget.data.status) && !removed ? str(widget.data.status) : null, `${sourceCount} source${sourceCount === 1 ? "" : "s"}`].filter(Boolean).join(" · ")}</p>
        {metadata.length || tags.length ? <p className="mt-1 truncate text-meta text-ink-muted">{[...metadata, ...tags.map((tag) => `#${tag}`)].join(" · ")}</p> : null}
      </div>
      <Money value={widget.data.amountMinor} currency={str(widget.data.currency, "INR")} className="shrink-0 font-semibold text-ink" />
    </div>
    {widget.actions.length ? <ActionRow widget={widget} disabled={disabled} pending={pending} onAction={onAction} icons={{ [widgetActionIds.edit_saved_transaction]: <PencilLine size={14} />, [widgetActionIds.request_remove_transaction]: <Trash2 size={14} /> }} /> : null}
  </Card>;
}

function TransactionEdit({ widget, onAction, disabled, pending }: WidgetProps) {
  const saved = typeof widget.data.transactionId === "string";
  const submitted = completionValues(widget);
  // `fields` is the whitelist of what this particular edit may change, in the
  // backend's own names. When only the amount is missing, the card asks for
  // that one thing instead of reprinting the whole record.
  const requested = Array.isArray(widget.data.fields) ? widget.data.fields.map(String) : null;
  const shows = (field: string) => !requested || requested.includes(field);
  const effectiveAmount = submitted.amountMinor ?? widget.data.amountMinor;
  const hasAmount = effectiveAmount != null;
  const completing = !hasAmount;
  const [amount, setAmount] = useState(hasAmount ? String(num(effectiveAmount) / 100) : "");
  const [merchant, setMerchant] = useState(str(submitted.merchant ?? widget.data.merchant));
  const [transactionAt, setTransactionAt] = useState(timestampInputValue(submitted.transactionAt ?? widget.data.transactionAt));
  const [categoryId, setCategoryId] = useState(str(submitted.categoryId ?? widget.data.categoryId));
  const [subcategoryId, setSubcategoryId] = useState(str(submitted.subcategoryId ?? widget.data.subcategoryId));
  const [transactionType, setTransactionType] = useState(str(submitted.transactionType ?? widget.data.transactionType, "expense"));
  const [location, setLocation] = useState(str(submitted.location ?? widget.data.location));
  const [spendNature, setSpendNature] = useState(str(submitted.spendNature ?? widget.data.spendNature, "unknown"));
  const submittedTags = submitted.tags ?? widget.data.tags;
  const [tags, setTags] = useState(Array.isArray(submittedTags) ? submittedTags.map(String).join(", ") : "");
  const [amountError, setAmountError] = useState<string | null>(null);
  const [transactionAtError, setTransactionAtError] = useState<string | null>(null);
  const [submittedAction, markSubmitted] = usePendingAction(pending);
  const categories = Array.isArray(widget.data.categories) ? widget.data.categories as Data[] : [];
  const subcategories = (Array.isArray(widget.data.subcategories) ? widget.data.subcategories as Data[] : []).filter((item) => str(item.categoryId) === categoryId);
  const needsCategory = categories.length > 0 && transactionType === "expense" && shows("category");
  const editable = { type: saved && shows("transaction_type"), location: saved && shows("location"), nature: saved && transactionType === "expense" && shows("spend_nature"), tags: saved && shows("tags") };
  // Persisted edit widgets created before the cancel action was added still
  // receive the safe backend cancel path after a refresh.
  const submitAction = saved ? widgetActionIds.update_saved_transaction : widgetActionIds.update_transaction_draft;
  const declaredNavigation = widget.actions.filter((action) => action.action !== submitAction);
  const navigationActions: Widget["actions"] = ensureDraftCancel(widget, declaredNavigation.length || !saved ? declaredNavigation : [{
    id: "cancel",
    label: "Cancel",
    action: widgetActionIds.cancel_saved_transaction_edit,
    style: "secondary",
    payload: { transactionId: widget.data.transactionId },
  }], disabled && !pending);

  function submit(event: FormEvent) {
    event.preventDefault();
    const minor = parseAmountToMinor(amount);
    if (minor === null) { setAmountError("Enter an amount greater than zero, like 1,500 or 1500.50."); return; }
    const utcTransactionAt = shows("transaction_at") ? timestampInputToUtc(transactionAt) : null;
    if (shows("transaction_at") && !utcTransactionAt) { setTransactionAtError("Enter a valid date and time."); return; }
    setAmountError(null);
    setTransactionAtError(null);
    const payload: Record<string, unknown> = saved
      ? { transactionId: widget.data.transactionId, amountMinor: minor }
      : { draftId: widget.data.draftId, amountMinor: minor };
    if (shows("merchant")) payload.merchant = merchant;
    if (shows("transaction_at")) payload.transactionAt = utcTransactionAt;
    if (saved && shows("transaction_type")) payload.transactionType = transactionType;
    if (saved && shows("location")) payload.location = location;
    if (saved && transactionType === "expense" && shows("spend_nature")) payload.spendNature = spendNature;
    if (saved && shows("tags")) payload.tags = tags.split(",").map((tag) => tag.trim()).filter(Boolean);
    if (saved && transactionType === "expense" && shows("category")) payload.categoryId = categoryId || null;
    if (saved && transactionType === "expense" && shows("subcategory")) payload.subcategoryId = subcategoryId || null;
    markSubmitted("submit");
    onAction(widget.id, saved ? widgetActionIds.update_saved_transaction : widgetActionIds.update_transaction_draft, payload);
  }

  return <Card className="hitl-card"><form onSubmit={submit} noValidate className="space-y-3 p-3">
    <h3 className="font-heading text-body font-semibold text-ink">{str(widget.data.title, saved ? "Edit transaction" : "Edit this entry")}</h3>
    <div className="grid gap-3 sm:grid-cols-2">
      <label className="block"><FieldLabel>Amount</FieldLabel><input disabled={disabled || pending} aria-label="Transaction amount" aria-invalid={Boolean(amountError)} aria-describedby={amountError ? `${widget.id}-amount-error` : undefined} inputMode="decimal" autoFocus={completing && !disabled} value={amount} onChange={(event) => { setAmount(event.target.value); if (amountError) setAmountError(null); }} placeholder="1,500" className={cn(inputClass, amountError && invalidClass)} />{amountError ? <span id={`${widget.id}-amount-error`}><FieldError>{amountError}</FieldError></span> : null}</label>
      {shows("merchant") ? <label className="block"><FieldLabel hint="optional">Merchant</FieldLabel><input disabled={disabled || pending} aria-label="Merchant" value={merchant} onChange={(event) => setMerchant(event.target.value)} placeholder="Where you paid" className={inputClass} /></label> : null}
      {shows("transaction_at") ? <label className="block"><FieldLabel>Date and time</FieldLabel><input disabled={disabled || pending} aria-label="Transaction date and time" aria-invalid={Boolean(transactionAtError)} type="datetime-local" value={transactionAt} onChange={(event) => { setTransactionAt(event.target.value); if (transactionAtError) setTransactionAtError(null); }} className={cn(inputClass, transactionAtError && invalidClass)} />{transactionAtError ? <FieldError>{transactionAtError}</FieldError> : null}</label> : null}
      {editable.type ? <div><FieldLabel>Type</FieldLabel><Combobox aria-label="Transaction type" disabled={disabled || pending} value={transactionType} onValueChange={(next) => {
        setTransactionType(next);
        if (next !== "expense" || !categories.some((item) => str(item.id) === categoryId)) {
          setCategoryId("");
          setSubcategoryId("");
        }
        if (next !== "expense") setSpendNature("unknown");
      }} options={editableTransactionTypes.map((type) => ({ value: type, label: type.replaceAll("_", " ") }))} searchable={false} triggerClassName="text-body" /></div> : null}
      {editable.location ? <label className="block"><FieldLabel hint="optional">Location</FieldLabel><input disabled={disabled || pending} aria-label="Transaction location" value={location} onChange={(event) => setLocation(event.target.value)} placeholder="City or place" className={inputClass} /></label> : null}
      {editable.nature ? <div><FieldLabel>Spend nature</FieldLabel><Combobox aria-label="Spend nature" disabled={disabled || pending} value={spendNature} onValueChange={setSpendNature} options={[{ value: "unknown", label: "Not set" }, { value: "essential", label: "Essential" }, { value: "discretionary", label: "Discretionary" }, { value: "potentially_avoidable", label: "Potentially avoidable" }]} triggerClassName="text-body" /></div> : null}
      {editable.tags ? <label className="block sm:col-span-2"><FieldLabel hint="comma separated">Tags</FieldLabel><input disabled={disabled || pending} aria-label="Transaction tags" value={tags} onChange={(event) => setTags(event.target.value)} placeholder="vacation, family, reimbursable" className={inputClass} /></label> : null}
      {needsCategory ? <>
        <div><FieldLabel>Category</FieldLabel><Combobox aria-label="Transaction category" disabled={disabled || pending} value={categoryId} onValueChange={(next) => { setCategoryId(next); setSubcategoryId(""); }} placeholder="Choose category" options={categories.map((item) => ({ value: str(item.id), label: str(item.label) }))} triggerClassName="text-body" /></div>
        <div><FieldLabel>Subcategory</FieldLabel><Combobox aria-label="Transaction subcategory" disabled={disabled || pending || !categoryId} value={subcategoryId} onValueChange={setSubcategoryId} placeholder={categoryId ? "Choose subcategory" : "Choose a category first"} options={subcategories.map((item) => ({ value: str(item.id), label: str(item.label) }))} triggerClassName="text-body" /></div>
      </> : null}
    </div>
    <HitlActions className="-mx-3 -mb-3 border-t border-line">
      {orderedActions(navigationActions).map((action) => <ActionButton key={action.id} action={action} pending={pending && submittedAction === action.id} disabled={disabled || pending} onClick={() => { markSubmitted(action.id); onAction(widget.id, action.action, action.payload); }} />)}
      <Button type="submit" disabled={disabled || pending || !amount.trim() || (needsCategory && (!categoryId || !subcategoryId))}>{pending && submittedAction === "submit" ? <Loader2 className="animate-spin" /> : null}{completing ? "Save entry" : "Apply changes"}</Button>
    </HitlActions>
  </form></Card>;
}

/** Charts are decoration for anyone who can't see them; the same numbers are
 *  always present as text, so the legend is the accessible source of truth. */
function FinancialSummary({ widget }: WidgetProps) {
  const currency = str(widget.data.currency, "INR");
  const scopePath = Array.isArray(widget.data.scopePath) ? widget.data.scopePath.map((item) => str(item)).filter(Boolean) : [];
  const description = str(widget.data.description);
  const breakdown = Array.isArray(widget.data.breakdown) ? widget.data.breakdown as Data[] : [];
  const chartData = breakdown.map((item) => ({ name: str(item.label), value: num(item.amount_minor) })).filter((item) => item.value > 0);
  const total = chartData.reduce((sum, item) => sum + item.value, 0);
  const leading = chartData.slice(0, 6);
  const rest = chartData.slice(6);
  const restTotal = rest.reduce((sum, item) => sum + item.value, 0);
  const count = num(widget.data.count);
  return <Card>
    <div className="px-4 pt-4">
      <p className="text-meta font-semibold tracking-[0.08em] text-ink-muted uppercase">{str(widget.data.period)}</p>
      <h3 className="mt-2 font-heading text-body font-medium text-ink-body">{str(widget.data.title)}</h3>
      {scopePath.length ? <p aria-label={`Category path: ${scopePath.join(" to ")}`} className="mt-1 text-note font-medium text-secondary">{scopePath.join(" → ")}</p> : null}
      <p className="money mt-1 text-display font-semibold text-ink">{formatMoney(widget.data.amountMinor, currency)}</p>
      <p className="text-note text-ink-muted">{count} recorded transaction{count === 1 ? "" : "s"}</p>
      {description ? <p className="mt-3 max-w-2xl text-note leading-5 text-ink-muted">{description}</p> : null}
    </div>
    {chartData.length ? <div className="p-4">
      <ul className="space-y-3">
        {leading.map((item, index) => <li key={item.name} className="flex items-center gap-2 text-note"><span className="size-2 shrink-0 rounded-full" style={{ background: palette[index % palette.length] }} /><span className="min-w-0 truncate text-ink-muted">{item.name}</span><Money value={item.value} currency={currency} className="ml-auto shrink-0 font-medium text-ink" /></li>)}
        {rest.length ? <li className="flex items-center gap-2 text-note"><span className="size-2 shrink-0 rounded-full bg-line-strong" /><span className="text-ink-muted">{rest.length} more {rest.length === 1 ? "category" : "categories"}</span><Money value={restTotal} currency={currency} className="ml-auto shrink-0 font-medium text-ink" /></li> : null}
        {total ? <li className="flex items-center gap-2 border-t border-line pt-2 text-note"><span className="font-medium text-ink-body">Total</span><Money value={total} currency={currency} className="ml-auto font-semibold text-ink" /></li> : null}
      </ul>
    </div> : count || num(widget.data.amountMinor)
      // A backend that answers an unscoped total sends no breakdown at all;
      // the header already tells the whole story, so only a real zero earns
      // the empty-state copy.
      ? <div aria-hidden className="pb-4" />
      : <p className="mx-5 my-4 rounded-2xl border border-dashed border-line py-6 text-center text-control text-ink-muted">No spending recorded in this period yet.</p>}
  </Card>;
}

type VisualEncoding = DataVisualizationData["views"][number]["encoding"];

function visualFields(encoding: VisualEncoding) {
  return [encoding.x, encoding.y, encoding.color, encoding.size, encoding.theta, encoding.row, encoding.column, ...encoding.tooltip]
    .filter((item): item is NonNullable<typeof item> => Boolean(item));
}

function vegaEncoding(encoding: VisualEncoding) {
  const fieldDefinition = (item: NonNullable<typeof encoding.x>) => ({
    field: item.field,
    type: item.type,
    title: item.title ?? undefined,
    sort: item.sort ?? undefined,
    // Vega uses d3-format. Currency is the `$` token and its actual symbol is
    // supplied by formatLocale below; a literal `₹` inside the specifier is
    // invalid and aborts the complete Vega render pipeline.
    ...(item.valueType === "money_minor" ? { format: "$,.2f" } : {}),
    ...(item.valueType === "percentage" ? { format: ".1%" } : {}),
  });
  return Object.fromEntries([
    ...(["x", "y", "color", "size", "theta", "row", "column"] as const)
      .filter((channel) => encoding[channel])
      .map((channel) => [channel, fieldDefinition(encoding[channel]!)]),
    ...(encoding.tooltip.length ? [["tooltip", encoding.tooltip.map(fieldDefinition)]] : []),
  ]);
}

function visualValue(value: unknown, encoding: NonNullable<VisualEncoding["x"]>) {
  const amount = num(value);
  if (encoding.valueType === "money_minor") return formatMoney(amount);
  if (encoding.valueType === "percentage") return `${(amount / 100).toFixed(1)}%`;
  return formatCount(amount, amount % 1 ? 2 : 0);
}

function chartGuide(view: DataVisualizationData["views"][number], rows: Array<Record<string, unknown>>) {
  const dimension = view.encoding.x ?? view.encoding.color;
  const measure = view.encoding.y ?? view.encoding.theta ?? view.encoding.color;
  const explanation = view.mark === "arc"
    ? `Segments represent ${dimension?.title ?? dimension?.field ?? "groups"}; their size represents ${measure?.title ?? measure?.field ?? "value"}.`
    : `${dimension?.title ?? dimension?.field ?? "Groups"} is plotted against ${measure?.title ?? measure?.field ?? "value"}.`;
  return <p className="mt-2 text-meta leading-4 text-ink-muted"><span className="font-medium text-ink-body">How to read this:</span> {explanation} Hover or focus the chart for exact values. {rows.length} data point{rows.length === 1 ? "" : "s"} included.</p>;
}

function visualChannels(view: DataVisualizationData["views"][number]) {
  const { x, y, color, theta } = view.encoding;
  return { x, y, color, theta };
}

function seriesData(view: DataVisualizationData["views"][number], rows: Array<Record<string, unknown>>) {
  const { x, y, color } = visualChannels(view);
  if (!x || !y || !color) return { rows, keys: y ? [y.field] : [] };
  const keys = [...new Set(rows.map((row) => str(row[color.field])).filter(Boolean))];
  const byDimension = new Map<string, Record<string, unknown>>();
  rows.forEach((row) => {
    const rawDimension = row[x.field];
    const id = String(rawDimension ?? "");
    const target = byDimension.get(id) ?? { [x.field]: rawDimension };
    target[str(row[color.field])] = row[y.field];
    byDimension.set(id, target);
  });
  return { rows: [...byDimension.values()], keys };
}

function RechartsView({ view, rows }: { view: DataVisualizationData["views"][number]; rows: Array<Record<string, unknown>> }) {
  const { x, y, color, theta } = visualChannels(view);
  const prepared = useMemo(() => seriesData(view, rows), [view, rows]);
  const height = Math.max(220, view.height);
  const tooltipFormatter = (value: unknown, name: unknown) => [y ? visualValue(value, y) : String(value ?? ""), String(name ?? "Value")];
  const yTick = (value: unknown) => y ? visualValue(value, y) : String(value ?? "");

  if (view.mark === "arc" && color && theta) {
    const moneyEncoding = view.encoding.tooltip.find((item) => item.valueType === "money_minor");
    const totalMinor = moneyEncoding ? rows.reduce((sum, row) => sum + num(row[moneyEncoding.field]), 0) : null;
    const donutHeight = Math.min(height, 300);
    return <div className="chart-donut-container">
    <div className="chart-donut-layout">
      <div style={{ height: donutHeight }} className="chart-reveal chart-donut-plot relative w-full min-w-0" role="img" aria-label={`${view.title}. ${rows.length} plotted data points.`}>
        <ResponsiveContainer width="100%" height="100%" minWidth={0}>
          <PieChart accessibilityLayer>
            <Pie data={rows} dataKey={theta.field} nameKey={color.field} innerRadius="48%" outerRadius="78%" paddingAngle={1.5} isAnimationActive={false}>
              {rows.map((row, index) => <Cell key={`${str(row[color.field])}-${index}`} fill={palette[index % palette.length]} />)}
            </Pie>
            <Tooltip formatter={(value, name) => [visualValue(value, theta), String(name)]} />
          </PieChart>
        </ResponsiveContainer>
        {totalMinor !== null ? <div className="pointer-events-none absolute inset-0 grid place-items-center text-center" aria-hidden="true"><div><p className="text-meta font-semibold tracking-[0.08em] text-ink-muted uppercase">Total</p><p className="money mt-1 text-title font-semibold text-secondary">{formatMoney(totalMinor)}</p></div></div> : null}
      </div>
      <ul className="chart-donut-legend" aria-label={`${color.title ?? color.field} legend`}>
        {rows.map((row, index) => <li key={`${str(row[color.field])}-${index}`} className="chart-donut-legend-item flex min-w-0 items-center gap-2 text-note">
          <span className="size-2.5 shrink-0 rounded-full" style={{ backgroundColor: palette[index % palette.length] }} aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate text-ink-muted">{formatDimension(row[color.field])}</span>
          <span className="shrink-0 font-medium tabular-nums text-ink-body">{visualValue(row[theta.field], theta)}</span>
        </li>)}
      </ul>
    </div>
    {chartGuide(view, rows)}
  </div>;
  }

  if (!x || !y) return <div role="alert" className="rounded-xl border border-dashed border-danger-line px-4 py-6 text-center text-note text-danger-ink">This visual is missing a validated axis. The underlying data was not discarded.</div>;
  const common = <>
    <CartesianGrid stroke={chartInk.grid} vertical={false} />
    <XAxis dataKey={x.field} tick={{ fill: chartInk.label, fontSize: 11 }} axisLine={{ stroke: chartInk.domain }} tickLine={false} />
    <YAxis tickFormatter={yTick} tick={{ fill: chartInk.label, fontSize: 11 }} axisLine={false} tickLine={false} width={82} />
    <Tooltip formatter={tooltipFormatter} />
    {prepared.keys.length > 1 ? <Legend iconType="circle" /> : null}
  </>;
  const series = prepared.keys.length ? prepared.keys : [y.field];
  let chart: React.ReactNode;
  if (view.mark === "bar" || view.mark === "tick") chart = <BarChart data={prepared.rows} accessibilityLayer>{common}{series.map((key, index) => <Bar key={key} dataKey={key} name={key} fill={palette[index % palette.length]} isAnimationActive={false} />)}</BarChart>;
  else if (view.mark === "area") chart = <AreaChart data={prepared.rows} accessibilityLayer>{common}{series.map((key, index) => <Area key={key} type="monotone" dataKey={key} name={key} stroke={palette[index % palette.length]} fill={palette[index % palette.length]} fillOpacity={0.14} isAnimationActive={false} />)}</AreaChart>;
  else if (view.mark === "point") chart = <ScatterChart accessibilityLayer>{common}<Scatter data={prepared.rows} dataKey={y.field} fill={palette[0]} isAnimationActive={false} /></ScatterChart>;
  else chart = <LineChart data={prepared.rows} accessibilityLayer>{common}{series.map((key, index) => <Line key={key} type="monotone" dataKey={key} name={key} stroke={palette[index % palette.length]} strokeWidth={2} dot={rows.length <= 24} isAnimationActive={false} connectNulls />)}</LineChart>;
  return <div>
    <div style={{ height }} className="chart-reveal w-full min-w-0" role="img" aria-label={`${view.title}. ${rows.length} plotted data points.`}><ResponsiveContainer width="100%" height="100%" minWidth={0}>{chart}</ResponsiveContainer></div>
    {chartGuide(view, rows)}
  </div>;
}

function VegaView({ view, rows }: { view: DataVisualizationData["views"][number]; rows: Array<Record<string, unknown>> }) {
  const target = useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = useState(0);
  const [status, setStatus] = useState<"loading" | "ready" | "failed">("loading");
  const [retry, setRetry] = useState(0);
  // Serialising the whole dataset is the price of keying the embed on the
  // contract rather than on object identity. It is paid when the contract
  // changes, not on every render of the conversation around it.
  const payload = useMemo(() => JSON.stringify({ view, rows }), [view, rows]);
  useEffect(() => {
    const parent = target.current?.parentElement;
    if (!parent) return;
    const measure = () => setContainerWidth(Math.max(240, Math.floor(parent.getBoundingClientRect().width || 640)));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(parent);
    return () => observer.disconnect();
  }, [payload]);
  useEffect(() => {
    if (!target.current || !containerWidth) return;
    let active = true;
    let finalized: (() => void) | undefined;
    setStatus("loading");
    const moneyFields = new Set(visualFields(view.encoding).filter((item) => item.valueType === "money_minor").map((item) => item.field));
    const percentageFields = new Set(visualFields(view.encoding).filter((item) => item.valueType === "percentage").map((item) => item.field));
    const values = rows.map((row) => Object.fromEntries(Object.entries(row).map(([key, value]) => [
      key,
      moneyFields.has(key) && typeof value === "number"
        ? value / 100
        : percentageFields.has(key) && typeof value === "number"
          ? value / 10_000
          : value,
    ])));
    const mark = view.mark === "arc"
      ? { type: "arc" as const, innerRadius: 54, outerRadius: 92, tooltip: true }
      : { type: view.mark, tooltip: true };
    const spec: TopLevelSpec = {
      $schema: "https://vega.github.io/schema/vega-lite/v6.json",
      data: { values },
      mark,
      // A measured numeric width works for ordinary, layered and faceted
      // views. It also avoids a zero-width first render while a virtualized
      // conversation column is settling.
      width: Math.max(240, containerWidth - 2),
      height: view.height,
      encoding: vegaEncoding(view.encoding),
      config: {
        background: "transparent",
        view: { stroke: null },
        axis: { labelColor: chartInk.label, titleColor: chartInk.title, gridColor: chartInk.grid, domainColor: chartInk.domain },
        legend: { labelColor: chartInk.label, titleColor: chartInk.title },
        range: { category: palette },
      },
    } as TopLevelSpec;
    const node = target.current;
    node.replaceChildren();
    void import("vega-embed").then(async ({ default: embed }) => {
      if (!active) return;
      const result = await embed(node, spec, {
        actions: false,
        renderer: "svg",
        formatLocale: {
          decimal: ".",
          thousands: ",",
          grouping: [3, 2],
          currency: ["₹", ""],
        },
      });
      if (!active) {
        result.finalize();
        return;
      }
      finalized = () => result.finalize();
      setStatus("ready");
    }).catch((error: unknown) => {
      if (!active) return;
      node.replaceChildren();
      setStatus("failed");
      console.error("Governed chart renderer failed", error);
    });
    return () => {
      active = false;
      finalized?.();
      node.replaceChildren();
    };
  // payload is the complete validated declarative contract for this view.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payload, containerWidth, retry]);
  const dimension = view.encoding.x ?? view.encoding.color;
  const measure = view.encoding.y ?? view.encoding.theta ?? view.encoding.color;
  const chartHelp = view.mark === "arc"
    ? `Segments represent ${dimension?.title ?? dimension?.field ?? "groups"}; their size represents ${measure?.title ?? measure?.field ?? "value"}.`
    : `${dimension?.title ?? dimension?.field ?? "Groups"} is plotted against ${measure?.title ?? measure?.field ?? "value"}.`;
  return <div className="min-w-0">
    <div className="relative min-h-[220px]">
      <div key={`${payload}-${retry}`} ref={target} role="img" className={cn("min-w-0 [&_.vega-embed]:w-full [&_.vega-embed>svg]:max-w-full", status === "ready" ? "chart-reveal" : "opacity-0")} aria-label={`${view.title}. ${rows.length} plotted data points.`} />
      {status === "loading" ? <div role="status" className="absolute inset-0 grid place-items-center rounded-xl bg-surface-sunken text-note text-ink-muted"><span className="flex items-center gap-2"><LoaderCircle className="animate-spin" />Preparing {rows.length} data point{rows.length === 1 ? "" : "s"}…</span></div> : null}
      {status === "failed" ? <div role="alert" className="absolute inset-0 grid place-items-center rounded-xl border border-dashed border-danger-line bg-danger-tint px-4 text-center"><div><p className="text-control font-medium text-danger-ink">The chart renderer hit a problem.</p><p className="mt-1 text-note leading-5 text-ink-muted">The validated data is still available. Retry the visual or use the description below.</p><Button type="button" variant="outline" size="sm" onClick={() => setRetry((value) => value + 1)} className="mt-3 h-9 rounded-lg"><RotateCcw size={14} />Retry chart</Button></div></div> : null}
    </div>
    <p className="mt-2 text-meta leading-4 text-ink-muted"><span className="font-medium text-ink-body">How to read this:</span> {chartHelp} Hover or focus the chart for exact values. {rows.length} data point{rows.length === 1 ? "" : "s"} included.</p>
  </div>;
}

function GovernedVisualization({ data }: { data: DataVisualizationData }) {
  const columns = data.layout.columns === 3 ? "lg:grid-cols-3" : data.layout.columns === 2 ? "lg:grid-cols-2" : "grid-cols-1";
  const singleView = data.views.length === 1 ? data.views[0] : null;
  const headerTitle = singleView?.title ?? data.title;
  const headerBody = singleView?.description ?? data.body ?? undefined;
  return <Card>
    <CardHeader eyebrow="Governed analysis" title={headerTitle} body={headerBody} />
    <div className={cn("grid", columns, singleView ? "p-4" : "gap-3 p-4")}>
      {data.views.map((view) => {
        const rows = data.datasets[view.dataset] ?? [];
        return <section key={view.id} className={cn("min-w-0", !singleView && "rounded-2xl border border-line bg-surface-sunken p-3")}>
          {!singleView ? <h4 className="text-control font-semibold text-ink">{view.title}</h4> : null}
          {!singleView && view.description ? <p className="mt-1 text-meta leading-4 text-ink-muted">{view.description}</p> : null}
          {rows.length ? <div className={cn("min-w-0 overflow-x-auto", !singleView && "mt-3")}>{view.mark === "rect" ? <VegaView view={view} rows={rows} /> : <RechartsView view={view} rows={rows} />}</div> : <EmptyNote>{data.emptyMessage}</EmptyNote>}
        </section>;
      })}
    </div>
  </Card>;
}

/** Validation is keyed on the payload, not on the render: the contract cannot
 *  change without `widget.data` changing, and re-checking a hundred rows on
 *  every keystroke elsewhere in the app buys nothing. Holding the parsed result
 *  also keeps its identity stable, which is what lets the views below memoise. */
function DataVisualization({ widget }: WidgetProps) {
  const parsed = useMemo(() => dataVisualizationDataSchema.safeParse(widget.data), [widget.data]);
  return parsed.success
    ? <GovernedVisualization data={parsed.data} />
    : <Card><EmptyNote>This visualization could not be rendered because its governed contract is invalid.</EmptyNote></Card>;
}

/** Compatibility adapter for persisted version-1 chart widgets. New agent
 * runs emit the generic multi-view visualization grammar above. */
function legacyChartToVisualization(chart: DataChartData): DataVisualizationData {
  const primary = chart.series[0];
  const fieldType = (kind: DataChartData["xAxis"]["type"]) => kind === "date" || kind === "datetime" ? "temporal" as const : kind === "number" ? "quantitative" as const : "nominal" as const;
  const x = { field: chart.xAxis.key, type: fieldType(chart.xAxis.type), title: chart.xAxis.label, valueType: chart.xAxis.type === "datetime" ? "datetime" as const : "category" as const, sort: null };
  const value = { field: primary.key, type: "quantitative" as const, title: primary.label, valueType: primary.valueType === "money" ? "money_minor" as const : primary.valueType, sort: null };
  const color = primary.groupKey ? { field: primary.groupKey, type: "nominal" as const, title: "Series", valueType: "category" as const, sort: null } : undefined;
  const mark = chart.chartType === "pie" ? "arc" as const : chart.chartType === "heatmap" ? "rect" as const : chart.chartType;
  const yDimension = chart.yAxis ? { field: chart.yAxis.key, type: fieldType(chart.yAxis.type), title: chart.yAxis.label, valueType: chart.yAxis.type === "datetime" ? "datetime" as const : "category" as const, sort: null } : undefined;
  const emptyChannels = { x: null, y: null, color: null, size: null, theta: null, row: null, column: null };
  const encoding: VisualEncoding = mark === "arc"
    ? { ...emptyChannels, theta: value, color: x, tooltip: [x, value] }
    : mark === "rect"
      ? { ...emptyChannels, x, y: yDimension ?? null, color: value, row: color ?? null, tooltip: [x, ...(yDimension ? [yDimension] : []), value, ...(color ? [color] : [])] }
      : { ...emptyChannels, x, y: value, color: color ?? null, tooltip: [x, value, ...(color ? [color] : [])] };
  return {
    title: chart.title, body: chart.body, datasets: { legacy: chart.rows },
    views: [{ id: "legacy-view", title: chart.title, description: chart.body, dataset: "legacy", mark, encoding, height: 320 }],
    layout: { columns: 1 }, queryResults: null, emptyMessage: chart.emptyMessage,
  };
}

function DataChart({ widget }: WidgetProps) {
  const converted = useMemo(() => {
    const parsed = dataChartDataSchema.safeParse(widget.data);
    return parsed.success ? legacyChartToVisualization(parsed.data) : null;
  }, [widget.data]);
  return converted
    ? <GovernedVisualization data={converted} />
    : <Card><EmptyNote>This chart could not be rendered because its data contract is invalid.</EmptyNote></Card>;
}

function Scenario({ widget }: WidgetProps) {
  const currency = str(widget.data.currency, "INR");
  const affordable = Boolean(widget.data.affordable_now);
  const available = num(widget.data.available_after_reserve_minor);
  const purchase = num(widget.data.purchase_minor);
  const progress = Math.max(0, Math.min(100, purchase ? available / purchase * 100 : 0));
  return <Card>
    <div className="px-4 py-4">
      <div className="flex items-center gap-3">
        <span className={cn("grid size-11 shrink-0 place-items-center rounded-2xl", affordable ? "bg-secondary-tint text-secondary" : "bg-danger-tint text-danger")}>{affordable ? <Check size={20} /> : <TrendingUp size={20} />}</span>
        <div className="min-w-0"><h3 className="font-heading text-body font-semibold text-ink">{str(widget.data.title)}</h3><p className="text-note text-ink-muted">{affordable ? "Affordable with your reserve intact" : "Build a little more room first"}</p></div>
      </div>
      <div className="mt-4 space-y-2">
        <div className="flex justify-between text-note text-ink-muted"><span>Available after reserve</span><Money value={available} currency={currency} className="font-medium text-ink-body" /></div>
        <Progress value={progress} aria-label="Share of the purchase you can cover" className="h-2 bg-line [&_[data-slot=progress-indicator]]:bg-secondary" />
        <div className="flex justify-between text-meta text-ink-muted"><span>{str(widget.data.rule)}</span><span>Goal <Money value={purchase} currency={currency} /></span></div>
      </div>
    </div>
    {str(widget.data.dataQuality) ? <p className="border-t border-line bg-surface-sunken px-4 py-3 text-meta text-ink-muted">{str(widget.data.dataQuality)}</p> : null}
  </Card>;
}

function ProgressCard({ widget, onAction, onCancel, disabled, pending }: WidgetProps) {
  const isGoal = widget.type === widgetTypeIds.goal_progress;
  const currency = str(widget.data.currency, "INR");
  const current = num(isGoal ? widget.data.currentMinor : widget.data.spentMinor);
  const total = num(isGoal ? widget.data.targetMinor : widget.data.amountMinor);
  const ratio = total ? current / total * 100 : 0;
  const progress = Math.max(0, Math.min(100, ratio));
  // Spending past a budget is the one thing this card exists to warn about.
  const over = !isGoal && current > total && total > 0;
  const remainder = over ? current - total : num(widget.data.remainingMinor);
  return <Card className={cn("hitl-card", over && "border-danger-line")}>
    <div className="p-3">
      <div className="flex items-center gap-3">
        <span className={cn("grid size-9 shrink-0 place-items-center rounded-lg", over ? "bg-danger-tint text-danger" : "bg-secondary-tint text-secondary")}>{isGoal ? <Target size={17} /> : over ? <TriangleAlert size={17} /> : <WalletCards size={17} />}</span>
        <div className="min-w-0"><p className="text-meta font-semibold tracking-[0.08em] text-ink-muted uppercase">{isGoal ? "Savings goal" : "Monthly budget"}</p><h3 className="mt-0.5 font-heading text-body font-semibold text-ink">{str(widget.data.title)}</h3></div>
        <span className={cn("money ml-auto shrink-0 text-control font-semibold", over ? "text-danger-ink" : "text-secondary")}>{Math.round(ratio)}%</span>
      </div>
      <div className="mt-3">
        <Progress value={progress} aria-label={isGoal ? "Progress towards this goal" : "Share of this budget spent"} className={cn("h-2 bg-line", over ? "[&_[data-slot=progress-indicator]]:bg-danger" : "[&_[data-slot=progress-indicator]]:bg-secondary")} />
        <div className="mt-2 flex flex-wrap justify-between gap-3 text-note text-ink-muted">
          <span><Money value={current} currency={currency} className="font-medium text-ink-body" /> {isGoal ? "saved" : "spent"}</span>
          <span className={over ? "font-medium text-danger-ink" : undefined}><Money value={remainder} currency={currency} className={cn("font-medium", over ? "text-danger-ink" : "text-ink-body")} /> {over ? "over budget" : "remaining"}</span>
        </div>
        <p className="mt-2 text-right text-note text-ink-muted">Target <Money value={total} currency={currency} className="font-semibold text-ink" /></p>
      </div>
    </div>
    <ActionRow widget={widget} disabled={disabled} pending={pending} onAction={onAction} onCancel={onCancel} />
  </Card>;
}

function ImportReview({ widget, onAction, onCancel, disabled, pending }: WidgetProps) {
  const complete = str(widget.data.status) === "completed";
  const total = num(widget.data.total);
  const ready = num(widget.data.highConfidence);
  const review = num(widget.data.needsReview);
  const duplicates = num(widget.data.duplicates);
  const replay = Boolean(widget.data.idempotentReplay);
  const tiles: Array<[string, number]> = [
    ["Rows", total],
    ["Ready", ready],
    ["Needs a look", review],
    ["Duplicates", duplicates],
  ];
  return <Card className="hitl-card">
    <div className="border-b border-line px-3.5 py-3">
      <p className={cn("text-meta font-semibold tracking-[0.08em] uppercase", complete ? "text-secondary" : "text-ink-muted")}>{complete ? "Import complete" : "Statement review"}</p>
      <h3 className="mt-1 truncate font-heading text-body font-semibold text-ink" title={str(widget.data.title)}>{str(widget.data.title)}</h3>
      <p className="mt-0.5 text-note leading-4 text-ink-muted">{replay ? "Already imported—nothing was duplicated." : complete ? `${ready} recorded${review ? ` · ${review} need review` : ""}.` : `${total} row${total === 1 ? "" : "s"} ready to review before import.`}</p>
    </div>
    {total > 0 ? <dl className="grid grid-cols-2 gap-1.5 p-2.5 sm:grid-cols-4">{tiles.map(([label, value]) => <div key={label} className={cn("rounded-lg px-2.5 py-2", label === "Duplicates" && !complete ? "bg-surface-sunken/60" : "bg-surface-sunken")}>
      <dt className="text-meta font-semibold tracking-wide text-ink-muted uppercase">{label}</dt>
      <dd className="money mt-0.5 text-title font-semibold text-ink">{!complete && label === "Duplicates" ? "—" : value}</dd>
    </div>)}</dl> : <EmptyNote>This file has no transaction rows I can read. Check that it’s the statement export and not a summary, then attach it again.</EmptyNote>}
    {(widget.actions.length || onCancel) && total > 0 ? <ActionRow widget={widget} disabled={disabled} pending={pending} onAction={onAction} onCancel={onCancel} /> : null}
  </Card>;
}

/** Both calculators keep their inputs after producing a result: the whole point
 *  of a scenario is trying the next one. */
function CalculatorShell({ eyebrow, title, body, result, onEdit, disabled, children }: { eyebrow: string; title: string; body?: string; result?: React.ReactNode; onEdit?: () => void; disabled?: boolean; children?: React.ReactNode }) {
  return <Card><div className="p-3">
    <p className="text-meta font-semibold tracking-[0.08em] text-ink-muted uppercase">{eyebrow}</p>
    <h3 className="mt-1 font-heading text-body font-semibold text-ink">{title}</h3>
    {body ? <p className="mt-1 text-note leading-5 text-ink-muted">{body}</p> : null}
    {result}
    {onEdit ? <Button type="button" variant="outline" disabled={disabled} onClick={onEdit} className="mt-3">Try different numbers</Button> : null}
    {children}
  </div></Card>;
}


function useCalculatorEditor(widget: Widget) {
  const [editing, setEditing] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const complete = (submit: () => void) => {
    setProblem(null);
    setEditing(false);
    submit();
  };
  return {
    currency: str(widget.data.currency, "INR"),
    result: widget.data.result as Data | undefined,
    editing,
    setEditing,
    problem,
    setProblem,
    complete,
  };
}

function CalculatorForm({ widget, onSubmit, problem, disabled, pending, submitLabel, children }: { widget: Widget; onSubmit: (event: FormEvent) => void; problem: string | null; disabled?: boolean; pending?: boolean; submitLabel: string; children: React.ReactNode }) {
  return <Card className="hitl-card"><form onSubmit={onSubmit} noValidate className="p-3">
    <h3 className="font-heading text-body font-semibold text-ink">{str(widget.data.title)}</h3>
    <p className="mt-1 text-note leading-5 text-ink-muted">{str(widget.data.body)}</p>
    <div className="mt-3 grid gap-3 sm:grid-cols-2">{children}</div>
    {problem ? <FieldError>{problem}</FieldError> : null}
    <HitlActions className="-mx-3 -mb-3 mt-3 border-t border-line"><Button type="submit" disabled={disabled || pending}>{pending ? <Loader2 className="animate-spin" /> : null}{submitLabel}</Button></HitlActions>
  </form></Card>;
}

function LoanCalculator({ widget, onAction, disabled, pending }: WidgetProps) {
  const { currency, result, editing, setEditing, problem, setProblem, complete } = useCalculatorEditor(widget);
  const [principal, setPrincipal] = useState(widget.data.principalMinor ? String(num(widget.data.principalMinor) / 100) : "");
  const [rate, setRate] = useState(widget.data.annualRatePercent == null ? "" : String(num(widget.data.annualRatePercent)));
  const [months, setMonths] = useState(widget.data.tenureMonths == null ? "" : String(num(widget.data.tenureMonths)));
  const [prepayment, setPrepayment] = useState(widget.data.prepaymentMinor ? String(num(widget.data.prepaymentMinor) / 100) : "");

  if (result && !editing) {
    const baseline = (result.baseline ?? {}) as Data;
    const after = (result.after_prepayment ?? {}) as Data;
    return <CalculatorShell eyebrow="Deterministic loan analysis" title={str(widget.data.title)} disabled={disabled} onEdit={() => setEditing(true)} result={<>
      <div className="mt-4 grid gap-2 sm:grid-cols-2">
        <div className="rounded-2xl bg-surface-sunken p-3"><p className="text-meta text-ink-muted uppercase">Interest saved</p><Money value={result.interest_saved_minor} currency={currency} className="mt-1 block text-title font-semibold text-money-in" /></div>
        <div className="rounded-2xl bg-surface-sunken p-3"><p className="text-meta text-ink-muted uppercase">EMI reduction</p><Money value={result.emi_reduction_minor} currency={currency} className="mt-1 block text-title font-semibold text-money-in" /></div>
      </div>
      <dl className="mt-4 text-note text-ink-muted"><div className="flex py-1"><dt>Baseline EMI</dt><dd className="ml-auto"><Money value={baseline.emi_minor} currency={currency} className="font-medium text-ink-body" /></dd></div><div className="flex py-1"><dt>After prepayment</dt><dd className="ml-auto"><Money value={after.emi_minor} currency={currency} className="font-medium text-ink-body" /></dd></div></dl>
    </>} />;
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const principalMinor = parseAmountToMinor(principal);
    const annualRatePercent = parseNumber(rate);
    const tenureMonths = parseNumber(months);
    const prepaymentMinor = prepayment.trim() ? parseAmountToMinor(prepayment) : 0;
    if (principalMinor === null) { setProblem("Enter the outstanding principal, like 5,00,000."); return; }
    if (annualRatePercent === null || annualRatePercent <= 0) { setProblem("Enter the annual interest rate, like 8.5."); return; }
    if (tenureMonths === null || tenureMonths < 1) { setProblem("Enter how many months are left, like 180."); return; }
    if (prepaymentMinor === null) { setProblem("Enter a prepayment amount, or leave it empty."); return; }
    complete(() => onAction(widget.id, widgetActionIds.calculate_loan_scenario, { principalMinor, annualRatePercent, tenureMonths, prepaymentMinor }, { markUsed: false }));
  }

  return <CalculatorForm widget={widget} onSubmit={submit} problem={problem} disabled={disabled} pending={pending} submitLabel="Calculate savings">
      <label className="block"><FieldLabel>Outstanding principal</FieldLabel><input disabled={disabled || pending} aria-label="Outstanding principal" inputMode="decimal" value={principal} onChange={(event) => setPrincipal(event.target.value)} placeholder="5,00,000" className={inputClass} /></label>
      <label className="block"><FieldLabel>Annual rate %</FieldLabel><input disabled={disabled || pending} aria-label="Annual interest rate" inputMode="decimal" value={rate} onChange={(event) => setRate(event.target.value)} placeholder="8.5" className={inputClass} /></label>
      <label className="block"><FieldLabel>Months remaining</FieldLabel><input disabled={disabled || pending} aria-label="Remaining tenure months" inputMode="numeric" value={months} onChange={(event) => setMonths(event.target.value)} placeholder="180" className={inputClass} /></label>
      <label className="block"><FieldLabel hint="optional">Prepayment</FieldLabel><input disabled={disabled || pending} aria-label="Prepayment amount" inputMode="decimal" value={prepayment} onChange={(event) => setPrepayment(event.target.value)} placeholder="1,00,000" className={inputClass} /></label>
  </CalculatorForm>;
}

function InvestmentProjection({ widget, onAction, disabled, pending }: WidgetProps) {
  const { currency, result, editing, setEditing, problem, setProblem, complete } = useCalculatorEditor(widget);
  const [monthly, setMonthly] = useState(widget.data.monthlyContributionMinor ? String(num(widget.data.monthlyContributionMinor) / 100) : "");
  const [current, setCurrent] = useState(widget.data.currentValueMinor ? String(num(widget.data.currentValueMinor) / 100) : "0");
  const [rate, setRate] = useState(widget.data.annualReturnPercent == null ? "10" : String(num(widget.data.annualReturnPercent)));
  const [years, setYears] = useState(widget.data.years == null ? "10" : String(num(widget.data.years)));

  if (result && !editing) return <CalculatorShell eyebrow="Assumption-based projection" title={str(widget.data.title)} disabled={disabled} onEdit={() => setEditing(true)} result={<>
    <Money value={result.projected_value_minor} currency={currency} className="mt-4 block text-display font-semibold text-ink" />
    <p className="text-note text-ink-muted">Projected after {num(result.years)} years at {num(result.assumed_annual_return_percent)}% assumed return</p>
    <div className="mt-4 flex flex-wrap gap-x-4 gap-1 text-note text-ink-muted"><span>Contributions <Money value={result.invested_minor} currency={currency} className="font-medium text-ink-body" /></span><span className="sm:ml-auto">Estimated growth <Money value={result.estimated_returns_minor} currency={currency} className="font-medium text-ink-body" /></span></div>
    <p className="mt-4 text-meta leading-5 text-ink-muted">Market returns are uncertain. This is a deterministic scenario, not a forecast or a guarantee.</p>
  </>} />;

  function submit(event: FormEvent) {
    event.preventDefault();
    const monthlyContributionMinor = parseAmountToMinor(monthly);
    const currentValueMinor = current.trim() ? parseAmountToMinor(current) ?? 0 : 0;
    const annualReturnPercent = parseNumber(rate);
    const projectionYears = parseNumber(years);
    if (monthlyContributionMinor === null) { setProblem("Enter a monthly contribution, like 10,000."); return; }
    if (annualReturnPercent === null || annualReturnPercent <= 0) { setProblem("Enter the return you want to assume, like 10."); return; }
    if (projectionYears === null || projectionYears < 1) { setProblem("Enter how many years to project, like 10."); return; }
    complete(() => onAction(widget.id, widgetActionIds.calculate_investment_scenario, { monthlyContributionMinor, currentValueMinor, annualReturnPercent, years: projectionYears }, { markUsed: false }));
  }

  return <CalculatorForm widget={widget} onSubmit={submit} problem={problem} disabled={disabled} pending={pending} submitLabel="Project value">
      <label className="block"><FieldLabel>Monthly contribution</FieldLabel><input disabled={disabled || pending} aria-label="Monthly contribution" inputMode="decimal" value={monthly} onChange={(event) => setMonthly(event.target.value)} placeholder="10,000" className={inputClass} /></label>
      <label className="block"><FieldLabel hint="optional">Current value</FieldLabel><input disabled={disabled || pending} aria-label="Current investment value" inputMode="decimal" value={current} onChange={(event) => setCurrent(event.target.value)} className={inputClass} /></label>
      <label className="block"><FieldLabel>Annual return %</FieldLabel><input disabled={disabled || pending} aria-label="Expected annual return" inputMode="decimal" value={rate} onChange={(event) => setRate(event.target.value)} className={inputClass} /></label>
      <label className="block"><FieldLabel>Years</FieldLabel><input disabled={disabled || pending} aria-label="Projection years" inputMode="numeric" value={years} onChange={(event) => setYears(event.target.value)} className={inputClass} /></label>
  </CalculatorForm>;
}

function ReconciliationReview({ widget, onAction, disabled, pending }: WidgetProps) {
  const incoming = (widget.data.incoming ?? {}) as Data;
  const existing = (widget.data.existing ?? {}) as Data;
  const signals = Array.isArray(widget.data.signals) ? widget.data.signals.map(String) : [];
  const existingSources = Math.max(1, num(existing.sourceCount));
  const [confirmingMerge, setConfirmingMerge] = useState(false);
  const merge = widget.actions.find((action) => action.action === widgetActionIds.merge_reconciliation);
  const separate = widget.actions.find((action) => action.action !== widgetActionIds.merge_reconciliation);

  return <Card className="hitl-card">
    <div className="border-b border-line px-3.5 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1"><h3 className="font-heading text-body font-semibold text-ink">{str(widget.data.title, "Possible duplicate")}</h3>{signals.length ? <p className="mt-0.5 text-note leading-4 text-ink-muted">Matched on {signals.map((signal) => signal.replaceAll("_", " ")).join(", ")}</p> : null}</div>
        <span className="money shrink-0 rounded-full bg-danger-tint px-2.5 py-1 text-meta font-semibold text-danger-ink">{Math.round(num(widget.data.score) * 100)}% match</span>
      </div>
    </div>
    <div className="grid gap-1.5 p-2.5 sm:grid-cols-2">
      <div className="rounded-lg bg-surface-sunken p-2.5"><p className="text-meta font-semibold tracking-[0.06em] text-ink-muted uppercase">New · {str(incoming.source, "unknown source")}</p><div className="mt-1.5 flex items-baseline gap-2"><Money value={incoming.amountMinor} currency={str(incoming.currency, "INR")} className="text-control font-semibold text-ink" /><p className="min-w-0 truncate text-note text-ink-body">{str(incoming.merchant, "Unknown merchant")}</p></div><p className="mt-1 text-meta text-ink-muted">{formatInstant(incoming.transactionAt)}</p></div>
      <div className="rounded-lg bg-secondary-tint p-2.5"><p className="text-meta font-semibold tracking-[0.06em] text-secondary uppercase">Saved · {existingSources} source{existingSources === 1 ? "" : "s"}</p><div className="mt-1.5 flex items-baseline gap-2"><Money value={existing.amountMinor} currency={str(existing.currency, "INR")} className="text-control font-semibold text-ink" /><p className="min-w-0 truncate text-note text-ink-body">{str(existing.merchant, "Unknown merchant")}</p></div><p className="mt-1 text-meta text-ink-muted">{formatInstant(existing.transactionAt)}</p></div>
    </div>
    {confirmingMerge && merge ? <div className="hitl-reveal border-t border-line px-3 py-2.5">
      <p className="text-note leading-4 text-ink-body">Merge into one transaction and keep both sources? This can’t be split here later.</p>
      <HitlActions className="-mx-3 -mb-2.5 mt-2.5 border-t border-line-soft">
        <Button type="button" variant="ghost" disabled={disabled || pending} onClick={() => setConfirmingMerge(false)}>Go back</Button>
        <Button type="button" disabled={disabled || pending} onClick={() => onAction(widget.id, merge.action, merge.payload)}>{pending ? <Loader2 className="animate-spin" /> : null}Merge</Button>
      </HitlActions>
    </div> : <HitlActions className="border-t border-line">
      {separate ? <ActionButton action={separate} pending={pending} disabled={disabled} onClick={() => onAction(widget.id, separate.action, separate.payload)} /> : null}
      {merge ? <Button type="button" disabled={disabled || pending} onClick={() => setConfirmingMerge(true)}>{merge.label}</Button> : null}
    </HitlActions>}
  </Card>;
}

function TransactionList({ widget, onAction, disabled, pending }: WidgetProps) {
  const transactions = Array.isArray(widget.data.transactions) ? widget.data.transactions as Data[] : [];
  return <Card>
    <CardHeader title={str(widget.data.title)} body={str(widget.data.body) || undefined} />
    {transactions.length ? <ul className="divide-y divide-line">{transactions.map((transaction, index) => {
      const actions = Array.isArray(transaction.actions) ? transaction.actions as Data[] : [];
      const amount = transaction.amountMinor;
      // Saved analyses and other non-monetary rows arrive with a zero amount and
      // a status; showing "₹0" there would be a lie about money.
      const showAmount = num(amount) !== 0;
      const status = str(transaction.status);
      return <li key={str(transaction.id, String(index))} className="flex flex-wrap items-center gap-3 px-3.5 py-3">
        <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-surface-sunken text-secondary"><ReceiptText /></span>
        <div className="min-w-0 flex-1"><p className="truncate text-control font-medium text-ink">{str(transaction.merchant, "Recorded item")}</p><p className="truncate text-note text-ink-muted">{[formatInstant(transaction.transactionAt), status].filter(Boolean).join(" · ")}</p></div>
        {showAmount ? <Money value={amount} currency={str(transaction.currency, "INR")} className="shrink-0 text-control font-semibold text-ink" /> : null}
        {actions.map((action, actionIndex) => { const actionId = action.action; if (!isWidgetActionId(actionId)) return null; const removing = actionId.includes("remove"); return <Button key={str(action.id, String(actionIndex))} type="button" size="sm" variant="outline" disabled={disabled || pending} onClick={() => onAction(widget.id, actionId, (action.payload ?? {}) as Record<string, unknown>)} className={cn("basis-full sm:basis-auto", removing && "border-danger-line text-danger-ink hover:bg-danger-tint")}>{removing ? <Trash2 size={14} /> : <PencilLine size={14} />}{str(action.label, "Review")}</Button>; })}
      </li>;
    })}</ul> : <EmptyNote>Nothing here yet. Record a few transactions and this list fills itself in.</EmptyNote>}
  </Card>;
}

function DynamicDataTable({ widget, onAction, disabled, pending }: WidgetProps) {
  const parsed = useMemo(() => dataTableDataSchema.safeParse(widget.data), [widget.data]);
  if (!parsed.success) return <Card><CardHeader title="This data view could not be rendered" body="The widget payload did not match the registered table contract." /></Card>;
  return <DataTableView data={parsed.data} disabled={disabled} pending={pending} onAction={(action, payload) => onAction(widget.id, action, payload)} />;
}

/** A note, not a card.
 *
 *  The harness uses this for welcomes, asides and refusals alike, and as a full
 *  card with an icon tile and a display-sized heading it outweighed the answer
 *  it was annotating — a greeting arrived as the loudest object in the thread,
 *  restating the sentence directly above it. So the neutral case is plain small
 *  text with no box at all.
 *
 *  A refusal is the one variant that still has to be noticed, and it keeps a
 *  hairline rule and the danger ink for exactly that reason. It is a marked
 *  paragraph rather than a panel: the weight goes to the words, not the frame.
 *
 *  The eyebrow is dropped in both. It defaulted to "Copilot insight" directly
 *  beneath a byline already reading COPILOT, and where the model chose its own
 *  it was decoration ("START NATURALLY") rather than information. */
function Insight({ widget }: WidgetProps) {
  const tone = str(widget.data.tone);
  const caution = tone === "caution" || /won’t|won't|need|missing|can’t|can't/i.test(str(widget.data.title));
  const title = str(widget.data.title);
  const body = str(widget.data.body);

  // The body is the useful half — the examples you can actually type — so it
  // carries the weight rather than the label above it. The caution variant
  // keeps its body at regular weight, because there it is explanatory prose
  // rather than a list of things to try.
  if (!caution) return <div className="widget-enter max-w-[62ch]">
    {title ? <p className="text-control font-medium text-ink-body">{title}</p> : null}
    {body ? <p className={cn("text-control font-medium leading-6 text-ink", title && "mt-0.5")}>{body}</p> : null}
  </div>;

  return <div className="widget-enter flex max-w-[62ch] gap-2 border-l-2 border-danger-line pl-3">
    <Info size={15} className="mt-0.5 shrink-0 text-danger" />
    <div className="min-w-0">
      {title ? <p className="text-control font-semibold text-danger-ink">{title}</p> : null}
      {body ? <p className={cn("text-control leading-6 text-ink-body", title && "mt-0.5")}>{body}</p> : null}
    </div>
  </div>;
}

function AnalysisTable({ widget }: WidgetProps) {
  const [fullWidth] = useTablesWide();
  const currency = str(widget.data.currency, "INR");
  const queryResults = Array.isArray(widget.data.queryResults) ? widget.data.queryResults as Data[] : [];
  const transforms = Array.isArray(widget.data.transforms) ? widget.data.transforms as Data[] : [];
  const context = widget.data.context && typeof widget.data.context === "object" ? widget.data.context as Record<string, unknown> : {};
  const allocationRows = Array.isArray(widget.data.rows) ? widget.data.rows as Data[] : [];
  const columns = Array.isArray(widget.data.columns) ? widget.data.columns.map(String) : [];
  const budgetRoom = Array.isArray(widget.data.budgetRoom) ? widget.data.budgetRoom as Data[] : [];
  const roomLabels = new Set(budgetRoom.map((item) => str(item.label)));
  const empty = !queryResults.length && !transforms.length && !allocationRows.length && !Object.keys(context).length;

  return <Card className={cn(fullWidth && WIDE_TABLE_BREAKOUT)}>
    <CardHeader eyebrow="Governed analysis" title={str(widget.data.title)} body={str(widget.data.body) || undefined} />
    {budgetRoom.length ? <div className="border-b border-line p-4">
      <p className="mb-2 text-meta font-semibold tracking-[0.08em] text-secondary uppercase">Below the limits you set</p>
      <ul className="flex flex-wrap gap-2">{budgetRoom.map((item, index) => <li key={str(item.label, String(index))} className="rounded-full bg-surface-sunken px-3 py-2 text-note text-ink-body">{str(item.label)} · <Money value={item.room_minor} currency={currency} className="font-semibold text-money-in" /> unspent</li>)}</ul>
    </div> : null}
    {transforms.length ? <div className="grid gap-2 border-b border-line p-4 sm:grid-cols-2">{transforms.map((transform, index) => { const values = Array.isArray(transform.values) ? transform.values as Data[] : []; return <div key={`${str(transform.name)}-${index}`} className="rounded-2xl bg-secondary-tint p-3">
      <p className="text-meta font-semibold tracking-[0.08em] text-secondary uppercase">{str(transform.operation).replaceAll("_", " ")}</p>
      <p className="mt-1 text-note font-semibold text-ink-body">{str(transform.name)}</p>
      {values.slice(0, 3).map((value, valueIndex) => <div key={str(value.label, String(valueIndex))} className="mt-2 flex gap-3 text-meta text-ink-muted"><span className="min-w-0 truncate">{formatDimension(value.label)}</span><span className="money ml-auto shrink-0 font-semibold text-ink-body">{str(transform.metric) === "transaction_count" ? formatCount(num(value.value)) : formatMoney(value.value, currency)}</span></div>)}
    </div>; })}</div> : null}
    {Object.keys(context).length ? <div className="grid gap-2 border-b border-line p-4 sm:grid-cols-2">{Object.entries(context).map(([source, rawRows]) => { const rows = Array.isArray(rawRows) ? rawRows as Data[] : []; return <div key={source} className="rounded-2xl border border-line p-3">
      <p className="text-meta font-semibold tracking-[0.08em] text-ink-muted uppercase">{source.replaceAll("_", " ")}</p>
      {rows.slice(0, 5).map((row, index) => <div key={str(row.id, String(index))} className="mt-2 flex items-center gap-2 text-meta text-ink-muted"><span className="min-w-0 truncate">{str(row.name, str(row.merchant, "Recorded item"))}</span>{row.remainingMinor != null ? <span className="ml-auto shrink-0"><Money value={row.remainingMinor} currency={str(row.currency, currency)} className="font-semibold text-ink-body" /> remaining</span> : row.balanceMinor != null ? <Money value={row.balanceMinor} currency={str(row.currency, currency)} className="ml-auto shrink-0 font-semibold text-ink-body" /> : row.principalMinor != null ? <Money value={row.principalMinor} currency={str(row.currency, currency)} className="ml-auto shrink-0 font-semibold text-ink-body" /> : null}</div>)}
      {!rows.length ? <p className="mt-2 text-meta text-ink-muted">No saved records</p> : null}
    </div>; })}</div> : null}
    {queryResults.map((result, resultIndex) => {
      const rows = Array.isArray(result.rows) ? result.rows as Data[] : [];
      const isCount = str(result.metric) === "transaction_count";
      const dimensionKeys = rows.reduce<string[]>((keys, row) => {
        Object.keys(row).filter((key) => key !== "value" && !keys.includes(key)).forEach((key) => keys.push(key));
        return keys;
      }, []);
      const table: DataTableData = {
        title: str(result.name),
        body: null,
        columns: [
          ...dimensionKeys.map((key, index) => ({ key, label: key.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()), type: "text" as const, align: "left" as const, priority: index === 0 ? "primary" as const : "secondary" as const, currencyKey: null, secondaryKeys: [] })),
          { key: "value", label: isCount ? "Transactions" : "Amount", type: isCount ? "number" : "money", align: "right", priority: "primary", currencyKey: isCount ? null : "currency", secondaryKeys: [] },
        ],
        rows: rows.map((row, index) => ({ ...row, _rowId: `${resultIndex}-${index}`, currency })),
        rowIdKey: "_rowId",
        rowActions: [],
        capabilitiesKey: "_capabilities",
        emptyMessage: "Nothing you’ve recorded matched this query.",
      };
      return <div key={`${str(result.name)}-${resultIndex}`} className="border-b border-line p-4 last:border-b-0">
        <div className="mb-3 flex flex-wrap items-end gap-3 gap-1"><p className="text-control font-semibold text-ink-body">{str(result.name)}</p><p className="text-meta text-ink-muted">{formatDay(result.start)} → {formatDay(result.end)}</p></div>
        <DataTableView data={table} embedded parentManagesWidth />
      </div>;
    })}
    {allocationRows.length ? <div className="overflow-x-auto p-4"><table className="w-full min-w-[520px] text-left text-note">
      <thead><tr className="text-ink-muted"><th scope="col" className="pb-2 font-medium">Category</th>{columns.map((column) => <th key={column} scope="col" className="pb-2 text-right font-medium">{column}</th>)}</tr></thead>
      <tbody className="divide-y divide-line">{allocationRows.map((row, index) => { const months = (row.months ?? {}) as Data; const highlighted = roomLabels.has(str(row.label)); return <tr key={str(row.id, String(index))} className={highlighted ? "bg-secondary-tint" : undefined}>
        <td className="py-3 font-medium text-ink-body">{str(row.label)}{highlighted ? <span className="ml-2 text-meta font-semibold text-secondary uppercase">room</span> : null}</td>
        {columns.map((column) => <td key={column} className="money py-3 text-right text-ink-muted">{formatMoney(months[column], currency)}</td>)}
      </tr>; })}</tbody>
    </table></div> : null}
    {empty ? <EmptyNote>This analysis ran but returned no rows. Record more transactions in this period and ask again.</EmptyNote> : null}
  </Card>;
}

function AvoidableExpenses({ widget, onAction, disabled, pending }: WidgetProps) {
  const currency = str(widget.data.currency, "INR");
  const transactions = Array.isArray(widget.data.transactions) ? widget.data.transactions as Data[] : [];
  // Each row is its own decision, so a decided row settles on its own instead of
  // taking the rest of the card down with it.
  const [decided, setDecided] = useState<Record<string, string>>({});
  const potential = widget.data.potentialMinor;

  function decide(id: string, spendNature: string) {
    const action = widget.actions.find((candidate) => candidate.payload.transactionId === id && candidate.payload.spendNature === spendNature);
    if (!action) return;
    setDecided((current) => ({ ...current, [id]: spendNature }));
    onAction(widget.id, action.action, action.payload, { markUsed: false });
  }

  return <Card className="hitl-card">
    <div className="border-b border-line px-3.5 py-3">
      <h3 className="font-heading text-body font-semibold text-ink">{str(widget.data.title)}</h3>
      {str(widget.data.body) ? <p className="mt-0.5 text-note leading-4 text-ink-muted">{str(widget.data.body)}</p> : null}
      {potential != null && transactions.length ? <p className="mt-1 text-meta text-ink-muted"><Money value={potential} currency={currency} className="font-semibold text-ink-body" /> · {transactions.length} expense{transactions.length === 1 ? "" : "s"}</p> : null}
    </div>
    <ul className="divide-y divide-line">{transactions.map((transaction, index) => {
      const id = str(transaction.id, String(index));
      const choice = decided[id];
      return <li key={id} className="p-3">
        <div className="flex items-start gap-3">
          <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-surface-sunken text-danger-ink"><ReceiptText size={14} /></span>
          <div className="min-w-0 flex-1">
            <div className="flex gap-3"><p className="min-w-0 truncate text-control font-semibold text-ink">{str(transaction.merchant, "Recorded expense")}</p><Money value={transaction.amountMinor} currency={str(transaction.currency, currency)} className="ml-auto shrink-0 text-control font-semibold text-ink" /></div>
            <p className="mt-0.5 text-meta text-ink-muted">{[transaction.category, transaction.subcategory, formatInstant(transaction.transactionAt)].filter(Boolean).map(String).join(" · ")}</p>
            <p className="mt-1.5 text-meta leading-4 text-ink-muted">{Array.isArray(transaction.reasons) && transaction.reasons.length ? transaction.reasons.join(" · ") : "Worth a second look"}</p>
            {choice ? <p className="hitl-reveal mt-2 flex items-center gap-2 text-meta font-medium text-secondary"><Check size={14} />{choice === "essential" ? "Kept as essential" : "Marked avoidable"}</p> : <div className="mt-2 flex flex-wrap gap-2">
              <Button type="button" disabled={disabled} variant="outline" size="sm" onClick={() => decide(id, "potentially_avoidable")}>Mark avoidable</Button>
              <Button type="button" disabled={disabled} variant="ghost" size="sm" onClick={() => decide(id, "essential")}>Keep — it’s essential</Button>
            </div>}
          </div>
        </div>
      </li>;
    })}</ul>
    {!transactions.length ? <EmptyNote>Nothing met the evidence threshold. Mark a few expenses as discretionary and I’ll have more to work with.</EmptyNote> : null}
    {pending ? <p className="sr-only" role="status">Saving your decision</p> : null}
  </Card>;
}

function LoanStrategy({ widget }: WidgetProps) {
  const loans = Array.isArray(widget.data.loans) ? widget.data.loans as Data[] : [];
  return <Card>
    <CardHeader eyebrow="Deterministic scenarios" title={str(widget.data.title)} body={str(widget.data.body) || undefined} />
    {loans.length ? loans.map((loan, index) => {
      const currency = str(loan.currency, "INR");
      const scenarios = Array.isArray(loan.options) ? loan.options as Data[] : [];
      return <div key={str(loan.loanId, String(index))} className="p-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-0"><p className="text-control font-semibold text-ink-body">{str(loan.name)}</p><p className="text-meta text-ink-muted">{[str(loan.lender), `${num(loan.annualRatePercent)}%`, `${num(loan.tenureMonths)} months`].filter(Boolean).join(" · ")}</p></div>
          <Money value={loan.principalMinor} currency={currency} className="ml-auto text-control font-semibold text-ink" />
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-2">{scenarios.map((scenario, scenarioIndex) => { const shorter = (scenario.shorter_tenure ?? {}) as Data; const lower = (scenario.lower_emi ?? {}) as Data; return <div key={scenarioIndex} className="rounded-2xl bg-surface-sunken p-3">
          <p className="text-meta font-semibold text-ink-muted uppercase">Prepay <Money value={scenario.prepayment_minor} currency={currency} /></p>
          <p className="mt-2 text-note leading-5 text-ink-body">Shorter tenure: save <Money value={shorter.interest_saved_minor} currency={currency} className="font-semibold" /> and {num(shorter.months_saved)} months</p>
          <p className="mt-1 text-note leading-5 text-ink-body">Lower EMI: save <Money value={lower.interest_saved_minor} currency={currency} className="font-semibold" /> interest</p>
        </div>; })}</div>
      </div>;
    }) : <EmptyNote>No active loans are saved yet.</EmptyNote>}
  </Card>;
}

/** A reasoning trace stays quiet as one line in the transcript, but unlike the
 *  old lifecycle block it expands into the actual provider-emitted thinking
 *  text rather than request/tool telemetry. */
function AgentActivity({ widget }: WidgetProps) {
  const steps = Array.isArray(widget.data.steps) ? widget.data.steps as Data[] : [];
  const total = num(widget.data.totalMs);
  const live = widget.data.live === true;
  // The persisted flag is authoritative in deployed builds. Local development
  // also upgrades older stored traces that predate the flag but already retain
  // their tool metadata.
  const debugTrace = widget.data.debugTrace === true || environment.isDevelopment;
  const broke = steps.some((step) => str(step.status) === "failed" || (!live && str(step.status) === "running"));
  const decision = [...steps].reverse().find((step) => (str(step.stageId) || str(step.id)) === "classification" && str(step.detail));
  const latest = steps.at(-1);
  const transcript = str(widget.data.reasoningTrace)
    || str(widget.data.summary)
    || str(decision?.detail)
    || str(latest?.detail)
    || str(latest?.label)
    || "Preparing a contextual answer";
  const summary = plainLine(widget.data.summary) || plainLine(transcript) || "Preparing a contextual answer";
  const expandedTranscript = plainTranscript(transcript) || summary;
  const modelPasses = steps.filter((step) => {
    const id = str(step.stageId) || str(step.id);
    const tool = str(step.tool);
    if (id === "classification" && tool === "unified_read_agent") return true;
    if (id === "response_synthesis" && /^gpt-/i.test(tool)) return true;
    return ["router", "validator", "reroute", "revalidation", "reasoning"].includes(id)
      && (tool.startsWith("agno_") || /^gpt-/i.test(tool));
  });
  const routeLabel = modelPasses.length === 0
    ? "Deterministic route"
    : modelPasses.length === 1
      ? "Single-pass route"
      : `${modelPasses.length}-pass route`;
  const trace = steps.map((step) => {
    const label = (plainLine(step.label) || plainLine(step.id)).replace(/[.!?]+$/, "");
    const detail = plainLine(step.detail).replace(/[.!?]+$/, "");
    const stage = str(step.stageId) || str(step.id);
    const tool = str(step.tool).trim();
    const resultTool = str(step.resultTool).trim();
    const duration = formatDuration(step.durationMs);
    const cumulative = formatDuration(step.cumulativeMs);
    const status = str(step.status);
    return { label, detail, stage, tool, resultTool, duration, cumulative, status };
  });
  const [open, setOpen] = useState(false);
  const detailsId = useId();
  const activityLabel = broke
    ? `Agent run failed: ${routeLabel}`
    : live
      ? `Agent run in progress: ${routeLabel}`
      : `Agent run complete: ${routeLabel}${total > 0 ? `, ${formatDuration(total)}` : ""}`;

  return <div aria-live={live ? "polite" : undefined} className="-ml-1.5 min-w-0">
    <button
      type="button"
      onClick={() => setOpen((current) => !current)}
      aria-label={activityLabel}
      aria-expanded={open}
      aria-controls={detailsId}
      className={cn("flex min-h-8 w-full min-w-0 items-center gap-2 rounded-lg px-2 text-left text-meta font-medium leading-5 transition-colors hover:bg-surface-sunken", broke ? "text-danger-ink" : "text-ink-muted")}
    >
      {broke ? <TriangleAlert size={14} className="shrink-0" /> : null}
      <span className="min-w-0 flex-1 truncate">{broke ? "This run hit a problem" : summary}</span>
      <span className="shrink-0 font-normal text-ink-muted/80">{routeLabel}</span>
      {total > 0 ? <span className="money ml-auto shrink-0 font-normal text-ink-muted/80">{formatDuration(total)}</span> : null}
      <ChevronDown size={14} className={cn("shrink-0 transition-transform duration-300 motion-reduce:transition-none", open && "rotate-180")} />
    </button>
    <div
      className={cn(
        "grid transition-[grid-template-rows,opacity] duration-300 ease-out motion-reduce:transition-none",
        open ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
      )}
    >
      <div id={detailsId} data-testid="agent-activity-details" aria-hidden={!open} className="overflow-hidden">
        <div className="px-2 pb-2 pt-1 text-meta leading-5 text-ink-muted">
          <p className="whitespace-pre-wrap">{expandedTranscript}</p>
          <div className="mt-3 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="font-semibold text-ink-body">Execution trace</span>
            <span>{routeLabel}</span>
          </div>
          {trace.length ? <ol className="mt-2 space-y-2.5" aria-label="Complete execution trace">
            {trace.map((step, index) => <li key={`${step.stage}-${index}`} className="grid grid-cols-[1.25rem_minmax(0,1fr)] gap-x-1.5">
              <span className="money text-ink-muted/70">{index + 1}.</span>
              <div className="min-w-0">
                <p><span className="font-semibold text-ink-body">{step.label}</span></p>
                {step.detail ? <p>{step.detail}</p> : null}
                {debugTrace && (step.stage || step.tool || step.resultTool) ? <p className="break-words">
                  {step.stage ? <><span className="text-ink-muted/80">Stage</span> <span className="font-mono text-secondary">{step.stage}</span></> : null}
                  {step.tool ? <>{step.stage ? <span> · </span> : null}<span className="text-ink-muted/80">Tool</span> <span className="font-mono text-secondary">{step.tool}</span></> : null}
                  {step.resultTool ? <>{step.stage || step.tool ? <span> · </span> : null}<span className="text-ink-muted/80">Output</span> <span className="font-mono text-secondary">{step.resultTool}</span></> : null}
                </p> : null}
                <p>
                  {step.status === "running" ? <span className="font-medium text-ink-body">Running</span> : step.status === "failed" ? <><span className="font-medium text-danger-ink">Failed after <span className="money">{step.duration}</span></span></> : <><span className="money font-medium text-ink-body">{step.duration}</span> step</>}
                  <span> · </span><span className="money font-medium text-ink-body">{step.cumulative}</span> total elapsed
                </p>
              </div>
            </li>)}
          </ol> : <p className="mt-2">No additional execution stages were recorded.</p>}
        </div>
      </div>
    </div>
  </div>;
}

function GenericWidget({ widget, onAction, disabled, pending }: WidgetProps) {
  return <Card>
    <div className="flex items-center gap-3 p-4">
      <span className="grid size-10 shrink-0 place-items-center rounded-2xl bg-surface-sunken text-secondary"><Landmark size={20} /></span>
      <div className="min-w-0"><h3 className="font-heading text-body font-semibold capitalize text-ink">{str(widget.data.title, widget.type.replaceAll("_", " "))}</h3><p className="mt-1 text-note leading-5 text-ink-muted">{str(widget.data.body, "This structured financial view is ready for review.")}</p></div>
    </div>
    <ActionRow widget={widget} disabled={disabled} pending={pending} onAction={onAction} />
  </Card>;
}

/** Registered renderers are the frontend half of the widget library. Adding a
 * business widget is an explicit registry operation, never model-written JSX. */
export const widgetRegistry: Partial<Record<Widget["type"], ComponentType<WidgetProps>>> = Object.freeze({
  agent_activity: AgentActivity,
  clarification: Clarification,
  category_selector: CategorySelector,
  taxonomy_editor: TaxonomyEditor,
  transaction_type_selector: Selector,
  subcategory_selector: Selector,
  account_selector: Selector,
  confirmation_card: Confirmation,
  transaction_preview: TransactionPreview,
  transaction_edit: TransactionEdit,
  financial_summary: FinancialSummary,
  data_chart: DataChart,
  data_visualization: DataVisualization,
  analysis_table: AnalysisTable,
  avoidable_expenses: AvoidableExpenses,
  scenario_analysis: Scenario,
  budget_progress: ProgressCard,
  goal_progress: ProgressCard,
  import_review: ImportReview,
  loan_calculator: LoanCalculator,
  loan_strategy: LoanStrategy,
  investment_projection: InvestmentProjection,
  reconciliation_review: ReconciliationReview,
  data_table: DynamicDataTable,
  transaction_list: TransactionList,
  insight_card: Insight,
});

/** Decisions collapse once they are recorded. Keeping a disabled form or a
 *  grid of dead options in the transcript makes history read like an unfinished
 *  task; the receipt preserves the outcome in a single, scannable line. */
const compactResolvedWidgets = new Set<Widget["type"]>([
  widgetTypeIds.clarification,
  widgetTypeIds.category_selector,
  widgetTypeIds.transaction_type_selector,
  widgetTypeIds.subcategory_selector,
  widgetTypeIds.taxonomy_editor,
  widgetTypeIds.account_selector,
  widgetTypeIds.confirmation_card,
  widgetTypeIds.transaction_preview,
  widgetTypeIds.transaction_edit,
  widgetTypeIds.transaction_list,
  widgetTypeIds.data_table,
  widgetTypeIds.budget_progress,
  widgetTypeIds.goal_progress,
  widgetTypeIds.reconciliation_review,
  widgetTypeIds.import_review,
]);

function completedChoice(widget: Widget) {
  const values = completionValues(widget);
  if (str(values.customText)) return str(values.customText);
  if (str(values.accountName)) return str(values.accountName);
  if (str(values.name)) return str(values.name);
  const selected = [values.categoryId, values.subcategoryId, values.accountId, values.optionId, values.transactionType].map((value) => str(value)).find(Boolean);
  if (!selected) return "";
  const candidates = [
    ...(Array.isArray(widget.data.suggestions) ? widget.data.suggestions as Data[] : []),
    ...(Array.isArray(widget.data.options) ? widget.data.options as Data[] : []),
  ];
  const match = candidates.find((candidate) => [candidate.id, candidate.categoryId, candidate.subcategoryId, candidate.accountId, candidate.value, candidate.transactionType].map((value) => str(value)).includes(selected));
  return match ? str(match.label ?? match.name, selected.replaceAll("_", " ")) : selected.replaceAll("_", " ");
}

function completionSummary(widget: Widget) {
  const completion = widget.data.completion && typeof widget.data.completion === "object" ? widget.data.completion as Data : {};
  const action = str(completion.action);
  const choice = completedChoice(widget);
  const selectionActions = new Set<string>([widgetActionIds.select_category, widgetActionIds.select_subcategory, widgetActionIds.select_account, widgetActionIds.select_transaction_type, widgetActionIds.resolve_clarification]);
  const creationActions = new Set<string>([widgetActionIds.create_category, widgetActionIds.create_subcategory]);
  if (selectionActions.has(action)) return { status: "Selected", detail: choice };
  if (creationActions.has(action)) return { status: "Added", detail: choice };
  if (action === widgetActionIds.commit_transaction) return { status: "Saved", detail: "" };
  if (action === widgetActionIds.update_saved_transaction || action === widgetActionIds.update_transaction_draft) return { status: "Updated", detail: "" };
  if (action === widgetActionIds.edit_transaction || action === widgetActionIds.edit_saved_transaction) return { status: "Editing", detail: "" };
  if (action === widgetActionIds.confirm_remove_transaction) return { status: "Removed", detail: "" };
  if (action === widgetActionIds.merge_reconciliation) return { status: "Merged", detail: "" };
  if (action === widgetActionIds.separate_reconciliation) return { status: "Kept separate", detail: "" };
  if (action === widgetActionIds.commit_import) return { status: "Imported", detail: "" };
  if (action === widgetActionIds.save_budget) return { status: "Budget saved", detail: "" };
  if (action === widgetActionIds.save_goal) return { status: "Goal saved", detail: "" };
  if (action === widgetActionIds.contribute_goal) return { status: "Contribution saved", detail: "" };
  return { status: "Done", detail: choice };
}

function HitlReceipt({ widget, lifecycle }: { widget: Widget; lifecycle: "completed" | "cancelled" }) {
  const cancelled = lifecycle === "cancelled";
  const summary = cancelled ? { status: "Cancelled", detail: "" } : completionSummary(widget);
  return <div role="status" className={cn("hitl-receipt widget-enter", cancelled && "hitl-receipt-cancelled")}>
    <span aria-hidden className="hitl-receipt-mark">{cancelled ? <X size={12} strokeWidth={2.5} /> : <Check size={12} strokeWidth={3} />}</span>
    <span className="font-medium text-ink-body">{summary.status}</span>
    {summary.detail ? <><span aria-hidden className="text-line-strong">·</span><span className="min-w-0 truncate text-ink-muted">{summary.detail}</span></> : null}
  </div>;
}

/** Memoised because a widget is expensive to draw and almost never changes: its
 *  payload is frozen once the turn is recorded, so the only reasons to redraw
 *  are the lock flags and the handler beside it. The transcript keeps those
 *  stable, so a widget that is not being interacted with stays put. */
export const WidgetRenderer = memo(function WidgetRenderer(props: WidgetProps) {
  const Renderer = widgetRegistry[props.widget.type] ?? GenericWidget;
  const lifecycle = str(props.widget.data.lifecycle, "pending");
  const resolved = lifecycle === "completed" || lifecycle === "cancelled";
  if (resolved && compactResolvedWidgets.has(props.widget.type)) return <HitlReceipt widget={props.widget} lifecycle={lifecycle} />;
  const rendererProps = resolved ? { ...props, disabled: true } : props;
  const readonly = Boolean(rendererProps.disabled && props.widget.type !== widgetTypeIds.agent_activity);
  // Keep read-only tables scrollable and selectable. Individual controls still
  // receive `disabled`, and the transcript-level action guard rejects stale
  // actions even if a renderer accidentally omits a disabled attribute.
  return <div aria-disabled={readonly || undefined} className={cn(readonly && "widget-readonly")}>
    <Renderer {...rendererProps} />
  </div>;
});
