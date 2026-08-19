import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DayDivider, localDayKey } from "@/components/day-divider";

/** Fixed "now" so "Today" and "Yesterday" mean something the assertions can
 *  name. Local noon, so the surrounding days stay on the intended date in any
 *  timezone the suite runs in. */
function freezeNow(year: number, month: number, day: number) {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(year, month - 1, day, 12, 0, 0));
}

function localNoon(year: number, month: number, day: number) {
  return new Date(year, month - 1, day, 12, 0, 0).toISOString();
}

afterEach(() => {
  vi.useRealTimers();
});

describe("day divider", () => {
  it("names the two days a person names by feel", () => {
    freezeNow(2026, 8, 19);

    const { rerender } = render(<DayDivider isoTime={localNoon(2026, 8, 19)} />);
    expect(screen.getByRole("separator")).toHaveTextContent("Today");

    rerender(<DayDivider isoTime={localNoon(2026, 8, 18)} />);
    expect(screen.getByRole("separator")).toHaveTextContent("Yesterday");
  });

  it("dates every other day, and keeps the year only when it is not this one", () => {
    freezeNow(2026, 8, 19);

    const { rerender } = render(<DayDivider isoTime={localNoon(2026, 8, 11)} />);
    expect(screen.getByRole("separator")).toHaveTextContent("Tue, 11 Aug");
    expect(screen.getByRole("separator")).not.toHaveTextContent("2026");

    rerender(<DayDivider isoTime={localNoon(2025, 12, 30)} />);
    expect(screen.getByRole("separator")).toHaveTextContent("Tue, 30 Dec 2025");
  });

  it("speaks the exact date even where it shows a relative one", () => {
    freezeNow(2026, 8, 19);

    render(<DayDivider isoTime={localNoon(2026, 8, 19)} />);

    expect(screen.getByRole("separator")).toHaveAttribute("aria-label", "Today, Wed, 19 Aug");
  });

  it("marks the day machine-readably in the reader's own timezone", () => {
    freezeNow(2026, 8, 19);

    const { container } = render(<DayDivider isoTime={localNoon(2026, 8, 3)} />);

    expect(container.querySelector("time")).toHaveAttribute("datetime", "2026-08-03");
  });

  it("renders nothing rather than an invented day for an unparseable instant", () => {
    const { container } = render(<DayDivider isoTime="not-a-timestamp" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("groups by local midnight, so a late-night entry stays on its own day", () => {
    // 2026-08-19T21:30 in a UTC+5:30 browser is still the 19th to the person
    // who wrote it, though the same instant is the 19th at 16:00 UTC.
    const lateEvening = new Date(2026, 7, 19, 21, 30, 0);
    const nextMorning = new Date(2026, 7, 20, 9, 15, 0);

    expect(localDayKey(lateEvening.toISOString())).toBe("2026-08-19");
    expect(localDayKey(nextMorning.toISOString())).toBe("2026-08-20");
    expect(localDayKey("not-a-timestamp")).toBeNull();
  });
});
