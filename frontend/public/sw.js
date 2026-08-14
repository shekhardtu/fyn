/* fyn's service worker: an installable shell, never a data cache.
 *
 * Three rules decide everything below:
 *   1. /api is never touched. Financial truth comes from the network or not
 *      at all — a stale balance shown confidently is worse than an offline
 *      notice. Offline reads are the query persister's job (IndexedDB),
 *      which knows freshness; an HTTP cache does not.
 *   2. Hashed build assets (/assets/) are immutable by construction, so they
 *      are cache-first forever; old versions are swept by cache-name rotation.
 *   3. Navigations are network-first with the cached shell as fallback, so a
 *      deploy is picked up on the next online load and an offline launch
 *      still boots the app.
 */
const VERSION = "v1";
const SHELL = `fyn-shell-${VERSION}`;
const ASSETS = `fyn-assets-${VERSION}`;
const OFFLINE_URL = "/offline.html";
const SHELL_URLS = ["/", OFFLINE_URL, "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL).then((cache) => cache.addAll(SHELL_URLS)).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== SHELL && key !== ASSETS).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          // Keep the freshest shell for the next offline launch.
          const copy = response.clone();
          caches.open(SHELL).then((cache) => cache.put("/", copy)).catch(() => undefined);
          return response;
        })
        .catch(async () => (await caches.match("/")) ?? (await caches.match(OFFLINE_URL)) ?? Response.error()),
    );
    return;
  }

  if (url.pathname.startsWith("/assets/") || url.pathname.startsWith("/icons/")) {
    event.respondWith(
      caches.match(request).then((cached) => cached ?? fetch(request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(ASSETS).then((cache) => cache.put(request, copy)).catch(() => undefined);
        }
        return response;
      })),
    );
  }
});
