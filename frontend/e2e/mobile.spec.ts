import { expect, type Page, test } from "@playwright/test";
import { sharedThreadUrl } from "./test-thread";

/** The phone contract: one column, a drawer for navigation, and no page-level
 *  sideways scroll — the layout rules that desktop testing never touches. */

/**
 * Puts content on the page that *will* pan it sideways unless the containment
 * rules hold: a table far wider than any phone, and a token with nowhere to
 * break.
 *
 * Without this the sideways-scroll assertions are vacuous. They measure a page
 * that happens to contain only ordinary prose, so they pass whether or not
 * `overflow-x: clip` and `overflow-wrap: anywhere` are there at all — which is
 * exactly how they passed throughout the pan bug they exist to catch. Supplying
 * the hostile content is what makes the measurement mean something.
 *
 * Returns the probe's natural width so the caller can assert the probe really
 * is wider than the viewport, and the test cannot quietly go vacuous again.
 */
async function addOverflowProbe(page: Page, container: string) {
  const probe = await page.evaluate((selector) => {
    const host = document.querySelector(selector) ?? document.body;
    const block = document.createElement("div");
    block.id = "overflow-probe";

    // A width no phone has, and one that cannot be negotiated away. A table
    // built from cells was the first attempt and it shrank itself to 372px on
    // a 412px screen, proving nothing — the point is a child that stays wider
    // than the viewport, which is what a real chart or a nowrap table is.
    const wide = document.createElement("div");
    wide.style.cssText = "width:1200px;height:8px";

    const token = document.createElement("p");
    token.textContent = "unbreakabletoken".repeat(30);

    block.append(wide, token);
    host.appendChild(block);
    return {
      natural: wide.getBoundingClientRect().width,
      tokenOverflow: token.scrollWidth - token.clientWidth,
      viewport: document.documentElement.clientWidth,
    };
  }, container);
  // If this ever fails the probe stopped being hostile, and every assertion
  // that leans on it stopped testing anything.
  expect(probe.natural, "the probe must be wider than the phone to prove anything").toBeGreaterThan(probe.viewport);
  expect(probe.tokenOverflow, "a long token must fold rather than widen its column").toBeLessThanOrEqual(1);
  return probe;
}

/**
 * No ancestor of the over-wide content may be something the user can pan.
 *
 * Checking only `document.documentElement` is not enough, and that is the trap
 * the earlier version fell into: the shell clips at three levels, so the page
 * never overflows no matter what the transcript does. The bug was one level
 * down — a container holding content wider than itself and handing every
 * horizontal drag to the user, so the whole transcript slid instead of the
 * table scrolling inside its own box.
 *
 * `hidden` and `clip` contain the overflow and are correct. `auto` and
 * `scroll` are pannable. `visible` is worse still: the overflow escapes to the
 * next ancestor and becomes someone else's problem. `.table-scroll` is the one
 * exemption — it is the box a wide table is *meant* to scroll in.
 */
async function expectNothingPannable(page: Page) {
  const offenders = await page.evaluate(() => {
    const found: string[] = [];
    for (let node = document.getElementById("overflow-probe")?.parentElement; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (node.scrollWidth - node.clientWidth <= 1) continue;
      if (node.classList.contains("table-scroll")) continue;
      if (style.overflowX === "hidden" || style.overflowX === "clip") continue;
      found.push(`${node.tagName}.${String(node.className).split(" ")[0]} overflow-x:${style.overflowX} (${node.scrollWidth} in ${node.clientWidth})`);
    }
    return found;
  });
  expect(offenders, "content wider than the phone must be contained, never pannable").toEqual([]);

  const pageOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(pageOverflow, "page-level horizontal overflow").toBeLessThanOrEqual(1);
}

test("the ledger fits a phone and navigation folds into a drawer", async ({ page }) => {
  await page.goto("/transactions");
  await expect(page.getByRole("heading", { name: "Recent transactions" })).toBeVisible();

  // Wide content must scroll inside its own container, never the page.
  await addOverflowProbe(page, ".panel-scroll, main");
  await expectNothingPannable(page);

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
  // The transcript is where the bug appeared: a wide table in one answer made
  // every horizontal drag slide the whole app instead of the table.
  await addOverflowProbe(page, ".conversation-scroll");
  await expectNothingPannable(page);
});

/**
 * The composer belongs to the bottom edge of what the reader can see, and on a
 * phone that edge is not the bottom of the viewport. A software keyboard slides
 * over the page on iOS without resizing anything, and the browser pans the page
 * up to reveal the field it just covered — a pan it often forgets to take back,
 * which is what leaves the composer stranded mid-screen with dead space below.
 *
 * Playwright cannot raise a real keyboard, so the two halves are exercised
 * separately: a browser that resizes its own layout viewport (Android Chrome,
 * Safari 26 with interactive-widget=resizes-content), and the iOS shape, where
 * the visible rectangle is published as `--app-height` over a `--viewport-offset`
 * pan. In both the dock must finish exactly on the visible bottom edge.
 */
test("the composer holds the bottom edge of what the phone can see", async ({ page }) => {
  await page.goto(sharedThreadUrl());
  await expect(page.getByRole("textbox").first()).toBeVisible();
  const dock = page.locator(".entry-dock");
  await expect(dock).toBeVisible();

  const visibleBottom = () => page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    const offset = Number.parseFloat(root.getPropertyValue("--viewport-offset")) || 0;
    const height = Number.parseFloat(root.getPropertyValue("--app-height")) || window.innerHeight;
    return {
      edge: offset + height,
      dock: document.querySelector(".entry-dock")!.getBoundingClientRect().bottom,
      // A document that can scroll is a document the browser can leave
      // scrolled, which is the stranded composer by another route.
      pageScroll: document.documentElement.scrollHeight - window.innerHeight,
    };
  });

  const resting = await visibleBottom();
  expect(resting.dock).toBeCloseTo(resting.edge, 0);
  expect(resting.pageScroll).toBeLessThanOrEqual(1);

  // The layout viewport shrinking under the app, which is what a keyboard does
  // on Android Chrome and on Safari 26.
  await page.setViewportSize({ width: 412, height: 460 });
  const resized = await visibleBottom();
  expect(resized.edge).toBeCloseTo(460, 0);
  expect(resized.dock).toBeCloseTo(resized.edge, 0);
  expect(resized.pageScroll).toBeLessThanOrEqual(1);

  // And the iOS shape: the visible rectangle is a short window panned down a
  // layout viewport that never changed size.
  await page.evaluate(() => {
    document.documentElement.style.setProperty("--app-height", "300px");
    document.documentElement.style.setProperty("--viewport-offset", "80px");
  });
  const panned = await visibleBottom();
  expect(panned.edge).toBeCloseTo(380, 0);
  expect(panned.dock).toBeCloseTo(380, 0);
});

test("settings borrows the rail and hands it back", async ({ page }) => {
  await page.goto("/settings/agent");
  await expect(page.getByRole("heading", { name: "Agent settings" })).toBeVisible();
  await addOverflowProbe(page, ".panel-scroll, main");
  await expectNothingPannable(page);

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
