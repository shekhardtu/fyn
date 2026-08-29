import { describe, expect, it } from "vitest";
import { formatCount, formatMoney, formatTimestamp, formatTransactionClassification, readComposerEntry, timestampInputToUtc, timestampInputValue } from "@/lib/format";

/** The formatters are cached across calls, so these assert two things at once:
 *  the printed figure, and that a second call through the same cached formatter
 *  still prints it. A cache keyed too loosely would show up as the second
 *  expectation in each pair failing. */
describe("formatMoney", () => {
  it("prints whole rupees without decimals", () => {
    expect(formatMoney(250000)).toBe("₹2,500");
    expect(formatMoney(250000)).toBe("₹2,500");
  });

  it("groups lakhs the Indian way", () => {
    expect(formatMoney(25000000)).toBe("₹2,50,000");
  });

  it("keeps two decimals when there are paise", () => {
    expect(formatMoney(150050)).toBe("₹1,500.50");
    expect(formatMoney(150050)).toBe("₹1,500.50");
  });

  it("does not leak one currency's formatter into another", () => {
    expect(formatMoney(250000, "INR")).toBe("₹2,500");
    expect(formatMoney(250000, "USD")).toBe("$2,500");
    expect(formatMoney(250000, "INR")).toBe("₹2,500");
  });

  it("does not leak the whole-rupee formatter into an amount with paise", () => {
    expect(formatMoney(100)).toBe("₹1");
    expect(formatMoney(150)).toBe("₹1.50");
    expect(formatMoney(100)).toBe("₹1");
  });

  it("reads absent money as an em dash and a real zero as zero", () => {
    expect(formatMoney(null)).toBe("—");
    expect(formatMoney(undefined)).toBe("—");
    expect(formatMoney("")).toBe("—");
    expect(formatMoney("not money")).toBe("—");
    expect(formatMoney(Number.NaN)).toBe("—");
    expect(formatMoney(Number.POSITIVE_INFINITY)).toBe("—");
    expect(formatMoney(0)).toBe("₹0");
  });

  it("accepts a numeric string the way the widget payloads send it", () => {
    expect(formatMoney("250000")).toBe("₹2,500");
  });
});

describe("formatCount", () => {
  it("groups counts the Indian way", () => {
    expect(formatCount(1234567)).toBe("12,34,567");
    expect(formatCount(1234567)).toBe("12,34,567");
  });

  it("keeps Intl's three-digit default so printed figures are unchanged", () => {
    expect(formatCount(1234.5678)).toBe((1234.5678).toLocaleString("en-IN"));
  });

  it("honours a narrower fraction limit without disturbing the default", () => {
    expect(formatCount(12.345, 2)).toBe("12.35");
    expect(formatCount(1234.5678)).toBe((1234.5678).toLocaleString("en-IN"));
  });

  it("falls back to zero for what is not a number", () => {
    expect(formatCount(undefined)).toBe("0");
    expect(formatCount("not a number")).toBe("0");
  });
});

describe("formatTimestamp", () => {
  it("prints the medium date and short time the tables expect", () => {
    const stamp = new Date("2026-08-11T09:30:00Z");
    expect(formatTimestamp(stamp)).toBe(stamp.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }));
    expect(formatTimestamp(stamp)).toBe(stamp.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }));
  });

  it("projects a UTC instant into an explicit user timezone", () => {
    const stamp = new Date("2026-08-11T23:30:00Z");
    expect(formatTimestamp(stamp, "Asia/Kolkata")).toBe(
      stamp.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Kolkata" }),
    );
  });

  it("round-trips the browser-local editor value back to the same UTC instant", () => {
    const instant = "2026-08-11T09:30:00.000Z";
    expect(timestampInputToUtc(timestampInputValue(instant))).toBe(instant);
  });

  it("round-trips editor values through the profile timezone", () => {
    const instant = "2026-08-11T18:00:00.000Z";
    expect(timestampInputValue(instant, "Asia/Kolkata")).toBe("2026-08-11T23:30");
    expect(timestampInputToUtc("2026-08-11T23:30", "Asia/Kolkata")).toBe(instant);
  });

  it("rejects wall-clock times that do not exist during a DST jump", () => {
    expect(timestampInputToUtc("2026-03-08T02:30", "America/New_York")).toBeNull();
  });
});

describe("formatTransactionClassification", () => {
  it("keeps direction visible without repeating typed taxonomy roots", () => {
    expect(formatTransactionClassification("income", "Income", "Salary")).toBe("Income · Salary");
    expect(formatTransactionClassification("investment", "Investments", "Stocks")).toBe("Investment · Stocks");
  });

  it("shows both direction and an expense hierarchy", () => {
    expect(formatTransactionClassification("expense", "Food", "Dining")).toBe("Expense · Food → Dining");
  });

  it("does not conceal an incompatible legacy hierarchy", () => {
    expect(formatTransactionClassification("income", "Other", "Other")).toBe("Income · Other → Other");
  });
});

describe("readComposerEntry", () => {
  it("reads the Indian notation the backend already accepts", () => {
    expect(readComposerEntry("Spent 2000 on lunch")).toEqual({ amountMinor: 200_000, kind: "expense" });
    expect(readComposerEntry("₹2,000 for groceries")).toEqual({ amountMinor: 200_000, kind: "expense" });
    expect(readComposerEntry("20k rent")).toEqual({ amountMinor: 2_000_000, kind: "expense" });
    expect(readComposerEntry("Got 3 lakh salary today")).toEqual({ amountMinor: 30_000_000, kind: "income" });
    expect(readComposerEntry("1.5 cr invested in mutual funds")).toEqual({ amountMinor: 1_500_000_000, kind: "investment" });
    expect(readComposerEntry("Transferred 5000 to savings")).toEqual({ amountMinor: 500_000, kind: "transfer" });
  });

  it("stays silent rather than guessing wrong", () => {
    expect(readComposerEntry("Show my last 5 transactions")).toBeNull();
    expect(readComposerEntry("How much did I spend this month?")).toBeNull();
    expect(readComposerEntry("")).toBeNull();
    expect(readComposerEntry("remind me about 12")).toBeNull();
  });

  it("treats a rupee marker as enough on its own", () => {
    expect(readComposerEntry("₹450")).toEqual({ amountMinor: 45_000, kind: "expense" });
  });
});
