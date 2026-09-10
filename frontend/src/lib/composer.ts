export type ComposerEffort = "auto" | "quick" | "thorough";

export const EFFORT_OPTIONS = [
  { value: "auto", label: "Auto", description: "Everyday questions and money entries", detail: "fyn chooses the effort for your question." },
  { value: "quick", label: "Quick", description: "Balances, totals, and simple lookups", detail: "Less thinking for straightforward questions." },
  { value: "thorough", label: "Thorough", description: "Spending patterns and what-if plans", detail: "More thinking for complex questions. May take longer." },
] as const;
