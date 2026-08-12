"use client";

import { Activity, CalendarDays, Check, ChevronDown, ChevronUp, CircleEllipsis, Info, Landmark, Loader2, LoaderCircle, PencilLine, Plus, ReceiptText, RotateCcw, Search, ShieldCheck, Sparkles, Target, Timer, Trash2, TrendingUp, TriangleAlert, Utensils, WalletCards, Wrench } from "lucide-react";
import { FormEvent, memo, useEffect, useMemo, useRef, useState, type ComponentType } from "react";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, Pie, PieChart, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis } from "recharts";
import type { TopLevelSpec } from "vega-lite";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { DataTableView } from "@/components/widget-library/data-table";
import { formatCount, formatDay, formatDimension, formatDuration, formatMoney, parseAmountToMinor, parseNumber } from "@/lib/format";
import { dataChartDataSchema, dataTableDataSchema, dataVisualizationDataSchema, editableTransactionTypes, widgetActionIds, widgetActions, widgetTypeIds, type DataChartData, type DataTableData, type DataVisualizationData, type Widget, type WidgetActionId } from "@/lib/protocol";
import { cn } from "@/lib/utils";

type Primitive = string | number | boolean | null | undefined;
type Data = Record<string, unknown>;

/** Chart series read left to right in the same order as the legend beside them. */
const palette = ["#22594d", "#c98f4b", "#5f8880", "#a6674f", "#8a9a5b", "#3f7f9e", "#8e6c9c", "#b0703f"];

export { formatMoney };

function str(value: unknown, fallback = "") { return typeof value === "string" ? value : fallback; }
function num(value: unknown) { const parsed = typeof value === "number" ? value : Number(value ?? 0); return Number.isFinite(parsed) ? parsed : 0; }
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
  return <section className={cn("overflow-hidden rounded-[22px] border border-line bg-surface shadow-[0_8px_28px_rgba(31,51,43,0.06)]", className)}>{children}</section>;
}

function CardHeader({ eyebrow, title, body, tone = "neutral", trailing }: { eyebrow?: string; title: string; body?: string; tone?: "neutral" | "caution"; trailing?: React.ReactNode }) {
  return <div className="flex items-start gap-3 border-b border-line-soft px-5 py-4">
    <div className="min-w-0 flex-1">
      {eyebrow ? <p className={cn("text-[11px] font-semibold tracking-[0.12em] uppercase", tone === "caution" ? "text-clay-ink" : "text-evergreen-ink")}>{eyebrow}</p> : null}
      <h3 className={cn("font-heading text-[15px] font-semibold text-ink", eyebrow && "mt-1")}>{title}</h3>
      {body ? <p className="mt-1 text-xs leading-5 text-ink-muted">{body}</p> : null}
    </div>
    {trailing}
  </div>;
}

function EmptyNote({ children }: { children: React.ReactNode }) {
  return <p className="px-5 py-6 text-center text-xs leading-5 text-ink-muted">{children}</p>;
}

function FieldLabel({ children, hint }: { children: React.ReactNode; hint?: string }) {
  return <span className="mb-1.5 block text-xs font-medium text-ink-muted">{children}{hint ? <span className="ml-1 font-normal text-ink-muted/80">{hint}</span> : null}</span>;
}

const inputClass = "block h-11 w-full rounded-xl border border-line bg-surface-sunken px-3 text-sm text-ink outline-none focus:border-evergreen-line focus:ring-2 focus:ring-[#bdd5cc]/40 disabled:opacity-50";
const invalidClass = "border-clay-line focus:border-clay focus:ring-clay-line/40";

function FieldError({ children }: { children: React.ReactNode }) {
  return <span className="mt-1 flex items-center gap-1 text-[11px] font-medium text-clay-ink"><TriangleAlert size={11} />{children}</span>;
}

export type WidgetProps = {
  widget: Widget;
  disabled?: boolean;
  /** True while this widget's own action is in flight. */
  pending?: boolean;
  onAction: (widgetId: string, action: WidgetActionId, payload: Record<string, unknown>, options?: { markUsed?: boolean }) => void;
};

/** Action buttons render their own progress so the click has an obvious effect. */
function ActionButton({ action, pending, disabled, onClick, icon }: { action: Widget["actions"][number]; pending?: boolean; disabled?: boolean; onClick: () => void; icon?: React.ReactNode }) {
  const destructive = /remove|delete|separate/.test(action.action) || action.style === "danger";
  return <Button type="button" disabled={disabled || pending} variant={action.style === "primary" ? "default" : "outline"} onClick={onClick} className={cn("h-11 rounded-xl px-4", action.style === "primary" && "bg-evergreen text-white hover:bg-evergreen-deep", destructive && "border-clay-line bg-white text-clay-ink hover:bg-clay-tint")}>
    {pending ? <Loader2 size={15} className="animate-spin" /> : icon}{action.label}
  </Button>;
}

function ActionRow({ widget, disabled, pending, onAction, icons }: WidgetProps & { icons?: Record<string, React.ReactNode> }) {
  if (!widget.actions.length) return null;
  return <div className="flex flex-wrap gap-2 border-t border-line-soft px-4 py-3 sm:px-5">
    {widget.actions.map((action) => <ActionButton key={action.id} action={action} pending={pending} disabled={disabled} icon={icons?.[action.action]} onClick={() => onAction(widget.id, action.action, action.payload)} />)}
  </div>;
}

function Selector({ widget, onAction, disabled, pending }: WidgetProps) {
  const icons: Record<string, React.ReactNode> = { food: <Utensils size={17} />, bills: <ReceiptText size={17} /> };
  const list = options(widget.data);
  const completedValues = completionValues(widget);
  const declaredAction = widget.actions[0];
  const basePayload = declaredAction?.payload ?? {};
  const suggestions = Array.isArray(widget.data.suggestions) ? widget.data.suggestions as Array<Record<string, unknown>> : [];
  const suggestedIds = new Set(suggestions.map((item) => str(item.id)));
  const remaining = list.filter((option) => !suggestedIds.has(str(option.id)));
  const field = widget.type === widgetTypeIds.category_selector ? "categoryId" : widget.type === widgetTypeIds.subcategory_selector ? "subcategoryId" : "optionId";
  return <Card>
    <CardHeader title={str(widget.data.title)} body={str(widget.data.body) || str(widget.data.category) || undefined} />
    {suggestions.length ? <div className="border-b border-line-soft p-3">
      <p className="mb-2 px-1 text-[11px] font-semibold tracking-[0.13em] text-ink-muted uppercase">Best guesses</p>
      <div className="grid gap-2 sm:grid-cols-3">{suggestions.map((suggestion) => {
        const id = str(suggestion.id);
        const selected = str(completedValues[field]) === id;
        const reasons = Array.isArray(suggestion.reasons) ? suggestion.reasons.map((reason) => str(reason)).filter(Boolean) : [];
        return <button key={id} type="button" aria-label={str(suggestion.label)} aria-pressed={selected || undefined} disabled={disabled || pending || !declaredAction?.action} onClick={() => declaredAction?.action && onAction(widget.id, declaredAction.action, { ...basePayload, [field]: id })} className="rounded-2xl border border-evergreen-line bg-evergreen-tint/50 p-3 text-left transition hover:border-evergreen-ink hover:bg-evergreen-tint disabled:opacity-50">
          <span className="flex items-center gap-1.5 text-sm font-semibold text-evergreen-ink">{selected ? <Check size={14} /> : null}{str(suggestion.label)}</span>
          {reasons.length ? <span className="mt-1.5 block text-[11px] leading-4 text-ink-muted">{reasons.join(" · ")}</span> : null}
        </button>;
      })}</div>
    </div> : null}
    {remaining.length ? <div className="grid grid-cols-1 gap-2 p-3 sm:grid-cols-2 lg:grid-cols-3">
      {remaining.map((option) => {
        const id = str(option.id); const slug = str(option.slug, id);
        const action = declaredAction?.action;
        const selected = str(completedValues[field]) === id;
        return <button key={id} type="button" disabled={disabled || pending || !action} aria-pressed={selected || undefined} onClick={() => action && onAction(widget.id, action, { ...basePayload, [field]: id })} className={cn("flex min-h-14 items-center gap-2.5 rounded-2xl border bg-surface-sunken px-3 text-left text-sm font-medium text-ink-body transition hover:border-evergreen-line hover:bg-evergreen-tint/40 disabled:cursor-not-allowed disabled:opacity-50", selected ? "border-evergreen-line bg-evergreen-tint/50" : "border-transparent")}>
          <span className="grid size-8 shrink-0 place-items-center rounded-xl bg-white text-evergreen-ink shadow-sm">{selected ? <Check size={17} /> : icons[slug] ?? <CircleEllipsis size={17} />}</span><span className="truncate">{str(option.label)}</span>
        </button>;
      })}
    </div> : list.length ? null : <EmptyNote>Nothing to choose from yet. Add the first one below.</EmptyNote>}
    {widget.type === widgetTypeIds.subcategory_selector && widget.data.allowCreate ? <button type="button" disabled={disabled || pending} onClick={() => onAction(widget.id, widgetActionIds.start_add_subcategory, basePayload)} className="flex min-h-12 w-full items-center gap-2 border-t border-line-soft px-5 py-4 text-sm font-semibold text-evergreen-ink hover:bg-evergreen-tint/30 disabled:opacity-50"><Plus size={16} /> Add new subcategory</button> : null}
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
  const basePayload = widget.actions[0]?.payload ?? {};
  const select = (categoryId: string) => onAction(widget.id, widgetActionIds.select_category, { ...basePayload, categoryId });

  if (widget.data.mode === "create") {
    function submit(event: FormEvent) {
      event.preventDefault();
      const name = newCategory.trim();
      if (name) onAction(widget.id, widgetActionIds.create_category, { ...basePayload, name });
    }
    return <Card><form onSubmit={submit} className="space-y-4 p-5">
      <div><h3 className="font-heading text-[15px] font-semibold text-ink">Add a new category</h3><p className="mt-1 text-xs leading-5 text-ink-muted">It stays private to your workspace and is applied to this transaction.</p></div>
      <label className="block"><FieldLabel>Category name</FieldLabel><input autoFocus={!disabled} disabled={disabled || pending} aria-label="New category name" value={newCategory} onChange={(event) => setNewCategory(event.target.value)} placeholder="e.g. Pets" maxLength={80} className={inputClass} /></label>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={disabled || pending || !newCategory.trim()} className="h-11 rounded-xl bg-evergreen px-4 text-white hover:bg-evergreen-deep">{pending ? <Loader2 size={15} className="animate-spin" /> : <Plus size={15} />} Add category</Button>
        <Button type="button" variant="outline" disabled={disabled || pending} onClick={() => onAction(widget.id, widgetActionIds.cancel_add_category, basePayload)} className="h-11 rounded-xl">Cancel</Button>
      </div>
    </form></Card>;
  }

  return <Card>
    <div className="border-b border-line-soft px-5 py-4">
      <h3 className="font-heading text-[15px] font-semibold text-ink">{str(widget.data.title)}</h3>
      <p className="mt-1 text-xs leading-5 text-ink-muted">{str(widget.data.body)}</p>
      <label className="relative mt-3 block"><Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-muted" /><input disabled={disabled || pending} aria-label="Search categories" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search categories" className={cn(inputClass, "pl-9")} /></label>
    </div>
    {!normalizedQuery && suggestions.length ? <div className="border-b border-line-soft p-3">
      <p className="mb-2 px-1 text-[11px] font-semibold tracking-[0.13em] text-ink-muted uppercase">Best guesses</p>
      <div className="grid gap-2 sm:grid-cols-3">{suggestions.map((suggestion) => { const selected = selectedCategoryId === str(suggestion.id); return <button key={str(suggestion.id)} type="button" aria-label={str(suggestion.label)} aria-pressed={selected || undefined} disabled={disabled || pending} onClick={() => select(str(suggestion.id))} className="rounded-2xl border border-evergreen-line bg-evergreen-tint/50 p-3 text-left transition hover:border-evergreen-ink hover:bg-evergreen-tint disabled:opacity-50">
        <span className="flex items-center gap-1.5 text-sm font-semibold text-evergreen-ink">{selected ? <Check size={14} /> : null}{str(suggestion.label)}</span>
        <span className="mt-1.5 block text-[11px] leading-4 text-ink-muted">{Array.isArray(suggestion.reasons) && suggestion.reasons.length ? suggestion.reasons.join(" · ") : "Suggested for this entry"}</span>
      </button>; })}</div>
    </div> : null}
    <div className="p-3">
      <p className="mb-2 px-1 text-[11px] font-semibold tracking-[0.13em] text-ink-muted uppercase">{normalizedQuery ? "Search results" : "All categories"}</p>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">{filtered.map((option) => { const selected = selectedCategoryId === str(option.id); return <button key={str(option.id)} type="button" aria-pressed={selected || undefined} disabled={disabled || pending} onClick={() => select(str(option.id))} className={cn("flex min-h-12 items-center gap-2.5 rounded-2xl bg-surface-sunken px-3 text-left text-sm font-medium text-ink-body transition hover:bg-evergreen-tint/40 disabled:opacity-50", selected && "ring-1 ring-evergreen-line")}><span className="grid size-8 shrink-0 place-items-center rounded-xl bg-white text-evergreen-ink">{selected ? <Check size={17} /> : <CircleEllipsis size={17} />}</span><span className="truncate">{str(option.label)}</span></button>; })}</div>
      {filtered.length === 0 ? <div className="px-1 py-5 text-center"><p className="text-xs text-ink-muted">No category matches “{query.trim()}”.</p><Button type="button" variant="outline" disabled={disabled || pending} onClick={() => onAction(widget.id, widgetActionIds.start_add_category, basePayload)} className="mt-2.5 h-10 rounded-xl text-xs"><Plus size={14} /> Create “{query.trim()}” instead</Button></div> : null}
    </div>
    <button type="button" disabled={disabled || pending} onClick={() => onAction(widget.id, widgetActionIds.start_add_category, basePayload)} className="flex min-h-12 w-full items-center gap-2 border-t border-line-soft px-5 py-4 text-sm font-semibold text-evergreen-ink hover:bg-evergreen-tint/30 disabled:opacity-50"><Plus size={16} /> Add new category</button>
  </Card>;
}

function TaxonomyEditor({ widget, onAction, disabled, pending }: WidgetProps) {
  const [name, setName] = useState(str(widget.data.name));
  const operation = str(widget.data.operation);
  const isSubcategory = operation === widgetActionIds.create_subcategory;
  const lifecycle = str(widget.data.lifecycle, "pending");
  const resolved = lifecycle === "completed" || lifecycle === "cancelled";
  const submitAction = isSubcategory ? widgetActionIds.create_subcategory : widgetActionIds.create_category;
  const basePayload = widget.actions[0]?.payload ?? {};
  function submit(event: FormEvent) {
    event.preventDefault();
    if (name.trim()) onAction(widget.id, submitAction, { ...basePayload, name: name.trim() });
  }
  return <Card><form onSubmit={submit} className="space-y-4 p-5">
    <div><h3 className="font-heading text-[15px] font-semibold text-ink">{isSubcategory ? `Add a subcategory under ${str(widget.data.parentCategory)}` : "Add a new category"}</h3><p className="mt-1 text-xs leading-5 text-ink-muted">{lifecycle === "completed" ? `${name} was added${isSubcategory ? ` under ${str(widget.data.parentCategory)}` : ""}.` : lifecycle === "cancelled" ? "No taxonomy changes were made." : <>Review the name before it is added to your finance taxonomy{widget.data.appliesToDraft ? " and applied to this transaction" : ""}.</>}</p></div>
    <label className="block"><FieldLabel>{isSubcategory ? "Subcategory name" : "Category name"}</FieldLabel><input autoFocus={!disabled && !resolved} disabled={disabled || pending || resolved} aria-label={isSubcategory ? "New subcategory name" : "New category name"} value={name} onChange={(event) => setName(event.target.value)} placeholder={isSubcategory ? "e.g. Materials" : "e.g. Pets"} maxLength={80} className={inputClass} /></label>
    {!resolved ? <div className="flex flex-wrap gap-2">
      <Button type="submit" disabled={disabled || pending || !name.trim()} className="h-11 rounded-xl bg-evergreen px-4 text-white hover:bg-evergreen-deep">{pending ? <Loader2 size={15} className="animate-spin" /> : <Plus size={15} />} {isSubcategory ? "Add subcategory" : "Add category"}</Button>
      <Button type="button" variant="outline" disabled={disabled || pending} onClick={() => onAction(widget.id, widgetActionIds.cancel_taxonomy_change, basePayload)} className="h-11 rounded-xl">Cancel</Button>
    </div> : <div className="flex items-center gap-2 text-xs font-semibold text-evergreen-ink"><Check size={15} />{lifecycle === "completed" ? "Added" : "Cancelled"}</div>}
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

  return <Card className={destructive ? "border-clay-line" : undefined}>
    <div className={cn("px-5 py-5 text-white", destructive ? "bg-[linear-gradient(135deg,#8c452f,#a2573d)]" : "bg-[linear-gradient(135deg,#1d5146,#2e6b5d)]")}>
      <div className="mb-5 flex items-center justify-between gap-3">
        <span className="rounded-full bg-white/15 px-2.5 py-1 text-[11px] font-semibold tracking-wide text-white uppercase">{str(data.status, destructive ? "Confirm removal" : "Ready to save")}</span>
        {destructive ? <Trash2 size={19} className="shrink-0 text-white/90" /> : <ShieldCheck size={19} className="shrink-0 text-[#cce3d8]" />}
      </div>
      <p className="money text-[30px] font-semibold">{formatMoney(data.amountMinor, str(data.currency, "INR"))}</p>
      <p className="mt-1 text-sm text-white/85">{[data.merchant, data.subcategory, data.transactionType].filter(Boolean).map(String).join(" · ")}</p>
    </div>
    <div className="space-y-3 px-5 py-4">
      <div className="flex items-center gap-2 text-sm"><CalendarDays size={16} className="text-ink-muted" /><span className="text-ink-muted">Date</span><span className="ml-auto font-medium text-ink">{formatDay(data.date) || "—"}</span></div>
      {rows.map(([label, value]) => <div key={label} className="flex flex-wrap items-baseline gap-x-3 rounded-2xl bg-surface-sunken px-4 py-3 text-sm"><span className="text-ink-muted">{label}</span><span className="ml-auto text-right font-medium text-ink">{value}</span></div>)}
      {Array.isArray(data.tags) && data.tags.length ? <p className="text-xs text-ink-muted">{data.tags.map(String).map((tag) => `#${tag}`).join(" · ")}</p> : null}
      {inferred.length ? <p className="flex items-start gap-2 text-[11px] leading-5 text-ink-muted"><Info size={13} className="mt-0.5 shrink-0" />I filled in {inferred.map((field) => field.replaceAll("_", " ")).join(", ")} myself. Edit to change {inferred.length === 1 ? "it" : "them"} before saving.</p> : null}
      <div className="flex flex-wrap gap-2 border-t border-line-soft pt-4">{widget.actions.map((action) => <ActionButton key={action.id} action={action} pending={pending} disabled={disabled} icon={action.action === widgetActionIds.edit_transaction ? <PencilLine size={15} /> : action.action === widgetActionIds.commit_transaction ? <Check size={15} /> : /remove|delete/.test(action.action) ? <Trash2 size={15} /> : undefined} onClick={() => onAction(widget.id, action.action, action.payload)} />)}</div>
    </div>
  </Card>;
}

function TransactionPreview({ widget, onAction, disabled, pending }: WidgetProps) {
  const removed = widget.data.status === "Removed";
  const category = [widget.data.category, widget.data.subcategory].filter(Boolean).map(String).join(" → ");
  const tags = Array.isArray(widget.data.tags) ? widget.data.tags.map(String) : [];
  const sourceCount = Math.max(1, num(widget.data.sourceCount));
  const metadata = [widget.data.location, str(widget.data.spendNature) !== "unknown" ? str(widget.data.spendNature).replaceAll("_", " ") : null].filter(Boolean).map(String);
  return <Card className={cn("border-evergreen-line bg-[#f7fbf8]", removed && "border-clay-line bg-[#fbf7f4]")}>
    <div className="flex items-center gap-3 p-4">
      <span className={cn("grid size-10 shrink-0 place-items-center rounded-full bg-evergreen-tint text-evergreen-ink", removed && "bg-[#eee4de] text-clay-ink")}>{removed ? <Trash2 size={17} /> : <Check size={18} strokeWidth={2.5} />}</span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-semibold text-ink">{str(widget.data.title, "Transaction saved")}</p>
        <p className="mt-0.5 text-xs text-ink-muted">{[category, formatDay(widget.data.date), str(widget.data.status) && !removed ? str(widget.data.status) : null, `${sourceCount} source${sourceCount === 1 ? "" : "s"}`].filter(Boolean).join(" · ")}</p>
        {metadata.length || tags.length ? <p className="mt-1 truncate text-[11px] text-ink-muted">{[...metadata, ...tags.map((tag) => `#${tag}`)].join(" · ")}</p> : null}
      </div>
      <Money value={widget.data.amountMinor} currency={str(widget.data.currency, "INR")} className="shrink-0 font-semibold text-ink" />
    </div>
    {widget.actions.length ? <div className="flex flex-wrap gap-2 border-t border-line-soft px-4 py-3">{widget.actions.map((action) => <ActionButton key={action.id} action={action} pending={pending} disabled={disabled} icon={action.action === widgetActionIds.edit_saved_transaction ? <PencilLine size={14} /> : <Trash2 size={14} />} onClick={() => onAction(widget.id, action.action, action.payload)} />)}</div> : null}
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
  const [date, setDate] = useState(str(submitted.date ?? widget.data.date));
  const [categoryId, setCategoryId] = useState(str(submitted.categoryId ?? widget.data.categoryId));
  const [subcategoryId, setSubcategoryId] = useState(str(submitted.subcategoryId ?? widget.data.subcategoryId));
  const [transactionType, setTransactionType] = useState(str(submitted.transactionType ?? widget.data.transactionType, "expense"));
  const [location, setLocation] = useState(str(submitted.location ?? widget.data.location));
  const [spendNature, setSpendNature] = useState(str(submitted.spendNature ?? widget.data.spendNature, "unknown"));
  const submittedTags = submitted.tags ?? widget.data.tags;
  const [tags, setTags] = useState(Array.isArray(submittedTags) ? submittedTags.map(String).join(", ") : "");
  const [amountError, setAmountError] = useState<string | null>(null);
  const categories = Array.isArray(widget.data.categories) ? widget.data.categories as Data[] : [];
  const subcategories = (Array.isArray(widget.data.subcategories) ? widget.data.subcategories as Data[] : []).filter((item) => str(item.categoryId) === categoryId);
  const needsCategory = categories.length > 0 && transactionType === "expense" && shows("category");
  const editable = { type: saved && shows("transaction_type"), location: saved && shows("location"), nature: saved && shows("spend_nature"), tags: saved && shows("tags") };
  // Persisted edit widgets created before the cancel action was added still
  // receive the safe backend cancel path after a refresh.
  const cancelAction: Widget["actions"][number] | undefined = widget.actions.find((action) => action.action === widgetActionIds.cancel_saved_transaction_edit) ?? (saved ? {
    id: "cancel",
    label: "Cancel",
    action: widgetActionIds.cancel_saved_transaction_edit,
    style: "secondary",
    payload: { transactionId: widget.data.transactionId },
  } : undefined);

  function submit(event: FormEvent) {
    event.preventDefault();
    const minor = parseAmountToMinor(amount);
    if (minor === null) { setAmountError("Enter an amount greater than zero, like 1,500 or 1500.50."); return; }
    setAmountError(null);
    onAction(widget.id, saved ? widgetActionIds.update_saved_transaction : widgetActionIds.update_transaction_draft, saved
      ? { transactionId: widget.data.transactionId, amountMinor: minor, merchant, date, transactionType, location, spendNature, tags: tags.split(",").map((tag) => tag.trim()).filter(Boolean), categoryId, subcategoryId }
      : { draftId: widget.data.draftId, amountMinor: minor, merchant, date });
  }

  return <Card><form onSubmit={submit} noValidate className="space-y-4 p-5">
    <div>
      <h3 className="font-heading text-[15px] font-semibold text-ink">{str(widget.data.title, saved ? "Edit transaction" : "Edit this entry")}</h3>
      <p className="mt-1 text-xs leading-5 text-ink-muted">{completing ? "Add what’s missing and I’ll finish recording it." : saved ? "Changes are written to the saved record when you apply them." : "Changes apply to this entry before it is saved."}</p>
    </div>
    <div className="grid gap-3 sm:grid-cols-2">
      <label className="block"><FieldLabel>Amount</FieldLabel><input disabled={disabled || pending} aria-label="Transaction amount" aria-invalid={Boolean(amountError)} aria-describedby={amountError ? `${widget.id}-amount-error` : undefined} inputMode="decimal" autoFocus={completing && !disabled} value={amount} onChange={(event) => { setAmount(event.target.value); if (amountError) setAmountError(null); }} placeholder="1,500" className={cn(inputClass, amountError && invalidClass)} />{amountError ? <span id={`${widget.id}-amount-error`}><FieldError>{amountError}</FieldError></span> : null}</label>
      {shows("merchant") ? <label className="block"><FieldLabel hint="optional">Merchant</FieldLabel><input disabled={disabled || pending} aria-label="Merchant" value={merchant} onChange={(event) => setMerchant(event.target.value)} placeholder="Where you paid" className={inputClass} /></label> : null}
      {shows("date") ? <label className="block"><FieldLabel>Date</FieldLabel><input disabled={disabled || pending} aria-label="Transaction date" type="date" value={date} onChange={(event) => setDate(event.target.value)} className={inputClass} /></label> : null}
      {editable.type ? <label className="block"><FieldLabel>Type</FieldLabel><select disabled={disabled || pending} aria-label="Transaction type" value={transactionType} onChange={(event) => setTransactionType(event.target.value)} className={inputClass}>{editableTransactionTypes.map((type) => <option key={type} value={type}>{type.replaceAll("_", " ")}</option>)}</select></label> : null}
      {editable.location ? <label className="block"><FieldLabel hint="optional">Location</FieldLabel><input disabled={disabled || pending} aria-label="Transaction location" value={location} onChange={(event) => setLocation(event.target.value)} placeholder="City or place" className={inputClass} /></label> : null}
      {editable.nature ? <label className="block"><FieldLabel>Spend nature</FieldLabel><select disabled={disabled || pending} aria-label="Spend nature" value={spendNature} onChange={(event) => setSpendNature(event.target.value)} className={inputClass}><option value="unknown">Not set</option><option value="essential">Essential</option><option value="discretionary">Discretionary</option><option value="potentially_avoidable">Potentially avoidable</option></select></label> : null}
      {editable.tags ? <label className="block sm:col-span-2"><FieldLabel hint="comma separated">Tags</FieldLabel><input disabled={disabled || pending} aria-label="Transaction tags" value={tags} onChange={(event) => setTags(event.target.value)} placeholder="vacation, family, reimbursable" className={inputClass} /></label> : null}
      {needsCategory ? <>
        <label className="block"><FieldLabel>Category</FieldLabel><select disabled={disabled || pending} aria-label="Transaction category" value={categoryId} onChange={(event) => { setCategoryId(event.target.value); setSubcategoryId(""); }} className={inputClass}><option value="">Choose category</option>{categories.map((item) => <option key={str(item.id)} value={str(item.id)}>{str(item.label)}</option>)}</select></label>
        <label className="block"><FieldLabel>Subcategory</FieldLabel><select aria-label="Transaction subcategory" value={subcategoryId} onChange={(event) => setSubcategoryId(event.target.value)} disabled={disabled || pending || !categoryId} className={inputClass}><option value="">{categoryId ? "Choose subcategory" : "Choose a category first"}</option>{subcategories.map((item) => <option key={str(item.id)} value={str(item.id)}>{str(item.label)}</option>)}</select></label>
      </> : null}
    </div>
    <div className="flex flex-wrap gap-2">
      <Button type="submit" disabled={disabled || pending || !amount.trim() || (needsCategory && (!categoryId || !subcategoryId))} className="h-11 rounded-xl bg-evergreen px-4 text-white hover:bg-evergreen-deep">{pending ? <Loader2 size={15} className="animate-spin" /> : null}{completing ? "Save this entry" : "Apply changes"}</Button>
      {cancelAction ? <ActionButton action={cancelAction} pending={pending} disabled={disabled} onClick={() => onAction(widget.id, cancelAction.action, cancelAction.payload)} /> : null}
    </div>
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
    <div className="px-5 pt-5">
      <p className="text-[11px] font-semibold tracking-[0.12em] text-ink-muted uppercase">{str(widget.data.period)}</p>
      <h3 className="mt-2 font-heading text-[15px] font-medium text-ink-body">{str(widget.data.title)}</h3>
      {scopePath.length ? <p aria-label={`Category path: ${scopePath.join(" to ")}`} className="mt-1 text-xs font-medium text-evergreen-ink">{scopePath.join(" → ")}</p> : null}
      <p className="money mt-1 text-[34px] font-semibold text-[#173d34]">{formatMoney(widget.data.amountMinor, currency)}</p>
      <p className="text-xs text-ink-muted">{count} recorded transaction{count === 1 ? "" : "s"}</p>
      {description ? <p className="mt-3 max-w-2xl text-xs leading-5 text-ink-muted">{description}</p> : null}
    </div>
    {chartData.length ? <div className="p-4">
      <ul className="space-y-2.5">
        {leading.map((item, index) => <li key={item.name} className="flex items-center gap-2 text-xs"><span className="size-2 shrink-0 rounded-full" style={{ background: palette[index % palette.length] }} /><span className="min-w-0 truncate text-ink-muted">{item.name}</span><Money value={item.value} currency={currency} className="ml-auto shrink-0 font-medium text-ink" /></li>)}
        {rest.length ? <li className="flex items-center gap-2 text-xs"><span className="size-2 shrink-0 rounded-full bg-[#c3cdc7]" /><span className="text-ink-muted">{rest.length} more {rest.length === 1 ? "category" : "categories"}</span><Money value={restTotal} currency={currency} className="ml-auto shrink-0 font-medium text-ink" /></li> : null}
        {total ? <li className="flex items-center gap-2 border-t border-line-soft pt-2 text-xs"><span className="font-medium text-ink-body">Total</span><Money value={total} currency={currency} className="ml-auto font-semibold text-ink" /></li> : null}
      </ul>
    </div> : <p className="mx-5 my-5 rounded-2xl border border-dashed border-line py-7 text-center text-sm text-ink-muted">No spending recorded in this period yet.</p>}
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
  return <p className="mt-2 text-[11px] leading-4 text-ink-muted"><span className="font-medium text-ink-body">How to read this:</span> {explanation} Hover or focus the chart for exact values. {rows.length} data point{rows.length === 1 ? "" : "s"} included.</p>;
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
      <div style={{ height: donutHeight }} className="chart-donut-plot relative w-full min-w-0" role="img" aria-label={`${view.title}. ${rows.length} plotted data points.`}>
        <ResponsiveContainer width="100%" height="100%" minWidth={0}>
          <PieChart accessibilityLayer>
            <Pie data={rows} dataKey={theta.field} nameKey={color.field} innerRadius="48%" outerRadius="78%" paddingAngle={1.5} isAnimationActive={false}>
              {rows.map((row, index) => <Cell key={`${str(row[color.field])}-${index}`} fill={palette[index % palette.length]} />)}
            </Pie>
            <Tooltip formatter={(value, name) => [visualValue(value, theta), String(name)]} />
          </PieChart>
        </ResponsiveContainer>
        {totalMinor !== null ? <div className="pointer-events-none absolute inset-0 grid place-items-center text-center" aria-hidden="true"><div><p className="text-[10px] font-semibold tracking-[0.12em] text-ink-muted uppercase">Total</p><p className="money mt-1 text-lg font-semibold text-evergreen-ink">{formatMoney(totalMinor)}</p></div></div> : null}
      </div>
      <ul className="chart-donut-legend" aria-label={`${color.title ?? color.field} legend`}>
        {rows.map((row, index) => <li key={`${str(row[color.field])}-${index}`} className="chart-donut-legend-item flex min-w-0 items-center gap-2 text-xs">
          <span className="size-2.5 shrink-0 rounded-full" style={{ backgroundColor: palette[index % palette.length] }} aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate text-ink-muted">{formatDimension(row[color.field])}</span>
          <span className="shrink-0 font-medium tabular-nums text-ink-body">{visualValue(row[theta.field], theta)}</span>
        </li>)}
      </ul>
    </div>
    {chartGuide(view, rows)}
  </div>;
  }

  if (!x || !y) return <div role="alert" className="rounded-xl border border-dashed border-clay-line px-4 py-6 text-center text-xs text-clay-ink">This visual is missing a validated axis. The underlying data was not discarded.</div>;
  const common = <>
    <CartesianGrid stroke="#e5e9e6" vertical={false} />
    <XAxis dataKey={x.field} tick={{ fill: "#67756f", fontSize: 11 }} axisLine={{ stroke: "#d8dedb" }} tickLine={false} />
    <YAxis tickFormatter={yTick} tick={{ fill: "#67756f", fontSize: 11 }} axisLine={false} tickLine={false} width={82} />
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
    <div style={{ height }} className="w-full min-w-0" role="img" aria-label={`${view.title}. ${rows.length} plotted data points.`}><ResponsiveContainer width="100%" height="100%" minWidth={0}>{chart}</ResponsiveContainer></div>
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
        axis: { labelColor: "#67756f", titleColor: "#34443e", gridColor: "#e5e9e6", domainColor: "#d8dedb" },
        legend: { labelColor: "#67756f", titleColor: "#34443e" },
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
      <div key={`${payload}-${retry}`} ref={target} role="img" className={cn("min-w-0 [&_.vega-embed]:w-full [&_.vega-embed>svg]:max-w-full", status !== "ready" && "opacity-0")} aria-label={`${view.title}. ${rows.length} plotted data points.`} />
      {status === "loading" ? <div role="status" className="absolute inset-0 grid place-items-center rounded-xl bg-surface-sunken text-xs text-ink-muted"><span className="flex items-center gap-2"><LoaderCircle size={15} className="animate-spin" />Preparing {rows.length} data point{rows.length === 1 ? "" : "s"}…</span></div> : null}
      {status === "failed" ? <div role="alert" className="absolute inset-0 grid place-items-center rounded-xl border border-dashed border-clay-line bg-clay-tint/30 px-5 text-center"><div><p className="text-sm font-medium text-clay-ink">The chart renderer hit a problem.</p><p className="mt-1 text-xs leading-5 text-ink-muted">The validated data is still available. Retry the visual or use the description below.</p><Button type="button" variant="outline" size="sm" onClick={() => setRetry((value) => value + 1)} className="mt-3 h-9 rounded-lg"><RotateCcw size={13} />Retry chart</Button></div></div> : null}
    </div>
    <p className="mt-2 text-[11px] leading-4 text-ink-muted"><span className="font-medium text-ink-body">How to read this:</span> {chartHelp} Hover or focus the chart for exact values. {rows.length} data point{rows.length === 1 ? "" : "s"} included.</p>
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
        return <section key={view.id} className={cn("min-w-0", !singleView && "rounded-2xl border border-line-soft bg-surface-sunken p-3")}>
          {!singleView ? <h4 className="text-sm font-semibold text-ink">{view.title}</h4> : null}
          {!singleView && view.description ? <p className="mt-1 text-[11px] leading-4 text-ink-muted">{view.description}</p> : null}
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
    <div className="px-5 py-5">
      <div className="flex items-center gap-3">
        <span className={cn("grid size-11 shrink-0 place-items-center rounded-2xl", affordable ? "bg-evergreen-tint text-evergreen-ink" : "bg-clay-tint text-clay")}>{affordable ? <Check size={20} /> : <TrendingUp size={20} />}</span>
        <div className="min-w-0"><h3 className="font-heading text-[15px] font-semibold text-ink">{str(widget.data.title)}</h3><p className="text-xs text-ink-muted">{affordable ? "Affordable with your reserve intact" : "Build a little more room first"}</p></div>
      </div>
      <div className="mt-5 space-y-2">
        <div className="flex justify-between text-xs text-ink-muted"><span>Available after reserve</span><Money value={available} currency={currency} className="font-medium text-ink-body" /></div>
        <Progress value={progress} aria-label="Share of the purchase you can cover" className="h-2 bg-line-soft [&_[data-slot=progress-indicator]]:bg-evergreen" />
        <div className="flex justify-between text-[11px] text-ink-muted"><span>{str(widget.data.rule)}</span><span>Goal <Money value={purchase} currency={currency} /></span></div>
      </div>
    </div>
    {str(widget.data.dataQuality) ? <p className="border-t border-line-soft bg-surface-sunken px-5 py-3 text-[11px] text-ink-muted">{str(widget.data.dataQuality)}</p> : null}
  </Card>;
}

function ProgressCard({ widget, onAction, disabled, pending }: WidgetProps) {
  const isGoal = widget.type === widgetTypeIds.goal_progress;
  const currency = str(widget.data.currency, "INR");
  const current = num(isGoal ? widget.data.currentMinor : widget.data.spentMinor);
  const total = num(isGoal ? widget.data.targetMinor : widget.data.amountMinor);
  const ratio = total ? current / total * 100 : 0;
  const progress = Math.max(0, Math.min(100, ratio));
  // Spending past a budget is the one thing this card exists to warn about.
  const over = !isGoal && current > total && total > 0;
  const remainder = over ? current - total : num(widget.data.remainingMinor);
  return <Card className={over ? "border-clay-line" : undefined}>
    <div className="p-5">
      <div className="flex items-center gap-3">
        <span className={cn("grid size-11 shrink-0 place-items-center rounded-2xl", over ? "bg-clay-tint text-clay" : "bg-evergreen-tint text-evergreen-ink")}>{isGoal ? <Target size={19} /> : over ? <TriangleAlert size={19} /> : <WalletCards size={19} />}</span>
        <div className="min-w-0"><p className="text-[11px] font-semibold tracking-[0.12em] text-ink-muted uppercase">{isGoal ? "Savings goal" : "Monthly budget"}</p><h3 className="mt-0.5 font-heading text-[15px] font-semibold text-ink">{str(widget.data.title)}</h3></div>
        <span className={cn("money ml-auto shrink-0 text-sm font-semibold", over ? "text-clay-ink" : "text-evergreen-ink")}>{Math.round(ratio)}%</span>
      </div>
      <div className="mt-5">
        <Progress value={progress} aria-label={isGoal ? "Progress towards this goal" : "Share of this budget spent"} className={cn("h-2.5 bg-line-soft", over ? "[&_[data-slot=progress-indicator]]:bg-clay" : "[&_[data-slot=progress-indicator]]:bg-evergreen")} />
        <div className="mt-2 flex flex-wrap justify-between gap-x-3 text-xs text-ink-muted">
          <span><Money value={current} currency={currency} className="font-medium text-ink-body" /> {isGoal ? "saved" : "spent"}</span>
          <span className={over ? "font-medium text-clay-ink" : undefined}><Money value={remainder} currency={currency} className={cn("font-medium", over ? "text-clay-ink" : "text-ink-body")} /> {over ? "over budget" : "remaining"}</span>
        </div>
        <p className="mt-3 text-right text-sm text-ink-muted">Target <Money value={total} currency={currency} className="font-semibold text-ink" /></p>
      </div>
    </div>
    <ActionRow widget={widget} disabled={disabled} pending={pending} onAction={onAction} />
  </Card>;
}

function ImportReview({ widget, onAction, disabled, pending }: WidgetProps) {
  const complete = str(widget.data.status) === "completed";
  const total = num(widget.data.total);
  const ready = num(widget.data.highConfidence);
  const review = num(widget.data.needsReview);
  const duplicates = num(widget.data.duplicates);
  const replay = Boolean(widget.data.idempotentReplay);
  const tiles: Array<[string, number, string]> = [
    ["Rows", total, "Rows found in the file"],
    ["Ready", ready, complete ? "Recorded automatically" : "Will be recorded automatically"],
    ["Needs a look", review, "I’ll ask about these in the conversation"],
    ["Duplicates", duplicates, complete ? "Matched to transactions you already have" : "Checked once the import runs"],
  ];
  return <Card>
    <div className="border-b border-line-soft px-5 py-4">
      <p className={cn("text-[11px] font-semibold tracking-[0.12em] uppercase", complete ? "text-evergreen-ink" : "text-ink-muted")}>{complete ? "Import complete" : "Statement review"}</p>
      <h3 className="mt-1 truncate font-heading text-[15px] font-semibold text-ink" title={str(widget.data.title)}>{str(widget.data.title)}</h3>
      <p className="mt-1 text-xs leading-5 text-ink-muted">{replay ? "You’ve imported this file before, so nothing was added twice." : complete ? `${ready} recorded${review ? `, ${review} waiting on you` : ""}.` : `Nothing is recorded until you import. ${total} row${total === 1 ? "" : "s"} read from this file.`}</p>
    </div>
    {total > 0 ? <dl className="grid grid-cols-2 gap-2 p-4 sm:grid-cols-4">{tiles.map(([label, value, hint]) => <div key={label} className={cn("rounded-2xl px-3 py-3", label === "Duplicates" && !complete ? "bg-surface-sunken/60" : "bg-surface-sunken")}>
      <dt className="text-[11px] font-semibold tracking-wide text-ink-muted uppercase">{label}</dt>
      <dd className="money mt-1 text-xl font-semibold text-ink">{!complete && label === "Duplicates" ? "—" : value}</dd>
      <dd className="mt-0.5 text-[11px] leading-4 text-ink-muted">{hint}</dd>
    </div>)}</dl> : <EmptyNote>This file has no transaction rows I can read. Check that it’s the statement export and not a summary, then attach it again.</EmptyNote>}
    {widget.actions.length && total > 0 ? <ActionRow widget={widget} disabled={disabled} pending={pending} onAction={onAction} /> : null}
  </Card>;
}

/** Both calculators keep their inputs after producing a result: the whole point
 *  of a scenario is trying the next one. */
function CalculatorShell({ eyebrow, title, body, result, onEdit, disabled, children }: { eyebrow: string; title: string; body?: string; result?: React.ReactNode; onEdit?: () => void; disabled?: boolean; children?: React.ReactNode }) {
  return <Card><div className="p-5">
    <p className="text-[11px] font-semibold tracking-[0.12em] text-ink-muted uppercase">{eyebrow}</p>
    <h3 className="mt-1 font-heading text-[15px] font-semibold text-ink">{title}</h3>
    {body ? <p className="mt-1 text-xs leading-5 text-ink-muted">{body}</p> : null}
    {result}
    {onEdit ? <Button type="button" variant="outline" disabled={disabled} onClick={onEdit} className="mt-4 h-11 rounded-xl">Try different numbers</Button> : null}
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
  return <Card><form onSubmit={onSubmit} noValidate className="p-5">
    <h3 className="font-heading text-[15px] font-semibold text-ink">{str(widget.data.title)}</h3>
    <p className="mt-1 text-xs leading-5 text-ink-muted">{str(widget.data.body)}</p>
    <div className="mt-4 grid gap-3 sm:grid-cols-2">{children}</div>
    {problem ? <FieldError>{problem}</FieldError> : null}
    <Button type="submit" disabled={disabled || pending} className="mt-4 h-11 rounded-xl bg-evergreen px-4 text-white hover:bg-evergreen-deep">{pending ? <Loader2 size={15} className="animate-spin" /> : null}{submitLabel}</Button>
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
      <div className="mt-5 grid gap-2 sm:grid-cols-2">
        <div className="rounded-2xl bg-surface-sunken p-3"><p className="text-[11px] text-ink-muted uppercase">Interest saved</p><Money value={result.interest_saved_minor} currency={currency} className="mt-1 block text-lg font-semibold text-evergreen-ink" /></div>
        <div className="rounded-2xl bg-surface-sunken p-3"><p className="text-[11px] text-ink-muted uppercase">EMI reduction</p><Money value={result.emi_reduction_minor} currency={currency} className="mt-1 block text-lg font-semibold text-evergreen-ink" /></div>
      </div>
      <dl className="mt-4 text-xs text-ink-muted"><div className="flex py-1"><dt>Baseline EMI</dt><dd className="ml-auto"><Money value={baseline.emi_minor} currency={currency} className="font-medium text-ink-body" /></dd></div><div className="flex py-1"><dt>After prepayment</dt><dd className="ml-auto"><Money value={after.emi_minor} currency={currency} className="font-medium text-ink-body" /></dd></div></dl>
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
    <Money value={result.projected_value_minor} currency={currency} className="mt-4 block text-[30px] font-semibold text-[#245548]" />
    <p className="text-xs text-ink-muted">Projected after {num(result.years)} years at {num(result.assumed_annual_return_percent)}% assumed return</p>
    <div className="mt-4 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-muted"><span>Contributions <Money value={result.invested_minor} currency={currency} className="font-medium text-ink-body" /></span><span className="sm:ml-auto">Estimated growth <Money value={result.estimated_returns_minor} currency={currency} className="font-medium text-ink-body" /></span></div>
    <p className="mt-4 text-[11px] leading-5 text-ink-muted">Market returns are uncertain. This is a deterministic scenario, not a forecast or a guarantee.</p>
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

  return <Card>
    <div className="border-b border-line-soft px-5 py-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1"><p className="text-[11px] font-semibold tracking-[0.12em] text-clay-ink uppercase">Needs your review</p><h3 className="mt-1 font-heading text-[15px] font-semibold text-ink">{str(widget.data.title, "Possible duplicate")}</h3></div>
        <span className="money shrink-0 rounded-full bg-clay-tint px-2.5 py-1 text-xs font-semibold text-clay-ink">{Math.round(num(widget.data.score) * 100)}% match</span>
      </div>
      {signals.length ? <p className="mt-2 text-xs leading-5 text-ink-muted">Matched on {signals.map((signal) => signal.replaceAll("_", " ")).join(", ")}.</p> : null}
    </div>
    <div className="grid gap-2 p-4 sm:grid-cols-2">
      <div className="rounded-2xl bg-surface-sunken p-4"><p className="text-[11px] font-semibold tracking-[.12em] text-ink-muted uppercase">Just arrived · from {str(incoming.source, "an unnamed source").toUpperCase()}</p><Money value={incoming.amountMinor} currency={str(incoming.currency, "INR")} className="mt-3 block text-xl font-semibold text-ink" /><p className="mt-1 text-sm text-ink-body">{str(incoming.merchant, "Unknown merchant")}</p><p className="mt-2 text-xs text-ink-muted">{formatDay(incoming.date)}</p></div>
      <div className="rounded-2xl bg-evergreen-tint/40 p-4"><p className="text-[11px] font-semibold tracking-[.12em] text-evergreen-ink uppercase">Already recorded · {existingSources} source{existingSources === 1 ? "" : "s"}</p><Money value={existing.amountMinor} currency={str(existing.currency, "INR")} className="mt-3 block text-xl font-semibold text-ink" /><p className="mt-1 text-sm text-ink-body">{str(existing.merchant, "Unknown merchant")}</p><p className="mt-2 text-xs text-ink-muted">{formatDay(existing.date)}</p></div>
    </div>
    {confirmingMerge && merge ? <div className="border-t border-line-soft px-4 py-3">
      <p className="text-xs leading-5 text-ink-body">Merging keeps one transaction and records both sources against it. It can’t be split again from here.</p>
      <div className="mt-2.5 flex flex-wrap gap-2">
        <Button type="button" disabled={disabled || pending} onClick={() => onAction(widget.id, merge.action, merge.payload)} className="h-11 rounded-xl bg-evergreen px-4 text-white hover:bg-evergreen-deep">{pending ? <Loader2 size={15} className="animate-spin" /> : null}Yes, merge them</Button>
        <Button type="button" variant="ghost" disabled={disabled || pending} onClick={() => setConfirmingMerge(false)} className="h-11 rounded-xl">Go back</Button>
      </div>
    </div> : <div className="flex flex-wrap gap-2 border-t border-line-soft px-4 py-3">
      {merge ? <Button type="button" disabled={disabled || pending} onClick={() => setConfirmingMerge(true)} className="h-11 rounded-xl bg-evergreen px-4 text-white hover:bg-evergreen-deep">{merge.label}</Button> : null}
      {separate ? <ActionButton action={separate} pending={pending} disabled={disabled} onClick={() => onAction(widget.id, separate.action, separate.payload)} /> : null}
    </div>}
  </Card>;
}

function TransactionList({ widget, onAction, disabled, pending }: WidgetProps) {
  const transactions = Array.isArray(widget.data.transactions) ? widget.data.transactions as Data[] : [];
  return <Card>
    <CardHeader title={str(widget.data.title)} body={str(widget.data.body) || undefined} />
    {transactions.length ? <ul className="divide-y divide-line-soft">{transactions.map((transaction, index) => {
      const actions = Array.isArray(transaction.actions) ? transaction.actions as Data[] : [];
      const amount = transaction.amountMinor;
      // Saved analyses and other non-monetary rows arrive with a zero amount and
      // a status; showing "₹0" there would be a lie about money.
      const showAmount = num(amount) !== 0;
      const status = str(transaction.status);
      return <li key={str(transaction.id, String(index))} className="flex flex-wrap items-center gap-3 px-5 py-3.5">
        <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-surface-sunken text-evergreen-ink"><ReceiptText size={16} /></span>
        <div className="min-w-0 flex-1"><p className="truncate text-sm font-medium text-ink">{str(transaction.merchant, "Recorded item")}</p><p className="truncate text-xs text-ink-muted">{[formatDay(transaction.date), status].filter(Boolean).join(" · ")}</p></div>
        {showAmount ? <Money value={amount} currency={str(transaction.currency, "INR")} className="shrink-0 text-sm font-semibold text-ink" /> : null}
        {actions.map((action, actionIndex) => { const actionId = action.action; if (!isWidgetActionId(actionId)) return null; const removing = actionId.includes("remove"); return <Button key={str(action.id, String(actionIndex))} type="button" variant="outline" disabled={disabled || pending} onClick={() => onAction(widget.id, actionId, (action.payload ?? {}) as Record<string, unknown>)} className={cn("h-10 basis-full rounded-lg text-[11px] sm:basis-auto", removing && "border-clay-line text-clay-ink hover:bg-clay-tint")}>{removing ? <Trash2 size={13} /> : <PencilLine size={13} />}{str(action.label, "Review")}</Button>; })}
      </li>;
    })}</ul> : <EmptyNote>Nothing here yet. Record a few transactions and this list fills itself in.</EmptyNote>}
  </Card>;
}

function DynamicDataTable({ widget, onAction, disabled, pending }: WidgetProps) {
  const parsed = useMemo(() => dataTableDataSchema.safeParse(widget.data), [widget.data]);
  if (!parsed.success) return <Card><CardHeader title="This data view could not be rendered" body="The widget payload did not match the registered table contract." /></Card>;
  return <DataTableView data={parsed.data} disabled={disabled} pending={pending} onAction={(action, payload) => onAction(widget.id, action, payload)} />;
}

function Insight({ widget }: WidgetProps) {
  // The harness uses insight cards for welcomes, notes and refusals alike; the
  // card should not congratulate the user on a refusal.
  const tone = str(widget.data.tone);
  const caution = tone === "caution" || /won’t|won't|need|missing|can’t|can't/i.test(str(widget.data.title));
  return <Card className={cn(caution ? "border-clay-line bg-[linear-gradient(135deg,#fdf5f1,#fffdfa)]" : "bg-[linear-gradient(135deg,#f4f7f2,#fffdfa)]")}>
    <div className="flex gap-3.5 p-5">
      <span className={cn("grid size-10 shrink-0 place-items-center rounded-2xl", caution ? "bg-clay-tint text-clay" : "bg-evergreen-tint text-evergreen-ink")}>{caution ? <Info size={18} /> : <Sparkles size={18} />}</span>
      <div className="min-w-0">
        <p className={cn("text-[11px] font-semibold tracking-[0.13em] uppercase", caution ? "text-clay-ink" : "text-evergreen-ink")}>{str(widget.data.eyebrow, "Copilot insight")}</p>
        <h3 className="mt-1 font-heading text-base font-semibold text-ink">{str(widget.data.title)}</h3>
        <p className="mt-1.5 text-sm leading-6 text-ink-body">{str(widget.data.body)}</p>
      </div>
    </div>
  </Card>;
}

function AnalysisTable({ widget }: WidgetProps) {
  const [fullWidth, setFullWidth] = useState(false);
  const currency = str(widget.data.currency, "INR");
  const queryResults = Array.isArray(widget.data.queryResults) ? widget.data.queryResults as Data[] : [];
  const transforms = Array.isArray(widget.data.transforms) ? widget.data.transforms as Data[] : [];
  const context = widget.data.context && typeof widget.data.context === "object" ? widget.data.context as Record<string, unknown> : {};
  const allocationRows = Array.isArray(widget.data.rows) ? widget.data.rows as Data[] : [];
  const columns = Array.isArray(widget.data.columns) ? widget.data.columns.map(String) : [];
  const budgetRoom = Array.isArray(widget.data.budgetRoom) ? widget.data.budgetRoom as Data[] : [];
  const roomLabels = new Set(budgetRoom.map((item) => str(item.label)));
  const empty = !queryResults.length && !transforms.length && !allocationRows.length && !Object.keys(context).length;

  return <Card className={cn(fullWidth && "relative left-1/2 z-20 w-[calc(100vw-2rem)] max-w-[742px] -translate-x-1/2 sm:w-[calc(100vw-3rem)] md:w-[min(742px,calc(100vw-328px))]")}>
    <CardHeader eyebrow="Governed analysis" title={str(widget.data.title)} body={str(widget.data.body) || undefined} />
    {budgetRoom.length ? <div className="border-b border-line-soft p-4">
      <p className="mb-2 text-[11px] font-semibold tracking-[0.1em] text-evergreen-ink uppercase">Below the limits you set</p>
      <ul className="flex flex-wrap gap-2">{budgetRoom.map((item, index) => <li key={str(item.label, String(index))} className="rounded-full bg-evergreen-tint px-3 py-1.5 text-xs text-evergreen-ink">{str(item.label)} · <Money value={item.room_minor} currency={currency} className="font-semibold" /> unspent</li>)}</ul>
    </div> : null}
    {transforms.length ? <div className="grid gap-2 border-b border-line-soft p-4 sm:grid-cols-2">{transforms.map((transform, index) => { const values = Array.isArray(transform.values) ? transform.values as Data[] : []; return <div key={`${str(transform.name)}-${index}`} className="rounded-2xl bg-evergreen-tint/40 p-3">
      <p className="text-[11px] font-semibold tracking-[0.1em] text-evergreen-ink uppercase">{str(transform.operation).replaceAll("_", " ")}</p>
      <p className="mt-1 text-xs font-semibold text-ink-body">{str(transform.name)}</p>
      {values.slice(0, 3).map((value, valueIndex) => <div key={str(value.label, String(valueIndex))} className="mt-2 flex gap-3 text-[11px] text-ink-muted"><span className="min-w-0 truncate">{formatDimension(value.label)}</span><span className="money ml-auto shrink-0 font-semibold text-ink-body">{str(transform.metric) === "transaction_count" ? formatCount(num(value.value)) : formatMoney(value.value, currency)}</span></div>)}
    </div>; })}</div> : null}
    {Object.keys(context).length ? <div className="grid gap-2 border-b border-line-soft p-4 sm:grid-cols-2">{Object.entries(context).map(([source, rawRows]) => { const rows = Array.isArray(rawRows) ? rawRows as Data[] : []; return <div key={source} className="rounded-2xl border border-line p-3">
      <p className="text-[11px] font-semibold tracking-[0.1em] text-ink-muted uppercase">{source.replaceAll("_", " ")}</p>
      {rows.slice(0, 5).map((row, index) => <div key={str(row.id, String(index))} className="mt-2 flex items-center gap-2 text-[11px] text-ink-muted"><span className="min-w-0 truncate">{str(row.name, str(row.merchant, "Recorded item"))}</span>{row.remainingMinor != null ? <span className="ml-auto shrink-0"><Money value={row.remainingMinor} currency={str(row.currency, currency)} className="font-semibold text-ink-body" /> remaining</span> : row.balanceMinor != null ? <Money value={row.balanceMinor} currency={str(row.currency, currency)} className="ml-auto shrink-0 font-semibold text-ink-body" /> : row.principalMinor != null ? <Money value={row.principalMinor} currency={str(row.currency, currency)} className="ml-auto shrink-0 font-semibold text-ink-body" /> : null}</div>)}
      {!rows.length ? <p className="mt-2 text-[11px] text-ink-muted">No saved records</p> : null}
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
      return <div key={`${str(result.name)}-${resultIndex}`} className="border-b border-line-soft p-4 last:border-b-0">
        <div className="mb-3 flex flex-wrap items-end gap-x-3 gap-y-1"><p className="text-sm font-semibold text-ink-body">{str(result.name)}</p><p className="text-[11px] text-ink-muted">{formatDay(result.start)} → {formatDay(result.end)}</p></div>
        <DataTableView data={table} embedded onInlineWidthChange={setFullWidth} />
      </div>;
    })}
    {allocationRows.length ? <div className="overflow-x-auto p-4"><table className="w-full min-w-[520px] text-left text-xs">
      <thead><tr className="text-ink-muted"><th scope="col" className="pb-2 font-medium">Category</th>{columns.map((column) => <th key={column} scope="col" className="pb-2 text-right font-medium">{column}</th>)}</tr></thead>
      <tbody className="divide-y divide-line-soft">{allocationRows.map((row, index) => { const months = (row.months ?? {}) as Data; const highlighted = roomLabels.has(str(row.label)); return <tr key={str(row.id, String(index))} className={highlighted ? "bg-evergreen-tint/40" : undefined}>
        <td className="py-2.5 font-medium text-ink-body">{str(row.label)}{highlighted ? <span className="ml-1.5 text-[10px] font-semibold text-evergreen-ink uppercase">room</span> : null}</td>
        {columns.map((column) => <td key={column} className="money py-2.5 text-right text-ink-muted">{formatMoney(months[column], currency)}</td>)}
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
    setDecided((current) => ({ ...current, [id]: spendNature }));
    onAction(widget.id, widgetActionIds.set_spend_nature, { transactionId: id, spendNature }, { markUsed: false });
  }

  return <Card>
    <div className="border-b border-line-soft px-5 py-4">
      <p className="text-[11px] font-semibold tracking-[0.12em] text-clay-ink uppercase">Review, not an automatic judgement</p>
      <h3 className="mt-1 font-heading text-[15px] font-semibold text-ink">{str(widget.data.title)}</h3>
      <p className="mt-1 text-xs leading-5 text-ink-muted">{str(widget.data.body)}</p>
      {potential != null && transactions.length ? <p className="mt-2 text-xs text-ink-muted"><Money value={potential} currency={currency} className="font-semibold text-ink-body" /> across {transactions.length} expense{transactions.length === 1 ? "" : "s"} worth a look.</p> : null}
    </div>
    <ul className="divide-y divide-line-soft">{transactions.map((transaction, index) => {
      const id = str(transaction.id, String(index));
      const choice = decided[id];
      return <li key={id} className="p-4">
        <div className="flex items-start gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-surface-sunken text-clay-ink"><ReceiptText size={15} /></span>
          <div className="min-w-0 flex-1">
            <div className="flex gap-3"><p className="min-w-0 truncate text-sm font-semibold text-ink">{str(transaction.merchant, "Recorded expense")}</p><Money value={transaction.amountMinor} currency={str(transaction.currency, currency)} className="ml-auto shrink-0 text-sm font-semibold text-ink" /></div>
            <p className="mt-0.5 text-[11px] text-ink-muted">{[transaction.category, transaction.subcategory, formatDay(transaction.date)].filter(Boolean).map(String).join(" · ")}</p>
            <p className="mt-2 text-[11px] leading-5 text-ink-muted">{Array.isArray(transaction.reasons) && transaction.reasons.length ? transaction.reasons.join(" · ") : "Worth a second look"}</p>
            {choice ? <p className="mt-2 flex items-center gap-1.5 text-[11px] font-medium text-evergreen-ink"><Check size={13} />Marked {choice === "essential" ? "essential" : "potentially avoidable"}</p> : <div className="mt-2 flex flex-wrap gap-2">
              <Button type="button" disabled={disabled} variant="outline" className="h-10 rounded-lg text-[11px]" onClick={() => decide(id, "potentially_avoidable")}>Mark avoidable</Button>
              <Button type="button" disabled={disabled} variant="ghost" className="h-10 rounded-lg text-[11px]" onClick={() => decide(id, "essential")}>Keep — it’s essential</Button>
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
          <div className="min-w-0"><p className="text-sm font-semibold text-ink-body">{str(loan.name)}</p><p className="text-[11px] text-ink-muted">{[str(loan.lender), `${num(loan.annualRatePercent)}%`, `${num(loan.tenureMonths)} months`].filter(Boolean).join(" · ")}</p></div>
          <Money value={loan.principalMinor} currency={currency} className="ml-auto text-sm font-semibold text-[#254e43]" />
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-2">{scenarios.map((scenario, scenarioIndex) => { const shorter = (scenario.shorter_tenure ?? {}) as Data; const lower = (scenario.lower_emi ?? {}) as Data; return <div key={scenarioIndex} className="rounded-2xl bg-surface-sunken p-3">
          <p className="text-[11px] font-semibold text-ink-muted uppercase">Prepay <Money value={scenario.prepayment_minor} currency={currency} /></p>
          <p className="mt-2 text-xs leading-5 text-ink-body">Shorter tenure: save <Money value={shorter.interest_saved_minor} currency={currency} className="font-semibold" /> and {num(shorter.months_saved)} months</p>
          <p className="mt-1 text-xs leading-5 text-ink-body">Lower EMI: save <Money value={lower.interest_saved_minor} currency={currency} className="font-semibold" /> interest</p>
        </div>; })}</div>
      </div>;
    }) : <EmptyNote>No active loans are saved yet.</EmptyNote>}
  </Card>;
}

/** A run is worth watching while it happens and worth a footnote once it is over:
 *  the steps answer "what is it doing?", which stops being the question the
 *  moment the answer arrives. So it opens itself live and folds to a single line
 *  when it finishes — and a reader who opens a finished run keeps it open. */
function AgentActivity({ widget }: WidgetProps) {
  const steps = Array.isArray(widget.data.steps) ? widget.data.steps as Data[] : [];
  const total = num(widget.data.totalMs);
  // The in-flight card sets `live` itself: until the first step streams in there
  // is no running step to infer it from, and a bare run should still read as one.
  // Only the transient stream card is live. Persisted traces are terminal by
  // definition; legacy open steps are failed runs, not eternal spinners.
  const live = widget.data.live === true;
  const broke = steps.some((step) => str(step.status) === "failed" || (!live && str(step.status) === "running"));
  const [override, setOverride] = useState<boolean | null>(null);
  const open = override ?? live;

  if (!open) return <button type="button" onClick={() => setOverride(true)} aria-expanded={false} className={cn("-ml-1.5 flex min-h-8 items-center gap-1.5 rounded-lg px-1.5 text-[11px] font-medium transition-colors", broke ? "text-clay-ink hover:bg-clay-tint" : "text-ink-muted hover:bg-surface-sunken hover:text-evergreen-ink")}>
    {broke ? <TriangleAlert size={12} /> : live ? <LoaderCircle size={12} className="animate-spin" /> : <Activity size={12} />}
    {broke ? "This run hit a problem" : live ? "Working on it" : `Worked for ${formatDuration(total)}`}
    {steps.length ? <span className="font-normal text-ink-muted/80">· {steps.length} step{steps.length === 1 ? "" : "s"}</span> : null}
    <ChevronDown size={12} />
  </button>;

  return <Card className="border-line bg-[#f7faf7] shadow-none">
    <button type="button" onClick={() => setOverride(false)} aria-expanded className="flex w-full items-center gap-3 border-b border-line-soft px-4 py-3 text-left transition-colors hover:bg-white/60">
      <span className="grid size-8 shrink-0 place-items-center rounded-xl bg-evergreen-tint text-evergreen-ink">{live ? <LoaderCircle size={15} className="animate-spin" /> : <Activity size={15} />}</span>
      <div className="min-w-0"><p className="truncate text-xs font-semibold text-ink-body">{str(widget.data.title, "Agent run")}</p><p className="mt-0.5 truncate text-[11px] text-ink-muted">{str(widget.data.engine, "Agno")} · {str(widget.data.model, "agent")}</p></div>
      <span className="money ml-auto flex shrink-0 items-center gap-1 rounded-full bg-white px-2.5 py-1 text-[11px] font-semibold text-ink-muted"><Timer size={11} /> {formatDuration(total)} total</span>
      <ChevronUp size={14} className="shrink-0 text-ink-muted" />
    </button>
    <ol aria-label="Steps in this run, each with its own duration and the total elapsed time" className="divide-y divide-line-soft px-4">
      {steps.map((step, index) => {
        const running = live && str(step.status) === "running";
        const failed = str(step.status) === "failed" || (!live && str(step.status) === "running");
        const tool = str(step.tool);
        const badge = str(step.badge);
        return <li key={str(step.id, String(index))} className="grid grid-cols-[20px_1fr_auto] gap-2 py-2.5">
          <span className={cn("pt-0.5 text-evergreen-ink", failed && "text-clay")}>{running ? <LoaderCircle size={13} className="animate-spin" /> : failed ? <TriangleAlert size={13} /> : tool && tool !== "deterministic_fallback" ? <Wrench size={13} /> : <Check size={13} />}</span>
          <div className="min-w-0"><div className="flex min-w-0 items-center gap-1.5"><p className="truncate text-[11px] font-medium text-ink-body">{str(step.label)}</p>{badge ? <span className={cn("shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-semibold tracking-wide", failed ? "bg-clay-tint text-clay-ink" : "bg-evergreen-tint text-evergreen-ink")}>{badge}</span> : null}</div>{tool ? <p className="mt-0.5 truncate font-mono text-[10px] text-ink-muted">{tool}</p> : null}{step.detail ? <p className="mt-0.5 text-[10px] leading-4 text-ink-muted">{str(step.detail)}</p> : null}</div>
          <div className="money text-right text-[10px] text-ink-muted"><p>{running ? "running" : formatDuration(step.durationMs)}</p><p className="mt-0.5">Σ {formatDuration(step.cumulativeMs)}</p></div>
        </li>;
      })}
      {!steps.length ? <li className="flex items-center gap-2 py-3 text-[11px] text-ink-muted"><LoaderCircle size={13} className="animate-spin" /> Working out how to answer…</li> : null}
    </ol>
  </Card>;
}

function GenericWidget({ widget, onAction, disabled, pending }: WidgetProps) {
  return <Card>
    <div className="flex items-center gap-3 p-5">
      <span className="grid size-10 shrink-0 place-items-center rounded-2xl bg-surface-sunken text-evergreen-ink"><Landmark size={18} /></span>
      <div className="min-w-0"><h3 className="font-heading text-[15px] font-semibold capitalize text-ink">{str(widget.data.title, widget.type.replaceAll("_", " "))}</h3><p className="mt-1 text-xs leading-5 text-ink-muted">{str(widget.data.body, "This structured financial view is ready for review.")}</p></div>
    </div>
    <ActionRow widget={widget} disabled={disabled} pending={pending} onAction={onAction} />
  </Card>;
}

/** Registered renderers are the frontend half of the widget library. Adding a
 * business widget is an explicit registry operation, never model-written JSX. */
export const widgetRegistry: Partial<Record<Widget["type"], ComponentType<WidgetProps>>> = Object.freeze({
  agent_activity: AgentActivity,
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

/** Memoised because a widget is expensive to draw and almost never changes: its
 *  payload is frozen once the turn is recorded, so the only reasons to redraw
 *  are the lock flags and the handler beside it. The transcript keeps those
 *  stable, so a widget that is not being interacted with stays put. */
export const WidgetRenderer = memo(function WidgetRenderer(props: WidgetProps) {
  const Renderer = widgetRegistry[props.widget.type] ?? GenericWidget;
  const lifecycle = str(props.widget.data.lifecycle, "pending");
  const resolved = lifecycle === "completed" || lifecycle === "cancelled";
  const rendererProps = resolved ? { ...props, disabled: true } : props;
  const readonly = Boolean(rendererProps.disabled && props.widget.type !== widgetTypeIds.agent_activity);
  // Keep read-only tables scrollable and selectable. Individual controls still
  // receive `disabled`, and the transcript-level action guard rejects stale
  // actions even if a renderer accidentally omits a disabled attribute.
  return <div aria-disabled={readonly || undefined} className={cn(readonly && "widget-readonly")}>
    <Renderer {...rendererProps} />
    {resolved && props.widget.type !== widgetTypeIds.taxonomy_editor ? <div className="mt-2 flex items-center gap-1.5 px-1 text-[11px] font-semibold text-evergreen-ink"><Check size={13} />{lifecycle === "completed" ? "Applied" : "Cancelled"}</div> : null}
  </div>;
});
