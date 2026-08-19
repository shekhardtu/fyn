import { beforeEach, describe, expect, it, vi } from "vitest";
import { initTheme, resolvedTheme, setThemePreference } from "@/lib/theme";

/* Node 24 injects its own method-less `localStorage` global that shadows
 * jsdom's, so persistence is exercised against a real in-memory Storage. */
const backing = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (key: string) => backing.get(key) ?? null,
  setItem: (key: string, value: string) => void backing.set(key, value),
  removeItem: (key: string) => void backing.delete(key),
  clear: () => backing.clear(),
});

describe("theme preference", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
  });

  it("stamps an explicit choice on the root and persists it", () => {
    setThemePreference("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(localStorage.getItem("fyn.theme")).toBe("dark");
    expect(resolvedTheme()).toBe("dark");
  });

  it("removes the stamp for system so CSS media queries decide", () => {
    setThemePreference("dark");
    setThemePreference("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(localStorage.getItem("fyn.theme")).toBeNull();
  });

  it("restores a stored choice on boot", () => {
    localStorage.setItem("fyn.theme", "light");
    initTheme();
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("treats junk storage as system", () => {
    localStorage.setItem("fyn.theme", "sepia");
    initTheme();
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });
});
