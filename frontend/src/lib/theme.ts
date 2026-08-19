import { useSyncExternalStore } from "react";

export type ThemePreference = "system" | "light" | "dark";

/**
 * One switch for the whole surface.
 *
 * The CSS in globals.css already resolves "system" on its own via
 * `prefers-color-scheme`, so this store only has to do two things: stamp an
 * explicit choice as `data-theme` on the root element (which the CSS lets win
 * over the OS), and tell React subscribers — charts mirror tokens into
 * JS props and must re-render when the resolved theme flips.
 */
const STORAGE_KEY = "fyn.theme";
const listeners = new Set<() => void>();
// The guard also covers jsdom, which ships no matchMedia.
const darkQuery = typeof window === "undefined" || typeof window.matchMedia !== "function"
  ? null
  : window.matchMedia("(prefers-color-scheme: dark)");

function storedPreference(): ThemePreference {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

let preference: ThemePreference = "system";

function apply() {
  const root = document.documentElement;
  if (preference === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", preference);
}

function notify() {
  for (const listener of [...listeners]) listener();
}

/** Called once before first render so an explicit choice never flashes. */
export function initTheme() {
  if (typeof window === "undefined") return;
  preference = storedPreference();
  apply();
  darkQuery?.addEventListener("change", notify);
}

export function setThemePreference(next: ThemePreference) {
  preference = next;
  try {
    if (next === "system") localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // Preference simply won't survive a reload.
  }
  apply();
  notify();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function useThemePreference(): [ThemePreference, typeof setThemePreference] {
  return [useSyncExternalStore(subscribe, () => preference, () => "system" as const), setThemePreference];
}

export function resolvedTheme(): "light" | "dark" {
  if (preference !== "system") return preference;
  return darkQuery?.matches ? "dark" : "light";
}

export function useResolvedTheme(): "light" | "dark" {
  return useSyncExternalStore(subscribe, resolvedTheme, () => "light" as const);
}
