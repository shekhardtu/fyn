import { expect, test } from "@playwright/test";
import { sharedThreadUrl } from "./test-thread";

/** The phone contract: one column, a drawer for navigation, and no page-level
 *  sideways scroll — the layout rules that desktop testing never touches. */

test("the ledger fits a phone and navigation folds into a drawer", async ({ page }) => {
  await page.goto("/transactions");
  await expect(page.getByRole("heading", { name: "Recent transactions" })).toBeVisible();

  // Wide content must scroll inside its own container, never the page.
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);

  // The rail is behind the hamburger at this size.
  const openNav = page.getByRole("button", { name: "Open navigation" }).first();
  await expect(openNav).toBeVisible();
  await openNav.click();
  await expect(page.getByRole("navigation", { name: "Money pages" })).toBeVisible();
  await page.getByRole("button", { name: "Close navigation" }).click();
});

test("the conversation composer is present and usable on a phone", async ({ page }) => {
  await page.goto(sharedThreadUrl());
  await expect(page.getByRole("textbox").first()).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});

test("settings borrows the rail and hands it back", async ({ page }) => {
  await page.goto("/settings/agent");
  await expect(page.getByRole("heading", { name: "Agent settings" })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);

  // The boundaries with no switch are on the page, not folded behind anything.
  await expect(page.getByRole("heading", { name: "Fixed, whatever you choose" })).toBeVisible();

  // At this size the rail is the drawer, and while settings is open it indexes
  // settings — the conversation list is not also in there competing.
  await page.getByRole("button", { name: "Open navigation" }).first().click();
  const rail = page.getByRole("navigation", { name: "Settings sections" });
  await expect(rail).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Money pages" })).toBeHidden();

  await rail.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page).toHaveURL(/\/settings\/app$/);
  await expect(page.getByRole("heading", { name: "Appearance" })).toBeVisible();

  // And leaving gives the rail back to the workspace.
  await page.getByRole("button", { name: "Open navigation" }).first().click();
  await page.getByRole("button", { name: "Back to your workspace" }).click();
  await expect(page.getByRole("navigation", { name: "Settings sections" })).toBeHidden();
});
