/* Same-origin by default: the dev server (vite.config.ts) and production
   nginx both relay /api to the backend, so the session cookie stays
   first-party. Set VITE_API_URL only to point a build at a remote API. */
const DEFAULT_API_URL = "";

function withoutTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

/** Public, build-time configuration for browser code.
 *
 * Keep environment reads at this boundary. Vite embeds every `VITE_*` value in
 * the client bundle, so secrets must never be added here. */
export const environment = Object.freeze({
  apiUrl: withoutTrailingSlash(import.meta.env.VITE_API_URL?.trim() || DEFAULT_API_URL),
  googleClientId: import.meta.env.VITE_GOOGLE_CLIENT_ID?.trim() || "",
  isDevelopment: import.meta.env.DEV,
});
