const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Building an `Intl` formatter costs around 18µs; formatting with one that
 *  already exists costs 0.4µs. A transcript prints hundreds of figures and
 *  reprints all of them on every render, so the formatters are built once for
 *  each shape and kept. There are only a handful of shapes — a currency and a
 *  digit count — so the map cannot grow unbounded. */
const formatters = new Map<string, Intl.NumberFormat | Intl.DateTimeFormat>();

function reuse<T extends Intl.NumberFormat | Intl.DateTimeFormat>(key: string, build: () => T) {
  const existing = formatters.get(key);
  if (existing) return existing as T;
  const created = build();
  formatters.set(key, created);
  return created;
}

/** Money that is genuinely absent reads as "—"; a real zero still reads as ₹0. */
export function formatMoney(value: unknown, currency = "INR") {
  if (value === null || value === undefined || value === "") return "—";
  const minor = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(minor)) return "—";
  const major = minor / 100;
  const digits = Number.isInteger(major) ? 0 : 2;
  return reuse(`money:${currency}:${digits}`, () => new Intl.NumberFormat("en-IN", { style: "currency", currency, maximumFractionDigits: digits })).format(major);
}

/** Counts and percentages, which reached for `toLocaleString` and so paid the
 *  same construction cost per call. Three fraction digits is `Intl`'s own
 *  default, kept here so the printed figures are unchanged. */
export function formatCount(value: unknown, maximumFractionDigits = 3) {
  const parsed = typeof value === "number" ? value : Number(value ?? 0);
  if (!Number.isFinite(parsed)) return "0";
  return reuse(`count:${maximumFractionDigits}`, () => new Intl.NumberFormat("en-IN", { maximumFractionDigits })).format(parsed);
}

/** A parsed timestamp, in the one style the tables print. */
export function formatTimestamp(value: Date, timeZone?: string) {
  return reuse(`stamp:${timeZone ?? "local"}`, () => new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "short", timeZone })).format(value);
}

export function formatInstant(value: unknown, timeZone?: string) {
  if (typeof value !== "string" || !value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? "" : formatTimestamp(parsed, timeZone);
}

/** Convert a UTC ISO instant to the browser's wall clock for datetime-local. */
export function timestampInputValue(value: unknown) {
  if (typeof value !== "string" || !value) return "";
  const instant = new Date(value);
  if (Number.isNaN(instant.valueOf())) return "";
  const local = new Date(instant.getTime() - instant.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

/** Convert a browser-local wall clock value to the canonical UTC ISO instant. */
export function timestampInputToUtc(value: string) {
  if (!value) return null;
  const instant = new Date(value);
  return Number.isNaN(instant.valueOf()) ? null : instant.toISOString();
}

/** Accepts what people actually type: "1,500", "₹500", "1,50,000", "1.5". */
export function parseAmountToMinor(input: string): number | null {
  const cleaned = input.replace(/[₹,\s]/g, "");
  if (!cleaned || !/^\d*\.?\d*$/.test(cleaned)) return null;
  const major = Number(cleaned);
  if (!Number.isFinite(major) || major <= 0) return null;
  return Math.round(major * 100);
}

/** What the composer thinks you have typed, before anything is sent.
 *
 *  The server takes seconds to answer, and for most of that time the only
 *  question on the writer's mind is whether the amount was understood. That is
 *  answerable here, for free, on every keystroke — the same Indian notation the
 *  backend accepts is parseable in the browser.
 *
 *  Deliberately silent when unsure. A wrong reading is worse than none, so a
 *  bare number only counts as money when something in the sentence says it is:
 *  a rupee marker, a scale word, or a verb about paying or being paid. "Show my
 *  last 5 transactions" reads as nothing at all, which is correct. */
export type ComposerReading = { amountMinor: number; kind: "expense" | "income" | "transfer" | "investment" };

const SCALES: Array<[RegExp, number]> = [
  [/^(k|thousand)$/, 1_000],
  [/^(l|lakh|lakhs|lac|lacs)$/, 100_000],
  [/^(cr|crore|crores)$/, 10_000_000],
];

/** Nouns that follow a count, not an amount. */
const COUNTED = /^(transactions?|months?|days?|weeks?|years?|times?|people|items?|rows?|%)/;
const INCOME = /\b(salary|income|received|refund(ed)?|credited|earned|bonus|got paid|reimbursed)\b/;
const TRANSFER = /\b(transfer(red)?|moved|sent to|paid into)\b/;
const INVESTMENT = /\b(invest(ed|ment)?|sip|mutual fund|stocks?|shares?)\b/;
const MONEY_VERB = /\b(spent|spend|paid|pay|bought|buy|cost|got|received|earned|salary|income|refund(ed)?|transfer(red)?|invest(ed)?|deposit(ed)?|withdrew|withdraw|sent|credited|debited|bill|recharge)\b/;

export function readComposerEntry(input: string): ComposerReading | null {
  const text = input.toLowerCase();
  const match = /(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?)\s*([a-z]*)/.exec(text);
  if (!match) return null;

  const trailing = text.slice(match.index + match[0].length).trimStart();
  const unit = match[2] ?? "";
  const scale = SCALES.find(([pattern]) => pattern.test(unit))?.[1] ?? null;
  // "5 transactions" is a count. So is "5" followed by one, once the unit slot
  // has eaten a word that turned out not to be a scale.
  if (COUNTED.test(unit) || (!scale && unit === "" && COUNTED.test(trailing))) return null;
  if (unit && !scale && !/^(on|for|at|to|from|of|in)$/.test(unit)) return null;

  const marked = /₹|\brs\.?\b|\binr\b/.test(text);
  if (!marked && !scale && !MONEY_VERB.test(text)) return null;

  const major = Number(match[1].replace(/,/g, "")) * (scale ?? 1);
  if (!Number.isFinite(major) || major <= 0) return null;

  const kind = INVESTMENT.test(text) ? "investment" : TRANSFER.test(text) ? "transfer" : INCOME.test(text) ? "income" : "expense";
  return { amountMinor: Math.round(major * 100), kind };
}

export function parseNumber(input: string): number | null {
  const cleaned = input.replace(/[₹,%\s]/g, "");
  if (!cleaned) return null;
  const value = Number(cleaned);
  return Number.isFinite(value) ? value : null;
}

/** "2026-08-11" → "11 Aug 2026", "2026-08" → "Aug 2026", anything else unchanged. */
export function formatDay(value: unknown) {
  if (typeof value !== "string" || !value) return "";
  const day = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (day) return `${Number(day[3])} ${MONTHS[Number(day[2]) - 1]} ${day[1]}`;
  const month = /^(\d{4})-(\d{2})$/.exec(value);
  if (month) return `${MONTHS[Number(month[2]) - 1]} ${month[1]}`;
  return value;
}

/** Dimension values coming back from analysis queries are often raw period keys. */
export function formatDimension(value: unknown) {
  if (typeof value !== "string") return String(value);
  return /^\d{4}-\d{2}(-\d{2})?$/.test(value) ? formatDay(value) : value.replaceAll("_", " ");
}

export function formatRelative(iso: string, now = Date.now()) {
  const time = Date.parse(iso);
  if (!Number.isFinite(time)) return "";
  const minutes = Math.round((now - time) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return formatDay(new Date(time).toISOString().slice(0, 10));
}

export function formatDuration(value: unknown) {
  const milliseconds = typeof value === "number" ? value : Number(value ?? 0);
  if (!Number.isFinite(milliseconds)) return "—";
  if (milliseconds < 1) return "<1 ms";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  return `${(milliseconds / 1000).toFixed(milliseconds < 10_000 ? 2 : 1)} s`;
}

export function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
