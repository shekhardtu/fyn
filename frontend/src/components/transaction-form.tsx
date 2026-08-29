import { Loader2, TriangleAlert } from "lucide-react";
import { type FormEvent, type ReactNode, type Ref, useEffect, useId, useRef, useState } from "react";
import { Combobox } from "@/components/ui/combobox";
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
  density?: "page" | "compact";
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
  const { currency, timeZone } = useUserDefaults();
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
  const amountErrorId = `${formId}-amount-error`;
  const transactionAtErrorId = `${formId}-time-error`;
  const [opened] = useState({ amount, merchant, transactionAt, transactionType, categoryId, subcategoryId, spendNature, location, tags });
  const deviceFix = useDeviceLocation(captureDeviceLocation && locationAllowed && shown.has("location"));
  const coordinateHint = deviceFix
    ? `${deviceFix.latitude.toFixed(6)}, ${deviceFix.longitude.toFixed(6)}${deviceFix.locationAccuracy === null ? "" : ` · accuracy ±${deviceFix.locationAccuracy} m`}`
    : null;
  const subcategories = categories.find((category) => category.id === categoryId)?.subcategories ?? [];
  const needsTaxonomy = requireExpenseTaxonomy && transactionType === "expense" && shown.has("category") && categories.length > 0;
  const blocked = disabled || Boolean(taxonomyPending) || !amount.trim() || (needsTaxonomy && (!categoryId || (shown.has("subcategory") && !subcategoryId)));
  const compact = density === "compact";

  const current = { amount, merchant, transactionAt, transactionType, categoryId, subcategoryId, spendNature, location, tags };
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

  const inputClass = cn(
    "manual-field block h-[var(--h-field)] w-full rounded-lg border border-line-strong bg-surface px-3 text-ink outline-none transition-colors disabled:opacity-50",
    compact ? "text-body duration-[110ms] ease-linear" : "mt-1 text-control",
  );
  const fieldClass = compact ? "block" : "text-note font-medium text-ink-body";
  const label = (text: string, hint?: string) => compact
    ? <span className="mb-2 block text-note font-medium text-ink-muted">{text}{hint ? <span className="ml-1 font-normal text-ink-muted/80">{hint}</span> : null}</span>
    : <>{text}{hint ? <span className="ml-1 font-normal text-ink-muted">{hint}</span> : null}</>;
  const fieldError = (message: string, id?: string) => <span id={id} role="alert" className="mt-1 flex items-center gap-1 text-meta font-medium text-danger-ink"><TriangleAlert size={14} />{message}</span>;
  const spanWide = compact ? "" : "sm:col-span-2";

  return <form onSubmit={submit} noValidate className={className}>
    {banner}
    {(problem || taxonomyError) ? <p role="alert" className={cn("rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note text-danger-ink", compact ? "mb-3" : "mb-4")}>{problem || taxonomyError}</p> : null}
    {taxonomyPending ? <p role="status" className="mb-3 flex items-center gap-2 text-note text-ink-muted"><Loader2 size={14} className="animate-spin" />Adding {taxonomyPending}…</p> : null}
    <div className={cn("grid sm:grid-cols-2", compact ? "gap-3" : "gap-4")}>
      <label className={fieldClass}>{label("Amount", currency)}<input ref={amountInputRef} disabled={disabled} aria-label="Transaction amount" aria-invalid={Boolean(amountError)} aria-describedby={amountError ? amountErrorId : undefined} inputMode="decimal" value={amount} onChange={(event) => { setAmount(event.target.value); if (amountError) setAmountError(null); }} placeholder={compact ? "1,500" : undefined} className={cn(inputClass, amountError && "manual-field-danger border-danger-line")} />{amountError ? fieldError(amountError, amountErrorId) : null}</label>
      {shown.has("merchant") ? <label className={cn(fieldClass, spanWide)}>{label("Merchant", compact ? "optional" : undefined)}<input disabled={disabled} aria-label="Merchant" value={merchant} maxLength={160} onChange={(event) => setMerchant(event.target.value)} placeholder={compact ? "Where you paid" : undefined} className={inputClass} /></label> : null}
      {shown.has("transaction_at") ? <label className={cn(fieldClass, spanWide)}>{label("Date and time", timeZone)}<input disabled={disabled} aria-label={`Transaction date and time${timeZone ? ` in ${timeZone}` : ""}`} aria-invalid={Boolean(transactionAtError)} aria-describedby={transactionAtError ? transactionAtErrorId : undefined} type="datetime-local" value={transactionAt} onChange={(event) => { setTransactionAt(event.target.value); if (transactionAtError) setTransactionAtError(null); }} className={cn(inputClass, transactionAtError && "manual-field-danger border-danger-line")} />{transactionAtError ? fieldError(transactionAtError, transactionAtErrorId) : null}</label> : null}
      {shown.has("transaction_type") ? <div className={fieldClass}>{label("Type")}<Combobox aria-label="Transaction type" disabled={disabled} value={transactionType} onValueChange={(next) => {
        setTransactionType(next as TransactionListItemOut["transactionType"]);
        if (next !== "expense" || !categories.some((item) => item.id === categoryId)) {
          setCategoryId("");
          setSubcategoryId("");
        }
        if (next !== "expense") setSpendNature("unknown");
      }} options={transactionTypes.map((type) => ({ value: type, label: compact ? type.replaceAll("_", " ") : titleCase(type) }))} searchable={false} triggerClassName={compact ? "text-body" : "mt-1"} /></div> : null}
      {shown.has("location") ? <label className={fieldClass}>{label("Location", "optional")}<input disabled={disabled} aria-label="Transaction location" value={location} maxLength={160} onChange={(event) => { locationTouched.current = true; setLocation(event.target.value); }} placeholder="City or place" className={inputClass} />{coordinateHint ? <span aria-live="polite" className="mt-1.5 block text-meta font-normal text-ink-muted">Coordinates {coordinateHint}</span> : null}</label> : null}
      {shown.has("spend_nature") && transactionType === "expense" ? <div className={fieldClass}>{label("Spend nature")}<Combobox aria-label="Spend nature" disabled={disabled} value={spendNature} onValueChange={(next) => setSpendNature(next as TransactionListItemOut["spendNature"])} options={[{ value: "unknown", label: "Not set" }, { value: "essential", label: "Essential" }, { value: "discretionary", label: "Discretionary" }, { value: "potentially_avoidable", label: "Potentially avoidable" }]} triggerClassName={compact ? "text-body" : "mt-1"} /></div> : null}
      {shown.has("tags") ? <label className={cn(fieldClass, "sm:col-span-2")}>{label("Tags", "comma separated")}<input disabled={disabled} aria-label="Transaction tags" value={tags} onChange={(event) => setTags(event.target.value)} placeholder="vacation, family, reimbursable" className={inputClass} /></label> : null}
      {transactionType === "expense" && shown.has("category") ? <div className={fieldClass}>{label("Category")}<Combobox aria-label="Transaction category" disabled={disabled || Boolean(taxonomyPending)} value={categoryId} onValueChange={(next) => { setCategoryId(next); setSubcategoryId(""); setTaxonomyError(null); }} placeholder={compact ? "Choose category" : undefined} options={categories.map((category) => ({ value: category.id, label: category.label }))} searchPlaceholder="Search or add new" onCreate={onCreateCategory ? (name) => void addCategory(name) : undefined} createHint="New category" triggerClassName={compact ? "text-body" : "mt-1"} /></div> : null}
      {transactionType === "expense" && shown.has("subcategory") ? <div className={fieldClass}>{label("Subcategory")}<Combobox aria-label="Transaction subcategory" disabled={disabled || Boolean(taxonomyPending) || !categoryId} value={subcategoryId} onValueChange={(next) => { setSubcategoryId(next); setTaxonomyError(null); }} placeholder={categoryId ? (compact ? "Choose subcategory" : "No subcategory") : (compact ? "Choose a category first" : "Choose category first")} options={compact ? subcategories.map((subcategory) => ({ value: subcategory.id, label: subcategory.label })) : [{ value: "", label: "No subcategory" }, ...subcategories.map((subcategory) => ({ value: subcategory.id, label: subcategory.label }))]} searchPlaceholder="Search or add new" onCreate={onCreateSubcategory && categoryId ? (name) => void addSubcategory(name) : undefined} createHint={`New in ${categories.find((category) => category.id === categoryId)?.label ?? "this category"}`} triggerClassName={compact ? "text-body" : "mt-1"} /></div> : null}
    </div>
    {afterFields}
    {renderActions({ blocked, taxonomyPending })}
  </form>;
}
