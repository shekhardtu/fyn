"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CheckCircle2, Loader2, Mail, Smartphone, TriangleAlert } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { ApiError, getAuthStatus, signInWithGoogle, startSignInCode, verifySignInCode, type OtpChannel, type OtpSent } from "@/lib/api";
import { cn } from "@/lib/utils";

const GOOGLE_CLIENT_ID = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID ?? "";
const GOOGLE_SCRIPT = "https://accounts.google.com/gsi/client";

/* ── Shared pieces ──────────────────────────────────────────────────────────
 * Signing in and linking a second method are the same two steps — name an
 * identifier, then prove you receive at it — so both pages drive the same
 * component and differ only in which pair of calls it is handed. */

export const CHANNEL_COPY: Record<OtpChannel, { label: string; noun: string; placeholder: string; hint: string; inputMode: "tel" | "email"; autoComplete: string; icon: ReactNode }> = {
  phone: {
    label: "Phone number",
    noun: "phone number",
    placeholder: "+91 98765 43210",
    hint: "A number without a country code is read as +91.",
    inputMode: "tel",
    autoComplete: "tel",
    icon: <Smartphone />,
  },
  email: {
    label: "Email address",
    noun: "email address",
    placeholder: "you@example.com",
    hint: "We’ll send a six-digit code to this address.",
    inputMode: "email",
    autoComplete: "email",
    icon: <Mail />,
  },
};

/** Seconds until another code may be asked for, and the way to start them.
 *
 *  The count always comes from the server's answer — the interval a send
 *  reports, or the `Retry-After` on a refusal — never from a guess here. It is
 *  a courtesy to the reader, not the limit itself: the limit is enforced
 *  server-side whatever this displays. */
function useCooldown(): [number, (seconds: number) => void] {
  const [remaining, setRemaining] = useState(0);
  useEffect(() => {
    if (remaining <= 0) return;
    const timer = window.setTimeout(() => setRemaining((value) => value - 1), 1_000);
    return () => window.clearTimeout(timer);
  }, [remaining]);
  return [remaining, setRemaining];
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

export type CodeExchangeProps = {
  channel: OtpChannel;
  /** Asks the server to send a code. Rejects with the reason it would not. */
  onStart: (value: string) => Promise<OtpSent>;
  /** Presents the code. Resolving means the identifier is now verified. */
  onVerify: (challengeId: string, code: string) => Promise<void>;
  submitLabel: string;
  onCancel?: () => void;
  autoFocus?: boolean;
};

/** Name an identifier, then enter the code sent to it.
 *
 *  The two steps are one component because the second cannot exist without the
 *  first: keeping the challenge in local state means a reload cannot strand a
 *  code-entry box with no challenge behind it. */
export function CodeExchange({ channel, onStart, onVerify, submitLabel, onCancel, autoFocus }: CodeExchangeProps) {
  const copy = CHANNEL_COPY[channel];
  const [value, setValue] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState<OtpSent | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  const [cooldown, startCooldown] = useCooldown();

  // Moving focus to the code box is the whole point of the step change; without
  // it a keyboard or screen-reader user is left on a field that just vanished.
  useEffect(() => { if (sent) codeRef.current?.focus(); }, [sent]);

  const fail = useCallback((cause: unknown) => {
    const error = cause instanceof Error ? cause : new Error("Something went wrong. Try again.");
    setProblem(error.message);
    if (error instanceof ApiError && error.retryAfterSeconds) startCooldown(error.retryAfterSeconds);
  }, [startCooldown]);

  const start = useMutation({
    mutationFn: (identifier: string) => onStart(identifier),
    onMutate: () => setProblem(null),
    onSuccess: (result) => { setSent(result); setCode(""); startCooldown(result.resendAfterSeconds); },
    onError: fail,
  });

  const verify = useMutation({
    mutationFn: ({ challengeId, submitted }: { challengeId: string; submitted: string }) => onVerify(challengeId, submitted),
    onMutate: () => setProblem(null),
    onError: fail,
  });

  const busy = start.isPending || verify.isPending;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!sent) { start.mutate(value.trim()); return; }
    verify.mutate({ challengeId: sent.challengeId, submitted: code.trim() });
  }

  return <form onSubmit={submit} className="space-y-4">
    {problem ? <Notice tone="error">{problem}</Notice> : null}

    {!sent ? <>
      <label className="block">
        <span className="text-control font-medium text-ink-body">{copy.label}</span>
        <div data-field className="mt-2 flex h-[var(--h-field)] items-center gap-2 rounded-lg border border-line-strong bg-surface px-3 transition-colors duration-[110ms] ease-linear focus-within:border-secondary">
          <span aria-hidden className="shrink-0 text-ink-muted">{copy.icon}</span>
          <input
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder={copy.placeholder}
            inputMode={copy.inputMode}
            autoComplete={copy.autoComplete}
            autoFocus={autoFocus}
            required
            className="h-full w-full bg-transparent text-body text-ink outline-none placeholder:text-ink-muted"
          />
        </div>
        <span className="mt-2 block text-note leading-5 text-ink-muted">{copy.hint}</span>
      </label>
      <div className="flex gap-2">
        <Button type="submit" disabled={busy || !value.trim() || cooldown > 0} size="lg" className="flex-1">
          {start.isPending ? <Loader2 className="animate-spin" /> : null}
          {cooldown > 0 ? `Wait ${cooldown}s` : start.isPending ? "Sending a code…" : "Send code"}
        </Button>
        {onCancel ? <Button type="button" size="lg" variant="ghost" onClick={onCancel}>Cancel</Button> : null}
      </div>
    </> : <>
      <Notice tone="success">
        Code sent to <strong className="font-semibold">{sent.destinationMasked}</strong>.
        {sent.debugCode ? <> Development mode, so here it is: <strong className="font-mono font-semibold">{sent.debugCode}</strong>.</> : null}
      </Notice>
      <label className="block">
        <span className="text-control font-medium text-ink-body">Six-digit code</span>
        <input
          ref={codeRef}
          value={code}
          onChange={(event) => setCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
          inputMode="numeric"
          autoComplete="one-time-code"
          placeholder="••••••"
          required
          className="mt-2 h-12 w-full rounded-lg border border-line-strong bg-surface px-3 text-center font-mono text-title text-ink tracking-[0.4em] outline-none transition-colors duration-[110ms] ease-linear focus:border-secondary"
        />
      </label>
      <Button type="submit" disabled={busy || code.length < 4} size="lg" className="w-full">
        {verify.isPending ? <Loader2 className="animate-spin" /> : null}
        {verify.isPending ? "Checking…" : submitLabel}
      </Button>
      <div className="flex items-center justify-between text-note">
        <button
          type="button"
          onClick={() => { setSent(null); setCode(""); setProblem(null); }}
          className="inline-flex items-center gap-1 font-medium text-ink-muted hover:text-ink-body"
        >
          <ArrowLeft size={14} /> Use a different {copy.noun}
        </button>
        <button
          type="button"
          disabled={busy || cooldown > 0}
          onClick={() => start.mutate(value.trim())}
          className="font-semibold text-secondary disabled:text-ink-muted"
        >
          {cooldown > 0 ? `Resend in ${cooldown}s` : "Resend code"}
        </button>
      </div>
    </>}
  </form>;
}

/* ── Google ─────────────────────────────────────────────────────────────── */

type GoogleIdentityApi = {
  accounts: {
    id: {
      initialize: (options: { client_id: string; callback: (response: { credential: string }) => void; ux_mode?: string }) => void;
      renderButton: (parent: HTMLElement, options: Record<string, unknown>) => void;
    };
  };
};

declare global {
  interface Window { google?: GoogleIdentityApi }
}

function loadGoogleScript(): Promise<void> {
  if (typeof document === "undefined") return Promise.resolve();
  const existing = document.querySelector<HTMLScriptElement>(`script[src="${GOOGLE_SCRIPT}"]`);
  if (existing) return existing.dataset.loaded ? Promise.resolve() : new Promise((resolve, reject) => {
    existing.addEventListener("load", () => resolve(), { once: true });
    existing.addEventListener("error", () => reject(new Error("blocked")), { once: true });
  });
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = GOOGLE_SCRIPT;
    script.async = true;
    script.addEventListener("load", () => { script.dataset.loaded = "true"; resolve(); }, { once: true });
    script.addEventListener("error", () => reject(new Error("blocked")), { once: true });
    document.head.append(script);
  });
}

/** Google's own button, because Google requires its rendering rather than a
 *  look-alike. The ID token it returns is passed straight to the server, which
 *  is the only place it is trusted. */
export function GoogleSignInButton({ onCredential, onProblem }: { onCredential: (credential: string) => void; onProblem: (message: string) => void }) {
  const holder = useRef<HTMLDivElement>(null);
  const [ready, setReady] = useState(false);
  const [blocked, setBlocked] = useState(false);
  // A missing client id is a fact about the build, not something to discover at
  // runtime and store in state.
  const configured = Boolean(GOOGLE_CLIENT_ID);

  useEffect(() => {
    if (!configured) return;
    let cancelled = false;
    loadGoogleScript()
      .then(() => {
        if (cancelled || !holder.current || !window.google) return;
        window.google.accounts.id.initialize({
          client_id: GOOGLE_CLIENT_ID,
          callback: (response) => onCredential(response.credential),
        });
        window.google.accounts.id.renderButton(holder.current, {
          type: "standard", theme: "outline", size: "large", text: "continue_with", shape: "pill", width: 320,
        });
        setReady(true);
      })
      .catch(() => {
        if (cancelled) return;
        setBlocked(true);
        onProblem("Google sign-in couldn’t load. Use your phone number or email instead.");
      });
    return () => { cancelled = true; };
  }, [configured, onCredential, onProblem]);

  if (!configured || blocked) return null;
  return <div className="flex min-h-[44px] justify-center">
    <div ref={holder} />
    {!ready ? <span className="text-note text-ink-muted">Loading Google sign-in…</span> : null}
  </div>;
}

/* ── The page ───────────────────────────────────────────────────────────── */

export function SignInPanel() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [channel, setChannel] = useState<OtpChannel>("phone");
  const [problem, setProblem] = useState<string | null>(null);

  // Someone who already has a session has no business on this page.
  const status = useQuery({ queryKey: ["auth"], queryFn: getAuthStatus, retry: false });
  const authenticated = status.data?.authenticated ?? false;

  const enter = useCallback(async () => {
    // Nothing cached under the previous visitor may survive into this session.
    queryClient.clear();
    router.replace("/");
  }, [queryClient, router]);

  useEffect(() => { if (authenticated) void enter(); }, [authenticated, enter]);

  const google = useMutation({
    mutationFn: signInWithGoogle,
    onMutate: () => setProblem(null),
    onSuccess: enter,
    onError: (cause: Error) => setProblem(cause.message),
  });
  // Stable across renders, so handing Google's button a credential handler does
  // not tear down and re-render the button on every keystroke elsewhere.
  const { mutate: exchangeGoogleCredential } = google;
  const onCredential = useCallback((credential: string) => exchangeGoogleCredential(credential), [exchangeGoogleCredential]);

  return <main className="grid min-h-dvh place-items-center bg-ground px-4 py-10">
    <div className="w-full max-w-sm">
      <div className="text-center">
        <h1 className="font-heading text-display font-semibold tracking-[-0.03em] text-ink">fyn AI</h1>
        <p className="mt-2 text-body leading-6 text-ink-muted">Sign in to your private workspace. No password to remember — we send a code.</p>
      </div>

      <div className="mt-6 rounded-xl border border-line bg-surface p-4 sm:p-6">
        {problem ? <div className="mb-4"><Notice tone="error">{problem}</Notice></div> : null}

        <div role="tablist" aria-label="How to sign in" className="mb-4 grid grid-cols-2 gap-1 rounded-lg bg-surface-sunken p-1">
          {(["phone", "email"] as const).map((option) => <button
            key={option}
            type="button"
            role="tab"
            aria-selected={channel === option}
            onClick={() => { setChannel(option); setProblem(null); }}
            className={cn(
              "hit-target flex h-8 items-center justify-center gap-2 rounded-md text-control font-medium transition-colors duration-[110ms] ease-linear active:scale-[.98]",
              channel === option ? "bg-surface text-ink" : "text-ink-muted hover:text-ink-body",
            )}
          >
            {CHANNEL_COPY[option].icon}{option === "phone" ? "Phone" : "Email"}
          </button>)}
        </div>

        <CodeExchange
          key={channel}
          channel={channel}
          autoFocus
          onStart={(value) => startSignInCode(channel, value)}
          onVerify={async (challengeId, code) => { await verifySignInCode(challengeId, code); await enter(); }}
          submitLabel="Sign in"
        />

        {status.data?.googleSignInAvailable !== false ? <>
          <div className="my-4 flex items-center gap-3 text-[11px] font-semibold tracking-[0.13em] text-ink-muted uppercase">
            <span className="h-px flex-1 bg-line" />or<span className="h-px flex-1 bg-line" />
          </div>
          {google.isPending
            ? <p role="status" className="flex items-center justify-center gap-2 text-note text-ink-muted"><Loader2 size={14} className="animate-spin" />Signing you in…</p>
            : <GoogleSignInButton onCredential={onCredential} onProblem={setProblem} />}
        </> : null}
      </div>

      <p className="mt-4 text-center text-note leading-5 text-ink-muted">
        A phone number or email address belongs to one account. You can link both
        to the same account later from your profile.
      </p>
    </div>
  </main>;
}
