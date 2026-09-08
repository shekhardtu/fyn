import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Landmark, Loader2, Plus, RotateCcw, SlidersHorizontal, Target, X } from "lucide-react";
import { type FormEvent, useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CurrencySelect } from "@/components/currency-select";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { useWorkspaceOverlay } from "@/components/ui/overlay";
import { toast } from "@/components/ui/toast";
import { useUserDefaults } from "@/components/user-defaults";
import { contributeGoal, createAccount, loadBudgets, loadCategories, loadGoals, saveBudget, saveGoal } from "@/lib/api";
import { formatMoney, parseAmountToMinor } from "@/lib/format";
import type { AccountCreateIn, AccountRecordOut, BudgetRecordOut, BudgetSaveIn, CategoryDirectoryOut, GoalRecordOut, GoalSaveIn } from "@/lib/generated/contracts";
import { cn } from "@/lib/utils";
import type { FinanceFormKind } from "@/routing/paths";

type Submission =
  | { kind: "budget"; values: BudgetSaveIn; id?: string }
  | { kind: "goal"; values: GoalSaveIn; id?: string }
  | { kind: "account"; values: AccountCreateIn }
  | { kind: "contribution"; id: string; amountMinor: number; requestId: string };

const COPY = {
  budget: { title: "Set budget", detail: "Give your monthly spending a limit.", action: "Save budget", saved: "Budget saved", icon: SlidersHorizontal },
  goal: { title: "Set a goal", detail: "Make room for something you’re saving for.", action: "Save goal", saved: "Goal saved", icon: Target },
  account: { title: "Add account", detail: "Record an account and its current balance.", action: "Add account", saved: "Account added", icon: Landmark },
  contribution: { title: "Add savings", detail: "Record your progress towards this goal.", action: "Add savings", saved: "Savings recorded", icon: Target },
};

export function FinanceFormDialog({ kind, id, categoryScope, onClose }: { kind: FinanceFormKind; id?: string; categoryScope?: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [dirty, setDirty] = useState(false);
  const [discard, setDiscard] = useState(false);
  const budgets = useQuery({ queryKey: ["budgets"], queryFn: loadBudgets, enabled: kind === "budget" });
  const categories = useQuery({ queryKey: ["categories"], queryFn: loadCategories, enabled: kind === "budget" });
  const goals = useQuery({ queryKey: ["goals"], queryFn: loadGoals, enabled: kind === "goal" || kind === "contribution" });
  const save = useMutation({
    mutationFn: async (submission: Submission): Promise<BudgetRecordOut | GoalRecordOut | AccountRecordOut> => {
      switch (submission.kind) {
        case "budget": return saveBudget(submission.values, submission.id);
        case "goal": return saveGoal(submission.values, submission.id);
        case "account": return createAccount(submission.values);
        case "contribution": return contributeGoal(submission.id, submission.amountMinor, submission.requestId);
      }
    },
    onSuccess: () => {
      for (const key of ["overview", "budgets", "goals", "accounts"]) void queryClient.invalidateQueries({ queryKey: [key] });
      toast.add({ title: COPY[kind].saved, type: "success", timeout: 4000 });
      onClose();
    },
  });
  const closeBehavior = useRef(() => {});
  useEffect(() => {
    closeBehavior.current = () => {
      if (save.isPending) return;
      if (discard) { setDiscard(false); return; }
      if (dirty) { setDiscard(true); return; }
      onClose();
    };
  });
  const requestClose = useCallback(() => closeBehavior.current(), []);
  const panelRef = useWorkspaceOverlay(true, requestClose);
  const budget = budgets.data?.find((item) => item.id === id);
  const goal = goals.data?.find((item) => item.id === id);
  const queries = kind === "budget" ? [budgets, categories] : kind === "goal" || kind === "contribution" ? [goals] : [];
  const loading = queries.some((query) => query.isPending);
  const failed = queries.some((query) => query.isError);
  const missing = !loading && !failed && ((id && kind === "budget" && !budget) || (id && (kind === "goal" || kind === "contribution") && !goal) || (kind === "contribution" && !id));
  const copy = COPY[kind];
  const title = id && (kind === "budget" || kind === "goal") ? `Edit ${kind}` : copy.title;
  const Icon = copy.icon;
  return createPortal(<section ref={panelRef} role="dialog" aria-modal="true" aria-labelledby="finance-form-title" className="money-entry-screen fixed inset-x-0 z-[55] flex w-full flex-col bg-ground">
    <header className="shrink-0 border-b border-line bg-surface">
      <div className="mx-auto flex w-full max-w-xl items-center gap-3 px-4 pt-[max(1rem,env(safe-area-inset-top))] pb-4 sm:px-6">
        <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-secondary-tint text-secondary"><Icon size={19} aria-hidden /></span>
        <div className="min-w-0 flex-1"><h2 id="finance-form-title" tabIndex={-1} data-overlay-initial-focus className="font-heading text-title font-semibold text-ink outline-none">{title}</h2><p className="text-note text-ink-muted">{copy.detail}</p></div>
        <Button type="button" variant="ghost" size="icon-lg" disabled={save.isPending} aria-label={`Close ${kind} form`} onClick={requestClose}><X /></Button>
      </div>
    </header>
    {loading || failed || missing ? <div className="mx-auto w-full max-w-xl px-4 py-8 sm:px-6">
      {loading ? <p role="status" className="flex items-center gap-2 text-note text-ink-muted"><Loader2 size={16} className="animate-spin" />Loading your details…</p> : <>
        <p role="alert" className="text-note text-danger-ink">{missing ? `This ${kind} is no longer available.` : "We couldn’t load the details for this form."}</p>
        {!missing ? <Button type="button" variant="outline" className="mt-4 min-h-12" onClick={() => queries.forEach((query) => void query.refetch())}><RotateCcw />Try again</Button> : null}
      </>}
    </div> : <FinanceEntry key={`${kind}:${id ?? "new"}`} kind={kind} budget={budget} budgets={budgets.data ?? []} goal={goal} categories={categories.data ?? []} categoryScope={categoryScope} saving={save.isPending} problem={save.error?.message} discard={discard} onDirty={setDirty} onDiscard={onClose} onKeep={() => setDiscard(false)} onClose={requestClose} onSubmit={(submission) => save.mutate(submission)} />}
  </section>, document.body);
}

const inputClass = "manual-field mt-2 block h-12 w-full min-w-0 rounded-xl border border-line-strong bg-ground/60 px-3.5 text-control text-ink outline-none focus:border-secondary disabled:opacity-60";
const labelClass = "block min-w-0 text-note font-medium text-ink-body";

function FinanceEntry({ kind, budget, budgets, goal, categories, categoryScope, saving, problem, discard, onDirty, onDiscard, onKeep, onClose, onSubmit }: {
  kind: FinanceFormKind; budget?: BudgetRecordOut; budgets: BudgetRecordOut[]; goal?: GoalRecordOut; categories: CategoryDirectoryOut[]; categoryScope?: boolean;
  saving: boolean; problem?: string; discard: boolean; onDirty: (value: boolean) => void; onDiscard: () => void; onKeep: () => void; onClose: () => void; onSubmit: (submission: Submission) => void;
}) {
  const defaults = useUserDefaults();
  const initialBudget = kind === "budget" ? budget ?? (!categoryScope ? budgets.find((item) => item.currency === defaults.currency && item.categoryId === null) : undefined) : undefined;
  const [currency, setCurrency] = useState(budget?.currency ?? goal?.currency ?? defaults.currency);
  const [scope, setScope] = useState(budget ? budget.categoryId ?? "overall" : categoryScope ? "" : "overall");
  const [amount, setAmount] = useState(initialBudget ? String(initialBudget.amountMinor / 100) : goal && kind === "goal" ? String(goal.targetMinor / 100) : "");
  const [name, setName] = useState(initialBudget?.name ?? goal?.name ?? "");
  const [targetDate, setTargetDate] = useState(goal?.targetDate ?? "");
  const [accountType, setAccountType] = useState<AccountCreateIn["accountType"]>("bank");
  const [owed, setOwed] = useState(false);
  const [institution, setInstitution] = useState("");
  const [mask, setMask] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [errorField, setErrorField] = useState<string | null>(null);
  const [requestId] = useState(() => crypto.randomUUID());
  const formRef = useRef<HTMLFormElement>(null);
  const feedbackRef = useRef<HTMLDivElement>(null);
  const uid = useId();
  const [opened] = useState(JSON.stringify({ currency, scope, amount, name, targetDate, accountType, owed, institution, mask }));
  const dirty = opened !== JSON.stringify({ currency, scope, amount, name, targetDate, accountType, owed, institution, mask });
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
  useEffect(() => {
    if (problem || discard || error) feedbackRef.current?.scrollIntoView?.({ block: "nearest" });
    if (errorField) formRef.current?.querySelector<HTMLElement>(`[name="${errorField}"]`)?.focus();
  }, [problem, discard, error, errorField]);
  const existingBudget = budget ?? budgets.find((item) => item.currency === currency && item.categoryId === (scope === "overall" ? null : scope));
  function changeScope(next: string) {
    const existing = budgets.find((item) => item.currency === currency && item.categoryId === (next === "overall" ? null : next));
    setScope(next);
    setName(existing?.name ?? "");
    setAmount(existing ? String(existing.amountMinor / 100) : "");
    setError(null);
  }
  function reject(message: string, field: string) { setError(message); setErrorField(field); }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (saving) return;
    const zero = kind === "account" && /^0+(\.0{1,2})?$/.test(amount.trim());
    const parsed = zero ? 0 : parseAmountToMinor(amount);
    if (parsed === null || !Number.isSafeInteger(parsed) || (!zero && parsed <= 0)) return reject(kind === "account" ? "Enter a valid balance, including 0 if this account is empty." : "Enter an amount greater than zero.", "amount");
    if ((kind === "goal" || kind === "account") && !name.trim()) return reject(kind === "goal" ? "Give your goal a name." : "Enter an account name.", "name");
    if (kind === "budget" && !scope) return reject("Choose the category for this budget.", "scope");
    setError(null);
    setErrorField(null);
    switch (kind) {
      case "budget": onSubmit({ kind, id: existingBudget?.id, values: { amountMinor: parsed, categoryId: scope === "overall" ? null : scope, name: name.trim() || (scope === "overall" ? "Monthly spending budget" : `${categories.find((category) => category.id === scope)?.label ?? "Category"} budget`) } }); break;
      case "goal": onSubmit({ kind, id: goal?.id, values: { name: name.trim(), targetMinor: parsed, targetDate: targetDate || null } }); break;
      case "account": onSubmit({ kind, values: { name: name.trim(), accountType, currency, balanceMinor: owed ? -parsed : parsed, institution: institution.trim() || null, mask: mask.trim() || null } }); break;
      case "contribution": if (goal) onSubmit({ kind, id: goal.id, amountMinor: parsed, requestId }); break;
    }
  }
  const amountLabel = kind === "budget" ? "Monthly limit" : kind === "goal" ? "Target amount" : kind === "account" ? "Current balance" : "Amount saved";
  const symbol = new Intl.NumberFormat("en", { style: "currency", currency, currencyDisplay: "narrowSymbol" }).formatToParts(0).find((part) => part.type === "currency")?.value ?? currency;
  return <form ref={formRef} noValidate onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
    <div className="panel-scroll min-h-0 flex-1 overflow-y-auto overscroll-contain"><div className="mx-auto w-full max-w-xl space-y-4 px-4 py-5 sm:px-6 sm:py-6">
      <div ref={feedbackRef} hidden={!discard && !error && !problem}>
        {discard ? <div role="alertdialog" aria-label="Discard unsaved changes" className="mb-4 rounded-xl border border-attention/40 bg-attention-tint p-4 text-note text-ink-body">You have unsaved changes. Discard them?<div className="mt-3 flex gap-3"><Button type="button" variant="destructive" className="min-h-11" onClick={onDiscard}>Discard</Button><Button type="button" variant="ghost" className="min-h-11" onClick={onKeep}>Keep editing</Button></div></div> : null}
        {error || problem ? <p id={`${uid}-error`} role="alert" className="rounded-xl border border-danger-line bg-danger-tint px-4 py-3 text-note text-danger-ink">{error || problem}</p> : null}
      </div>
      {kind === "budget" ? <div className={labelClass}>Budget for<Combobox aria-label="Budget scope" value={scope} onValueChange={changeScope} disabled={saving || Boolean(budget)} options={[{ value: "overall", label: "All spending" }, ...categories.map((category) => ({ value: category.id, label: category.label }))]} placeholder="Choose a category" triggerClassName={cn(inputClass, "flex")} /><p className="mt-2 text-meta font-normal text-ink-muted">Repeats every month until you change it. Applies to expenses in {currency}.</p>{existingBudget && !budget ? <p className="mt-2 text-note text-secondary">Saving will update your existing limit for this scope.</p> : null}</div> : null}
      {kind === "contribution" && goal ? <div className="rounded-xl border border-line bg-surface p-4"><p className="font-semibold text-ink">{goal.name}</p><p className="mt-1 text-note text-ink-muted">{formatMoney(goal.currentMinor, currency)} saved of {formatMoney(goal.targetMinor, currency)}</p></div> : null}
      <div className="manual-field-group rounded-2xl border border-secondary-line bg-secondary-tint/50 p-4 sm:p-5">
        <div className="flex items-center justify-between gap-3"><label htmlFor={`${uid}-amount`} className="text-note font-medium text-ink-body">{amountLabel} <span className="font-normal text-ink-muted">required</span></label>{kind === "account" ? <CurrencySelect value={currency} onChange={setCurrency} disabled={saving} label="Account currency" /> : <span className="text-note font-semibold text-secondary">{currency}</span>}</div>
        <div data-field className="mt-3 flex items-baseline gap-2 font-heading text-[2.75rem] leading-tight"><span aria-hidden className="shrink-0 whitespace-nowrap text-secondary">{owed && kind === "account" ? "−" : ""}{symbol}</span><input id={`${uid}-amount`} name="amount" aria-label={amountLabel} aria-required="true" aria-invalid={errorField === "amount"} aria-describedby={errorField === "amount" ? `${uid}-error` : undefined} inputMode="decimal" value={amount} onChange={(event) => { setAmount(event.target.value); setError(null); setErrorField(null); }} placeholder="0.00" disabled={saving} className="manual-field w-full min-w-0 bg-transparent font-semibold text-ink outline-none placeholder:text-ink-muted/60" /></div>
        {kind === "account" ? <div role="group" aria-label="Balance direction" className="mt-3 flex gap-2">{[false, true].map((value) => <button key={String(value)} type="button" disabled={saving} aria-pressed={owed === value} onClick={() => setOwed(value)} className={cn("min-h-11 flex-1 rounded-lg border px-3 text-note font-medium", owed === value ? "border-secondary-line bg-surface text-secondary" : "border-transparent text-ink-muted")}>{value ? "Money owed" : "Available balance"}</button>)}</div> : null}
      </div>
      {kind === "goal" || kind === "account" ? <div className="space-y-4 rounded-2xl border border-line bg-surface p-4 sm:p-5">
        <label className={labelClass}>{kind === "goal" ? "Goal name" : "Account name"} <span className="font-normal text-ink-muted">required</span><input name="name" aria-required="true" aria-label={kind === "goal" ? "Goal name" : "Account name"} aria-invalid={errorField === "name"} aria-describedby={errorField === "name" ? `${uid}-error` : undefined} value={name} onChange={(event) => { setName(event.target.value); setError(null); setErrorField(null); }} maxLength={120} placeholder={kind === "goal" ? "e.g. Emergency fund" : "e.g. Everyday account"} disabled={saving} className={inputClass} /></label>
        {kind === "goal" ? <label className={labelClass}>Target date <span className="font-normal text-ink-muted">optional</span><input type="date" aria-label="Goal target date" value={targetDate} onChange={(event) => setTargetDate(event.target.value)} disabled={saving} className={inputClass} /></label> : <div className={labelClass}>Account type<Combobox aria-label="Account type" disabled={saving} value={accountType} onValueChange={(value) => setAccountType(value as AccountCreateIn["accountType"])} options={[{ value: "bank", label: "Bank account" }, { value: "savings", label: "Savings account" }, { value: "checking", label: "Checking / current account" }, { value: "credit_card", label: "Credit card" }, { value: "cash", label: "Cash" }, { value: "wallet", label: "Digital wallet" }, { value: "investment", label: "Investment account" }, { value: "other", label: "Other" }]} triggerClassName={cn(inputClass, "flex")} /></div>}
      </div> : null}
      {kind === "budget" || kind === "account" ? <details className="group rounded-2xl border border-line bg-surface"><summary tabIndex={0} className="flex min-h-14 cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-note font-semibold text-ink sm:px-5">More details <span className="ml-auto text-meta font-normal text-ink-muted">Optional</span><ChevronDown size={16} className="group-open:rotate-180" aria-hidden /></summary><div className="space-y-4 border-t border-line p-4 sm:p-5">
        {kind === "budget" ? <label className={labelClass}>Budget name<input aria-label="Budget name" value={name} onChange={(event) => setName(event.target.value)} maxLength={120} disabled={saving} placeholder="Monthly spending budget" className={inputClass} /></label> : <>
          <label className={labelClass}>Bank or institution<input aria-label="Bank or institution" value={institution} onChange={(event) => setInstitution(event.target.value)} maxLength={120} disabled={saving} className={inputClass} /></label>
          <label className={labelClass}>Last four digits<input aria-label="Last four digits" value={mask} onChange={(event) => setMask(event.target.value.replace(/\D/g, "").slice(0, 4))} inputMode="numeric" maxLength={4} disabled={saving} placeholder="e.g. 1234" className={inputClass} /></label>
        </>}
      </div></details> : null}
      {kind === "account" ? <p className="text-note text-ink-muted">This records the balance you enter. It doesn’t connect to your bank or refresh automatically.</p> : kind === "contribution" ? <p className="text-note text-ink-muted">This records savings progress. It doesn’t move money between accounts or create an expense.</p> : kind === "goal" ? <p className="text-note text-ink-muted">Your goal is tracked in {currency}. Add savings as you make progress.</p> : null}
    </div></div>
    <footer className="shrink-0 border-t border-line bg-surface"><div className="mx-auto flex w-full max-w-xl gap-3 px-4 pt-3 pb-[max(1rem,env(safe-area-inset-bottom))] sm:px-6">
      <Button type="submit" disabled={saving || !amount.trim() || ((kind === "goal" || kind === "account") && !name.trim()) || (kind === "budget" && !scope)} className="h-12 min-w-0 flex-1 rounded-xl">{saving ? <Loader2 className="animate-spin" /> : <Plus />}{saving ? "Saving…" : COPY[kind].action}</Button>
      <Button type="button" variant="ghost" disabled={saving} onClick={onClose} className="h-12 px-5">Cancel</Button>
    </div></footer>
  </form>;
}
