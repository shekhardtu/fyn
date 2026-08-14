import { environment } from "@/config/environment";

/** Installs the app shell worker in production builds.
 *
 * Dev stays unregistered — a worker serving yesterday's cached shell over a
 * live dev server is the most confusing bug a build tool can produce — and
 * any worker left behind by a previous production visit to this origin is
 * actively removed for the same reason.
 */
export function setupServiceWorker() {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
  if (environment.isDevelopment) {
    void navigator.serviceWorker.getRegistrations()
      .then((registrations) => registrations.forEach((registration) => void registration.unregister()))
      .catch(() => undefined);
    return;
  }
  window.addEventListener("load", () => {
    void navigator.serviceWorker.register("/sw.js").catch(() => undefined);
  });
}
