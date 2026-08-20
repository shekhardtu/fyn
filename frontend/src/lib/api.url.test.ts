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
  it("adds the API mount same-origin, where it separates resources from SPA routes", async () => {
    const build = await apiUrlWith("");
    expect(build("/bootstrap")).toBe("/api/bootstrap");
    expect(build("/agent")).toBe("/api/agent");
  });

  it("keeps resources at root against a dedicated API host", async () => {
    const build = await apiUrlWith("https://api.fynai.co");
    expect(build("/bootstrap")).toBe("https://api.fynai.co/bootstrap");
    expect(build("/agent")).toBe("https://api.fynai.co/agent");
  });

  it("keeps a plausible page and resource namespace separate same-origin", async () => {
    const build = await apiUrlWith("");
    expect(build("/settings")).toBe("/api/settings");
  });

  it("adds the mount once even when a resource has api inside its path", async () => {
    const build = await apiUrlWith("");
    expect(build("/imports/api/csv")).toBe("/api/imports/api/csv");
  });

  it("carries the query string through untouched", async () => {
    const build = await apiUrlWith("https://api.fynai.co");
    expect(build("/agent/runs/1/events?after=9")).toBe("https://api.fynai.co/agent/runs/1/events?after=9");
  });
});
