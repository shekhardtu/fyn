/**
 * The window the reader can actually see.
 *
 * `100dvh` answers with the layout viewport, and on iOS the software keyboard
 * does not touch that number — it slides over the page instead. The composer
 * is left parked underneath it, so the browser pans the page up to reveal the
 * field it just covered. That pan is the bug: when the keyboard goes away the
 * page is frequently left where the pan put it, with the composer stranded
 * mid-screen and dead space below it, and only a manual drag puts it back.
 * The same thing happens in the installed PWA, where there is no address bar
 * whose collapse would otherwise hide it.
 *
 * `visualViewport` is the only API that reports the rectangle the reader is
 * looking at, so the shell is measured from it rather than from a CSS unit:
 *
 *   --app-height       what is visible, which the shell takes as its height
 *   --viewport-offset  how far the browser has panned, which the shell pays
 *                      back as a `top`, so it stays glued to the visible area
 *                      rather than to the layout viewport nobody can see
 *   --keyboard-inset   how much shorter the visual viewport is than the
 *                      layout viewport
 *
 * and `data-keyboard="open"` on the root is that last fact as a selector, so
 * the dock can drop the home-indicator padding the keyboard is already
 * covering. Browsers which honour `interactive-widget=resizes-content` from
 * the viewport meta shrink the layout viewport themselves; there the offset
 * and the inset are simply zero and the height is the one the browser already
 * chose. Nothing here has to know which browser it is on.
 */

export type ViewportMetrics = {
  /** Visible height, in CSS pixels. */
  height: number;
  /** How far the visible rectangle has been panned down the layout viewport. */
  offset: number;
  /** How much shorter the visual viewport is than the layout viewport. */
  keyboard: number;
};

/* A keyboard is the better part of a phone screen. Anything smaller than this
   is the address bar collapsing, a rotation settling, or rounding — none of
   which should read as "the keyboard is up". */
const KEYBOARD_MIN = 96;
/* WebKit can fire `visualViewport.resize` before its height and offset fields
   have caught up, especially in a home-screen app. Two animation frames avoid
   reading the event's stale layout, the short trailing read catches values
   which arrive tens of milliseconds later, and the lifecycle read covers the
   full keyboard animation when focus or PWA visibility changes do not produce
   a final viewport event. */
const VIEWPORT_SETTLE_MS = 80;
const KEYBOARD_SETTLE_MS = 500;

const listeners = new Set<(metrics: ViewportMetrics) => void>();
let current: ViewportMetrics | null = null;
let keyboardSession = false;

/** The last published measurement, or null before the first one. */
export function viewportMetrics() {
  return current;
}

/**
 * Called back after every published change — the transcript follows the end of
 * the conversation, and the end moves when the keyboard takes half the screen.
 */
export function subscribeToViewport(listener: (metrics: ViewportMetrics) => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function measure(): ViewportMetrics | null {
  const view = window.visualViewport;
  const layout = window.innerHeight;
  // Every browser the app ships to has `visualViewport`; the fallback is for
  // jsdom and for anything old enough that the layout viewport is all there is.
  if (!view) return { height: layout, offset: 0, keyboard: 0 };
  // A pinch zoom shrinks the visible rectangle exactly the way a keyboard
  // does, and resizing the shell to a zoomed-in crop would fight the reader
  // mid-gesture. Hold the last measurement until the page is back at 1.
  if (view.scale > 1.01) return null;
  const height = Math.round(view.height);
  // WebKit 26 can under-report offsetTop while pageTop is already correct.
  // They describe the same pan once ordinary document scrolling is removed,
  // so prefer whichever non-negative reading has caught up further.
  const offset = Math.max(
    0,
    Math.round(view.offsetTop),
    Math.round(view.pageTop - window.scrollY),
  );
  // A pan only changes which part of the layout viewport is visible; it does
  // not make the software keyboard shorter. Keeping it out of this subtraction
  // is what lets a heavily panned iOS viewport still count as keyboard-open.
  return { height, offset, keyboard: Math.max(0, Math.round(layout - height)) };
}

function publish(next: ViewportMetrics) {
  const previous = current;
  const open = next.keyboard >= KEYBOARD_MIN;
  if (open) keyboardSession = true;

  // A close animation can cross the threshold before the viewport has reached
  // its final height. Retry the page-origin repair on every settling read until
  // both the shrink and pan are gone; an early scrollTo is harmless, while a
  // single early attempt is exactly what leaves installed WebKit apps stuck.
  if (!open && keyboardSession && document.documentElement.scrollHeight <= window.innerHeight + 1) {
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
    window.scrollTo(0, 0);
    if (next.keyboard === 0 && next.offset === 0) keyboardSession = false;
  }

  // Writing a custom property on the root invalidates inherited style for
  // every element under it, and these events arrive in bursts while a keyboard
  // animates, so unchanged frames say nothing.
  if (previous && previous.height === next.height && previous.offset === next.offset && previous.keyboard === next.keyboard) return;
  current = next;
  const root = document.documentElement;
  root.style.setProperty("--app-height", `${next.height}px`);
  root.style.setProperty("--viewport-offset", `${next.offset}px`);
  root.style.setProperty("--keyboard-inset", `${next.keyboard}px`);

  if (open) root.dataset.keyboard = "open";
  else delete root.dataset.keyboard;

  for (const listener of [...listeners]) listener(next);
}

/**
 * Installed once, before the first render, so the shell's first paint is
 * already the right height. Returns a teardown for tests.
 */
export function initViewport() {
  if (typeof window === "undefined") return () => {};
  let frame = 0;
  let settleTimer = 0;
  let recoveryTimer = 0;

  const scheduleMeasurement = () => {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      // A second frame is intentional. In standalone WebKit the resize event
      // and even its first animation frame can expose the previous offsetTop.
      frame = requestAnimationFrame(() => {
        frame = 0;
        const next = measure();
        if (next) publish(next);
      });
    });
  };

  const sync = () => {
    scheduleMeasurement();
    window.clearTimeout(settleTimer);
    settleTimer = window.setTimeout(scheduleMeasurement, VIEWPORT_SETTLE_MS);
  };

  const recover = () => {
    sync();
    window.clearTimeout(recoveryTimer);
    recoveryTimer = window.setTimeout(scheduleMeasurement, KEYBOARD_SETTLE_MS);
  };

  const recoverWhenVisible = () => {
    if (document.visibilityState === "visible") recover();
  };

  const view = window.visualViewport;
  // `scroll` is the pan; `resize` is the keyboard, the rotation and the
  // address bar. Focus moving between fields can swap one keyboard for another
  // — a number pad for a text one — and Safari does not always call that a
  // resize, so focus changes re-measure too.
  view?.addEventListener("resize", sync);
  view?.addEventListener("scroll", sync);
  view?.addEventListener("scrollend", sync);
  window.addEventListener("resize", sync);
  window.addEventListener("scroll", sync);
  window.addEventListener("orientationchange", recover);
  window.addEventListener("pageshow", recover);
  window.addEventListener("focus", recover);
  document.addEventListener("visibilitychange", recoverWhenVisible);
  document.addEventListener("focusin", recover, true);
  document.addEventListener("focusout", recover, true);

  const first = measure();
  if (first) publish(first);

  return () => {
    if (frame) cancelAnimationFrame(frame);
    window.clearTimeout(settleTimer);
    window.clearTimeout(recoveryTimer);
    view?.removeEventListener("resize", sync);
    view?.removeEventListener("scroll", sync);
    view?.removeEventListener("scrollend", sync);
    window.removeEventListener("resize", sync);
    window.removeEventListener("scroll", sync);
    window.removeEventListener("orientationchange", recover);
    window.removeEventListener("pageshow", recover);
    window.removeEventListener("focus", recover);
    document.removeEventListener("visibilitychange", recoverWhenVisible);
    document.removeEventListener("focusin", recover, true);
    document.removeEventListener("focusout", recover, true);
    current = null;
    keyboardSession = false;
    const root = document.documentElement;
    root.style.removeProperty("--app-height");
    root.style.removeProperty("--viewport-offset");
    root.style.removeProperty("--keyboard-inset");
    delete root.dataset.keyboard;
  };
}
