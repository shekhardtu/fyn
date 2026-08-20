/**
 * The one place that answers "how do I get this on my home screen?".
 *
 * Chromium can be asked directly, so it gets a button. iOS Safari cannot be
 * asked at all, so it gets the three taps that actually work — vague advice
 * to "use the browser menu" is what sends people hunting.
 */
import { CheckCircle2, Download, Share, SquarePlus } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useInstallOffer } from "@/lib/install-prompt";

export function InstallAppSetting() {
  const offer = useInstallOffer();
  const [busy, setBusy] = useState(false);

  if (offer.kind === "installed") {
    return <Row icon={<CheckCircle2 size={17} />} title="Installed">
      fyn is on your home screen. It opens in its own window, without browser chrome.
    </Row>;
  }

  if (offer.kind === "prompt") {
    return <Row icon={<Download size={17} />} title="Add fyn to your home screen" action={
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={busy}
        onClick={async () => { setBusy(true); try { await offer.install(); } finally { setBusy(false); } }}
      >Install</Button>
    }>
      Opens in its own window and launches straight to your ledger.
    </Row>;
  }

  if (offer.kind === "instructions") {
    return <Row icon={<SquarePlus size={17} />} title="Add fyn to your Home Screen">
      <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1">
        Tap <Share size={14} aria-label="the Share button" className="inline shrink-0 text-secondary" /> in Safari’s toolbar, choose
        <b className="font-medium text-ink-body">Add to Home Screen</b>, then <b className="font-medium text-ink-body">Add</b>.
      </span>
      <span className="mt-1 block">Safari only — Chrome and Firefox on iOS have no such option.</span>
    </Row>;
  }

  // Kept visible rather than hidden: someone reading a browser that cannot
  // install web apps should learn that, not find a gap where the option was.
  return <Row icon={<Download size={17} />} title="Add fyn to your home screen">
    This browser doesn’t install web apps. Safari on iOS, or Chrome or Edge on Android and desktop, all do.
  </Row>;
}

function Row({ icon, title, action, children }: {
  icon: React.ReactNode;
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return <div className="flex items-center gap-3 rounded-lg border border-line bg-surface p-4">
    <span className="shrink-0 self-start pt-0.5 text-secondary">{icon}</span>
    <div className="min-w-0 flex-1">
      <p className="text-control font-medium text-ink">{title}</p>
      <p className="mt-0.5 text-note leading-5 text-ink-muted">{children}</p>
    </div>
    {action ? <div className="shrink-0">{action}</div> : null}
  </div>;
}
