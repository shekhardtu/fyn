/**
 * The manifest names fifteen files and asserts the pixel size of most of them.
 * Nothing else checks either claim: a renamed icon or a re-captured screenshot
 * at a new size fails silently, in an install dialog, on someone else's phone.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const PUBLIC = resolve(__dirname, "../../public");
const manifest = JSON.parse(readFileSync(resolve(PUBLIC, "manifest.webmanifest"), "utf8"));

/** Width and height from a PNG's IHDR, which is always the first chunk. */
function pngSize(path: string): `${number}x${number}` {
  const header = readFileSync(path).subarray(16, 24);
  return `${header.readUInt32BE(0)}x${header.readUInt32BE(4)}`;
}

const referenced: { src: string; sizes?: string }[] = [
  ...manifest.icons,
  ...manifest.screenshots,
  ...manifest.shortcuts.flatMap((shortcut: { icons: { src: string; sizes: string }[] }) => shortcut.icons),
];

describe("web app manifest", () => {
  it.each(referenced.map((entry) => entry.src))("ships %s", (src) => {
    expect(() => readFileSync(resolve(PUBLIC, src.replace(/^\//, "")))).not.toThrow();
  });

  it.each(referenced.filter((entry) => entry.src.endsWith(".png")).map((entry) => [entry.src, entry.sizes]))(
    "declares %s as %s, and it is",
    (src, sizes) => expect(pngSize(resolve(PUBLIC, src.replace(/^\//, "")))).toBe(sizes),
  );

  it("offers both form factors, which Chrome needs for the rich install dialog", () => {
    const factors = new Set(manifest.screenshots.map((shot: { form_factor: string }) => shot.form_factor));
    expect(factors).toEqual(new Set(["narrow", "wide"]));
  });

  it("points every shortcut and the share target at a route the app serves", () => {
    const routes = readFileSync(resolve(__dirname, "../app/router.tsx"), "utf8");
    for (const { url } of manifest.shortcuts) {
      const [path] = url.split("?");
      // "/" is the index route, which the table declares as `index: true`.
      if (path === "/") continue;
      expect(routes).toContain(`path: "${path.replace(/^\//, "")}"`);
    }
    expect(manifest.share_target.action).toBe("/");
    expect(manifest.share_target.method).toBe("GET");
  });
});
