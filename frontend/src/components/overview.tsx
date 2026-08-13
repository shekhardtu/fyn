"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowDownRight, ArrowRight, ArrowUpRight, CalendarDays, RotateCcw, ScanSearch } from "lucide-react";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { CategoryExplorer } from "@/components/category-explorer";
import { useWorkspaceShell } from "@/components/workspace";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { SiteHeader, useAutoHideSiteHeader } from "@/components/ui/site-header";
import { loadOverview } from "@/lib/api";
import { formatCount, formatMoney } from "@/lib/format";
import type { OverviewCategoryOut, OverviewOut } from "@/lib/generated/contracts";
import { cn } from "@/lib/utils";

function monthKey(value: Date) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}`;
}

function recentMonths(count = 12) {
  const now = new Date();
  return Array.from({ length: count }, (_, index) => {
    const value = new Date(now.getFullYear(), now.getMonth() - index, 1);
    return {
      value: monthKey(value),
      label: new Intl.DateTimeFormat("en-IN", { month: "long", year: "numeric" }).format(value),
    };
  });
}

function comparisonSentence(summary: OverviewOut["summary"], completeMonth: boolean) {
  if (summary.previousSpentMinor === 0) return "No comparable spending was recorded for the previous period.";
  if (summary.changeMinor === 0) return `The same as ${completeMonth ? "the previous month" : "this point last month"}.`;
  const direction = summary.changeMinor < 0 ? "less" : "more";
  const percent = summary.changePercent === null ? "" : ` (${formatCount(Math.abs(summary.changePercent), 1)}%)`;
  return `${formatMoney(Math.abs(summary.changeMinor), summary.currency)}${percent} ${direction} than ${completeMonth ? "the previous month" : "this point last month"}.`;
}

function OverviewSkeleton() {
  return <div role="status" aria-label="Loading your financial overview" className="space-y-5">
    <div className="grid gap-px overflow-hidden rounded-xl border border-line bg-line sm:grid-cols-3">
      {[0, 1, 2].map((item) => <div key={item} className="h-28 bg-surface p-5"><div className="h-2.5 w-20 animate-pulse rounded-full bg-line" /><div className="mt-5 h-6 w-28 animate-pulse rounded-full bg-line" /></div>)}
    </div>
    <div className="grid min-h-96 gap-px overflow-hidden rounded-xl border border-line bg-line lg:grid-cols-[0.9fr_1.1fr]">
      <div className="bg-surface p-5"><div className="h-3 w-32 animate-pulse rounded-full bg-line" /></div>
      <div className="bg-surface p-5"><div className="h-3 w-40 animate-pulse rounded-full bg-line" /></div>
    </div>
  </div>;
}

function MoneyStatement({ overview }: { overview: OverviewOut }) {
  const { summary, period } = overview;
  const items = [
    { label: "Income", value: summary.incomeMinor, tone: "text-money-in" },
    { label: "Spent", value: summary.spentMinor, tone: "text-money-out" },
    { label: "Net", value: summary.netMinor, tone: summary.netMinor < 0 ? "text-money-out" : "text-ink" },
  ];

  return <section aria-labelledby="money-statement-title" className="overflow-hidden rounded-xl border border-line bg-surface">
    <h2 id="money-statement-title" className="sr-only">Money statement for {period.label}</h2>
    <div className="grid divide-y divide-line sm:grid-cols-3 sm:divide-x sm:divide-y-0">
      {items.map((item) => <div key={item.label} className="px-5 py-4 sm:px-6 sm:py-5">
        <p className="ledger-meta">{item.label}</p>
        <p className={cn("mt-2 font-heading text-[clamp(1.35rem,3vw,1.75rem)] leading-none font-semibold tracking-[-0.035em] tabular-nums", item.tone)}>{formatMoney(item.value, summary.currency)}</p>
      </div>)}
    </div>
    <div className="flex flex-col gap-1 border-t border-line bg-ground px-5 py-3 text-note sm:flex-row sm:items-center sm:justify-between sm:px-6">
      <p className="flex items-center gap-1.5 font-medium text-ink-body">
        {summary.changeMinor < 0 ? <ArrowDownRight className="text-money-in" /> : summary.changeMinor > 0 ? <ArrowUpRight className="text-money-out" /> : <ArrowRight />}
        {comparisonSentence(summary, !period.isCurrent)}
      </p>
      <p className="text-ink-muted">{summary.expenseCount} expense{summary.expenseCount === 1 ? "" : "s"} · Recorded through {new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short" }).format(new Date(`${period.end}T12:00:00`))}</p>
    </div>
  </section>;
}

export function ExpenseBreakdown({ categories, currency }: { categories: OverviewCategoryOut[]; currency: string }) {
  return <CategoryExplorer categories={categories} currency={currency} />;
}

export function OverviewPage() {
  const router = useRouter();
  const shell = useWorkspaceShell();
  const months = useMemo(() => recentMonths(), []);
  const [month, setMonth] = useState(months[0].value);
  const { headerVisible, updateHeaderForScroll } = useAutoHideSiteHeader();
  const overview = useQuery({ queryKey: ["overview", month], queryFn: () => loadOverview(month) });
  const noFinancialData = overview.data && overview.data.summary.incomeMinor === 0 && overview.data.summary.spentMinor === 0;
  const latestConversation = shell.conversations[0];

  return <main id="main-content" onScroll={(event) => updateHeaderForScroll(event.currentTarget.scrollTop)} className="min-h-0 min-w-0 overflow-y-auto bg-ground">
    <SiteHeader title="Overview" subtitle="Your money, at a glance" subtitleClassName="hidden sm:block" hidden={!headerVisible} navOpen={shell.navOpen} onOpenNav={shell.openNav} end={<div className="relative">
        <CalendarDays className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-ink-muted" />
        <Combobox aria-label="Overview month" value={month} onValueChange={setMonth} options={months} searchPlaceholder="Search months" triggerClassName="h-9 w-auto pl-9 font-medium text-ink-body" />
      </div>} />

    <div className="mx-auto w-full max-w-[70rem] px-4 py-7 sm:px-6 sm:py-10 lg:px-8">
      <div className="mb-6">
        <p className="ledger-meta">Financial briefing</p>
        <h2 className="mt-2 font-heading text-[clamp(1.7rem,4vw,2.25rem)] leading-tight font-semibold tracking-[-0.04em] text-ink">Here’s where your money stands.</h2>
        <p className="mt-2 max-w-xl text-body leading-6 text-ink-muted">A grounded view of the income and expenses recorded in fyn AI.</p>
      </div>

      {overview.isPending ? <OverviewSkeleton /> : overview.isError ? <div role="alert" className="rounded-xl border border-danger-line bg-surface px-6 py-10 text-center">
        <h2 className="font-heading text-title font-semibold text-ink">We couldn’t open your overview</h2>
        <p className="mt-2 text-control text-ink-muted">Your records are safe. Try loading the briefing again.</p>
        <Button type="button" variant="outline" className="mt-5" onClick={() => overview.refetch()}><RotateCcw /> Try again</Button>
      </div> : noFinancialData ? <div className="rounded-xl border border-line bg-surface px-6 py-12 text-center">
        <span className="mx-auto grid size-11 place-items-center rounded-xl bg-secondary-tint text-secondary"><ScanSearch /></span>
        <h2 className="mt-4 font-heading text-title font-semibold text-ink">Your overview is ready for its first record</h2>
        <p className="mx-auto mt-2 max-w-md text-control leading-6 text-ink-muted">Record an expense or income in a conversation. Categories and subcategories will begin arranging themselves here automatically.</p>
        <Button type="button" className="mt-5" disabled={!latestConversation} onClick={() => latestConversation && router.push(`/c/${encodeURIComponent(latestConversation.id)}`)}>Open a conversation <ArrowRight /></Button>
      </div> : overview.data ? <div className="space-y-5">
        <MoneyStatement overview={overview.data} />
        <ExpenseBreakdown categories={overview.data.categories} currency={overview.data.summary.currency} />
      </div> : null}
    </div>
  </main>;
}
