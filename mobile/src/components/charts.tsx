import { memo, useMemo, useState } from "react";
import { StyleSheet, View, type LayoutChangeEvent } from "react-native";
import Svg, { Circle, G, Line, Path, Rect, Text as SvgText } from "react-native-svg";

import { Type } from "@/components/ui";
import { formatCount, formatDimension, formatMoney } from "@/lib/format";
import { space, text as textSize, type Palette } from "@/lib/theme";
import { useAppearance, useStyles, useTheme } from "@/lib/appearance";

/**
 * The chart set, drawn directly.
 *
 * The web app plots with Recharts, which is a DOM renderer, and one heatmap
 * with Vega, which needs a browser outright. Neither runs here. Rather than
 * take on Skia for six chart types over a few dozen points, these are drawn as
 * SVG: it keeps the palette and the type scale identical to the rest of the
 * app, adds no native module to the build, and at this data size the cost is
 * nowhere near a frame.
 */

export type Series = { key: string; label: string; currency?: string; valueType?: string };
export type ChartKind = "bar" | "line" | "area" | "pie" | "scatter" | "heatmap" | "donut";

type Row = Record<string, unknown>;

const PADDING = { top: 8, right: 8, bottom: 26, left: 46 };
const HEIGHT = 200;

function num(value: unknown) {
  const parsed = typeof value === "number" ? value : Number(value ?? 0);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Axis figures have to be short or they collide. Money is shown in the scale
 *  Indian readers actually use — thousands, lakh, crore — not an abbreviated
 *  rupee figure that reads as a different number. */
function tickLabel(value: number, currency?: string) {
  const abs = Math.abs(value);
  const major = currency ? value / 100 : value;
  const absMajor = Math.abs(major);
  if (currency) {
    if (absMajor >= 10_000_000) return `${(major / 10_000_000).toFixed(1)}Cr`;
    if (absMajor >= 100_000) return `${(major / 100_000).toFixed(1)}L`;
    if (absMajor >= 1_000) return `${Math.round(major / 1_000)}k`;
    return String(Math.round(major));
  }
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return formatCount(value, 1);
}

function niceTicks(max: number, count = 4) {
  if (max <= 0) return [0];
  const raw = max / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((factor) => factor * magnitude).find((candidate) => candidate >= raw) ?? magnitude * 10;
  const ticks: number[] = [];
  for (let value = 0; value <= max + step / 2; value += step) ticks.push(value);
  return ticks;
}

function shortCategory(value: unknown) {
  const label = formatDimension(value);
  return label.length > 9 ? `${label.slice(0, 8)}…` : label;
}

export const ChartView = memo(function ChartView({ kind, rows, xKey, yKey, series, groupKey }: {
  kind: ChartKind;
  rows: Row[];
  xKey: string;
  yKey?: string;
  groupKey?: string;
  series: Series[];
}) {
  const styles = useStyles(makeStyles);
  const { chartPalette } = useAppearance();
  const [width, setWidth] = useState(0);
  const onLayout = (event: LayoutChangeEvent) => setWidth(event.nativeEvent.layout.width);

  const plotted = series.length ? series : [{ key: yKey ?? "value", label: "Value" }];
  const currency = plotted[0]?.currency;

  const geometry = useMemo(() => {
    const values = rows.flatMap((row) => plotted.map((entry) => num(row[entry.key])));
    const max = Math.max(0, ...values);
    const min = Math.min(0, ...values);
    return { max, min };
  }, [rows, plotted]);

  if (!rows.length) return null;

  return (
    <View onLayout={onLayout} style={{ width: "100%" }}>
      {width > 0 ? (
        <>
          {kind === "pie" || kind === "donut"
            ? <PieChart width={width} rows={rows} xKey={xKey} valueKey={plotted[0].key} currency={currency} />
            : kind === "heatmap"
              ? <Heatmap width={width} rows={rows} xKey={xKey} yKey={yKey ?? groupKey ?? "y"} valueKey={plotted[0].key} currency={currency} />
              : <CartesianChart kind={kind} width={width} rows={rows} xKey={xKey} series={plotted} max={geometry.max} min={geometry.min} currency={currency} />}
          {plotted.length > 1 ? (
            <View style={styles.legend}>
              {plotted.map((entry, index) => (
                <View key={entry.key} style={styles.legendItem}>
                  <View style={[styles.swatch, { backgroundColor: chartPalette[index % chartPalette.length] }]} />
                  <Type size="meta" color="muted">{entry.label}</Type>
                </View>
              ))}
            </View>
          ) : null}
        </>
      ) : (
        <View style={{ height: HEIGHT }} />
      )}
    </View>
  );
});

function CartesianChart({ kind, width, rows, xKey, series, max, min, currency }: {
  kind: ChartKind;
  width: number;
  rows: Row[];
  xKey: string;
  series: Series[];
  max: number;
  min: number;
  currency?: string;
}) {
  const color = useTheme();
  const { chartPalette } = useAppearance();
  const innerWidth = Math.max(1, width - PADDING.left - PADDING.right);
  const innerHeight = HEIGHT - PADDING.top - PADDING.bottom;
  const ticks = niceTicks(max || 1);
  const ceiling = ticks[ticks.length - 1] || 1;
  const floor = Math.min(0, min);
  const span = ceiling - floor || 1;

  const yFor = (value: number) => PADDING.top + innerHeight - ((value - floor) / span) * innerHeight;
  const bandWidth = innerWidth / rows.length;
  const xCentre = (index: number) => PADDING.left + bandWidth * index + bandWidth / 2;

  // Labelling every category overprints on a phone; this thins them to what
  // fits and always keeps the first and last so the range stays readable.
  const labelStride = Math.max(1, Math.ceil(rows.length / Math.max(2, Math.floor(innerWidth / 52))));

  return (
    <Svg width={width} height={HEIGHT} accessibilityRole="image">
      {ticks.map((tick) => (
        <G key={tick}>
          <Line x1={PADDING.left} y1={yFor(tick)} x2={width - PADDING.right} y2={yFor(tick)} stroke={color.lineSoft} strokeWidth={1} />
          <SvgText x={PADDING.left - 6} y={yFor(tick) + 3} fontSize={textSize.meta - 1} fill={color.inkMuted} textAnchor="end">
            {tickLabel(tick, currency)}
          </SvgText>
        </G>
      ))}

      {series.map((entry, seriesIndex) => {
        const stroke = chartPalette[seriesIndex % chartPalette.length];

        if (kind === "bar") {
          const slot = bandWidth / series.length;
          const barWidth = Math.max(2, slot * 0.68);
          return rows.map((row, index) => {
            const value = num(row[entry.key]);
            const top = yFor(Math.max(value, 0));
            const base = yFor(0);
            return (
              <Rect
                key={`${entry.key}-${index}`}
                x={PADDING.left + bandWidth * index + slot * seriesIndex + (slot - barWidth) / 2}
                y={Math.min(top, base)}
                width={barWidth}
                height={Math.max(1, Math.abs(base - top))}
                rx={3}
                fill={stroke}
              />
            );
          });
        }

        const points = rows.map((row, index) => `${xCentre(index)},${yFor(num(row[entry.key]))}`);
        if (!points.length) return null;

        if (kind === "scatter") {
          return rows.map((row, index) => (
            <Circle key={`${entry.key}-${index}`} cx={xCentre(index)} cy={yFor(num(row[entry.key]))} r={3.5} fill={stroke} />
          ));
        }

        const line = `M${points.join("L")}`;
        return (
          <G key={entry.key}>
            {kind === "area" ? (
              <Path
                d={`${line}L${xCentre(rows.length - 1)},${yFor(floor)}L${xCentre(0)},${yFor(floor)}Z`}
                fill={stroke}
                fillOpacity={0.12}
              />
            ) : null}
            <Path d={line} stroke={stroke} strokeWidth={2} fill="none" strokeLinejoin="round" strokeLinecap="round" />
            {rows.length <= 12
              ? rows.map((row, index) => (
                  <Circle key={index} cx={xCentre(index)} cy={yFor(num(row[entry.key]))} r={3} fill={color.surface} stroke={stroke} strokeWidth={2} />
                ))
              : null}
          </G>
        );
      })}

      {rows.map((row, index) =>
        index % labelStride === 0 || index === rows.length - 1 ? (
          <SvgText
            key={`label-${index}`}
            x={xCentre(index)}
            y={HEIGHT - 8}
            fontSize={textSize.meta - 1}
            fill={color.inkMuted}
            textAnchor="middle"
          >
            {shortCategory(row[xKey])}
          </SvgText>
        ) : null,
      )}
    </Svg>
  );
}

function PieChart({ width, rows, xKey, valueKey, currency }: { width: number; rows: Row[]; xKey: string; valueKey: string; currency?: string }) {
  const styles = useStyles(makeStyles);
  const { chartPalette } = useAppearance();
  const size = Math.min(width, 200);
  const radius = size / 2 - 4;
  const centre = size / 2;
  const total = rows.reduce((sum, row) => sum + Math.max(0, num(row[valueKey])), 0);

  let angle = -Math.PI / 2;
  const slices = rows.map((row, index) => {
    const value = Math.max(0, num(row[valueKey]));
    const sweep = total > 0 ? (value / total) * Math.PI * 2 : 0;
    const start = angle;
    const end = angle + sweep;
    angle = end;
    const x1 = centre + radius * Math.cos(start);
    const y1 = centre + radius * Math.sin(start);
    const x2 = centre + radius * Math.cos(end);
    const y2 = centre + radius * Math.sin(end);
    // A single slice covering the whole circle cannot be drawn as one arc —
    // the start and end points coincide and the path collapses to nothing.
    const path = sweep >= Math.PI * 2 - 1e-6
      ? `M${centre},${centre - radius}A${radius},${radius} 0 1 1 ${centre - 0.01},${centre - radius}Z`
      : `M${centre},${centre}L${x1},${y1}A${radius},${radius} 0 ${sweep > Math.PI ? 1 : 0} 1 ${x2},${y2}Z`;
    return { path, value, label: formatDimension(row[xKey]), fill: chartPalette[index % chartPalette.length] };
  });

  return (
    <View style={styles.pieRow}>
      <Svg width={size} height={size} accessibilityRole="image">
        {slices.map((slice, index) => <Path key={index} d={slice.path} fill={slice.fill} />)}
      </Svg>
      <View style={styles.pieLegend}>
        {slices.slice(0, 7).map((slice, index) => (
          <View key={index} style={styles.legendItem}>
            <View style={[styles.swatch, { backgroundColor: slice.fill }]} />
            <Type size="meta" color="muted" numberOfLines={1} style={{ flex: 1 }}>{slice.label}</Type>
            <Type size="meta" color="body" tabular>
              {currency ? formatMoney(slice.value, currency) : formatCount(slice.value)}
            </Type>
          </View>
        ))}
      </View>
    </View>
  );
}

function Heatmap({ width, rows, xKey, yKey, valueKey, currency }: { width: number; rows: Row[]; xKey: string; yKey: string; valueKey: string; currency?: string }) {
  const color = useTheme();
  const xs = [...new Set(rows.map((row) => formatDimension(row[xKey])))];
  const ys = [...new Set(rows.map((row) => formatDimension(row[yKey])))];
  const max = Math.max(1, ...rows.map((row) => num(row[valueKey])));

  const left = 54;
  const cell = Math.max(12, (width - left - 4) / Math.max(1, xs.length));
  const height = ys.length * cell + 22;

  return (
    <Svg width={width} height={height} accessibilityRole="image">
      {rows.map((row, index) => {
        const x = xs.indexOf(formatDimension(row[xKey]));
        const y = ys.indexOf(formatDimension(row[yKey]));
        const intensity = num(row[valueKey]) / max;
        return (
          <Rect
            key={index}
            x={left + x * cell + 1}
            y={y * cell + 1}
            width={cell - 2}
            height={cell - 2}
            rx={2}
            fill={color.secondary}
            fillOpacity={0.12 + intensity * 0.8}
          />
        );
      })}
      {ys.map((label, index) => (
        <SvgText key={label} x={left - 6} y={index * cell + cell / 2 + 3} fontSize={textSize.meta - 1} fill={color.inkMuted} textAnchor="end">
          {label.length > 8 ? `${label.slice(0, 7)}…` : label}
        </SvgText>
      ))}
      {xs.map((label, index) =>
        index % Math.max(1, Math.ceil(xs.length / 6)) === 0 ? (
          <SvgText key={label} x={left + index * cell + cell / 2} y={height - 6} fontSize={textSize.meta - 1} fill={color.inkMuted} textAnchor="middle">
            {label.length > 6 ? `${label.slice(0, 5)}…` : label}
          </SvgText>
        ) : null,
      )}
    </Svg>
  );
}

const makeStyles = (color: Palette) => StyleSheet.create({
  legend: { flexDirection: "row", flexWrap: "wrap", gap: space.base, marginTop: space.snug },
  legendItem: { flexDirection: "row", alignItems: "center", gap: space.tight },
  swatch: { width: 8, height: 8, borderRadius: 2 },
  pieRow: { flexDirection: "row", alignItems: "center", gap: space.gutter },
  pieLegend: { flex: 1, gap: space.tight, minWidth: 0 },
});
