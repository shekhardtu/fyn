import { expect, test } from "@playwright/test";
import { sharedThreadUrl } from "./test-thread";

test("conversation URLs are shareable and support browser navigation", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: "http://localhost:3000" });
  await page.goto(sharedThreadUrl());
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
  await input.fill("How much did I spend in the last two days?");
  await input.press("Enter");
  // The run is expanded while it streams, then folds to a summary line once the
  // answer lands — reopening it has to bring the whole trace back.
  await expect(page.getByText("fyn AI is working").last()).toBeVisible({ timeout: 30_000 });
  const finished = page.getByRole("button", { name: /Worked for .* step/ });
  await expect(finished.last()).toBeVisible({ timeout: 30_000 });
  await finished.last().click();
  await expect(page.getByText("AG-UI agent run").last()).toBeVisible();
  await expect(page.getByText(/search_transactions|get_spending_summary|calculate_affordability/).last()).toBeVisible();
  // The validator runs on every Agno route; whether it accepts, rejects and
  // reroutes, or falls through to the deterministic path is the model's call,
  // so the step is the invariant and its verdict is not.
  await expect(page.getByText("agno_validator").last()).toBeVisible();
  await expect(page.getByText(/\d+(?:\.\d+)? (?:ms|s) total/).last()).toBeVisible();
  await expect(page.getByText(/^Σ (?:<1|\d+(?:\.\d+)?) (?:ms|s)$/).last()).toBeVisible();
  await page.reload();
  // A reloaded run comes back collapsed: the trace is kept, not foregrounded.
  await expect(page.getByText("AG-UI agent run")).toHaveCount(0);
  await finished.last().click();
  await expect(page.getByText("AG-UI agent run").last()).toBeVisible();
  await expect(page.getByText("agno_validator").last()).toBeVisible();
});

test("bare amount follows clarification, auto-save, edit/remove controls, and refresh persistence", async ({ page }) => {
  await page.goto(sharedThreadUrl());
  await expect(page.getByLabel("Message fyn AI")).toBeVisible();
  const input = page.getByLabel("Message fyn AI");
  await input.fill("₹1,234");
  await input.press("Enter");
  await expect(page.getByText("Where should I categorize this?", { exact: true })).toBeVisible();
  const categoryStep = page.getByRole("group", { name: "Action required: Where should I categorize this?" });
  await expect(categoryStep).toBeFocused();
  await expect(categoryStep).toBeInViewport();
  await page.getByRole("button", { name: "Food", exact: true }).click();
  await expect(page.getByText("What type of food expense?", { exact: true })).toBeVisible();
  const subcategoryStep = page.getByRole("group", { name: "Action required: What type of food expense?" });
  await expect(subcategoryStep).toBeFocused();
  await expect(subcategoryStep).toBeInViewport();
  await page.getByRole("button", { name: "Dining", exact: true }).click();
  await expect(page.getByText(/Added ₹1,234 Dining expense/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Remove", exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText(/Added ₹1,234 Dining expense/)).toBeVisible();
});

test("ambiguous add request becomes a HITL draft instead of a validator dead end", async ({ page }) => {
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  await input.fill("Add 500");
  await input.press("Enter");

  await expect(page.getByText("What kind of financial event is this?", { exact: true })).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "Expense", exact: true }).click();
  await page.getByRole("button", { name: "Food", exact: true }).click();
  await page.getByRole("button", { name: "Dining", exact: true }).click();
  await expect(page.getByText(/Added ₹500 Dining expense/)).toBeVisible();
});

test("rich entry and grounded analytics work without a form", async ({ page }) => {
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  await input.fill("Spent ₹2,345 at Swiggy today");
  await input.press("Enter");
  await expect(page.getByText("Food → Delivery", { exact: false })).toBeVisible();
  await expect(page.getByText(/Added ₹2,345 Swiggy expense/)).toBeVisible();
  await input.fill("How much did I spend on food this month?");
  await input.press("Enter");
  await expect(page.getByRole("heading", { name: "Food · This month" })).toBeVisible();
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
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Privacy & data" })).toBeVisible();
  const location = page.getByRole("switch");
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
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  await input.fill("Spent ₹777 at Toit today");
  await input.press("Enter");
  await expect(page.getByText("₹777", { exact: true }).last()).toBeVisible();

  await page.reload();
  await expect(page).toHaveURL(sharedThreadUrl());
  await expect(page.locator("article").getByText("Spent ₹777 at Toit today", { exact: true })).toBeVisible();
});

test("merchant recategorization can be saved and is learned", async ({ page }) => {
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  await input.fill("Spent ₹900 at Toit today");
  await input.press("Enter");
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByLabel("Transaction category").selectOption({ label: "Entertainment" });
  await page.getByLabel("Transaction subcategory").selectOption({ label: "Events" });
  await page.getByRole("button", { name: "Apply changes" }).click();
  await input.fill("Paid ₹1,100 at Toit today");
  await input.press("Enter");
  await expect(page.getByText("Entertainment → Events", { exact: false }).last()).toBeVisible();
});

test("travelling query is category-aware instead of returning the generic template", async ({ page }) => {
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  await input.fill("Spent ₹1,000 on a cab today");
  await input.press("Enter");
  await expect(page.getByText("Transport → Cab", { exact: false })).toBeVisible();
  await input.fill("Spent ₹500 on fuel today");
  await input.press("Enter");
  await expect(page.getByText("Transport → Fuel", { exact: false })).toBeVisible();
  await input.fill("How much did I spend on Travelling this month?");
  await input.press("Enter");
  await expect(page.getByRole("heading", { name: "Transport · This month" })).toBeVisible();
  await expect(page.getByText("Cab", { exact: true }).last()).toBeVisible();
  await expect(page.getByText("Fuel", { exact: true }).last()).toBeVisible();
});

test("category selector supports prediction, search, and adding a category", async ({ page }) => {
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  await input.fill("₹321");
  await input.press("Enter");
  await expect(page.getByText("Best guesses", { exact: true })).toBeVisible();
  const search = page.getByLabel("Search categories");
  await search.fill("Health");
  await expect(page.getByRole("button", { name: "Health", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Add new category" }).click();
  const categoryName = `Pets ${Date.now()}`;
  await page.getByLabel("New category name").fill(categoryName);
  await page.getByRole("button", { name: "Add category" }).click();
  await expect(page.getByText(`${categoryName} → Other`, { exact: false })).toBeVisible();
});

test("automatic entry requires confirmation before removal", async ({ page }) => {
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  await input.fill("₹250 for coffee");
  await input.press("Enter");
  await page.getByRole("button", { name: "Remove", exact: true }).click();
  await expect(page.getByText("Remove this transaction?", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Remove transaction", exact: true }).click();
  await expect(page.getByText(/Removed the ₹250 transaction/)).toBeVisible();
});

test("contextual breakdown and merchant removal choose the correct tools", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto(sharedThreadUrl());
  const input = page.getByLabel("Message fyn AI");
  await expect(input).toBeEnabled();

  await input.fill("Can you show the spend summary");
  await input.press("Enter");
  await expect(page.getByRole("heading", { name: "Spending · This month" })).toBeVisible();
  await input.fill("Show the food breakdown");
  await input.press("Enter");
  await expect(page.getByRole("heading", { name: "Food · This month" })).toBeVisible();
  await expect(page.getByText("More information needed", { exact: true })).not.toBeVisible();

  await input.fill("Do I have any earnings from current month?");
  await input.press("Enter");
  await expect(page.getByText(/You earned ₹[\d,]+ across \d+ transactions?\./).last()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "Income" }).last()).toBeVisible();

  const merchant = `RemovalCafe${Date.now()}`;
  await input.fill(`Spent ₹654 at ${merchant} today`);
  await input.press("Enter");
  await expect(page.getByText(new RegExp(`Added ₹654 ${merchant} expense`))).toBeVisible();
  await input.fill(`Spent ₹765 at ${merchant} today`);
  await input.press("Enter");
  await expect(page.getByText(new RegExp(`Added ₹765 ${merchant} expense`))).toBeVisible();
  await input.fill(`Show to all expenses on ${merchant}`);
  await input.press("Enter");
  await expect(page.getByText("Matching transactions", { exact: true })).toBeVisible();
  const matchingCard = page.locator("section").filter({ has: page.getByText("Matching transactions", { exact: true }) });
  await expect(matchingCard.getByRole("button", { name: "Edit", exact: true })).toHaveCount(2);
  await expect(matchingCard.getByRole("button", { name: "Remove", exact: true })).toHaveCount(2);
  await input.fill(`I want to remove the ${merchant} expense from the list`);
  await input.press("Enter");
  await expect(page.getByText("Choose a transaction to remove", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Review removal" })).toHaveCount(2);
  await page.getByRole("button", { name: "Review removal" }).first().click();
  await expect(page.getByText("Remove this transaction?", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByText("Kept the transaction.", { exact: true })).toBeVisible();
});

test("mobile layout keeps the composer and privacy controls usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(sharedThreadUrl());
  await expect(page.getByLabel("Message fyn AI")).toBeVisible();
  await expect(page.getByRole("button", { name: "Send message" })).toBeVisible();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await expect(page.getByRole("complementary")).toBeVisible();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  // The panel is a modal drawer: it traps focus and closes on Escape, so it
  // announces itself as a dialog rather than a plain region.
  const settings = page.getByRole("dialog", { name: "Privacy & data" });
  await expect(settings).toBeVisible();
  // The drawer slides in, and `toBeVisible` resolves the moment it starts. A
  // box measured mid-transform comes back off the compositor's float maths —
  // 390.00003 rather than 390 — so the assertion below was testing floating
  // point rather than layout. Wait for the entrance to settle, then measure.
  await settings.evaluate((panel) => Promise.all(panel.getAnimations().map((animation) => animation.finished)));
  const box = await settings.boundingBox();
  expect(box?.width).toBeLessThanOrEqual(390);
  await expect(page.getByLabel("Deletion confirmation")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(settings).toBeHidden();
});

test.skip("history pagination requires creating disposable threads and is disabled by the fixed-thread policy", async () => {});
