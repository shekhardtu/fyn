import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { cookiesMock } = vi.hoisted(() => ({ cookiesMock: vi.fn() }));

vi.mock("next/headers", () => ({ cookies: cookiesMock }));

import { generateMetadata } from "@/app/c/[conversationId]/page";

describe("conversation metadata", () => {
  beforeEach(() => {
    cookiesMock.mockResolvedValue({ toString: () => "fyn_session=session-token" });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    cookiesMock.mockReset();
  });

  it("loads the selected conversation title for client-side navigation", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ title: "August spending review" }),
    } as Response);

    await expect(generateMetadata({ params: Promise.resolve({ conversationId: "thread / 2" }) })).resolves.toEqual({
      title: "August spending review",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/conversations/thread%20%2F%202",
      expect.objectContaining({ cache: "no-store", headers: { Cookie: "fyn_session=session-token" } }),
    );
  });

  it("falls back to the parent app title when the conversation is unavailable", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: false } as Response);

    await expect(generateMetadata({ params: Promise.resolve({ conversationId: "missing" }) })).resolves.toEqual({});
  });
});
