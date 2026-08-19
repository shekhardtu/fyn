import { cn } from "@/lib/utils";

/** The reader's own calendar day for an instant, as `YYYY-MM-DD`.
 *
 *  Local rather than UTC, because the grouping has to agree with the delivery
 *  time printed under every message, and that is printed in the browser's
 *  timezone. An entry written at 2am in Delhi belongs to the day the person
 *  writing it was living in, whatever UTC calls that moment. */
export function localDayKey(value: string): string | null {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return null;
  const month = `${parsed.getMonth() + 1}`.padStart(2, "0");
  const day = `${parsed.getDate()}`.padStart(2, "0");
  return `${parsed.getFullYear()}-${month}-${day}`;
}

/** Built once: a long thread formats one date per day rather than one per
 *  message, but the transcript re-renders on every streamed token. */
const dayParts = new Intl.DateTimeFormat("en-IN", { weekday: "short", day: "numeric", month: "short", year: "numeric" });

/** "Tue, 19 Aug", and the year only outside the current one — on a thread from
 *  this morning it is noise, on one from 2025 it is the whole point.
 *
 *  Assembled from parts rather than from a second formatter, because the same
 *  locale punctuates the two shapes differently ("Tue, 30 Dec" but "Tue, 30
 *  Dec, 2025") and a marker that changes shape with the year is a marker the
 *  eye has to stop and read. */
function exactDate(instant: Date, withYear: boolean) {
  const parts = new Map(dayParts.formatToParts(instant).map((part) => [part.type, part.value]));
  const day = `${parts.get("weekday")}, ${parts.get("day")} ${parts.get("month")}`;
  return withYear ? `${day} ${parts.get("year")}` : day;
}

/** Where a day sits relative to the reader, in the words they'd use for it.
 *  "Today" and "Yesterday" are the only two days anyone names by feel; every
 *  other day is named by its date. */
function dayNames(instant: Date, now: Date) {
  const exact = exactDate(instant, instant.getFullYear() !== now.getFullYear());
  const key = localDayKey(instant.toISOString());
  // Built from local calendar parts rather than by subtracting 24 hours, so a
  // month end, a year end, and a daylight-saving shift all land on the right
  // day.
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  const relative = key === localDayKey(now.toISOString())
    ? "Today"
    : key === localDayKey(yesterday.toISOString())
      ? "Yesterday"
      : null;
  return { label: relative ?? exact, spoken: relative ? `${relative}, ${exact}` : exact, key };
}

/** The mark a new day opens under.
 *
 *  A pill centred over the column, and deliberately the quietest thing in the
 *  transcript: it tells you when you are, and then gets out of the way of what
 *  was said. Orientation is not a turn in the conversation, so it carries no
 *  emphasis of its own — muted ink at the meta size on the sunken ground. */
export function DayDivider({ isoTime, className }: { isoTime: string; className?: string }) {
  const instant = new Date(isoTime);
  if (Number.isNaN(instant.valueOf())) return null;
  const { label, spoken, key } = dayNames(instant, new Date());

  return <div className={cn("day-pill", className)} role="separator" aria-label={spoken}>
    <time dateTime={key ?? undefined} aria-hidden>{label}</time>
  </div>;
}
