import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getAgentSettings, setAnswerStyle, setAnswerValidationMode } from "@/lib/api";

const fetchMock = vi.fn();

function response(body: unknown) {
  return {
    ok: true,
    status: 200,
    headers: new Headers(),
    json: async () => body,
  };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => vi.unstubAllGlobals());

describe("agent settings API", () => {
  it("loads and saves user-level answer settings independently", async () => {
    fetchMock.mockResolvedValueOnce(response({ answerValidationMode: "full", answerStyle: "explained" }));
    await expect(getAgentSettings()).resolves.toEqual({ answerValidationMode: "full", answerStyle: "explained" });
    expect(fetchMock.mock.calls[0][0]).toContain("/api/agent-settings");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: "include" });

    fetchMock.mockResolvedValueOnce(response({ answerValidationMode: "off", answerStyle: "explained" }));
    await expect(setAnswerValidationMode("off")).resolves.toEqual({ answerValidationMode: "off", answerStyle: "explained" });
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      credentials: "include",
      method: "PATCH",
      body: JSON.stringify({ answerValidationMode: "off" }),
    });

    fetchMock.mockResolvedValueOnce(response({ answerValidationMode: "off", answerStyle: "concise" }));
    await expect(setAnswerStyle("concise")).resolves.toEqual({ answerValidationMode: "off", answerStyle: "concise" });
    expect(fetchMock.mock.calls[2][1]).toMatchObject({
      credentials: "include",
      method: "PATCH",
      body: JSON.stringify({ answerStyle: "concise" }),
    });
  });
});
