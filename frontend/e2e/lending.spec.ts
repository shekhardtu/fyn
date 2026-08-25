import { expect, test } from "@playwright/test";

import { API_MOUNT_PATH } from "@/config/api-path";

const COUNTERPARTY = "Browser lending fixture";

test("new plans begin with an email-or-phone identity before financial details", async ({ page }) => {
  await page.goto("/loans");
  await page.getByRole("button", { name: "New plan", exact: true }).click();

  const dialog = page.getByRole("dialog", { name: "Create a shared plan" });
  const email = dialog.getByRole("combobox", { name: /Email address/ });
  await expect(email).toBeFocused();
  await expect(dialog.getByRole("tab", { name: "Email" })).toHaveAttribute("aria-selected", "true");
  await expect(dialog.locator("input").first()).toHaveAttribute("type", "email");

  await dialog.getByRole("tab", { name: "Phone" }).click();
  await expect(dialog.getByRole("combobox", { name: /Phone number/ })).toBeVisible();
  await expect(dialog.getByText(/Partial suggestions only include people you have already shared a record with/)).toBeVisible();
});

test("a shared lending record is visible from portfolio through document evidence", async ({ page }) => {
  const listed = await page.request.get(`${API_MOUNT_PATH}/loan-agreements`);
  expect(listed.ok(), await listed.text()).toBeTruthy();

  const loans = (await listed.json()) as { items: { id: string; counterpartyName: string }[] };
  let loan = loans.items.find((item) => item.counterpartyName === COUNTERPARTY);

  if (!loan) {
    const created = await page.request.post(`${API_MOUNT_PATH}/loan-agreements`, {
      headers: { "Idempotency-Key": "playwright-personal-lending-fixture-v1" },
      data: {
        direction: "lent",
        counterpartyName: COUNTERPARTY,
        inviteChannel: "email",
        inviteValue: "browser-lending-fixture@example.test",
        principalMinor: 125000,
        currency: "INR",
        moneyDate: "2026-08-24",
        dueDate: "2027-08-24",
        annualRateBps: 300,
        note: "Repeatable browser evidence fixture",
        securityItems: [{
          kind: "post_dated_cheque",
          description: "Cheque held until both people confirm closure",
          maskedIdentifier: "ending 4821",
          statedValueMinor: 125000,
        }],
      },
    });
    expect(created.ok(), await created.text()).toBeTruthy();
    const payload = await created.json() as { loan: { id: string; counterpartyName: string } };
    loan = payload.loan;
  }

  await page.goto("/loans");
  await expect(page.getByRole("heading", { name: "Personal lending" })).toBeVisible();
  await expect(page.getByText(COUNTERPARTY, { exact: true })).toBeVisible();

  await page.getByText(COUNTERPARTY, { exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/loans/${loan.id}$`));
  await expect(page.getByRole("heading", { name: "Shared repayment plan" })).toBeVisible();
  await expect(page.getByText("Content fingerprint", { exact: false })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Assurance and return record" })).toBeVisible();
  await expect(page.getByText("ending 4821", { exact: false })).toBeVisible();
  await expect(page.getByText("Fyn records what both people agreed", { exact: false })).toBeVisible();
});
