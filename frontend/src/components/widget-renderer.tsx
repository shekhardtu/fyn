import { CalendarDays, Check, ChevronDown, CircleEllipsis, Info, Landmark, Loader2, PencilLine, Plus, ReceiptText, Search, Target, Trash2, TriangleAlert, Utensils, WalletCards, X } from "lucide-react";
import { FormEvent, memo, useEffect, useId, useMemo, useRef, useState, type ComponentType } from "react";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Progress } from "@/components/ui/progress";
import { ChartView } from "@/components/widget-library/chart";
import { formatDimension, formatDuration, formatInstant, formatMoney, formatTransactionClassification, parseAmountToMinor, parseNumber, timestampInputToUtc, timestampInputValue } from "@/lib/format";
import { dataChartDataSchema, editableTransactionTypes, widgetActionIds, widgetTypeIds, type AgentRunMetrics, type CategoryDirectoryOut, type CategoryDirectorySubcategoryOut, type Widget, type WidgetActionId } from "@/lib/protocol";
import { cn } from "@/lib/utils";

type Primitive = string | number | boolean | null | undefined;
type Data = Record<string, unknown>;

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
function tracePayload(value: unknown) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
function options(data: Data) { return Array.isArray(data.options) ? data.options as Array<Record<string, Primitive>> : []; }
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

/**
 * Native `autoFocus` runs before a newly mounted virtual transcript row has
 * received its final transform. Focusing from an effect runs after that layout
 * work, and `preventScroll` keeps keyboard focus from moving the transcript.
 */
function useHitlAutofocus<T extends HTMLElement>(enabled: boolean) {
  const ref = useRef<T>(null);
  useEffect(() => {
    if (enabled) ref.current?.focus({ preventScroll: true });
  }, [enabled]);
  return ref;
}

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
  /** Taxonomy mutations are supplied by the conversation shell, matching the
   *  standalone transaction editor's dependency boundary. */
  onCreateCategory?: (name: string) => Promise<CategoryDirectoryOut>;
  onCreateSubcategory?: (categoryId: string, name: string) => Promise<CategoryDirectorySubcategoryOut>;
  /** Posts text as a new user message through the composer's own guards.
   *  Used by suggestion widgets; absent in read-only render contexts. */
  onPostPrompt?: (text: string) => void;
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
  const [customOpen, setCustomOpen] = useState(listed.length === 0 || Boolean(completedValues.customText));
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
    if (customOpen && !disabled && !pending) customInput.current?.focus({ preventScroll: true });
  }, [customOpen, disabled, pending]);
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
      <button type="button" data-inline-disclosure="true" aria-expanded={customOpen} disabled={disabled || pending} onClick={() => setCustomOpen((open) => !open)} className="hitl-disclosure">
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
  const accountInput = useHitlAutofocus<HTMLInputElement>(accountOpen && !disabled && !pending);
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
      {list.length ? <button type="button" data-inline-disclosure="true" aria-expanded={accountOpen} disabled={disabled || pending} onClick={() => setAccountOpen((open) => !open)} className="hitl-disclosure"><PencilLine size={14} />Use another account<ChevronDown size={14} className={cn("ml-auto transition-transform duration-[var(--m-state)]", accountOpen && "rotate-180")} /></button> : null}
      {accountOpen ? <form onSubmit={submitAccount} className="hitl-reveal flex flex-col items-stretch gap-2 p-3 sm:flex-row">
        <input ref={accountInput} value={accountName} disabled={disabled || pending} maxLength={120} onChange={(event) => setAccountName(event.target.value)} className={inputClass} placeholder="Account name" aria-label="Account name" />
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
  const newCategoryInput = useHitlAutofocus<HTMLInputElement>(widget.data.mode === "create" && !disabled && !pending);
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
      <label className="block"><FieldLabel>Category name</FieldLabel><input ref={newCategoryInput} disabled={disabled || pending} aria-label="New category name" value={newCategory} onChange={(event) => setNewCategory(event.target.value)} placeholder="e.g. Pets" maxLength={80} className={inputClass} /></label>
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
  const initialSubcategories = Array.isArray(widget.data.subcategories) ? widget.data.subcategories.map(String) : [];
  const [subcategories, setSubcategories] = useState(initialSubcategories.join(", "));
  const [submittedAction, markSubmitted] = usePendingAction(pending);
  const operation = str(widget.data.operation);
  const isSubcategory = operation === widgetActionIds.create_subcategory;
  const isTaxonomyPath = operation === widgetActionIds.create_taxonomy_path;
  const lifecycle = str(widget.data.lifecycle, "pending");
  const resolved = lifecycle === "completed" || lifecycle === "cancelled";
  const nameInput = useHitlAutofocus<HTMLInputElement>(!disabled && !pending && !resolved);
  const submitAction = isTaxonomyPath ? widgetActionIds.create_taxonomy_path : isSubcategory ? widgetActionIds.create_subcategory : widgetActionIds.create_category;
  const declaredAction = widget.actions[0];
  const basePayload = declaredAction?.payload ?? {};
  const navigationActions = ensureDraftCancel(
    widget,
    widget.actions.filter((action) => action.id !== declaredAction?.id),
    disabled && !pending,
  );
  function submit(event: FormEvent) {
    event.preventDefault();
    const childNames = subcategories.split(",").map((item) => item.trim()).filter(Boolean);
    if (name.trim() && (!isTaxonomyPath || childNames.length)) {
      markSubmitted(declaredAction?.id ?? "create");
      onAction(widget.id, submitAction, {
        ...basePayload,
        name: name.trim(),
        ...(isTaxonomyPath ? { subcategories: childNames } : {}),
      });
    }
  }
  return <Card className="hitl-card"><form onSubmit={submit} className="space-y-3 p-3">
    <label className="block"><FieldLabel hint={isSubcategory && widget.data.parentCategory ? `under ${str(widget.data.parentCategory)}` : undefined}>{isSubcategory ? "Subcategory name" : "Category name"}</FieldLabel><input ref={nameInput} disabled={disabled || pending || resolved} aria-label={isSubcategory ? "New subcategory name" : "New category name"} value={name} onChange={(event) => setName(event.target.value)} placeholder={isSubcategory ? "e.g. Materials" : "e.g. Pets"} maxLength={80} className={inputClass} /></label>
    {isTaxonomyPath ? <label className="block"><FieldLabel hint="comma separated">Subcategories</FieldLabel><input disabled={disabled || pending || resolved} aria-label="New subcategory names" value={subcategories} onChange={(event) => setSubcategories(event.target.value)} placeholder="e.g. Vet, Food, Grooming" className={inputClass} /></label> : null}
    {!resolved ? <HitlActions className="-mx-3 -mb-3 border-t border-line">
      {orderedActions(navigationActions).map((action) => <ActionButton key={action.id} action={action} pending={pending && submittedAction === action.id} disabled={disabled || pending} onClick={() => { markSubmitted(action.id); onAction(widget.id, action.action, action.payload); }} />)}
      <Button type="submit" disabled={disabled || pending || !name.trim() || (isTaxonomyPath && !subcategories.trim())}>{pending && submittedAction === (declaredAction?.id ?? "create") ? <Loader2 className="animate-spin" /> : <Plus />} {isTaxonomyPath ? "Add category and subcategories" : isSubcategory ? "Add subcategory" : "Add category"}</Button>
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

function OperationForm({ widget, onAction, disabled, pending }: WidgetProps) {
  const schema = widget.data.inputSchema && typeof widget.data.inputSchema === "object" ? widget.data.inputSchema as Data : {};
  const properties = schema.properties && typeof schema.properties === "object" ? schema.properties as Record<string, Data> : {};
  const required = new Set(Array.isArray(schema.required) ? schema.required.map(String) : []);
  const initial = widget.data.inputs && typeof widget.data.inputs === "object" ? widget.data.inputs as Data : {};
  const [values, setValues] = useState<Data>(initial);
  const submitAction = widget.actions.find((action) => action.action === widgetActionIds.submit_operation);
  const navigation = widget.actions.filter((action) => action.action !== widgetActionIds.submit_operation);
  const [submitted, markSubmitted] = usePendingAction(pending);
  const missing = [...required].filter((key) => values[key] === undefined || values[key] === "" || (Array.isArray(values[key]) && !(values[key] as unknown[]).length));

  function update(key: string, value: unknown) {
    setValues((current) => ({ ...current, [key]: value }));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!submitAction || missing.length) return;
    const normalized = Object.fromEntries(Object.entries(values).map(([key, value]) => {
      const field = properties[key] ?? {};
      if (field.type === "array" && typeof value === "string") return [key, value.split(",").map((item) => item.trim()).filter(Boolean)];
      if ((field.type === "integer" || field.type === "number") && typeof value === "string") return [key, field.type === "integer" ? Number.parseInt(value, 10) : Number(value)];
      return [key, value];
    }));
    markSubmitted(submitAction.id);
    onAction(widget.id, submitAction.action, { ...submitAction.payload, inputs: normalized });
  }

  return <Card className="hitl-card">
    <CardHeader title={str(widget.data.title)} body={str(widget.data.body) || undefined} />
    <form onSubmit={submit} className="space-y-3 p-3">
      {Object.entries(properties).map(([key, field]) => {
        const label = str(field.title, formatEnumLabel(key));
        const value = values[key];
        const choices = Array.isArray(field.enum) ? field.enum.map(String) : [];
        if (field.type === "boolean") return <label key={key} className="flex items-center gap-2 text-note text-ink-body"><input type="checkbox" checked={value === true} disabled={disabled || pending} onChange={(event) => update(key, event.target.checked)} />{label}</label>;
        if (choices.length) return <label key={key} className="block"><FieldLabel>{label}</FieldLabel><select aria-label={label} required={required.has(key)} disabled={disabled || pending} value={str(value)} onChange={(event) => update(key, event.target.value)} className={inputClass}><option value="">Choose…</option>{choices.map((choice) => <option key={choice} value={choice}>{formatEnumLabel(choice)}</option>)}</select></label>;
        const type = field.format === "date" ? "date" : field.format === "date-time" ? "datetime-local" : field.type === "integer" || field.type === "number" ? "number" : "text";
        const shown = Array.isArray(value) ? value.join(", ") : value == null ? "" : String(value);
        return <label key={key} className="block"><FieldLabel hint={field.type === "array" ? "comma separated" : undefined}>{label}</FieldLabel><input aria-label={label} required={required.has(key)} disabled={disabled || pending} type={type} value={shown} min={typeof field.minimum === "number" ? field.minimum : undefined} max={typeof field.maximum === "number" ? field.maximum : undefined} minLength={typeof field.minLength === "number" ? field.minLength : undefined} maxLength={typeof field.maxLength === "number" ? field.maxLength : undefined} onChange={(event) => update(key, event.target.value)} className={inputClass} /></label>;
      })}
      <HitlActions className="-mx-3 -mb-3 border-t border-line">
        {orderedActions(navigation).map((action) => <ActionButton key={action.id} action={action} pending={pending && submitted === action.id} disabled={disabled || pending} onClick={() => { markSubmitted(action.id); onAction(widget.id, action.action, action.payload); }} />)}
        {submitAction ? <Button type="submit" disabled={disabled || pending || Boolean(missing.length)}>{pending && submitted === submitAction.id ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />} {submitAction.label}</Button> : null}
      </HitlActions>
    </form>
  </Card>;
}

function OperationApproval({ widget, onAction, disabled, pending }: WidgetProps) {
  const inputs = widget.data.inputs && typeof widget.data.inputs === "object" ? widget.data.inputs as Data : {};
  return <Card className="hitl-card">
    <CardHeader eyebrow={str(widget.data.effect) === "mutation" ? "Will change your data" : "Review"} title={str(widget.data.title)} body={str(widget.data.body) || str(widget.data.summary)} tone={str(widget.data.effect) === "mutation" ? "caution" : "neutral"} />
    {Object.keys(inputs).length ? <dl className="divide-y divide-line px-3.5">{Object.entries(inputs).map(([key, value]) => <div key={key} className="flex gap-4 py-2.5 text-note"><dt className="text-ink-muted">{formatEnumLabel(key)}</dt><dd className="ml-auto text-right font-medium text-ink-body">{Array.isArray(value) ? value.join(", ") : String(value)}</dd></div>)}</dl> : null}
    <ActionRow widget={widget} disabled={disabled} pending={pending} onAction={onAction} icons={{ [widgetActionIds.approve_operation]: <Check size={14} /> }} />
  </Card>;
}

function TransactionEdit({ widget, onAction, onCreateCategory, onCreateSubcategory, disabled, pending }: WidgetProps) {
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
  const amountInput = useHitlAutofocus<HTMLInputElement>(completing && !disabled && !pending);
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
  const [categories, setCategories] = useState<Data[]>(Array.isArray(widget.data.categories) ? widget.data.categories as Data[] : []);
  const [allSubcategories, setAllSubcategories] = useState<Data[]>(Array.isArray(widget.data.subcategories) ? widget.data.subcategories as Data[] : []);
  const [taxonomyPending, setTaxonomyPending] = useState<"category" | "subcategory" | null>(null);
  const [taxonomyError, setTaxonomyError] = useState<string | null>(null);
  const subcategories = allSubcategories.filter((item) => str(item.categoryId) === categoryId);
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

  async function addCategory(name: string) {
    if (!onCreateCategory) return;
    setTaxonomyError(null);
    setTaxonomyPending("category");
    try {
      const created = await onCreateCategory(name);
      setCategories((current) => current.some((item) => str(item.id) === created.id)
        ? current
        : [...current, { id: created.id, label: created.label }]);
      setAllSubcategories((current) => {
        const known = new Set(current.map((item) => str(item.id)));
        return [
          ...current,
          ...created.subcategories
            .filter((item) => !known.has(item.id))
            .map((item) => ({ id: item.id, categoryId: created.id, label: item.label })),
        ];
      });
      setCategoryId(created.id);
      setSubcategoryId("");
    } catch (cause) {
      setTaxonomyError(cause instanceof Error ? cause.message : "That category could not be added. Try again.");
    } finally {
      setTaxonomyPending(null);
    }
  }

  async function addSubcategory(name: string) {
    if (!categoryId || !onCreateSubcategory) return;
    setTaxonomyError(null);
    setTaxonomyPending("subcategory");
    try {
      const created = await onCreateSubcategory(categoryId, name);
      setAllSubcategories((current) => current.some((item) => str(item.id) === created.id)
        ? current
        : [...current, { id: created.id, categoryId, label: created.label }]);
      setSubcategoryId(created.id);
    } catch (cause) {
      setTaxonomyError(cause instanceof Error ? cause.message : "That subcategory could not be added. Try again.");
    } finally {
      setTaxonomyPending(null);
    }
  }

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
    {taxonomyError ? <p role="alert" className="rounded-lg border border-danger-line bg-danger-tint px-3 py-2 text-note text-danger-ink">{taxonomyError}</p> : null}
    {taxonomyPending ? <p role="status" className="flex items-center gap-2 text-note text-ink-muted"><Loader2 size={14} className="animate-spin" />Adding {taxonomyPending}…</p> : null}
    <div className="grid gap-3 sm:grid-cols-2">
      <label className="block"><FieldLabel>Amount</FieldLabel><input ref={amountInput} disabled={disabled || pending} aria-label="Transaction amount" aria-invalid={Boolean(amountError)} aria-describedby={amountError ? `${widget.id}-amount-error` : undefined} inputMode="decimal" value={amount} onChange={(event) => { setAmount(event.target.value); if (amountError) setAmountError(null); }} placeholder="1,500" className={cn(inputClass, amountError && invalidClass)} />{amountError ? <span id={`${widget.id}-amount-error`}><FieldError>{amountError}</FieldError></span> : null}</label>
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
        <div><FieldLabel>Category</FieldLabel><Combobox aria-label="Transaction category" disabled={disabled || pending || Boolean(taxonomyPending)} value={categoryId} onValueChange={(next) => { setCategoryId(next); setSubcategoryId(""); setTaxonomyError(null); }} placeholder="Choose category" options={categories.map((item) => ({ value: str(item.id), label: str(item.label) }))} searchPlaceholder="Search or add new" onCreate={onCreateCategory ? (name) => void addCategory(name) : undefined} createHint="New category" triggerClassName="text-body" /></div>
        <div><FieldLabel>Subcategory</FieldLabel><Combobox aria-label="Transaction subcategory" disabled={disabled || pending || Boolean(taxonomyPending) || !categoryId} value={subcategoryId} onValueChange={(next) => { setSubcategoryId(next); setTaxonomyError(null); }} placeholder={categoryId ? "Choose subcategory" : "Choose a category first"} options={subcategories.map((item) => ({ value: str(item.id), label: str(item.label) }))} searchPlaceholder="Search or add new" onCreate={onCreateSubcategory ? (name) => void addSubcategory(name) : undefined} createHint={`New in ${categories.find((item) => str(item.id) === categoryId)?.label ?? "this category"}`} triggerClassName="text-body" /></div>
      </> : null}
    </div>
    <HitlActions className="-mx-3 -mb-3 border-t border-line">
      {orderedActions(navigationActions).map((action) => <ActionButton key={action.id} action={action} pending={pending && submittedAction === action.id} disabled={disabled || pending || Boolean(taxonomyPending)} onClick={() => { markSubmitted(action.id); onAction(widget.id, action.action, action.payload); }} />)}
      <Button type="submit" disabled={disabled || pending || Boolean(taxonomyPending) || !amount.trim() || (needsCategory && (!categoryId || !subcategoryId))}>{pending && submittedAction === "submit" ? <Loader2 className="animate-spin" /> : null}{completing ? "Save entry" : "Apply changes"}</Button>
    </HitlActions>
  </form></Card>;
}

/** Charts are decoration for anyone who can't see them; the same numbers are
 *  always present as text, so the legend is the accessible source of truth. */
function ProgressCard({ widget, onAction, onCancel, disabled, pending }: WidgetProps) {
  const isGoal = widget.type === widgetTypeIds.goal_progress;
  const currency = str(widget.data.currency, "INR");
  const current = num(isGoal ? widget.data.currentMinor : widget.data.spentMinor);
  const total = num(isGoal ? widget.data.targetMinor : widget.data.amountMinor);
  const saveBudget = !isGoal ? widget.actions.find((action) => action.action === widgetActionIds.save_budget) : undefined;
  const navigationActions = saveBudget ? widget.actions.filter((action) => action.id !== saveBudget.id) : [];
  const initialAmount = String(total / 100).replace(/\.0+$/, "");
  const [amount, setAmount] = useState(initialAmount);
  const budgetAmountInput = useHitlAutofocus<HTMLInputElement>(Boolean(saveBudget) && !disabled && !pending);
  const [amountError, setAmountError] = useState<string | null>(null);
  const [submitted, markSubmitted] = usePendingAction(pending);
  const ratio = total ? current / total * 100 : 0;
  const progress = Math.max(0, Math.min(100, ratio));
  // Spending past a budget is the one thing this card exists to warn about.
  const over = !isGoal && current > total && total > 0;
  const remainder = over ? current - total : num(widget.data.remainingMinor);
  const summary = <>
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
  </>;
  if (!saveBudget) return <Card className={cn("hitl-card", over && "border-danger-line")}>
    {summary}
    <ActionRow widget={widget} disabled={disabled} pending={pending} onAction={onAction} onCancel={onCancel} />
  </Card>;

  const submitBudget = (event: FormEvent) => {
    event.preventDefault();
    const amountMinor = parseAmountToMinor(amount);
    if (!amountMinor || amountMinor <= 0) {
      setAmountError("Enter a monthly amount greater than zero.");
      return;
    }
    setAmountError(null);
    markSubmitted(saveBudget.id);
    onAction(widget.id, saveBudget.action, { ...saveBudget.payload, amountMinor });
  };

  return <Card className={cn("hitl-card", over && "border-danger-line")}><form onSubmit={submitBudget} noValidate>
    {summary}
    <div className="border-t border-line px-3 py-3">
      <label className="block"><FieldLabel>Monthly limit</FieldLabel><input ref={budgetAmountInput} disabled={disabled || pending} aria-label="Monthly budget amount" aria-invalid={Boolean(amountError)} inputMode="decimal" value={amount} onChange={(event) => { setAmount(event.target.value); if (amountError) setAmountError(null); }} className={cn(inputClass, amountError && invalidClass)} />{amountError ? <FieldError>{amountError}</FieldError> : null}</label>
    </div>
    <HitlActions className="border-t border-line">
      {orderedActions(navigationActions).map((action) => <ActionButton key={action.id} action={action} pending={pending && submitted === action.id} disabled={disabled || pending} onClick={() => { markSubmitted(action.id); onAction(widget.id, action.action, action.payload); }} />)}
      <Button type="submit" disabled={disabled || pending || !amount.trim()}>{pending && submitted === saveBudget.id ? <Loader2 className="animate-spin" /> : null}{saveBudget.label}</Button>
    </HitlActions>
  </form></Card>;
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
      <p className="mt-0.5 text-note leading-4 text-ink-muted">{replay ? (complete ? "Already imported—nothing was duplicated." : "This statement is already staged—nothing was duplicated.") : complete ? `${ready} recorded${review ? ` · ${review} need review` : ""}.` : `${total} row${total === 1 ? "" : "s"} ready to review before import.`}</p>
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

function ChartWidget({ widget, disabled, pending }: WidgetProps) {
  const parsed = useMemo(() => dataChartDataSchema.safeParse(widget.data), [widget.data]);
  if (!parsed.success) return <Card><CardHeader title="This chart could not be rendered" body="The widget payload did not match the registered chart contract." /></Card>;
  return <ChartView data={parsed.data} disabled={disabled} pending={pending} />;
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
function Insight({ widget, onAction, disabled, pending }: WidgetProps) {
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
    {widget.actions.length ? <ActionRow widget={widget} disabled={disabled} pending={pending} onAction={onAction} /> : null}
  </div>;

  return <div className="widget-enter flex max-w-[62ch] gap-2 border-l-2 border-danger-line pl-3">
    <Info size={15} className="mt-0.5 shrink-0 text-danger" />
    <div className="min-w-0">
      {title ? <p className="text-control font-semibold text-danger-ink">{title}</p> : null}
      {body ? <p className={cn("text-control leading-6 text-ink-body", title && "mt-0.5")}>{body}</p> : null}
    </div>
  </div>;
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

function TracePayloadDisclosure({ label, payload }: { label: string; payload: string }) {
  const [open, setOpen] = useState(false);
  const contentId = useId();
  function collapseFromTranscript() {
    const selection = typeof window === "undefined" ? null : window.getSelection();
    if (selection && !selection.isCollapsed) return;
    setOpen(false);
  }
  return <div>
    <button
      type="button"
      data-inline-disclosure="true"
      aria-expanded={open}
      aria-controls={contentId}
      onClick={() => setOpen((current) => !current)}
      className="flex items-center gap-0.5 font-semibold text-ink-body"
    >
      <ChevronDown size={12} className={cn("shrink-0 transition-transform duration-[var(--m-state)] motion-reduce:transition-none", !open && "-rotate-90")} />
      <span>{label}</span>
    </button>
    <div className={cn(
      "grid transition-[grid-template-rows,opacity] duration-[var(--m-enter)] ease-out motion-reduce:transition-none",
      open ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
    )}>
      <div id={contentId} aria-hidden={!open} className="overflow-hidden">
        <pre
          onClick={collapseFromTranscript}
          title="Click to collapse; drag to select text"
          className="cursor-text select-text whitespace-pre-wrap break-words pt-1 pl-3 font-mono text-[11px] leading-4 text-ink-muted"
        >{payload}</pre>
      </div>
    </div>
  </div>;
}

function AgentActivity({ widget }: WidgetProps) {
  const steps = Array.isArray(widget.data.steps) ? widget.data.steps as Data[] : [];
  const total = num(widget.data.totalMs);
  const live = widget.data.live === true;
  const debugTrace = widget.data.debugTrace === true;
  const broke = steps.some((step) => str(step.status) === "failed");
  // `summary`, `modelPassCount` and `debugTrace` are server-authored: stored
  // widgets carry the terminal values (migration 0027 upgraded older traces),
  // and the live card assembles them from streamed run aggregates. A failure
  // line keeps its exact characters — identifiers like query_presence read as
  // markdown to plainLine.
  const summary = (broke ? str(widget.data.summary).replace(/\s+/g, " ").trim() : plainLine(widget.data.summary))
    || "Preparing a contextual answer";
  const expandedTranscript = broke
    ? summary
    : plainTranscript(str(widget.data.reasoningTrace)) || summary;
  const storedPassCount = widget.data.modelPassCount;
  const modelPassCount = typeof storedPassCount === "number" && Number.isFinite(storedPassCount)
    ? Math.max(0, Math.trunc(storedPassCount))
    : 0;
  const routeLabel = modelPassCount === 0
    ? "Deterministic"
    : modelPassCount === 1
      ? "Single model pass"
      : `${modelPassCount} model passes`;
  const metrics = widget.data.metrics as AgentRunMetrics | null | undefined;
  const metricSummary = metrics && metrics.modelPasses > 0
    ? [
        `${metrics.totalTokens.toLocaleString()} tokens (${metrics.inputTokens.toLocaleString()} in / ${metrics.outputTokens.toLocaleString()} out)`,
        metrics.modelDurationMs !== null ? `${formatDuration(metrics.modelDurationMs)} model time` : null,
        metrics.firstModelTimeToFirstTokenMs !== null ? `${formatDuration(metrics.firstModelTimeToFirstTokenMs)} first model token` : null,
        metrics.costUsd !== null
          ? `$${metrics.costUsd.toFixed(6)} provider cost`
          : `provider cost unavailable (${Math.round(metrics.costCoverage * 100)}% coverage)`,
      ].filter(Boolean).join(" · ")
    : "";
  const trace = steps.map((step) => {
    const label = (plainLine(step.label) || plainLine(step.id)).replace(/[.!?]+$/, "");
    const detail = plainLine(step.detail).replace(/[.!?]+$/, "");
    const stage = str(step.stageId) || str(step.id);
    const tool = str(step.tool).trim();
    const resultTool = str(step.resultTool).trim();
    const duration = formatDuration(step.durationMs);
    const cumulative = formatDuration(step.cumulativeMs);
    const status = str(step.status);
    const input = tracePayload(step.input);
    const output = tracePayload(step.output);
    return { label, detail, stage, tool, resultTool, duration, cumulative, status, input, output };
  });
  const [open, setOpen] = useState(false);
  const detailsId = useId();
  const activityLabel = broke
    ? `Agent run failed: ${summary} ${routeLabel}`
    : live
      ? `Agent run in progress: ${routeLabel}`
      : `Agent run complete: ${routeLabel}${total > 0 ? `, ${formatDuration(total)}` : ""}`;

  return <div aria-live={live ? "polite" : undefined} className="-ml-1.5 min-w-0">
    <button
      type="button"
      onClick={() => setOpen((current) => !current)}
      data-inline-disclosure="true"
      aria-label={activityLabel}
      aria-expanded={open}
      aria-controls={detailsId}
      className={cn("flex min-h-8 w-full min-w-0 items-center gap-2 rounded-lg px-2 text-left text-meta font-medium leading-5 transition-colors hover:bg-surface-sunken", broke ? "text-danger-ink" : "text-ink-muted")}
    >
      {broke ? <TriangleAlert size={14} className="shrink-0" /> : null}
      <span className={cn("min-w-0 flex-1", broke ? "break-words" : "truncate")}>{summary}</span>
      <span className="shrink-0 font-normal text-ink-muted/80">{routeLabel}</span>
      {total > 0 ? <span className="money ml-auto shrink-0 font-normal text-ink-muted/80">{formatDuration(total)}</span> : null}
      <ChevronDown size={14} className={cn("shrink-0 transition-transform duration-[var(--m-enter)] motion-reduce:transition-none", open && "rotate-180")} />
    </button>
    <div
      className={cn(
        "grid transition-[grid-template-rows,opacity] duration-[var(--m-enter)] ease-out motion-reduce:transition-none",
        open ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
      )}
    >
      <div id={detailsId} data-testid="agent-activity-details" aria-hidden={!open} className="overflow-hidden">
        <div className="px-2 pb-2 pt-1 text-meta leading-5 text-ink-muted">
          <p className="whitespace-pre-wrap break-words">{expandedTranscript}</p>
          <div className="mt-3 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="font-semibold text-ink-body">Execution trace</span>
            <span>{routeLabel}</span>
          </div>
          {metricSummary ? <p className="mt-1 money" data-testid="agent-run-metrics">Agno metrics · {metricSummary}</p> : null}
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
                {debugTrace && (step.input || step.output) ? <div className="mt-1 space-y-1">
                  {step.input ? <TracePayloadDisclosure label="Input" payload={step.input} /> : null}
                  {step.output ? <TracePayloadDisclosure label="Output" payload={step.output} /> : null}
                </div> : null}
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

function RelatedQuestions({ widget, onPostPrompt }: WidgetProps) {
  const questions = Array.isArray(widget.data.questions) ? widget.data.questions.map(String).filter(Boolean) : [];
  // Deliberately not bound to the active-widget interrupt gating: a suggestion
  // is always tappable, and sendPrompt's own guards ignore taps mid-run.
  if (!questions.length || !onPostPrompt) return null;
  const bandId = `${widget.id}-band`;
  // The same form the blank thread opens with: quiet ruled rows with the
  // ledger tick, not a cloud of pills. A follow-up and a starter are one
  // affordance — a line you could write next — so they wear one shape, and a
  // reader who learned "Try" already knows how to read "Ask next".
  return <div className="next-entries max-w-[62ch]" role="group" aria-labelledby={bandId}>
    <p id={bandId} className="leaf-band mb-1">Ask next</p>
    {questions.map((question, index) => <button
      key={question}
      type="button"
      // Posting a question is valid long after this turn's decision is made,
      // so the row opts out of the widget-readonly button retirement the
      // same way persistent table controls do.
      data-readonly-keep="true"
      onClick={() => onPostPrompt(question)}
      // Staggered rather than one shared reveal: the rows are separate offers,
      // and landing all at once alongside the finished answer is what made
      // them feel like a jolt at the end of the turn.
      style={{ animationDelay: `${index * 50}ms` }}
      className="next-entry"
    ><span aria-hidden className="ledger-mark" />{question}</button>)}
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
  avoidable_expenses: AvoidableExpenses,
  budget_progress: ProgressCard,
  goal_progress: ProgressCard,
  import_review: ImportReview,
  loan_calculator: LoanCalculator,
  investment_projection: InvestmentProjection,
  reconciliation_review: ReconciliationReview,
  data_chart: ChartWidget,
  insight_card: Insight,
  related_questions: RelatedQuestions,
  operation_form: OperationForm,
  operation_approval: OperationApproval,
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
  widgetTypeIds.budget_progress,
  widgetTypeIds.goal_progress,
  widgetTypeIds.reconciliation_review,
  widgetTypeIds.import_review,
  widgetTypeIds.operation_form,
  widgetTypeIds.operation_approval,
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
  const creationActions = new Set<string>([widgetActionIds.create_category, widgetActionIds.create_subcategory, widgetActionIds.create_taxonomy_path]);
  if (selectionActions.has(action)) return { status: "Selected", detail: choice };
  if (action === widgetActionIds.create_taxonomy_path) {
    const values = completionValues(widget);
    const children = Array.isArray(values.subcategories) ? values.subcategories.map(String) : [];
    return { status: "Added", detail: [choice, children.join(", ")].filter(Boolean).join(" → ") };
  }
  if (creationActions.has(action)) return { status: "Added", detail: choice };
  if (action === widgetActionIds.commit_transaction) return { status: "Saved", detail: "" };
  if (action === widgetActionIds.update_saved_transaction || action === widgetActionIds.update_transaction_draft) return { status: "Updated", detail: "" };
  if (action === widgetActionIds.edit_transaction || action === widgetActionIds.edit_saved_transaction) return { status: "Editing", detail: "" };
  if (action === widgetActionIds.confirm_remove_transaction) return { status: "Removed", detail: "" };
  if (action === widgetActionIds.merge_reconciliation) return { status: "Merged", detail: "" };
  if (action === widgetActionIds.separate_reconciliation) return { status: "Kept separate", detail: "" };
  if (action === widgetActionIds.commit_import) return { status: "Imported", detail: "" };
  if (action === widgetActionIds.edit_budget) return { status: "Editing budget", detail: "" };
  if (action === widgetActionIds.request_delete_budget) return { status: "Reviewing deletion", detail: "" };
  if (action === widgetActionIds.delete_budget) return { status: "Budget deleted", detail: "" };
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
