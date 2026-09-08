import { ArrowDownLeft, ArrowUpRight, ChevronDown, Loader2, MapPin, SlidersHorizontal, TriangleAlert } from "lucide-react";
import { type FormEvent, type ReactNode, type Ref, useEffect, useId, useRef, useState } from "react";
import { Combobox } from "@/components/ui/combobox";
import { CurrencySelect } from "@/components/currency-select";
import { useUserDefaults } from "@/components/user-defaults";
import { resolveLocationLabel } from "@/lib/api";
import { fixForEntry, useDeviceLocation, type LocationFields } from "@/lib/device-location";
import { parseAmountToMinor, timestampInputToUtc, timestampInputValue } from "@/lib/format";
import { editableTransactionTypes, type TransactionListItemOut } from "@/lib/protocol";
import { cn } from "@/lib/utils";

export type TransactionFormField =
  | "amount"
  | "merchant"
  | "transaction_at"
  | "transaction_type"
  | "location"
  | "spend_nature"
  | "tags"
  | "category"
  | "subcategory";

export type TransactionFormSubcategory = { id: string; label: string };
export type TransactionFormCategory = {
  id: string;
  label: string;
  subcategories?: TransactionFormSubcategory[];
};

export type TransactionFormInitialValues = {
  currency?: string | null;
  amountMinor?: number | null;
  merchant?: string | null;
  transactionAt?: string | null;
  transactionType?: TransactionListItemOut["transactionType"] | null;
  categoryId?: string | null;
  subcategoryId?: string | null;
  spendNature?: TransactionListItemOut["spendNature"] | null;
  location?: string | null;
  tags?: string[];
};

export type TransactionFormValues = LocationFields & {
  currency: string;
  amountMinor: number;
  merchant: string | null;
  transactionAt: string | null;
  transactionType: TransactionListItemOut["transactionType"];
  categoryId: string | null;
  subcategoryId: string | null;
  spendNature: TransactionListItemOut["spendNature"];
  location: string | null;
  tags: string[];
};

type ActionState = {
  blocked: boolean;
  taxonomyPending: "category" | "subcategory" | null;
};

const allFields: TransactionFormField[] = [
  "amount",
  "merchant",
  "transaction_at",
  "transaction_type",
  "location",
  "spend_nature",
  "category",
  "subcategory",
];

function titleCase(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

/**
 * The canonical manual transaction form used by both the ledger drawer and
 * the conversation HITL editor. Shells own their actions and lifecycle; this
 * component owns field state, validation, taxonomy growth, and the optional
 * device-location fix so those behaviours cannot drift between entry points.
 */
export function TransactionForm({
  initialValues,
  categories: initialCategories,
  fields = allFields,
  transactionTypes = editableTransactionTypes,
  disabled = false,
  density = "page",
  locationAllowed = false,
  captureDeviceLocation = false,
  requireExpenseTaxonomy = false,
  amountInputRef,
  problem,
  banner,
  afterFields,
  className,
  onDirtyChange,
  onCreateCategory,
  onCreateSubcategory,
  onSubmit,
  renderActions,
}: {
  initialValues: TransactionFormInitialValues;
  categories: TransactionFormCategory[];
  fields?: TransactionFormField[];
  transactionTypes?: readonly TransactionListItemOut["transactionType"][];
  disabled?: boolean;
  density?: "page" | "compact" | "entry";
  locationAllowed?: boolean;
  captureDeviceLocation?: boolean;
  requireExpenseTaxonomy?: boolean;
  amountInputRef?: Ref<HTMLInputElement>;
  problem?: string | null;
  banner?: ReactNode;
  afterFields?: ReactNode;
  className?: string;
  onDirtyChange?: (dirty: boolean) => void;
  onCreateCategory?: (name: string) => Promise<TransactionFormCategory>;
  onCreateSubcategory?: (categoryId: string, name: string) => Promise<TransactionFormSubcategory>;
  onSubmit: (values: TransactionFormValues) => void;
  renderActions: (state: ActionState) => ReactNode;
}) {
  const { currency: defaultCurrency, timeZone } = useUserDefaults();
  const [currency, setCurrency] = useState(initialValues.currency ?? defaultCurrency);
  const shown = new Set(fields);
  const [amount, setAmount] = useState(initialValues.amountMinor == null ? "" : String(initialValues.amountMinor / 100));
  const [merchant, setMerchant] = useState(initialValues.merchant ?? "");
  const [transactionAt, setTransactionAt] = useState(timestampInputValue(initialValues.transactionAt ?? new Date().toISOString(), timeZone));
  const [transactionType, setTransactionType] = useState<TransactionListItemOut["transactionType"]>(initialValues.transactionType ?? "expense");
  const [categoryId, setCategoryId] = useState(initialValues.categoryId ?? "");
  const [subcategoryId, setSubcategoryId] = useState(initialValues.subcategoryId ?? "");
  const [spendNature, setSpendNature] = useState<TransactionListItemOut["spendNature"]>(initialValues.spendNature ?? "unknown");
  const [location, setLocation] = useState(initialValues.location ?? "");
  const [tags, setTags] = useState((initialValues.tags ?? []).join(", "));
  const [categories, setCategories] = useState(initialCategories);
  const [amountError, setAmountError] = useState<string | null>(null);
  const [transactionAtError, setTransactionAtError] = useState<string | null>(null);
  const [taxonomyError, setTaxonomyError] = useState<string | null>(null);
  const [taxonomyPending, setTaxonomyPending] = useState<"category" | "subcategory" | null>(null);
  const locationTouched = useRef(false);
  const locationLookup = useRef<Promise<string | null> | null>(null);
  const formId = useId();
  const formRef = useRef<HTMLFormElement>(null);
  const feedbackRef = useRef<HTMLDivElement>(null);
  const amountErrorId = `${formId}-amount-error`;
  const transactionAtErrorId = `${formId}-time-error`;
  const [opened] = useState({ amount, currency, merchant, transactionAt, transactionType, categoryId, subcategoryId, spendNature, location, tags });
  const deviceFix = useDeviceLocation(captureDeviceLocation && locationAllowed && shown.has("location"));
  const coordinateHint = deviceFix
    ? `${deviceFix.latitude.toFixed(6)}, ${deviceFix.longitude.toFixed(6)}${deviceFix.locationAccuracy === null ? "" : ` · accuracy ±${deviceFix.locationAccuracy} m`}`
    : null;
  const subcategories = categories.find((category) => category.id === categoryId)?.subcategories ?? [];
  const needsTaxonomy = requireExpenseTaxonomy && transactionType === "expense" && shown.has("category") && categories.length > 0;
  const blocked = disabled || Boolean(taxonomyPending) || !amount.trim() || (needsTaxonomy && (!categoryId || (shown.has("subcategory") && !subcategoryId)));
  const compact = density === "compact";
  const entry = density === "entry";
  const hasBanner = Boolean(banner);

  // The save action remains visible while fields scroll. Bring a failed
  // field or save response back into view so feedback cannot be missed.
  useEffect(() => {
    if (!entry) return;
    if (amountError || transactionAtError) {
      formRef.current?.querySelector<HTMLInputElement>('input[aria-invalid="true"]')?.focus();
    } else if (hasBanner || problem || taxonomyError) {
      feedbackRef.current?.scrollIntoView?.({ block: "nearest" });
    }
  }, [entry, amountError, transactionAtError, hasBanner, problem, taxonomyError]);

  const current = { amount, currency, merchant, transactionAt, transactionType, categoryId, subcategoryId, spendNature, location, tags };
  const dirty = Object.entries(opened).some(([key, value]) => current[key as keyof typeof opened] !== value);
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);

  // A resolved label is convenience, never a prerequisite for saving the
  // stronger coordinate fix. A typed label always wins over a late lookup.
  useEffect(() => {
    if (!deviceFix) return;
    let live = true;
    locationLookup.current ??= resolveLocationLabel(deviceFix.latitude, deviceFix.longitude);
    void locationLookup.current
      .then((resolved) => {
        if (!live || !resolved || locationTouched.current) return;
        setLocation((current) => current.trim() ? current : resolved);
      })
      .catch(() => undefined);
    return () => { live = false; };
  }, [deviceFix]);

  async function addCategory(name: string) {
    if (!onCreateCategory) return;
    setTaxonomyError(null);
    setTaxonomyPending("category");
    try {
      const created = await onCreateCategory(name);
      setCategories((current) => current.some((item) => item.id === created.id) ? current : [...current, created]);
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
      setCategories((current) => current.map((category) => category.id !== categoryId || category.subcategories?.some((item) => item.id === created.id)
        ? category
        : { ...category, subcategories: [...(category.subcategories ?? []), created] }));
      setSubcategoryId(created.id);
    } catch (cause) {
      setTaxonomyError(cause instanceof Error ? cause.message : "That subcategory could not be added. Try again.");
    } finally {
      setTaxonomyPending(null);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const amountMinor = parseAmountToMinor(amount);
    const instant = shown.has("transaction_at") ? timestampInputToUtc(transactionAt, timeZone) : null;
    if (amountMinor === null) {
      setAmountError("Enter an amount greater than zero, like 1,500 or 1500.50.");
      return;
    }
    if (shown.has("transaction_at") && !instant) {
      setTransactionAtError("Enter a valid date and time.");
      return;
    }
    setAmountError(null);
    setTransactionAtError(null);
    onSubmit({
      currency,
      amountMinor,
      merchant: merchant.trim() || null,
      transactionAt: instant,
      transactionType,
      categoryId: transactionType === "expense" ? categoryId || null : null,
      subcategoryId: transactionType === "expense" ? subcategoryId || null : null,
      spendNature: transactionType === "expense" ? spendNature : "unknown",
      location: location.trim() || null,
      tags: tags.split(",").map((tag) => tag.trim()).filter(Boolean),
      ...fixForEntry(deviceFix),
    });
  }

  function changeType(next: string) {
    setTransactionType(next as TransactionListItemOut["transactionType"]);
    if (next !== "expense" || !categories.some((item) => item.id === categoryId)) {
      setCategoryId("");
      setSubcategoryId("");
    }
    if (next !== "expense") setSpendNature("unknown");
  }

  const inputClass = cn(
    "manual-field block h-[var(--h-field)] w-full rounded-lg border border-line-strong bg-surface px-3 text-ink outline-none transition-colors disabled:opacity-50",
    compact ? "text-body duration-[110ms] ease-linear" : "mt-1 text-control",
    entry && "mt-2 h-12 min-w-0 rounded-xl bg-ground/60 px-3.5 placeholder:text-ink-muted hover:border-ink-muted/60 focus:border-secondary",
  );
  const triggerClass = compact ? "text-body" : entry ? "mt-2 h-12 rounded-xl bg-ground/60 px-3.5 hover:border-ink-muted/60" : "mt-1";
  const fieldClass = cn("block min-w-0", !compact && "text-note font-medium text-ink-body");
  const label = (text: string, hint?: string) => compact
    ? <span className="mb-2 block text-note font-medium text-ink-muted">{text}{hint ? <span className="ml-1 font-normal text-ink-muted/80">{hint}</span> : null}</span>
    : <>{text}{hint ? <span className="ml-1 font-normal text-ink-muted">{hint}</span> : null}</>;
  const fieldError = (message: string, id?: string) => <span id={id} role="alert" className="mt-1 flex items-center gap-1 text-meta font-medium text-danger-ink"><TriangleAlert size={14} />{message}</span>;
  const spanWide = compact || entry ? "" : "sm:col-span-2";
  const currencySymbol = new Intl.NumberFormat("en", { style: "currency", currency, currencyDisplay: "narrowSymbol" }).formatToParts(0).find((part) => part.type === "currency")?.value ?? currency;

  const feedback = <div ref={feedbackRef}>
    {banner}
    {(problem || taxonomyError) ? <p role="alert" className={cn("rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note text-danger-ink", compact ? "mb-3" : "mb-4")}>{problem || taxonomyError}</p> : null}
    {taxonomyPending ? <p role="status" className="mb-3 flex items-center gap-2 text-note text-ink-muted"><Loader2 size={14} className="animate-spin" />Adding {taxonomyPending}…</p> : null}
  </div>;

  const amountInput = <input id={`${formId}-amount`} ref={amountInputRef} disabled={disabled} aria-label="Transaction amount" aria-required="true" aria-invalid={Boolean(amountError)} aria-describedby={amountError ? amountErrorId : undefined} inputMode="decimal" value={amount} onChange={(event) => { setAmount(event.target.value); if (amountError) setAmountError(null); }} placeholder={entry ? "0.00" : compact ? "1,500" : undefined} className={cn(entry ? "manual-field min-w-0 w-full bg-transparent font-semibold text-ink outline-none placeholder:text-ink-muted/60 disabled:opacity-50" : inputClass, amountError && "manual-field-danger border-danger-line")} />;
  const amountField = entry
    ? <div className={cn("manual-field-group block rounded-2xl border border-secondary-line bg-secondary-tint/50 p-4 transition-colors focus-within:border-secondary sm:p-5", amountError && "border-danger-line")}>
      <div className="flex items-center justify-between gap-3 text-note font-medium text-ink-body"><label htmlFor={`${formId}-amount`}>Amount <span className="font-normal text-ink-muted">required</span></label><CurrencySelect label="Transaction currency" value={currency} onChange={setCurrency} disabled={disabled} /></div>
      <span data-field className="mt-2 flex items-baseline gap-2 font-heading text-[2.75rem] leading-tight sm:text-[3.25rem]"><span aria-hidden className="text-secondary">{currencySymbol}</span>{amountInput}</span>
      {amountError ? fieldError(amountError, amountErrorId) : null}
      {currency !== defaultCurrency ? <p className="mt-2 text-meta text-ink-muted">Saved in {currency}. Your overview totals use {defaultCurrency}; this amount won’t be converted or included in those totals.</p> : null}
    </div>
    : <label className={fieldClass}>{label("Amount", currency)}{amountInput}{amountError ? fieldError(amountError, amountErrorId) : null}</label>;
  const merchantField = shown.has("merchant") ? <label className={cn(fieldClass, spanWide)}>{label("Merchant", compact || entry ? "optional" : undefined)}<input disabled={disabled} aria-label="Merchant" value={merchant} maxLength={160} onChange={(event) => setMerchant(event.target.value)} placeholder={entry ? (transactionType === "income" ? "e.g. Employer or client" : "e.g. Cafe or store") : compact ? "Where you paid" : undefined} className={inputClass} /></label> : null;
  const dateField = shown.has("transaction_at") ? <label className={cn(fieldClass, spanWide)}>{label("Date and time", timeZone)}<input disabled={disabled} aria-label={`Transaction date and time${timeZone ? ` in ${timeZone}` : ""}`} aria-invalid={Boolean(transactionAtError)} aria-describedby={transactionAtError ? transactionAtErrorId : undefined} type="datetime-local" value={transactionAt} onChange={(event) => { setTransactionAt(event.target.value); if (transactionAtError) setTransactionAtError(null); }} className={cn(inputClass, transactionAtError && "manual-field-danger border-danger-line")} />{transactionAtError ? fieldError(transactionAtError, transactionAtErrorId) : null}</label> : null;
  const typeField = shown.has("transaction_type") ? <div className={fieldClass}>{label("Type")}<Combobox aria-label="Transaction type" disabled={disabled} value={transactionType} onValueChange={changeType} options={transactionTypes.map((type) => ({ value: type, label: compact ? type.replaceAll("_", " ") : titleCase(type) }))} searchable={false} triggerClassName={triggerClass} /></div> : null;
  const locationField = shown.has("location") ? <div className={fieldClass}><label>{label("Location", "optional")}<input disabled={disabled} aria-label="Transaction location" value={location} maxLength={160} onChange={(event) => { locationTouched.current = true; setLocation(event.target.value); }} placeholder="City or place" className={inputClass} /></label>{coordinateHint ? entry
    ? <details className="mt-2 text-meta font-normal text-ink-muted"><summary tabIndex={0} className="flex min-h-11 cursor-pointer list-none items-center gap-1.5"><MapPin size={14} aria-hidden />Device location attached<ChevronDown size={12} aria-hidden className="ml-auto" /></summary><p className="pb-2">Coordinates {coordinateHint}</p></details>
    : <span aria-live="polite" className="mt-1.5 block text-meta font-normal text-ink-muted">Coordinates {coordinateHint}</span> : null}</div> : null;
  const natureField = shown.has("spend_nature") && transactionType === "expense" ? <div className={fieldClass}>{label("Spend nature", entry ? "optional" : undefined)}<Combobox aria-label="Spend nature" disabled={disabled} value={spendNature} onValueChange={(next) => setSpendNature(next as TransactionListItemOut["spendNature"])} options={[{ value: "unknown", label: "Not set" }, { value: "essential", label: "Essential" }, { value: "discretionary", label: "Discretionary" }, { value: "potentially_avoidable", label: "Potentially avoidable" }]} triggerClassName={triggerClass} /></div> : null;
  const tagsField = shown.has("tags") ? <label className={cn(fieldClass, "sm:col-span-2")}>{label("Tags", "comma separated")}<input disabled={disabled} aria-label="Transaction tags" value={tags} onChange={(event) => setTags(event.target.value)} placeholder="vacation, family, reimbursable" className={inputClass} /></label> : null;
  const categoryField = transactionType === "expense" && shown.has("category") ? <div className={fieldClass}>{label("Category", entry && !needsTaxonomy ? "optional" : undefined)}<Combobox aria-label="Transaction category" disabled={disabled || Boolean(taxonomyPending)} value={categoryId} onValueChange={(next) => { setCategoryId(next); setSubcategoryId(""); setTaxonomyError(null); }} placeholder={entry ? "Choose" : compact ? "Choose category" : undefined} options={categories.map((category) => ({ value: category.id, label: category.label }))} searchPlaceholder="Search or add new" onCreate={onCreateCategory ? (name) => void addCategory(name) : undefined} createHint="New category" triggerClassName={triggerClass} /></div> : null;
  const subcategoryOptions = subcategories.map((subcategory) => ({ value: subcategory.id, label: subcategory.label }));
  if (!compact && (!entry || categoryId)) subcategoryOptions.unshift({ value: "", label: entry ? "None" : "No subcategory" });
  const subcategoryField = transactionType === "expense" && shown.has("subcategory") ? <div className={fieldClass}>{label("Subcategory", entry && !needsTaxonomy ? "optional" : undefined)}<Combobox aria-label="Transaction subcategory" disabled={disabled || Boolean(taxonomyPending) || !categoryId} value={subcategoryId} onValueChange={(next) => { setSubcategoryId(next); setTaxonomyError(null); }} placeholder={entry ? "Category first" : categoryId ? (compact ? "Choose subcategory" : "No subcategory") : (compact ? "Choose a category first" : "Choose category first")} options={subcategoryOptions} searchPlaceholder="Search or add new" onCreate={onCreateSubcategory && categoryId ? (name) => void addSubcategory(name) : undefined} createHint={`New in ${categories.find((category) => category.id === categoryId)?.label ?? "this category"}`} triggerClassName={triggerClass} /></div> : null;
  const otherTypes = transactionTypes.filter((type) => type !== "expense" && type !== "income");
  const otherTypeSelected = otherTypes.some((type) => type === transactionType);

  const entryFields = <>
    {shown.has("transaction_type") ? <div role="group" aria-label="Transaction type" className="mb-4 flex gap-1 rounded-xl border border-line bg-sunken p-1">
      {(["expense", "income"] as const).filter((type) => transactionTypes.includes(type)).map((type) => {
        const Icon = type === "expense" ? ArrowUpRight : ArrowDownLeft;
        return <button key={type} type="button" aria-pressed={transactionType === type} disabled={disabled} onClick={() => changeType(type)} className={cn("flex min-h-11 min-w-0 flex-1 items-center justify-center gap-1.5 rounded-lg border px-2 text-note font-semibold transition-colors disabled:opacity-50", transactionType === type ? "border-secondary-line bg-surface text-secondary" : "border-transparent text-ink-muted hover:bg-surface/60 hover:text-ink")}><Icon size={16} aria-hidden />{titleCase(type)}</button>;
      })}
      {otherTypes.length ? <div className="min-w-0 flex-1"><Combobox aria-label="More transaction types" disabled={disabled} value={otherTypeSelected ? transactionType : ""} onValueChange={changeType} options={otherTypes.map((type) => ({ value: type, label: titleCase(type) }))} placeholder="More" triggerClassName={cn("h-11 justify-center rounded-lg px-2 text-note font-semibold", otherTypeSelected ? "border-secondary-line bg-surface text-secondary" : "border-transparent bg-transparent text-ink-muted hover:bg-surface/60")} /></div> : null}
    </div> : null}
    {amountField}
    <div className="mt-4 space-y-4 rounded-2xl border border-line bg-surface p-4 sm:p-5">
      {merchantField}
      {categoryField || subcategoryField ? <div className="grid grid-cols-2 gap-3">{categoryField}{subcategoryField}</div> : null}
      {dateField}
    </div>
    {locationField || natureField || tagsField ? <details className="group/details mt-4 rounded-2xl border border-line bg-surface">
      <summary tabIndex={0} className="flex min-h-16 cursor-pointer list-none items-center gap-3 rounded-2xl px-4 py-3 hover:bg-ground sm:px-5">
        <SlidersHorizontal aria-hidden size={18} className="shrink-0 text-ink-muted" /><span className="min-w-0 flex-1"><span className="block text-note font-semibold text-ink">More details</span><span className="block text-meta text-ink-muted">{[shown.has("location") ? "Location" : null, natureField ? "spend nature" : null, tagsField ? "tags" : null].filter(Boolean).join(", ")}{location ? " · Location added" : " · Optional"}</span></span><ChevronDown size={16} aria-hidden className="shrink-0 text-ink-muted transition-transform group-open/details:rotate-180" />
      </summary>
      <div className="grid gap-4 border-t border-line p-4 sm:grid-cols-2 sm:p-5">{locationField}{natureField}{tagsField}</div>
    </details> : null}
  </>;

  return <form ref={formRef} onSubmit={submit} noValidate className={className}>
    {entry ? <div className="panel-scroll min-h-0 flex-1 overflow-y-auto overscroll-contain"><div className="mx-auto w-full max-w-xl px-4 py-5 sm:px-6 sm:py-6">{feedback}{entryFields}{afterFields}</div></div> : <>
      {feedback}
      <div className={cn("grid sm:grid-cols-2", compact ? "gap-3" : "gap-4")}>{amountField}{merchantField}{dateField}{typeField}{locationField}{natureField}{tagsField}{categoryField}{subcategoryField}</div>
      {afterFields}
    </>}
    {renderActions({ blocked, taxonomyPending })}
  </form>;
}
