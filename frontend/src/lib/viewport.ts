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
 *   --keyboard-inset   how much of the layout viewport the keyboard covers
 *
 * and `data-keyboard="open"` on the root is that last fact as a selector, so
 * the dock can drop the home-indicator padding the keyboard is already
 * covering. Android Chrome and Safari 26 honour `interactive-widget=
 * resizes-content` from the viewport meta and shrink the layout viewport
 * themselves; there the offset and the inset are simply zero and the height
 * is the one the browser already chose. Nothing here has to know which
 * browser it is on.
 */

export type ViewportMetrics = {
  /** Visible height, in CSS pixels. */
  height: number;
  /** How far the visible rectangle has been panned down the layout viewport. */
  offset: number;
  /** How much of the layout viewport a software keyboard is covering. */
  keyboard: number;
};

/* A keyboard is the better part of a phone screen. Anything smaller than this
   is the address bar collapsing, a rotation settling, or rounding — none of
   which should read as "the keyboard is up". */
const KEYBOARD_MIN = 96;

const listeners = new Set<(metrics: ViewportMetrics) => void>();
let current: ViewportMetrics | null = null;

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
  const offset = Math.round(view.offsetTop);
  return { height, offset, keyboard: Math.max(0, Math.round(layout - height - offset)) };
}

function publish(next: ViewportMetrics) {
  const previous = current;
  // Writing a custom property on the root invalidates inherited style for
  // every element under it, and these events arrive in bursts while a keyboard
  // animates, so unchanged frames say nothing.
  if (previous && previous.height === next.height && previous.offset === next.offset && previous.keyboard === next.keyboard) return;
  current = next;
  const root = document.documentElement;
  root.style.setProperty("--app-height", `${next.height}px`);
  root.style.setProperty("--viewport-offset", `${next.offset}px`);
  root.style.setProperty("--keyboard-inset", `${next.keyboard}px`);

  const open = next.keyboard >= KEYBOARD_MIN;
  if (open) root.dataset.keyboard = "open";
  else delete root.dataset.keyboard;
  // The pan belongs to the browser, and iOS does not reliably take it back
  // when the keyboard leaves. The offset above already keeps the shell in the
  // right place, so this is only tidying: put the layout viewport back at the
  // top, and never on a page that has its own scrolling to preserve.
  if (previous && previous.keyboard >= KEYBOARD_MIN && !open && document.documentElement.scrollHeight <= window.innerHeight + 1) {
    window.scrollTo(0, 0);
  }

  for (const listener of [...listeners]) listener(next);
}

/**
 * Installed once, before the first render, so the shell's first paint is
 * already the right height. Returns a teardown for tests.
 */
export function initViewport() {
  if (typeof window === "undefined") return () => {};
  let frame = 0;
  const sync = () => {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      const next = measure();
      if (next) publish(next);
    });
  };

  const view = window.visualViewport;
  // `scroll` is the pan; `resize` is the keyboard, the rotation and the
  // address bar. Focus moving between fields can swap one keyboard for another
  // — a number pad for a text one — and Safari does not always call that a
  // resize, so focus changes re-measure too.
  view?.addEventListener("resize", sync);
  view?.addEventListener("scroll", sync);
  window.addEventListener("resize", sync);
  window.addEventListener("orientationchange", sync);
  document.addEventListener("focusin", sync, true);
  document.addEventListener("focusout", sync, true);

  const first = measure();
  if (first) publish(first);

  return () => {
    if (frame) cancelAnimationFrame(frame);
    view?.removeEventListener("resize", sync);
    view?.removeEventListener("scroll", sync);
    window.removeEventListener("resize", sync);
    window.removeEventListener("orientationchange", sync);
    document.removeEventListener("focusin", sync, true);
    document.removeEventListener("focusout", sync, true);
    current = null;
    const root = document.documentElement;
    root.style.removeProperty("--app-height");
    root.style.removeProperty("--viewport-offset");
    root.style.removeProperty("--keyboard-inset");
    delete root.dataset.keyboard;
  };
}
