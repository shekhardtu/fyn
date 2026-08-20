/**
 * Text handed to fyn by the platform share sheet.
 *
 * The manifest declares a GET share target, so a share arrives as an ordinary
 * navigation to `/` carrying `title`, `text` and `url`. That keeps the whole
 * feature inside the app: no service-worker POST handling, and a shared
 * receipt or payment message lands in the composer where the person can read
 * it before sending.
 */

const SHARE_KEYS = ["title", "text", "url"] as const;
// Long enough for a pasted statement fragment, short enough that a rogue
// share cannot stuff the composer with a megabyte of text.
const MAX_SHARED = 2_000;

/**
 * Reads the shared text and removes it from the URL, so a refresh or a Back
 * does not silently re-seed a composer the person already cleared.
 *
 * Returns "" when this navigation was not a share, which is every other one.
 */
export function takeSharedText(): string {
  if (typeof window === "undefined") return "";
  const params = new URLSearchParams(window.location.search);
  const parts = SHARE_KEYS
    .map((key) => params.get(key)?.trim())
    .filter((part): part is string => Boolean(part));
  if (!parts.length) return "";

  for (const key of SHARE_KEYS) params.delete(key);
  const query = params.toString();
  window.history.replaceState(null, "", `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`);

  // Share sheets routinely send one link as both `text` and `url`; pasting it
  // twice would read as a mistake fyn made.
  return [...new Set(parts)].join(" ").slice(0, MAX_SHARED);
}
