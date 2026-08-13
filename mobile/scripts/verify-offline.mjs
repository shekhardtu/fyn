/**
 * Drives the app through losing the network and getting it back.
 *
 * The real thing to check is not that a banner appears — it is that the app
 * refuses to send rather than spending its ten-second deadline on a request
 * that cannot work, and that the transcript already fetched stays readable.
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
const TOKEN = process.env.SESSION_TOKEN?.trim();

const log = (...parts) => console.log("·", ...parts);

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
if (TOKEN) {
  await context.addCookies([{ name: "fyn_session", value: TOKEN, domain: "localhost", path: "/", httpOnly: true, sameSite: "Lax" }]);
}
const page = await context.newPage();
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));

try {
  await page.goto(APP, { waitUntil: "networkidle", timeout: 90_000 });
  await page.getByPlaceholder(/Spent/).waitFor({ timeout: 30_000 });
  const online = await page.locator("body").innerText();
  log(`online: banner shown = ${/Offline/.test(online)}`);

  // ── Lose the network ───────────────────────────────────────────────────────
  await context.setOffline(true);
  await page.waitForTimeout(3500);
  await page.screenshot({ path: `${SHOTS}/offline-01.png` });

  const offline = await page.locator("body").innerText();
  log(`offline: banner shown = ${/Offline/.test(offline)}`);
  log(`offline: transcript still readable = ${offline.length > 200}`);

  // Sending must refuse immediately, not hang on the request deadline.
  await page.getByPlaceholder(/Spent/).fill("Spent 250 on coffee");
  const started = Date.now();
  await page.getByLabel("Send").click();
  await page.waitForTimeout(1500);
  const elapsed = Date.now() - started;
  const refusal = await page.locator("body").innerText();
  const refused = /offline/i.test(refusal) && /send it when/i.test(refusal);
  log(`offline: send refused in ${elapsed}ms, message kept = ${refused}`);
  await page.screenshot({ path: `${SHOTS}/offline-02.png` });

  // ── Get it back ────────────────────────────────────────────────────────────
  await context.setOffline(false);
  await page.waitForTimeout(4000);
  const back = await page.locator("body").innerText();
  log(`back online: banner cleared = ${!/Offline\n/.test(back)}`);
  await page.screenshot({ path: `${SHOTS}/offline-03.png` });
} catch (error) {
  log(`FAILED: ${error.message}`);
  await page.screenshot({ path: `${SHOTS}/offline-99.png` });
  process.exitCode = 1;
} finally {
  console.log(errors.length ? `\npage errors:\n  ${[...new Set(errors)].slice(0, 5).join("\n  ")}` : "\nno page errors");
  await browser.close();
}
