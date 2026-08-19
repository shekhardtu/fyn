import { ScanSearch } from "lucide-react";
import { useState } from "react";
import { formatCount, formatMoney } from "@/lib/format";
import { cn } from "@/lib/utils";

export type ExplorerSubcategory = {
  id: string;
  label: string;
  amountMinor: number;
  count: number;
  sharePercent: number;
};

export type ExplorerCategory = ExplorerSubcategory & {
  subcategories: ExplorerSubcategory[];
};

export function CategoryExplorer({
  categories,
  currency,
  eyebrow = "Expense explorer",
  title = "Where your money went",
  instruction = "Hover or select a category to scan its subcategories",
  emptyTitle = "No expenses recorded yet",
  emptyBody = "Once expenses are recorded, this view will rank the categories and reveal where each category went.",
}: {
  categories: ExplorerCategory[];
  currency: string;
  eyebrow?: string;
  title?: string;
  instruction?: string;
  emptyTitle?: string;
  emptyBody?: string;
}) {
  const [selectedId, setSelectedId] = useState(categories[0]?.id ?? "");
  const selected = categories.find((category) => category.id === selectedId) ?? categories[0];

  if (!selected) return <section className="rounded-xl border border-line bg-surface px-6 py-12 text-center">
    <span className="mx-auto grid size-10 place-items-center rounded-xl bg-secondary-tint text-secondary"><ScanSearch /></span>
    <h2 className="mt-4 font-heading text-title font-semibold text-ink">{emptyTitle}</h2>
    <p className="mx-auto mt-2 max-w-sm text-control leading-6 text-ink-muted">{emptyBody}</p>
  </section>;

  return <section aria-labelledby="category-explorer-title" className="overflow-hidden rounded-xl border border-line bg-line">
    <div className="bg-surface px-5 py-4 sm:px-6">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="ledger-meta">{eyebrow}</p>
          <h2 id="category-explorer-title" className="mt-1 font-heading text-title font-semibold tracking-[-0.02em] text-ink">{title}</h2>
        </div>
        <p className="text-note text-ink-muted">{instruction}</p>
      </div>
    </div>

    <div className="grid gap-px lg:grid-cols-[0.9fr_1.1fr]">
      <div className="bg-surface p-2 sm:p-3">
        <div role="group" className="space-y-1" aria-label="Expense categories">
          {categories.map((category) => {
            const active = category.id === selected.id;
            return <button
              key={category.id}
              type="button"
              aria-pressed={active}
              onClick={() => setSelectedId(category.id)}
              onFocus={() => setSelectedId(category.id)}
              onPointerEnter={(event) => { if (event.pointerType === "mouse") setSelectedId(category.id); }}
              className={cn("group relative w-full overflow-hidden rounded-lg px-3 py-3 text-left transition-colors duration-[110ms] hover:bg-surface-sunken", active && "bg-surface-sunken")}
            >
              <span aria-hidden className="absolute inset-y-2 left-0 w-0.5 rounded-full bg-secondary opacity-0 transition-opacity group-aria-pressed:opacity-100" />
              <span className="flex items-baseline justify-between gap-4">
                <span className={cn("min-w-0 truncate text-control font-medium", active ? "text-ink" : "text-ink-body")}>{category.label}</span>
                <span className="shrink-0 font-heading text-control font-semibold text-ink tabular-nums">{formatMoney(category.amountMinor, currency)}</span>
              </span>
              <span className="mt-2 flex items-center gap-3">
                <span className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-line" aria-hidden><span className="block h-full rounded-full bg-secondary" style={{ width: `${category.sharePercent ? Math.max(category.sharePercent, 2) : 0}%` }} /></span>
                <span className="w-10 text-right text-meta text-ink-muted tabular-nums">{formatCount(category.sharePercent, 1)}%</span>
              </span>
            </button>;
          })}
        </div>
      </div>

      <div className="bg-ground px-5 py-5 sm:px-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="ledger-meta">Inside {selected.label}</p>
            <p className="mt-1 font-heading text-[1.35rem] font-semibold tracking-[-0.03em] text-ink tabular-nums">{formatMoney(selected.amountMinor, currency)}</p>
          </div>
          <span className="rounded-md border border-line bg-surface px-2 py-1 text-meta font-medium text-ink-muted">{selected.count} transaction{selected.count === 1 ? "" : "s"}</span>
        </div>

        <div role="group" className="mt-6 space-y-4" aria-label={`${selected.label} subcategories`}>
          {selected.subcategories.map((subcategory) => <div key={subcategory.id}>
            <div className="flex items-baseline justify-between gap-4">
              <div className="min-w-0">
                <p className="truncate text-control font-medium text-ink-body">{subcategory.label}</p>
                <p className="mt-0.5 text-meta text-ink-muted">{subcategory.count} transaction{subcategory.count === 1 ? "" : "s"}</p>
              </div>
              <div className="shrink-0 text-right">
                <p className="font-heading text-control font-semibold text-ink tabular-nums">{formatMoney(subcategory.amountMinor, currency)}</p>
                <p className="mt-0.5 text-meta text-ink-muted tabular-nums">{formatCount(subcategory.sharePercent, 1)}%</p>
              </div>
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-line" aria-hidden><div className="h-full rounded-full bg-secondary/70" style={{ width: `${subcategory.sharePercent ? Math.max(subcategory.sharePercent, 2) : 0}%` }} /></div>
          </div>)}
        </div>
      </div>
    </div>
  </section>;
}
