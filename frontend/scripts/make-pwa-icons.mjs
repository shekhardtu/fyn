/**
 * Draws the PWA icon set from the design tokens: a rupee glyph cut by a
 * rising stroke on the app's own indigo. Rendered through the Playwright
 * Chromium that is already installed for the e2e suite, so this adds no
 * image toolchain.
 *
 *   node scripts/make-pwa-icons.mjs
 */
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(here, "../public/icons");

const require = createRequire(import.meta.url);
const { chromium } = require("@playwright/test");

const INDIGO = "#4340e0";
const INDIGO_DEEP = "#3733c4";
const WHITE = "#ffffff";

function mark({ size, inset, scale = 1 }) {
  const glyph = size * 0.52;
  return `
    <svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
      <defs>
        <linearGradient id="ground" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${INDIGO}"/>
          <stop offset="100%" stop-color="${INDIGO_DEEP}"/>
        </linearGradient>
      </defs>
      <rect width="${size}" height="${size}" fill="url(#ground)"/>
      <g transform="translate(${size / 2}, ${size / 2}) scale(${scale}) translate(${-size / 2}, ${-size / 2})">
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
      </g>
    </svg>`;
}

const TARGETS = [
  { file: "pwa-192.png", size: 192, inset: -8 },
  { file: "pwa-512.png", size: 512, inset: -20 },
  // Maskable: the platform may crop to a circle, so the mark stays inside the
  // 80% safe zone while the indigo plate bleeds to the edge.
  { file: "pwa-maskable-512.png", size: 512, inset: -20, scale: 0.72 },
  { file: "apple-touch-icon.png", size: 180, inset: -7 },
];

const browser = await chromium.launch();
await mkdir(out, { recursive: true });

for (const target of TARGETS) {
  const page = await browser.newPage({
    viewport: { width: target.size, height: target.size },
    deviceScaleFactor: 1,
  });
  await page.setContent(
    `<body style="margin:0;width:${target.size}px;height:${target.size}px">${mark(target)}</body>`,
  );
  await page.screenshot({ path: resolve(out, target.file) });
  await page.close();
  console.log(`wrote public/icons/${target.file} (${target.size}px)`);
}

await browser.close();
