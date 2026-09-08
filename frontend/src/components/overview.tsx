import { useQuery } from "@tanstack/react-query";
import {
  ArrowDownLeft,
  ArrowDownRight,
  ArrowRight,
  ArrowUpRight,
  CalendarDays,
  CircleAlert,
  CreditCard,
  Landmark,
  MessageSquareText,
  Plus,
  ReceiptText,
  RotateCcw,
  Target,
  SlidersHorizontal,
  WalletCards,
} from "lucide-react";
import { useId, useMemo, useState, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { Area, AreaChart, CartesianGrid, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { CategoryExplorer } from "@/components/category-explorer";
import { DeleteAccountButton } from "@/components/delete-account-button";
import { FinanceFormDialog } from "@/components/finance-forms";
import { useUserDefaults } from "@/components/user-defaults";
import { Button, buttonVariants } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { SiteHeader } from "@/components/ui/site-header";
import { useWorkspaceShell } from "@/components/workspace";
import { loadGoals, loadOverview } from "@/lib/api";
import { formatCount, formatMoney, formatShortDate } from "@/lib/format";
import type { OverviewAccountOut, OverviewBudgetOut, OverviewCategoryOut, OverviewOut, OverviewTransactionOut, OverviewTrendPointOut } from "@/lib/generated/contracts";
import { cn } from "@/lib/utils";
import { appPaths, financeFormParams, type FinanceFormKind } from "@/routing/paths";

export type TrendRange = "7" | "30" | "max";

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

function OverviewSkeleton() {
  return <div role="status" aria-label="Loading your financial overview" className="space-y-4">
    <div className="h-60 animate-pulse rounded-2xl border border-line bg-surface sm:h-44" />
    <div className="grid gap-4 lg:grid-cols-2">
      {[0, 1].map((item) => <div key={item} className="h-72 animate-pulse rounded-xl border border-line bg-surface" />)}
    </div>
  </div>;
}

function SectionHeading({ id, eyebrow, title, detail, action }: { id?: string; eyebrow: string; title: string; detail?: string; action?: ReactNode }) {
  return <div className="flex flex-wrap items-start justify-between gap-3">
    <div className="min-w-0">
      <p className="ledger-meta">{eyebrow}</p>
      <h2 id={id} className="mt-1 font-heading text-[1.0625rem] font-semibold tracking-[-0.025em] text-ink sm:text-title">{title}</h2>
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
  return <section aria-labelledby="linked-accounts-title" className="min-w-0 rounded-2xl border border-line bg-surface p-4 sm:p-6">
    <SectionHeading id="linked-accounts-title" eyebrow="Your accounts" title={accounts.length ? `${accounts.length} account${accounts.length === 1 ? "" : "s"}, one view` : "Start with an account"} action={<Button type="button" variant="ghost" size="sm" className="min-h-11" onClick={onPlan}><Plus /> Add account</Button>} />
    {accounts.length ? <div aria-label="Recorded accounts" tabIndex={0} className="mt-4 flex snap-x snap-mandatory gap-3 overflow-x-auto overscroll-x-contain pb-2">
      {accounts.map((account) => <div key={account.id} className="w-[85%] max-w-72 shrink-0 snap-start rounded-xl border border-line bg-ground p-4 sm:w-64">
        <div className="flex items-center gap-2.5">
          <span className="grid size-9 shrink-0 place-items-center rounded-lg border border-line bg-surface text-secondary"><AccountIcon type={account.accountType} /></span>
          <div className="min-w-0 flex-1"><p className="truncate text-control font-semibold text-ink">{account.name}</p><p className="truncate text-note text-ink-muted">{[account.institution, account.mask ? `•••• ${account.mask}` : titleCase(account.accountType)].filter(Boolean).join(" · ")}</p></div>
          <DeleteAccountButton account={account} />
        </div>
        <p className="mt-4 font-heading text-title font-semibold tabular-nums text-ink">{formatMoney(account.balanceMinor, account.currency)}</p>
        <p className="mt-1 text-note text-ink-muted">Recorded balance</p>
      </div>)}
    </div> : <p className="mt-3 text-control leading-6 text-ink-muted">Add an account and its balance with fyn to see it here.</p>}
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
  const [range, setRange] = useState<TrendRange>("max");
  const allPoints = useMemo(() => cumulativeTrend(overview.trend), [overview.trend]);
  const visiblePoints = visibleTrend(allPoints, range);
  const currency = overview.summary.currency;
  const previousLabel = new Intl.DateTimeFormat("en-IN", { month: "short" }).format(new Date(`${overview.period.previousStart}T12:00:00`));

  return <section aria-labelledby="cash-flow-title" className="min-w-0 rounded-2xl border border-line bg-surface p-4 sm:p-6">
    <div className="flex items-center justify-between gap-3">
      <div className="min-w-0">
        <h2 id="cash-flow-title" className="font-heading text-control font-semibold text-ink sm:text-title">Your month in motion</h2>
        <p className="mt-1 hidden text-note text-ink-muted sm:block">Running totals through {trendDate.format(new Date(`${overview.period.end}T12:00:00`))}</p>
      </div>
      <div role="group" aria-label="Trend range" className="flex shrink-0 rounded-lg border border-line bg-ground p-0.5">
        {(["7", "30", "max"] as const).map((option) => <button
          key={option}
          type="button"
          aria-pressed={range === option}
          onClick={() => setRange(option)}
          className={cn("h-11 min-w-11 rounded-md px-2.5 text-note font-semibold text-ink-muted transition-colors", range === option && "bg-surface text-ink")}
        >{option === "max" ? "Month" : `${option}d`}</button>)}
      </div>
    </div>

    <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-meta text-ink-muted" aria-label="Cash flow chart legend">
      <span className="flex items-center gap-1.5"><i className="size-2 rounded-full bg-money-in" />Income</span>
      <span className="flex items-center gap-1.5"><i className="size-2 rounded-full bg-money-out" />Spent</span>
      <span className="flex items-center gap-1.5"><i className="size-2 rounded-full bg-secondary" />Net</span>
      <span className="flex items-center gap-1.5"><i className="h-px w-3 bg-ink-muted" />{previousLabel} expenses</span>
    </div>

    <div className="mt-3 h-[clamp(6rem,calc(100dvh_-_37.25rem),10rem)] min-w-0 sm:h-64" role="img" aria-label={`Cumulative income, expenses, net cash flow and previous-month spending for ${overview.period.label}`}>
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
          <Area type="monotone" dataKey="balance" name="Net cash flow" stroke="var(--secondary)" strokeWidth={2.5} fill="url(#overviewBalanceFill)" isAnimationActive={false} />
          <Line type="monotone" dataKey="income" name="Income" stroke="var(--money-in)" strokeWidth={2} dot={false} isAnimationActive={false} />
          <Line type="monotone" dataKey="expenses" name="Expenses" stroke="var(--money-out)" strokeWidth={2} dot={false} isAnimationActive={false} />
          <Line type="monotone" dataKey="previousExpenses" name={`${previousLabel} expenses`} stroke="var(--ink-muted)" strokeWidth={1.5} strokeDasharray="5 5" dot={false} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  </section>;
}

function budgetPace(budget: OverviewBudgetOut, overview: OverviewOut) {
  const elapsedDays = Math.max(1, Number(overview.period.end.slice(-2)));
  const monthDays = daysInMonth(overview.period.end);
  const dailyAverage = Math.round(budget.spentMinor / elapsedDays / 100) * 100;
  const dailyBudget = Math.round(budget.amountMinor / monthDays / 100) * 100;
  const projected = overview.period.isCurrent ? Math.round(dailyAverage * monthDays) : budget.spentMinor;
  return { dailyAverage, dailyBudget, projected, projectedVariance: projected - budget.amountMinor };
}

function MetricChange({ label, current, previous, lowerIsBetter = false, comparisonId }: { label: string; current: number; previous: number; lowerIsBetter?: boolean; comparisonId: string }) {
  const difference = current - previous;
  const percentage = previous === 0 ? null : Math.abs(difference / previous * 100);
  const favorable = lowerIsBetter ? difference < 0 : difference > 0;
  const neutral = difference === 0 || percentage === null;
  const Icon = neutral ? ArrowRight : difference > 0 ? ArrowUpRight : ArrowDownRight;
  const change = difference === 0 ? "No change" : percentage === null ? "No prior baseline" : `${percentage < 0.1 ? "<0.1" : formatCount(percentage, 1)}% ${difference > 0 ? "higher" : "lower"}`;
  return <p aria-label={`${label} change from previous period`} aria-describedby={comparisonId} className={cn("mt-1 flex items-start gap-1 text-meta leading-4", neutral ? "text-ink-muted" : favorable ? "text-money-in" : "text-money-out")}>
    <Icon size={13} aria-hidden className="mt-0.5 shrink-0" /><span>{change}</span>
  </p>;
}

export function MoneySummary({ overview, onPlan }: { overview: OverviewOut; onPlan: () => void }) {
  const { summary } = overview;
  const comparisonId = useId();
  const previousIncome = overview.trend.reduce((sum, point) => sum + point.previousIncomeMinor, 0);
  const comparisonStart = new Date(`${overview.period.previousStart}T12:00:00`);
  const comparisonEnd = new Date(`${overview.period.previousEnd}T12:00:00`);
  const comparisonRange = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short" }).formatRange(comparisonStart, comparisonEnd);
  const overall = overview.budgets?.find((budget) => budget.categoryId === null);
  const pace = overall ? budgetPace(overall, overview) : null;
  const over = overall && overall.overMinor > 0;
  const categoryOverruns = overview.budgets?.filter((budget) => budget.categoryId !== null && budget.overMinor > 0) ?? [];
  return <section aria-label={`Money summary for ${overview.period.label}`} className="overflow-hidden rounded-2xl border border-secondary-line bg-surface">
    <div className="grid grid-cols-2 xl:grid-cols-1">
      <div className="bg-secondary-tint px-4 py-3 sm:px-5 sm:py-4">
        <p className="text-control font-medium text-ink-body">Spent this month</p>
        <p className="mt-1 font-heading text-[clamp(1.4rem,6vw,2rem)] leading-tight font-semibold tracking-[-0.05em] tabular-nums text-ink">{formatMoney(summary.spentMinor, summary.currency)}</p>
        <MetricChange label="Spending" current={summary.spentMinor} previous={summary.previousSpentMinor} lowerIsBetter comparisonId={comparisonId} />
        <p className="mt-2 text-note text-ink-muted">{summary.expenseCount} expense{summary.expenseCount === 1 ? "" : "s"}</p>
        <p id={comparisonId} className="mt-1 text-meta leading-4 text-ink-muted">Changes vs {comparisonRange}<span className="sr-only"> {comparisonEnd.getFullYear()}, the previous comparison period.</span></p>
      </div>
      <div className="grid items-center gap-1.5 px-4 py-2.5 sm:grid-cols-2 sm:gap-2 sm:py-3 xl:px-5">
        <div className="min-w-0">
          <p className="flex items-center gap-1.5 text-note text-ink-muted"><ArrowDownLeft size={15} className="text-money-in" /> Income</p>
          <p className="mt-1 font-heading text-control leading-tight font-semibold tracking-tight tabular-nums text-money-in sm:text-title">{formatMoney(summary.incomeMinor, summary.currency)}</p>
          <MetricChange label="Income" current={summary.incomeMinor} previous={previousIncome} comparisonId={comparisonId} />
        </div>
        <div className="min-w-0">
          <p className="text-note text-ink-muted">Income − expenses</p>
          <p className={cn("mt-1 font-heading text-control leading-tight font-semibold tracking-tight tabular-nums sm:text-title", summary.netMinor < 0 ? "text-money-out" : "text-ink")}>{formatMoney(summary.netMinor, summary.currency)}</p>
          <MetricChange label="Income minus expenses" current={summary.netMinor} previous={previousIncome - summary.previousSpentMinor} comparisonId={comparisonId} />
        </div>
      </div>
    </div>
    <div role="group" aria-label="Budget at a glance" className="border-t border-line px-4 py-2 sm:px-5 sm:py-3">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <p className={cn("min-w-0 flex-1 text-note font-semibold sm:text-control", over ? "text-money-out" : "text-ink")}>
          {overall ? `${formatMoney(over ? overall.overMinor : overall.remainingMinor, overall.currency)} ${over ? "over" : "left in"} monthly budget` : "No overall budget set"}
        </p>
        <Button type="button" variant="link" className="min-h-11 px-0 text-note" onClick={onPlan}>{overall ? "Adjust budget" : "Set budget"}<ArrowRight /></Button>
      </div>
      {overall ? <>
        <div role="progressbar" aria-label="Monthly budget used" aria-valuemin={0} aria-valuemax={Math.max(100, Math.ceil(overall.percentUsed))} aria-valuenow={overall.percentUsed} aria-valuetext={`${formatCount(overall.percentUsed, 0)}% of ${formatMoney(overall.amountMinor, overall.currency)}`} className="mt-1 h-1.5 overflow-hidden rounded-full bg-line">
          <span className={cn("block h-full rounded-full", over ? "bg-money-out" : "bg-secondary")} style={{ width: `${Math.min(100, overall.percentUsed)}%` }} />
        </div>
        <p className="mt-1 text-note text-ink-muted sm:mt-2">{formatCount(overall.percentUsed, 0)}% of your {formatMoney(overall.amountMinor, overall.currency)} monthly limit used</p>
      </> : <p className="text-note leading-5 text-ink-muted">Set a limit to track your spending against a budget.</p>}
      {overview.period.isCurrent && !over && pace && pace.projectedVariance > 0 ? <p aria-label="Budget pace warning" className="mt-2 flex items-start gap-2 text-note leading-5 text-attention sm:mt-3"><CircleAlert size={15} className="mt-0.5 shrink-0" />At this pace, spending may finish {formatMoney(pace.projectedVariance, summary.currency)} over budget.</p> : null}
      {categoryOverruns.length ? <p className="mt-2 flex items-start gap-2 text-note leading-5 text-money-out"><CircleAlert size={15} className="mt-0.5 shrink-0" />{categoryOverruns.length} category budget{categoryOverruns.length === 1 ? " is" : "s are"} over the limit.</p> : null}
    </div>
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
  const { dailyAverage, dailyBudget, projected, projectedVariance } = budgetPace(budget, overview);
  const paceRatio = dailyBudget ? dailyAverage / dailyBudget : 0;
  const chartMax = Math.max(dailyBudget, ...overview.trend.map((point) => point.spentMinor), 1);
  const budgetLine = Math.max(4, Math.min(100, dailyBudget / chartMax * 100));
  const crossedOn = overview.trend.reduce<{ spent: number; point: OverviewTrendPointOut | null }>((state, point) => {
    if (state.point) return state;
    const spent = state.spent + point.spentMinor;
    return { spent, point: spent > budget.amountMinor ? point : null };
  }, { spent: 0, point: null }).point;
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
      <Button type="button" variant="link" size="sm" className="min-h-11 shrink-0 px-2" onClick={onPlan}>Edit</Button>
    </div>
    <div className="mt-2 flex flex-wrap items-baseline justify-between gap-2">
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

export function SpendingLimit({ overview, onPlan }: { overview: OverviewOut; onPlan: (budgetId?: string, categoryScope?: boolean) => void }) {
  const [scope, setScope] = useState<"overall" | "categories">("overall");
  const budgets = overview.budgets ?? [];
  const overall = budgets.find((budget) => budget.categoryId === null);
  const categoryBudgets = budgets
    .filter((budget) => budget.categoryId !== null)
    .sort((left, right) => right.percentUsed - left.percentUsed || (left.category ?? left.name).localeCompare(right.category ?? right.name));
  const categoryTitle = categoryBudgets.length
    ? `${categoryBudgets.length} category budget${categoryBudgets.length === 1 ? "" : "s"}`
    : "No category budgets";
  const planOverall = () => onPlan(overall?.id);
  const planCategory = () => onPlan(undefined, true);
  return <section aria-labelledby="spending-limit-title" className="min-w-0 rounded-2xl border border-line bg-surface p-4 sm:p-6">
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
          className={cn("h-11 rounded-md px-3 text-note font-semibold text-ink-muted transition-colors", scope === option && "bg-surface text-ink")}
        >{option === "overall" ? "Overall" : "Category"}</button>)}
      </div>}
    />

    {scope === "overall" ? (overall ? <OverallBudgetPanel budget={overall} overview={overview} onPlan={planOverall} /> : <BudgetEmptyAction
      title="Set overall budget"
      detail={`${formatMoney(overview.summary.spentMinor, overview.summary.currency)} spent in ${overview.period.label} · ${overview.summary.expenseCount} expense${overview.summary.expenseCount === 1 ? "" : "s"}`}
      onPlan={planOverall}
    />) : <div className="mt-5">
      {categoryBudgets.length ? <>
        <div className="flex items-center justify-between gap-3">
          <p className="text-meta leading-5 text-ink-muted">Category limits stay independent from the overall cap.</p>
          <Button type="button" variant="ghost" size="sm" className="min-h-11 shrink-0" onClick={planCategory}><Plus /> Add category</Button>
        </div>
        <div className="mt-3 max-h-52 space-y-3 overflow-y-auto pr-1">
          {categoryBudgets.map((budget) => <div key={budget.id} className="rounded-lg bg-ground p-3.5"><BudgetBar budget={budget} label={budget.category ?? budget.name} /><Button type="button" variant="ghost" className="mt-2 min-h-11" onClick={() => onPlan(budget.id)}>Edit {budget.category ?? budget.name} budget</Button></div>)}
        </div>
      </> : <BudgetEmptyAction
        title="Add category budget"
        detail="Set an independent limit for Food, Transport, Shopping, or another category."
      onPlan={planCategory}
      />}
    </div>}
  </section>;
}

function PlanningActions({ onBudget, onGoal, onEditGoal, onContribute }: { onBudget: () => void; onGoal: () => void; onEditGoal: (id: string) => void; onContribute: (id: string) => void }) {
  const goals = useQuery({ queryKey: ["goals"], queryFn: loadGoals });
  return <section aria-labelledby="planning-title" className="min-w-0 rounded-2xl border border-line bg-surface p-4 sm:p-6">
    <SectionHeading id="planning-title" eyebrow="Looking ahead" title="Budgets & goals" detail="Set a limit or work towards something you want." />
    <div className="mt-4 grid gap-3 sm:grid-cols-2">
      <Button type="button" variant="outline" className="min-h-12 justify-start px-4" onClick={onBudget}><SlidersHorizontal /> Set budget <ArrowRight className="ml-auto" /></Button>
      <Button type="button" variant="outline" className="min-h-12 justify-start px-4" onClick={onGoal}><Target /> Set a goal <ArrowRight className="ml-auto" /></Button>
    </div>
    {goals.isError ? <p role="alert" className="mt-4 text-note text-danger-ink">Goals couldn’t be loaded. <Button type="button" variant="link" className="min-h-11" onClick={() => void goals.refetch()}>Try again</Button></p> : goals.isPending ? <p role="status" className="mt-4 text-note text-ink-muted">Loading goals…</p> : goals.data.length ? <div className="mt-5 space-y-3">{goals.data.map((goal) => <article key={goal.id} aria-label={`Savings goal: ${goal.name}`} className="rounded-xl border border-line bg-ground p-4">
      <div className="flex items-baseline justify-between gap-3"><h3 className="text-control font-semibold text-ink">{goal.name}</h3><span className="text-note text-ink-muted">{Math.round(goal.currentMinor / goal.targetMinor * 100)}%</span></div>
      <p className="mt-1 text-note text-ink-muted">{formatMoney(goal.currentMinor, goal.currency)} of {formatMoney(goal.targetMinor, goal.currency)} saved</p>
      <div role="progressbar" aria-label={`${goal.name} progress`} aria-valuemin={0} aria-valuemax={Math.max(100, Math.round(goal.currentMinor / goal.targetMinor * 100))} aria-valuenow={Math.round(goal.currentMinor / goal.targetMinor * 100)} className="mt-3 h-1.5 overflow-hidden rounded-full bg-line"><div className="h-full rounded-full bg-secondary" style={{ width: `${Math.min(100, goal.currentMinor / goal.targetMinor * 100)}%` }} /></div>
      {goal.targetDate ? <p className="mt-2 text-meta text-ink-muted">Target: {formatShortDate(goal.targetDate)}</p> : null}
      <div className="mt-3 flex gap-2"><Button type="button" variant="outline" className="min-h-11" onClick={() => onContribute(goal.id)}><Plus /> Add savings</Button><Button type="button" variant="ghost" className="min-h-11" onClick={() => onEditGoal(goal.id)}>Edit goal</Button></div>
    </article>)}</div> : <p className="mt-4 text-note text-ink-muted">Your savings goals will appear here.</p>}
  </section>;
}

function CostAnalysis({ categories, currency, onViewAll }: { categories: OverviewCategoryOut[]; currency: string; onViewAll: () => void }) {
  const visible = categories.slice(0, 5);
  return <section aria-labelledby="cost-analysis-title" className="min-w-0 rounded-2xl border border-line bg-surface p-4 sm:p-6">
    <SectionHeading id="cost-analysis-title" eyebrow="Spending breakdown" title="Where it went" detail={`${categories.length} categor${categories.length === 1 ? "y" : "ies"} this month`} action={<Button type="button" variant="ghost" size="sm" className="min-h-11" onClick={onViewAll}>Explore <ArrowRight /></Button>} />
    <div className="mt-4 flex h-2 overflow-hidden rounded-full bg-line" aria-hidden>
      {visible.map((category, index) => <span key={category.id} className="h-full bg-secondary" style={{ width: `${category.sharePercent}%`, opacity: Math.max(0.35, 1 - index * 0.13) }} />)}
    </div>
    <div className="mt-5 space-y-3.5">
      {!categories.length ? <p className="text-control text-ink-muted">No expenses recorded for this month.</p> : null}
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
  const { timeZone } = useUserDefaults();
  return <section aria-labelledby="transaction-history-title" className="min-w-0 overflow-hidden rounded-2xl border border-line bg-surface">
    <div className="px-4 pt-4 pb-3 sm:px-6 sm:pt-6">
      <SectionHeading id="transaction-history-title" eyebrow="Your latest entries" title="Recent activity" action={<Button type="button" variant="ghost" size="sm" className="min-h-11" onClick={onViewAll}>View all <ArrowRight /></Button>} />
    </div>
    {transactions.length ? <div className="divide-y divide-line">
      {transactions.slice(0, 5).map((transaction) => {
        const { incoming, Icon } = transactionDirection(transaction);
        return <button key={transaction.id} type="button" onClick={onViewAll} className="grid min-h-18 w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2.5 px-4 py-3 text-left transition-colors hover:bg-ground active:bg-surface-sunken sm:px-6">
          <span className={cn("grid size-9 place-items-center rounded-lg bg-ground", incoming ? "text-money-in" : "text-money-out")}><Icon size={16} /></span>
          <span className="min-w-0">
            <span className="block truncate text-note font-semibold text-ink">{transaction.merchant ?? titleCase(transaction.transactionType)}</span>
            <span className="mt-0.5 block truncate text-meta text-ink-muted">{[transaction.category ?? titleCase(transaction.transactionType), transaction.account, formatShortDate(transaction.transactionAt, timeZone)].filter(Boolean).join(" · ")}</span>
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
  // A reload can restore a persisted snapshot from before an account deletion.
  const overview = useQuery({ queryKey: ["overview", month], queryFn: () => loadOverview(month), refetchOnMount: "always" });
  // This response covers one month. An empty period says nothing about the
  // account's history and must never replace the dashboard with onboarding.
  const noMonthlyActivity = overview.data && overview.data.recentTransactions.length === 0 && overview.data.summary.incomeMinor === 0 && overview.data.summary.spentMinor === 0;
  const previousMonth = months[months.findIndex((option) => option.value === month) + 1];
  const conversationId = shell.conversations[0]?.id ?? shell.defaultConversationId;
  const periodLabel = overview.data?.period.label ?? months.find((option) => option.value === month)!.label;
  const openPlanner = (prompt: string) => {
    if (conversationId) navigate(appPaths.conversation(conversationId, prompt));
  };
  const formParam = params.get("form");
  const formKind = ["budget", "goal", "account", "contribution"].includes(formParam ?? "") ? formParam as FinanceFormKind : null;
  const openForm = (kind: FinanceFormKind, id?: string, categoryScope?: boolean) => setParams((previous) => financeFormParams(previous, kind, id, categoryScope ? "category" : undefined));
  const overallBudget = overview.data?.budgets.find((budget) => budget.categoryId === null);
  const openBudget = (id?: string, categoryScope?: boolean) => openForm("budget", id, categoryScope);
  const briefingPrompt = `Review my finances for ${periodLabel}. Summarize my income, expenses, and spending patterns, and explain what needs my attention.`;
  const addTransactionLink = (className?: string) => <Link to={appPaths.newTransaction} className={cn(buttonVariants({ size: "lg" }), "min-h-12 rounded-xl", className)}><Plus size={18} /> Add transaction</Link>;

  return <main id="main-content" className="flex min-h-0 min-w-0 flex-col overflow-hidden bg-ground">
    <div className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-y-contain">
      <SiteHeader title="Overview" subtitle="Your money, at a glance" subtitleClassName="hidden sm:block" navOpen={shell.navOpen} onOpenNav={shell.openNav} end={<div className="relative">
        <CalendarDays className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-ink-muted" />
        <Combobox aria-label="Overview month" value={month} onValueChange={setMonth} options={months} searchPlaceholder="Search months" triggerClassName="min-h-11 w-auto max-w-[12.5rem] pl-9 font-medium text-ink-body" />
      </div>} />

      <div className="mx-auto w-full max-w-6xl px-4 pt-3 pb-6 sm:px-6 sm:py-5 lg:px-8">
        <div className="mb-5 hidden items-center justify-between gap-4 md:flex">
          <div>
            <h2 className="font-heading text-title font-semibold tracking-[-0.025em] text-ink">Monthly overview</h2>
            <p className="mt-1 text-note text-ink-muted">{overview.data ? `Records through ${trendDate.format(new Date(`${overview.data.period.end}T12:00:00`))}` : "Loading your monthly records"}</p>
          </div>
          <div className="flex items-center gap-3">
            <Button type="button" variant="outline" className="min-h-12" disabled={!conversationId} onClick={() => openPlanner(briefingPrompt)}><MessageSquareText /> Ask fyn</Button>
            {addTransactionLink()}
          </div>
        </div>

        {overview.isPending ? <OverviewSkeleton /> : overview.isError ? <div role="alert" className="rounded-2xl border border-danger-line bg-surface px-5 py-10 text-center">
          <h2 className="font-heading text-title font-semibold text-ink">We couldn’t open your overview</h2>
          <p className="mt-2 text-control text-ink-muted">Your records are safe. Try loading your overview again.</p>
          <Button type="button" variant="outline" className="mt-5 min-h-11" onClick={() => overview.refetch()}><RotateCcw /> Try again</Button>
        </div> : overview.data ? <div className="space-y-4 sm:space-y-5">
          <div className="grid gap-3 sm:gap-5 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.4fr)]">
            <MoneySummary overview={overview.data} onPlan={() => openBudget(overallBudget?.id)} />
            <TrendChart overview={overview.data} />
          </div>

          {noMonthlyActivity ? <section aria-label="No activity this month" className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line bg-surface p-4 sm:px-5">
            <div className="min-w-0 flex-1 basis-64">
              <h2 className="text-control font-semibold text-ink">No transactions in {periodLabel}</h2>
              <p className="mt-1 text-note leading-5 text-ink-muted">Choose another month, or open Transactions to see records across all dates.</p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {previousMonth ? <Button type="button" variant="outline" className="min-h-11" onClick={() => setMonth(previousMonth.value)}><CalendarDays /> View {previousMonth.label}</Button> : null}
              <Link to={appPaths.transactions} className={cn(buttonVariants({ variant: "link" }), "min-h-11 px-1 text-note")}>All transactions <ArrowRight /></Link>
            </div>
          </section> : null}

          <SpendingLimit overview={overview.data} onPlan={openBudget} />

          <div className="grid gap-4 sm:gap-5 xl:grid-cols-[1.2fr_1fr]">
            <TransactionHistory transactions={overview.data.recentTransactions} onViewAll={() => navigate(appPaths.transactions)} />
            <CostAnalysis categories={overview.data.categories} currency={overview.data.summary.currency} onViewAll={() => navigate(appPaths.categories)} />
          </div>

          <div className="grid gap-4 sm:gap-5 xl:grid-cols-2">
            <AccountStrip accounts={overview.data.accounts} onPlan={() => openForm("account")} />
            <PlanningActions onBudget={() => openBudget(overallBudget?.id)} onGoal={() => openForm("goal")} onEditGoal={(id) => openForm("goal", id)} onContribute={(id) => openForm("contribution", id)} />
          </div>
        </div> : null}
      </div>
    </div>
    <nav aria-label="Overview quick actions" className="z-20 flex shrink-0 gap-3 border-t border-line bg-surface px-4 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] md:hidden">
      {addTransactionLink("min-w-0 flex-1")}
      <Button type="button" variant="outline" size="lg" className="min-h-12 rounded-xl px-4" disabled={!conversationId} onClick={() => openPlanner(briefingPrompt)}><MessageSquareText size={18} /> Ask fyn</Button>
    </nav>
    {formKind ? <FinanceFormDialog key={`${formKind}:${params.get("record") ?? "new"}`} kind={formKind} id={params.get("record") ?? undefined} categoryScope={params.get("scope") === "category"} onClose={() => setParams((previous) => financeFormParams(previous, null), { replace: true })} /> : null}
  </main>;
}
