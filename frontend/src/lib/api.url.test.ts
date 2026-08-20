/**
 * `apiUrl` is the only thing standing between a working deployment and every
 * request going to the wrong path, and the two configurations it serves are
 * exact opposites. Both are asserted here because only one of them is ever
 * exercised by a given build.
 */
import { describe, expect, it, vi } from "vitest";

async function apiUrlWith(apiUrlValue: string) {
  vi.resetModules();
  vi.doMock("@/config/environment", () => ({
    environment: { apiUrl: apiUrlValue, googleClientId: "", isDevelopment: false },
  }));
  return (await import("@/lib/api")).apiUrl;
}

describe("apiUrl", () => {
  it("keeps /api same-origin, where it separates API routes from SPA routes", async () => {
    const build = await apiUrlWith("");
    expect(build("/api/bootstrap")).toBe("/api/bootstrap");
    expect(build("/api/agent")).toBe("/api/agent");
  });

  it("drops /api against a dedicated API host, which already says it", async () => {
    const build = await apiUrlWith("https://api.fynai.co");
    expect(build("/api/bootstrap")).toBe("https://api.fynai.co/bootstrap");
    expect(build("/api")).toBe("https://api.fynai.co");
  });

  it("strips only the leading segment, never one inside the path", async () => {
    const build = await apiUrlWith("https://api.fynai.co");
    expect(build("/api/imports/api/csv")).toBe("https://api.fynai.co/imports/api/csv");
  });

  it("does not mistake a path that merely starts with those letters", async () => {
    const build = await apiUrlWith("https://api.fynai.co");
    expect(build("/apixyz/thing")).toBe("https://api.fynai.co/apixyz/thing");
  });

  it("carries the query string through untouched", async () => {
    const build = await apiUrlWith("https://api.fynai.co");
    expect(build("/api/agent/runs/1/events?after=9")).toBe("https://api.fynai.co/agent/runs/1/events?after=9");
  });
});
