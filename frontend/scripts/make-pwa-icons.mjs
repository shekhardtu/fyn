/**
 * Draws fyn's icon set from one geometry definition.
 *
 * The mark is a ledger entry: the 2px tick that opens every rail row in the
 * app, with the ruled lines it marks. That tick is the product's signature
 * shape — reusing it here means the icon says the same thing the interface
 * does, rather than borrowing the rupee-and-rising-line that every finance app
 * already uses.
 *
 * One flat indigo, no gradient: the design language allows a single
 * interactive colour, and a plate that shades to a second one is a second one.
 *
 * Rendered through the Playwright Chromium already installed for the e2e
 * suite, so this adds no image toolchain.
 *
 *   node scripts/make-pwa-icons.mjs
 */
import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { readFile } from "node:fs/promises";

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(here, "../public/icons");

const require = createRequire(import.meta.url);
const { chromium } = require("@playwright/test");

const INDIGO = "#4340e0";
const WHITE = "#ffffff";

/**
 * The mark, laid out from a single stroke unit so every part stays in
 * proportion and nothing falls below a pixel.
 *
 * Below 48px the second rule is dropped: three strokes plus their gaps cannot
 * survive a 16px favicon, and a mark that turns to mush small is worse than a
 * simpler one that stays legible. `scale` pulls the art into the 80% safe zone
 * for maskable icons, where the platform may crop to a circle.
 */
function mark({ size, scale = 1 }) {
  // One drawing at every size. The stroke thickens below 64px rather than the
  // mark losing a part: an icon that sheds strokes to fit stops being the same
  // mark, and a 16px favicon reduced to a tick and a dash reads as nothing.
  // Two pixels is the floor — below that a stroke greys out instead of drawing.
  const u = Math.max(2, Math.round(size * (size <= 64 ? 0.11 : 0.085)));

  const tickH = u * 4.4;
  const gap = u * 1.3;
  const ruleTop = u * 3.2;
  const ruleBottom = u * 2.1;
  const x0 = (size - (u + gap + ruleTop)) / 2;
  const mid = size / 2;
  const ruleX = x0 + u + gap;

  return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" role="img" aria-label="fyn">
  <rect width="${size}" height="${size}" fill="${INDIGO}"/>
  <g transform="translate(${mid} ${mid}) scale(${scale}) translate(${-mid} ${-mid})">
    <rect x="${x0}" y="${mid - tickH / 2}" width="${u}" height="${tickH}" rx="${u / 2}" fill="${WHITE}"/>
    <rect x="${ruleX}" y="${mid - u * 1.6}" width="${ruleTop}" height="${u}" rx="${u / 2}" fill="${WHITE}"/>
    <rect x="${ruleX}" y="${mid + u * 0.6}" width="${ruleBottom}" height="${u}" rx="${u / 2}" fill="${WHITE}"/>
  </g>
</svg>`;
}

/**
 * Glyphs for the manifest's app shortcuts — the long-press menu on Android and
 * the jump list on desktop. Same plate and same white strokes as the app mark,
 * so the menu reads as one family rather than three borrowed pictograms.
 */
function shortcut({ size, glyph }) {
  const u = Math.round(size * 0.085);
  const mid = size / 2;
  const arm = u * 3.4;
  const white = `fill="${WHITE}"`;
  const art = {
    // A plus: adding an entry.
    add: `<rect x="${mid - u / 2}" y="${mid - arm}" width="${u}" height="${arm * 2}" rx="${u / 2}" ${white}/>
    <rect x="${mid - arm}" y="${mid - u / 2}" width="${arm * 2}" height="${u}" rx="${u / 2}" ${white}/>`,
    // Three rising columns: the overview.
    overview: [0.55, 1.0, 1.5].map((height, index) => {
      const columnWidth = u * 1.15;
      const gap = u * 0.85;
      const span = columnWidth * 3 + gap * 2;
      const x = mid - span / 2 + index * (columnWidth + gap);
      const tall = arm * height;
      return `<rect x="${x}" y="${mid + arm * 0.75 - tall}" width="${columnWidth}" height="${tall}" rx="${u / 2}" ${white}/>`;
    }).join("\n    "),
    // Two stacked rules: a thread of conversation.
    ask: `<rect x="${mid - arm}" y="${mid - u * 1.9}" width="${arm * 2}" height="${u}" rx="${u / 2}" ${white}/>
    <rect x="${mid - arm}" y="${mid + u * 0.4}" width="${arm * 1.3}" height="${u}" rx="${u / 2}" ${white}/>`,
  }[glyph];

  return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" role="img" aria-label="${glyph}">
  <rect width="${size}" height="${size}" rx="${size * 0.22}" fill="${INDIGO}"/>
  ${art}
</svg>`;
}

const SHORTCUTS = [
  { file: "shortcut-add.png", glyph: "add" },
  { file: "shortcut-overview.png", glyph: "overview" },
  { file: "shortcut-ask.png", glyph: "ask" },
];

const TARGETS = [
  { file: "favicon-16.png", size: 16 },
  { file: "favicon-32.png", size: 32 },
  { file: "favicon-48.png", size: 48 },
  { file: "apple-touch-icon.png", size: 180 },
  { file: "pwa-192.png", size: 192 },
  { file: "pwa-256.png", size: 256 },
  { file: "pwa-384.png", size: 384 },
  { file: "pwa-512.png", size: 512 },
  // The platform may crop a maskable icon to a circle, so the art sits inside
  // the 80% safe zone while the indigo plate bleeds to the edge.
  { file: "pwa-maskable-512.png", size: 512, scale: 0.72 },
];

/** Pack PNGs into a multi-size .ico, so no image library is needed for it. */
function ico(pngs) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(pngs.length, 4);

  let offset = 6 + pngs.length * 16;
  const entries = pngs.map(({ size, data }) => {
    const entry = Buffer.alloc(16);
    entry.writeUInt8(size >= 256 ? 0 : size, 0);
    entry.writeUInt8(size >= 256 ? 0 : size, 1);
    entry.writeUInt8(0, 2);
    entry.writeUInt8(0, 3);
    entry.writeUInt16LE(1, 4);
    entry.writeUInt16LE(32, 6);
    entry.writeUInt32LE(data.length, 8);
    entry.writeUInt32LE(offset, 12);
    offset += data.length;
    return entry;
  });
  return Buffer.concat([header, ...entries, ...pngs.map((p) => p.data)]);
}

/**
 * iOS shows a blank white screen while an installed PWA launches unless the
 * page declares a startup image for that exact device resolution. Android and
 * desktop derive their splash from the manifest instead, which is why
 * background_color is the app's indigo rather than the page background: the
 * launch screen should look like the product, not like an empty document.
 *
 * Portrait only, covering the current iPhone sizes and the two common iPads.
 * A device without a match falls back to white, which is what every device did
 * before this existed.
 */
const SPLASH = [
  { w: 1290, h: 2796, cssW: 430, cssH: 932, dpr: 3 },
  { w: 1284, h: 2778, cssW: 428, cssH: 926, dpr: 3 },
  { w: 1179, h: 2556, cssW: 393, cssH: 852, dpr: 3 },
  { w: 1170, h: 2532, cssW: 390, cssH: 844, dpr: 3 },
  { w: 1125, h: 2436, cssW: 375, cssH: 812, dpr: 3 },
  { w: 828, h: 1792, cssW: 414, cssH: 896, dpr: 2 },
  { w: 750, h: 1334, cssW: 375, cssH: 667, dpr: 2 },
  { w: 2048, h: 2732, cssW: 1024, cssH: 1366, dpr: 2 },
  { w: 1620, h: 2160, cssW: 810, cssH: 1080, dpr: 2 },
];

function splash({ w, h }) {
  // The mark at a size that reads on a launch screen without dominating it.
  const markSize = Math.round(Math.min(w, h) * 0.28);
  return `<body style="margin:0;width:${w}px;height:${h}px;background:${INDIGO};display:flex;align-items:center;justify-content:center">${mark({ size: markSize })}</body>`;
}

const browser = await chromium.launch();
await mkdir(out, { recursive: true });

for (const target of TARGETS) {
  const page = await browser.newPage({ viewport: { width: target.size, height: target.size }, deviceScaleFactor: 1 });
  await page.setContent(`<body style="margin:0;width:${target.size}px;height:${target.size}px">${mark(target)}</body>`);
  await page.screenshot({ path: resolve(out, target.file) });
  await page.close();
  console.log(`wrote public/icons/${target.file} (${target.size}px)`);
}
for (const { file, glyph } of SHORTCUTS) {
  const size = 192;
  const page = await browser.newPage({ viewport: { width: size, height: size }, deviceScaleFactor: 1 });
  await page.setContent(`<body style="margin:0;width:${size}px;height:${size}px">${shortcut({ size, glyph })}</body>`);
  await page.screenshot({ path: resolve(out, file), omitBackground: true });
  await page.close();
  console.log(`wrote public/icons/${file} (${size}px)`);
}
for (const device of SPLASH) {
  const page = await browser.newPage({ viewport: { width: device.w, height: device.h }, deviceScaleFactor: 1 });
  await page.setContent(splash(device));
  await page.screenshot({ path: resolve(out, `splash-${device.w}x${device.h}.png`) });
  await page.close();
}
console.log(`wrote ${SPLASH.length} splash screens`);

await browser.close();

// The link tags iOS needs, emitted beside the images so the two cannot drift.
await writeFile(
  resolve(here, "apple-splash-links.html"),
  SPLASH.map((d) =>
    `<link rel="apple-touch-startup-image" media="(device-width: ${d.cssW}px) and (device-height: ${d.cssH}px) and (-webkit-device-pixel-ratio: ${d.dpr}) and (orientation: portrait)" href="/icons/splash-${d.w}x${d.h}.png" />`,
  ).join("\n") + "\n",
  "utf8",
);
console.log("wrote scripts/apple-splash-links.html (paste into index.html when the device list changes)");

// The scalable master, served directly to browsers that prefer it.
await writeFile(resolve(out, "icon.svg"), `${mark({ size: 512 })}\n`, "utf8");
console.log("wrote public/icons/icon.svg");

const sizes = [16, 32, 48];
await writeFile(
  resolve(here, "../public/favicon.ico"),
  ico(await Promise.all(sizes.map(async (size) => ({ size, data: await readFile(resolve(out, `favicon-${size}.png`)) })))),
);
console.log(`wrote public/favicon.ico (${sizes.join(", ")}px)`);
