import { beforeEach, describe, expect, it, vi } from "vitest";

const runAgentMock = vi.fn();

vi.mock("@ag-ui/client", () => ({
  HttpAgent: class {
    messages: Array<{ id: string; role: string; content: string }> = [];
    isRunning = false;
    url = "";
    abortController = new AbortController();

    constructor(options: { url: string }) {
      this.url = options.url;
    }

    addMessage(message: { id: string; role: string; content: string }) {
      this.messages.push(message);
    }

    setMessages(messages: Array<{ id: string; role: string; content: string }>) {
      this.messages = messages;
    }

    runAgent(input: unknown, subscriber: unknown) {
      return runAgentMock(input, subscriber);
    }
  },
}));

import { reportAgentClientTelemetry, sendAgentMessage } from "@/lib/api";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  runAgentMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockResolvedValue({
    ok: true,
    json: async () => ({
      transport: { streaming: true, resumable: true },
      humanInTheLoop: { supported: true, interrupts: true },
    }),
  });
});

describe("durable agent response handling", () => {
  it("does not present provisional streamed text as success after RUN_ERROR", async () => {
    runAgentMock.mockImplementation(async (_input, subscriber) => {
      subscriber.onTextMessageContentEvent({
        event: { messageId: "provisional-message", delta: "The transfer has been cancelled." },
        textMessageBuffer: "",
      });
      subscriber.onRunErrorEvent({
        event: { message: "fyn AI could not complete this request.", code: "RuntimeError" },
      });
    });

    await expect(sendAgentMessage("thread-id", "Cancel the transfer")).rejects.toThrow(
      "fyn AI could not complete this request.",
    );
  });

  it("dispatches browser telemetry later and never joins the agent request", async () => {
    vi.useFakeTimers();
    Object.defineProperty(window, "requestIdleCallback", { value: undefined, configurable: true });

    reportAgentClientTelemetry("run-id", {
      schemaVersion: 1,
      submitToRunCreatedMs: 0.2,
      submitToFirstActivityReceivedMs: 10,
      submitToFirstReasoningReceivedMs: null,
      submitToFirstTextReceivedMs: 500,
      submitToFirstAnswerVisibleMs: 516,
      submitToResponseResolvedMs: 550,
      submitToComposerUnlockedMs: 566,
      pageVisibleAtSubmit: true,
      replayed: false,
    });

    expect(fetchMock).not.toHaveBeenCalled();
    await vi.runAllTimersAsync();
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toContain("/agent/runs/run-id/telemetry");
    vi.useRealTimers();
  });
});
