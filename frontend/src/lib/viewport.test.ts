import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { initViewport, subscribeToViewport, viewportMetrics } from "@/lib/viewport";

/** Stands in for the real `visualViewport`, which jsdom does not move. */
class FakeVisualViewport extends EventTarget {
  height = 800;
  offsetTop = 0;
  pageTop = 0;
  scale = 1;
}

let view: FakeVisualViewport;
let teardown: () => void;
let scrollTo: ReturnType<typeof vi.fn>;

function setLayoutHeight(value: number) {
  Object.defineProperty(window, "innerHeight", { value, configurable: true });
}

/** One keyboard, sliding over the page without resizing the layout viewport. */
function showKeyboard(height: number, pan = 0) {
  view.height = 800 - height;
  view.offsetTop = pan;
  view.pageTop = pan;
  view.dispatchEvent(new Event("resize"));
}

function readVariable(name: string) {
  return document.documentElement.style.getPropertyValue(name);
}

beforeEach(() => {
  vi.useFakeTimers();
  view = new FakeVisualViewport();
  Object.defineProperty(window, "visualViewport", { value: view, configurable: true });
  setLayoutHeight(800);
  scrollTo = vi.fn();
  vi.stubGlobal("scrollTo", scrollTo);
  // Run the coalescing frame inline; returning 0 keeps the module's "a frame is
  // already booked" guard from latching on a callback that has already run.
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 0; });
  teardown = initViewport();
});

afterEach(() => {
  teardown();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("initViewport", () => {
  it("publishes the visible rectangle before anything has moved", () => {
    expect(readVariable("--app-height")).toBe("800px");
    expect(readVariable("--viewport-offset")).toBe("0px");
    expect(readVariable("--keyboard-inset")).toBe("0px");
    expect(document.documentElement.dataset.keyboard).toBeUndefined();
  });

  it("hands the shell the height above the keyboard, and the pan to pay back", () => {
    showKeyboard(336, 120);

    expect(readVariable("--app-height")).toBe("464px");
    expect(readVariable("--viewport-offset")).toBe("120px");
    expect(readVariable("--keyboard-inset")).toBe("336px");
    expect(document.documentElement.dataset.keyboard).toBe("open");
  });

  it("does not mistake a large keyboard pan for a closed keyboard", () => {
    showKeyboard(336, 300);

    expect(readVariable("--viewport-offset")).toBe("300px");
    expect(readVariable("--keyboard-inset")).toBe("336px");
    expect(document.documentElement.dataset.keyboard).toBe("open");
    expect(scrollTo).not.toHaveBeenCalled();
  });

  it("reads a browser that resizes its own layout viewport as no keyboard at all", () => {
    // Chrome honours interactive-widget=resizes-content:
    // the visible height shrinks, but so does the layout viewport, so there is
    // no covered strip and no inset to correct.
    setLayoutHeight(464);
    showKeyboard(336);

    expect(readVariable("--app-height")).toBe("464px");
    expect(readVariable("--keyboard-inset")).toBe("0px");
    expect(document.documentElement.dataset.keyboard).toBeUndefined();
  });

  it("does not treat the address bar collapsing as a keyboard", () => {
    showKeyboard(48);

    expect(readVariable("--app-height")).toBe("752px");
    expect(document.documentElement.dataset.keyboard).toBeUndefined();
  });

  it("holds its last measurement through a pinch zoom", () => {
    view.scale = 2;
    showKeyboard(400, 200);

    expect(readVariable("--app-height")).toBe("800px");
    expect(readVariable("--viewport-offset")).toBe("0px");
  });

  it("puts the page back at the top once the keyboard leaves", () => {
    showKeyboard(336, 120);
    expect(scrollTo).not.toHaveBeenCalled();

    showKeyboard(0);

    expect(readVariable("--app-height")).toBe("800px");
    expect(readVariable("--viewport-offset")).toBe("0px");
    expect(document.documentElement.dataset.keyboard).toBeUndefined();
    expect(scrollTo).toHaveBeenCalledWith(0, 0);
  });

  it("remeasures values that WebKit updates after its resize event", () => {
    showKeyboard(336);
    expect(readVariable("--viewport-offset")).toBe("0px");

    // Standalone WebKit can publish the resize before offsetTop/pageTop catch
    // up, and then supply the correct fields without another event.
    view.offsetTop = 260;
    view.pageTop = 260;
    vi.advanceTimersByTime(80);

    expect(readVariable("--viewport-offset")).toBe("260px");
  });

  it("finishes a keyboard close whose final metrics arrive without an event", () => {
    showKeyboard(336, 260);
    document.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));

    // The first close event still exposes the keyboard-sized rectangle.
    vi.advanceTimersByTime(80);
    expect(readVariable("--app-height")).toBe("464px");

    // WebKit finishes its animation later but omits the final resize/scroll.
    view.height = 800;
    view.offsetTop = 0;
    view.pageTop = 0;
    vi.advanceTimersByTime(420);

    expect(readVariable("--app-height")).toBe("800px");
    expect(readVariable("--viewport-offset")).toBe("0px");
    expect(document.documentElement.dataset.keyboard).toBeUndefined();
    expect(scrollTo).toHaveBeenCalledWith(0, 0);
  });

  it("uses pageTop when WebKit under-reports offsetTop", () => {
    view.height = 464;
    view.offsetTop = 80;
    view.pageTop = 240;
    view.dispatchEvent(new Event("resize"));

    expect(readVariable("--viewport-offset")).toBe("240px");
  });

  it("repairs the origin under a pinned shell, whatever the document claims its height is", () => {
    // WebKit can report a document taller than the window while the keyboard
    // is up, even with the shell pinned over the whole screen and nothing
    // under it scrolling. Trusting that number skipped the repair exactly
    // where it was needed.
    const shell = document.createElement("div");
    shell.className = "app-shell";
    document.body.append(shell);
    Object.defineProperty(document.documentElement, "scrollHeight", { value: 1400, configurable: true });

    showKeyboard(336, 120);
    showKeyboard(0);

    expect(scrollTo).toHaveBeenCalledWith(0, 0);
    shell.remove();
  });

  it("follows the browser's own pan without waiting for a resize", () => {
    view.offsetTop = 90;
    view.dispatchEvent(new Event("scroll"));

    expect(readVariable("--viewport-offset")).toBe("90px");
  });

  it("tells subscribers once per real change", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeToViewport(listener);

    showKeyboard(336);
    showKeyboard(336);

    expect(listener).toHaveBeenCalledTimes(1);
    expect(viewportMetrics()).toEqual({ height: 464, offset: 0, keyboard: 336 });
    unsubscribe();
  });

  it("stops measuring and hands the stylesheet back its fallbacks", () => {
    teardown();
    showKeyboard(336);

    expect(readVariable("--app-height")).toBe("");
    expect(viewportMetrics()).toBeNull();
  });
});
