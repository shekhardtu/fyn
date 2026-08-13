import { useEffect, useRef } from "react";
import { Animated, Easing, StyleSheet, View, type StyleProp, type ViewStyle } from "react-native";

import { useStyles, useTheme } from "@/lib/appearance";
import { radius, space, type Palette } from "@/lib/theme";

/**
 * The shape of what is coming, while it comes.
 *
 * A spinner says "wait" and nothing else; a skeleton says what will be there,
 * which makes the same wait read as shorter and stops the layout jumping when
 * the data lands. The shapes below are deliberately the real ones — a row that
 * is 64pt tall here is 64pt tall when it is filled.
 *
 * The pulse runs on the native driver, so a slow parse on the JS thread cannot
 * make the placeholder stutter — which would signal the opposite of progress.
 */
function usePulse() {
  const value = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    const pulse = Animated.loop(
      Animated.sequence([
        Animated.timing(value, { toValue: 1, duration: 700, easing: Easing.inOut(Easing.quad), useNativeDriver: true }),
        Animated.timing(value, { toValue: 0, duration: 700, easing: Easing.inOut(Easing.quad), useNativeDriver: true }),
      ]),
    );
    pulse.start();
    return () => pulse.stop();
  }, [value]);
  return value.interpolate({ inputRange: [0, 1], outputRange: [0.45, 1] });
}

export function Bone({ width, height = 12, style }: { width: number | `${number}%`; height?: number; style?: StyleProp<ViewStyle> }) {
  const color = useTheme();
  const opacity = usePulse();
  return (
    <Animated.View
      style={[{ width, height, borderRadius: radius.chip, backgroundColor: color.sunken, opacity }, style]}
    />
  );
}

/** A transcript that has not arrived: one asked question, one card of answer. */
export function TranscriptSkeleton() {
  const styles = useStyles(makeStyles);
  return (
    <View style={styles.transcript} accessibilityLabel="Loading your conversation">
      <View style={{ alignSelf: "flex-end", gap: space.snug }}>
        <Bone width={180} height={16} />
      </View>
      <View style={{ gap: space.snug }}>
        <Bone width="90%" height={14} />
        <Bone width="70%" height={14} />
      </View>
      <View style={styles.card}>
        <Bone width={140} height={16} />
        <Bone width={96} height={28} style={{ marginTop: space.base }} />
        <Bone width="100%" height={6} style={{ marginTop: space.base }} />
        <Bone width="100%" height={6} style={{ marginTop: space.snug }} />
      </View>
    </View>
  );
}

/** Rows of a list, at the height the real rows will be. */
export function ListSkeleton({ rows = 6, height = 64 }: { rows?: number; height?: number }) {
  const styles = useStyles(makeStyles);
  return (
    <View accessibilityLabel="Loading">
      {Array.from({ length: rows }, (_, index) => (
        <View key={index} style={[styles.row, { minHeight: height }]}>
          <View style={{ flex: 1, gap: space.snug }}>
            <Bone width={index % 3 === 0 ? 200 : 150} height={14} />
            <Bone width={110} height={11} />
          </View>
          <Bone width={72} height={14} />
        </View>
      ))}
    </View>
  );
}

const makeStyles = (color: Palette) => StyleSheet.create({
  transcript: { gap: space.wide, padding: space.gutter },
  card: {
    padding: space.gutter,
    borderRadius: radius.card,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: color.line,
    backgroundColor: color.surface,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.base,
    paddingHorizontal: space.gutter,
    paddingVertical: space.base,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: color.line,
  },
});
