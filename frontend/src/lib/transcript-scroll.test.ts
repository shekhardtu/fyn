import { describe, expect, it } from "vitest";
import { transcriptElementOffset } from "@/lib/transcript-scroll";

function dimensions(element: HTMLElement, values: { top: number; scrollTop?: number; scrollHeight?: number; clientHeight?: number }) {
  element.getBoundingClientRect = () => ({
    x: 0,
    y: values.top,
    top: values.top,
    right: 0,
    bottom: values.top,
    left: 0,
    width: 0,
    height: 0,
    toJSON: () => ({}),
  });
  if (values.scrollTop !== undefined) element.scrollTop = values.scrollTop;
  if (values.scrollHeight !== undefined) Object.defineProperty(element, "scrollHeight", { configurable: true, value: values.scrollHeight });
  if (values.clientHeight !== undefined) Object.defineProperty(element, "clientHeight", { configurable: true, value: values.clientHeight });
}

describe("transcriptElementOffset", () => {
  it("aligns the target within only the transcript scroller and honors scroll margin", () => {
    const scroller = document.createElement("div");
    const target = document.createElement("div");
    target.style.scrollMarginTop = "16px";
    dimensions(scroller, { top: 100, scrollTop: 900, scrollHeight: 3_000, clientHeight: 600 });
    dimensions(target, { top: 340 });

    expect(transcriptElementOffset(scroller, target)).toBe(1_124);
  });

  it("clamps a final target to the reachable bottom instead of chasing an impossible alignment", () => {
    const scroller = document.createElement("div");
    const target = document.createElement("div");
    dimensions(scroller, { top: 0, scrollTop: 1_300, scrollHeight: 2_000, clientHeight: 500 });
    dimensions(target, { top: 400 });

    expect(transcriptElementOffset(scroller, target)).toBe(1_500);
  });

  it("never returns a negative offset", () => {
    const scroller = document.createElement("div");
    const target = document.createElement("div");
    dimensions(scroller, { top: 100, scrollTop: 0, scrollHeight: 1_000, clientHeight: 500 });
    dimensions(target, { top: 40 });

    expect(transcriptElementOffset(scroller, target)).toBe(0);
  });
});
