import { expect, test } from "@playwright/test";

import { API_URL } from "./test-thread";

/** Opt-in multi-turn latency checks, kept separate from standalone samples. */
const FLOWS = [{
  id: "analysis_refinement",
  setup: "Across the last three full months, compare monthly spending by category and identify the most volatile category.",
  followUp: "Now calculate a fixed monthly discretionary-spending cap that is 10% below that period's historical average and show which categories require the largest reductions.",
}] as const;

const enabled = process.env.RUN_AGENT_LATENCY_CONTEXT_CORPUS === "1";

test.describe("agent contextual latency release corpus", () => {
  test.skip(!enabled, "Set RUN_AGENT_LATENCY_CONTEXT_CORPUS=1 to make live model calls.");
  test.setTimeout(600_000);

  test("records explicit multi-turn browser cohorts", async ({ page, request }) => {
    const cohortStartedAt = new Date().toISOString();
    const conversationIds: string[] = [];

    for (const flow of FLOWS) {
      const created = await request.post(`${API_URL}/conversations`, { data: {} });
      expect(created.ok(), `Could not create context flow ${flow.id}: ${await created.text()}`).toBeTruthy();
      const payload = await created.json() as { id?: string };
      expect(payload.id).toBeTruthy();
      conversationIds.push(String(payload.id));

      await page.goto(`/c/${payload.id}`);
      const composer = page.getByLabel("Message fyn AI");
      await expect(composer).toBeEnabled({ timeout: 30_000 });

      for (const [turn, prompt] of [["setup", flow.setup], ["follow_up", flow.followUp]] as const) {
        const startedAt = Date.now();
        await composer.fill(prompt);
        await composer.press("Enter");
        const running = page.getByText("fyn AI is working");
        await expect(running.last()).toBeVisible({ timeout: 30_000 });
        await expect(running).toHaveCount(0, { timeout: 120_000 });
        await expect(composer).toBeEnabled({ timeout: 120_000 });

        process.stdout.write(`${JSON.stringify({
          type: "agent_latency_context_sample",
          flow: flow.id,
          turn,
          elapsedMs: Date.now() - startedAt,
        })}\n`);
      }
    }

    process.stdout.write(`${JSON.stringify({
      type: "agent_latency_context_cohort",
      cohortStartedAt,
      conversationIds,
      reportArguments: [
        "--since",
        cohortStartedAt,
        ...conversationIds.flatMap((id) => ["--conversation-id", id]),
      ],
    })}\n`);
  });
});
