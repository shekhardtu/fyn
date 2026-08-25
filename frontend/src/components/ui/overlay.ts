import { useEffect, useRef } from "react";

/** Keeps Tab inside an open overlay, closes it on Escape, and hands focus back
 *  to whatever opened it. Lives here so widget-level dialogs can use it
 *  without importing the workspace shell. Pass a stable `onClose` — the
 *  effect re-arms (and re-focuses the first control) when it changes. */
export function useWorkspaceOverlay<T extends HTMLElement = HTMLElement>(open: boolean, onClose: () => void) {
  const ref = useRef<T>(null);
  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement as HTMLElement | null;
    const node = ref.current;
    const focusable = () => Array.from(node?.querySelectorAll<HTMLElement>('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])') ?? []).filter((element) => element.offsetParent !== null);
    const initial = node?.querySelector<HTMLElement>("[data-overlay-initial-focus]");
    (initial ?? focusable()[0])?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === first || !node?.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      // Whatever opened this may have gone inert behind it — on mobile the rail
      // closes as the drawer opens — so fall back to a control still on screen.
      const reachable = opener?.isConnected && !opener.closest("[inert]") ? opener : document.querySelector<HTMLElement>("header button");
      reachable?.focus?.();
    };
  }, [open, onClose]);
  return ref;
}
