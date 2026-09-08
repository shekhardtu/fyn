import { expect, type Page, test } from "@playwright/test";
import type { Bootstrap, BudgetRecordOut, GoalRecordOut, OverviewOut } from "../src/lib/generated/contracts";

// Deterministic UI checks never read or mutate a real financial account.
test.use({ storageState: { cookies: [], origins: [] } });
const threadId = "00000000-0000-4000-8000-000000000001";
const fixtureId = (index: number) => `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
const bootstrap: Bootstrap = {
  user: { id: "00000000-0000-4000-8000-000000000002", name: "Sam", currency: "INR", timezone: "Asia/Kolkata" },
  active_conversation: { id: threadId, title: "My finances", messages: [], updated_at: "2026-09-08T10:00:00Z" },
  features: { personalLending: false },
};
const overview: OverviewOut = {
  period: { start: "2026-09-01", end: "2026-09-08", previousStart: "2026-08-01", previousEnd: "2026-08-08", label: "September 2026", isCurrent: true },
  summary: { currency: "INR", incomeMinor: 12_500_000, spentMinor: 2_346_000, netMinor: 10_154_000, expenseCount: 24, previousSpentMinor: 2_600_000, changeMinor: -254_000, changePercent: -9.8 },
  budgets: [{ id: fixtureId(3), name: "Monthly budget", categoryId: null, categorySlug: null, category: null, amountMinor: 6_000_000, spentMinor: 2_346_000, remainingMinor: 3_654_000, overMinor: 0, percentUsed: 39.1, currency: "INR", period: "monthly" }],
  categories: [
    { id: "food", label: "Food & dining", amountMinor: 1_000_000, count: 12, sharePercent: 42.6, subcategories: [] },
    { id: "shopping", label: "Shopping", amountMinor: 800_000, count: 4, sharePercent: 34.1, subcategories: [] },
    { id: "transport", label: "Transport", amountMinor: 546_000, count: 8, sharePercent: 23.3, subcategories: [] },
  ],
  trend: Array.from({ length: 8 }, (_, index) => ({ day: index + 1, date: `2026-09-0${index + 1}`, incomeMinor: index === 0 ? 12_500_000 : 0, spentMinor: index === 0 ? 246_000 : 300_000, previousIncomeMinor: index === 0 ? 12_000_000 : 0, previousSpentMinor: 325_000 })),
  recentTransactions: ["Blue Tokai", "Uber", "Amazon", "Whole Foods", "Salary"].map((merchant, index) => ({ id: fixtureId(10 + index), merchant, transactionType: index === 4 ? "income" : "expense", amountMinor: index === 4 ? 12_500_000 : [45_000, 32_000, 249_900, 128_000][index], currency: "INR", transactionAt: `2026-09-0${8 - index}T10:00:00Z`, category: index === 4 ? null : ["Food & dining", "Transport", "Shopping", "Groceries"][index], account: "Everyday account" })),
  accounts: ["Everyday account", "Savings", "Travel fund", "Cash wallet"].map((name, index) => ({ id: fixtureId(20 + index), name, accountType: "savings", institution: "My bank", mask: `123${index}`, balanceMinor: 8_000_000 - index * 1_000_000, currency: "INR" })),
};

async function mockApp(page: Page, data: OverviewOut | ((url: URL) => OverviewOut) = overview, financeWrites = false) {
  const writes: string[] = [];
  const budgets: BudgetRecordOut[] = overview.budgets.map(({ id, name, amountMinor, categoryId, currency, period }) => ({ id, name, amountMinor, categoryId, currency, period }));
  const goals: GoalRecordOut[] = [];
  const accounts = [...overview.accounts];
  const categories = [{ id: fixtureId(30), slug: "food", label: "Food", icon: "utensils", editable: false, hints: [], subcategories: [] }];
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api/, "");
    if (route.request().method() !== "GET") {
      writes.push(path);
      if (financeWrites) {
        if (route.request().method() === "DELETE" && path.startsWith("/accounts/")) {
          const index = accounts.findIndex((account) => path === `/accounts/${account.id}`);
          if (index < 0) { await route.fulfill({ status: 404, json: { detail: "Unknown account" } }); return; }
          accounts.splice(index, 1);
          await route.fulfill({ status: 204 }); return;
        }
        const body = route.request().postDataJSON();
        if (path === "/budgets") {
          const budget = { ...body, id: fixtureId(40 + budgets.length), currency: "INR", period: "monthly" };
          budgets.push(budget);
          await route.fulfill({ status: 201, json: budget }); return;
        }
        if (path.startsWith("/budgets/")) {
          const budget = budgets.find((budget) => path === `/budgets/${budget.id}`)!;
          Object.assign(budget, body);
          await route.fulfill({ json: budget }); return;
        }
        if (path === "/goals") {
          const goal = { ...body, id: fixtureId(50 + goals.length), currency: "INR", currentMinor: 0 };
          goals.push(goal);
          await route.fulfill({ status: 201, json: goal }); return;
        }
        if (path.startsWith("/goals/")) {
          const goal = goals.find((goal) => path.includes(goal.id))!;
          if (path.endsWith("/contributions")) goal.currentMinor += body.amountMinor;
          else Object.assign(goal, body);
          await route.fulfill({ json: goal }); return;
        }
        if (path === "/accounts") {
          const account = { ...body, id: fixtureId(60 + accounts.length) };
          accounts.push(account);
          await route.fulfill({ status: 201, json: account }); return;
        }
      }
      await route.fulfill({ status: 503, json: { detail: "Please try again." } });
      return;
    }
    const overviewData = typeof data === "function" ? data(url) : data;
    const payloads: Record<string, unknown> = {
      "/bootstrap": bootstrap,
      // Exercise the bootstrap fallback while conversation history is empty.
      "/conversations": { items: [], nextCursor: null },
      [`/conversations/${threadId}`]: bootstrap.active_conversation,
      [`/agent/threads/${threadId}`]: { threadId, activeRun: null, latestRun: null, interrupts: [] },
      "/overview": financeWrites ? { ...overviewData, accounts, budgets: budgets.map((budget) => ({ ...budget, category: budget.categoryId ? "Food" : null, categorySlug: budget.categoryId ? "food" : null, spentMinor: 0, remainingMinor: budget.amountMinor, overMinor: 0, percentUsed: 0 })) } : overviewData,
      "/privacy": { locationEnabled: false, sources: {} },
      "/categories": categories,
      "/budgets": budgets,
      "/goals": goals,
      "/transactions": [],
      "/dashboards": { dashboards: [] },
    };
    await route.fulfill({ status: path in payloads ? 200 : 404, json: payloads[path] ?? {} });
  });
  return writes;
}

for (const width of [360, 375, 390, 1440]) {
  test(`overview and full-screen entry fit a ${width}px viewport`, async ({ page }, testInfo) => {
    const height = width === 360 ? 740 : width === 375 ? 667 : width === 390 ? 844 : 1000;
    await page.setViewportSize({ width, height });
    if (width === 1440) await page.emulateMedia({ colorScheme: "dark" });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await mockApp(page);
    await page.goto("/overview");
    await expect(page.getByRole("region", { name: "Money summary for September 2026" })).toBeVisible();
    await expect(page.getByLabel("Spending change from previous period")).toHaveText(/9.8% lower/);
    await expect(page.getByLabel("Income change from previous period")).toHaveText(/4.2% higher/);
    await expect(page.getByLabel("Income minus expenses change from previous period")).toHaveText(/8% higher/);
    await expect(page.getByText(/Changes vs 1.*8 Aug/)).toBeVisible();
    await expect(page.getByRole("group", { name: "Spending pace", exact: true })).toBeVisible();
    const budgetBox = await page.getByRole("region", { name: "₹60,000 limit", exact: true }).boundingBox();
    const recentBox = await page.getByRole("region", { name: "Recent activity", exact: true }).boundingBox();
    expect(budgetBox!.y + budgetBox!.height).toBeLessThanOrEqual(recentBox!.y);
    const actions = page.getByRole("navigation", { name: "Overview quick actions" });
    const add = width < 768 ? actions.getByRole("link", { name: "Add transaction" }) : page.getByRole("link", { name: "Add transaction" }).first();
    await expect(add).toBeInViewport();
    if (width < 768) {
      await expect(actions).toBeInViewport();
      const chart = page.getByRole("region", { name: "Your month in motion", exact: true });
      await expect(chart).toBeInViewport({ ratio: 1 });
      // An element can intersect the viewport while being covered by a dock.
      // Check the complete chart card against both persistent pieces of chrome.
      const chartBox = await chart.boundingBox();
      const dockBox = await actions.boundingBox();
      const headerBox = await page.locator("main header").boundingBox();
      expect(chartBox!.y).toBeGreaterThanOrEqual(headerBox!.y + headerBox!.height);
      expect(chartBox!.y + chartBox!.height).toBeLessThanOrEqual(dockBox!.y);
      await expect(page.getByRole("button", { name: "Month", exact: true })).toHaveAttribute("aria-pressed", "true");
      await page.getByRole("heading", { name: "Where it went" }).scrollIntoViewIfNeeded();
      await expect(add).toBeInViewport();
      await expect(page.getByRole("combobox", { name: "Overview month" })).toBeInViewport();
      await page.getByText("Spent this month", { exact: true }).scrollIntoViewIfNeeded();
    }
    if (width === 1440) {
      for (const names of [["Money summary for September 2026", "Your month in motion"], ["Recent activity", "Where it went"], ["4 accounts, one view", "Budgets & goals"]]) {
        const left = await page.getByRole("region", { name: names[0], exact: true }).boundingBox();
        const right = await page.getByRole("region", { name: names[1], exact: true }).boundingBox();
        expect(Math.abs(left!.y - right!.y)).toBeLessThanOrEqual(1);
        expect(Math.abs(left!.height - right!.height)).toBeLessThanOrEqual(1);
      }
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath(`overview-${width}.png`) });

    await add.click();
    await expect(page).toHaveURL(/\/transactions\?new=1$/);
    const editor = page.getByRole("dialog", { name: "Add transaction", exact: true });
    await expect(editor).toBeVisible();
    const box = await editor.boundingBox();
    expect(box?.x).toBe(0);
    expect(box?.y).toBe(0);
    expect(box?.width).toBe(width);
    expect(box?.height).toBe(height);
    await expect(editor.getByRole("button", { name: "Add transaction", exact: true })).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath(`add-transaction-${width}.png`) });
    await page.reload();
    await expect(editor).toBeVisible();
    await editor.getByLabel("Transaction amount").fill("450");
    await editor.getByLabel("Merchant", { exact: true }).fill("Lunch");
    await editor.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(page.getByRole("alertdialog", { name: "Discard unsaved changes" })).toBeVisible();
    await page.getByRole("button", { name: "Keep editing" }).click();
    await expect(editor.getByLabel("Transaction amount")).toHaveValue("450");
    await editor.getByRole("button", { name: "Add transaction", exact: true }).click();
    await expect(editor.getByRole("alert")).toContainText("Please try again");
    await expect(editor.getByLabel("Merchant", { exact: true })).toHaveValue("Lunch");
    expect(errors).toEqual([]);
  });
}

test("advice actions prefill the composer without sending", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const writes = await mockApp(page);
  const actions: Array<[string | RegExp, RegExp]> = [
    ["Ask fyn", /Review my finances for September 2026/],
  ];
  for (const [label, prompt] of actions) {
    await page.goto("/overview");
    await page.getByRole("button", { name: label, exact: typeof label === "string" }).first().click();
    await expect(page.getByRole("textbox", { name: "Message fyn AI" })).toHaveValue(prompt);
    await expect(page).toHaveURL(new RegExp(`/c/${threadId}$`));
  }
  await page.goto("/dashboards");
  await page.getByRole("button", { name: "Open a conversation" }).click();
  await expect(page.getByRole("textbox", { name: "Message fyn AI" })).toHaveValue(/save it to a dashboard/);
  expect(writes).toEqual([]);
});

test.describe("touch transaction entry", () => {
  test.use({ isMobile: true, hasTouch: true, viewport: { width: 390, height: 844 }, colorScheme: "dark" });

  test("keeps optional fields, readable inputs, and save feedback reachable on a short viewport", async ({ page }, testInfo) => {
    await mockApp(page);
    await page.goto("/transactions?new=1");
    const editor = page.getByRole("dialog", { name: "Add transaction", exact: true });
    const amount = editor.getByLabel("Transaction amount");
    const save = editor.getByRole("button", { name: "Add transaction", exact: true });
    const moreDetails = editor.locator("summary").filter({ hasText: "More details" });
    await expect(editor.getByLabel("Transaction location")).toBeHidden();
    await amount.fill("450");
    await editor.getByRole("combobox", { name: "Transaction currency" }).click();
    await page.getByRole("option", { name: "US dollar (USD)", exact: true }).click();
    await expect(editor.getByRole("combobox", { name: "Transaction currency" })).toHaveText("USD");
    await expect(editor).toContainText("Your overview totals use INR");
    await editor.getByLabel("Merchant", { exact: true }).fill("Lunch");
    expect(await amount.evaluate((input) => Number.parseFloat(getComputedStyle(input).fontSize))).toBeGreaterThanOrEqual(44);
    expect(await editor.getByLabel("Merchant", { exact: true }).evaluate((input) => Number.parseFloat(getComputedStyle(input).fontSize))).toBeGreaterThanOrEqual(16);
    await expect(save).toBeEnabled();
    await page.screenshot({ path: testInfo.outputPath("add-transaction-dark-mobile.png"), animations: "disabled" });

    await moreDetails.click();
    await editor.getByLabel("Transaction location").fill("Bengaluru");
    await editor.getByRole("combobox", { name: "Spend nature" }).click();
    await page.getByRole("option", { name: "Essential", exact: true }).click();
    await moreDetails.click();
    await expect(editor.getByLabel("Transaction location")).toBeHidden();
    await expect(moreDetails).toContainText("Location added");

    await editor.getByRole("combobox", { name: "More transaction types" }).click();
    await page.getByRole("option", { name: "Refund", exact: true }).click();
    await expect(editor.getByRole("combobox", { name: "Transaction category" })).toHaveCount(0);
    await expect(amount).toHaveValue("450");
    // A reduced viewport exercises the same layout constraint as a keyboard;
    // native keyboard behaviour still needs an actual-device smoke test.
    await page.setViewportSize({ width: 390, height: 420 });
    await expect(save).toBeInViewport({ ratio: 1 });
    const request = page.waitForRequest((request) => request.method() === "POST" && new URL(request.url()).pathname === "/api/transactions");
    await save.click();
    expect((await request).postDataJSON()).toMatchObject({ currency: "USD", amountMinor: 45_000, merchant: "Lunch", transactionType: "refund", location: "Bengaluru", categoryId: null, spendNature: "unknown" });
    await expect(editor.getByRole("alert")).toBeInViewport({ ratio: 1 });
    await expect(editor.getByRole("alert")).toContainText("Please try again");
    await expect(save).toBeInViewport({ ratio: 1 });
    await expect(editor.getByLabel("Merchant", { exact: true })).toHaveValue("Lunch");
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath("add-transaction-short-viewport.png") });
  });
});

test.describe("direct mobile finance forms", () => {
  test.use({ isMobile: true, hasTouch: true });
  for (const width of [360, 390, 1440]) {
    test(`budget, goal, savings and account forms save without chat at ${width}px`, async ({ page }, testInfo) => {
      const height = width === 360 ? 740 : width === 390 ? 844 : 1000;
      await page.setViewportSize({ width, height });
      if (width !== 360) await page.emulateMedia({ colorScheme: "dark" });
      const writes = await mockApp(page, overview, true);
      const editor = page.getByRole("dialog", { name: /^(Edit budget|Set budget|Set a goal|Edit goal|Add savings|Add account)$/ });
      async function checkForm(action: string, screenshot: string) {
        await expect(editor).toBeVisible();
        const box = await editor.boundingBox();
        expect(box).toMatchObject({ x: 0, y: 0, width, height });
        await expect(editor.getByRole("button", { name: action, exact: true })).toBeInViewport({ ratio: 1 });
        // Saving the previous form can leave a success toast on screen. The
        // next form's action must be clickable immediately, above that toast.
        await editor.getByRole("button", { name: action, exact: true }).click({ trial: true });
        expect(await editor.evaluate((element) => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
        await page.screenshot({ path: testInfo.outputPath(`${screenshot}-${width}.png`), animations: "disabled" });
      }
      await page.goto("/overview");
      await page.getByRole("button", { name: "Adjust budget", exact: true }).click();
      await expect(page).toHaveURL(new RegExp(`form=budget&record=${fixtureId(3)}`));
      await expect(editor.getByRole("textbox", { name: "Monthly limit" })).toHaveValue("60000");
      await checkForm("Save budget", "edit-budget");
      await editor.getByRole("textbox", { name: "Monthly limit" }).fill("65000");
      await editor.getByRole("button", { name: "Save budget", exact: true }).click();
      await expect(editor).toHaveCount(0);
      await expect(page.getByRole("group", { name: "Budget at a glance" })).toContainText("₹65,000");

      await page.getByRole("button", { name: "Category", exact: true }).click();
      await page.getByRole("button", { name: /Add category budget/ }).click();
      await page.reload();
      await expect(editor.getByRole("combobox", { name: "Budget scope" })).toHaveText("Choose a category");
      await editor.getByRole("combobox", { name: "Budget scope" }).click();
      await page.getByRole("option", { name: "Food", exact: true }).click();
      await editor.getByRole("textbox", { name: "Monthly limit" }).fill("5000");
      await checkForm("Save budget", "category-budget");
      await editor.getByRole("button", { name: "Save budget" }).click();
      await expect(editor).toHaveCount(0);

      await page.getByRole("button", { name: "Set a goal", exact: true }).click();
      await editor.getByRole("textbox", { name: "Target amount" }).fill("100000");
      await editor.getByRole("textbox", { name: "Goal name" }).fill("Emergency fund");
      await editor.getByLabel("Goal target date").fill("2027-01-01");
      await checkForm("Save goal", "new-goal");
      await editor.getByRole("button", { name: "Save goal" }).click();
      const goal = page.getByRole("article", { name: "Savings goal: Emergency fund" });
      await expect(goal).toContainText("₹1,00,000");
      await goal.getByRole("button", { name: "Edit goal" }).click();
      await expect(editor.getByRole("textbox", { name: "Target amount" })).toHaveValue("100000");
      await editor.getByRole("button", { name: "Cancel" }).click();
      await goal.getByRole("button", { name: "Add savings" }).click();
      await editor.getByRole("textbox", { name: "Amount saved" }).fill("1000");
      await checkForm("Add savings", "add-savings");
      await editor.getByRole("button", { name: "Add savings" }).click();
      await expect(goal).toContainText("₹1,000 of ₹1,00,000 saved");

      await page.getByRole("button", { name: "Add account", exact: true }).click();
      await editor.getByRole("textbox", { name: "Current balance" }).fill("250");
      await editor.getByRole("button", { name: "Money owed" }).click();
      await editor.getByRole("textbox", { name: "Account name" }).fill("Travel card");
      await editor.getByRole("combobox", { name: "Account type" }).click();
      await page.getByRole("option", { name: "Credit card", exact: true }).click();
      await editor.getByRole("combobox", { name: "Account currency" }).click();
      await page.getByRole("option", { name: "US dollar (USD)", exact: true }).click();
      await checkForm("Add account", "add-account");
      await editor.getByRole("button", { name: "Add account", exact: true }).click();
      await expect(editor).toHaveCount(0);
      await expect(page.getByText("Travel card", { exact: true })).toBeVisible();
      await expect(page.getByText("-$250", { exact: true })).toBeVisible();
      await expect(page).toHaveURL(/\/overview$/);
      expect(writes).toEqual([`/budgets/${fixtureId(3)}`, "/budgets", "/goals", `/goals/${fixtureId(50)}/contributions`, "/accounts"]);
    });
  }
});

for (const width of [360, 1440]) {
  test(`account deletion confirms, refreshes, and reaches the empty state at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 360 ? 740 : 1000 });
    await page.emulateMedia({ colorScheme: "dark" });
    const writes = await mockApp(page, overview, true);
    await page.goto("/overview");
    const trigger = page.getByRole("button", { name: "Delete account Everyday account", exact: true });
    await trigger.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`account-cards-${width}.png`) });
    await trigger.click();
    const dialog = page.getByRole("alertdialog", { name: "Delete account?" });
    await expect(dialog).toContainText("Everyday account");
    await expect(dialog).toContainText("Your transactions will stay");
    await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
    await expect(dialog.getByRole("button", { name: "Delete account", exact: true })).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath(`delete-account-${width}.png`) });
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
    expect(writes).toEqual([]);

    for (const [index, account] of overview.accounts.entries()) {
      await page.getByRole("button", { name: `Delete account ${account.name}`, exact: true }).click();
      await dialog.getByRole("button", { name: "Delete account", exact: true }).click();
      await expect(dialog).toHaveCount(0);
      await expect(page.getByRole("button", { name: `Delete account ${account.name}`, exact: true })).toHaveCount(0);
      const remaining = overview.accounts.length - index - 1;
      await expect(page.getByRole("heading", { name: remaining ? `${remaining} account${remaining === 1 ? "" : "s"}, one view` : "Start with an account", exact: true })).toBeVisible();
    }
    await expect(page.getByRole("button", { name: "Add account", exact: true })).toBeVisible();
    expect(writes).toEqual(overview.accounts.map((account) => `/accounts/${account.id}`));
    await page.reload();
    await expect(page.getByRole("heading", { name: "Start with an account" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  });
}

test("an empty month keeps the dashboard, direct entry, and contextual chat available", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const writes = await mockApp(page, { ...overview, summary: { ...overview.summary, incomeMinor: 0, spentMinor: 0, netMinor: 0, expenseCount: 0 }, accounts: [], budgets: [], recentTransactions: [], categories: [], trend: overview.trend.map((point) => ({ ...point, incomeMinor: 0, spentMinor: 0 })) });
  await page.goto("/overview");
  await expect(page.getByRole("heading", { name: "No transactions in September 2026" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Money summary for September 2026" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Recent activity" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Your month in motion" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Your month in motion", exact: true })).toBeInViewport({ ratio: 1 });
  await expect(page.getByRole("heading", { name: "Your money story starts here" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "All transactions", exact: true })).toHaveAttribute("href", "/transactions");
  await expect(page.getByRole("navigation", { name: "Overview quick actions" }).getByRole("link", { name: "Add transaction" })).toBeEnabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: testInfo.outputPath("empty-month-mobile.png") });
  await page.getByRole("button", { name: "Ask fyn", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Message fyn AI" })).toHaveValue(/Review my finances for September 2026/);
  expect(writes).toEqual([]);
});

test("an empty September links to August records and preserves the chosen month on reload", async ({ page }, testInfo) => {
  await page.clock.setFixedTime(new Date("2026-09-08T10:00:00Z"));
  await page.emulateMedia({ colorScheme: "dark" });
  await page.setViewportSize({ width: 1440, height: 1000 });
  const august: OverviewOut = {
    ...overview,
    period: { start: "2026-08-01", end: "2026-08-31", previousStart: "2026-07-01", previousEnd: "2026-07-31", label: "August 2026", isCurrent: false },
    trend: overview.trend.map((point) => ({ ...point, date: point.date.replace("2026-09", "2026-08") })),
    recentTransactions: overview.recentTransactions.map((transaction) => ({ ...transaction, transactionAt: transaction.transactionAt.replace("2026-09", "2026-08") })),
  };
  const emptySeptember: OverviewOut = {
    ...overview,
    summary: { ...overview.summary, incomeMinor: 0, spentMinor: 0, netMinor: 0, expenseCount: 0 },
    accounts: [], budgets: [], recentTransactions: [], categories: [],
    trend: overview.trend.map((point) => ({ ...point, incomeMinor: 0, spentMinor: 0 })),
  };
  const requestedMonths: Array<string | null> = [];
  await mockApp(page, (url) => {
    if (url.pathname === "/api/overview") requestedMonths.push(url.searchParams.get("month"));
    return url.searchParams.get("month") === "2026-08-01" ? august : emptySeptember;
  });
  await page.goto("/overview");
  await expect(page.getByRole("heading", { name: "No transactions in September 2026" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("empty-month-desktop.png") });
  await page.getByRole("button", { name: "View August 2026", exact: true }).click();
  await expect(page).toHaveURL(/\/overview\?month=2026-08$/);
  await expect(page.getByRole("combobox", { name: "Overview month" })).toHaveText("August 2026");
  await expect(page.getByRole("region", { name: "Money summary for August 2026" })).toContainText("₹1,01,540");
  await expect(page.getByText("Blue Tokai", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "No activity this month" })).toHaveCount(0);
  expect(requestedMonths).toEqual(expect.arrayContaining(["2026-09-01", "2026-08-01"]));

  await page.reload();
  await expect(page.getByRole("region", { name: "Money summary for August 2026" })).toContainText("₹1,01,540");
  await page.goBack();
  await expect(page.getByRole("heading", { name: "No transactions in September 2026" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Money summary for September 2026" })).toBeVisible();
});
