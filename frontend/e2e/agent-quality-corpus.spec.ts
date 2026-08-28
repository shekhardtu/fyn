import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { API_URL } from "./test-thread";

type Check = { id: string; passed: boolean; detail: string };
type QualitySample = {
  scenario: string;
  category: string;
  conversationId: string;
  prompt: string;
  answer: string;
  rubric: string;
  referenceFacts: string[];
  elapsedMs: number;
  taskStatus: string;
  runId: string | null;
  hardChecks: Check[];
  hardPassed: boolean;
};

type Transaction = {
  id: string;
  transactionType: string;
  amountMinor: number;
  currency: string;
  merchant: string | null;
  transactionAt: string;
  category: string | null;
  subcategory: string | null;
  spendNature: string;
  deletedAt: string | null;
};

type Oracle = {
  categories: Array<{ label: string; subcategories: Array<{ label: string }> }>;
  transactions: Transaction[];
  currentMonth: string;
  previousMonth: string;
  currentNetMinor: number;
  previousNetMinor: number;
  threeMonthGrossMinor: number;
  threeMonthRefundMinor: number;
  threeMonthNetMinor: number;
  excludedMonthBeforeWindow: string;
  topCategories: string[];
  comparisonDrivers: Array<{
    category: string;
    currentMinor: number;
    previousMinor: number;
    differenceMinor: number;
  }>;
  recentTransactions: Transaction[];
  emiMinor: number;
};

type TurnOptions = {
  category: string;
  rubric: string;
  referenceFacts?: string[];
  expectedTerms?: string[];
  expectedMoney?: number[];
  forbiddenTerms?: string[];
  requiresSource?: boolean;
  requiresRelatedQuestions?: boolean;
};

const enabled = process.env.RUN_AGENT_QUALITY_CORPUS === "1";
const resultsPath = process.env.AGENT_QUALITY_RESULTS_PATH || "test-results/agent-quality-browser.json";
const requestedScenarios = new Set(
  (process.env.AGENT_QUALITY_SCENARIOS || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean),
);
const forbiddenAnswerPattern = /traceback|sqlalchemy|validation_error|internal diagnostic|tool arguments|system prompt/i;
const failureAnswerPattern = /couldn(?:'|’)t|could not complete|please restate|try again later/i;

function monthKey(value: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
  }).formatToParts(new Date(value));
  const year = parts.find((part) => part.type === "year")?.value;
  const month = parts.find((part) => part.type === "month")?.value;
  return `${year}-${month}`;
}

function shiftMonthKey(value: string, offset: number): string {
  const [year, month] = value.split("-").map(Number);
  const shifted = new Date(Date.UTC(year, month - 1 + offset, 15));
  return `${shifted.getUTCFullYear()}-${String(shifted.getUTCMonth() + 1).padStart(2, "0")}`;
}

function localDateKey(value: string): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(value));
}

function monthLabel(value: string): string {
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "UTC",
    year: "numeric",
    month: "long",
  }).format(new Date(`${value}-15T00:00:00Z`));
}

function shouldRunScenario(scenario: string): boolean {
  return requestedScenarios.size === 0 || requestedScenarios.has(scenario);
}

function normalizedText(value: string): string {
  return value.normalize("NFKC").replace(/[,_\s]/g, "").toLocaleLowerCase("en-IN");
}

function containsMoney(answer: string, amountMinor: number): boolean {
  const amount = amountMinor / 100;
  const exact = Number.isInteger(amount) ? amount.toFixed(0) : amount.toFixed(2);
  return normalizedText(answer).includes(normalizedText(exact));
}

function check(id: string, passed: boolean, detail: string): Check {
  return { id, passed, detail };
}

async function jsonResponse<T>(response: Awaited<ReturnType<APIRequestContext["get"]>>, label: string): Promise<T> {
  expect(response.ok(), `${label}: ${await response.text()}`).toBeTruthy();
  return await response.json() as T;
}

async function createConversation(request: APIRequestContext): Promise<string> {
  const response = await request.post(`${API_URL}/conversations`, { data: {} });
  const payload = await jsonResponse<{ id: string }>(response, "create eval conversation");
  expect(payload.id).toBeTruthy();
  return payload.id;
}

async function allTransactions(request: APIRequestContext): Promise<Transaction[]> {
  const transactions: Transaction[] = [];
  for (let offset = 0; ; offset += 100) {
    const response = await request.get(
      `${API_URL}/transactions?limit=100&offset=${offset}&include_removed=false`,
    );
    const page = await jsonResponse<Transaction[]>(response, "read eval transactions");
    transactions.push(...page);
    if (page.length < 100) return transactions;
  }
}

async function buildOracle(request: APIRequestContext): Promise<Oracle> {
  const bootstrap = await jsonResponse<{ user: { name: string; currency: string } }>(
    await request.get(`${API_URL}/bootstrap`),
    "read eval bootstrap",
  );
  expect(bootstrap.user.name).toBe("FYN Quality Eval");
  expect(bootstrap.user.currency).toBe("INR");

  const overview = await jsonResponse<{
    period: { start: string; end: string; previousStart: string; previousEnd: string };
    categories: Array<{ label: string }>;
  }>(await request.get(`${API_URL}/overview`), "read eval overview");
  const categories = await jsonResponse<Oracle["categories"]>(
    await request.get(`${API_URL}/categories`),
    "read eval taxonomy",
  );
  const transactions = await allTransactions(request);
  expect(
    transactions.length,
    "The dedicated FYN Quality Eval account must contain the complete synthetic ledger.",
  ).toBeGreaterThanOrEqual(200);
  const loan = await jsonResponse<{ baseline: { emi_minor: number } }>(
    await request.post(`${API_URL}/calculators/loan`, {
      data: {
        principal_minor: 120_000_000,
        annual_rate_percent: 8,
        tenure_months: 60,
        prepayment_minor: 0,
      },
    }),
    "calculate eval EMI oracle",
  );

  const currentMonth = overview.period.start.slice(0, 7);
  const previousMonth = overview.period.previousStart.slice(0, 7);
  const twoMonthsAgo = shiftMonthKey(previousMonth, -1);
  const selectedMonths = new Set<string>([
    currentMonth,
    previousMonth,
    twoMonthsAgo,
  ]);
  const netFor = (month: string) => transactions
    .filter((item) => monthKey(item.transactionAt) === month)
    .reduce((total, item) => total + (
      item.transactionType === "expense" ? item.amountMinor
        : item.transactionType === "refund" ? -item.amountMinor
          : 0
    ), 0);
  const selected = transactions.filter((item) => selectedMonths.has(monthKey(item.transactionAt)));
  const gross = selected
    .filter((item) => item.transactionType === "expense")
    .reduce((total, item) => total + item.amountMinor, 0);
  const refunds = selected
    .filter((item) => item.transactionType === "refund")
    .reduce((total, item) => total + item.amountMinor, 0);
  const categoryNetFor = (start: string, end: string) => transactions
    .filter((item) => {
      const occurredOn = localDateKey(item.transactionAt);
      return occurredOn >= start && occurredOn <= end;
    })
    .filter((item) => item.transactionType === "expense" || item.transactionType === "refund")
    .reduce((totals, item) => {
      const category = item.category || "Uncategorized";
      totals.set(category, (totals.get(category) || 0) + (
        item.transactionType === "expense" ? item.amountMinor : -item.amountMinor
      ));
      return totals;
    }, new Map<string, number>());
  const currentByCategory = categoryNetFor(overview.period.start, overview.period.end);
  const previousByCategory = categoryNetFor(overview.period.previousStart, overview.period.previousEnd);
  const comparisonDrivers = [...new Set([
    ...currentByCategory.keys(),
    ...previousByCategory.keys(),
  ])].map((category) => {
    const currentMinor = currentByCategory.get(category) || 0;
    const previousMinor = previousByCategory.get(category) || 0;
    return {
      category,
      currentMinor,
      previousMinor,
      differenceMinor: currentMinor - previousMinor,
    };
  }).sort((left, right) => Math.abs(right.differenceMinor) - Math.abs(left.differenceMinor));

  return {
    categories,
    transactions,
    currentMonth,
    previousMonth,
    currentNetMinor: netFor(currentMonth),
    previousNetMinor: netFor(previousMonth),
    threeMonthGrossMinor: gross,
    threeMonthRefundMinor: refunds,
    threeMonthNetMinor: gross - refunds,
    excludedMonthBeforeWindow: monthLabel(shiftMonthKey(twoMonthsAgo, -1)),
    topCategories: overview.categories.slice(0, 3).map((item) => item.label),
    comparisonDrivers,
    recentTransactions: transactions.slice(0, 5),
    emiMinor: loan.baseline.emi_minor,
  };
}

async function runCompletedTurn(
  page: Page,
  request: APIRequestContext,
  conversationId: string,
  scenario: string,
  prompt: string,
  options: TurnOptions,
): Promise<QualitySample> {
  await page.goto(`/c/${conversationId}`);
  const composer = page.getByLabel("Message fyn AI");
  await expect(composer).toBeEnabled({ timeout: 30_000 });
  const startedAt = Date.now();
  await composer.fill(prompt);
  await composer.press("Enter");
  const running = page.getByText("fyn AI is working");
  await expect(running.last()).toBeVisible({ timeout: 30_000 });
  await expect(running).toHaveCount(0, { timeout: 180_000 });
  await expect(composer).toBeEnabled({ timeout: 30_000 });
  const elapsedMs = Date.now() - startedAt;

  const conversation = await jsonResponse<{
    messages: Array<{ role: string; content: string; citations: unknown[] }>;
  }>(
    await request.get(`${API_URL}/conversations/${conversationId}`),
    `read ${scenario} answer`,
  );
  const answerMessage = [...conversation.messages].reverse().find((item) => item.role === "assistant");
  const answer = answerMessage?.content.trim() || "";
  const thread = await jsonResponse<{
    latestRun: { id: string; status: string; taskStatus: string } | null;
  }>(
    await request.get(`${API_URL}/agent/threads/${conversationId}`),
    `read ${scenario} run`,
  );
  const run = thread.latestRun;
  const lowerAnswer = answer.toLocaleLowerCase("en-IN");
  const hardChecks = [
    check("run_succeeded", run?.status === "succeeded", `run status was ${run?.status || "missing"}`),
    check("task_succeeded", run?.taskStatus === "succeeded", `task status was ${run?.taskStatus || "missing"}`),
    check("answer_present", answer.length >= 12, `answer length was ${answer.length}`),
    check("no_internal_leak", !forbiddenAnswerPattern.test(answer), "answer contains no internal diagnostic markers"),
    check("no_failure_fallback", !failureAnswerPattern.test(answer), "answer is not a generic failure fallback"),
    ...(options.requiresSource
      ? [check("grounded_source", Boolean(answerMessage?.citations.length), `citations: ${answerMessage?.citations.length || 0}`)]
      : []),
    ...(options.expectedTerms || []).map((term) => check(
      `mentions_${term.toLocaleLowerCase("en-IN").replace(/[^a-z0-9]+/g, "_")}`,
      lowerAnswer.includes(term.toLocaleLowerCase("en-IN")),
      `expected term: ${term}`,
    )),
    ...(options.expectedMoney || []).map((amount) => check(
      `money_${amount}`,
      containsMoney(answer, amount),
      `expected exact minor-unit value: ${amount}`,
    )),
    ...(options.forbiddenTerms || []).map((term) => check(
      `does_not_mention_${term.toLocaleLowerCase("en-IN").replace(/[^a-z0-9]+/g, "_")}`,
      !lowerAnswer.includes(term.toLocaleLowerCase("en-IN")),
      `must not expand the requested scope to: ${term}`,
    )),
  ];

  if (options.requiresRelatedQuestions) {
    const related = page.getByRole("group", { name: "Ask next" }).last();
    try {
      await expect(related).toBeVisible({ timeout: 45_000 });
      hardChecks.push(check(
        "three_related_questions",
        await related.getByRole("button").count() === 3,
        `related question count: ${await related.getByRole("button").count()}`,
      ));
    } catch {
      hardChecks.push(check("three_related_questions", false, "related questions did not arrive within 45s"));
    }
  }

  return {
    scenario,
    category: options.category,
    conversationId,
    prompt,
    answer,
    rubric: options.rubric,
    referenceFacts: options.referenceFacts || [],
    elapsedMs,
    taskStatus: run?.taskStatus || "missing",
    runId: run?.id || null,
    hardChecks,
    hardPassed: hardChecks.every((item) => item.passed),
  };
}

test.describe("agent quality release corpus", () => {
  test.skip(!enabled, "Set RUN_AGENT_QUALITY_CORPUS=1 to make live model calls.");
  test.setTimeout(1_800_000);

  test("runs isolated browser journeys and writes a gradeable artifact", async ({ page, request }) => {
    const cohortStartedAt = new Date().toISOString();
    const samples: QualitySample[] = [];
    const infrastructureErrors: string[] = [];

    try {
      const oracle = await buildOracle(request);
      const standalone = [
        {
          scenario: "conversational_warmth",
          prompt: "Hi — how are you doing today?",
          options: {
            category: "conversation",
            rubric: "Respond naturally and warmly, without sounding templated, pretending to have feelings, or turning a greeting into a financial lecture.",
            requiresRelatedQuestions: true,
          },
        },
        {
          scenario: "clear_financial_explanation",
          prompt: "Explain principal versus interest using a simple ₹1 lakh loan example, without using my records.",
          options: {
            category: "education",
            rubric: "Explain both concepts accurately in plain language, use the requested example, distinguish balance repayment from borrowing cost, and avoid claiming to read personal records.",
            expectedTerms: ["principal", "interest"],
          },
        },
        {
          scenario: "grounded_taxonomy",
          prompt: "What expense categories do I have? Give three examples with their subcategories.",
          options: {
            category: "runtime_read",
            rubric: "Use authenticated taxonomy, answer the requested count and examples directly, preserve category/subcategory hierarchy, and do not invent paths.",
            referenceFacts: [
              ...oracle.categories.slice(0, 3).map((item) => `${item.label}: ${item.subcategories.slice(0, 3).map((child) => child.label).join(", ")}`),
            ],
            expectedTerms: ["Food", "Dining"],
            requiresSource: true,
          },
        },
        {
          scenario: "grounded_recent_records",
          prompt: "Show my five most recent transactions with their dates and amounts.",
          options: {
            category: "runtime_read",
            rubric: "Return exactly the five latest canonical records in recency order with date, amount, direction, and recognizable merchant/category labels.",
            referenceFacts: oracle.recentTransactions.map((item) => `${item.transactionAt.slice(0, 10)} | ${item.merchant || item.category || item.transactionType} | ${item.amountMinor} minor ${item.currency}`),
            expectedTerms: oracle.recentTransactions.slice(0, 3).map((item) => item.merchant || item.category || item.transactionType),
            expectedMoney: oracle.recentTransactions.slice(0, 2).map((item) => item.amountMinor),
            requiresSource: true,
          },
        },
        {
          scenario: "deterministic_emi",
          prompt: "What is the monthly EMI on a ₹12 lakh loan at 8% annual interest for 5 years? Include total interest and state the assumptions.",
          options: {
            category: "calculator",
            rubric: "Use the deterministic loan calculator, give the exact EMI and total interest, state monthly payments and the five-year/8% assumptions, and avoid unsupported caveats.",
            referenceFacts: [`Exact EMI: ${oracle.emiMinor} minor INR`, "Principal: 120000000 minor INR", "Tenure: 60 months", "Annual rate: 8%"],
            expectedTerms: ["EMI", "interest", "60"],
            expectedMoney: [oracle.emiMinor],
            requiresSource: true,
          },
        },
        {
          scenario: "grounded_month_summary",
          prompt: "How much did I spend this month? Give the exact net total and the top three categories.",
          options: {
            category: "semantic_analysis",
            rubric: "Use authenticated records, distinguish net spending from gross expenses, provide the exact month-to-date total, and rank the requested categories without unsupported arithmetic.",
            referenceFacts: [`Net month-to-date: ${oracle.currentNetMinor} minor INR`, `Top categories: ${oracle.topCategories.join(", ")}`],
            expectedTerms: oracle.topCategories,
            expectedMoney: [oracle.currentNetMinor],
            requiresSource: true,
          },
        },
        {
          scenario: "safe_empty_result",
          prompt: "Show expenses at a merchant named No Such Benchmark Merchant during January 2020.",
          options: {
            category: "empty_result",
            rubric: "State clearly that no matching records were found for the exact merchant and period, preserve the scope, and do not manufacture examples or values.",
            referenceFacts: ["Expected matching transaction count: 0", "Period: January 2020", "Merchant: No Such Benchmark Merchant"],
            expectedTerms: ["No Such Benchmark Merchant", "2020"],
            requiresSource: true,
          },
        },
        {
          scenario: "net_refund_reconciliation",
          prompt: "Across this month and the previous two months, reconcile gross expenses, refunds, and net spending, then identify the top categories without double-counting transactions.",
          options: {
            category: "complex_analysis",
            rubric: "Use one authenticated three-month scope, report gross expenses, refunds and net exactly, make the subtraction auditable, rank categories by the requested net basis, and avoid double counting.",
            referenceFacts: [
              `Gross expenses: ${oracle.threeMonthGrossMinor} minor INR`,
              `Refunds: ${oracle.threeMonthRefundMinor} minor INR`,
              `Net spending: ${oracle.threeMonthNetMinor} minor INR`,
            ],
            expectedTerms: ["gross", "refund", "net"],
            expectedMoney: [oracle.threeMonthGrossMinor, oracle.threeMonthRefundMinor, oracle.threeMonthNetMinor],
            forbiddenTerms: [oracle.excludedMonthBeforeWindow],
            requiresSource: true,
          },
        },
      ] satisfies Array<{ scenario: string; prompt: string; options: TurnOptions }>;

      for (const item of standalone.filter((candidate) => shouldRunScenario(candidate.scenario))) {
        const conversationId = await createConversation(request);
        try {
          samples.push(await runCompletedTurn(
            page,
            request,
            conversationId,
            item.scenario,
            item.prompt,
            item.options,
          ));
        } catch (error) {
          infrastructureErrors.push(`${item.scenario}: ${error instanceof Error ? error.message : String(error)}`);
        }
      }

      const includeContextSetup = shouldRunScenario("contextual_comparison_setup");
      const includeContextFollowUp = shouldRunScenario("contextual_follow_up");
      if (includeContextSetup || includeContextFollowUp) {
        const contextualConversation = await createConversation(request);
        const setupPrompt = "Compare this month's net spending with the same elapsed days last month and show the three largest category drivers.";
        try {
          const setupSample = await runCompletedTurn(
            page,
            request,
            contextualConversation,
            "contextual_comparison_setup",
            setupPrompt,
            {
              category: "context",
              rubric: "Perform a like-for-like elapsed-day comparison, report both exact totals and the difference, and rank three category drivers by absolute change.",
            referenceFacts: [
              `Current net: ${oracle.currentNetMinor} minor INR`,
              `Previous net: ${oracle.previousNetMinor} minor INR`,
              ...oracle.comparisonDrivers.slice(0, 3).map((item, index) => (
                `Driver ${index + 1}: ${item.category}; current ${item.currentMinor} minor INR; previous ${item.previousMinor} minor INR; difference ${item.differenceMinor} minor INR`
              )),
            ],
              expectedMoney: [oracle.currentNetMinor, oracle.previousNetMinor],
              requiresSource: true,
            },
          );
          if (includeContextSetup) samples.push(setupSample);
          if (includeContextFollowUp) {
            samples.push(await runCompletedTurn(
              page,
              request,
              contextualConversation,
              "contextual_follow_up",
              "Based on that comparison, which two drivers deserve attention first and why? Keep the same period and figures.",
              {
                category: "context",
                rubric: "Resolve 'that comparison' and 'same period' from the immediately preceding turn, prioritize exactly two grounded drivers, preserve figures, and explain the practical reason without inventing new facts.",
                referenceFacts: [
                  "Must preserve the preceding elapsed-day comparison scope",
                  "Must choose exactly two previously grounded category drivers",
                  ...oracle.comparisonDrivers.slice(0, 3).map((item, index) => (
                    `Driver ${index + 1}: ${item.category}; current ${item.currentMinor} minor INR; previous ${item.previousMinor} minor INR; difference ${item.differenceMinor} minor INR`
                  )),
                ],
                requiresSource: true,
              },
            ));
          }
        } catch (error) {
          infrastructureErrors.push(`contextual_flow: ${error instanceof Error ? error.message : String(error)}`);
        }
      }

      if (shouldRunScenario("governed_transaction_action")) {
        const actionConversation = await createConversation(request);
        const actionPrompt = "Add ₹500 expense under Food → Dining at Quality Eval Cafe today.";
        try {
          const sample = await runCompletedTurn(
            page,
            request,
            actionConversation,
            "governed_transaction_action",
            actionPrompt,
            {
              category: "action",
              rubric: "Execute the explicit transaction exactly once using profile INR, preserve amount, direction, taxonomy, merchant and date, then acknowledge what was saved without asking for currency.",
              referenceFacts: ["Amount: 50000 minor INR", "Type: expense", "Category: Food", "Subcategory: Dining", "Merchant: Quality Eval Cafe"],
              expectedTerms: ["Food", "Dining"],
              expectedMoney: [50_000],
            },
          );
          const matches = await jsonResponse<Transaction[]>(
            await request.get(`${API_URL}/transactions?limit=10&q=Quality%20Eval%20Cafe&include_removed=false`),
            "verify eval transaction action",
          );
          const saved = matches.filter((item) => item.merchant === "Quality Eval Cafe" && item.amountMinor === 50_000);
          sample.hardChecks.push(check("one_exact_mutation", saved.length === 1, `matching saved rows: ${saved.length}`));
          sample.hardChecks.push(check("profile_currency_preserved", saved[0]?.currency === "INR", `saved currency: ${saved[0]?.currency || "missing"}`));
          sample.hardPassed = sample.hardChecks.every((item) => item.passed);
          samples.push(sample);
          if (saved[0]) {
            const removed = await request.delete(`${API_URL}/transactions/${saved[0].id}`);
            expect(removed.ok(), `clean up eval mutation: ${await removed.text()}`).toBeTruthy();
          }
        } catch (error) {
          infrastructureErrors.push(`governed_transaction_action: ${error instanceof Error ? error.message : String(error)}`);
        }
      }
    } finally {
      const artifact = {
        schemaVersion: 1,
        kind: "fyn_agent_quality_browser_eval",
        cohortStartedAt,
        finishedAt: new Date().toISOString(),
        environment: "localhost",
        account: "FYN Quality Eval",
        samples,
        infrastructureErrors,
      };
      mkdirSync(dirname(resultsPath), { recursive: true });
      writeFileSync(resultsPath, `${JSON.stringify(artifact, null, 2)}\n`, "utf8");
      process.stdout.write(`${JSON.stringify({
        type: "agent_quality_browser_cohort",
        cohortStartedAt,
        scenarios: samples.map((item) => item.scenario),
        hardPassed: samples.filter((item) => item.hardPassed).length,
        hardFailed: samples.filter((item) => !item.hardPassed).length,
        infrastructureErrors: infrastructureErrors.length,
        resultsPath,
      })}\n`);
    }
  });
});
