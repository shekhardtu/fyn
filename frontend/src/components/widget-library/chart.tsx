import { useMemo } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatCount, formatDimension, formatMoney } from "@/lib/format";
import { readVisualChannels } from "@/lib/generated/contracts.readers";
import type { DataChartData, WidgetActionId } from "@/lib/protocol";

type Row = Record<string, unknown>;

/** One surface for the widget adapter and for any page that embeds a chart.
 *  A chart has no row actions today; the action props exist for parity and
 *  forward compatibility. */
export type ChartViewProps = {
  data: DataChartData;
  disabled?: boolean;
  pending?: boolean;
  embedded?: boolean;
  /** The enclosing card draws its own chrome and title; the chart renders only its plot. */
  parentManagesWidth?: boolean;
  onAction?: (action: WidgetActionId, payload: Record<string, unknown>) => void;
};

/** Fixed assignment order, never cycled: series N always wears --chart-N, and
 *  anything past the palette folds into "Other". Slot 1 is the fyn indigo. */
const SERIES_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
] as const;
const MAX_SERIES = SERIES_COLORS.length;
const OTHER_LABEL = "Other";
/** The pivoted category key. Encodings name real row fields, so a reserved
 *  double-underscore key cannot collide with a series name. */
const X_KEY = "__x";
const Y_KEY = "__y";

const MARK_NAMES: Record<string, string> = {
  bar: "Bar",
  line: "Line",
  area: "Area",
  point: "Scatter",
  rect: "Grid",
  tick: "Bar",
  arc: "Pie",
};

const compactFormatters = new Map<string, Intl.NumberFormat>();

function compactMoney(valueMinor: number, currency: string) {
  let formatter = compactFormatters.get(currency);
  if (!formatter) {
    formatter = new Intl.NumberFormat("en-IN", { style: "currency", currency, notation: "compact", maximumFractionDigits: 1 });
    compactFormatters.set(currency, formatter);
  }
  return formatter.format(valueMinor / 100);
}

function toNumber(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function categoryText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return formatDimension(value);
}

type ShapedArc = { kind: "arc"; slices: Array<{ name: string; value: number }> };
type ShapedXy = { kind: "xy"; rows: Row[]; seriesKeys: string[] };
type Shaped = ShapedArc | ShapedXy;

/** Rows arrive long-form from the governed executor. An arc aggregates into
 *  slices; an x/y mark with a color channel pivots wide, one dataKey per
 *  series in first-appearance order, folding past-the-palette series into
 *  "Other" so a hue is never invented. */
function shapeRows(data: DataChartData): Shaped {
  const view = data.view;
  const { category, measure } = readVisualChannels(view.mark, view.encoding as unknown as Row);

  if (view.mark === "arc") {
    const totals = new Map<string, number>();
    for (const row of data.rows) {
      const name = categoryText(row[category.field]);
      totals.set(name, (totals.get(name) ?? 0) + (toNumber(row[measure.field]) ?? 0));
    }
    const entries = [...totals.entries()];
    const kept = entries.slice(0, entries.length > MAX_SERIES ? MAX_SERIES - 1 : MAX_SERIES);
    const slices = kept.map(([name, value]) => ({ name, value }));
    const folded = entries.slice(kept.length);
    if (folded.length) slices.push({ name: OTHER_LABEL, value: folded.reduce((sum, [, value]) => sum + value, 0) });
    return { kind: "arc", slices };
  }

  const colorField = view.encoding.color && view.encoding.color.field !== category.field ? view.encoding.color.field : null;
  if (!colorField) {
    return {
      kind: "xy",
      rows: data.rows.map((row) => ({ [X_KEY]: row[category.field], [measure.title]: toNumber(row[measure.field]) })),
      seriesKeys: [measure.title],
    };
  }

  const seriesOrder: string[] = [];
  for (const row of data.rows) {
    const name = categoryText(row[colorField]);
    if (!seriesOrder.includes(name)) seriesOrder.push(name);
  }
  const seriesKeys = seriesOrder.length > MAX_SERIES ? [...seriesOrder.slice(0, MAX_SERIES - 1), OTHER_LABEL] : seriesOrder;
  const byX = new Map<unknown, Row>();
  for (const row of data.rows) {
    const x = row[category.field];
    let bucket = byX.get(x);
    if (!bucket) {
      bucket = { [X_KEY]: x };
      byX.set(x, bucket);
    }
    const raw = categoryText(row[colorField]);
    const name = seriesKeys.includes(raw) ? raw : OTHER_LABEL;
    bucket[name] = (toNumber(bucket[name]) ?? 0) + (toNumber(row[measure.field]) ?? 0);
  }
  return { kind: "xy", rows: [...byX.values()], seriesKeys };
}

export function ChartView({ data, embedded = false }: ChartViewProps) {
  const view = data.view;
  const currency = data.currency ?? "INR";
  const { category, measure } = useMemo(
    () => readVisualChannels(view.mark, view.encoding as unknown as Row),
    [view.mark, view.encoding],
  );
  const shaped = useMemo(() => shapeRows(data), [data]);
  const seriesCount = shaped.kind === "arc" ? shaped.slices.length : shaped.seriesKeys.length;

  const formatValue = (value: number) => (measure.money ? formatMoney(value, currency) : formatCount(value));
  const formatAxisValue = (value: number) => (measure.money ? compactMoney(value, currency) : formatCount(value));

  const markName = MARK_NAMES[view.mark] ?? "Data";
  const moneyTotal = measure.money
    ? data.rows.reduce((sum, row) => sum + (toNumber(row[measure.field]) ?? 0), 0)
    : null;
  const ariaLabel = `${markName} chart titled “${view.title}” showing ${measure.title} by ${category.title} across ${
    shaped.kind === "arc" ? `${shaped.slices.length} slices` : `${shaped.rows.length} ${category.title} values`
  }${moneyTotal !== null ? `, totalling ${formatMoney(moneyTotal, currency)}` : ""}.`;

  // Every color below is a token; the plot inherits light/dark from the page.
  const tooltip = (
    <Tooltip
      cursor={view.mark === "line" || view.mark === "area" ? { stroke: "var(--line-strong)" } : { fill: "var(--sunken)", opacity: 0.6 }}
      labelFormatter={(value) => categoryText(value)}
      formatter={(value, name) => [formatValue(Number(value)), String(name)]}
      contentStyle={{ border: "1px solid var(--line)", borderRadius: 10, background: "var(--surface)", boxShadow: "var(--shadow-overlay)", fontSize: 12 }}
      labelStyle={{ color: "var(--ink-muted)" }}
      itemStyle={{ color: "var(--ink)" }}
    />
  );
  // Identity never rides color alone, and legend text wears ink, not the hue.
  const legend = seriesCount >= 2 ? (
    <Legend iconType="circle" iconSize={8} formatter={(value) => <span style={{ color: "var(--ink-body)", fontSize: 12 }}>{String(value)}</span>} />
  ) : null;
  const grid = <CartesianGrid stroke="var(--line-soft)" vertical={false} />;
  const xAxis = (
    <XAxis
      dataKey={X_KEY}
      {...(view.mark === "point" ? { type: "category" as const, allowDuplicatedCategory: false } : {})}
      tickFormatter={(value) => categoryText(value)}
      tick={{ fill: "var(--ink-muted)", fontSize: 11 }}
      axisLine={{ stroke: "var(--line)" }}
      tickLine={false}
      minTickGap={24}
    />
  );
  // One y-axis, always — two measures of different scale are two charts.
  const yAxis = (
    <YAxis
      {...(view.mark === "point" ? { dataKey: Y_KEY } : {})}
      tickFormatter={(value) => formatAxisValue(Number(value))}
      tick={{ fill: "var(--ink-muted)", fontSize: 11 }}
      axisLine={false}
      tickLine={false}
      width={measure.money ? 64 : 44}
    />
  );
  const margin = { top: 8, right: 12, left: 0, bottom: 0 };

  function plot() {
    if (shaped.kind === "arc") {
      return (
        <PieChart margin={margin}>
          {tooltip}
          {legend}
          {/* The 2px surface stroke is the spacer between adjacent fills. */}
          <Pie data={shaped.slices} dataKey="value" nameKey="name" innerRadius="52%" outerRadius="86%" paddingAngle={1} stroke="var(--surface)" strokeWidth={2} isAnimationActive={false}>
            {shaped.slices.map((slice, index) => <Cell key={slice.name} fill={SERIES_COLORS[index]} />)}
          </Pie>
        </PieChart>
      );
    }
    const { rows, seriesKeys } = shaped;
    if (view.mark === "line") {
      return (
        <LineChart data={rows} margin={margin} accessibilityLayer>
          {grid}{xAxis}{yAxis}{tooltip}{legend}
          {seriesKeys.map((key, index) => (
            <Line key={key} type="monotone" dataKey={key} name={key} stroke={SERIES_COLORS[index]} strokeWidth={2} dot={false} isAnimationActive={false} />
          ))}
        </LineChart>
      );
    }
    if (view.mark === "area") {
      return (
        <AreaChart data={rows} margin={margin} accessibilityLayer>
          {grid}{xAxis}{yAxis}{tooltip}{legend}
          {seriesKeys.map((key, index) => (
            <Area key={key} type="monotone" dataKey={key} name={key} stroke={SERIES_COLORS[index]} strokeWidth={2} fill={SERIES_COLORS[index]} fillOpacity={0.12} isAnimationActive={false} />
          ))}
        </AreaChart>
      );
    }
    if (view.mark === "point") {
      return (
        <ScatterChart margin={margin}>
          {grid}{xAxis}{yAxis}{tooltip}{legend}
          {seriesKeys.map((key, index) => (
            <Scatter
              key={key}
              name={key}
              data={rows.filter((row) => toNumber(row[key]) !== null).map((row) => ({ [X_KEY]: row[X_KEY], [Y_KEY]: toNumber(row[key]) }))}
              fill={SERIES_COLORS[index]}
              isAnimationActive={false}
            />
          ))}
        </ScatterChart>
      );
    }
    // bar — and the v1 fallbacks: `rect` (heatmap-lite) and `tick` render as
    // grouped bars until a dedicated mark earns its keep.
    return (
      <BarChart data={rows} margin={margin} accessibilityLayer barGap={2} barCategoryGap="24%">
        {grid}{xAxis}{yAxis}{tooltip}{legend}
        {seriesKeys.map((key, index) => (
          <Bar key={key} dataKey={key} name={key} fill={SERIES_COLORS[index]} radius={[4, 4, 0, 0]} maxBarSize={48} isAnimationActive={false} />
        ))}
      </BarChart>
    );
  }

  const plotRegion = data.rows.length ? (
    <div role="img" aria-label={ariaLabel} className="min-w-0" style={{ height: view.height }}>
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        {plot()}
      </ResponsiveContainer>
    </div>
  ) : (
    <p className="px-4 py-6 text-center text-note leading-5 text-ink-muted">There is nothing to plot yet.</p>
  );

  if (embedded) return plotRegion;

  return (
    <section className="widget-enter overflow-hidden rounded-lg border border-line bg-surface">
      <div className="border-b border-line px-3.5 py-3">
        <h3 className="font-heading text-body font-semibold leading-5 text-ink">{view.title}</h3>
        {view.description ? <p className="mt-0.5 text-note leading-4 text-ink-muted">{view.description}</p> : null}
      </div>
      <div className="px-2 pt-3 pb-2">{plotRegion}</div>
    </section>
  );
}
