import { useQuery } from "@tanstack/react-query";
import { router } from "expo-router";
import { useState } from "react";
import { ActivityIndicator, RefreshControl, ScrollView, StyleSheet, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { Banner, Button, Card, CardHeader, Divider, Money, Pill, Type } from "@/components/ui";
import { loadOverview } from "@/lib/api";
import { formatCount, formatMoney } from "@/lib/format";
import { space, type Palette } from "@/lib/theme";
import { useStyles, useTheme } from "@/lib/appearance";

/**
 * The month at a glance — the one screen that answers "how am I doing?"
 * without a conversation.
 *
 * Everything here is already computed by the server's overview endpoint. The
 * screen deliberately adds no arithmetic of its own: a figure this app shows
 * and a figure the conversation shows must never be able to disagree.
 */
export default function OverviewScreen() {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  const insets = useSafeAreaInsets();
  const [month] = useState<string | undefined>(undefined);

  const overview = useQuery({ queryKey: ["overview", month ?? "current"], queryFn: () => loadOverview(month) });

  return (
    <View style={styles.root}>
      <View style={[styles.header, { paddingTop: insets.top + space.snug }]}>
        <Button variant="ghost" size="control" onPress={() => router.back()} accessibilityLabel="Back">← Back</Button>
        <Type size="control" weight="semibold" color="ink" style={{ flex: 1, textAlign: "center" }}>Overview</Type>
        <View style={{ width: 64 }} />
      </View>
      <Divider />

      <ScrollView
        contentContainerStyle={{ padding: space.gutter, gap: space.gutter, paddingBottom: insets.bottom + space.section }}
        refreshControl={
          <RefreshControl refreshing={overview.isRefetching} onRefresh={() => void overview.refetch()} tintColor={color.secondary} />
        }
      >
        {overview.isPending ? (
          <View style={styles.centre}><ActivityIndicator color={color.secondary} /></View>
        ) : overview.isError ? (
          <View style={{ gap: space.base }}>
            <Banner>{(overview.error as Error).message}</Banner>
            <Button variant="outline" onPress={() => void overview.refetch()}>Try again</Button>
          </View>
        ) : (
          <Summary data={overview.data} />
        )}
      </ScrollView>
    </View>
  );
}

function Summary({ data }: { data: NonNullable<ReturnType<typeof useQuery<Awaited<ReturnType<typeof loadOverview>>>>["data"]> }) {
  const styles = useStyles(makeStyles);
  const { summary, period, categories } = data;
  const currency = summary.currency;
  // A month that is still running has not "increased" on a finished one yet;
  // saying so would read as a verdict on a comparison that is not complete.
  const comparable = summary.previousSpentMinor > 0;
  const up = summary.changeMinor > 0;

  return (
    <>
      <Card>
        <CardHeader title={period.label} body={period.isCurrent ? "So far this month" : undefined} />
        <View style={styles.body}>
          <View>
            <Type size="meta" weight="semibold" color="muted" style={styles.eyebrow}>SPENT</Type>
            <Money value={formatMoney(summary.spentMinor, currency)} size="display" style={{ marginTop: space.tight }} />
            <Type size="note" color="muted" style={{ marginTop: space.tight }}>
              across {formatCount(summary.expenseCount, 0)} {summary.expenseCount === 1 ? "expense" : "expenses"}
            </Type>
          </View>

          {comparable ? (
            <Pill tone={up ? "out" : "in"}>
              {`${up ? "↑" : "↓"} ${formatMoney(Math.abs(summary.changeMinor), currency)}`}
              {summary.changePercent !== null ? ` · ${formatCount(Math.abs(summary.changePercent), 0)}%` : ""}
              {" vs last month"}
            </Pill>
          ) : null}

          <Divider />

          <View style={styles.split}>
            <View style={{ flex: 1 }}>
              <Type size="meta" weight="semibold" color="muted" style={styles.eyebrow}>IN</Type>
              <Money value={formatMoney(summary.incomeMinor, currency)} size="title" color={summary.incomeMinor > 0 ? "in" : "ink"} />
            </View>
            <View style={{ flex: 1 }}>
              <Type size="meta" weight="semibold" color="muted" style={styles.eyebrow}>NET</Type>
              <Money
                value={formatMoney(summary.netMinor, currency)}
                size="title"
                color={summary.netMinor < 0 ? "out" : summary.netMinor > 0 ? "in" : "ink"}
              />
            </View>
          </View>
        </View>
      </Card>

      <Card>
        <CardHeader title="Where it went" body={categories.length ? undefined : "Nothing recorded for this month yet."} />
        {categories.map((entry, index) => (
          <View key={entry.id}>
            {index ? <Divider /> : null}
            <View style={styles.category}>
              <View style={styles.categoryRow}>
                <Type size="control" weight="medium" color="ink" style={{ flex: 1 }} numberOfLines={1}>{entry.label}</Type>
                <Money value={formatMoney(entry.amountMinor, currency)} size="control" />
              </View>
              <View style={styles.track}>
                <View style={[styles.fill, { width: `${Math.max(0, Math.min(100, entry.sharePercent))}%` }]} />
              </View>
              <Type size="meta" color="muted">
                {formatCount(entry.sharePercent, 0)}% · {formatCount(entry.count, 0)} {entry.count === 1 ? "expense" : "expenses"}
              </Type>
            </View>
          </View>
        ))}
      </Card>
    </>
  );
}

const makeStyles = (color: Palette) => StyleSheet.create({
  root: { flex: 1, backgroundColor: color.surface },
  centre: { paddingVertical: space.section, alignItems: "center" },
  header: { flexDirection: "row", alignItems: "center", paddingHorizontal: space.snug, paddingBottom: space.snug },
  body: { gap: space.base, paddingHorizontal: space.gutter, paddingVertical: space.base },
  eyebrow: { letterSpacing: 0.8 },
  split: { flexDirection: "row", gap: space.gutter },
  category: { gap: space.snug, paddingHorizontal: space.gutter, paddingVertical: space.base },
  categoryRow: { flexDirection: "row", alignItems: "center", gap: space.base },
  track: { height: 6, borderRadius: 3, backgroundColor: color.sunken, overflow: "hidden" },
  fill: { height: "100%", borderRadius: 3, backgroundColor: color.secondary },
});
