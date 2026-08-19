import { useEffect, useRef } from "react";

/** Marks whether a scroll container has content hidden above or below, so a
 *  cut-off list can say so with a fade instead of ending on a hard edge.
 *
 *  Written to the DOM rather than to state, deliberately. This fires on every
 *  scroll event, and in React state that is a re-render of the whole host per
 *  frame of scrolling — for two fades whose only job is to be visible or not.
 *  The flags land as data attributes on the scroller's parent and CSS does the
 *  rest, so scrolling now costs nothing in React at all. */
export function useScrollEdges<T extends HTMLElement>(dependency: unknown) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const node = ref.current;
    const host = node?.parentElement;
    if (!node || !host) return;
    // Held so the attribute is only written when the answer actually changes;
    // a scroll within the same state is the common case.
    let top: boolean | null = null;
    let bottom: boolean | null = null;
    const update = () => {
      const nextTop = node.scrollTop > 4;
      const nextBottom = Math.ceil(node.scrollTop + node.clientHeight) < node.scrollHeight - 4;
      if (nextTop !== top) { top = nextTop; host.dataset.edgeTop = String(nextTop); }
      if (nextBottom !== bottom) { bottom = nextBottom; host.dataset.edgeBottom = String(nextBottom); }
    };
    update();
    node.addEventListener("scroll", update, { passive: true });
    if (typeof ResizeObserver === "undefined") return () => node.removeEventListener("scroll", update);
    const observer = new ResizeObserver(update);
    observer.observe(node);
    if (node.firstElementChild) observer.observe(node.firstElementChild);
    return () => { node.removeEventListener("scroll", update); observer.disconnect(); };
  }, [dependency]);
  return ref;
}
