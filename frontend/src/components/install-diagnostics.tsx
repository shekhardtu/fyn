import { useEffect, useState } from "react";
import { SettingsGroup } from "@/components/settings-parts";
import { Button } from "@/components/ui/button";
import { subscribeToViewport } from "@/lib/viewport";

/** What this install is, and what its browser says about the screen.
 *
 *  A home-screen app is not the browser it was installed from. iOS hands every
 *  one of them — Safari's and Chrome's alike — to its own standalone WebKit,
 *  which measures a keyboard differently and can launch from the service
 *  worker's cached shell rather than the network. So "it works in the browser"
 *  and "it works installed" are two separate facts, and the first is no
 *  evidence for the second. Without an address bar there is nowhere to read a
 *  version and no console to ask, which is what this group is for: the two
 *  questions a support conversation otherwise cannot answer, plus the
 *  measurements behind the one bug this shape of app keeps producing.
 */

/** Written into the HTML at build time by the `fyn-build-version` plugin. */
function buildVersion() {
  const meta = document.querySelector('meta[name="application-version"]');
  const value = meta?.getAttribute("content")?.trim();
  // The placeholder survives only if the page was served by something that
  // never ran the build, which is itself worth seeing.
  return !value || value.startsWith("__") ? "unknown" : value;
}

function displayMode() {
  if (typeof window.matchMedia !== "function") return "browser";
  for (const mode of ["standalone", "fullscreen", "minimal-ui"]) {
    if (window.matchMedia(`(display-mode: ${mode})`).matches) return mode;
  }
  return (navigator as { standalone?: boolean }).standalone ? "standalone" : "browser";
}

function px(value: number | undefined) {
  return value === undefined || Number.isNaN(value) ? "—" : `${Math.round(value)}`;
}

/** The numbers that decide where the shell is put, in the order they are used:
 *  what the browser reports, what was published from it, and where the shell
 *  actually landed. A row that disagrees with its neighbours is the bug. */
function screenReadings() {
  const root = document.documentElement;
  const view = window.visualViewport;
  const style = getComputedStyle(root);
  const shell = document.querySelector(".app-shell");
  return [
    { label: "layout height", value: px(window.innerHeight) },
    { label: "visual height", value: px(view?.height) },
    { label: "visual offsetTop", value: px(view?.offsetTop) },
    { label: "visual pageTop", value: px(view?.pageTop) },
    { label: "window scrollY", value: px(window.scrollY) },
    { label: "document scrollHeight", value: px(root.scrollHeight) },
    { label: "published height", value: style.getPropertyValue("--app-height").trim() || "—" },
    { label: "published offset", value: style.getPropertyValue("--viewport-offset").trim() || "—" },
    { label: "published inset", value: style.getPropertyValue("--keyboard-inset").trim() || "—" },
    { label: "keyboard", value: root.dataset.keyboard ?? "closed" },
    { label: "shell top", value: px(shell?.getBoundingClientRect().top) },
    { label: "shell bottom", value: px(shell?.getBoundingClientRect().bottom) },
  ];
}

export function InstallDiagnostics() {
  const [watching, setWatching] = useState(false);
  const [readings, setReadings] = useState(screenReadings);

  useEffect(() => {
    if (!watching) return;
    const refresh = () => setReadings(screenReadings());
    // Published changes cover the keyboard; the poll covers the states WebKit
    // reaches without announcing them, which are the interesting ones.
    const unsubscribe = subscribeToViewport(refresh);
    const timer = window.setInterval(refresh, 250);
    return () => { unsubscribe(); window.clearInterval(timer); };
  }, [watching]);

  return <SettingsGroup title="This install" description="Which build is running here, and how this device measures its own screen. Useful when the installed app and the browser disagree.">
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-note">
      <dt className="text-ink-muted">Build</dt>
      <dd className="money text-ink-body">{buildVersion()}</dd>
      <dt className="text-ink-muted">Running as</dt>
      <dd className="text-ink-body">{displayMode()}</dd>
      <dt className="text-ink-muted">Engine</dt>
      <dd className="text-ink-body break-all">{navigator.userAgent}</dd>
    </dl>

    {watching ? <div className="mt-4">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 rounded-lg border border-line bg-surface-sunken p-3 text-note">
        {readings.map((reading) => <div key={reading.label} className="contents">
          <dt className="text-ink-muted">{reading.label}</dt>
          <dd className="money text-ink-body">{reading.value}</dd>
        </div>)}
      </dl>
      <label className="mt-3 block">
        <span className="text-note text-ink-muted">Tap here to raise the keyboard, then close it — the numbers keep updating.</span>
        <input
          type="text"
          inputMode="text"
          aria-label="Keyboard test field"
          placeholder="Type anything"
          className="manual-field mt-1.5 h-[var(--h-field)] w-full rounded-lg border border-line bg-surface px-3 text-body text-ink outline-none transition-colors duration-[110ms] ease-linear"
        />
      </label>
    </div> : <Button type="button" variant="outline" size="lg" onClick={() => setWatching(true)} className="mt-4 w-full sm:w-auto">
      Show screen measurements
    </Button>}
  </SettingsGroup>;
}
