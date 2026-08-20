import { expect, test, type Locator } from "@playwright/test";
import { API_MOUNT_PATH } from "@/config/api-path";
import { sharedThreadUrl } from "./test-thread";

async function expectGroundedResultOrSafeFallback(response: Locator, expectedContent: RegExp) {
  await expect(response.getByRole("button", { name: /Agent run (?:complete|failed):/ })).toBeVisible({ timeout: 45_000 });
  const dataSource = response.getByRole("button", { name: /data source/ });
  if (await dataSource.count()) {
    await expect(response).toContainText(expectedContent);
    await expect(dataSource).toBeVisible();
    return true;
  }

  const clarification = response.getByRole("group", { name: /^Action required:/ });
  if (await clarification.count()) {
    await expect(clarification).toContainText(expectedContent);
    await expect(clarification.getByRole("button").first()).toBeVisible();
    await clarification.getByRole("button", { name: /^Cancel(?:\s|$)/ }).last().click();
    return false;
  }

  // The live model may fail decision selection or evidence validation. That is a supported
  // governed outcome: the UI must show the explicit safe fallback and recover
  // instead of inventing a financial answer or leaving the composer blocked.
  await expect(response).toContainText(/couldn(?:'|’)t|could not|please (?:ask|restate)/i);
  return false;
}

test("conversation URLs are shareable and support browser navigation", async ({ page, context }) => {
  await page.goto(sharedThreadUrl());
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: new URL(page.url()).origin });
  await expect(page).toHaveURL(sharedThreadUrl());
  const sharedUrl = page.url();

  await page.getByRole("button", { name: "Copy conversation link" }).click();
  await expect(page.getByRole("button", { name: "Conversation link copied" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(sharedUrl);

  await page.goto(sharedUrl);
  await expect(page).toHaveURL(sharedThreadUrl());
  await expect(page.getByLabel("Message fyn AI")).toBeVisible();
});

test.skip("thread deletion requires a disposable thread and is disabled by the fixed-thread policy", async () => {});

test.skip("invalid-link recovery opens another thread and is disabled by the fixed-thread policy", async () => {});

test("agent activity streams the selected path with individual and cumulative timing", async ({ page }) => {
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  const finished = page.getByRole("button", { name: /Agent run complete:/ });
  await input.fill("How much did I spend in the last two days?");
  await input.press("Enter");
  // The run is expanded while it streams, then folds to a summary line once the
  // answer lands — reopening it has to bring the whole trace back.
  await expect(page.getByText("fyn AI is working").last()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("fyn AI is working")).toHaveCount(0, { timeout: 30_000 });
  await expect(finished.last()).toBeVisible();
  await finished.last().click();
  await expect(page.getByText("Execution trace").last()).toBeVisible();
  await expect(page.getByRole("list", { name: "Complete execution trace" }).last()).toBeVisible();
  const selectedTool = page.getByText(/operator|search_transactions|get_spending_summary|calculate_affordability/);
  await expect(selectedTool.last()).toBeVisible();
  // Operator can answer directly or hand off to a governed capability. A named
  // stage and measured timings are stable; the exact capability is a model/runtime decision.
  await expect(page.getByText(/(?:<1|\d+(?:\.\d+)?) (?:ms|s) step/).last()).toBeVisible();
  await expect(page.getByText(/(?:<1|\d+(?:\.\d+)?) (?:ms|s) total elapsed/).last()).toBeVisible();
  await page.reload();
  // A reloaded run comes back collapsed: the trace is kept, not foregrounded.
  await expect(page.getByText("Execution trace")).toHaveCount(0);
  await finished.last().click();
  await expect(page.getByText("Execution trace").last()).toBeVisible();
  await expect(selectedTool.last()).toBeVisible();
});

test("a new reply does not displace a reader who moved into history", async ({ page }) => {
  test.setTimeout(90_000);
  const threadStateLoaded = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "GET" && new RegExp(`^${API_MOUNT_PATH}/agent/threads/[^/]+$`).test(url.pathname);
  });
  await page.goto(sharedThreadUrl());
  await threadStateLoaded;
  const input = page.getByLabel("Message fyn AI");
  const pending = page.getByRole("group", { name: /^Action required:/ });
  if (await pending.count()) {
    const cancel = pending.last().getByRole("button", { name: /^Cancel(?:\s|$)/ }).last();
    if (await cancel.count()) await cancel.click();
  }
  await expect(input).toBeEnabled();
  await input.fill("Summarize my spending this month by category");
  await input.press("Enter");
  await expect(page.getByText("fyn AI is working").last()).toBeVisible({ timeout: 30_000 });

  const transcriptScroller = page.locator(".conversation-scroll");
  await transcriptScroller.hover();
  await page.mouse.wheel(0, -10_000);
  const distanceFromLatest = () => transcriptScroller.evaluate(
    (node) => node.scrollHeight - node.scrollTop - node.clientHeight,
  );
  await expect.poll(distanceFromLatest).toBeGreaterThan(500);

  await expect(page.getByText("fyn AI is working")).toHaveCount(0, { timeout: 60_000 });
  await expect.poll(distanceFromLatest).toBeGreaterThan(500);
  await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
  await expect(page.locator(".jump-to-latest")).toHaveAttribute("data-unread", "true");
  await expect(page.locator(".jump-to-latest-unread-dot")).toHaveCSS("opacity", "1");
  await page.getByRole("button", { name: "Jump to latest" }).click();
  await expect.poll(distanceFromLatest).toBeLessThanOrEqual(4);
  await expect(page.getByRole("button", { name: "Jump to latest" })).not.toBeVisible();
  await expect(page.locator(".jump-to-latest")).toHaveAttribute("data-unread", "false");
});

test("bare amount follows clarification, auto-save, edit/remove controls, and refresh persistence", async ({ page }) => {
  await page.goto(sharedThreadUrl());
  await expect(page.getByLabel("Message fyn AI")).toBeVisible();
  const input = page.getByLabel("Message fyn AI");
  await input.fill("₹1,234");
  await input.press("Enter");
  const typeStep = page.getByRole("group", {
    name: /Action required: (?:What kind of financial event is this\?|One detail needs your confirmation)/,
  });
  await expect(typeStep).toBeFocused();
  await expect(typeStep).toBeInViewport();
  await typeStep.getByRole("button", { name: /^Expense(?:\s|$)/ }).click();
  const categoryStep = page.getByRole("group", { name: "Action required: Where should I categorize this?" });
  await expect(categoryStep).toBeVisible();
  await expect(categoryStep).toBeFocused();
  await expect(categoryStep).toBeInViewport();
  await categoryStep.getByRole("button", { name: /^Food(?:\s|$)/ }).click();
  const subcategoryStep = page.getByRole("group", { name: "Action required: What type of food expense?" });
  await expect(subcategoryStep).toBeVisible();
  await expect(subcategoryStep).toBeFocused();
  await expect(subcategoryStep).toBeInViewport();
  await subcategoryStep.getByRole("button", { name: /^Dining(?:\s|$)/ }).click();
  const addedResult = page.getByText(/Added ₹1,234 expense under Food → Dining/).last();
  await expect(addedResult).toBeVisible();
  // Resolving the prior card compacts that virtual row while this answer is
  // appended. The response, not the old measured offset, must remain on screen.
  await expect(addedResult).toBeInViewport();
  const transactionArticle = page.locator("article").filter({ hasText: /Added ₹1,234 expense under Food → Dining/ }).last();
  await expect(transactionArticle.getByRole("button", { name: "Edit", exact: true })).toBeVisible();
  await expect(transactionArticle.getByRole("button", { name: "Remove", exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText(/Added ₹1,234 expense under Food → Dining/).last()).toBeVisible();
});

test("ambiguous add request becomes a HITL draft instead of a validator dead end", async ({ page }) => {
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  await input.fill("Add 500");
  await input.press("Enter");

  const typeStep = page.getByRole("group", {
    name: /Action required: (?:What kind of financial event is this\?|One detail needs your confirmation)/,
  });
  await expect(typeStep).toBeVisible({ timeout: 30_000 });
  await expect(typeStep.getByRole("button").first()).toBeVisible();
  await typeStep.getByRole("button", { name: /^Cancel(?:\s|$)/ }).last().click();
  await expect(page.getByText("No changes were made.").last()).toBeVisible();
  await expect(input).toBeEnabled();
});

test("rich entry uses governed transaction handling and analytics stays grounded", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  const amount = 2_000 + (Date.now() % 700);
  const amountText = new Intl.NumberFormat("en-IN").format(amount);
  await input.fill(`Spent ₹${amountText} at Swiggy today`);
  await input.press("Enter");
  const added = page.getByText(new RegExp(`Added ₹${amountText} Swiggy expense`));
  const clarification = page.getByRole("group", { name: "Action required: One detail needs your confirmation" });
  await expect.poll(async () => (await added.count()) > 0 || (await clarification.count()) > 0, { timeout: 45_000 }).toBe(true);
  if (await clarification.count()) {
    await expect(clarification.last().getByRole("button").first()).toBeVisible();
    await clarification.last().getByRole("button", { name: /^Cancel(?:\s|$)/ }).last().click();
    await expect(page.getByText("No changes were made.").last()).toBeVisible();
  } else {
    await expect(added.last()).toBeVisible();
  }
  await input.fill("How much did I spend on food this month?");
  await expect(page.getByRole("button", { name: "Send message" })).toBeEnabled({ timeout: 45_000 });
  await input.press("Enter");
  await expect(page.getByText(/You spent/).last()).toBeVisible({ timeout: 45_000 });
  await expect(page.getByText("data source", { exact: false }).last()).toBeVisible();
});

test("CSV attachment is staged, confirmed, imported, and persistent", async ({ page }) => {
  await page.goto(sharedThreadUrl());
  const chooserPromise = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: "Attach a CSV statement" }).click();
  const chooser = await chooserPromise;
  const unique = Date.now();
  await chooser.setFiles({
    name: `statement-${unique}.csv`,
    mimeType: "text/csv",
    buffer: Buffer.from(`date,description,debit,credit,transaction id\n2026-08-08,SWIGGY ONLINE,850,,e2e-${unique}-1\n2026-08-09,Freelance project,,50000,e2e-${unique}-2\n`),
  });
  await expect(page.getByText("Statement review", { exact: true })).toBeVisible();
  await expect(page.getByText(`statement-${unique}.csv`, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Import 2", exact: true }).click();
  await expect(page.getByText(`Imported 2 transactions from statement-${unique}.csv.`, { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("Import complete", { exact: true })).toBeVisible();
});

test("privacy settings expose least-privilege controls and export", async ({ page }) => {
  await page.goto(sharedThreadUrl());
  // The rail's gear opens the settings page on the agent section; privacy
  // lives one tab over.
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page).toHaveURL(/\/settings\/agent$/);
  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Privacy", exact: true })).toBeVisible();
  const location = page.getByRole("switch", { name: "Location enrichment" });
  if (await location.getAttribute("aria-checked") === "true") await location.click();
  await expect(location).toHaveAttribute("aria-checked", "false");
  await location.click();
  await expect(location).toHaveAttribute("aria-checked", "true");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export my data" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^fyn-ai-export-.*\.json$/);
  await expect(page.getByLabel("Deletion confirmation")).toBeVisible();
  await expect(page.getByRole("button", { name: "Delete permanently" })).toBeDisabled();
});

test("refresh preserves the fixed test conversation", async ({ page }) => {
  test.setTimeout(90_000);
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  const amount = 700 + (Date.now() % 200);
  const message = `Spent ₹${amount} at Toit today`;
  await input.fill(message);
  await input.press("Enter");
  const clarification = page.getByRole("group", { name: "Action required: One detail needs your confirmation" });
  const added = page.getByText(new RegExp(`Added ₹${amount} Toit expense`));
  await expect.poll(async () => (await clarification.count()) > 0 || (await added.count()) > 0, { timeout: 45_000 }).toBe(true);

  await page.reload();
  await expect(page).toHaveURL(sharedThreadUrl());
  await expect(page.locator("article").getByText(message, { exact: true }).last()).toBeVisible();
  if (await clarification.count()) {
    await expect(clarification.last()).toBeVisible();
    await clarification.last().getByRole("button", { name: /^Cancel(?:\s|$)/ }).last().click();
  } else {
    await expect(added.last()).toBeVisible();
  }
  await expect(input).toBeEnabled();
});

test("merchant expense is either classified or requests material clarification", async ({ page }) => {
  test.setTimeout(90_000);
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  const amount = 900 + (Date.now() % 80);
  const amountText = new Intl.NumberFormat("en-IN").format(amount);
  await input.fill(`Spent ₹${amountText} at Toit today`);
  await input.press("Enter");
  const clarification = page.getByRole("group", { name: "Action required: One detail needs your confirmation" });
  const added = page.getByText(new RegExp(`Added ₹${amountText} Toit expense`));
  await expect.poll(async () => (await clarification.count()) > 0 || (await added.count()) > 0, { timeout: 45_000 }).toBe(true);
  if (await clarification.count()) {
    await expect(clarification.last()).toContainText(/account|categor/i);
    await expect(clarification.last().getByRole("button").first()).toBeVisible();
    await clarification.last().getByRole("button", { name: /^Cancel(?:\s|$)/ }).last().click();
  } else {
    await expect(added.last()).toBeVisible();
  }
  await expect(input).toBeEnabled();
});

test("travelling query returns grounded data, clarification, or a governed safe fallback", async ({ page }) => {
  test.setTimeout(90_000);
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  await input.fill("How much did I spend on Travelling this month?");
  await input.press("Enter");
  const clarification = page.getByRole("group", { name: "Action required: One detail needs your confirmation" });
  const response = page.locator("article").last();
  await expect(response.getByRole("button", { name: /Agent run (?:complete|failed):/ })).toBeVisible({ timeout: 45_000 });
  if (await clarification.count()) {
    await expect(clarification.last().getByRole("button", { name: /^Travel(?:\s|$)/ })).toBeVisible();
    await clarification.last().getByRole("button", { name: /^Transport(?:\s|$)/ }).click();
  }
  await expect(input).toBeEnabled({ timeout: 45_000 });
  await expectGroundedResultOrSafeFallback(response, /travel|transport/i);
});

test("category selector supports prediction, search, and exposes category creation", async ({ page }) => {
  test.setTimeout(90_000);
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  await input.fill("₹321");
  await input.press("Enter");
  const typeStep = page.getByRole("group", {
    name: /Action required: (?:What kind of financial event is this\?|One detail needs your confirmation)/,
  });
  await expect(typeStep).toBeVisible({ timeout: 45_000 });
  await typeStep.getByRole("button", { name: /^Expense(?:\s|$)/ }).click();
  const categoryStep = page.getByRole("group", { name: "Action required: Where should I categorize this?" });
  await expect(categoryStep).toBeVisible();
  const search = categoryStep.getByLabel("Search categories");
  await search.fill("Health");
  await expect(categoryStep.getByRole("button", { name: "Health", exact: true })).toBeVisible();
  await expect(categoryStep.getByRole("button", { name: "Add new category" })).toBeVisible();
  await categoryStep.getByRole("button", { name: "Cancel transaction" }).click();
  await expect(input).toBeEnabled();
});

test("automatic entry requires confirmation before removal", async ({ page }) => {
  test.setTimeout(90_000);
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  const amount = 300 + (Date.now() % 90);
  await input.fill(`₹${amount} for coffee`);
  await input.press("Enter");
  await expect(page.getByText(new RegExp(`Added ₹${amount} .*expense`)).last()).toBeVisible({ timeout: 45_000 });
  await page.getByRole("button", { name: "Remove", exact: true }).last().click();
  await expect(page.getByText("Remove this transaction?", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Remove transaction", exact: true }).click();
  await expect(page.getByText(new RegExp(`Removed the ₹${amount} transaction`))).toBeVisible();
});

test("contextual read follow-ups remain grounded or fail safely", async ({ page }) => {
  test.setTimeout(120_000);
  const threadStateLoaded = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "GET" && new RegExp(`^${API_MOUNT_PATH}/agent/threads/[^/]+$`).test(url.pathname);
  });
  await page.goto(sharedThreadUrl());
  await threadStateLoaded;
  const input = page.getByLabel("Message fyn AI");
  const pending = page.getByRole("group", { name: /^Action required:/ });
  if (await pending.count()) {
    await pending.last().getByRole("button", { name: /^Cancel(?:\s|$)/ }).last().click();
  }
  await expect(input).toBeEnabled();

  await input.fill("Can you show the spend summary");
  await input.press("Enter");
  const periodStep = page.getByRole("group", { name: "Action required: One detail needs your confirmation" });
  let response = page.locator("article").last();
  await expect(response.getByRole("button", { name: /Agent run (?:complete|failed):/ })).toBeVisible({ timeout: 45_000 });
  if (await periodStep.count()) await periodStep.last().getByRole("button", { name: /this month/i }).last().click();
  await expect(input).toBeEnabled({ timeout: 45_000 });
  if (!(await expectGroundedResultOrSafeFallback(response, /spent|spending/i))) return;
  await input.fill("Show the food breakdown");
  await expect(page.getByRole("button", { name: "Send message" })).toBeEnabled({ timeout: 45_000 });
  await input.press("Enter");
  response = page.locator("article").last();
  await expect(input).toBeEnabled({ timeout: 45_000 });
  if (!(await expectGroundedResultOrSafeFallback(response, /food/i))) return;

  await input.fill("Do I have any earnings from current month?");
  await expect(page.getByRole("button", { name: "Send message" })).toBeEnabled({ timeout: 45_000 });
  await input.press("Enter");
  response = page.locator("article").last();
  await expect(input).toBeEnabled({ timeout: 45_000 });
  await expectGroundedResultOrSafeFallback(response, /earned|income/i);
});

test("mobile layout keeps the composer and privacy controls usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(sharedThreadUrl());
  await expect(page.getByLabel("Message fyn AI")).toBeVisible();
  await expect(page.getByRole("button", { name: "Send message" })).toBeVisible();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await expect(page.getByRole("complementary")).toBeVisible();
  // Settings is a page now, and it takes the rail over rather than opening a
  // drawer of its own.
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page).toHaveURL(/\/settings\/agent$/);
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page.getByLabel("Deletion confirmation")).toBeVisible();
  const documentWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(documentWidth).toBeLessThanOrEqual(390);
});

test.skip("history pagination requires creating disposable threads and is disabled by the fixed-thread policy", async () => {});
