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

import { reportAgentClientTelemetry, sendAgentMessage, waitForAgentRelatedQuestions } from "@/lib/api";

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
  it("starts an ordinary run without waiting for capability discovery", async () => {
    let releaseCapabilities: ((value: unknown) => void) | undefined;
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { releaseCapabilities = resolve; }));
    runAgentMock.mockImplementation(async (_input, subscriber) => {
      subscriber.onCustomEvent({
        event: {
          name: "fyn.response.v1",
          value: {
            response: {
              message: "Hello!",
              widgets: [],
              widgetUpdates: [],
              pendingAction: null,
              citations: [],
              conversation_id: "40d31ac3-8960-4789-ae70-f437e4f88155",
              message_id: "51fcbf58-daa9-4bf8-bc19-eb72830aeb27",
              user_message_id: null,
              delivered_at: "2026-08-26T00:00:00Z",
            },
          },
        },
      });
      subscriber.onRunFinishedEvent({ outcome: "success", event: {} });
    });

    const run = sendAgentMessage("40d31ac3-8960-4789-ae70-f437e4f88155", "Hi");
    await vi.waitFor(() => expect(runAgentMock).toHaveBeenCalledOnce());
    await expect(run).resolves.toMatchObject({ response: { message: "Hello!" } });

    releaseCapabilities?.({
      ok: true,
      json: async () => ({
        transport: { streaming: true, resumable: true },
        humanInTheLoop: { supported: true, interrupts: true },
      }),
    });
  });

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

  it("shows the first text immediately and coalesces later chunks per frame", async () => {
    const onText = vi.fn();
    const conversationId = "40d31ac3-8960-4789-ae70-f437e4f88155";
    const messageId = "51fcbf58-daa9-4bf8-bc19-eb72830aeb27";
    runAgentMock.mockImplementation(async (_input, subscriber) => {
      subscriber.onTextMessageContentEvent({
        event: { messageId, delta: "A" },
        textMessageBuffer: "",
      });
      subscriber.onTextMessageContentEvent({
        event: { messageId, delta: "B" },
        textMessageBuffer: "A",
      });
      subscriber.onTextMessageContentEvent({
        event: { messageId, delta: "C" },
        textMessageBuffer: "AB",
      });
      subscriber.onCustomEvent({
        event: {
          name: "fyn.response.v1",
          value: {
            response: {
              message: "ABC",
              widgets: [],
              widgetUpdates: [],
              pendingAction: null,
              citations: [],
              conversation_id: conversationId,
              message_id: messageId,
              user_message_id: null,
              delivered_at: "2026-08-27T00:00:00Z",
            },
          },
        },
      });
      subscriber.onRunFinishedEvent({ outcome: "success", event: {} });
    });

    await sendAgentMessage(conversationId, "Hi", { onText });

    expect(onText.mock.calls).toEqual([["A"], ["ABC"]]);
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

  it("reads completed related questions through the independent endpoint", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        runId: "3d6c6d32-c4f8-4bca-a2ff-9d25eaf0165b",
        messageId: "6a1e130a-ec37-42ba-a97a-e4d77bcbb382",
        kind: "related_questions",
        status: "completed",
        widget: {
          id: "related-6a1e130a-ec37-42ba-a97a-e4d77bcbb382",
          type: "related_questions",
          version: 1,
          data: { questions: ["What changed this month?"] },
          actions: [],
        },
      }),
    });

    const result = await waitForAgentRelatedQuestions("3d6c6d32-c4f8-4bca-a2ff-9d25eaf0165b");

    expect(result?.messageId).toBe("6a1e130a-ec37-42ba-a97a-e4d77bcbb382");
    expect(result?.widget.type).toBe("related_questions");
    expect(fetchMock.mock.calls[0][0]).toContain("/agent/runs/3d6c6d32-c4f8-4bca-a2ff-9d25eaf0165b/related-questions");
  });

  it("treats unscheduled enrichment as optional", async () => {
    fetchMock.mockResolvedValueOnce({ ok: false, status: 404 });

    await expect(waitForAgentRelatedQuestions("missing-run")).resolves.toBeNull();
  });
});
