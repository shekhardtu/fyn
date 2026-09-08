export const CURRENCY_OPTIONS = [
  { value: "INR", label: "Indian rupee (INR)" },
  { value: "USD", label: "US dollar (USD)" },
  { value: "EUR", label: "Euro (EUR)" },
  { value: "GBP", label: "Pound sterling (GBP)" },
  { value: "AED", label: "UAE dirham (AED)" },
  { value: "SGD", label: "Singapore dollar (SGD)" },
  { value: "AUD", label: "Australian dollar (AUD)" },
  { value: "CAD", label: "Canadian dollar (CAD)" },
  { value: "JPY", label: "Japanese yen (JPY)" },
  { value: "CNY", label: "Chinese yuan (CNY)" },
  { value: "CHF", label: "Swiss franc (CHF)" },
  { value: "NZD", label: "New Zealand dollar (NZD)" },
];

export function currencyOptions(current: string) {
  return CURRENCY_OPTIONS.some((option) => option.value === current) ? CURRENCY_OPTIONS : [{ value: current, label: current }, ...CURRENCY_OPTIONS];
}
