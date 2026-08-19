import { useEffect } from "react";

/**
 * Binds one plain-key shortcut for the current page.
 *
 * Plain means unmodified: a key pressed on its own, Gmail-style. It fires
 * only outside text entry — a slash typed into a field must stay a slash —
 * and never underneath an open dialog, where the keys belong to the overlay.
 * Pass a stable handler (useCallback) or the listener re-binds per render.
 */
export function usePlainKey(key: string, handler: () => void) {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== key || event.defaultPrevented) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable='true'], [contenteditable='']")) return;
      if (document.querySelector("[role='dialog'], [role='alertdialog']")) return;
      event.preventDefault();
      handler();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [key, handler]);
}
