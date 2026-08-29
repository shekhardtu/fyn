import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Coins, Download, FileText, Globe2, Loader2, LogOut, Mail, Pencil, Plus, ShieldCheck, Smartphone, Trash2, TriangleAlert, UploadCloud } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useNavigate } from "react-router";
import { appPaths } from "@/routing/paths";
import { CHANNEL_COPY, CodeExchange } from "@/components/sign-in";
import { SettingsGroup, settingsProblem, settingsSaved } from "@/components/settings-parts";
import { Button } from "@/components/ui/button";
import { Combobox, type ComboboxOption } from "@/components/ui/combobox";
import { deleteDocumentAsset, documentAssetDownloadUrl, getProfile, isUnauthorized, loadDocumentAssets, removeIdentity, signOut, startLinkCode, updateProfile, uploadDocumentAsset, verifyLinkCode, type OtpChannel, type Profile } from "@/lib/api";
import type { DocumentAssetOut, IdentityOut } from "@/lib/protocol";

const PROVIDER_COPY: Record<IdentityOut["provider"], { label: string; icon: ReactNode }> = {
  phone: { label: "Phone number", icon: <Smartphone /> },
  email: { label: "Email address", icon: <Mail /> },
  google: { label: "Google", icon: <GoogleMark /> },
};

const CURRENCY_OPTIONS: ComboboxOption[] = [
  { value: "INR", label: "Indian rupee (INR)" },
  { value: "USD", label: "US dollar (USD)" },
  { value: "EUR", label: "Euro (EUR)" },
  { value: "GBP", label: "Pound sterling (GBP)" },
  { value: "AED", label: "UAE dirham (AED)" },
  { value: "SGD", label: "Singapore dollar (SGD)" },
  { value: "AUD", label: "Australian dollar (AUD)" },
  { value: "CAD", label: "Canadian dollar (CAD)" },
  { value: "JPY", label: "Japanese yen (JPY)" },
  { value: "CNY", label: "Chinese yuan (CNY)" },
  { value: "CHF", label: "Swiss franc (CHF)" },
  { value: "NZD", label: "New Zealand dollar (NZD)" },
];

const FALLBACK_TIMEZONES = [
  "Asia/Kolkata",
  "Asia/Dubai",
  "Asia/Singapore",
  "Asia/Tokyo",
  "Europe/London",
  "Europe/Berlin",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "Australia/Sydney",
  "Pacific/Auckland",
  "UTC",
];

const SUPPORTED_TIMEZONES = [...new Set([
  ...FALLBACK_TIMEZONES,
  ...(typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : []),
])];

function timezoneLabel(timezone: string) {
  const offset = new Intl.DateTimeFormat("en", { timeZone: timezone, timeZoneName: "shortOffset" })
    .formatToParts(new Date())
    .find((part) => part.type === "timeZoneName")?.value;
  return `${timezone.replaceAll("_", " ")}${offset ? ` (${offset})` : ""}`;
}

function GoogleMark() {
  return <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden focusable="false">
    <path fill="#4285F4" d="M23 12.3c0-.8-.1-1.6-.2-2.3H12v4.5h6.2a5.3 5.3 0 0 1-2.3 3.5v2.9h3.7c2.2-2 3.4-5 3.4-8.6Z" />
    <path fill="#34A853" d="M12 23.5c3.1 0 5.7-1 7.6-2.8l-3.7-2.9c-1 .7-2.3 1.1-3.9 1.1-3 0-5.5-2-6.4-4.7H1.8v3A11.5 11.5 0 0 0 12 23.5Z" />
    <path fill="#FBBC05" d="M5.6 14.2a6.9 6.9 0 0 1 0-4.4v-3H1.8a11.5 11.5 0 0 0 0 10.4l3.8-3Z" />
    <path fill="#EA4335" d="M12 5.1c1.7 0 3.2.6 4.4 1.7l3.3-3.3A11.5 11.5 0 0 0 1.8 6.8l3.8 3c.9-2.7 3.4-4.7 6.4-4.7Z" />
  </svg>;
}

function formatDate(value: string, timeZone?: string) {
  // en-IN like every other date in the product; the browser locale would
  // print this one page differently from the ledger it sits beside.
  return new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone }).format(new Date(value));
}

/** Why a linked method cannot be removed, or null when it can be.
 *
 *  Stated on the row rather than discovered by pressing a button that refuses:
 *  the last way into an account is the one thing no confirmation can undo. */
function removalBlock(profile: Profile): string | null {
  return profile.identities.length <= 1
    ? "This is the only way to sign in, so it can’t be removed. Link another method first."
    : null;
}

export function ProfilePanel() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [linking, setLinking] = useState<OtpChannel | null>(null);
  const [confirmingRemoval, setConfirmingRemoval] = useState<string | null>(null);
  const [confirmingDocumentRemoval, setConfirmingDocumentRemoval] = useState<string | null>(null);
  const [displayName, setDisplayName] = useState<string | null>(null);
  const [currency, setCurrency] = useState<string | null>(null);
  const [timezone, setTimezone] = useState<string | null>(null);

  const profile = useQuery({ queryKey: ["profile"], queryFn: getProfile, retry: false });
  const documents = useQuery({ queryKey: ["document-assets"], queryFn: loadDocumentAssets, retry: false });
  const currencyOptions = useMemo(() => {
    const current = profile.data?.currency;
    return !current || CURRENCY_OPTIONS.some((option) => option.value === current)
      ? CURRENCY_OPTIONS
      : [{ value: current, label: current }, ...CURRENCY_OPTIONS];
  }, [profile.data?.currency]);
  const timezoneOptions = useMemo(() => {
    const current = profile.data?.timezone;
    const values = !current || SUPPORTED_TIMEZONES.includes(current)
      ? SUPPORTED_TIMEZONES
      : [current, ...SUPPORTED_TIMEZONES];
    return values.map((value) => ({ value, label: timezoneLabel(value) }));
  }, [profile.data?.timezone]);

  const leave = useCallback(() => { queryClient.clear(); navigate(appPaths.login, { replace: true }); }, [navigate, queryClient]);
  const signedOut = isUnauthorized(profile.error);
  useEffect(() => {
    if (signedOut) leave();
  }, [signedOut, leave]);
  const leaveSession = useMutation({
    mutationFn: signOut,
    onSuccess: leave,
    onError: (cause: Error) => settingsProblem(cause.message),
  });

  const unlink = useMutation({
    mutationFn: removeIdentity,
    onSuccess: (updated) => {
      queryClient.setQueryData(["profile"], updated);
      settingsSaved("That sign-in method was removed.");
    },
    onError: (cause: Error) => settingsProblem(cause.message),
  });
  const saveProfile = useMutation({
    mutationFn: () => updateProfile({ displayName: displayName ?? profile.data?.displayName ?? "" }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["profile"], updated);
      setDisplayName(null);
      void queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
      settingsSaved("Your display name was updated.");
    },
    onError: (cause: Error) => settingsProblem(cause.message),
  });
  const saveDefaults = useMutation({
    mutationFn: () => updateProfile({
      displayName: profile.data?.displayName ?? "",
      currency: currency ?? profile.data?.currency ?? "",
      timezone: timezone ?? profile.data?.timezone ?? "",
    }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["profile"], updated);
      setCurrency(null);
      setTimezone(null);
      // Currency changes which records and totals belong in money views;
      // timezone changes their day/month boundaries. Mark every dependent
      // query stale, while leaving the document repository alone.
      void queryClient.invalidateQueries({
        predicate: (query) => !["profile", "document-assets"].includes(String(query.queryKey[0])),
      });
      settingsSaved("Your currency and timezone defaults were updated.");
    },
    onError: (cause: Error) => settingsProblem(cause.message),
  });
  const addDocument = useMutation({
    mutationFn: (file: File) => uploadDocumentAsset(file, "supporting_evidence"),
    onSuccess: (asset) => {
      queryClient.setQueryData(["document-assets"], (current: DocumentAssetOut[] | undefined) => current ? [asset, ...current] : [asset]);
      settingsSaved("The document was added to your private repository.");
    },
    onError: (cause: Error) => settingsProblem(cause.message),
  });
  const removeDocument = useMutation({
    mutationFn: deleteDocumentAsset,
    onSuccess: (_value, assetId) => {
      queryClient.setQueryData(["document-assets"], documents.data?.filter((asset) => asset.id !== assetId) ?? []);
      setConfirmingDocumentRemoval(null);
      settingsSaved("The private document was removed.");
    },
    onError: (cause: Error) => settingsProblem(cause.message),
  });

  if (profile.isError) {
    if (signedOut) return null;
    return <div className="grid place-items-center py-10">
      <div role="alert" className="max-w-sm rounded-xl border border-danger-line bg-surface p-6 text-center">
        <span className="mx-auto grid size-11 place-items-center rounded-lg bg-danger-tint text-danger"><TriangleAlert size={20} /></span>
        <h1 className="mt-4 font-heading text-title font-semibold text-ink">We couldn’t load your profile</h1>
        <p className="mt-2 text-control leading-6 text-ink-muted">{profile.error.message}</p>
        <Button type="button" onClick={() => profile.refetch()} size="lg" className="mt-4">Try again</Button>
      </div>
    </div>;
  }

  if (!profile.data) return <div className="grid place-items-center py-16"><Loader2 size={20} className="animate-spin text-ink-muted" aria-label="Loading your profile" /></div>;

  const account = profile.data;
  const editedDisplayName = displayName ?? account.displayName;
  const blocked = removalBlock(account);
  const has = (provider: IdentityOut["provider"]) => account.identities.some((item) => item.provider === provider);
  const missingChannels = (["phone", "email"] as const).filter((channel) => !has(channel));
  const editedCurrency = currency ?? account.currency;
  const editedTimezone = timezone ?? account.timezone;
  const defaultsChanged = editedCurrency !== account.currency || editedTimezone !== account.timezone;

  const linkExchange = (channel: OtpChannel, replacing: boolean) => <CodeExchange
    channel={channel}
    autoFocus
    submitLabel={replacing ? "Verify and save" : "Verify and add"}
    onCancel={() => setLinking(null)}
    onStart={(value) => startLinkCode(channel, value)}
    onVerify={async (challengeId, code) => {
      const updated = await verifyLinkCode(challengeId, code);
      queryClient.setQueryData(["profile"], updated);
      // The rail shows the account, so it has to hear about this too.
      void queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
      setLinking(null);
      settingsSaved(replacing ? `Your ${CHANNEL_COPY[channel].noun} was updated.` : `Your ${CHANNEL_COPY[channel].noun} is linked. You can sign in with it now.`);
    }}
  />;

  return <div>
    <header className="mb-7 flex items-center gap-3">
      <span className="ledger-stamp shrink-0">{account.displayName.slice(0, 1)}</span>
      <div className="min-w-0">
        <h2 className="truncate font-heading text-title font-semibold tracking-[-0.015em] text-ink">{account.displayName}</h2>
        <p className="ledger-meta mt-1 truncate">{account.currency} · {account.timezone}</p>
      </div>
    </header>

    <SettingsGroup
      title="Your details"
      description="Your name appears on shared agreements and acknowledgement evidence. Use the name people you know will recognize."
    >
      <form onSubmit={(event) => { event.preventDefault(); saveProfile.mutate(); }}>
        <label className="block text-control font-medium text-ink-body">Display name
          <input value={editedDisplayName} onChange={(event) => setDisplayName(event.target.value)} minLength={2} maxLength={120} required autoComplete="name" className="manual-field mt-2 h-11 w-full rounded-lg border border-line-strong bg-surface px-3 text-body text-ink outline-none placeholder:text-ink-muted" />
        </label>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={saveProfile.isPending || editedDisplayName.trim() === account.displayName || editedDisplayName.trim().toLowerCase() === "you"}>{saveProfile.isPending ? <Loader2 className="animate-spin" /> : <Check />}Save name</Button>
          <span className="text-note text-ink-muted">{account.currency} · {account.timezone}</span>
        </div>
      </form>
    </SettingsGroup>

    <SettingsGroup
      title="Regional defaults"
      description="These defaults shape new records, calculations, calendar boundaries, and how dates and money are shown across Fyn."
    >
      <form onSubmit={(event) => { event.preventDefault(); saveDefaults.mutate(); }}>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <p className="flex items-center gap-2 text-control font-medium text-ink-body"><Coins size={16} className="text-secondary" />Default currency</p>
            <Combobox
              aria-label="Default currency"
              value={editedCurrency}
              onValueChange={setCurrency}
              options={currencyOptions}
              searchPlaceholder="Search currencies"
              triggerClassName="mt-2"
            />
            <p className="mt-2 text-note leading-5 text-ink-muted">Used when you record or calculate an amount without naming a currency.</p>
          </div>
          <div>
            <p className="flex items-center gap-2 text-control font-medium text-ink-body"><Globe2 size={16} className="text-secondary" />Timezone</p>
            <Combobox
              aria-label="Timezone"
              value={editedTimezone}
              onValueChange={setTimezone}
              options={timezoneOptions}
              searchable
              searchPlaceholder="Search timezones"
              triggerClassName="mt-2"
            />
            <p className="mt-2 text-note leading-5 text-ink-muted">Used for transaction dates, month boundaries, reminders, and activity times.</p>
          </div>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={saveDefaults.isPending || !defaultsChanged}>
            {saveDefaults.isPending ? <Loader2 className="animate-spin" /> : <Check />}
            {saveDefaults.isPending ? "Updating…" : "Update defaults"}
          </Button>
          <span className="text-note text-ink-muted">Existing money records keep their original currency; values are never converted automatically.</span>
        </div>
      </form>
    </SettingsGroup>

    <SettingsGroup
      title="Private document repository"
      description="Keep important PDFs and images ready to reuse. Nothing here is shared until you explicitly add it to an agreement."
    >
      <div className="mb-4 flex items-start gap-3 rounded-xl border border-secondary-line bg-secondary-tint p-4">
        <ShieldCheck className="mt-0.5 shrink-0 text-secondary" size={18} />
        <p className="text-note leading-5 text-ink-body">When you share a document, Fyn creates an immutable copy for that agreement. Your private original stays here and can be reused.</p>
      </div>
      <label className="flex cursor-pointer items-center justify-center gap-2 rounded-xl border border-dashed border-line-strong bg-surface-sunken px-4 py-5 text-control font-semibold text-ink transition-colors hover:border-secondary">
        {addDocument.isPending ? <Loader2 className="animate-spin text-secondary" /> : <UploadCloud className="text-secondary" />}
        {addDocument.isPending ? "Uploading securely…" : "Add PDF, JPG, or PNG"}
        <input
          type="file"
          accept="application/pdf,image/png,image/jpeg"
          disabled={addDocument.isPending}
          className="sr-only"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) addDocument.mutate(file);
            event.target.value = "";
          }}
        />
      </label>
      {documents.isPending ? <p className="mt-4 flex items-center gap-2 text-note text-ink-muted"><Loader2 size={15} className="animate-spin" />Loading private documents…</p>
        : documents.isError ? <p role="alert" className="mt-4 text-note text-danger-ink">Your private documents couldn’t be loaded.</p>
        : documents.data?.length ? <ul className="mt-4 divide-y divide-line overflow-hidden rounded-xl border border-line bg-surface">
          {documents.data.map((asset) => <li key={asset.id} className="flex items-center gap-3 px-3 py-3">
            <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-secondary-tint text-secondary"><FileText size={17} /></span>
            <div className="min-w-0 flex-1"><p className="truncate text-control font-semibold text-ink">{asset.originalFilename}</p><p className="mt-0.5 text-meta text-ink-muted">{asset.classification.replaceAll("_", " ")} · {(asset.byteSize / 1024).toFixed(0)} KB</p></div>
            <a href={documentAssetDownloadUrl(asset.id)} className="grid size-10 place-items-center rounded-xl text-ink-muted hover:bg-surface-sunken hover:text-ink" aria-label={`Download ${asset.originalFilename}`}><Download size={17} /></a>
            {confirmingDocumentRemoval === asset.id ? <div className="flex items-center gap-1"><Button type="button" variant="destructive" size="sm" disabled={removeDocument.isPending} onClick={() => removeDocument.mutate(asset.id)}>{removeDocument.isPending ? <Loader2 className="animate-spin" /> : <Trash2 />}Remove</Button><Button type="button" variant="ghost" size="sm" onClick={() => setConfirmingDocumentRemoval(null)}>Keep</Button></div>
              : <Button type="button" variant="ghost" size="icon-lg" aria-label={`Remove ${asset.originalFilename}`} onClick={() => setConfirmingDocumentRemoval(asset.id)}><Trash2 /></Button>}
          </li>)}
        </ul> : <p className="mt-4 text-note text-ink-muted">No private documents yet.</p>}
    </SettingsGroup>

    <SettingsGroup
      title="How you sign in"
      description="Use any verified method below to sign in. You can keep one phone number and one email address on this account."
    >
      <ul className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-surface">
        {account.identities.map((identity) => {
          const channel = identity.provider === "phone" || identity.provider === "email" ? identity.provider : null;
          const managedByGoogle = channel === "email" && identity.source === "google";
          const editable = channel !== null && !managedByGoogle;
          const editing = editable && linking === channel;
          const editorId = `edit-identity-${identity.id}`;

          return <li key={identity.id}>
            <div className="flex items-center gap-3 px-4 py-4">
              <span aria-hidden className="grid size-9 shrink-0 place-items-center rounded-xl bg-secondary-tint text-secondary">{PROVIDER_COPY[identity.provider].icon}</span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-control font-medium text-ink-body">{identity.value}</p>
                <p className="mt-0.5 flex items-center gap-1 text-note text-ink-muted">
                  <Check size={14} className="shrink-0 text-secondary" />
                  {managedByGoogle
                    ? `${PROVIDER_COPY[identity.provider].label} · managed by Google`
                    : `${PROVIDER_COPY[identity.provider].label} · verified ${formatDate(identity.verifiedAt, profile.data.timezone)}`}
                </p>
              </div>
              {confirmingRemoval === identity.id
                // Removing a way in can't be undone from here, so it gets a real
                // question in place — not a modal, and never a bare trash click.
                ? <div className="flex shrink-0 items-center gap-1">
                  <Button type="button" variant="destructive" size="sm" disabled={unlink.isPending} onClick={() => { setConfirmingRemoval(null); unlink.mutate(identity.id); }}>
                    {unlink.isPending && unlink.variables === identity.id ? <Loader2 className="animate-spin" /> : <Trash2 />} Remove
                  </Button>
                  <Button type="button" variant="ghost" size="sm" disabled={unlink.isPending} onClick={() => setConfirmingRemoval(null)}>Keep</Button>
                </div>
                : <div className="flex shrink-0 items-center gap-1">
                  {editable ? <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label={`Edit ${PROVIDER_COPY[identity.provider].label.toLowerCase()} ${identity.value}`}
                    aria-expanded={editing}
                    aria-controls={editorId}
                    onClick={() => { setConfirmingRemoval(null); setLinking(editing ? null : channel); }}
                    className="text-ink-body"
                  >
                    <Pencil /> Edit
                  </Button> : null}
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-lg"
                    aria-label={`Remove ${PROVIDER_COPY[identity.provider].label.toLowerCase()} ${identity.value}`}
                    title={blocked ?? "Remove this sign-in method"}
                    disabled={Boolean(blocked) || unlink.isPending}
                    onClick={() => { setLinking(null); setConfirmingRemoval(identity.id); }}
                    className="shrink-0 rounded-xl text-ink-muted hover:text-danger"
                  >
                    {unlink.isPending && unlink.variables === identity.id ? <Loader2 className="animate-spin" /> : <Trash2 />}
                  </Button>
                </div>}
            </div>

            {editing ? <div id={editorId} className="border-t border-line bg-surface-sunken px-4 py-4 sm:pl-16">
              <p className="text-control font-semibold text-ink-body">Edit your {CHANNEL_COPY[channel].noun}</p>
              <p className="mt-1 mb-4 text-note leading-5 text-ink-muted">Your current {CHANNEL_COPY[channel].noun} will keep working until the new one is verified.</p>
              {linkExchange(channel, true)}
            </div> : null}
          </li>;
        })}
        </ul>

      {blocked ? <p className="mt-2 text-note leading-5 text-ink-muted">{blocked}</p> : null}

      {missingChannels.length > 0 ? <div className="mt-4 space-y-3">
        {missingChannels.map((channel) => {
            const article = channel === "email" ? "an" : "a";
            if (linking === channel) {
              return <div key={channel} className="rounded-lg border border-secondary-line bg-secondary-tint/30 p-4">
                <p className="text-control font-semibold text-ink-body">Add {article} {CHANNEL_COPY[channel].noun}</p>
                <p className="mt-1 mb-4 text-note leading-5 text-ink-muted">Once verified, you can use it to sign in to this account.</p>
                {linkExchange(channel, false)}
              </div>;
            }
            return <Button
              key={channel}
              type="button"
              variant="outline"
              onClick={() => setLinking(channel)}
              className="h-11 w-full justify-start rounded-xl px-4"
            >
              <Plus />Add {article} {CHANNEL_COPY[channel].noun}
            </Button>;
        })}
      </div> : null}

      <p className="mt-4 text-note leading-5 text-ink-muted">
        A phone number or email address can belong to only one account. If one is already in
        use, sign in to that account and remove it there first.
      </p>
    </SettingsGroup>

    <SettingsGroup title="This device" description="Signing out leaves your records exactly where they are.">
      <Button
        type="button"
        variant="outline"
        size="lg"
        disabled={leaveSession.isPending}
        onClick={() => leaveSession.mutate()}
        className="w-full sm:w-auto"
      >
        {leaveSession.isPending ? <Loader2 className="animate-spin" /> : <LogOut />}
        Sign out
      </Button>
    </SettingsGroup>
  </div>;
}
