/** Detached browser observations for one agent interaction.
 *
 * This adapter owns no UI state and cannot influence orchestration. All methods
 * are best-effort no-ops on unsupported browsers or unexpected failures.
 */

import { reportAgentClientTelemetry } from "@/lib/api";
import type { AgentClientTelemetryIn } from "@/lib/generated/contracts";

type TimingKey = Exclude<keyof AgentClientTelemetryIn, "schemaVersion" | "pageVisibleAtSubmit" | "replayed">;

function clock(): number {
  try {
    return typeof performance !== "undefined" ? performance.now() : Date.now();
  } catch {
    return Date.now();
  }
}

export class AgentRunTelemetry {
  private readonly startedAt = clock();
  private readonly timings: Partial<Record<TimingKey, number>> = {};
  private readonly pageVisibleAtSubmit = typeof document === "undefined" ? undefined : document.visibilityState === "visible";
  private runId: string | null = null;
  private sent = false;

  constructor(private readonly replayed = false) {}

  private mark(key: TimingKey): void {
    try {
      if (this.timings[key] === undefined) {
        this.timings[key] = Math.max(0, Math.round((clock() - this.startedAt) * 10) / 10);
      }
    } catch {
      // A missing observation is preferable to affecting the interaction.
    }
  }

  bindRun(runId: string): void {
    try {
      this.runId = runId;
      this.mark("submitToRunCreatedMs");
    } catch {
      // Best-effort only.
    }
  }

  activityReceived(): void { this.mark("submitToFirstActivityReceivedMs"); }
  reasoningReceived(): void { this.mark("submitToFirstReasoningReceivedMs"); }
  textReceived(): void { this.mark("submitToFirstTextReceivedMs"); }
  answerVisible(): void { this.mark("submitToFirstAnswerVisibleMs"); }
  responseResolved(): void { this.mark("submitToResponseResolvedMs"); }
  composerUnlocked(): void { this.mark("submitToComposerUnlockedMs"); }

  report(): void {
    try {
      if (this.sent || !this.runId) return;
      this.sent = true;
      reportAgentClientTelemetry(this.runId, {
        schemaVersion: 1,
        submitToRunCreatedMs: this.timings.submitToRunCreatedMs ?? null,
        submitToFirstActivityReceivedMs: this.timings.submitToFirstActivityReceivedMs ?? null,
        submitToFirstReasoningReceivedMs: this.timings.submitToFirstReasoningReceivedMs ?? null,
        submitToFirstTextReceivedMs: this.timings.submitToFirstTextReceivedMs ?? null,
        submitToFirstAnswerVisibleMs: this.timings.submitToFirstAnswerVisibleMs ?? null,
        submitToResponseResolvedMs: this.timings.submitToResponseResolvedMs ?? null,
        submitToComposerUnlockedMs: this.timings.submitToComposerUnlockedMs ?? null,
        pageVisibleAtSubmit: this.pageVisibleAtSubmit ?? null,
        replayed: this.replayed,
      });
    } catch {
      // Telemetry must never escape into React or the agent request lifecycle.
    }
  }
}
