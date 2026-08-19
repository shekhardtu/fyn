import { useQuery } from "@tanstack/react-query";
import {
  ArrowDownLeft,
  ArrowDownRight,
  ArrowRight,
  ArrowUpRight,
  CalendarDays,
  CreditCard,
  Landmark,
  Plus,
  ReceiptText,
  RotateCcw,
  ScanSearch,
  Sparkles,
  Target,
  WalletCards,
} from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { Area, AreaChart, CartesianGrid, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { CategoryExplorer } from "@/components/category-explorer";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { SiteHeader, useAutoHideSiteHeader } from "@/components/ui/site-header";
import { useWorkspaceShell } from "@/components/workspace";
import { loadOverview } from "@/lib/api";
import { formatCount, formatMoney } from "@/lib/format";
import type { OverviewAccountOut, OverviewBudgetOut, OverviewCategoryOut, OverviewOut, OverviewTransactionOut, OverviewTrendPointOut } from "@/lib/generated/contracts";
import { cn } from "@/lib/utils";
import { appPaths } from "@/routing/paths";

export type TrendRange = "7" | "30" | "max";

const transactionDate = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short" });
const trendDate = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short" });
const budgetDate = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short" });
const compactFormatters = new Map<string, Intl.NumberFormat>();

function compactMoney(valueMinor: number, currency: string) {
  let formatter = compactFormatters.get(currency);
  if (!formatter) {
    formatter = new Intl.NumberFormat("en-IN", { style: "currency", currency, notation: "compact", maximumFractionDigits: 1 });
    compactFormatters.set(currency, formatter);
  }
  return formatter.format(valueMinor / 100);
}

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

function titleCase(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function daysInMonth(day: string) {
  const [year, month] = day.split("-").map(Number);
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

function percentChange(current: number, previous: number) {
  if (!previous) return null;
  return (current - previous) / Math.abs(previous) * 100;
}

function trendSentence(change: number | null, comparisonLabel: string) {
  if (change === null) return `No ${comparisonLabel} baseline`;
  if (change === 0) return `Unchanged from ${comparisonLabel}`;
  return `${formatCount(Math.abs(change), 1)}% ${change > 0 ? "higher" : "lower"} than ${comparisonLabel}`;
}

function OverviewSkeleton() {
  return <div role="status" aria-label="Loading your financial overview" className="space-y-5">
    <div className="h-24 animate-pulse rounded-xl border border-line bg-surface" />
    <div className="grid gap-5 lg:grid-cols-12">
      <div className="h-[28rem] animate-pulse rounded-xl border border-line bg-surface lg:col-span-8" />
      <div className="h-[28rem] animate-pulse rounded-xl border border-line bg-surface lg:col-span-4" />
    </div>
    <div className="grid gap-5 lg:grid-cols-3">
      {[0, 1, 2].map((item) => <div key={item} className="h-72 animate-pulse rounded-xl border border-line bg-surface" />)}
    </div>
  </div>;
}

function SectionHeading({ id, eyebrow, title, detail, action }: { id?: string; eyebrow: string; title: string; detail?: string; action?: ReactNode }) {
  return <div className="flex items-start justify-between gap-4">
    <div className="min-w-0">
      <p className="ledger-meta">{eyebrow}</p>
      <h2 id={id} className="mt-1 font-heading text-title font-semibold tracking-[-0.025em] text-ink">{title}</h2>
      {detail ? <p className="mt-1 text-note leading-5 text-ink-muted">{detail}</p> : null}
    </div>
    {action ? <div className="shrink-0">{action}</div> : null}
  </div>;
}

function AccountIcon({ type }: { type: string }) {
  if (type.includes("credit") || type.includes("card")) return <CreditCard size={17} />;
  if (type.includes("bank") || type.includes("saving") || type.includes("current")) return <Landmark size={17} />;
  return <WalletCards size={17} />;
}

function AccountStrip({ accounts, onPlan }: { accounts: OverviewAccountOut[]; onPlan: () => void }) {
  return <section aria-labelledby="linked-accounts-title" className="rounded-xl border border-line bg-surface px-5 py-4 sm:px-6">
    <div className="flex flex-col gap-4 lg:flex-row lg:items-center">
      <div className="flex min-w-[11rem] items-center gap-3 lg:border-r lg:border-line lg:pr-6">
        <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-secondary-tint text-secondary"><WalletCards size={18} /></span>
        <div>
          <p className="ledger-meta">Accounts</p>
          <h2 id="linked-accounts-title" className="mt-0.5 font-heading text-control font-semibold text-ink">{accounts.length ? `${accounts.length} recorded` : "None recorded"}</h2>
        </div>
      </div>

      {accounts.length ? <div className="grid min-w-0 flex-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {accounts.slice(0, 3).map((account) => <div key={account.id} className="flex min-w-0 items-center gap-3 rounded-lg bg-ground px-3 py-2.5">
          <span className="grid size-8 shrink-0 place-items-center rounded-lg border border-line bg-surface text-secondary"><AccountIcon type={account.accountType} /></span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-note font-semibold text-ink">{account.name}</p>
            <p className="truncate text-meta text-ink-muted">{[account.institution, account.mask ? `•••• ${account.mask}` : titleCase(account.accountType)].filter(Boolean).join(" · ")}</p>
          </div>
          <p className="shrink-0 font-heading text-note font-semibold tabular-nums text-ink">{formatMoney(account.balanceMinor, account.currency)}</p>
        </div>)}
      </div> : <p className="min-w-0 flex-1 text-control text-ink-muted">Add an account balance in a conversation to see your cash position here.</p>}

      <Button type="button" variant="outline" size="sm" onClick={onPlan}><Plus /> {accounts.length ? "Add account" : "Record account"}</Button>
    </div>
  </section>;
}

export type ChartPoint = OverviewTrendPointOut & {
  income: number;
  expenses: number;
  balance: number;
  previousExpenses: number;
};

export function cumulativeTrend(points: OverviewTrendPointOut[]): ChartPoint[] {
  let income = 0;
  let expenses = 0;
  let previousExpenses = 0;
  return points.map((point) => {
    income += point.incomeMinor;
    expenses += point.spentMinor;
    previousExpenses += point.previousSpentMinor;
    return { ...point, income, expenses, balance: income - expenses, previousExpenses };
  });
}

export function visibleTrend(points: ChartPoint[], range: TrendRange) {
  return range === "max" ? points : points.slice(-Number(range));
}

function TrendChart({ overview }: { overview: OverviewOut }) {
  const [range, setRange] = useState<TrendRange>("7");
  const allPoints = useMemo(() => cumulativeTrend(overview.trend), [overview.trend]);
  const visiblePoints = visibleTrend(allPoints, range);
  const currency = overview.summary.currency;
  const previousLabel = new Intl.DateTimeFormat("en-IN", { month: "short" }).format(new Date(`${overview.period.previousStart}T12:00:00`));

  return <section aria-labelledby="cash-flow-title" className="min-w-0 rounded-xl border border-line bg-surface p-5 sm:p-6 lg:col-span-8">
    <SectionHeading
      id="cash-flow-title"
      eyebrow="Cash flow"
      title="Your month in motion"
      detail={`Cumulative movement through ${trendDate.format(new Date(`${overview.period.end}T12:00:00`))}`}
      action={<div role="group" aria-label="Trend range" className="flex rounded-lg border border-line bg-ground p-0.5">
        {(["7", "30", "max"] as const).map((option) => <button
          key={option}
          type="button"
          aria-pressed={range === option}
          onClick={() => setRange(option)}
          className={cn("hit-target h-7 rounded-md px-2.5 text-meta font-semibold text-ink-muted transition-colors", range === option && "bg-surface text-ink")}
        >{option === "max" ? "Max" : `${option}d`}</button>)}
      </div>}
    />

    <div className="mt-5 flex flex-wrap gap-x-5 gap-y-2 text-meta text-ink-muted" aria-label="Cash flow chart legend">
      <span className="flex items-center gap-1.5"><i className="size-2 rounded-full bg-money-in" />Income</span>
      <span className="flex items-center gap-1.5"><i className="size-2 rounded-full bg-money-out" />Expenses</span>
      <span className="flex items-center gap-1.5"><i className="size-2 rounded-full bg-secondary" />Balance</span>
      <span className="flex items-center gap-1.5"><i className="h-px w-3 bg-ink-muted" />{previousLabel} expenses</span>
    </div>

    <div className="mt-4 h-[17rem] min-w-0" role="img" aria-label={`Income, expenses, balance and previous-month spending trend for ${overview.period.label}`}>
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <AreaChart data={visiblePoints} accessibilityLayer margin={{ top: 8, right: 4, left: -10, bottom: 0 }}>
          <defs>
            <linearGradient id="overviewBalanceFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--secondary)" stopOpacity={0.2} />
              <stop offset="100%" stopColor="var(--secondary)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="var(--line-soft)" vertical={false} />
          <XAxis dataKey="date" tickFormatter={(value) => trendDate.format(new Date(`${value}T12:00:00`))} tick={{ fill: "var(--ink-muted)", fontSize: 11 }} axisLine={{ stroke: "var(--line)" }} tickLine={false} minTickGap={28} />
          <YAxis tickFormatter={(value) => compactMoney(Number(value), currency)} tick={{ fill: "var(--ink-muted)", fontSize: 11 }} axisLine={false} tickLine={false} width={68} />
          <Tooltip
            labelFormatter={(value) => trendDate.format(new Date(`${value}T12:00:00`))}
            formatter={(value, name) => [formatMoney(Number(value), currency), String(name)]}
            contentStyle={{ border: "1px solid var(--line)", borderRadius: 10, background: "var(--surface)", boxShadow: "var(--shadow-overlay)", fontSize: 12 }}
          />
          <Area type="monotone" dataKey="balance" name="Balance" stroke="var(--secondary)" strokeWidth={2.5} fill="url(#overviewBalanceFill)" isAnimationActive={false} />
          <Line type="monotone" dataKey="income" name="Income" stroke="var(--money-in)" strokeWidth={2} dot={false} isAnimationActive={false} />
          <Line type="monotone" dataKey="expenses" name="Expenses" stroke="var(--money-out)" strokeWidth={2} dot={false} isAnimationActive={false} />
          <Line type="monotone" dataKey="previousExpenses" name={`${previousLabel} expenses`} stroke="var(--ink-muted)" strokeWidth={1.5} strokeDasharray="5 5" dot={false} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  </section>;
}

function MetricRow({ label, value, previous, currency, direction, last }: {
  label: string;
  value: number;
  previous: number;
  currency: string;
  direction: "higher-good" | "lower-good";
  last?: boolean;
}) {
  const change = percentChange(value, previous);
  const difference = value - previous;
  const favorable = difference === 0 || (direction === "higher-good" ? difference > 0 : difference < 0);
  const Icon = change === null ? ArrowRight : difference > 0 ? ArrowUpRight : difference < 0 ? ArrowDownRight : ArrowRight;
  return <div className={cn("px-5 py-5 sm:px-6", !last && "border-b border-line")}>
    <p className="ledger-meta">{label}</p>
    <p className={cn("mt-2 font-heading text-[clamp(1.45rem,3vw,1.85rem)] leading-none font-semibold tracking-[-0.04em] tabular-nums", label === "Income" ? "text-money-in" : label === "Expenses" ? "text-money-out" : value < 0 ? "text-money-out" : "text-ink")}>{formatMoney(value, currency)}</p>
    <p className={cn("mt-3 flex items-center gap-1.5 text-note", favorable ? "text-money-in" : "text-money-out")}>
      <Icon size={14} />
      <span>{trendSentence(change, "last month")}</span>
    </p>
  </div>;
}

function MoneySummary({ overview }: { overview: OverviewOut }) {
  const previousIncome = overview.trend.reduce((sum, point) => sum + point.previousIncomeMinor, 0);
  const previousBalance = previousIncome - overview.summary.previousSpentMinor;
  return <section aria-label={`Money summary for ${overview.period.label}`} className="overflow-hidden rounded-xl border border-line bg-surface lg:col-span-4">
    <MetricRow label="Income" value={overview.summary.incomeMinor} previous={previousIncome} currency={overview.summary.currency} direction="higher-good" />
    <MetricRow label="Expenses" value={overview.summary.spentMinor} previous={overview.summary.previousSpentMinor} currency={overview.summary.currency} direction="lower-good" />
    <MetricRow label="Balance" value={overview.summary.netMinor} previous={previousBalance} currency={overview.summary.currency} direction="higher-good" last />
  </section>;
}

function BudgetBar({ budget, label }: { budget: OverviewBudgetOut; label: string }) {
  const over = budget.overMinor > 0;
  return <div>
    <div className="flex items-baseline justify-between gap-3">
      <p className="truncate text-note font-semibold text-ink-body">{label}</p>
      <p className={cn("shrink-0 text-meta font-semibold tabular-nums", over ? "text-money-out" : "text-ink-muted")}>{formatCount(budget.percentUsed, 0)}%</p>
    </div>
    <div
      role="progressbar"
      aria-label={`${label} budget used`}
      aria-valuemin={0}
      aria-valuemax={Math.max(100, Math.ceil(budget.percentUsed))}
      aria-valuenow={budget.percentUsed}
      className="mt-2 h-1.5 overflow-hidden rounded-full bg-line"
    >
      <span className={cn("block h-full rounded-full", over ? "bg-money-out" : "bg-secondary")} style={{ width: `${Math.min(100, budget.percentUsed)}%` }} />
    </div>
    <div className="mt-1.5 flex items-baseline justify-between gap-3 text-meta">
      <p className="text-ink-muted">{formatMoney(budget.spentMinor, budget.currency)} of {formatMoney(budget.amountMinor, budget.currency)}</p>
      <p className={cn("shrink-0 font-medium", over ? "text-money-out" : "text-money-in")}>{formatMoney(over ? budget.overMinor : budget.remainingMinor, budget.currency)} {over ? "over" : "left"}</p>
    </div>
  </div>;
}

function BudgetEmptyAction({ title, detail, onPlan }: { title: string; detail: string; onPlan: () => void }) {
  return <button
    type="button"
    onClick={onPlan}
    className="group mt-4 flex w-full items-center gap-3 rounded-lg border border-dashed border-line-strong bg-ground p-4 text-left transition-colors hover:border-secondary hover:bg-secondary-tint"
  >
    <span className="grid size-9 shrink-0 place-items-center rounded-lg border border-line bg-surface text-secondary transition-colors group-hover:border-secondary"><Plus size={17} /></span>
    <span className="min-w-0">
      <span className="block text-note font-semibold text-ink">{title}</span>
      <span className="mt-0.5 block text-meta leading-5 text-ink-muted">{detail}</span>
    </span>
  </button>;
}

function OverallBudgetPanel({ budget, overview, onPlan }: { budget: OverviewBudgetOut; overview: OverviewOut; onPlan: () => void }) {
  const elapsedDays = Math.max(1, Number(overview.period.end.slice(-2)));
  const monthDays = daysInMonth(overview.period.end);
  const dailyAverage = Math.round(budget.spentMinor / elapsedDays / 100) * 100;
  const dailyBudget = Math.round(budget.amountMinor / monthDays / 100) * 100;
  const projected = overview.period.isCurrent ? Math.round(dailyAverage * monthDays) : budget.spentMinor;
  const paceRatio = dailyBudget ? dailyAverage / dailyBudget : 0;
  const chartMax = Math.max(dailyBudget, ...overview.trend.map((point) => point.spentMinor), 1);
  const budgetLine = Math.max(4, Math.min(100, dailyBudget / chartMax * 100));
  const crossedOn = overview.trend.reduce<{ spent: number; point: OverviewTrendPointOut | null }>((state, point) => {
    if (state.point) return state;
    const spent = state.spent + point.spentMinor;
    return { spent, point: spent > budget.amountMinor ? point : null };
  }, { spent: 0, point: null }).point;
  const projectedVariance = projected - budget.amountMinor;
  const status = budget.overMinor > 0
    ? crossedOn ? `Budget crossed on ${budgetDate.format(new Date(`${crossedOn.date}T12:00:00`))}` : "The monthly budget has been crossed"
    : !overview.period.isCurrent
      ? `Month closed ${formatMoney(budget.remainingMinor, budget.currency)} within budget`
    : projectedVariance > 0
      ? `At this pace, spending may finish ${formatMoney(projectedVariance, budget.currency)} over budget`
      : `At this pace, spending may finish ${formatMoney(Math.abs(projectedVariance), budget.currency)} within budget`;
  const over = budget.overMinor > 0;
  const paceLabel = paceRatio > 1 ? `${formatCount(paceRatio, 1)}× pace` : `${formatCount(paceRatio * 100, 0)}% pace`;

  return <div className="mt-5 rounded-lg bg-ground p-4">
    <div className="flex items-center justify-between gap-3">
      <p className="text-note font-semibold text-ink-body">Overall spending</p>
      <Button type="button" variant="link" size="sm" className="h-auto shrink-0 px-0 py-0" onClick={onPlan}>Edit</Button>
    </div>
    <div className="mt-2 flex items-baseline justify-between gap-3">
      <p className="text-note tabular-nums text-ink-muted">{formatMoney(budget.spentMinor, budget.currency)} of {formatMoney(budget.amountMinor, budget.currency)}</p>
      <p className={cn("shrink-0 text-meta font-semibold tabular-nums", over || paceRatio > 1 ? "text-money-out" : "text-money-in")}>
        {formatCount(budget.percentUsed, 0)}% <span aria-hidden>·</span> {paceLabel}
      </p>
    </div>
    <div
      role="progressbar"
      aria-label="Overall spending budget used"
      aria-valuemin={0}
      aria-valuemax={Math.max(100, Math.ceil(budget.percentUsed))}
      aria-valuenow={budget.percentUsed}
      className="mt-2 h-1.5 overflow-hidden rounded-full bg-line"
    >
      <span className={cn("block h-full rounded-full", over ? "bg-money-out" : "bg-secondary")} style={{ width: `${Math.min(100, budget.percentUsed)}%` }} />
    </div>
    <p aria-label="Budget status" className="mt-2 flex flex-wrap items-baseline gap-x-1 text-meta text-ink-muted">
      <span className={cn("font-medium", over ? "text-money-out" : "text-money-in")}>{formatMoney(over ? budget.overMinor : budget.remainingMinor, budget.currency)} {over ? "over" : "left"}</span>
      <span>· {overview.summary.expenseCount} expense{overview.summary.expenseCount === 1 ? "" : "s"}</span>
      {over ? <span className="font-medium text-money-out">· {status}</span> : null}
    </p>

    <div role="group" aria-label="Spending pace" className="mt-3.5 border-t border-line pt-3.5">
      <div className="flex items-baseline justify-between gap-3">
        <p className="text-meta font-semibold text-ink">Spending pace</p>
        <p className="text-meta text-ink-muted">Daily spend vs {formatMoney(dailyBudget, budget.currency)}/day</p>
      </div>
      <div role="img" aria-label="Daily spending bars with the daily budget pace marker" className="relative mt-2 flex h-7 items-end gap-0.5 overflow-hidden rounded-sm">
        <span aria-hidden className="absolute inset-x-0 z-10 border-t border-dashed border-secondary/70" style={{ bottom: `${budgetLine}%` }} />
        {overview.trend.map((point) => <span
          key={point.date}
          aria-hidden
          className={cn("min-w-px flex-1 rounded-t-[2px]", point.spentMinor > dailyBudget ? "bg-money-out" : "bg-secondary")}
          style={{ height: point.spentMinor ? `${Math.max(5, point.spentMinor / chartMax * 100)}%` : "2px" }}
        />)}
      </div>
      <div className="mt-2.5 grid grid-cols-3 divide-x divide-line border-y border-line py-2">
        <div className="pr-2"><p className="text-meta text-ink-muted">Average/day</p><p className="mt-0.5 truncate text-note font-semibold tabular-nums text-ink">{formatMoney(dailyAverage, budget.currency)}</p></div>
        <div className="px-2.5"><p className="text-meta text-ink-muted">Budget/day</p><p className="mt-0.5 truncate text-note font-semibold tabular-nums text-ink">{formatMoney(dailyBudget, budget.currency)}</p></div>
        <div className="pl-2.5"><p className="text-meta text-ink-muted">{overview.period.isCurrent ? "Projected" : "Month total"}</p><p className={cn("mt-0.5 truncate text-note font-semibold tabular-nums", projected > budget.amountMinor ? "text-money-out" : "text-money-in")}>{formatMoney(projected, budget.currency)}</p></div>
      </div>
      {!over ? <p className={cn("mt-2 text-meta font-medium", projectedVariance > 0 ? "text-money-out" : "text-money-in")}>{status}</p> : null}
    </div>
  </div>;
}

export function SpendingLimit({ overview, onPlan }: { overview: OverviewOut; onPlan: () => void }) {
  const [scope, setScope] = useState<"overall" | "categories">("overall");
  const budgets = overview.budgets ?? [];
  const overall = budgets.find((budget) => budget.categoryId === null);
  const categoryBudgets = budgets
    .filter((budget) => budget.categoryId !== null)
    .sort((left, right) => right.percentUsed - left.percentUsed || (left.category ?? left.name).localeCompare(right.category ?? right.name));
  const categoryTitle = categoryBudgets.length
    ? `${categoryBudgets.length} category budget${categoryBudgets.length === 1 ? "" : "s"}`
    : "No category budgets";
  return <section aria-labelledby="spending-limit-title" className="rounded-xl border border-line bg-surface p-5 sm:p-6">
    <SectionHeading
      id="spending-limit-title"
      eyebrow="Monthly spending limit"
      title={scope === "overall" ? (overall ? `${formatMoney(overall.amountMinor, overall.currency)} limit` : "No overall limit") : categoryTitle}
      action={<div role="group" aria-label="Budget scope" className="flex rounded-lg border border-line bg-ground p-0.5">
        {(["overall", "categories"] as const).map((option) => <button
          key={option}
          type="button"
          aria-pressed={scope === option}
          onClick={() => setScope(option)}
          className={cn("hit-target h-7 rounded-md px-2.5 text-meta font-semibold text-ink-muted transition-colors", scope === option && "bg-surface text-ink")}
        >{option === "overall" ? "Overall" : "Category"}</button>)}
      </div>}
    />

    {scope === "overall" ? (overall ? <OverallBudgetPanel budget={overall} overview={overview} onPlan={onPlan} /> : <BudgetEmptyAction
      title="Set overall budget"
      detail={`${formatMoney(overview.summary.spentMinor, overview.summary.currency)} spent in ${overview.period.label} · ${overview.summary.expenseCount} expense${overview.summary.expenseCount === 1 ? "" : "s"}`}
      onPlan={onPlan}
    />) : <div className="mt-5">
      {categoryBudgets.length ? <>
        <div className="flex items-center justify-between gap-3">
          <p className="text-meta leading-5 text-ink-muted">Category limits stay independent from the overall cap.</p>
          <Button type="button" variant="ghost" size="sm" className="shrink-0" onClick={onPlan}><Plus /> Add category</Button>
        </div>
        <div className="mt-3 max-h-52 space-y-3 overflow-y-auto pr-1">
          {categoryBudgets.map((budget) => <div key={budget.id} className="rounded-lg bg-ground p-3.5"><BudgetBar budget={budget} label={budget.category ?? budget.name} /></div>)}
        </div>
      </> : <BudgetEmptyAction
        title="Add category budget"
        detail="Set an independent limit for Food, Transport, Shopping, or another category."
        onPlan={onPlan}
      />}
    </div>}
  </section>;
}

function FinancialHealth({ overview }: { overview: OverviewOut }) {
  const rawSavingsRate = overview.summary.incomeMinor ? overview.summary.netMinor / overview.summary.incomeMinor * 100 : 0;
  const savingsRate = Math.max(0, Math.min(100, rawSavingsRate));
  const daysRemaining = overview.period.isCurrent ? Math.max(0, daysInMonth(overview.period.end) - Number(overview.period.end.slice(-2))) : 0;
  const healthLabel = overview.summary.incomeMinor === 0 ? "Income not recorded" : rawSavingsRate >= 20 ? "Healthy cushion" : rawSavingsRate >= 0 ? "Watch your pace" : "Spending is ahead";

  return <section aria-labelledby="financial-health-title" className="rounded-xl border border-line bg-surface p-5 sm:p-6">
    <SectionHeading id="financial-health-title" eyebrow="Financial health" title={healthLabel} detail={overview.period.isCurrent ? `${daysRemaining} day${daysRemaining === 1 ? "" : "s"} remaining this month` : "Completed month"} />
    <div className="mt-6 flex items-center gap-5">
      <div className="relative grid size-28 shrink-0 place-items-center rounded-full" style={{ background: `conic-gradient(var(--money-in) ${savingsRate}%, var(--line) 0)` }}>
        <div className="grid size-[5.4rem] place-items-center rounded-full bg-surface text-center">
          <div><p className="font-heading text-title font-semibold tabular-nums text-ink">{formatCount(rawSavingsRate, 0)}%</p><p className="text-meta text-ink-muted">saved</p></div>
        </div>
      </div>
      <div className="min-w-0 flex-1 space-y-3">
        <div><p className="text-meta text-ink-muted">Kept this month</p><p className={cn("mt-0.5 font-heading text-control font-semibold tabular-nums", overview.summary.netMinor < 0 ? "text-money-out" : "text-money-in")}>{formatMoney(overview.summary.netMinor, overview.summary.currency)}</p></div>
        <div className="border-t border-line pt-3"><p className="text-meta text-ink-muted">Average daily spend</p><p className="mt-0.5 font-heading text-control font-semibold tabular-nums text-ink">{formatMoney(Math.round(overview.summary.spentMinor / Math.max(1, Number(overview.period.end.slice(-2)))), overview.summary.currency)}</p></div>
      </div>
    </div>
  </section>;
}

function GoalTracker({ onPlan }: { onPlan: () => void }) {
  return <section aria-labelledby="goal-tracker-title" className="rounded-xl border border-line bg-surface p-5 sm:p-6">
    <SectionHeading id="goal-tracker-title" eyebrow="Goal tracker" title="Make the next rupee intentional" action={<Button type="button" variant="ghost" size="sm" onClick={onPlan}><Plus /> Add goal</Button>} />
    <div className="mt-6 flex items-start gap-4 rounded-lg border border-dashed border-line-strong bg-ground p-4">
      <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-secondary-tint text-secondary"><Target size={18} /></span>
      <div>
        <h3 className="text-control font-semibold text-ink">No active goals yet</h3>
        <p className="mt-1 text-note leading-5 text-ink-muted">Tell fyn what you are saving for and build a target that fits your cash flow.</p>
        <Button type="button" variant="link" size="sm" className="mt-2 px-0" onClick={onPlan}>Plan with fyn <ArrowRight /></Button>
      </div>
    </div>
  </section>;
}

function CostAnalysis({ categories, currency, onViewAll }: { categories: OverviewCategoryOut[]; currency: string; onViewAll: () => void }) {
  const visible = categories.slice(0, 5);
  const total = categories.reduce((sum, category) => sum + category.amountMinor, 0);
  return <section aria-labelledby="cost-analysis-title" className="rounded-xl border border-line bg-surface p-5 sm:p-6 lg:col-span-2">
    <SectionHeading id="cost-analysis-title" eyebrow="Cost analysis" title="Where it went" detail={`${categories.length} categor${categories.length === 1 ? "y" : "ies"} this month`} action={<Button type="button" variant="ghost" size="sm" onClick={onViewAll}>Explore <ArrowRight /></Button>} />
    <p className="mt-5 font-heading text-[1.65rem] font-semibold tracking-[-0.04em] tabular-nums text-ink">{formatMoney(total, currency)}</p>
    <div className="mt-3 flex h-2 overflow-hidden rounded-full bg-line" aria-hidden>
      {visible.map((category, index) => <span key={category.id} className="h-full bg-secondary" style={{ width: `${category.sharePercent}%`, opacity: Math.max(0.35, 1 - index * 0.13) }} />)}
    </div>
    <div className="mt-5 space-y-3.5">
      {visible.map((category, index) => <div key={category.id}>
        <div className="flex items-baseline justify-between gap-4">
          <p className="flex min-w-0 items-center gap-2 text-note font-medium text-ink-body"><i className="size-2 shrink-0 rounded-full bg-secondary" style={{ opacity: Math.max(0.35, 1 - index * 0.13) }} /><span className="truncate">{category.label}</span></p>
          <p className="shrink-0 text-note font-semibold tabular-nums text-ink">{formatMoney(category.amountMinor, currency)}</p>
        </div>
        <div className="mt-1.5 flex items-center gap-3"><span className="h-1 min-w-0 flex-1 overflow-hidden rounded-full bg-line"><span className="block h-full rounded-full bg-secondary" style={{ width: `${category.sharePercent}%` }} /></span><span className="w-9 text-right text-meta tabular-nums text-ink-muted">{formatCount(category.sharePercent, 0)}%</span></div>
      </div>)}
    </div>
  </section>;
}

function transactionDirection(transaction: OverviewTransactionOut) {
  if (["income", "refund", "reimbursement", "cash_deposit"].includes(transaction.transactionType)) return { incoming: true, Icon: ArrowDownLeft };
  return { incoming: false, Icon: transaction.transactionType === "expense" ? ReceiptText : ArrowUpRight };
}

function TransactionHistory({ transactions, onViewAll }: { transactions: OverviewTransactionOut[]; onViewAll: () => void }) {
  return <section aria-labelledby="transaction-history-title" className="overflow-hidden rounded-xl border border-line bg-surface lg:col-span-2">
    <div className="px-5 pt-5 pb-3 sm:px-6 sm:pt-6">
      <SectionHeading id="transaction-history-title" eyebrow="Recent activity" title="Transaction history" action={<Button type="button" variant="ghost" size="sm" onClick={onViewAll}>View all <ArrowRight /></Button>} />
    </div>
    {transactions.length ? <div className="divide-y divide-line">
      {transactions.map((transaction) => {
        const { incoming, Icon } = transactionDirection(transaction);
        return <button key={transaction.id} type="button" onClick={onViewAll} className="grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 px-5 py-3 text-left transition-colors hover:bg-ground sm:px-6">
          <span className={cn("grid size-9 place-items-center rounded-lg bg-ground", incoming ? "text-money-in" : "text-money-out")}><Icon size={16} /></span>
          <span className="min-w-0">
            <span className="block truncate text-note font-semibold text-ink">{transaction.merchant ?? titleCase(transaction.transactionType)}</span>
            <span className="mt-0.5 block truncate text-meta text-ink-muted">{[transaction.category ?? titleCase(transaction.transactionType), transaction.account, transactionDate.format(new Date(transaction.transactionAt))].filter(Boolean).join(" · ")}</span>
          </span>
          <span className={cn("font-heading text-note font-semibold tabular-nums", incoming ? "text-money-in" : "text-money-out")}>{incoming ? "+" : "−"}{formatMoney(transaction.amountMinor, transaction.currency)}</span>
        </button>;
      })}
    </div> : <div className="px-6 py-12 text-center"><ReceiptText className="mx-auto text-ink-muted" /><p className="mt-3 text-control font-semibold text-ink">No transactions this month</p><p className="mt-1 text-note text-ink-muted">New entries will appear here.</p></div>}
  </section>;
}

export function ExpenseBreakdown({ categories, currency }: { categories: OverviewCategoryOut[]; currency: string }) {
  return <CategoryExplorer categories={categories} currency={currency} />;
}

export function OverviewPage() {
  const navigate = useNavigate();
  const shell = useWorkspaceShell();
  const months = useMemo(() => recentMonths(), []);
  const [params, setParams] = useSearchParams();
  const monthParam = params.get("month");
  const month = months.some((option) => option.value === monthParam) ? monthParam as string : months[0].value;
  function setMonth(next: string) {
    setParams((previous) => {
      const merged = new URLSearchParams(previous);
      if (next === months[0].value) merged.delete("month"); else merged.set("month", next);
      return merged;
    });
  }
  const { headerVisible, updateHeaderForScroll } = useAutoHideSiteHeader();
  const overview = useQuery({ queryKey: ["overview", month], queryFn: () => loadOverview(month) });
  const noFinancialData = overview.data && overview.data.summary.incomeMinor === 0 && overview.data.summary.spentMinor === 0 && overview.data.accounts.length === 0 && (overview.data.budgets?.length ?? 0) === 0;
  const latestConversation = shell.conversations[0];
  const openPlanner = () => latestConversation && navigate(appPaths.conversation(latestConversation.id));

  return <main id="main-content" onScroll={(event) => updateHeaderForScroll(event.currentTarget.scrollTop)} className="min-h-0 min-w-0 overflow-y-auto bg-ground">
    <SiteHeader title="Overview" subtitle="Your money, at a glance" subtitleClassName="hidden sm:block" hidden={!headerVisible} navOpen={shell.navOpen} onOpenNav={shell.openNav} end={<div className="relative">
      <CalendarDays className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-ink-muted" />
      <Combobox aria-label="Overview month" value={month} onValueChange={setMonth} options={months} searchPlaceholder="Search months" triggerClassName="h-9 w-auto pl-9 font-medium text-ink-body" />
    </div>} />

    <div className="mx-auto w-full max-w-[86rem] px-4 py-7 sm:px-6 sm:py-9 lg:px-8">
      <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="ledger-meta">Financial briefing · {overview.data?.period.label ?? "Loading"}</p>
          <h2 className="mt-2 font-heading text-[clamp(1.7rem,4vw,2.35rem)] leading-tight font-semibold tracking-[-0.045em] text-ink">Here’s where your money stands.</h2>
          <p className="mt-2 max-w-xl text-body leading-6 text-ink-muted">Cash flow, spending pace, and the records that shaped your month.</p>
        </div>
        {overview.data ? <p className="flex items-center gap-2 text-note text-ink-muted"><Sparkles size={14} className="text-secondary" /> Updated through {trendDate.format(new Date(`${overview.data.period.end}T12:00:00`))}</p> : null}
      </div>

      {overview.isPending ? <OverviewSkeleton /> : overview.isError ? <div role="alert" className="rounded-xl border border-danger-line bg-surface px-6 py-10 text-center">
        <h2 className="font-heading text-title font-semibold text-ink">We couldn’t open your overview</h2>
        <p className="mt-2 text-control text-ink-muted">Your records are safe. Try loading the briefing again.</p>
        <Button type="button" variant="outline" className="mt-5" onClick={() => overview.refetch()}><RotateCcw /> Try again</Button>
      </div> : noFinancialData ? <div className="rounded-xl border border-line bg-surface px-6 py-12 text-center">
        <span className="mx-auto grid size-11 place-items-center rounded-xl bg-secondary-tint text-secondary"><ScanSearch /></span>
        <h2 className="mt-4 font-heading text-title font-semibold text-ink">Your overview is ready for its first record</h2>
        <p className="mx-auto mt-2 max-w-md text-control leading-6 text-ink-muted">Record income, an expense, or an account balance in a conversation. Your cash-flow dashboard will arrange itself here automatically.</p>
        <Button type="button" className="mt-5" disabled={!latestConversation} onClick={openPlanner}>Open a conversation <ArrowRight /></Button>
        {!latestConversation ? <p className="mt-2 text-note text-ink-muted">Your first conversation is still being prepared — this lights up in a moment.</p> : null}
      </div> : overview.data ? <div className="space-y-5">
        <AccountStrip accounts={overview.data.accounts} onPlan={openPlanner} />
        <div className="grid gap-5 lg:grid-cols-12">
          <TrendChart overview={overview.data} />
          <MoneySummary overview={overview.data} />
        </div>
        <div className="grid gap-5 md:grid-cols-2 xl:grid-cols-3">
          <SpendingLimit overview={overview.data} onPlan={openPlanner} />
          <FinancialHealth overview={overview.data} />
          <GoalTracker onPlan={openPlanner} />
        </div>
        <div className="grid items-start gap-5 lg:grid-cols-4">
          <CostAnalysis categories={overview.data.categories} currency={overview.data.summary.currency} onViewAll={() => navigate(appPaths.categories)} />
          <TransactionHistory transactions={overview.data.recentTransactions} onViewAll={() => navigate(appPaths.transactions)} />
        </div>
      </div> : null}
    </div>
  </main>;
}
