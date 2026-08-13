/**
 * Draws the app icon set from the design tokens.
 *
 * The icons are generated rather than hand-exported so the mark can never drift
 * from the palette the app actually renders with — the indigo here is the same
 * `secondary` the composer's send button wears. Rendered through headless
 * Chromium, which is already present for the verification run, so this adds no
 * image toolchain to the project.
 *
 *   node scripts/make-icons.mjs
 */
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const assets = resolve(here, "../assets");

/** Chromium is already installed for the verification run in the web app, so
 *  this borrows it rather than adding a second copy to this package. */
function loadChromium() {
  const require = createRequire(import.meta.url);
  for (const candidate of ["playwright", "@playwright/test", "../../frontend/node_modules/@playwright/test"]) {
    try {
      return require(candidate).chromium;
    } catch {
      continue;
    }
  }
  throw new Error("Playwright not found. Run this from a checkout that has the web app's devDependencies installed.");
}

const chromium = loadChromium();

const INDIGO = "#4340e0";
const INDIGO_DEEP = "#3733c4";
const WHITE = "#ffffff";

/**
 * The mark: a rupee glyph cut by a rising stroke.
 *
 * It has to read at 40px on a home screen, so it is one shape and one accent —
 * a wordmark would be illegible and a chart of bars would look like every other
 * finance app. The rising stroke is the only nod to analysis; the ₹ says whose
 * money this is.
 */
function mark({ size, inset, background }) {
  const glyph = size * 0.52;
  return `
    <svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
      <defs>
        <linearGradient id="ground" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${INDIGO}"/>
          <stop offset="100%" stop-color="${INDIGO_DEEP}"/>
        </linearGradient>
      </defs>
      ${background ? `<rect width="${size}" height="${size}" fill="url(#ground)"/>` : ""}
      <g transform="translate(${size / 2}, ${size / 2 + inset})">
        <text
          x="0" y="0"
          font-family="Helvetica Neue, Helvetica, Arial, sans-serif"
          font-size="${glyph}"
          font-weight="600"
          fill="${WHITE}"
          text-anchor="middle"
          dominant-baseline="central"
        >₹</text>
      </g>
      <path
        d="M ${size * 0.26} ${size * 0.74} L ${size * 0.42} ${size * 0.62} L ${size * 0.54} ${size * 0.68} L ${size * 0.76} ${size * 0.42}"
        fill="none"
        stroke="${WHITE}"
        stroke-opacity="0.55"
        stroke-width="${size * 0.045}"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    </svg>`;
}

const TARGETS = [
  // Full bleed: iOS applies its own corner mask, so the square must reach the edge.
  { file: "icon.png", size: 1024, inset: -40, background: true },
  { file: "splash-icon.png", size: 512, inset: -20, background: false, transparent: true },
  { file: "favicon.png", size: 96, inset: -4, background: true },
  // Android's adaptive foreground is cropped to a circle at worst, so the mark
  // sits inside the safe zone with the ground supplied separately.
  { file: "android-icon-foreground.png", size: 1024, inset: -30, background: false, transparent: true, scale: 0.62 },
  { file: "android-icon-monochrome.png", size: 1024, inset: -30, background: false, transparent: true, scale: 0.62 },
];

const browser = await chromium.launch();
await mkdir(assets, { recursive: true });

for (const target of TARGETS) {
  const page = await browser.newPage({
    viewport: { width: target.size, height: target.size },
    deviceScaleFactor: 1,
  });
  const inner = mark({ size: target.size, inset: target.inset, background: target.background });
  const scaled = target.scale
    ? `<div style="transform:scale(${target.scale});transform-origin:center">${inner}</div>`
    : inner;
  await page.setContent(
    `<body style="margin:0;width:${target.size}px;height:${target.size}px;display:grid;place-items:center;background:${target.transparent ? "transparent" : INDIGO}">${scaled}</body>`,
  );
  await page.screenshot({ path: resolve(assets, target.file), omitBackground: Boolean(target.transparent) });
  await page.close();
  console.log(`wrote assets/${target.file} (${target.size}px)`);
}

// The adaptive background is a flat plate; Android composites the foreground on top.
const plate = await browser.newPage({ viewport: { width: 1024, height: 1024 }, deviceScaleFactor: 1 });
await plate.setContent(`<body style="margin:0;width:1024px;height:1024px;background:${INDIGO}"></body>`);
await plate.screenshot({ path: resolve(assets, "android-icon-background.png") });
await plate.close();
console.log("wrote assets/android-icon-background.png (1024px)");

await browser.close();
