/**
 * Captures the manifest's install-dialog screenshots from the running app.
 *
 * Chrome shows a richer install dialog when a manifest carries screenshots,
 * and it wants both form factors: `narrow` for phones, `wide` for desktop.
 * These have to be the real product — a mock-up here would be a promise the
 * app does not keep at the moment someone decides to install it.
 *
 * Reuses the e2e suite's stored session rather than signing in, because
 * sign-in codes are rate limited and this is not worth burning one.
 *
 *   yarn dev  (and the backend)  then  node scripts/make-pwa-screenshots.mjs
 */
import { createRequire } from "node:module";
import { mkdir, readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(here, "../public/screenshots");
const session = resolve(here, "../e2e/.auth/session.json");
const base = process.env.SCREENSHOT_BASE_URL ?? "http://localhost:3000";

const require = createRequire(import.meta.url);
const { chromium } = require("@playwright/test");

// The conversation shot points at the e2e suite's durable thread rather than
// "/", which lands on a fresh empty workspace — a screenshot of an empty state
// is the one thing an install dialog should never show.
const thread = JSON.parse(await readFile(resolve(here, "../e2e/.auth/thread.json"), "utf8"));
if (!thread.id) throw new Error("No shared thread recorded; run `yarn test:e2e` once first.");

const SHOTS = [
  { file: "overview", path: "/overview", label: "Where the month went" },
  { file: "transactions", path: "/transactions", label: "Every entry, searchable" },
  { file: "conversation", path: `/c/${thread.id}`, label: "Ask in plain language" },
];

// Viewports are CSS pixels, and the layout switches on those — so a "narrow"
// shot has to be captured at a phone's CSS width and scaled up by its device
// pixel ratio. Capturing 1080 CSS pixels wide would silently photograph the
// desktop layout and label it the phone one.
const FORM_FACTORS = [
  { name: "narrow", width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
  { name: "wide", width: 1920, height: 1080, deviceScaleFactor: 1, isMobile: false, hasTouch: false },
];

await mkdir(out, { recursive: true });
const browser = await chromium.launch();
const captured = [];

for (const factor of FORM_FACTORS) {
  const context = await browser.newContext({
    storageState: session,
    viewport: { width: factor.width, height: factor.height },
    deviceScaleFactor: factor.deviceScaleFactor,
    isMobile: factor.isMobile,
    hasTouch: factor.hasTouch,
  });
  const page = await context.newPage();
  for (const shot of SHOTS) {
    await page.goto(`${base}${shot.path}`, { waitUntil: "networkidle" });
    // Let the skeletons settle; a screenshot of loading bars sells nothing.
    await page.waitForTimeout(1_500);
    if (page.url().includes("/login")) {
      throw new Error("The stored session is no longer valid — run `yarn test:e2e` once to refresh it, then retry.");
    }
    const file = `${shot.file}-${factor.name}.png`;
    await page.screenshot({ path: resolve(out, file) });
    // `sizes` describes the file, not the viewport it was taken through.
    const pixels = `${factor.width * factor.deviceScaleFactor}x${factor.height * factor.deviceScaleFactor}`;
    captured.push({ src: `/screenshots/${file}`, sizes: pixels, type: "image/png", form_factor: factor.name, label: shot.label });
    console.log(`wrote public/screenshots/${file}`);
  }
  await context.close();
}

await browser.close();
console.log(`\n${JSON.stringify(captured, null, 2)}`);
