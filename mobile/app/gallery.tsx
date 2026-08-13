import { router, useLocalSearchParams } from "expo-router";
import { useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { Banner, Button, Chip, Divider, Type } from "@/components/ui";
import { WidgetView } from "@/components/widget-renderer";
import { useStyles } from "@/lib/appearance";
import { widgetSchema } from "@/lib/protocol";
import { space, type Palette } from "@/lib/theme";
import { WIDGET_FIXTURES } from "@/lib/widget-fixtures";

/**
 * Every widget type, drawn side by side.
 *
 * The agent chooses which widgets to emit, and for over half the twenty-five it
 * never does in ordinary use — the calculators, the loan strategy and the
 * reconciliation review need a conversation that is hard to provoke on demand.
 * That left most of the renderer shipped without ever having been drawn once,
 * which is not a state a financial UI should ship in.
 *
 * This is a development surface, reachable from Settings, not a product screen.
 * Each fixture is put through the same Zod parse a live payload takes, so a
 * contract drift shows up here as a failed card rather than as a crash in
 * somebody's conversation.
 */
export default function GalleryScreen() {
  const insets = useSafeAreaInsets();
  const styles = useStyles(makeStyles);
  // `?only=` narrows the gallery to one type, which is how a single widget can
  // be pulled up by deep link and looked at on its own.
  const { only } = useLocalSearchParams<{ only?: string }>();
  const shown = only ? WIDGET_FIXTURES.filter((w) => w.type === only) : WIDGET_FIXTURES;
  const [spent, setSpent] = useState(false);
  const [disabled, setDisabled] = useState(false);

  return (
    <View style={styles.root}>
      <View style={[styles.header, { paddingTop: insets.top + space.snug }]}>
        <Button variant="ghost" size="control" onPress={() => router.back()} accessibilityLabel="Back">← Back</Button>
        <Type size="control" weight="semibold" color="ink" style={{ flex: 1, textAlign: "center" }}>Widget gallery</Type>
        <View style={{ width: 64 }} />
      </View>
      <Divider />

      <ScrollView contentContainerStyle={{ padding: space.gutter, gap: space.gutter, paddingBottom: insets.bottom + space.section }}>
        <Type size="note" color="muted">
          {shown.length} widget types against fixture payloads. Toggle the states a real
          transcript puts them through.
        </Type>
        <View style={styles.toggles}>
          <Chip label="Spent" selected={spent} onPress={() => setSpent((value) => !value)} />
          <Chip label="Disabled" selected={disabled} onPress={() => setDisabled((value) => !value)} />
        </View>

        {shown.map((fixture) => {
          const parsed = widgetSchema.safeParse(fixture);
          return (
            <View key={fixture.id} style={{ gap: space.snug }}>
              <Type size="meta" weight="semibold" color="muted" style={{ letterSpacing: 0.8 }}>
                {fixture.type.replaceAll("_", " ").toUpperCase()}
              </Type>
              {parsed.success ? (
                <WidgetView
                  widget={fixture}
                  currency="INR"
                  disabled={disabled || spent}
                  spent={spent}
                  pending={false}
                  onAction={(id, action) => console.log("gallery action", id, action)}
                />
              ) : (
                <Banner>
                  {`This fixture no longer matches the ${fixture.type} contract: ${parsed.error.issues[0]?.message ?? "unknown"}`}
                </Banner>
              )}
            </View>
          );
        })}
      </ScrollView>
    </View>
  );
}

const makeStyles = (color: Palette) => StyleSheet.create({
  root: { flex: 1, backgroundColor: color.surface },
  header: { flexDirection: "row", alignItems: "center", paddingHorizontal: space.snug, paddingBottom: space.snug },
  toggles: { flexDirection: "row", gap: space.snug },
});
