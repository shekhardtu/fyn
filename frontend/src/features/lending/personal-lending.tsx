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
  FileDown,
  FileText,
  HandCoins,
  IndianRupee,
  Loader2,
  Mail,
  MessageCircleMore,
  Paperclip,
  PencilLine,
  Phone,
  Plus,
  ReceiptIndianRupee,
  RotateCcw,
  Search,
  ShieldCheck,
  UserRoundCheck,
  UsersRound,
  UploadCloud,
  Trash2,
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
  documentAssetDownloadUrl,
  fulfillPersonalLoanDocumentRequests,
  getProfile,
  getAuthStatus,
  loadLoanInvitation,
  loadDocumentAssets,
  loadPersonalLoan,
  loadPersonalLoans,
  loadSharedDocumentRevisions,
  loanAgreementPdfUrl,
  loanEvidenceBundleUrl,
  proposePersonalLoanTerms,
  recordPersonalLoanPayment,
  recordPersonalLoanFunding,
  redeemLoanInvitation,
  searchContacts,
  sendPersonalLoanReminder,
  uploadDocumentAsset,
} from "@/lib/api";
import { formatInstant, formatMoney, parseAmountToMinor } from "@/lib/format";
import type {
  CreatePersonalLoanIn,
  ContactSuggestionOut,
  DocumentAssetOut,
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
  const pending = status === "pending_acceptance" || status === "funding_pending" || status === "settlement_pending";
  return <span className={cn(
    "rounded-full px-2.5 py-1 text-meta font-semibold",
    settled ? "bg-surface-sunken text-ink-muted" : pending ? "bg-attention-tint text-attention-ink" : "bg-secondary-tint text-secondary-hover",
  )}>{status === "pending_acceptance" ? "Waiting for agreement" : status === "funding_pending" ? "Waiting for money transfer" : status === "settlement_pending" ? "Ready to close" : titleCase(status)}</span>;
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
  intent: "record_given" | "record_received" | "offer_to_lend" | "request_to_borrow" | null;
  counterpartyName: string;
  inviteChannel: "phone" | "email";
  inviteValue: string;
  amount: string;
  moneyDate: string;
  dueDate: string;
  interestPercent: string;
  interestPeriod: "monthly" | "yearly";
  interestMode: "simple" | "compound";
  note: string;
  securityKind: "none" | "gold" | "post_dated_cheque" | "cancelled_cheque" | "document" | "other";
  securityDescription: string;
  securityIdentifier: string;
  securityValue: string;
};

type DraftAttachment = {
  id: string;
  file: File;
  classification: "external_agreement" | "assurance_item" | "transfer_receipt" | "supporting_evidence";
  assetId?: string;
};

type DraftDocumentRequest = {
  id: string;
  label: string;
  classification: "external_agreement" | "assurance_item" | "transfer_receipt" | "identity_evidence" | "witness_statement" | "supporting_evidence";
  instructions: string;
  required: boolean;
};

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <label className="block"><span className="text-control font-medium text-ink-body">{label}</span>{children}{hint ? <span className="mt-1.5 block text-meta leading-5 text-ink-muted">{hint}</span> : null}</label>;
}

const fieldClass = "manual-field mt-2 h-11 w-full rounded-lg border border-line-strong bg-surface px-3 text-body text-ink outline-none placeholder:text-ink-muted";

function interestAmountMinor(principalMinor: number | null, ratePercent: number, period: "monthly" | "yearly", mode: "simple" | "compound", days: number) {
  if (!principalMinor || !Number.isFinite(ratePercent) || ratePercent <= 0 || days <= 0) return 0;
  const periodDays = period === "monthly" ? 30 : 365;
  const periodicRate = ratePercent / 100;
  if (mode === "compound") {
    const fullPeriods = Math.floor(days / periodDays);
    const remainingDays = days % periodDays;
    const factor = ((1 + periodicRate) ** fullPeriods) * (1 + periodicRate * remainingDays / periodDays);
    return Math.round(principalMinor * (factor - 1));
  }
  return Math.round(principalMinor * periodicRate * days / periodDays);
}

function interestBasis(period: "monthly" | "yearly", mode: "simple" | "compound" = "simple") {
  const method = mode === "compound" ? "Compound" : "Simple on fixed principal";
  return period === "monthly" ? `${method} monthly · 30-day basis` : `${method} yearly · actual days/365`;
}

function CreateLoanDrawer({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const panelRef = useWorkspaceOverlay(true, onClose);
  const [problem, setProblem] = useState<string | null>(null);
  const [step, setStep] = useState(0);
  const [attachments, setAttachments] = useState<DraftAttachment[]>([]);
  const [selectedAssetIds, setSelectedAssetIds] = useState<string[]>([]);
  const [documentRequests, setDocumentRequests] = useState<DraftDocumentRequest[]>([]);
  const [resolvedContact, setResolvedContact] = useState<ContactSuggestionOut | null>(null);
  const [draft, setDraft] = useState<CreateDraft>({
    intent: null,
    counterpartyName: "",
    inviteChannel: "email",
    inviteValue: "",
    amount: "",
    moneyDate: localDate(),
    dueDate: localDate(30),
    interestPercent: "0",
    interestPeriod: "yearly",
    interestMode: "simple",
    note: "",
    securityKind: "none",
    securityDescription: "",
    securityIdentifier: "",
    securityValue: "",
  });
  const profile = useQuery({ queryKey: ["profile"], queryFn: getProfile, retry: false });
  const documentLibrary = useQuery({ queryKey: ["document-assets"], queryFn: loadDocumentAssets, enabled: step >= 3, retry: false });
  const direction: "lent" | "borrowed" = draft.intent === "record_received" || draft.intent === "request_to_borrow" ? "borrowed" : "lent";
  const moneyMoved = draft.intent === "record_given" || draft.intent === "record_received";
  const principalMinor = parseAmountToMinor(draft.amount);
  const rate = Number(draft.interestPercent || 0);
  const days = Math.max((new Date(draft.dueDate).valueOf() - new Date(draft.moneyDate).valueOf()) / 86_400_000, 0);
  const interestMinor = interestAmountMinor(principalMinor, rate, draft.interestPeriod, draft.interestMode, days);
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
  const counterpartyRole = direction === "lent" ? "borrower" : "lender";
  const amountLabel = draft.intent === "record_given" ? "Amount I gave" : draft.intent === "record_received" ? "Amount I received" : draft.intent === "offer_to_lend" ? "Amount I can lend" : "Amount I want to borrow";
  const returnDateLabel = direction === "lent" ? "When will they return it?" : "When will I return it?";
  const termsValid = Boolean(principalMinor && draft.moneyDate && draft.dueDate && draft.dueDate >= draft.moneyDate && rate >= 0 && rate <= 100);
  const identifierLooksComplete = draft.inviteChannel === "email"
    ? /^[^@\s]+@[^@\s.]+\.[^@\s.]+$/.test(draft.inviteValue.trim())
    : draft.inviteValue.replace(/\D/g, "").length >= 10;
  const personValid = Boolean(counterpartyName && identifierLooksComplete);
  const hasRealProfile = Boolean(profile.data && profile.data.displayName.trim().toLowerCase() !== "you");
  const securityValid = draft.securityKind === "none" || Boolean(draft.securityDescription.trim());
  const documentRequestsValid = documentRequests.every((item) => item.label.trim().length >= 2);
  const selectedAssets = documentLibrary.data?.filter((asset) => selectedAssetIds.includes(asset.id)) ?? [];
  const sharedDocumentCount = selectedAssetIds.length + attachments.length;
  const documentsValid = documentRequestsValid && sharedDocumentCount <= 8;
  const steps = ["Intent", "Person", "Terms", "Documents", "Review"];

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
    mutationFn: async () => {
      if (!draft.intent) throw new Error("Choose whether you are lending, borrowing, offering, or requesting money.");
      const uploadedIds: string[] = [...selectedAssetIds];
      for (const attachment of attachments) {
        if (attachment.assetId) {
          uploadedIds.push(attachment.assetId);
          continue;
        }
        const uploaded = await uploadDocumentAsset(attachment.file, attachment.classification);
        uploadedIds.push(uploaded.id);
        setAttachments((current) => current.map((item) => item.id === attachment.id ? { ...item, assetId: uploaded.id } : item));
        queryClient.setQueryData(["document-assets"], (current: DocumentAssetOut[] | undefined) => current ? [uploaded, ...current] : [uploaded]);
      }
        const payload: CreatePersonalLoanIn = {
          direction,
          intent: draft.intent,
          counterpartyName,
          inviteChannel: draft.inviteChannel,
          inviteValue: draft.inviteChannel === "phone" && !draft.inviteValue.trim().startsWith("+") ? `+91${draft.inviteValue.replace(/\D/g, "")}` : draft.inviteValue.trim(),
          principalMinor: principalMinor!,
          currency: "INR",
          moneyDate: draft.moneyDate,
          dueDate: draft.dueDate,
          interestRateBps: Math.round(rate * 100),
          interestPeriod: draft.interestPeriod,
          interestMode: draft.interestMode,
          note: draft.note.trim() || null,
          securityItems: draft.securityKind === "none" ? [] : [{
            kind: draft.securityKind,
            description: draft.securityDescription.trim(),
            maskedIdentifier: draft.securityIdentifier.trim() || null,
            statedValueMinor: parseAmountToMinor(draft.securityValue),
          }],
          documentRequests: direction === "lent" ? documentRequests.map((item) => ({
            label: item.label.trim(),
            classification: item.classification,
            instructions: item.instructions.trim() || null,
            required: item.required,
          })) : [],
          assetIds: uploadedIds,
        };
        return await createPersonalLoan(payload);
    },
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
    if (step < steps.length - 1) {
      if (stepValid) setStep((value) => Math.min(steps.length - 1, value + 1));
      return;
    }
    if (!draft.intent || !personValid || !termsValid || !securityValid || !documentsValid || !principalMinor || !hasRealProfile || mutation.isPending) return;
    mutation.mutate();
  }

  const stepValid = step === 0 ? Boolean(draft.intent) : step === 1 ? personValid && hasRealProfile : step === 2 ? termsValid : step === 3 ? securityValid && documentsValid : true;

  function addFiles(files: FileList | null) {
    if (!files) return;
    const accepted = Array.from(files).filter((file) => /^(application\/pdf|image\/(png|jpeg))$/.test(file.type) && file.size <= 10 * 1024 * 1024);
    setAttachments((current) => [...current, ...accepted.map((file) => ({ id: crypto.randomUUID(), file, classification: "supporting_evidence" as const }))].slice(0, 8));
  }

  function addDocumentRequest(label = "", classification: DraftDocumentRequest["classification"] = "supporting_evidence") {
    setDocumentRequests((current) => [...current, { id: crypto.randomUUID(), label, classification, instructions: "", required: true }].slice(0, 8));
  }

  function toggleLibraryAsset(assetId: string) {
    setSelectedAssetIds((current) => current.includes(assetId) ? current.filter((id) => id !== assetId) : [...current, assetId].slice(0, 8));
  }

  return <section ref={panelRef} role="dialog" aria-modal="true" aria-labelledby="create-loan-title" className="fixed inset-0 z-50 flex flex-col bg-ground">
      <header className="border-b border-line bg-surface px-4 pt-[max(1rem,env(safe-area-inset-top))] sm:px-6">
        <div className="mx-auto flex max-w-7xl items-center pb-4">
          <span className="grid size-10 place-items-center rounded-xl bg-secondary-tint text-secondary"><HandCoins size={20} /></span>
          <div className="ml-3"><h2 id="create-loan-title" className="font-heading text-title font-semibold text-ink">Create a trusted agreement</h2><p className="text-note text-ink-muted">One exact record, reviewed independently by both people.</p></div>
          <Button type="button" variant="ghost" size="icon-lg" className="ml-auto" aria-label="Close" disabled={mutation.isPending} onClick={onClose}><X /></Button>
        </div>
        <nav aria-label="Agreement creation progress" className="mx-auto max-w-7xl overflow-x-auto"><ol className="flex min-w-max gap-1">{steps.map((label, index) => <li key={label}><button type="button" disabled={index > step || mutation.isPending} onClick={() => setStep(index)} aria-current={index === step ? "step" : undefined} className={cn("flex items-center gap-2 border-b-2 px-3 pb-3 text-note font-semibold", index === step ? "border-secondary text-secondary" : index < step ? "border-transparent text-ink-body" : "border-transparent text-ink-muted")}><span className={cn("grid size-6 place-items-center rounded-full text-meta", index <= step ? "bg-secondary-tint text-secondary" : "bg-surface-sunken")}>{index < step ? <Check size={13} /> : index + 1}</span>{label}</button></li>)}</ol></nav>
      </header>

      <form onSubmit={submit} className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto grid w-full max-w-7xl items-start gap-8 px-4 py-6 sm:px-6 lg:grid-cols-[minmax(0,1fr)_22rem] lg:py-9">
          <div className="rounded-2xl border border-line bg-surface p-5 shadow-sm sm:p-8">
            {problem ? <p role="alert" className="mb-5 rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note text-danger-ink">{problem}</p> : null}
            {!hasRealProfile && !profile.isPending ? <div className="mb-6 rounded-xl border border-attention/40 bg-attention-tint p-4"><h3 className="font-heading text-body font-semibold text-ink">Add your real name first</h3><p className="mt-1 text-note leading-5 text-ink-muted">Your name becomes part of the agreement and acknowledgement evidence.</p><Link to={appPaths.settings} className="mt-3 inline-flex text-control font-semibold text-secondary">Open Profile <ArrowRight size={15} /></Link></div> : null}

        {step === 1 ? <fieldset><legend className="font-heading text-[1.45rem] font-semibold tracking-[-0.02em] text-ink">Who is the {counterpartyRole}?</legend><p className="mt-2 text-note leading-5 text-ink-muted">Start with the {counterpartyRole}’s email address or phone number used to sign in. Financial details remain private until they verify it.</p>
          <div role="tablist" aria-label="Find by sign-in method" className="flex w-fit rounded-lg bg-surface-sunken p-1">{(["email", "phone"] as const).map((channel) => <button key={channel} type="button" role="tab" aria-selected={draft.inviteChannel === channel} onClick={() => changeChannel(channel)} className={cn("hit-target flex items-center gap-2 rounded-md px-3 py-1.5 text-note font-medium", draft.inviteChannel === channel ? "bg-surface text-ink shadow-sm" : "text-ink-muted")}>{channel === "email" ? <Mail size={14} /> : <Phone size={14} />}{titleCase(channel)}</button>)}</div>
          <div className="mt-4">
            <Field label={`${titleCase(counterpartyRole)}’s ${draft.inviteChannel === "email" ? "email address" : "phone number"}`} hint="Search starts after three characters. A complete identifier can match an exact Fyn account.">
              <div className="relative"><Search className="absolute top-[1.3rem] left-3 text-ink-muted" size={16} /><input autoFocus data-overlay-initial-focus value={draft.inviteValue} onChange={(event) => changeIdentifier(event.target.value)} required type={draft.inviteChannel === "email" ? "email" : "tel"} inputMode={draft.inviteChannel === "email" ? "email" : "tel"} autoComplete={draft.inviteChannel === "email" ? "email" : "tel"} role="combobox" aria-autocomplete="list" aria-expanded={currentSuggestions.length > 0} aria-controls="contact-suggestions" placeholder={draft.inviteChannel === "email" ? "rahul@example.com" : "98765 43210"} className={cn(fieldClass, "pl-9 pr-9")} />{contactQuery.isFetching && identifierHasSearchLength ? <Loader2 aria-label="Searching contacts" className="absolute top-[1.3rem] right-3 animate-spin text-ink-muted" size={16} /> : null}</div>
            </Field>
            {currentSuggestions.length ? <ul id="contact-suggestions" role="listbox" aria-label="Matching people" className="mt-2 overflow-hidden rounded-xl border border-line bg-surface shadow-sm">{currentSuggestions.map((contact) => <li key={`${contact.channel}:${contact.identifier}`}><button type="button" role="option" aria-selected={matchedContact?.identifier === contact.identifier} onClick={() => chooseContact(contact)} className="flex w-full items-center gap-3 border-b border-line px-3 py-3 text-left last:border-0 hover:bg-surface-sunken focus-visible:outline-2 focus-visible:outline-inset focus-visible:outline-ring"><span className="grid size-9 shrink-0 place-items-center rounded-full bg-secondary-tint text-secondary"><UserRoundCheck size={17} /></span><span className="min-w-0 flex-1"><strong className="block truncate text-control font-semibold text-ink">{contact.displayName}</strong><span className="block truncate text-note text-ink-muted">{contact.identifier}</span></span><span className="shrink-0 rounded-full bg-secondary-tint px-2 py-1 text-meta font-semibold text-secondary-hover">{contact.matchKind === "exact" ? "Exact Fyn account" : "Shared before"}</span></button></li>)}</ul> : null}
            {identifierHasSearchLength && !contactQuery.isFetching && !contactQuery.isError && currentSuggestions.length === 0 ? <p className="mt-2 text-meta leading-5 text-ink-muted">{identifierLooksComplete ? `No existing Fyn account matched. We’ll invite this ${draft.inviteChannel}.` : "No previous contact matched yet. Keep typing the complete email or phone number."}</p> : null}
            {contactQuery.isError ? <p role="status" className="mt-2 text-meta leading-5 text-ink-muted">Contact lookup is unavailable. You can still enter the details and send an invitation.</p> : null}
          </div>
          <div className="mt-5"><Field label={`${titleCase(counterpartyRole)}’s name`} hint={matchedContact ? "Pulled from their Fyn profile. You can adjust how the name appears in this plan." : "This name appears in the shared repayment plan."}><input value={draft.counterpartyName || matchedContact?.displayName || ""} onChange={(event) => setDraft((current) => ({ ...current, counterpartyName: event.target.value }))} required maxLength={120} autoComplete="name" placeholder={counterpartyRole === "borrower" ? "Rahul" : "Hari"} className={fieldClass} /></Field>{matchedContact ? <p role="status" className="mt-2 flex items-center gap-1.5 text-meta font-medium text-money-in"><CheckCircle2 size={14} />Matched to a Fyn account that signs in with this {draft.inviteChannel}</p> : null}</div>
          <p className="mt-4 text-meta leading-5 text-ink-muted">Partial suggestions only include people you have already shared a record with. The recipient must still verify this {draft.inviteChannel} before joining.</p>
        </fieldset> : null}

        {step === 0 ? <fieldset><legend className="font-heading text-[1.45rem] font-semibold tracking-[-0.02em] text-ink">What do you want to do?</legend><p className="mt-2 text-note leading-5 text-ink-muted">Start with your intent. The following questions will use lender or borrower language automatically.</p><div className="mt-6 grid gap-3 sm:grid-cols-2">
          {([
            ["record_given", "I already gave money", "They will acknowledge receiving it and returning it."],
            ["record_received", "I already received money", "I will acknowledge receiving it and returning it."],
            ["offer_to_lend", "I’m offering to lend", "Agree the terms first, then confirm when money moves."],
            ["request_to_borrow", "I’m asking to borrow", "They review the request before any money moves."],
          ] as const).map(([intent, label, detail]) => <button key={intent} type="button" aria-pressed={draft.intent === intent} onClick={() => setDraft((current) => ({ ...current, intent }))} className={cn("rounded-xl border p-5 text-left transition-all", draft.intent === intent ? "border-secondary bg-secondary-tint shadow-sm" : "border-line bg-surface hover:border-line-strong hover:bg-surface-sunken")}><span className="flex items-center gap-2 font-heading text-body font-semibold text-ink">{draft.intent === intent ? <CheckCircle2 size={18} className="text-secondary" /> : <span className="size-[18px] rounded-full border border-line-strong" />}{label}</span><span className="mt-2 block text-note leading-5 text-ink-muted">{detail}</span></button>)}
        </div></fieldset> : null}

        {step === 2 ? <fieldset><legend className="font-heading text-[1.45rem] font-semibold tracking-[-0.02em] text-ink">Set clear repayment terms</legend><p className="mt-2 text-note leading-5 text-ink-muted">The total effect of interest is calculated now so neither person has to interpret a bare percentage.</p><div className="mt-6 grid gap-5 sm:grid-cols-2">
          <div className="sm:col-span-2"><Field label={amountLabel} hint="Enter rupees; Fyn stores the exact paise value."><div className="relative"><IndianRupee className="absolute top-[1.3rem] left-3 text-ink-muted" size={16} /><input value={draft.amount} onChange={(event) => setDraft((current) => ({ ...current, amount: event.target.value }))} required inputMode="decimal" placeholder="25,000" className={cn(fieldClass, "pl-9")} /></div></Field></div>
          <Field label={moneyMoved ? (direction === "lent" ? "Date I gave the money" : "Date I received the money") : "Expected funding date"}><input type="date" value={draft.moneyDate} onChange={(event) => setDraft((current) => ({ ...current, moneyDate: event.target.value }))} required className={fieldClass} /></Field>
          <Field label={returnDateLabel}><input type="date" min={draft.moneyDate} value={draft.dueDate} onChange={(event) => setDraft((current) => ({ ...current, dueDate: event.target.value }))} required className={fieldClass} /></Field>
          <fieldset>
            <legend className="text-control font-medium text-ink-body">Interest rate</legend>
            <div role="radiogroup" aria-label="Interest period" className="mt-2 grid grid-cols-2 rounded-lg bg-surface-sunken p-1">
              {(["monthly", "yearly"] as const).map((period) => <button key={period} type="button" role="radio" aria-checked={draft.interestPeriod === period} onClick={() => setDraft((current) => ({ ...current, interestPeriod: period }))} className={cn("hit-target rounded-md px-3 py-2 text-note font-semibold transition-colors", draft.interestPeriod === period ? "bg-surface text-ink shadow-sm" : "text-ink-muted hover:text-ink-body")}>{period === "monthly" ? "Monthly" : "Yearly"}</button>)}
            </div>
            <div className="relative"><input aria-label={`${draft.interestPeriod === "monthly" ? "Monthly" : "Yearly"} interest rate`} type="number" min="0" max="100" step="0.01" value={draft.interestPercent} onChange={(event) => setDraft((current) => ({ ...current, interestPercent: event.target.value }))} required className={cn(fieldClass, "pr-9")} /><span className="absolute top-[1.2rem] right-3 text-note text-ink-muted">%</span></div>
            <p className="mt-1.5 text-meta leading-5 text-ink-muted">{rate ? `${interestBasis(draft.interestPeriod, draft.interestMode)} · ${formatMoney(interestMinor)} total interest` : "Use 0 for an interest-free plan."}</p>
            <details className="mt-3 rounded-lg border border-line bg-ground px-3 py-2">
              <summary className="cursor-pointer text-meta font-semibold text-ink-body">Advanced · calculation method</summary>
              <div role="radiogroup" aria-label="Interest calculation method" className="mt-3 grid gap-2">
                {(["simple", "compound"] as const).map((mode) => <button key={mode} type="button" role="radio" aria-checked={draft.interestMode === mode} onClick={() => setDraft((current) => ({ ...current, interestMode: mode }))} className={cn("rounded-lg border px-3 py-3 text-left", draft.interestMode === mode ? "border-secondary bg-secondary-tint" : "border-line bg-surface")}><strong className="block text-note text-ink">{mode === "simple" ? "Simple · fixed principal" : "Compound"}</strong><span className="mt-1 block text-meta leading-4 text-ink-muted">{mode === "simple" ? "Interest is calculated on the original principal." : "Interest is added each period before the next period is calculated."}</span></button>)}
              </div>
            </details>
          </fieldset>
          <Field label="Reason or context" hint="Optional, and visible to both people."><input value={draft.note} onChange={(event) => setDraft((current) => ({ ...current, note: event.target.value }))} maxLength={2000} placeholder="For the laptop" className={fieldClass} /></Field>
        </div></fieldset> : null}

        {step === 3 ? <div>
        {direction === "lent" ? <section><div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="font-heading text-[1.45rem] font-semibold tracking-[-0.02em] text-ink">Request documents from the borrower</h3><p className="mt-2 max-w-2xl text-note leading-5 text-ink-muted">Ask {counterpartyName || "the borrower"} for evidence they should provide. They choose it from their private repository after verifying the invitation; you review the shared copy before acknowledging.</p></div><Button type="button" variant="outline" onClick={() => addDocumentRequest()}><Plus /> Add request</Button></div>
          <div className="mt-4 flex flex-wrap gap-2"><button type="button" onClick={() => addDocumentRequest("Transfer receipt", "transfer_receipt")} className="rounded-full border border-line px-3 py-1.5 text-meta font-semibold text-ink-body hover:border-secondary">+ Transfer receipt</button><button type="button" onClick={() => addDocumentRequest("Cheque or assurance photo", "assurance_item")} className="rounded-full border border-line px-3 py-1.5 text-meta font-semibold text-ink-body hover:border-secondary">+ Assurance evidence</button><button type="button" onClick={() => addDocumentRequest("Existing signed note", "external_agreement")} className="rounded-full border border-line px-3 py-1.5 text-meta font-semibold text-ink-body hover:border-secondary">+ Existing document</button></div>
          {documentRequests.length ? <div className="mt-4 space-y-3">{documentRequests.map((request, index) => <div key={request.id} className="rounded-xl border border-line bg-ground p-4"><div className="flex items-center gap-2"><span className="grid size-7 place-items-center rounded-full bg-secondary-tint text-meta font-semibold text-secondary">{index + 1}</span><strong className="text-control text-ink">Borrower document request</strong><Button type="button" variant="ghost" size="icon-lg" className="ml-auto" aria-label={`Remove document request ${index + 1}`} onClick={() => setDocumentRequests((current) => current.filter((item) => item.id !== request.id))}><Trash2 /></Button></div><div className="mt-3 grid gap-3 sm:grid-cols-2"><Field label="Document name"><input value={request.label} required minLength={2} maxLength={120} placeholder="Transfer receipt" onChange={(event) => setDocumentRequests((current) => current.map((item) => item.id === request.id ? { ...item, label: event.target.value } : item))} className={fieldClass} /></Field><Field label="Category"><select value={request.classification} onChange={(event) => setDocumentRequests((current) => current.map((item) => item.id === request.id ? { ...item, classification: event.target.value as DraftDocumentRequest["classification"] } : item))} className={fieldClass}><option value="transfer_receipt">Transfer receipt</option><option value="assurance_item">Assurance evidence</option><option value="external_agreement">Existing agreement</option><option value="identity_evidence">Identity evidence</option><option value="supporting_evidence">Other evidence</option></select></Field><div className="sm:col-span-2"><Field label="Instructions" hint="Optional. Explain what is acceptable without requesting unnecessary sensitive data."><input value={request.instructions} maxLength={500} placeholder="Upload the receipt after the transfer is made" onChange={(event) => setDocumentRequests((current) => current.map((item) => item.id === request.id ? { ...item, instructions: event.target.value } : item))} className={fieldClass} /></Field></div></div><label className="mt-3 flex items-center gap-2 text-note text-ink-body"><input type="checkbox" checked={request.required} onChange={(event) => setDocumentRequests((current) => current.map((item) => item.id === request.id ? { ...item, required: event.target.checked } : item))} className="size-4 accent-[var(--color-secondary)]" />Required before the borrower can acknowledge</label></div>)}</div> : <div className="mt-4 rounded-xl border border-line bg-ground px-4 py-5 text-note leading-5 text-ink-muted">No borrower documents requested. This does not affect signing: both people still acknowledge the generated agreement through their verified Fyn session.</div>}
        </section> : <div><h3 className="font-heading text-[1.45rem] font-semibold tracking-[-0.02em] text-ink">Share supporting evidence</h3><p className="mt-2 text-note leading-5 text-ink-muted">Optional. Choose only documents you want the lender to review with this agreement.</p></div>}

        <fieldset className={cn(direction === "lent" && "mt-7 border-t border-line pt-7")}><legend className="font-heading text-body font-semibold text-ink">Share from my private repository <span className="font-normal text-ink-muted">· optional</span></legend><p className="mt-1 text-note leading-5 text-ink-muted">The lender does not need to upload anything. Select your own evidence only when it helps; Fyn copies selected files into this exact revision.</p>
          {documentLibrary.isPending ? <p className="mt-4 flex items-center gap-2 text-note text-ink-muted"><Loader2 size={15} className="animate-spin" />Loading your documents…</p> : documentLibrary.data?.length ? <ul className="mt-4 grid gap-2 sm:grid-cols-2">{documentLibrary.data.map((asset) => <li key={asset.id}><label className={cn("flex cursor-pointer items-center gap-3 rounded-xl border p-3", selectedAssetIds.includes(asset.id) ? "border-secondary bg-secondary-tint" : "border-line bg-surface")}><input type="checkbox" checked={selectedAssetIds.includes(asset.id)} onChange={() => toggleLibraryAsset(asset.id)} className="size-4 accent-[var(--color-secondary)]" /><FileText size={17} className="shrink-0 text-secondary" /><span className="min-w-0"><strong className="block truncate text-note text-ink">{asset.originalFilename}</strong><span className="text-meta text-ink-muted">{titleCase(asset.classification)} · {(asset.byteSize / 1024).toFixed(0)} KB</span></span></label></li>)}</ul> : <p className="mt-4 rounded-lg bg-ground px-4 py-3 text-note text-ink-muted">Your repository is empty. Add a file below or manage reusable documents in Profile.</p>}
          <label className="mt-4 flex cursor-pointer flex-col items-center rounded-2xl border border-dashed border-line-strong bg-ground px-6 py-7 text-center transition-colors hover:border-secondary"><UploadCloud className="text-secondary" /><span className="mt-2 text-control font-semibold text-ink">Add a new private document</span><span className="mt-1 text-note text-ink-muted">PDF, JPG, or PNG up to 10 MB. It remains in your repository after this agreement.</span><input type="file" multiple accept="application/pdf,image/png,image/jpeg" className="sr-only" onChange={(event) => { addFiles(event.target.files); event.target.value = ""; }} /></label>
          {attachments.length ? <ul className="mt-4 space-y-3">{attachments.map((attachment) => <li key={attachment.id} className="grid items-center gap-3 rounded-xl border border-line bg-surface p-3 sm:grid-cols-[auto_minmax(0,1fr)_12rem_auto]"><span className="grid size-10 place-items-center rounded-lg bg-secondary-tint text-secondary"><Paperclip size={17} /></span><div className="min-w-0"><strong className="block truncate text-control text-ink">{attachment.file.name}</strong><span className="text-meta text-ink-muted">Ready to add · {(attachment.file.size / 1024).toFixed(0)} KB</span></div><select aria-label={`Category for ${attachment.file.name}`} value={attachment.classification} onChange={(event) => setAttachments((current) => current.map((item) => item.id === attachment.id ? { ...item, classification: event.target.value as DraftAttachment["classification"] } : item))} className="manual-field h-10 rounded-lg border border-line-strong bg-surface px-2 text-note text-ink"><option value="supporting_evidence">Supporting evidence</option><option value="external_agreement">Existing agreement</option><option value="transfer_receipt">Transfer receipt</option><option value="assurance_item">Assurance item</option></select><Button type="button" variant="ghost" size="icon-lg" aria-label={`Remove ${attachment.file.name}`} onClick={() => setAttachments((current) => current.filter((item) => item.id !== attachment.id))}><Trash2 /></Button></li>)}</ul> : null}
          {sharedDocumentCount > 8 ? <p role="alert" className="mt-3 text-note font-medium text-danger-ink">Choose no more than eight files for one agreement revision.</p> : null}
        </fieldset>

        <fieldset className="mt-7 rounded-xl border border-line bg-ground p-4 sm:p-5">
          <legend className="px-1 text-control font-medium text-ink-body">Optional assurance item</legend>
          <p className="mt-1 text-note leading-5 text-ink-muted">Describe a cheque, gold item, or document only if both of you want it in the shared record. Fyn does not take custody or enforce it.</p>
          <select value={draft.securityKind} onChange={(event) => setDraft((current) => ({ ...current, securityKind: event.target.value as CreateDraft["securityKind"] }))} className={fieldClass} aria-label="Assurance item type">
            <option value="none">No assurance item</option><option value="gold">Gold item</option><option value="post_dated_cheque">Post-dated cheque</option><option value="cancelled_cheque">Cancelled cheque</option><option value="document">Document</option><option value="other">Other item</option>
          </select>
          {draft.securityKind !== "none" ? <div className="mt-4 grid gap-4 sm:grid-cols-2"><Field label="Description"><input value={draft.securityDescription} onChange={(event) => setDraft((current) => ({ ...current, securityDescription: event.target.value }))} required maxLength={240} placeholder="22k gold chain, approx. 12 g" className={fieldClass} /></Field><Field label="Masked reference" hint="Never enter a complete cheque or account number."><input value={draft.securityIdentifier} onChange={(event) => setDraft((current) => ({ ...current, securityIdentifier: event.target.value }))} maxLength={120} placeholder="Cheque ending 4821" className={fieldClass} /></Field><Field label="Stated value" hint="Optional description, not a valuation by Fyn."><input value={draft.securityValue} onChange={(event) => setDraft((current) => ({ ...current, securityValue: event.target.value }))} inputMode="decimal" placeholder="25,000" className={fieldClass} /></Field></div> : null}
        </fieldset></div> : null}

        {step === 4 ? <section aria-label="Agreement review"><p className="ledger-meta text-secondary">Final review</p><h3 className="mt-2 font-heading text-[1.45rem] font-semibold tracking-[-0.02em] text-ink">Review what {counterpartyName || "the other person"} will receive</h3><div className="mt-6 rounded-sm border border-line bg-[#fffefa] px-6 py-8 shadow-[0_8px_32px_rgba(23,34,27,.08)] sm:px-10"><div className="border-b border-line pb-5 text-center"><p className="ledger-meta text-secondary">Fyn verified acknowledgement</p><h4 className="mt-2 font-heading text-title font-semibold text-ink">Shared Repayment Agreement</h4><p className="mt-2 text-meta text-ink-muted">Draft revision 1 · Shared files {sharedDocumentCount} · Borrower requests {documentRequests.length}</p></div><p className="mt-6 text-body leading-7 text-ink-body"><strong>{profile.data?.displayName}</strong> and <strong>{counterpartyName}</strong> are recording a shared repayment plan for <strong>{principalMinor ? formatMoney(principalMinor) : "—"}</strong>. The amount is expected back by <strong>{formatDate(draft.dueDate)}</strong>, with {rate ? `${rate}% ${draft.interestPeriod} ${draft.interestMode} interest` : "no interest"}.</p><dl className="mt-6 grid gap-px overflow-hidden rounded-lg border border-line bg-line sm:grid-cols-2"><div className="bg-surface p-4"><dt className="ledger-meta">Principal</dt><dd className="mt-1 font-semibold text-ink">{principalMinor ? formatMoney(principalMinor) : "—"}</dd></div><div className="bg-surface p-4"><dt className="ledger-meta">Total repayable</dt><dd className="mt-1 font-semibold text-ink">{principalMinor ? formatMoney(principalMinor + interestMinor) : "—"}</dd></div><div className="bg-surface p-4"><dt className="ledger-meta">Interest method</dt><dd className="mt-1 font-semibold text-ink">{rate ? interestBasis(draft.interestPeriod, draft.interestMode) : "Interest-free"}</dd></div><div className="bg-surface p-4"><dt className="ledger-meta">Interest amount</dt><dd className="mt-1 font-semibold text-ink">{principalMinor ? formatMoney(interestMinor) : "—"}</dd></div><div className="bg-surface p-4"><dt className="ledger-meta">Funding state</dt><dd className="mt-1 font-semibold text-ink">{moneyMoved ? "Recorded as already moved" : "Confirmation required later"}</dd></div><div className="bg-surface p-4"><dt className="ledger-meta">Return date</dt><dd className="mt-1 font-semibold text-ink">{formatDate(draft.dueDate)}</dd></div></dl>{sharedDocumentCount ? <div className="mt-6"><p className="ledger-meta">Files I am sharing</p><ul className="mt-2 space-y-2">{selectedAssets.map((item) => <li key={item.id} className="flex items-center gap-2 text-note text-ink-body"><Paperclip size={14} className="text-secondary" />{item.originalFilename}<span className="ml-auto text-meta text-ink-muted">{titleCase(item.classification)}</span></li>)}{attachments.map((item) => <li key={item.id} className="flex items-center gap-2 text-note text-ink-body"><Paperclip size={14} className="text-secondary" />{item.file.name}<span className="ml-auto text-meta text-ink-muted">{titleCase(item.classification)}</span></li>)}</ul></div> : null}{documentRequests.length ? <div className="mt-6 rounded-xl border border-secondary-line bg-secondary-tint p-4"><p className="ledger-meta text-secondary">Requested from borrower</p><ul className="mt-2 space-y-2">{documentRequests.map((item) => <li key={item.id} className="flex items-center gap-2 text-note text-ink-body"><FileText size={14} className="text-secondary" />{item.label}<span className="ml-auto text-meta font-semibold text-ink-muted">{item.required ? "Required" : "Optional"}</span></li>)}</ul><p className="mt-3 text-meta leading-5 text-ink-muted">The borrower provides these after joining. Their upload creates a replacement revision for your review.</p></div> : null}<p className="mt-7 border-t border-line pt-5 text-meta leading-5 text-ink-muted">Your authenticated acknowledgement is tied to the exact terms and the files you share now. Requested borrower files are acknowledged in a replacement revision after they are provided.</p></div></section> : null}
          </div>

          <aside className="space-y-4 lg:sticky lg:top-6"><section aria-label="Live agreement summary" className="rounded-2xl border border-line bg-surface p-5"><p className="ledger-meta">Agreement summary</p><h3 className="mt-2 font-heading text-title font-semibold text-ink">{counterpartyName || (draft.intent ? "Choose a person" : "Choose your intent")}</h3><p className="mt-1 text-note text-ink-muted">{draft.intent ? direction === "lent" ? "You are the lender" : "You are the borrower" : "The form adapts to your role"}</p><dl className="mt-5 space-y-3 border-t border-line pt-4"><div className="flex justify-between gap-3"><dt className="text-note text-ink-muted">Principal</dt><dd className="text-control font-semibold text-ink">{principalMinor ? formatMoney(principalMinor) : "—"}</dd></div><div className="flex justify-between gap-3"><dt className="text-note text-ink-muted">Interest · {titleCase(draft.interestPeriod)}</dt><dd className="text-control font-semibold text-ink">{principalMinor ? formatMoney(interestMinor) : "—"}</dd></div><div className="flex justify-between gap-3"><dt className="text-note text-ink-muted">Total</dt><dd className="text-control font-semibold text-ink">{principalMinor ? formatMoney(principalMinor + interestMinor) : "—"}</dd></div><div className="flex justify-between gap-3"><dt className="text-note text-ink-muted">Files I share</dt><dd className="text-control font-semibold text-ink">{sharedDocumentCount}</dd></div>{direction === "lent" && draft.intent ? <div className="flex justify-between gap-3"><dt className="text-note text-ink-muted">Requested from borrower</dt><dd className="text-control font-semibold text-ink">{documentRequests.length}</dd></div> : null}</dl></section><TrustNote compact /></aside>
        </div>

        <footer className="sticky bottom-0 border-t border-line bg-surface/95 px-4 py-4 pb-[max(1rem,env(safe-area-inset-bottom))] backdrop-blur-sm sm:px-6"><div className="mx-auto flex max-w-7xl items-center justify-between gap-3"><Button type="button" size="lg" variant="ghost" disabled={step === 0 || mutation.isPending} onClick={() => setStep((value) => Math.max(0, value - 1))}><ArrowLeft /> Back</Button><div className="flex items-center gap-2"><span className="hidden text-note text-ink-muted sm:block">Step {step + 1} of {steps.length}</span>{step < steps.length - 1 ? <Button type="button" size="lg" disabled={!stepValid || mutation.isPending} onClick={() => setStep((value) => Math.min(steps.length - 1, value + 1))}>Continue <ArrowRight /></Button> : <Button type="submit" size="lg" disabled={!draft.intent || !personValid || !termsValid || !securityValid || !documentsValid || !hasRealProfile || mutation.isPending}>{mutation.isPending ? <Loader2 className="animate-spin" /> : <ShieldCheck />}{mutation.isPending ? `Securing ${sharedDocumentCount ? "documents and " : ""}agreement…` : "Acknowledge and send"}</Button>}</div></div></footer>
      </form>
    </section>;
}

function AgreementDocument({ loan }: { loan: PersonalLoanDetailOut }) {
  const revision = loan.documentRevision;
  const content = revision.content as { plainLanguage?: string; terms?: Record<string, unknown>; parties?: Record<string, string> };
  const agreementRateBps = Number(content.terms?.interestRateBps ?? content.terms?.annualRateBps ?? loan.interestRateBps);
  const agreementPeriod = content.terms?.interestPeriod === "monthly" ? "monthly" : content.terms?.interestPeriod === "yearly" ? "yearly" : loan.interestPeriod;
  const agreementMode = content.terms?.interestMode === "compound" ? "compound" : content.terms?.interestMode === "simple" ? "simple" : loan.interestMode;
  const currentParticipant = loan.participants.find((item) => item.isCurrentUser);
  const acceptedByMe = revision.acceptances.some((item) => item.participantId === currentParticipant?.id);
  return <section aria-label="Canonical agreement" className="overflow-hidden rounded-2xl border border-line bg-surface">
    <div className="flex flex-wrap items-center gap-3 border-b border-line bg-surface px-4 py-3 sm:px-5">
      <div className="min-w-0 flex-1"><p className="ledger-meta text-secondary">Canonical agreement</p><p className="mt-1 text-note text-ink-muted">Revision {revision.revisionNumber} · Evidence {revision.evidenceHash.slice(0, 12)}…</p></div>
      <a href={loanAgreementPdfUrl(loan.id)} target="_blank" rel="noreferrer" className="inline-flex h-10 items-center gap-2 rounded-md border border-line-strong px-3 text-note font-semibold text-ink-body hover:bg-surface-sunken"><FileText size={15} /> View PDF</a>
      <a href={loanEvidenceBundleUrl(loan.id)} className="inline-flex h-10 items-center gap-2 rounded-md border border-line-strong px-3 text-note font-semibold text-ink-body hover:bg-surface-sunken"><FileDown size={15} /> Evidence bundle</a>
    </div>
    <article className="agreement-paper m-3 rounded-sm border border-line bg-[#fffefa] px-5 py-8 shadow-[0_10px_35px_rgba(23,34,27,.07)] sm:m-5 sm:px-10 sm:py-10">
      <header className="border-b border-line pb-6 text-center"><p className="ledger-meta text-secondary">Authenticated electronic acknowledgement</p><h2 className="mt-2 font-heading text-[1.6rem] font-semibold tracking-[-0.025em] text-ink">Shared Repayment Agreement</h2><p className="mt-2 text-meta text-ink-muted">Agreement {loan.id} · Revision {revision.revisionNumber} · {titleCase(revision.state)}</p></header>
      <section className="mt-7"><p className="text-body leading-7 text-ink-body">{content.plainLanguage}</p><div className="mt-6 grid gap-px overflow-hidden rounded-lg border border-line bg-line sm:grid-cols-2"><div className="bg-surface p-4"><p className="ledger-meta">Lender</p><p className="mt-1 font-heading text-body font-semibold text-ink">{content.parties?.lender}</p></div><div className="bg-surface p-4"><p className="ledger-meta">Borrower</p><p className="mt-1 font-heading text-body font-semibold text-ink">{content.parties?.borrower}</p></div></div></section>
      <section className="mt-8"><h3 className="font-heading text-body font-semibold text-ink">Repayment terms</h3><dl className="mt-3 grid gap-px overflow-hidden rounded-lg border border-line bg-line sm:grid-cols-2 lg:grid-cols-4">
        <div className="bg-surface p-4"><dt className="ledger-meta">Principal</dt><dd className="mt-1 font-semibold text-ink">{formatMoney(loan.currentTerms.principalMinor, loan.currency)}</dd></div>
        <div className="bg-surface p-4"><dt className="ledger-meta">Interest rate</dt><dd className="mt-1 font-semibold text-ink">{agreementRateBps / 100}% {agreementPeriod}</dd><span className="mt-1 block text-meta leading-4 text-ink-muted">{agreementRateBps ? interestBasis(agreementPeriod, agreementMode) : "Interest-free"}</span></div>
        <div className="bg-surface p-4"><dt className="ledger-meta">Return date</dt><dd className="mt-1 font-semibold text-ink">{formatDate((content.terms?.dueDate as string) ?? loan.dueDate)}</dd></div>
        <div className="bg-surface p-4"><dt className="ledger-meta">Total repayable</dt><dd className="mt-1 font-semibold text-ink">{formatMoney(Number(content.terms?.totalRepayableMinor ?? loan.totalRepayableMinor), loan.currency)}</dd></div>
      </dl></section>
      {loan.securityItems.length ? <div className="mt-5 rounded-xl border border-attention/30 bg-attention-tint p-4"><div className="flex items-start gap-3"><ShieldCheck className="mt-0.5 shrink-0 text-attention-ink" size={18} /><div><h3 className="text-control font-semibold text-ink">Assurance and return record</h3>{loan.securityItems.map((item) => <div key={item.id} className="mt-2 text-note leading-5 text-ink-body"><strong>{titleCase(item.kind)}</strong> · {item.description}{item.maskedIdentifier ? ` · ${item.maskedIdentifier}` : ""}<span className="block text-ink-muted">Provided by {item.providedBy}, stated as held by {item.heldBy} · {titleCase(item.state)}</span></div>)}<p className="mt-2 text-meta leading-5 text-ink-muted">This is descriptive acknowledgement only. On closure, the stated holder records return and the provider confirms it.</p></div></div></div> : null}
      {revision.assets.length ? <section className="mt-8"><div className="flex items-center justify-between gap-3"><h3 className="font-heading text-body font-semibold text-ink">Supporting documents</h3><span className="rounded-full bg-secondary-tint px-2.5 py-1 text-meta font-semibold text-secondary">{revision.assets.length} attached</span></div><ul className="mt-3 divide-y divide-line overflow-hidden rounded-lg border border-line">{revision.assets.map((asset) => <li key={asset.id} className="flex items-center gap-3 px-3 py-3"><span className="grid size-9 place-items-center rounded-lg bg-secondary-tint text-secondary"><Paperclip size={15} /></span><div className="min-w-0 flex-1"><a href={documentAssetDownloadUrl(asset.id)} target="_blank" rel="noreferrer" className="block truncate text-control font-semibold text-ink hover:text-secondary">{asset.originalFilename}</a><p className="mt-0.5 text-meta text-ink-muted">{titleCase(asset.classification)} · {(asset.byteSize / 1024).toFixed(0)} KB</p></div><span className="hidden font-mono text-[10px] text-ink-muted sm:block">{asset.sha256.slice(0, 12)}…</span></li>)}</ul></section> : null}
      {revision.changes.length ? <div className="mt-5"><h3 className="ledger-meta">What changed in this revision</h3><ul className="mt-2 space-y-2">{revision.changes.map((change) => <li key={change.id} className="flex items-start gap-2 text-note text-ink-body"><PencilLine className="mt-0.5 shrink-0 text-secondary" size={14} /><span><strong className="font-semibold">{change.summary}</strong> by {change.authoredBy}</span></li>)}</ul></div> : null}
      <section className="mt-8 border-t border-line pt-6">
        <h3 className="font-heading text-body font-semibold text-ink">Signatures and independent acknowledgements</h3>
        <p className="mt-1 text-note leading-5 text-ink-muted">No handwritten signature image is required. Submitting the acknowledgement records that person’s authenticated electronic signature for this exact revision and file manifest. It is not a certificate-based digital signature or regulated eSign.</p>
        <div className="mt-3 grid gap-3">
          {loan.participants.map((participant) => {
            const acceptance = revision.acceptances.find((item) => item.participantId === participant.id);
            return <div key={participant.id} className={cn("rounded-lg border p-4", acceptance ? "border-secondary-line bg-secondary-tint" : "border-line bg-surface-sunken")}>
              <div className="flex flex-wrap items-center gap-2">
                {acceptance ? <CheckCircle2 size={17} className="text-secondary" /> : <Clock3 size={17} className="text-ink-muted" />}
                <strong className="text-control text-ink">{acceptance ? `Electronically acknowledged by ${participant.displayName}` : participant.displayName}</strong>
                <span className="ml-auto text-meta font-semibold text-ink-muted">{acceptance ? formatInstant(acceptance.acceptedAt) : "Awaiting acknowledgement"}</span>
              </div>
              {acceptance ? <>
                <p className="mt-2 ledger-meta text-secondary">Authenticated electronic acknowledgement</p>
                <p className="mt-2 text-note leading-5 text-ink-body">{acceptance.statementText}</p>
                <p className="mt-2 text-meta font-medium text-ink-muted">{acceptance.actorIdentifierMasked ?? "Authenticated Fyn account"} · {acceptance.actorTimezone}</p>
                {acceptance.requestIpHash ? <p className="mt-1 font-mono text-[10px] text-ink-muted">Network fingerprint {acceptance.requestIpHash.slice(0, 12)}…</p> : null}
              </> : <p className="mt-2 text-note text-ink-muted">This person has not yet signed revision {revision.revisionNumber} through acknowledgement.</p>}
            </div>;
          })}
        </div>
      </section>
      <section className="mt-8 border-t border-line pt-5"><p className="ledger-meta">Evidence fingerprints</p><dl className="mt-3 space-y-2 font-mono text-[10px] leading-4 text-ink-muted"><div><dt className="inline font-sans font-semibold text-ink-body">Content </dt><dd className="inline break-all">{revision.contentHash}</dd></div><div><dt className="inline font-sans font-semibold text-ink-body">Attachments </dt><dd className="inline break-all">{revision.manifestHash}</dd></div><div><dt className="inline font-sans font-semibold text-ink-body">Combined </dt><dd className="inline break-all">{revision.evidenceHash}</dd></div></dl></section>
      {acceptedByMe ? <p className="mt-6 flex items-center gap-2 text-note font-semibold text-money-in"><CheckCircle2 size={16} /> You acknowledged this exact revision.</p> : null}
    </article>
  </section>;
}

function RevisionSnapshot({ revision }: { revision: DocumentRevisionOut }) {
  const content = revision.content as { terms?: Record<string, unknown>; parties?: Record<string, string>; plainLanguage?: string };
  const terms = content.terms ?? {};
  const rateBps = Number(terms.interestRateBps ?? terms.annualRateBps ?? 0);
  const period = terms.interestPeriod === "monthly" ? "monthly" : "yearly";
  const mode = terms.interestMode === "compound" ? "compound" : "simple";
  const currency = String(terms.currency ?? "INR");
  return <article aria-label={`Revision ${revision.revisionNumber} details`} className="mt-4 rounded-xl border border-secondary-line bg-ground p-4 sm:p-5">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="ledger-meta text-secondary">Revision {revision.revisionNumber}</p><h3 className="mt-1 font-heading text-body font-semibold text-ink">{content.parties?.lender} and {content.parties?.borrower}</h3><p className="mt-1 text-note text-ink-muted">Proposed by {revision.authoredBy} · {formatInstant(revision.proposedAt)}</p></div><StatusBadge status={revision.state} /></div>
    <p className="mt-4 text-note leading-5 text-ink-body">{content.plainLanguage}</p>
    <dl className="mt-4 grid gap-px overflow-hidden rounded-lg border border-line bg-line sm:grid-cols-2"><div className="bg-surface p-3"><dt className="ledger-meta">Principal</dt><dd className="mt-1 text-control font-semibold text-ink">{formatMoney(Number(terms.principalMinor ?? 0), currency)}</dd></div><div className="bg-surface p-3"><dt className="ledger-meta">Total repayable</dt><dd className="mt-1 text-control font-semibold text-ink">{formatMoney(Number(terms.totalRepayableMinor ?? 0), currency)}</dd></div><div className="bg-surface p-3"><dt className="ledger-meta">Interest</dt><dd className="mt-1 text-control font-semibold text-ink">{rateBps ? `${rateBps / 100}% ${period} ${mode}` : "Interest-free"}</dd></div><div className="bg-surface p-3"><dt className="ledger-meta">Return date</dt><dd className="mt-1 text-control font-semibold text-ink">{formatDate(String(terms.dueDate ?? ""))}</dd></div></dl>
    {revision.changes.length ? <section className="mt-4"><h4 className="text-control font-semibold text-ink">Changes from revision {revision.revisionNumber - 1}</h4><ul className="mt-2 space-y-2">{revision.changes.map((change) => <li key={change.id} className="flex items-start gap-2 text-note text-ink-body"><PencilLine size={14} className="mt-0.5 shrink-0 text-secondary" /><span>{change.summary}<span className="block text-meta text-ink-muted">{change.authoredBy}</span></span></li>)}</ul></section> : <p className="mt-4 text-note text-ink-muted">This is the first terms revision, or only its supporting-document manifest changed.</p>}
    {revision.assets.length ? <section className="mt-4"><h4 className="text-control font-semibold text-ink">Files in this revision</h4><ul className="mt-2 space-y-2">{revision.assets.map((asset) => <li key={asset.id}><a href={documentAssetDownloadUrl(asset.id)} target="_blank" rel="noreferrer" className="flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-note font-semibold text-ink hover:border-secondary"><FileText size={14} className="text-secondary" />{asset.originalFilename}<span className="ml-auto font-mono text-[10px] text-ink-muted">{asset.sha256.slice(0, 10)}…</span></a></li>)}</ul></section> : null}
    <section className="mt-4"><h4 className="text-control font-semibold text-ink">Acknowledgement evidence</h4>{revision.acceptances.length ? <ul className="mt-2 space-y-2">{revision.acceptances.map((acceptance) => <li key={acceptance.participantId} className="rounded-lg border border-line bg-surface p-3"><p className="text-note font-semibold text-ink">Electronically acknowledged by {acceptance.participantName}</p><p className="mt-1 text-meta text-ink-muted">{formatInstant(acceptance.acceptedAt)} · {acceptance.actorIdentifierMasked ?? "Authenticated Fyn account"}</p>{acceptance.requestIpHash ? <p className="mt-1 font-mono text-[10px] text-ink-muted">Network fingerprint {acceptance.requestIpHash.slice(0, 12)}…</p> : null}</li>)}</ul> : <p className="mt-2 text-note text-ink-muted">No acknowledgement was recorded for this revision.</p>}</section>
    <details className="mt-4"><summary className="cursor-pointer text-meta font-semibold text-ink-body">Evidence fingerprints</summary><dl className="mt-2 space-y-1 font-mono text-[10px] leading-4 text-ink-muted"><div className="break-all">Content {revision.contentHash}</div><div className="break-all">Manifest {revision.manifestHash}</div><div className="break-all">Combined {revision.evidenceHash}</div></dl></details>
  </article>;
}

function RevisionHistory({ documentId, currentRevisionId }: { documentId: string; currentRevisionId: string }) {
  const [open, setOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const query = useQuery({ queryKey: ["shared-document-revisions", documentId], queryFn: () => loadSharedDocumentRevisions(documentId), enabled: open });
  const selected = query.data?.find((revision) => revision.id === selectedId) ?? null;
  return <section className="rounded-xl border border-line bg-surface">
    <button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)} className="flex w-full items-center gap-3 px-4 py-4 text-left sm:px-5"><FileClock className="text-secondary" size={18} /><span className="min-w-0 flex-1"><strong className="block text-control font-semibold text-ink">Revision history</strong><span className="text-note text-ink-muted">Open any version to inspect its terms, changes, files, acknowledgements, and fingerprints.</span></span><ChevronRight className={cn("text-ink-muted transition-transform", open && "rotate-90")} size={18} /></button>
    {open ? <div className="border-t border-line p-4 sm:p-5">{query.isPending ? <p role="status" className="flex items-center gap-2 text-note text-ink-muted"><Loader2 className="animate-spin" />Loading revisions…</p> : query.isError ? <p role="alert" className="text-note text-danger-ink">Revision history couldn’t be loaded.</p> : <><ol className="space-y-3">{query.data?.map((revision: DocumentRevisionOut) => <li key={revision.id} className={cn("rounded-lg border p-3", selectedId === revision.id ? "border-secondary bg-secondary-tint" : "border-line bg-ground")}><div className="flex flex-wrap items-center gap-2"><strong className="text-control text-ink">Revision {revision.revisionNumber}</strong>{revision.id === currentRevisionId ? <span className="rounded-full bg-secondary-tint px-2 py-1 text-meta font-semibold text-secondary">Current view</span> : null}<StatusBadge status={revision.state} /><Button type="button" variant="ghost" size="sm" className="ml-auto" aria-expanded={selectedId === revision.id} onClick={() => setSelectedId((current) => current === revision.id ? null : revision.id)}>{selectedId === revision.id ? "Hide" : "View revision"}</Button></div><p className="mt-1 text-note text-ink-muted">{revision.authoredBy} · {formatInstant(revision.proposedAt)} · {revision.changes.length} changed fields · {revision.assets.length} files</p><p className="mt-2 truncate font-mono text-[11px] text-ink-muted">{revision.evidenceHash}</p></li>)}</ol>{selected ? <RevisionSnapshot revision={selected} /> : null}</>}</div> : null}
  </section>;
}

function ActionProblem({ children }: { children: string }) {
  return <p role="alert" className="rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note text-danger-ink">{children}</p>;
}

function DocumentRequestsPanel({ loan }: { loan: PersonalLoanDetailOut }) {
  const queryClient = useQueryClient();
  const [selection, setSelection] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<string | null>(null);
  const requests = loan.documentRequests;
  const mine = requests.filter((item) => item.requestedFromCurrentUser && item.state === "requested");
  const library = useQuery({ queryKey: ["document-assets"], queryFn: loadDocumentAssets, enabled: mine.length > 0, retry: false });
  const update = (next: PersonalLoanDetailOut) => {
    queryClient.setQueryData(["personal-loan", loan.id], next);
    void queryClient.invalidateQueries({ queryKey: ["personal-loans"] });
    setSelection({});
    setProblem(null);
  };
  const upload = useMutation({
    mutationFn: ({ file, classification }: { requestId: string; file: File; classification: string }) => uploadDocumentAsset(file, classification),
    onSuccess: (asset, variables) => {
      queryClient.setQueryData(["document-assets"], (current: DocumentAssetOut[] | undefined) => current ? [asset, ...current] : [asset]);
      setSelection((current) => ({ ...current, [variables.requestId]: asset.id }));
      setProblem(null);
    },
    onError: (cause: Error) => setProblem(cause.message),
  });
  const fulfill = useMutation({
    mutationFn: () => fulfillPersonalLoanDocumentRequests(loan.id, {
      items: mine.filter((item) => selection[item.id]).map((item) => ({ requestId: item.id, assetId: selection[item.id] })),
      expectedRowVersion: loan.rowVersion,
    }),
    onSuccess: ({ loan: next }) => update(next),
    onError: (cause: Error) => setProblem(cause.message),
  });
  if (!requests.length) return null;
  const requiredReady = mine.filter((item) => item.required).every((item) => Boolean(selection[item.id]));
  const hasSelection = mine.some((item) => Boolean(selection[item.id]));
  const selectedIds = new Set(Object.values(selection));

  return <section aria-label="Requested documents" className="rounded-2xl border border-secondary-line bg-surface">
    <div className="flex items-start gap-3 border-b border-line bg-secondary-tint px-4 py-4 sm:px-5"><span className="grid size-10 shrink-0 place-items-center rounded-xl bg-surface text-secondary"><FileText size={18} /></span><div><h2 className="font-heading text-body font-semibold text-ink">{mine.length ? "Documents requested from you" : "Borrower document requests"}</h2><p className="mt-1 text-note leading-5 text-ink-muted">{mine.length ? "Choose a private repository file for each required item. Submitting creates a shared copy and a new agreement revision for the lender to review." : "Track what the borrower must provide before the agreement can be completed."}</p></div></div>
    <div className="space-y-3 p-4 sm:p-5">
      {problem ? <ActionProblem>{problem}</ActionProblem> : null}
      {requests.map((request) => <article key={request.id} className="rounded-xl border border-line bg-ground p-4"><div className="flex flex-wrap items-start gap-2"><div className="min-w-0 flex-1"><h3 className="text-control font-semibold text-ink">{request.label}</h3><p className="mt-1 text-meta text-ink-muted">Requested by {request.requestedBy} · {request.required ? "Required" : "Optional"} · {titleCase(request.classification)}</p>{request.instructions ? <p className="mt-2 text-note leading-5 text-ink-body">{request.instructions}</p> : null}</div><StatusBadge status={request.state} /></div>
        {request.state === "fulfilled" && request.fulfilledAsset ? <a href={documentAssetDownloadUrl(request.fulfilledAsset.id)} target="_blank" rel="noreferrer" className="mt-3 flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-note font-semibold text-ink hover:border-secondary"><FileText size={15} className="text-secondary" />{request.fulfilledAsset.originalFilename}<DownloadIcon /></a> : null}
        {request.state === "requested" && request.requestedFromCurrentUser ? <div className="mt-3 grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]"><select aria-label={`Private document for ${request.label}`} value={selection[request.id] ?? ""} onChange={(event) => setSelection((current) => ({ ...current, [request.id]: event.target.value }))} className={cn(fieldClass, "mt-0")}><option value="">Choose from private repository…</option>{library.data?.map((asset) => <option key={asset.id} value={asset.id} disabled={selectedIds.has(asset.id) && selection[request.id] !== asset.id}>{asset.originalFilename} · {titleCase(asset.classification)}</option>)}</select><label className="inline-flex h-11 cursor-pointer items-center justify-center gap-2 rounded-md border border-line-strong bg-surface px-3 text-note font-semibold text-ink hover:bg-surface-sunken">{upload.isPending && upload.variables?.requestId === request.id ? <Loader2 size={15} className="animate-spin" /> : <UploadCloud size={15} />}Upload new<input type="file" accept="application/pdf,image/png,image/jpeg" disabled={upload.isPending} className="sr-only" onChange={(event) => { const file = event.target.files?.[0]; if (file) upload.mutate({ requestId: request.id, file, classification: request.classification }); event.target.value = ""; }} /></label></div> : null}
      </article>)}
      {mine.length ? <><p className="flex items-start gap-2 text-meta leading-5 text-ink-muted"><ShieldCheck size={15} className="mt-0.5 shrink-0 text-secondary" />Your originals stay private. Only the copied files listed in the replacement revision become visible to both people.</p><Button type="button" size="lg" disabled={!requiredReady || !hasSelection || fulfill.isPending || upload.isPending} onClick={() => fulfill.mutate()}>{fulfill.isPending ? <Loader2 className="animate-spin" /> : <ShieldCheck />}Provide documents and acknowledge new revision</Button></> : null}
    </div>
  </section>;
}

function DownloadIcon() {
  return <FileDown className="ml-auto text-ink-muted" size={15} />;
}

function LoanActions({ loan }: { loan: PersonalLoanDetailOut }) {
  const queryClient = useQueryClient();
  const [view, setView] = useState<"none" | "funding" | "payment" | "amend" | "reminder">("none");
  const [acknowledgementChecked, setAcknowledgementChecked] = useState(false);
  const [fundingDate, setFundingDate] = useState(localDate());
  const [fundingNote, setFundingNote] = useState("");
  const [amount, setAmount] = useState("");
  const [paymentDate, setPaymentDate] = useState(localDate());
  const [paymentNote, setPaymentNote] = useState("");
  const [dueDate, setDueDate] = useState(loan.currentTerms.dueDate);
  const [rate, setRate] = useState(String(loan.currentTerms.interestRateBps / 100));
  const [ratePeriod, setRatePeriod] = useState<"monthly" | "yearly">(loan.currentTerms.interestPeriod);
  const [rateMode, setRateMode] = useState<"simple" | "compound">(loan.currentTerms.interestMode);
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
  const funding = useMutation({ mutationFn: () => recordPersonalLoanFunding(loan.id, { occurredOn: fundingDate, note: fundingNote.trim() || null }), onSuccess: ({ loan: next }) => update(next), onError: fail });
  const confirmFunding = useMutation({ mutationFn: () => confirmPersonalLoanPayment(loan.fundingCashflow!.id, loan.rowVersion), onSuccess: ({ loan: next }) => update(next), onError: fail });
  const payment = useMutation({ mutationFn: () => {
    const minor = parseAmountToMinor(amount);
    if (!minor) throw new Error("Enter a payment amount greater than zero.");
    return recordPersonalLoanPayment(loan.id, { amountMinor: minor, occurredOn: paymentDate, note: paymentNote.trim() || null });
  }, onSuccess: ({ loan: next }) => update(next), onError: fail });
  const amend = useMutation({ mutationFn: () => proposePersonalLoanTerms(loan.id, { dueDate, interestRateBps: Math.round(Number(rate) * 100), interestPeriod: ratePeriod, interestMode: rateMode, note: termNote.trim() || null, expectedRowVersion: loan.rowVersion }), onSuccess: ({ loan: next }) => update(next), onError: fail });
  const reminder = useMutation({ mutationFn: () => sendPersonalLoanReminder(loan.id, { tone: "friendly", note: reminderNote.trim() || null }), onSuccess: (result) => { setProblem(null); setSuccess(`Reminder queued by ${result.channel} to ${result.destinationMasked}.`); setView("none"); setReminderNote(""); }, onError: fail });
  const close = useMutation({ mutationFn: () => closePersonalLoan(loan.id), onSuccess: ({ loan: next }) => update(next), onError: fail });
  const busy = accept.isPending || funding.isPending || confirmFunding.isPending || payment.isPending || amend.isPending || reminder.isPending || close.isPending;
  const me = loan.participants.find((item) => item.isCurrentUser);
  const awaitingMyDocumentAcceptance = loan.documentRevision.state === "proposed" && !loan.documentRevision.acceptances.some((item) => item.participantId === me?.id);
  const missingRequiredDocuments = loan.documentRequests.some((item) => item.requestedFromCurrentUser && item.required && item.state === "requested");
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
    {awaitingMyDocumentAcceptance && !missingRequiredDocuments ? <div className="mt-4 rounded-xl border border-secondary-line bg-secondary-tint p-4"><div className="flex items-start gap-3"><UserRoundCheck className="mt-0.5 shrink-0 text-secondary" /><div className="min-w-0"><h3 className="font-heading text-body font-semibold text-ink">Sign this revision by acknowledging</h3><p className="mt-1 text-note leading-5 text-ink-muted">Review the canonical agreement and every supporting file first. No handwritten signature or uploaded signature image is required: submission records your authenticated electronic acknowledgement against the exact revision and file fingerprints. This is not a certificate-based digital signature or regulated eSign.</p><label className="mt-4 flex cursor-pointer items-start gap-3 rounded-lg border border-secondary-line bg-surface p-3 text-note leading-5 text-ink-body"><input type="checkbox" checked={acknowledgementChecked} onChange={(event) => setAcknowledgementChecked(event.target.checked)} className="mt-1 size-4 accent-[var(--color-secondary)]" /><span>I reviewed revision {loan.documentRevision.revisionNumber} and its supporting documents, and I acknowledge this shared record.</span></label><Button type="button" className="mt-4" disabled={busy || !acknowledgementChecked} onClick={() => accept.mutate()}>{accept.isPending ? <Loader2 className="animate-spin" /> : <ShieldCheck />}Acknowledge as my signature · revision {loan.documentRevision.revisionNumber}</Button><Button type="button" variant="ghost" className="mt-2" disabled={busy} onClick={() => setView("amend")}><PencilLine /> Propose changes instead</Button></div></div></div> : null}
    {awaitingMyDocumentAcceptance && missingRequiredDocuments ? <p className="mt-4 rounded-lg border border-attention/40 bg-attention-tint px-4 py-3 text-note leading-5 text-attention-ink">Provide every required borrower document first. The acknowledgement will then apply to the new revision and its exact file fingerprints.</p> : null}

    <div className="mt-4 flex flex-wrap gap-2">
      {loan.status === "funding_pending" && me?.role === "lender" && !loan.fundingCashflow ? <Button type="button" onClick={() => setView(view === "funding" ? "none" : "funding")}><HandCoins /> Record money sent</Button> : null}
      {loan.status === "funding_pending" && me?.role === "borrower" && loan.fundingCashflow?.state === "proposed" ? <Button type="button" disabled={busy} onClick={() => confirmFunding.mutate()}>{confirmFunding.isPending ? <Loader2 className="animate-spin" /> : <Check />}Confirm money received</Button> : null}
      {loan.status === "active" ? <><Button type="button" variant="outline" onClick={() => setView(view === "payment" ? "none" : "payment")}><ReceiptIndianRupee /> Record payment</Button><Button type="button" variant="outline" onClick={() => setView(view === "amend" ? "none" : "amend")}><PencilLine /> Propose change</Button><Button type="button" variant="ghost" onClick={() => setView(view === "reminder" ? "none" : "reminder")}><MessageCircleMore /> Send reminder</Button></> : null}
      {canActOnClosure ? <Button type="button" disabled={busy} onClick={() => close.mutate()}>{close.isPending ? <Loader2 className="animate-spin" /> : <CheckCircle2 />}{closureProposal ? (loan.securityItems.length ? "Confirm item returned and close" : "Confirm closure") : (loan.securityItems.length ? "Mark item returned and propose closure" : "Propose closure")}</Button> : null}
    </div>
    {loan.status === "settlement_pending" && loan.securityItems.length > 0 && me?.role === "borrower" && !closureProposal ? <p className="mt-3 text-note leading-5 text-ink-muted">Waiting for {loan.securityItems[0].heldBy} to record return of the assurance item before you confirm closure.</p> : null}
    {loan.status === "settlement_pending" && loan.securityItems.length > 0 && me?.role === "lender" && closureProposal ? <p className="mt-3 text-note leading-5 text-ink-muted">Return recorded. Waiting for {loan.securityItems[0].providedBy} to confirm receipt and close the plan.</p> : null}

    {view === "funding" ? <form className="mt-5 grid gap-4 rounded-xl bg-ground p-4" onSubmit={(event) => { event.preventDefault(); funding.mutate(); }}><Field label="Date money was sent"><input autoFocus type="date" value={fundingDate} onChange={(event) => setFundingDate(event.target.value)} required className={fieldClass} /></Field><Field label="Transfer reference" hint="Optional. Do not enter a complete bank-account number."><input value={fundingNote} onChange={(event) => setFundingNote(event.target.value)} maxLength={500} placeholder="UPI or bank reference" className={fieldClass} /></Field><div className="flex gap-2"><Button type="submit" disabled={busy}>{funding.isPending ? <Loader2 className="animate-spin" /> : <HandCoins />}Record for borrower confirmation</Button><Button type="button" variant="ghost" onClick={() => setView("none")}>Cancel</Button></div></form> : null}

    {view === "payment" ? <form className="mt-5 grid gap-4 rounded-xl bg-ground p-4 sm:grid-cols-2" onSubmit={(event) => { event.preventDefault(); payment.mutate(); }}><Field label="Amount paid"><input autoFocus value={amount} onChange={(event) => setAmount(event.target.value)} required inputMode="decimal" placeholder="5,000" className={fieldClass} /></Field><Field label="Payment date"><input type="date" value={paymentDate} onChange={(event) => setPaymentDate(event.target.value)} required className={fieldClass} /></Field><div className="sm:col-span-2"><Field label="Note"><input value={paymentNote} onChange={(event) => setPaymentNote(event.target.value)} maxLength={500} placeholder="Bank transfer reference, optional" className={fieldClass} /></Field></div><div className="flex gap-2 sm:col-span-2"><Button type="submit" disabled={busy}>{payment.isPending ? <Loader2 className="animate-spin" /> : null}Record for confirmation</Button><Button type="button" variant="ghost" onClick={() => setView("none")}>Cancel</Button></div></form> : null}
    {view === "amend" ? <form className="mt-5 grid gap-4 rounded-xl bg-ground p-4 sm:grid-cols-2" onSubmit={(event) => { event.preventDefault(); amend.mutate(); }}><Field label="New return date"><input autoFocus type="date" min={loan.moneyDate} value={dueDate} onChange={(event) => setDueDate(event.target.value)} required className={fieldClass} /></Field><fieldset><legend className="text-control font-medium text-ink-body">Interest rate</legend><div role="radiogroup" aria-label="Revised interest period" className="mt-2 grid grid-cols-2 rounded-lg bg-surface-sunken p-1">{(["monthly", "yearly"] as const).map((period) => <button key={period} type="button" role="radio" aria-checked={ratePeriod === period} onClick={() => setRatePeriod(period)} className={cn("hit-target rounded-md px-3 py-2 text-note font-semibold", ratePeriod === period ? "bg-surface text-ink shadow-sm" : "text-ink-muted")}>{titleCase(period)}</button>)}</div><div className="relative"><input aria-label={`Revised ${ratePeriod} interest rate`} type="number" min="0" max="100" step="0.01" value={rate} onChange={(event) => setRate(event.target.value)} required className={cn(fieldClass, "pr-9")} /><span className="absolute top-[1.2rem] right-3 text-note text-ink-muted">%</span></div><p className="mt-1.5 text-meta text-ink-muted">{Number(rate) ? interestBasis(ratePeriod, rateMode) : "Interest-free"}</p><details className="mt-2 text-meta text-ink-muted"><summary className="cursor-pointer font-semibold text-ink-body">Advanced · calculation method</summary><div role="radiogroup" aria-label="Revised interest calculation method" className="mt-2 flex gap-2">{(["simple", "compound"] as const).map((mode) => <button key={mode} type="button" role="radio" aria-checked={rateMode === mode} onClick={() => setRateMode(mode)} className={cn("rounded-md border px-3 py-2 font-semibold", rateMode === mode ? "border-secondary bg-secondary-tint text-ink" : "border-line bg-surface")}>{mode === "simple" ? "Simple" : "Compound"}</button>)}</div></details></fieldset><div className="sm:col-span-2"><Field label="Why is this changing?"><input value={termNote} onChange={(event) => setTermNote(event.target.value)} maxLength={2000} className={fieldClass} /></Field></div><p className="text-note leading-5 text-ink-muted sm:col-span-2">This creates a new immutable document revision. The current plan remains active until both people acknowledge the new one.</p><div className="flex gap-2 sm:col-span-2"><Button type="submit" disabled={busy}>{amend.isPending ? <Loader2 className="animate-spin" /> : null}Propose revision</Button><Button type="button" variant="ghost" onClick={() => setView("none")}>Cancel</Button></div></form> : null}
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
            <div className="space-y-5"><DocumentRequestsPanel loan={loan} /><AgreementDocument loan={loan} /><Payments loan={loan} /><RevisionHistory documentId={loan.documentRevision.documentId} currentRevisionId={loan.documentRevision.id} /></div>
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
