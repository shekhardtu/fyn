import { cn } from "@/lib/utils";
import { useUserDefaults } from "@/components/user-defaults";
import { calendarDayKey } from "@/lib/format";

const dayLabelFormatters = new Map<string, Intl.DateTimeFormat>();

function dayLabelFormatter(timeZone?: string) {
  const key = timeZone ?? "local";
  const existing = dayLabelFormatters.get(key);
  if (existing) return existing;
  const created = new Intl.DateTimeFormat("en-IN", {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone,
  });
  dayLabelFormatters.set(key, created);
  return created;
}

/** The profile's calendar day for an instant, as `YYYY-MM-DD`.
 *
 *  The saved timezone rather than UTC (or whichever zone this device happens
 *  to use) keeps grouping aligned with every delivery time and month boundary
 *  elsewhere in the account. */
export function localDayKey(value: string, timeZone?: string): string | null {
  return calendarDayKey(value, timeZone);
}

/** "Tue, 19 Aug", and the year only outside the current one — on a thread from
 *  this morning it is noise, on one from 2025 it is the whole point.
 *
 *  Assembled from parts rather than from a second formatter, because the same
 *  locale punctuates the two shapes differently ("Tue, 30 Dec" but "Tue, 30
 *  Dec, 2025") and a marker that changes shape with the year is a marker the
 *  eye has to stop and read. */
function exactDate(instant: Date, withYear: boolean, timeZone?: string) {
  const parts = new Map(dayLabelFormatter(timeZone).formatToParts(instant).map((part) => [part.type, part.value]));
  const day = `${parts.get("weekday")}, ${parts.get("day")} ${parts.get("month")}`;
  return withYear ? `${day} ${parts.get("year")}` : day;
}

function previousDayKey(key: string | null) {
  if (!key) return null;
  const [year, month, day] = key.split("-").map(Number);
  const previous = new Date(Date.UTC(year, month - 1, day - 1));
  return previous.toISOString().slice(0, 10);
}

/** Where a day sits relative to the reader, in the words they'd use for it.
 *  "Today" and "Yesterday" are the only two days anyone names by feel; every
 *  other day is named by its date. */
function dayNames(instant: Date, now: Date, timeZone?: string) {
  const key = localDayKey(instant.toISOString(), timeZone);
  const today = localDayKey(now.toISOString(), timeZone);
  const exact = exactDate(instant, key?.slice(0, 4) !== today?.slice(0, 4), timeZone);
  // Built from local calendar parts rather than by subtracting 24 hours, so a
  // month end, a year end, and a daylight-saving shift all land on the right
  // day.
  const relative = key === today
    ? "Today"
    : key === previousDayKey(today)
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
  const { timeZone } = useUserDefaults();
  const instant = new Date(isoTime);
  if (Number.isNaN(instant.valueOf())) return null;
  const { label, spoken, key } = dayNames(instant, new Date(), timeZone);

  return <div className={cn("day-pill", className)} role="separator" aria-label={spoken}>
    <time dateTime={key ?? undefined} aria-hidden>{label}</time>
  </div>;
}
