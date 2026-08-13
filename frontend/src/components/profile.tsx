"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, CheckCircle2, Loader2, LogOut, Mail, Plus, Smartphone, Trash2, TriangleAlert } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { CHANNEL_COPY, CodeExchange } from "@/components/sign-in";
import { Button } from "@/components/ui/button";
import { getProfile, isUnauthorized, removeIdentity, signOut, startLinkCode, verifyLinkCode, type OtpChannel, type Profile } from "@/lib/api";
import type { IdentityOut } from "@/lib/protocol";
import { cn } from "@/lib/utils";

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
  return new Date(value).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function Notice({ tone, children }: { tone: "error" | "success"; children: ReactNode }) {
  const error = tone === "error";
  return <p
    role={error ? "alert" : "status"}
    className={cn(
      "flex items-start gap-2 rounded-lg border px-4 py-3 text-note leading-5",
      error ? "border-danger-line bg-danger-tint text-danger-ink" : "border-secondary-line bg-secondary-tint text-secondary-hover",
    )}
  >
    {error ? <TriangleAlert className="mt-0.5 shrink-0" /> : <CheckCircle2 className="mt-0.5 shrink-0" />}
    <span className="min-w-0">{children}</span>
  </p>;
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
  const router = useRouter();
  const queryClient = useQueryClient();
  const [linking, setLinking] = useState<OtpChannel | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const profile = useQuery({ queryKey: ["profile"], queryFn: getProfile, retry: false });

  const leave = useCallback(() => { queryClient.clear(); router.replace("/login"); }, [queryClient, router]);
  const signedOut = isUnauthorized(profile.error);
  useEffect(() => {
    if (signedOut) leave();
  }, [signedOut, leave]);

  const leaveSession = useMutation({
    mutationFn: signOut,
    onSuccess: leave,
    onError: (cause: Error) => setProblem(cause.message),
  });

  const unlink = useMutation({
    mutationFn: removeIdentity,
    onMutate: () => { setProblem(null); setNotice(null); },
    onSuccess: (updated) => {
      queryClient.setQueryData(["profile"], updated);
      setNotice("That sign-in method was removed.");
    },
    onError: (cause: Error) => setProblem(cause.message),
  });

  if (profile.isError) {
    if (signedOut) return null;
    return <main className="grid min-h-dvh place-items-center bg-ground p-6">
      <div role="alert" className="max-w-sm rounded-xl border border-danger-line bg-surface p-6 text-center">
        <span className="mx-auto grid size-11 place-items-center rounded-lg bg-danger-tint text-danger"><TriangleAlert size={20} /></span>
        <h1 className="mt-4 font-heading text-title font-semibold text-ink">We couldn’t load your profile</h1>
        <p className="mt-2 text-control leading-6 text-ink-muted">{profile.error.message}</p>
        <Button type="button" onClick={() => profile.refetch()} size="lg" className="mt-4">Try again</Button>
      </div>
    </main>;
  }

  if (!profile.data) return <main className="grid min-h-dvh place-items-center bg-ground"><Loader2 size={20} className="animate-spin text-ink-muted" aria-label="Loading your profile" /></main>;

  const account = profile.data;
  const blocked = removalBlock(account);
  const has = (provider: IdentityOut["provider"]) => account.identities.some((item) => item.provider === provider);
  const googleOwnsEmail = account.identities.some((item) => item.provider === "email" && item.source === "google");

  return <main className="min-h-dvh bg-ground px-4 py-8">
    <div className="mx-auto w-full max-w-lg">
      <button type="button" onClick={() => router.push("/")} className="inline-flex items-center gap-2 text-control font-medium text-ink-muted hover:text-ink-body">
        <ArrowLeft size={14} /> Back to your workspace
      </button>

      <header className="mt-4 flex items-center gap-3">
        <span className="ledger-stamp shrink-0">{account.displayName.slice(0, 1)}</span>
        <div className="min-w-0">
          <h1 className="truncate font-heading text-title font-semibold tracking-[-0.015em] text-ink">{account.displayName}</h1>
          <p className="ledger-meta mt-1 truncate">{account.currency} · {account.timezone}</p>
        </div>
      </header>

      <div className="mt-6 space-y-3">
        {problem ? <Notice tone="error">{problem}</Notice> : null}
        {notice ? <Notice tone="success">{notice}</Notice> : null}
      </div>

      <section className="mt-6 rounded-xl border border-line bg-surface p-4 sm:p-6">
        <h2 className="font-heading text-base font-semibold text-ink">How you sign in</h2>
        <p className="mt-1 text-note leading-5 text-ink-muted">
          Link a phone number and an email address to the same account and either one will
          get you in. Each belongs to one account only.
        </p>

        <ul className="mt-4 divide-y divide-line rounded-lg border border-line">
          {account.identities.map((identity) => <li key={identity.id} className="flex items-center gap-3 px-4 py-4">
            <span aria-hidden className="grid size-9 shrink-0 place-items-center rounded-xl bg-secondary-tint text-secondary">{PROVIDER_COPY[identity.provider].icon}</span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-control font-medium text-ink-body">{identity.value}</p>
              <p className="mt-0.5 flex items-center gap-1 text-note text-ink-muted">
                <Check size={14} className="shrink-0 text-secondary" />
                {PROVIDER_COPY[identity.provider].label} · verified {formatDate(identity.verifiedAt)}
              </p>
            </div>
            <Button
              type="button"
              variant="ghost"
              size="icon-lg"
              aria-label={`Remove ${PROVIDER_COPY[identity.provider].label.toLowerCase()} ${identity.value}`}
              title={blocked ?? "Remove this sign-in method"}
              disabled={Boolean(blocked) || unlink.isPending}
              onClick={() => unlink.mutate(identity.id)}
              className="shrink-0 rounded-xl text-ink-muted hover:text-danger"
            >
              {unlink.isPending && unlink.variables === identity.id ? <Loader2 className="animate-spin" /> : <Trash2 />}
            </Button>
          </li>)}
        </ul>

        {blocked ? <p className="mt-2 text-note leading-5 text-ink-muted">{blocked}</p> : null}

        <div className="mt-4 space-y-3">
          {(["phone", "email"] as const).map((channel) => {
            const linked = has(channel);
            // A Google-issued address is managed at Google, so the only honest
            // thing to offer here is an explanation, not a disabled button.
            const managed = channel === "email" && googleOwnsEmail;
            if (linking === channel) {
              return <div key={channel} className="rounded-lg border border-secondary-line bg-secondary-tint/30 p-4">
                <p className="mb-3 text-control font-semibold text-ink-body">{linked ? `Change your ${CHANNEL_COPY[channel].noun}` : `Add a ${CHANNEL_COPY[channel].noun}`}</p>
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
                    setNotice(linked ? `Your ${CHANNEL_COPY[channel].noun} was updated.` : `Your ${CHANNEL_COPY[channel].noun} is linked. You can sign in with it now.`);
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
              onClick={() => { setLinking(channel); setNotice(null); setProblem(null); }}
              className="h-11 w-full justify-start rounded-xl px-4"
            >
              <Plus />{linked ? `Change your ${CHANNEL_COPY[channel].noun}` : `Add a ${CHANNEL_COPY[channel].noun}`}
            </Button>;
          })}
        </div>
      </section>

      <p className="mt-4 text-note leading-5 text-ink-muted">
        If a phone number or email address is already linked to another account, it can’t be
        added here. Sign in to that account and delete it first — deleting an account releases
        its phone number and email address.
      </p>

      <Button
        type="button"
        variant="outline"
        disabled={leaveSession.isPending}
        onClick={() => leaveSession.mutate()}
        className="mt-6 h-11 w-full rounded-xl"
      >
        {leaveSession.isPending ? <Loader2 className="animate-spin" /> : <LogOut />}
        Sign out
      </Button>
    </div>
  </main>;
}
