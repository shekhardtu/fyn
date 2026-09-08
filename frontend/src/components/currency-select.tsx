import { Combobox } from "@/components/ui/combobox";
import { currencyOptions } from "@/lib/currencies";

export function CurrencySelect({ value, onChange, disabled, label = "Currency" }: { value: string; onChange: (value: string) => void; disabled?: boolean; label?: string }) {
  return <Combobox aria-label={label} value={value} displayValue={value} onValueChange={onChange} disabled={disabled} options={currencyOptions(value)} searchable searchPlaceholder="Search currencies" triggerClassName="h-11 w-24 shrink-0 rounded-xl border-secondary-line bg-surface/60 font-semibold text-secondary" />;
}
