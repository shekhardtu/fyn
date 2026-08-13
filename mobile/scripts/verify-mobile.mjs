/**
 * Drives the Expo app in a phone-sized viewport against the live backend.
 *
 * This is not a substitute for the simulator — it cannot exercise the Keychain,
 * haptics, or the native list — but it does exercise everything that would
 * break first: the module graph at runtime, the contracts, the SSE stream, the
 * HITL round trip, and the widgets the analytics path returns.
 */
import { createRequire } from "node:module";

/** Chromium comes from the web app's devDependencies, which are already
 *  installed for its own end-to-end suite, so this package stays free of a
 *  second browser download. */
const chromium = (() => {
  const require = createRequire(import.meta.url);
  for (const candidate of ["playwright", "@playwright/test", "../../frontend/node_modules/@playwright/test"]) {
    try { return require(candidate).chromium; } catch { continue; }
  }
  throw new Error("Playwright not found. Install the web app's devDependencies first.");
})();

const APP = process.env.APP_URL ?? "http://localhost:8082";
const SHOTS = process.env.SHOT_DIR ?? ".";
const RUN_TURNS = process.env.TURNS !== "0";

const log = (...parts) => console.log("·", ...parts);

const browser = await chromium.launch();
const SCHEME = process.env.SCHEME === "dark" ? "dark" : "light";

const context = await browser.newContext({
  colorScheme: SCHEME,
  viewport: { width: 390, height: 844 },
  deviceScaleFactor: 2,
  isMobile: true,
  hasTouch: true,
});
const page = await context.newPage();

/**
 * Reuse a session where one is supplied.
 *
 * Signing in on every run burns through the OTP resend window and the server
 * correctly starts answering 429. `SESSION_TOKEN` lets a run skip straight to
 * the workspace; the web target reads its session from the cookie, so that is
 * where the token goes.
 */
const REUSED = process.env.SESSION_TOKEN?.trim();
if (REUSED) {
  await context.addCookies([{
    name: "fyn_session",
    value: REUSED,
    domain: "localhost",
    path: "/",
    httpOnly: true,
    sameSite: "Lax",
  }]);
  log("reusing an existing session");
}

const errors = [];
page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
page.on("console", (message) => {
  if (message.type() === "error") errors.push(`console: ${message.text().slice(0, 300)}`);
});

async function shot(name) {
  const suffix = SCHEME === "dark" ? "-dark" : "";
  await page.screenshot({ path: `${SHOTS}/${name}${suffix}.png` });
  log(`shot ${name}${suffix}.png`);
}

/** Sends one message and waits for the run to land. */
async function turn(text, name, waitMs = 120_000) {
  const composer = page.getByPlaceholder(/Spent/);
  await composer.waitFor({ timeout: 30_000 });
  await composer.fill(text);
  await page.getByLabel("Send").click();
  log(`sent: ${text}`);
  // The run is over when the activity trace stops claiming to be working.
  await page
    .getByLabel("fyn AI is working on your message")
    .waitFor({ state: "detached", timeout: waitMs })
    .catch(() => log("  (still running at the deadline)"));
  await page.waitForTimeout(2500);
  await shot(name);
}

try {
  log(`opening ${APP}`);
  await page.goto(APP, { waitUntil: "networkidle", timeout: 90_000 });
  await page.waitForTimeout(2500);

  // ── Sign in ────────────────────────────────────────────────────────────────
  if (REUSED) {
    await shot("01-signed-in");
  } else {
  const emailTab = page.getByText("Email", { exact: true });
  if (await emailTab.count()) await emailTab.first().click();

  const identifier = page.getByPlaceholder("you@example.com");
  await identifier.waitFor({ timeout: 15_000 });
  await identifier.fill("hari@tryloop.ai");
  await page.getByText("Send code", { exact: true }).click();
  log("requested code");

  // OTP_DEBUG_ECHO is on in development, so the code comes back on screen and
  // the run does not need a mailbox.
  const fill = page.getByText(/Development code/);
  await fill.waitFor({ timeout: 30_000 });
  await fill.click();
  await page.getByText("Sign in", { exact: true }).click();
  await page.waitForTimeout(6000);
  await shot("01-signed-in");
  }

  await page.getByPlaceholder(/Spent/).waitFor({ timeout: 30_000 });
  log("workspace reached");

  // A fresh thread, so the run is reading its own output rather than the
  // wreckage of every previous run.
  if (process.env.FRESH === "1") {
    await page.getByLabel("Start a new conversation").click();
    await page.getByPlaceholder(/Spent/).waitFor({ timeout: 30_000 });
    await page.waitForTimeout(2000);
    log("started a fresh conversation");
  }

  if (RUN_TURNS) {
    // ── A HITL round trip ────────────────────────────────────────────────────
    // A bare amount is the deliberately ambiguous path: the harness must come
    // back asking rather than guessing, which is the clearest HITL surface.
    await turn("2500", "02-bare-amount", 120_000);

    // Answer whichever clarification came back. The point is not which one it
    // is — it is that pressing it submits, locks the card, and leaves a receipt.
    // Every spent control in the transcript still reads "Food"; only the live
    // one is pressable, so the test picks by that rather than by position.
    const candidates = page.getByText("Food", { exact: true });
    const total = await candidates.count();
    let choice = null;
    for (let index = total - 1; index >= 0; index -= 1) {
      const candidate = candidates.nth(index);
      if (await candidate.isEnabled().catch(() => false)) { choice = candidate; break; }
    }
    if (choice) {
      await choice.scrollIntoViewIfNeeded();
      await choice.click({ timeout: 30_000 });
      log(`answered the clarification (${total} candidates, took the live one)`);
      await page.waitForTimeout(18_000);
      await shot("03-after-hitl");

      // The spent control must be read-only now, and say so.
      const receipt = await page.getByText(/^(Chosen ·|Done|Cancelled)/).count();
      log(receipt ? "receipt rendered on the spent control" : "NO receipt on the spent control");
    } else {
      log("no clarification surfaced for the bare amount");
    }

    // ── Analytics: table and chart widgets ───────────────────────────────────
    await turn("Show me my spending summary for this month", "04-analysis", 120_000);
    await turn("Show me a chart of my spending by category this month", "04b-chart", 150_000);
    await turn("List my largest expenses", "04c-table", 150_000);

    // ── The pushed screens ───────────────────────────────────────────────────
    await page.getByLabel("All conversations").click();
    await page.waitForTimeout(2500);
    await shot("05-conversations");

    await page.getByLabel("Back to the conversation").click();
    await page.waitForTimeout(1500);

    await page.getByLabel("Settings").click();
    await page.waitForTimeout(2500);
    await shot("06-settings");

    // ── The money screens ────────────────────────────────────────────────────
    await page.getByText("This month’s overview").click();
    await page.waitForTimeout(4000);
    await shot("07-overview");
    // The pushed screen keeps the one underneath it mounted, so both headers
    // carry a Back button; the topmost is the live one.
    await page.getByLabel("Back").last().click();
    await page.waitForTimeout(1500);

    await page.getByText("All transactions").click();
    await page.waitForTimeout(4000);
    await shot("08-transactions");
  }

  const body = await page.locator("body").innerText();
  log("--- transcript tail ---");
  console.log(body.slice(-1800));
} catch (error) {
  log(`FAILED: ${error.message}`);
  await shot("99-failure");
  process.exitCode = 1;
} finally {
  const unique = [...new Set(errors)];
  if (unique.length) {
    console.log("\n--- page errors ---");
    unique.slice(0, 25).forEach((entry) => console.log("  !", entry));
  } else {
    console.log("\nno page errors");
  }
  await browser.close();
}
