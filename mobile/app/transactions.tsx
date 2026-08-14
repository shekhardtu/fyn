import { FlashList } from "@shopify/flash-list";
import { useInfiniteQuery } from "@tanstack/react-query";
import { router } from "expo-router";
import { useEffect, useMemo, useState } from "react";
import { ActivityIndicator, RefreshControl, ScrollView, StyleSheet, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { ListSkeleton } from "@/components/skeleton";
import { Banner, Button, Chip, Divider, Field, Money, Type } from "@/components/ui";
import { loadTransactions } from "@/lib/api";
import { formatDay, formatDimension, formatInstant, formatMoney, formatTransactionClassification } from "@/lib/format";
import type { TransactionListItemOut } from "@/lib/protocol";
import { space, type Palette } from "@/lib/theme";
import { useStyles, useTheme } from "@/lib/appearance";

const PAGE = 50;

const FILTERS: Array<{ id: TransactionListItemOut["transactionType"] | null; label: string }> = [
  { id: null, label: "All" },
  { id: "expense", label: "Expenses" },
  { id: "income", label: "Income" },
  { id: "transfer", label: "Transfers" },
  { id: "investment", label: "Investments" },
];

function tone(type: TransactionListItemOut["transactionType"]) {
  if (type === "income" || type === "refund" || type === "reimbursement" || type === "cash_deposit") return "in" as const;
  if (type === "expense" || type === "investment" || type === "loan_payment" || type === "cash_withdrawal") return "out" as const;
  return "ink" as const;
}

/**
 * The ledger.
 *
 * The conversation is the primary interface, but "show me everything" is a
 * question a list answers better than a chat turn does — and it is the screen
 * people reach for when they distrust a number.
 */
export default function TransactionsScreen() {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  const insets = useSafeAreaInsets();
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [transactionType, setTransactionType] = useState<TransactionListItemOut["transactionType"] | null>(null);

  // Typing should not fire a request per keystroke over a cellular link.
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const ledger = useInfiniteQuery({
    queryKey: ["transactions", debounced, transactionType],
    queryFn: ({ pageParam }) => loadTransactions({ limit: PAGE, offset: pageParam, search: debounced, transactionType }),
    initialPageParam: 0,
    // The endpoint pages by offset and does not report a total, so a short page
    // is the only signal that the end has been reached.
    getNextPageParam: (last, all) => (last.length < PAGE ? undefined : all.length * PAGE),
  });

  const rows = useMemo(() => ledger.data?.pages.flat() ?? [], [ledger.data]);

  return (
    <View style={styles.root}>
      <View style={[styles.header, { paddingTop: insets.top + space.snug }]}>
        <Button variant="ghost" size="control" onPress={() => router.back()} accessibilityLabel="Back">← Back</Button>
        <Type size="control" weight="semibold" color="ink" style={{ flex: 1, textAlign: "center" }}>Transactions</Type>
        <View style={{ width: 64 }} />
      </View>

      <View style={styles.controls}>
        <View style={{ paddingHorizontal: space.gutter }}>
        <Field
          value={search}
          onChangeText={setSearch}
          placeholder="Search merchants"
          autoCapitalize="none"
          autoCorrect={false}
          returnKeyType="search"
          accessibilityLabel="Search transactions by merchant"
        />
        </View>
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={[styles.filters, { paddingLeft: space.gutter }]}
          keyboardShouldPersistTaps="handled"
        >
          {FILTERS.map((filter) => (
            <Chip
              key={filter.label}
              label={filter.label}
              selected={filter.id === transactionType}
              onPress={() => setTransactionType(filter.id)}
            />
          ))}
        </ScrollView>
      </View>
      <Divider />

      {ledger.isPending ? (
        <ListSkeleton rows={8} height={64} />
      ) : ledger.isError ? (
        <View style={{ padding: space.gutter, gap: space.base }}>
          <Banner>{(ledger.error as Error).message}</Banner>
          <Button variant="outline" onPress={() => void ledger.refetch()}>Try again</Button>
        </View>
      ) : (
        <FlashList
          data={rows}
          keyExtractor={(row) => row.id}
          contentContainerStyle={{ paddingBottom: insets.bottom + space.section }}
          refreshControl={
            <RefreshControl refreshing={ledger.isRefetching && !ledger.isFetchingNextPage} onRefresh={() => void ledger.refetch()} tintColor={color.secondary} />
          }
          onEndReachedThreshold={0.6}
          onEndReached={() => { if (ledger.hasNextPage && !ledger.isFetchingNextPage) void ledger.fetchNextPage(); }}
          ListEmptyComponent={
            <Type size="note" color="muted" style={styles.empty}>
              {debounced ? `Nothing matches “${debounced}”.` : "No transactions recorded yet."}
            </Type>
          }
          ListFooterComponent={ledger.isFetchingNextPage ? <ActivityIndicator color={color.secondary} style={{ margin: space.gutter }} /> : null}
          renderItem={({ item }) => <Row row={item} />}
        />
      )}
    </View>
  );
}

function Row({ row }: { row: TransactionListItemOut }) {
  const styles = useStyles(makeStyles);
  const removed = Boolean(row.deletedAt);
  const detail = [formatTransactionClassification(row.transactionType, row.category, row.subcategory), formatDay(row.transactionAt.slice(0, 10)), row.location, removed ? `Removed ${formatInstant(row.deletedAt)}` : null].filter(Boolean).join(" · ");
  return (
    <View>
      <View style={styles.row}>
        <View style={{ flex: 1, minWidth: 0 }}>
          <Type size="control" weight="medium" color={removed ? "muted" : "ink"} numberOfLines={1} style={removed ? styles.struck : undefined}>
            {row.merchant || formatDimension(row.transactionType)}
          </Type>
          <Type size="meta" color="muted" numberOfLines={1}>{detail}</Type>
        </View>
        <View style={{ alignItems: "flex-end" }}>
          <Money value={formatMoney(row.amountMinor, row.currency)} size="control" color={removed ? "muted" : tone(row.transactionType)} style={removed ? styles.struck : undefined} />
          {removed ? <Type size="meta" color="danger">Removed</Type> : row.status === "provisional" ? <Type size="meta" color="attention">Provisional</Type> : null}
          {row.sourceCount > 1 ? <Type size="meta" color="muted">{row.sourceCount} sources</Type> : null}
        </View>
      </View>
      <Divider />
    </View>
  );
}

const makeStyles = (color: Palette) => StyleSheet.create({
  root: { flex: 1, backgroundColor: color.surface },
  centre: { flex: 1, alignItems: "center", justifyContent: "center" },
  header: { flexDirection: "row", alignItems: "center", paddingHorizontal: space.snug, paddingBottom: space.snug },
  controls: { paddingBottom: space.base, gap: space.snug },
  filters: { flexDirection: "row", gap: space.snug, paddingRight: space.gutter },
  row: { flexDirection: "row", alignItems: "center", gap: space.base, paddingHorizontal: space.gutter, paddingVertical: space.base, minHeight: 64 },
  struck: { textDecorationLine: "line-through" },
  empty: { padding: space.section, textAlign: "center" },
});
