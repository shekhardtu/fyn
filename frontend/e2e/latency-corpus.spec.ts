import { expect, test } from "@playwright/test";

import { API_URL } from "./test-thread";

/**
 * An opt-in, real-browser latency corpus.
 *
 * The normal browser suite skips this file because these turns call the live
 * model and are evidence collection rather than functional regression tests.
 * Every prompt still travels through the production UI, AG-UI stream, browser
 * paint, detached client telemetry, and durable server metrics. The console
 * prints only scenario ids, elapsed time, cohort start, and conversation ids;
 * prompts and answers never enter the benchmark report.
 */
const CORPUS = [
  { id: "short_greeting", prompt: "Hi" },
  { id: "short_wellbeing", prompt: "How are you doing?" },
  { id: "ordinary_explanation", prompt: "Explain principal versus interest without using my records." },
  { id: "runtime_taxonomy", prompt: "What expense categories and subcategories do I have?" },
  { id: "runtime_recent_records", prompt: "Show my five most recent transactions with their dates and amounts." },
  { id: "calculator_emi", prompt: "What is the monthly EMI on a ₹12 lakh loan at 8% annual interest for 5 years?" },
  { id: "calculator_prepayment", prompt: "For a ₹12 lakh loan at 8% over 5 years, compare total interest with and without a ₹1 lakh prepayment after 12 months while keeping the EMI fixed." },
  { id: "analysis_month_total", prompt: "How much did I spend this month?" },
  { id: "analysis_comparison", prompt: "Compare my spending this month with the same elapsed days last month and show the three largest category drivers." },
  { id: "analysis_empty", prompt: "Show expenses at a merchant named No Such Benchmark Merchant during January 2020." },
  { id: "analysis_three_month", prompt: "Across the last three full months, compare monthly spending by category and identify the most volatile category." },
  { id: "analysis_optimization", prompt: "Using the last three full months, calculate a fixed monthly discretionary-spending cap that is 10% below my historical average and show which categories require the largest reductions." },
  { id: "analysis_multi_dimension", prompt: "How was my August 1–27, 2026 Food spending distributed across days, merchants, and subcategories?" },
  { id: "analysis_windowed_drivers", prompt: "For each day from August 18–27, 2026, show Food spending, its change from the previous recorded day, the three-day rolling average, and the leading merchant and subcategory." },
  { id: "analysis_net_refund_mix", prompt: "Across August 1–27, 2026, reconcile gross expenses, refunds, and net spending by category, then identify which merchants contributed most to the net total without double-counting transactions." },
] as const;

const enabled = process.env.RUN_AGENT_LATENCY_CORPUS === "1";
const repetitions = Math.max(1, Number.parseInt(process.env.AGENT_LATENCY_CORPUS_REPETITIONS || "1", 10) || 1);
const requestedScenarios = new Set(
  (process.env.AGENT_LATENCY_CORPUS_SCENARIOS || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean),
);
const selectedCorpus = requestedScenarios.size
  ? CORPUS.filter((scenario) => requestedScenarios.has(scenario.id))
  : CORPUS;

test.describe("agent latency release corpus", () => {
  test.skip(!enabled, "Set RUN_AGENT_LATENCY_CORPUS=1 to make live model calls.");
  test.setTimeout(Math.max(900_000, repetitions * selectedCorpus.length * 120_000));

  test("records isolated current-code browser cohorts", async ({ page, request }) => {
    expect(selectedCorpus.length, "AGENT_LATENCY_CORPUS_SCENARIOS did not match a corpus id").toBeGreaterThan(0);
    const cohortStartedAt = new Date().toISOString();
    const conversationIds: string[] = [];

    for (let repetition = 1; repetition <= repetitions; repetition += 1) {
      for (const scenario of selectedCorpus) {
        // Each corpus item is a standalone latency sample. Reusing one thread
        // here used to turn every later item into an unrelated follow-up,
        // growing prompt history and active analysis state across scenarios.
        // Contextual behavior is benchmarked separately with explicit flows.
        const created = await request.post(`${API_URL}/conversations`, { data: {} });
        expect(
          created.ok(),
          `Could not create corpus conversation ${repetition}/${scenario.id}: ${await created.text()}`,
        ).toBeTruthy();
        const payload = await created.json() as { id?: string };
        expect(payload.id).toBeTruthy();
        conversationIds.push(String(payload.id));

        await page.goto(`/c/${payload.id}`);
        const composer = page.getByLabel("Message fyn AI");
        await expect(composer).toBeEnabled({ timeout: 30_000 });
        const startedAt = Date.now();

        await composer.fill(scenario.prompt);
        await composer.press("Enter");
        const running = page.getByText("fyn AI is working");
        await expect(running.last()).toBeVisible({ timeout: 30_000 });
        await expect(running).toHaveCount(0, { timeout: 120_000 });
        await expect(composer).toBeEnabled({ timeout: 120_000 });

        process.stdout.write(`${JSON.stringify({
          type: "agent_latency_sample",
          scenario: scenario.id,
          repetition,
          elapsedMs: Date.now() - startedAt,
        })}\n`);
      }
    }

    process.stdout.write(`${JSON.stringify({
      type: "agent_latency_cohort",
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
