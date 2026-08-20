/**
 * Whether this browser can add fyn to the home screen, and how.
 *
 * There are two entirely different mechanisms and no common API over them.
 * Chromium fires `beforeinstallprompt`, which can be saved and replayed from a
 * button of our own. Safari fires nothing at all: on iOS the only route is the
 * Share sheet, so the honest affordance there is instructions, not a button
 * that cannot work.
 *
 * Both are hidden once the app is already installed, which the two platforms
 * also report differently — `display-mode: standalone` for the standard, and
 * `navigator.standalone` for Safari's own predating version of it.
 */
import { useCallback, useEffect, useState } from "react";

/** Chromium's saved install event. Not in lib.dom, so it is named here. */
type InstallPromptEvent = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
};

export type InstallOffer =
  /** Already on the home screen — there is nothing to offer. */
  | { kind: "installed" }
  /** Chromium: a real prompt is held and can be replayed on demand. */
  | { kind: "prompt"; install: () => Promise<"accepted" | "dismissed"> }
  /** iOS Safari: the Share sheet is the only route, so describe it. */
  | { kind: "instructions" }
  /** A browser that does not install web apps. Offer nothing rather than lie. */
  | { kind: "unavailable" };

export function isStandalone(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia?.("(display-mode: standalone)").matches
    || (window.navigator as Navigator & { standalone?: boolean }).standalone === true;
}

/** iOS Safari, including iPadOS, which reports itself as a Mac with touch. */
export function isIosSafari(): boolean {
  if (typeof navigator === "undefined") return false;
  const agent = navigator.userAgent;
  const ios = /iPad|iPhone|iPod/.test(agent)
    || (agent.includes("Macintosh") && navigator.maxTouchPoints > 1);
  // Every iOS browser is Safari underneath and shares its Share sheet, but
  // only the real one has "Add to Home Screen" — Chrome and Firefox on iOS
  // do not, so telling their users to look for it would send them hunting.
  return ios && !/CriOS|FxiOS|EdgiOS|OPiOS/.test(agent);
}

export function useInstallOffer(): InstallOffer {
  const [saved, setSaved] = useState<InstallPromptEvent | null>(null);
  const [installed, setInstalled] = useState(isStandalone);

  useEffect(() => {
    const hold = (event: Event) => {
      // Chromium shows its own mini-infobar unless this is prevented, and two
      // competing invitations read as a nag rather than an offer.
      event.preventDefault();
      setSaved(event as InstallPromptEvent);
    };
    const done = () => { setInstalled(true); setSaved(null); };
    window.addEventListener("beforeinstallprompt", hold);
    window.addEventListener("appinstalled", done);
    return () => {
      window.removeEventListener("beforeinstallprompt", hold);
      window.removeEventListener("appinstalled", done);
    };
  }, []);

  const install = useCallback(async () => {
    if (!saved) return "dismissed" as const;
    await saved.prompt();
    const { outcome } = await saved.userChoice;
    // The event is single-use: Chromium will fire a fresh one if the person
    // declines and remains eligible, so holding a spent one offers a button
    // that silently does nothing.
    setSaved(null);
    return outcome;
  }, [saved]);

  if (installed) return { kind: "installed" };
  if (saved) return { kind: "prompt", install };
  if (isIosSafari()) return { kind: "instructions" };
  return { kind: "unavailable" };
}
