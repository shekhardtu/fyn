import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Download, FileText, Loader2, LogOut, Mail, Plus, ShieldCheck, Smartphone, Trash2, TriangleAlert, UploadCloud } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useNavigate } from "react-router";
import { appPaths } from "@/routing/paths";
import { CHANNEL_COPY, CodeExchange } from "@/components/sign-in";
import { SettingsGroup, settingsProblem, settingsSaved } from "@/components/settings-parts";
import { Button } from "@/components/ui/button";
import { deleteDocumentAsset, documentAssetDownloadUrl, getProfile, isUnauthorized, loadDocumentAssets, removeIdentity, signOut, startLinkCode, updateProfile, uploadDocumentAsset, verifyLinkCode, type OtpChannel, type Profile } from "@/lib/api";
import type { DocumentAssetOut, IdentityOut } from "@/lib/protocol";

const PROVIDER_COPY: Record<IdentityOut["provider"], { label: string; icon: ReactNode }> = {
  phone: { label: "Phone number", icon: <Smartphone /> },
  email: { label: "Email address", icon: <Mail /> },
  google: { label: "Google", icon: <GoogleMark /> },
};

function GoogleMark() {
  return <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden focusable="false">
    <path fill="#4285F4" d="M23 12.3c0-.8-.1-1.6-.2-2.3H12v4.5h6.2a5.3 5.3 0 0 1-2.3 3.5v2.9h3.7c2.2-2 3.4-5 3.4-8.6Z" />
    <path fill="#34A853" d="M12 23.5c3.1 0 5.7-1 7.6-2.8l-3.7-2.9c-1 .7-2.3 1.1-3.9 1.1-3 0-5.5-2-6.4-4.7H1.8v3A11.5 11.5 0 0 0 12 23.5Z" />
    <path fill="#FBBC05" d="M5.6 14.2a6.9 6.9 0 0 1 0-4.4v-3H1.8a11.5 11.5 0 0 0 0 10.4l3.8-3Z" />
    <path fill="#EA4335" d="M12 5.1c1.7 0 3.2.6 4.4 1.7l3.3-3.3A11.5 11.5 0 0 0 1.8 6.8l3.8 3c.9-2.7 3.4-4.7 6.4-4.7Z" />
  </svg>;
}

function formatDate(value: string) {
  // en-IN like every other date in the product; the browser locale would
  // print this one page differently from the ledger it sits beside.
  return new Date(value).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
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

  const profile = useQuery({ queryKey: ["profile"], queryFn: getProfile, retry: false });
  const documents = useQuery({ queryKey: ["document-assets"], queryFn: loadDocumentAssets, retry: false });

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
    mutationFn: () => updateProfile(displayName ?? profile.data?.displayName ?? ""),
    onSuccess: (updated) => {
      queryClient.setQueryData(["profile"], updated);
      setDisplayName(null);
      void queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
      settingsSaved("Your display name was updated.");
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
  const googleOwnsEmail = account.identities.some((item) => item.provider === "email" && item.source === "google");

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
      description="Link a phone number and an email address to the same account and either one will get you in. Each belongs to one account only."
    >
      <ul className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-surface">
          {account.identities.map((identity) => <li key={identity.id} className="flex items-center gap-3 px-4 py-4">
            <span aria-hidden className="grid size-9 shrink-0 place-items-center rounded-xl bg-secondary-tint text-secondary">{PROVIDER_COPY[identity.provider].icon}</span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-control font-medium text-ink-body">{identity.value}</p>
              <p className="mt-0.5 flex items-center gap-1 text-note text-ink-muted">
                <Check size={14} className="shrink-0 text-secondary" />
                {PROVIDER_COPY[identity.provider].label} · verified {formatDate(identity.verifiedAt)}
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
              : <Button
                type="button"
                variant="ghost"
                size="icon-lg"
                aria-label={`Remove ${PROVIDER_COPY[identity.provider].label.toLowerCase()} ${identity.value}`}
                title={blocked ?? "Remove this sign-in method"}
                disabled={Boolean(blocked) || unlink.isPending}
                onClick={() => setConfirmingRemoval(identity.id)}
                className="shrink-0 rounded-xl text-ink-muted hover:text-danger"
              >
                {unlink.isPending && unlink.variables === identity.id ? <Loader2 className="animate-spin" /> : <Trash2 />}
              </Button>}
          </li>)}
        </ul>

      {blocked ? <p className="mt-2 text-note leading-5 text-ink-muted">{blocked}</p> : null}

      <div className="mt-4 space-y-3">
        {(["phone", "email"] as const).map((channel) => {
            const linked = has(channel);
            // A Google-issued address is managed at Google, so the only honest
            // thing to offer here is an explanation, not a disabled button.
            const managed = channel === "email" && googleOwnsEmail;
            const article = channel === "email" ? "an" : "a";
            if (linking === channel) {
              return <div key={channel} className="rounded-lg border border-secondary-line bg-secondary-tint/30 p-4">
                <p className="mb-3 text-control font-semibold text-ink-body">{linked ? `Change your ${CHANNEL_COPY[channel].noun}` : `Add ${article} ${CHANNEL_COPY[channel].noun}`}</p>
                <CodeExchange
                  channel={channel}
                  autoFocus
                  submitLabel={linked ? "Verify and replace" : "Verify and link"}
                  onCancel={() => setLinking(null)}
                  onStart={(value) => startLinkCode(channel, value)}
                  onVerify={async (challengeId, code) => {
                    const updated = await verifyLinkCode(challengeId, code);
                    queryClient.setQueryData(["profile"], updated);
                    // The rail shows the account, so it has to hear about this too.
                    void queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
                    setLinking(null);
                    settingsSaved(linked ? `Your ${CHANNEL_COPY[channel].noun} was updated.` : `Your ${CHANNEL_COPY[channel].noun} is linked. You can sign in with it now.`);
                  }}
                />
              </div>;
            }
            if (managed) {
              return <p key={channel} className="rounded-lg border border-line bg-surface-sunken px-4 py-3 text-note leading-5 text-ink-muted">
                Your email address comes from your Google sign-in, so it’s managed in your
                Google account rather than here.
              </p>;
            }
            return <Button
              key={channel}
              type="button"
              variant="outline"
              onClick={() => setLinking(channel)}
              className="h-11 w-full justify-start rounded-xl px-4"
            >
              <Plus />{linked ? `Change your ${CHANNEL_COPY[channel].noun}` : `Add ${article} ${CHANNEL_COPY[channel].noun}`}
            </Button>;
        })}
      </div>

      <p className="mt-4 text-note leading-5 text-ink-muted">
        If a phone number or email address is already linked to another account, it can’t be
        added here. Sign in to that account and delete it first — deleting an account releases
        its phone number and email address.
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
