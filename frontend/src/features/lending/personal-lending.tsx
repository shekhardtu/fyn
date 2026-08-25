import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronRight,
  Clock3,
  Copy,
  FileClock,
  FileText,
  HandCoins,
  IndianRupee,
  Loader2,
  Mail,
  MessageCircleMore,
  PencilLine,
  Phone,
  Plus,
  ReceiptIndianRupee,
  RotateCcw,
  Search,
  ShieldCheck,
  UserRoundCheck,
  UsersRound,
  X,
} from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { Button } from "@/components/ui/button";
import { SiteHeader } from "@/components/ui/site-header";
import { useWorkspaceOverlay } from "@/components/ui/overlay";
import { useWorkspaceShell } from "@/components/workspace";
import {
  acceptPersonalLoan,
  closePersonalLoan,
  confirmPersonalLoanPayment,
  createPersonalLoan,
  getAuthStatus,
  loadLoanInvitation,
  loadPersonalLoan,
  loadPersonalLoans,
  loadSharedDocumentRevisions,
  proposePersonalLoanTerms,
  recordPersonalLoanPayment,
  redeemLoanInvitation,
  searchContacts,
  sendPersonalLoanReminder,
} from "@/lib/api";
import { formatInstant, formatMoney, parseAmountToMinor } from "@/lib/format";
import type {
  CreatePersonalLoanIn,
  ContactSuggestionOut,
  DocumentRevisionOut,
  PersonalLoanDetailOut,
  PersonalLoanSummaryOut,
} from "@/lib/protocol";
import { cn } from "@/lib/utils";
import { appPaths } from "@/routing/paths";


const dateLabel = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", year: "numeric" });

function localDate(offsetDays = 0) {
  const value = new Date();
  value.setDate(value.getDate() + offsetDays);
  return new Date(value.getTime() - value.getTimezoneOffset() * 60_000).toISOString().slice(0, 10);
}

function formatDate(value: string) {
  const parsed = new Date(`${value}T00:00:00`);
  return Number.isNaN(parsed.valueOf()) ? value : dateLabel.format(parsed);
}

function useDebouncedValue<T>(value: T, delayMs = 250) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [delayMs, value]);
  return debounced;
}

function titleCase(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function PageHeader({ title, subtitle, end }: { title: string; subtitle: ReactNode; end?: ReactNode }) {
  const shell = useWorkspaceShell();
  return <SiteHeader title={title} subtitle={subtitle} navOpen={shell.navOpen} onOpenNav={shell.openNav} end={end} />;
}

function TrustNote({ compact = false }: { compact?: boolean }) {
  return <div className={cn("flex items-start gap-3 rounded-xl border border-secondary-line bg-secondary-tint text-ink-body", compact ? "px-4 py-3" : "p-4 sm:p-5")}>
    <ShieldCheck className="mt-0.5 shrink-0 text-secondary" size={compact ? 18 : 21} />
    <div>
      <p className="text-control font-semibold text-ink">A shared record, in plain language</p>
      <p className="mt-1 text-note leading-5 text-ink-muted">Fyn records what both people agreed, keeps every revision, and sends reminders. It does not move money, hold collateral, or decide a dispute.</p>
    </div>
  </div>;
}

function QueryProblem({ message, retry }: { message: string; retry: () => void }) {
  return <div role="alert" className="rounded-xl border border-danger-line bg-surface px-5 py-10 text-center">
    <p className="font-heading text-title font-semibold text-ink">{message}</p>
    <p className="mt-2 text-note text-ink-muted">Nothing was changed.</p>
    <Button type="button" variant="outline" className="mt-5" onClick={retry}><RotateCcw /> Try again</Button>
  </div>;
}

function StatusBadge({ status, responseNeeded = false }: { status: string; responseNeeded?: boolean }) {
  if (responseNeeded) return <span className="rounded-full bg-attention-tint px-2.5 py-1 text-meta font-semibold text-attention-ink">Your response</span>;
  const settled = status === "closed";
  const pending = status === "pending_acceptance" || status === "settlement_pending";
  return <span className={cn(
    "rounded-full px-2.5 py-1 text-meta font-semibold",
    settled ? "bg-surface-sunken text-ink-muted" : pending ? "bg-attention-tint text-attention-ink" : "bg-secondary-tint text-secondary-hover",
  )}>{status === "pending_acceptance" ? "Waiting for agreement" : status === "settlement_pending" ? "Ready to close" : titleCase(status)}</span>;
}

function LoanRow({ loan }: { loan: PersonalLoanSummaryOut }) {
  const gave = loan.direction === "lent";
  return <Link to={appPaths.loan(loan.id)} className="group grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 border-b border-line px-4 py-4 transition-colors last:border-0 hover:bg-surface-sunken/70 focus-visible:outline-2 focus-visible:outline-inset focus-visible:outline-ring sm:gap-4 sm:px-5">
    <span className={cn("grid size-10 place-items-center rounded-xl", gave ? "bg-secondary-tint text-secondary" : "bg-ground text-ink-body")}><HandCoins size={19} /></span>
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-2"><p className="truncate text-control font-semibold text-ink">{loan.counterpartyName}</p><StatusBadge status={loan.status} responseNeeded={loan.responseNeeded} /></div>
      <p className="mt-1 truncate text-note text-ink-muted">{gave ? "You gave" : "You received"} · Return by {formatDate(loan.dueDate)}</p>
    </div>
    <div className="flex items-center gap-2 text-right">
      <div><p className="font-heading text-body font-semibold tabular-nums text-ink">{formatMoney(loan.outstandingPrincipalMinor + loan.accruedInterestMinor, loan.currency)}</p><p className="mt-0.5 text-meta text-ink-muted">remaining</p></div>
      <ChevronRight className="text-ink-muted transition-transform group-hover:translate-x-0.5" size={17} />
    </div>
  </Link>;
}

export function PersonalLoansPage() {
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState<"all" | "lent" | "borrowed">("all");
  const query = useQuery({ queryKey: ["personal-loans"], queryFn: loadPersonalLoans });
  const items = query.data?.items.filter((item) => filter === "all" || item.direction === filter) ?? [];

  return <main className="min-h-0 flex-1 overflow-y-auto bg-ground">
    <PageHeader title="Personal lending" subtitle="Clear, shared plans with people you know" end={<Button type="button" onClick={() => setCreating(true)}><Plus /> New plan</Button>} />
    <div className="mx-auto w-full max-w-6xl px-4 py-5 sm:px-6 sm:py-8">
      <TrustNote />

      <section aria-label="Personal lending totals" className="mt-5 grid gap-3 sm:grid-cols-3">
        <div className="rounded-xl border border-line bg-surface p-4 sm:p-5"><p className="ledger-meta">Money I gave</p><p className="mt-2 font-heading text-[1.55rem] font-semibold tracking-[-0.03em] text-ink">{query.data ? formatMoney(query.data.moneyIGaveMinor) : "—"}</p><p className="mt-1 text-note text-ink-muted">Still expected back</p></div>
        <div className="rounded-xl border border-line bg-surface p-4 sm:p-5"><p className="ledger-meta">Money I received</p><p className="mt-2 font-heading text-[1.55rem] font-semibold tracking-[-0.03em] text-ink">{query.data ? formatMoney(query.data.moneyIReceivedMinor) : "—"}</p><p className="mt-1 text-note text-ink-muted">Still expected to return</p></div>
        <div className="rounded-xl border border-line bg-surface p-4 sm:p-5"><p className="ledger-meta">Needs my response</p><p className="mt-2 font-heading text-[1.55rem] font-semibold tracking-[-0.03em] text-ink">{query.data?.needsResponseCount ?? "—"}</p><p className="mt-1 text-note text-ink-muted">Agreements or confirmations</p></div>
      </section>

      <section className="mt-6">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="font-heading text-title font-semibold text-ink">Shared plans</h2>
          <div role="tablist" aria-label="Filter personal loans" className="flex rounded-lg bg-surface-sunken p-1">
            {(["all", "lent", "borrowed"] as const).map((option) => <button key={option} role="tab" aria-selected={filter === option} onClick={() => setFilter(option)} className={cn("hit-target rounded-md px-3 py-1.5 text-note font-medium", filter === option ? "bg-surface text-ink shadow-sm" : "text-ink-muted")}>{option === "all" ? "All" : option === "lent" ? "I gave" : "I received"}</button>)}
          </div>
        </div>
        {query.isPending ? <div role="status" className="rounded-xl border border-line bg-surface p-8 text-center text-note text-ink-muted"><Loader2 className="mx-auto mb-3 animate-spin" />Loading shared plans…</div>
          : query.isError ? <QueryProblem message="Your personal loans couldn’t be loaded" retry={() => { void query.refetch(); }} />
          : items.length ? <div className="overflow-hidden rounded-xl border border-line bg-surface">{items.map((loan) => <LoanRow key={loan.id} loan={loan} />)}</div>
          : <div className="rounded-xl border border-dashed border-line-strong bg-surface px-5 py-12 text-center"><span className="mx-auto grid size-12 place-items-center rounded-2xl bg-secondary-tint text-secondary"><UsersRound /></span><h3 className="mt-4 font-heading text-title font-semibold text-ink">{filter === "all" ? "No shared plans yet" : "Nothing in this view"}</h3><p className="mx-auto mt-2 max-w-md text-note leading-5 text-ink-muted">Create a calm, written record when money changes hands with a friend, relative, or colleague.</p>{filter === "all" ? <Button type="button" className="mt-5" onClick={() => setCreating(true)}><Plus /> Create your first plan</Button> : null}</div>}
      </section>
    </div>
    {creating ? <CreateLoanDrawer onClose={() => setCreating(false)} /> : null}
  </main>;
}

type CreateDraft = {
  direction: "lent" | "borrowed";
  counterpartyName: string;
  inviteChannel: "phone" | "email";
  inviteValue: string;
  amount: string;
  moneyDate: string;
  dueDate: string;
  interestPercent: string;
  note: string;
  securityKind: "none" | "gold" | "post_dated_cheque" | "cancelled_cheque" | "document" | "other";
  securityDescription: string;
  securityIdentifier: string;
  securityValue: string;
};

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <label className="block"><span className="text-control font-medium text-ink-body">{label}</span>{children}{hint ? <span className="mt-1.5 block text-meta leading-5 text-ink-muted">{hint}</span> : null}</label>;
}

const fieldClass = "manual-field mt-2 h-11 w-full rounded-lg border border-line-strong bg-surface px-3 text-body text-ink outline-none placeholder:text-ink-muted";

function CreateLoanDrawer({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const panelRef = useWorkspaceOverlay(true, onClose);
  const [problem, setProblem] = useState<string | null>(null);
  const [resolvedContact, setResolvedContact] = useState<ContactSuggestionOut | null>(null);
  const [draft, setDraft] = useState<CreateDraft>({
    direction: "lent",
    counterpartyName: "",
    inviteChannel: "email",
    inviteValue: "",
    amount: "",
    moneyDate: localDate(),
    dueDate: localDate(30),
    interestPercent: "0",
    note: "",
    securityKind: "none",
    securityDescription: "",
    securityIdentifier: "",
    securityValue: "",
  });
  const principalMinor = parseAmountToMinor(draft.amount);
  const rate = Number(draft.interestPercent || 0);
  const days = Math.max((new Date(draft.dueDate).valueOf() - new Date(draft.moneyDate).valueOf()) / 86_400_000, 0);
  const interestMinor = principalMinor && Number.isFinite(rate) ? Math.round(principalMinor * rate / 100 * days / 365) : 0;
  const debouncedIdentifier = useDebouncedValue(draft.inviteValue.trim());
  const identifierHasSearchLength = debouncedIdentifier.replace(/[^a-z0-9]/gi, "").length >= 3;
  const contactQuery = useQuery({
    queryKey: ["contact-suggestions", draft.inviteChannel, debouncedIdentifier],
    queryFn: ({ signal }) => searchContacts(draft.inviteChannel, debouncedIdentifier, signal),
    enabled: identifierHasSearchLength,
    staleTime: 30_000,
    retry: false,
  });
  const currentSuggestions = draft.inviteValue.trim() === debouncedIdentifier ? contactQuery.data ?? [] : [];
  const exactContact = currentSuggestions.find((item) => item.matchKind === "exact") ?? null;
  const matchedContact = resolvedContact ?? exactContact;
  const counterpartyName = draft.counterpartyName.trim() || matchedContact?.displayName || "";
  const valid = Boolean(principalMinor && counterpartyName && draft.inviteValue.trim() && draft.moneyDate && draft.dueDate && draft.dueDate >= draft.moneyDate && rate >= 0 && rate <= 100);
  const identifierLooksComplete = draft.inviteChannel === "email"
    ? /^[^@\s]+@[^@\s.]+\.[^@\s.]+$/.test(draft.inviteValue.trim())
    : draft.inviteValue.replace(/\D/g, "").length >= 10;

  function chooseContact(contact: ContactSuggestionOut) {
    setResolvedContact(contact);
    setDraft((current) => ({
      ...current,
      inviteChannel: contact.channel,
      inviteValue: contact.identifier,
      counterpartyName: contact.displayName,
    }));
  }

  function changeIdentifier(value: string) {
    const previous = resolvedContact;
    setResolvedContact(null);
    setDraft((current) => ({
      ...current,
      inviteValue: value,
      counterpartyName: previous && current.counterpartyName === previous.displayName ? "" : current.counterpartyName,
    }));
  }

  function changeChannel(channel: CreateDraft["inviteChannel"]) {
    setResolvedContact(null);
    setDraft((current) => ({
      ...current,
      inviteChannel: channel,
      inviteValue: "",
      counterpartyName: resolvedContact && current.counterpartyName === resolvedContact.displayName ? "" : current.counterpartyName,
    }));
  }

  const mutation = useMutation({
    mutationFn: (payload: CreatePersonalLoanIn) => createPersonalLoan(payload),
    onMutate: () => setProblem(null),
    onSuccess: ({ loan }) => {
      queryClient.setQueryData(["personal-loan", loan.id], loan);
      void queryClient.invalidateQueries({ queryKey: ["personal-loans"] });
      void queryClient.invalidateQueries({ queryKey: ["overview"] });
      navigate(appPaths.loan(loan.id));
    },
    onError: (cause: Error) => setProblem(cause.message),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!valid || !principalMinor || mutation.isPending) return;
    mutation.mutate({
      direction: draft.direction,
      counterpartyName,
      inviteChannel: draft.inviteChannel,
      inviteValue: draft.inviteChannel === "phone" && !draft.inviteValue.trim().startsWith("+") ? `+91${draft.inviteValue.replace(/\D/g, "")}` : draft.inviteValue.trim(),
      principalMinor,
      currency: "INR",
      moneyDate: draft.moneyDate,
      dueDate: draft.dueDate,
      annualRateBps: Math.round(rate * 100),
      note: draft.note.trim() || null,
      securityItems: draft.securityKind === "none" ? [] : [{
        kind: draft.securityKind,
        description: draft.securityDescription.trim(),
        maskedIdentifier: draft.securityIdentifier.trim() || null,
        statedValueMinor: parseAmountToMinor(draft.securityValue),
      }],
    });
  }

  return <>
    <button type="button" tabIndex={-1} aria-hidden className="fixed inset-0 z-40 bg-scrim/25 backdrop-blur-[2px]" onClick={mutation.isPending ? undefined : onClose} />
    <section ref={panelRef} role="dialog" aria-modal="true" aria-labelledby="create-loan-title" className="drawer-right fixed inset-y-0 right-0 z-50 flex w-full max-w-2xl flex-col border-l border-line bg-surface shadow-[var(--shadow-overlay)]">
      <header className="flex items-center border-b border-line px-4 pt-[max(1rem,env(safe-area-inset-top))] pb-4 sm:px-6">
        <span className="grid size-10 place-items-center rounded-xl bg-secondary-tint text-secondary"><HandCoins size={20} /></span>
        <div className="ml-3"><h2 id="create-loan-title" className="font-heading text-title font-semibold text-ink">Create a shared plan</h2><p className="text-note text-ink-muted">Record first. The other person reviews next.</p></div>
        <Button type="button" variant="ghost" size="icon-lg" className="ml-auto" aria-label="Close" disabled={mutation.isPending} onClick={onClose}><X /></Button>
      </header>

      <form onSubmit={submit} className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6">
        {problem ? <p role="alert" className="mb-5 rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note text-danger-ink">{problem}</p> : null}

        <fieldset><legend className="ledger-meta mb-3">Who is this with?</legend>
          <div role="tablist" aria-label="Find by sign-in method" className="flex w-fit rounded-lg bg-surface-sunken p-1">{(["email", "phone"] as const).map((channel) => <button key={channel} type="button" role="tab" aria-selected={draft.inviteChannel === channel} onClick={() => changeChannel(channel)} className={cn("hit-target flex items-center gap-2 rounded-md px-3 py-1.5 text-note font-medium", draft.inviteChannel === channel ? "bg-surface text-ink shadow-sm" : "text-ink-muted")}>{channel === "email" ? <Mail size={14} /> : <Phone size={14} />}{titleCase(channel)}</button>)}</div>
          <div className="mt-4">
            <Field label={draft.inviteChannel === "email" ? "Email address" : "Phone number"} hint="Search starts after three characters. A complete identifier can match an exact Fyn account.">
              <div className="relative"><Search className="absolute top-[1.3rem] left-3 text-ink-muted" size={16} /><input data-overlay-initial-focus value={draft.inviteValue} onChange={(event) => changeIdentifier(event.target.value)} required type={draft.inviteChannel === "email" ? "email" : "tel"} inputMode={draft.inviteChannel === "email" ? "email" : "tel"} autoComplete={draft.inviteChannel === "email" ? "email" : "tel"} role="combobox" aria-autocomplete="list" aria-expanded={currentSuggestions.length > 0} aria-controls="contact-suggestions" placeholder={draft.inviteChannel === "email" ? "rahul@example.com" : "98765 43210"} className={cn(fieldClass, "pl-9 pr-9")} />{contactQuery.isFetching && identifierHasSearchLength ? <Loader2 aria-label="Searching contacts" className="absolute top-[1.3rem] right-3 animate-spin text-ink-muted" size={16} /> : null}</div>
            </Field>
            {currentSuggestions.length ? <ul id="contact-suggestions" role="listbox" aria-label="Matching people" className="mt-2 overflow-hidden rounded-xl border border-line bg-surface shadow-sm">{currentSuggestions.map((contact) => <li key={`${contact.channel}:${contact.identifier}`}><button type="button" role="option" aria-selected={matchedContact?.identifier === contact.identifier} onClick={() => chooseContact(contact)} className="flex w-full items-center gap-3 border-b border-line px-3 py-3 text-left last:border-0 hover:bg-surface-sunken focus-visible:outline-2 focus-visible:outline-inset focus-visible:outline-ring"><span className="grid size-9 shrink-0 place-items-center rounded-full bg-secondary-tint text-secondary"><UserRoundCheck size={17} /></span><span className="min-w-0 flex-1"><strong className="block truncate text-control font-semibold text-ink">{contact.displayName}</strong><span className="block truncate text-note text-ink-muted">{contact.identifier}</span></span><span className="shrink-0 rounded-full bg-secondary-tint px-2 py-1 text-meta font-semibold text-secondary-hover">{contact.matchKind === "exact" ? "Exact Fyn account" : "Shared before"}</span></button></li>)}</ul> : null}
            {identifierHasSearchLength && !contactQuery.isFetching && !contactQuery.isError && currentSuggestions.length === 0 ? <p className="mt-2 text-meta leading-5 text-ink-muted">{identifierLooksComplete ? `No existing Fyn account matched. We’ll invite this ${draft.inviteChannel}.` : "No previous contact matched yet. Keep typing the complete email or phone number."}</p> : null}
            {contactQuery.isError ? <p role="status" className="mt-2 text-meta leading-5 text-ink-muted">Contact lookup is unavailable. You can still enter the details and send an invitation.</p> : null}
          </div>
          <div className="mt-5"><Field label="Person’s name" hint={matchedContact ? "Pulled from their Fyn profile. You can adjust how the name appears in this plan." : "This name appears in the shared repayment plan."}><input value={draft.counterpartyName || matchedContact?.displayName || ""} onChange={(event) => setDraft((current) => ({ ...current, counterpartyName: event.target.value }))} required maxLength={120} autoComplete="name" placeholder="Rahul" className={fieldClass} /></Field>{matchedContact ? <p role="status" className="mt-2 flex items-center gap-1.5 text-meta font-medium text-money-in"><CheckCircle2 size={14} />Matched to a Fyn account that signs in with this {draft.inviteChannel}</p> : null}</div>
          <p className="mt-4 text-meta leading-5 text-ink-muted">Partial suggestions only include people you have already shared a record with. The recipient must still verify this {draft.inviteChannel} before joining.</p>
        </fieldset>

        <fieldset className="mt-7"><legend className="ledger-meta mb-3">What happened?</legend><div className="grid grid-cols-2 gap-2">
          {(["lent", "borrowed"] as const).map((direction) => <button key={direction} type="button" aria-pressed={draft.direction === direction} onClick={() => setDraft((current) => ({ ...current, direction }))} className={cn("rounded-xl border p-4 text-left transition-colors", draft.direction === direction ? "border-secondary bg-secondary-tint" : "border-line bg-surface hover:bg-surface-sunken")}><span className="font-heading text-body font-semibold text-ink">{direction === "lent" ? "I gave money" : "I received money"}</span><span className="mt-1 block text-note text-ink-muted">{direction === "lent" ? "They will return it to me" : "I will return it to them"}</span></button>)}
        </div></fieldset>

        <fieldset className="mt-7"><legend className="ledger-meta mb-3">Plan details</legend><div className="grid gap-5 sm:grid-cols-2">
          <div className="sm:col-span-2"><Field label="Amount" hint="Enter rupees; Fyn stores the exact paise value."><div className="relative"><IndianRupee className="absolute top-[1.3rem] left-3 text-ink-muted" size={16} /><input value={draft.amount} onChange={(event) => setDraft((current) => ({ ...current, amount: event.target.value }))} required inputMode="decimal" placeholder="25,000" className={cn(fieldClass, "pl-9")} /></div></Field></div>
          <Field label={draft.direction === "lent" ? "Date I gave the money" : "Date I received the money"}><input type="date" value={draft.moneyDate} onChange={(event) => setDraft((current) => ({ ...current, moneyDate: event.target.value }))} required className={fieldClass} /></Field>
          <Field label="Return by"><input type="date" min={draft.moneyDate} value={draft.dueDate} onChange={(event) => setDraft((current) => ({ ...current, dueDate: event.target.value }))} required className={fieldClass} /></Field>
          <Field label="Annual interest" hint="Use 0 for an interest-free plan."><div className="relative"><input type="number" min="0" max="100" step="0.01" value={draft.interestPercent} onChange={(event) => setDraft((current) => ({ ...current, interestPercent: event.target.value }))} required className={cn(fieldClass, "pr-9")} /><span className="absolute top-[1.2rem] right-3 text-note text-ink-muted">%</span></div></Field>
          <Field label="Reason or context" hint="Optional, and visible to both people."><input value={draft.note} onChange={(event) => setDraft((current) => ({ ...current, note: event.target.value }))} maxLength={2000} placeholder="For the laptop" className={fieldClass} /></Field>
        </div></fieldset>

        <fieldset className="mt-7 rounded-xl border border-line bg-ground p-4 sm:p-5">
          <legend className="px-1 text-control font-medium text-ink-body">Optional assurance item</legend>
          <p className="mt-1 text-note leading-5 text-ink-muted">Describe a cheque, gold item, or document only if both of you want it in the shared record. Fyn does not take custody or enforce it.</p>
          <select value={draft.securityKind} onChange={(event) => setDraft((current) => ({ ...current, securityKind: event.target.value as CreateDraft["securityKind"] }))} className={fieldClass} aria-label="Assurance item type">
            <option value="none">No assurance item</option><option value="gold">Gold item</option><option value="post_dated_cheque">Post-dated cheque</option><option value="cancelled_cheque">Cancelled cheque</option><option value="document">Document</option><option value="other">Other item</option>
          </select>
          {draft.securityKind !== "none" ? <div className="mt-4 grid gap-4 sm:grid-cols-2"><Field label="Description"><input value={draft.securityDescription} onChange={(event) => setDraft((current) => ({ ...current, securityDescription: event.target.value }))} required maxLength={240} placeholder="22k gold chain, approx. 12 g" className={fieldClass} /></Field><Field label="Masked reference" hint="Never enter a complete cheque or account number."><input value={draft.securityIdentifier} onChange={(event) => setDraft((current) => ({ ...current, securityIdentifier: event.target.value }))} maxLength={120} placeholder="Cheque ending 4821" className={fieldClass} /></Field><Field label="Stated value" hint="Optional description, not a valuation by Fyn."><input value={draft.securityValue} onChange={(event) => setDraft((current) => ({ ...current, securityValue: event.target.value }))} inputMode="decimal" placeholder="25,000" className={fieldClass} /></Field></div> : null}
        </fieldset>

        <section aria-label="Plan preview" className="mt-6 rounded-xl border border-line bg-ground p-4 sm:p-5">
          <div className="flex items-center gap-2"><FileText className="text-secondary" size={18} /><h3 className="font-heading text-body font-semibold text-ink">Plain-language preview</h3></div>
          <p className="mt-3 text-note leading-6 text-ink-body">{counterpartyName || "The other person"} will review a plan for <strong>{principalMinor ? formatMoney(principalMinor) : "the amount"}</strong>, with {rate ? `${rate}% annual simple interest` : "no interest"}, to be returned by <strong>{draft.dueDate ? formatDate(draft.dueDate) : "the agreed date"}</strong>.</p>
          {principalMinor ? <div className="mt-4 grid grid-cols-3 gap-3 border-t border-line pt-4 text-note"><div><span className="text-ink-muted">Principal</span><strong className="mt-1 block text-ink">{formatMoney(principalMinor)}</strong></div><div><span className="text-ink-muted">Interest</span><strong className="mt-1 block text-ink">{formatMoney(interestMinor)}</strong></div><div><span className="text-ink-muted">Total</span><strong className="mt-1 block text-ink">{formatMoney(principalMinor + interestMinor)}</strong></div></div> : null}
        </section>
        <TrustNote compact />

        <div className="sticky bottom-0 -mx-4 mt-6 flex items-center gap-2 border-t border-line bg-surface/95 px-4 pt-4 pb-[max(1rem,env(safe-area-inset-bottom))] backdrop-blur-sm sm:-mx-6 sm:px-6">
          <Button type="submit" size="lg" disabled={!valid || mutation.isPending}>{mutation.isPending ? <Loader2 className="animate-spin" /> : <ArrowRight />}{mutation.isPending ? "Creating shared record…" : "Create and invite"}</Button>
          <Button type="button" size="lg" variant="ghost" disabled={mutation.isPending} onClick={onClose}>Cancel</Button>
        </div>
      </form>
    </section>
  </>;
}

function AgreementDocument({ loan }: { loan: PersonalLoanDetailOut }) {
  const revision = loan.documentRevision;
  const content = revision.content as { plainLanguage?: string; terms?: Record<string, unknown> };
  const currentParticipant = loan.participants.find((item) => item.isCurrentUser);
  const acceptedByMe = revision.acceptances.some((item) => item.participantId === currentParticipant?.id);
  return <section className="rounded-xl border border-line bg-surface">
    <div className="flex flex-wrap items-start gap-3 border-b border-line px-4 py-4 sm:px-5">
      <span className="grid size-9 place-items-center rounded-lg bg-secondary-tint text-secondary"><FileText size={17} /></span>
      <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><h2 className="font-heading text-body font-semibold text-ink">{revision.documentTitle}</h2><span className="rounded-full bg-surface-sunken px-2 py-1 text-meta font-semibold text-ink-muted">Revision {revision.revisionNumber}</span></div><p className="mt-1 text-note text-ink-muted">Authored by {revision.authoredBy} · Content fingerprint {revision.contentHash.slice(0, 10)}…</p></div>
      {acceptedByMe ? <span className="flex items-center gap-1 text-note font-semibold text-money-in"><CheckCircle2 size={16} /> You acknowledged this revision</span> : null}
    </div>
    <div className="p-4 sm:p-5">
      <p className="text-body leading-7 text-ink-body">{content.plainLanguage}</p>
      <dl className="mt-5 grid gap-4 rounded-xl bg-ground p-4 sm:grid-cols-2 lg:grid-cols-4">
        <div><dt className="ledger-meta">Principal</dt><dd className="mt-1 font-semibold text-ink">{formatMoney(loan.currentTerms.principalMinor, loan.currency)}</dd></div>
        <div><dt className="ledger-meta">Annual interest</dt><dd className="mt-1 font-semibold text-ink">{Number(content.terms?.annualRateBps ?? loan.annualRateBps) / 100}%</dd></div>
        <div><dt className="ledger-meta">Return date</dt><dd className="mt-1 font-semibold text-ink">{formatDate((content.terms?.dueDate as string) ?? loan.dueDate)}</dd></div>
        <div><dt className="ledger-meta">Total repayable</dt><dd className="mt-1 font-semibold text-ink">{formatMoney(Number(content.terms?.totalRepayableMinor ?? loan.totalRepayableMinor), loan.currency)}</dd></div>
      </dl>
      {loan.securityItems.length ? <div className="mt-5 rounded-xl border border-attention/30 bg-attention-tint p-4"><div className="flex items-start gap-3"><ShieldCheck className="mt-0.5 shrink-0 text-attention-ink" size={18} /><div><h3 className="text-control font-semibold text-ink">Assurance and return record</h3>{loan.securityItems.map((item) => <div key={item.id} className="mt-2 text-note leading-5 text-ink-body"><strong>{titleCase(item.kind)}</strong> · {item.description}{item.maskedIdentifier ? ` · ${item.maskedIdentifier}` : ""}<span className="block text-ink-muted">Provided by {item.providedBy}, stated as held by {item.heldBy} · {titleCase(item.state)}</span></div>)}<p className="mt-2 text-meta leading-5 text-ink-muted">This is descriptive acknowledgement only. On closure, the stated holder records return and the provider confirms it.</p></div></div></div> : null}
      {revision.changes.length ? <div className="mt-5"><h3 className="ledger-meta">What changed in this revision</h3><ul className="mt-2 space-y-2">{revision.changes.map((change) => <li key={change.id} className="flex items-start gap-2 text-note text-ink-body"><PencilLine className="mt-0.5 shrink-0 text-secondary" size={14} /><span><strong className="font-semibold">{change.summary}</strong> by {change.authoredBy}</span></li>)}</ul></div> : null}
      <div className="mt-5 border-t border-line pt-4"><h3 className="ledger-meta">Acknowledgements</h3><div className="mt-2 flex flex-wrap gap-2">{loan.participants.map((participant) => { const acceptance = revision.acceptances.find((item) => item.participantId === participant.id); return <span key={participant.id} className={cn("flex items-center gap-1.5 rounded-full border px-2.5 py-1.5 text-note", acceptance ? "border-secondary-line bg-secondary-tint text-secondary-hover" : "border-line bg-surface-sunken text-ink-muted")}>{acceptance ? <Check size={13} /> : <Clock3 size={13} />}{participant.displayName} · {acceptance ? "acknowledged" : "waiting"}</span>; })}</div></div>
    </div>
  </section>;
}

function RevisionHistory({ documentId }: { documentId: string }) {
  const [open, setOpen] = useState(false);
  const query = useQuery({ queryKey: ["shared-document-revisions", documentId], queryFn: () => loadSharedDocumentRevisions(documentId), enabled: open });
  return <section className="rounded-xl border border-line bg-surface">
    <button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)} className="flex w-full items-center gap-3 px-4 py-4 text-left sm:px-5"><FileClock className="text-secondary" size={18} /><span className="min-w-0 flex-1"><strong className="block text-control font-semibold text-ink">Revision history</strong><span className="text-note text-ink-muted">Every version stays available with its author, changes, and acknowledgements.</span></span><ChevronRight className={cn("text-ink-muted transition-transform", open && "rotate-90")} size={18} /></button>
    {open ? <div className="border-t border-line p-4 sm:p-5">{query.isPending ? <p role="status" className="flex items-center gap-2 text-note text-ink-muted"><Loader2 className="animate-spin" />Loading revisions…</p> : query.isError ? <p role="alert" className="text-note text-danger-ink">Revision history couldn’t be loaded.</p> : <ol className="space-y-3">{query.data?.map((revision: DocumentRevisionOut) => <li key={revision.id} className="rounded-lg border border-line bg-ground p-3"><div className="flex items-center justify-between gap-3"><strong className="text-control text-ink">Revision {revision.revisionNumber}</strong><StatusBadge status={revision.state} /></div><p className="mt-1 text-note text-ink-muted">{revision.authoredBy} · {formatInstant(revision.proposedAt)} · {revision.changes.length} changed fields</p><p className="mt-2 font-mono text-[11px] text-ink-muted">{revision.contentHash}</p></li>)}</ol>}</div> : null}
  </section>;
}

function ActionProblem({ children }: { children: string }) {
  return <p role="alert" className="rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note text-danger-ink">{children}</p>;
}

function LoanActions({ loan }: { loan: PersonalLoanDetailOut }) {
  const queryClient = useQueryClient();
  const [view, setView] = useState<"none" | "payment" | "amend" | "reminder">("none");
  const [amount, setAmount] = useState("");
  const [paymentDate, setPaymentDate] = useState(localDate());
  const [paymentNote, setPaymentNote] = useState("");
  const [dueDate, setDueDate] = useState(loan.currentTerms.dueDate);
  const [rate, setRate] = useState(String(loan.currentTerms.annualRateBps / 100));
  const [termNote, setTermNote] = useState(loan.currentTerms.note ?? "");
  const [reminderNote, setReminderNote] = useState("");
  const [success, setSuccess] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const update = (next: PersonalLoanDetailOut) => {
    queryClient.setQueryData(["personal-loan", loan.id], next);
    void queryClient.invalidateQueries({ queryKey: ["personal-loans"] });
    void queryClient.invalidateQueries({ queryKey: ["overview"] });
    setProblem(null);
    setSuccess(null);
    setView("none");
  };
  const fail = (cause: Error) => { setSuccess(null); setProblem(cause.message); };
  const accept = useMutation({ mutationFn: () => acceptPersonalLoan(loan.id, loan.rowVersion), onSuccess: ({ loan: next }) => update(next), onError: fail });
  const payment = useMutation({ mutationFn: () => {
    const minor = parseAmountToMinor(amount);
    if (!minor) throw new Error("Enter a payment amount greater than zero.");
    return recordPersonalLoanPayment(loan.id, { amountMinor: minor, occurredOn: paymentDate, note: paymentNote.trim() || null });
  }, onSuccess: ({ loan: next }) => update(next), onError: fail });
  const amend = useMutation({ mutationFn: () => proposePersonalLoanTerms(loan.id, { dueDate, annualRateBps: Math.round(Number(rate) * 100), note: termNote.trim() || null, expectedRowVersion: loan.rowVersion }), onSuccess: ({ loan: next }) => update(next), onError: fail });
  const reminder = useMutation({ mutationFn: () => sendPersonalLoanReminder(loan.id, { tone: "friendly", note: reminderNote.trim() || null }), onSuccess: (result) => { setProblem(null); setSuccess(`Reminder queued by ${result.channel} to ${result.destinationMasked}.`); setView("none"); setReminderNote(""); }, onError: fail });
  const close = useMutation({ mutationFn: () => closePersonalLoan(loan.id), onSuccess: ({ loan: next }) => update(next), onError: fail });
  const busy = accept.isPending || payment.isPending || amend.isPending || reminder.isPending || close.isPending;
  const me = loan.participants.find((item) => item.isCurrentUser);
  const awaitingMyDocumentAcceptance = loan.documentRevision.state === "proposed" && !loan.documentRevision.acceptances.some((item) => item.participantId === me?.id);
  const closureProposal = loan.activity.find((item) => item.eventType === "loan.closure_proposed");
  const canActOnClosure = loan.status === "settlement_pending" && (
    loan.securityItems.length > 0
      ? (closureProposal ? me?.role === "borrower" && closureProposal.actorParticipantId !== me.id : me?.role === "lender")
      : (!closureProposal || closureProposal.actorParticipantId !== me?.id)
  );

  return <section className="rounded-xl border border-line bg-surface p-4 sm:p-5">
    <h2 className="font-heading text-body font-semibold text-ink">Actions</h2>
    <p className="mt-1 text-note text-ink-muted">Every financial change is recorded first and confirmed by the other person.</p>
    {problem ? <div className="mt-4"><ActionProblem>{problem}</ActionProblem></div> : null}
    {success ? <p role="status" className="mt-4 flex items-center gap-2 rounded-lg border border-secondary-line bg-secondary-tint px-4 py-3 text-note text-secondary-hover"><CheckCircle2 size={16} />{success}</p> : null}
    {awaitingMyDocumentAcceptance ? <div className="mt-4 rounded-xl border border-secondary-line bg-secondary-tint p-4"><div className="flex items-start gap-3"><UserRoundCheck className="mt-0.5 shrink-0 text-secondary" /><div><h3 className="font-heading text-body font-semibold text-ink">Review and acknowledge revision {loan.documentRevision.revisionNumber}</h3><p className="mt-1 text-note leading-5 text-ink-muted">Your acknowledgement is tied to the exact content fingerprint shown in the document.</p><Button type="button" className="mt-4" disabled={busy} onClick={() => accept.mutate()}>{accept.isPending ? <Loader2 className="animate-spin" /> : <Check />}Acknowledge this revision</Button></div></div></div> : null}

    <div className="mt-4 flex flex-wrap gap-2">
      {loan.status === "active" ? <><Button type="button" variant="outline" onClick={() => setView(view === "payment" ? "none" : "payment")}><ReceiptIndianRupee /> Record payment</Button><Button type="button" variant="outline" onClick={() => setView(view === "amend" ? "none" : "amend")}><PencilLine /> Propose change</Button><Button type="button" variant="ghost" onClick={() => setView(view === "reminder" ? "none" : "reminder")}><MessageCircleMore /> Send reminder</Button></> : null}
      {canActOnClosure ? <Button type="button" disabled={busy} onClick={() => close.mutate()}>{close.isPending ? <Loader2 className="animate-spin" /> : <CheckCircle2 />}{closureProposal ? (loan.securityItems.length ? "Confirm item returned and close" : "Confirm closure") : (loan.securityItems.length ? "Mark item returned and propose closure" : "Propose closure")}</Button> : null}
    </div>
    {loan.status === "settlement_pending" && loan.securityItems.length > 0 && me?.role === "borrower" && !closureProposal ? <p className="mt-3 text-note leading-5 text-ink-muted">Waiting for {loan.securityItems[0].heldBy} to record return of the assurance item before you confirm closure.</p> : null}
    {loan.status === "settlement_pending" && loan.securityItems.length > 0 && me?.role === "lender" && closureProposal ? <p className="mt-3 text-note leading-5 text-ink-muted">Return recorded. Waiting for {loan.securityItems[0].providedBy} to confirm receipt and close the plan.</p> : null}

    {view === "payment" ? <form className="mt-5 grid gap-4 rounded-xl bg-ground p-4 sm:grid-cols-2" onSubmit={(event) => { event.preventDefault(); payment.mutate(); }}><Field label="Amount paid"><input autoFocus value={amount} onChange={(event) => setAmount(event.target.value)} required inputMode="decimal" placeholder="5,000" className={fieldClass} /></Field><Field label="Payment date"><input type="date" value={paymentDate} onChange={(event) => setPaymentDate(event.target.value)} required className={fieldClass} /></Field><div className="sm:col-span-2"><Field label="Note"><input value={paymentNote} onChange={(event) => setPaymentNote(event.target.value)} maxLength={500} placeholder="Bank transfer reference, optional" className={fieldClass} /></Field></div><div className="flex gap-2 sm:col-span-2"><Button type="submit" disabled={busy}>{payment.isPending ? <Loader2 className="animate-spin" /> : null}Record for confirmation</Button><Button type="button" variant="ghost" onClick={() => setView("none")}>Cancel</Button></div></form> : null}
    {view === "amend" ? <form className="mt-5 grid gap-4 rounded-xl bg-ground p-4 sm:grid-cols-2" onSubmit={(event) => { event.preventDefault(); amend.mutate(); }}><Field label="New return date"><input autoFocus type="date" min={loan.moneyDate} value={dueDate} onChange={(event) => setDueDate(event.target.value)} required className={fieldClass} /></Field><Field label="Annual interest"><input type="number" min="0" max="100" step="0.01" value={rate} onChange={(event) => setRate(event.target.value)} required className={fieldClass} /></Field><div className="sm:col-span-2"><Field label="Why is this changing?"><input value={termNote} onChange={(event) => setTermNote(event.target.value)} maxLength={2000} className={fieldClass} /></Field></div><p className="text-note leading-5 text-ink-muted sm:col-span-2">This creates a new immutable document revision. The current plan remains active until both people acknowledge the new one.</p><div className="flex gap-2 sm:col-span-2"><Button type="submit" disabled={busy}>{amend.isPending ? <Loader2 className="animate-spin" /> : null}Propose revision</Button><Button type="button" variant="ghost" onClick={() => setView("none")}>Cancel</Button></div></form> : null}
    {view === "reminder" ? <form className="mt-5 rounded-xl bg-ground p-4" onSubmit={(event) => { event.preventDefault(); reminder.mutate(); }}><Field label="Friendly note" hint="Fyn rate-limits reminders so they stay useful, not uncomfortable."><input autoFocus value={reminderNote} onChange={(event) => setReminderNote(event.target.value)} maxLength={500} placeholder="Just checking that our date still works for you" className={fieldClass} /></Field><div className="mt-4 flex gap-2"><Button type="submit" disabled={busy}>{reminder.isPending ? <Loader2 className="animate-spin" /> : <MessageCircleMore />}Queue reminder</Button><Button type="button" variant="ghost" onClick={() => setView("none")}>Cancel</Button></div></form> : null}
  </section>;
}

function Payments({ loan }: { loan: PersonalLoanDetailOut }) {
  const queryClient = useQueryClient();
  const [problem, setProblem] = useState<string | null>(null);
  const confirm = useMutation({
    mutationFn: (cashflowId: string) => confirmPersonalLoanPayment(cashflowId, loan.rowVersion),
    onSuccess: ({ loan: next }) => { queryClient.setQueryData(["personal-loan", loan.id], next); void queryClient.invalidateQueries({ queryKey: ["personal-loans"] }); void queryClient.invalidateQueries({ queryKey: ["overview"] }); setProblem(null); },
    onError: (cause: Error) => setProblem(cause.message),
  });
  const me = loan.participants.find((item) => item.isCurrentUser)?.displayName;
  return <section className="rounded-xl border border-line bg-surface">
    <div className="border-b border-line px-4 py-4 sm:px-5"><h2 className="font-heading text-body font-semibold text-ink">Payment record</h2><p className="mt-1 text-note text-ink-muted">A recorded payment changes the shared balance only after the other person confirms it.</p></div>
    {problem ? <div className="p-4 pb-0"><ActionProblem>{problem}</ActionProblem></div> : null}
    {loan.cashflows.length ? <div>{loan.cashflows.map((cashflow) => { const canConfirm = cashflow.state === "proposed" && cashflow.initiatedBy !== me; return <article key={cashflow.id} className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-4 last:border-0 sm:px-5"><span className={cn("grid size-9 place-items-center rounded-lg", cashflow.state === "confirmed" ? "bg-secondary-tint text-money-in" : "bg-attention-tint text-attention-ink")}><ReceiptIndianRupee size={17} /></span><div className="min-w-0 flex-1"><p className="text-control font-semibold text-ink">{formatMoney(cashflow.amountMinor, cashflow.currency)} · {formatDate(cashflow.occurredOn)}</p><p className="mt-1 text-note text-ink-muted">Recorded by {cashflow.initiatedBy}{cashflow.confirmedBy ? ` · Confirmed by ${cashflow.confirmedBy}` : " · Waiting for confirmation"}</p></div>{canConfirm ? <Button type="button" disabled={confirm.isPending} onClick={() => confirm.mutate(cashflow.id)}>{confirm.isPending ? <Loader2 className="animate-spin" /> : <Check />}Confirm payment</Button> : <StatusBadge status={cashflow.state} />}</article>; })}</div> : <div className="px-4 py-8 text-center text-note text-ink-muted">No repayments have been recorded yet.</div>}
  </section>;
}

function ActivityTimeline({ loan }: { loan: PersonalLoanDetailOut }) {
  return <section className="rounded-xl border border-line bg-surface p-4 sm:p-5"><h2 className="font-heading text-body font-semibold text-ink">Shared activity</h2><ol className="mt-4 space-y-0">{loan.activity.map((event, index) => <li key={event.id} className="grid grid-cols-[auto_1fr] gap-3"><div className="flex flex-col items-center"><span className="mt-1 size-2 rounded-full bg-secondary" />{index < loan.activity.length - 1 ? <span className="h-full w-px bg-line" /> : null}</div><div className="pb-5"><p className="text-control font-medium text-ink">{titleCase(event.eventType.replace("loan.", "").replace("document.", "Document ").replace("payment.", "Payment ").replace("participant.", "Participant ").replaceAll(".", " "))}</p><p className="mt-1 text-note text-ink-muted">{event.actorName ? `${event.actorName} · ` : ""}{formatInstant(event.createdAt)}</p></div></li>)}</ol></section>;
}

export function PersonalLoanDetailPage() {
  const { loanId = "" } = useParams();
  const navigate = useNavigate();
  const query = useQuery({ queryKey: ["personal-loan", loanId], queryFn: () => loadPersonalLoan(loanId), enabled: Boolean(loanId) });
  const loan = query.data;
  const sharePath = loan?.invitation?.sharePath;

  async function copyInvite() {
    if (!sharePath) return;
    await navigator.clipboard.writeText(new URL(sharePath, window.location.origin).toString());
  }

  return <main className="min-h-0 flex-1 overflow-y-auto bg-ground">
    <PageHeader title={loan?.counterpartyName ?? "Shared plan"} subtitle={loan ? `${loan.direction === "lent" ? "Money I gave" : "Money I received"} · ${formatMoney(loan.principalMinor, loan.currency)}` : "Loading personal loan"} end={<Button type="button" variant="ghost" onClick={() => navigate(appPaths.loans)}><ArrowLeft /> All plans</Button>} />
    <div className="mx-auto w-full max-w-6xl px-4 py-5 sm:px-6 sm:py-8">
      {query.isPending ? <div role="status" className="py-20 text-center text-note text-ink-muted"><Loader2 className="mx-auto mb-3 animate-spin" />Loading the shared record…</div>
        : query.isError || !loan ? <QueryProblem message="This shared plan couldn’t be loaded" retry={() => { void query.refetch(); }} />
        : <>
          <div className="flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-2"><StatusBadge status={loan.status} responseNeeded={loan.responseNeeded} />{loan.counterpartyVerification ? <span className="flex items-center gap-1 rounded-full bg-secondary-tint px-2.5 py-1 text-meta font-semibold text-secondary-hover"><ShieldCheck size={13} />{titleCase(loan.counterpartyVerification)}</span> : null}</div>{sharePath ? <Button type="button" variant="outline" onClick={() => { void copyInvite(); }}><Copy /> Copy private invite</Button> : null}</div>

          <section className="mt-4 overflow-hidden rounded-2xl border border-line bg-surface"><div className="grid gap-px bg-line sm:grid-cols-4"><div className="bg-surface p-4 sm:p-5"><p className="ledger-meta">Original amount</p><p className="mt-2 font-heading text-title font-semibold text-ink">{formatMoney(loan.principalMinor, loan.currency)}</p></div><div className="bg-surface p-4 sm:p-5"><p className="ledger-meta">Remaining principal</p><p className="mt-2 font-heading text-title font-semibold text-ink">{formatMoney(loan.outstandingPrincipalMinor, loan.currency)}</p></div><div className="bg-surface p-4 sm:p-5"><p className="ledger-meta">Remaining interest</p><p className="mt-2 font-heading text-title font-semibold text-ink">{formatMoney(loan.accruedInterestMinor, loan.currency)}</p></div><div className="bg-surface p-4 sm:p-5"><p className="ledger-meta">Return by</p><p className="mt-2 font-heading text-title font-semibold text-ink">{formatDate(loan.dueDate)}</p></div></div></section>

          <div className="mt-5 grid items-start gap-5 lg:grid-cols-[minmax(0,1.55fr)_minmax(19rem,.8fr)]">
            <div className="space-y-5"><AgreementDocument loan={loan} /><Payments loan={loan} /><RevisionHistory documentId={loan.documentRevision.documentId} /></div>
            <aside className="space-y-5"><LoanActions loan={loan} /><ActivityTimeline loan={loan} /><TrustNote compact /></aside>
          </div>
        </>}
    </div>
  </main>;
}

export function LoanInvitationPage() {
  const { token = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const invitation = useQuery({ queryKey: ["loan-invitation", token], queryFn: () => loadLoanInvitation(token), enabled: Boolean(token), retry: false });
  const auth = useQuery({ queryKey: ["auth"], queryFn: getAuthStatus, retry: false });
  const redeem = useMutation({
    mutationFn: () => redeemLoanInvitation(token),
    onSuccess: ({ loan }) => { queryClient.setQueryData(["personal-loan", loan.id], loan); void queryClient.invalidateQueries({ queryKey: ["personal-loans"] }); navigate(appPaths.loan(loan.id), { replace: true }); },
  });
  const path = appPaths.loanInvitation(token);

  return <main className="grid min-h-dvh place-items-center bg-ground px-4 py-8">
    <div className="w-full max-w-xl">
      <div className="mb-6 text-center"><p className="font-heading text-display font-semibold tracking-[-0.03em] text-ink">fyn AI</p><p className="mt-1 text-note text-ink-muted">Private shared record</p></div>
      <section className="overflow-hidden rounded-2xl border border-line bg-surface shadow-[var(--shadow-overlay)]">
        <div className="border-b border-line bg-secondary-tint px-5 py-5 sm:px-7"><div className="flex items-start gap-3"><span className="grid size-11 place-items-center rounded-xl bg-surface text-secondary"><HandCoins /></span><div><p className="ledger-meta text-secondary">Personal lending invitation</p><h1 className="mt-1 font-heading text-[1.45rem] font-semibold tracking-[-0.025em] text-ink">Review a shared repayment plan</h1></div></div></div>
        <div className="p-5 sm:p-7">
          {invitation.isPending || auth.isPending ? <p role="status" className="py-10 text-center text-note text-ink-muted"><Loader2 className="mx-auto mb-3 animate-spin" />Checking this private invitation…</p>
            : invitation.isError || !invitation.data?.tokenValid ? <div className="py-8 text-center"><Clock3 className="mx-auto text-ink-muted" /><h2 className="mt-4 font-heading text-title font-semibold text-ink">This invitation is no longer available</h2><p className="mt-2 text-note text-ink-muted">Ask the sender to share a fresh invitation.</p></div>
            : <><div className="flex items-start gap-3"><UserRoundCheck className="mt-0.5 shrink-0 text-secondary" /><div><h2 className="font-heading text-title font-semibold text-ink">{invitation.data.senderName} invited {invitation.data.recipientName}</h2><p className="mt-2 text-note leading-5 text-ink-muted">It was sent to <strong className="font-semibold text-ink-body">{invitation.data.destinationMasked}</strong>. The full financial terms become visible only after that address is verified.</p></div></div>
              <div className="my-5"><TrustNote compact /></div>
              {redeem.error ? <ActionProblem>{redeem.error.message}</ActionProblem> : null}
              {!auth.data?.authenticated ? <div><Link to={`${appPaths.login}?next=${encodeURIComponent(path)}`} className="inline-flex h-[var(--h-lg)] items-center justify-center gap-2 rounded-md bg-secondary px-4 text-control font-medium text-on-secondary">Sign in to review <ArrowRight size={15} /></Link><p className="mt-3 text-note text-ink-muted">Use the {invitation.data.channel} that received this invitation.</p></div>
                : invitation.data.canRedeem ? <Button type="button" size="lg" disabled={redeem.isPending} onClick={() => redeem.mutate()}>{redeem.isPending ? <Loader2 className="animate-spin" /> : <ShieldCheck />}{redeem.isPending ? "Opening shared plan…" : "Verify and review plan"}</Button>
                  : <div className="rounded-lg border border-attention/40 bg-attention-tint px-4 py-3 text-note leading-5 text-attention-ink">You are signed in, but this account has not verified the {invitation.data.channel} that received the invitation. Link that address in Profile, or sign in with the matching account.</div>}
            </>}
        </div>
      </section>
      <p className="mt-4 text-center text-meta leading-5 text-ink-muted">The invitation token is private. Fyn stores only a one-way fingerprint of it.</p>
    </div>
  </main>;
}
