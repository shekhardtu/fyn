import { memo, useEffect, useRef } from "react";
import { Animated, Easing, StyleSheet, View } from "react-native";

import { Type } from "@/components/ui";
import type { AgentActivity } from "@/lib/api";
import { formatDuration } from "@/lib/format";
import { radius, space, type Palette } from "@/lib/theme";
import { useStyles } from "@/lib/appearance";

/**
 * What the run is doing, while it does it.
 *
 * This is the whole of the app's perceived speed. The reply itself cannot be
 * streamed token by token — the harness validates a typed contract before any
 * of it is allowed on screen — so the honest thing to show is the real stages
 * as they complete, with their real timings. A spinner would be a lie about how
 * much is happening.
 *
 * Every animation here is driven with `useNativeDriver`, which hands the
 * opacity and transform to the platform's own animation thread. A Zod parse or
 * a chart mounting on the JS thread cannot make the progress stutter — which is
 * the specific thing the web version cannot promise.
 */

const DOT_COUNT = 3;

function Dot({ delay }: { delay: number }) {
  const styles = useStyles(makeStyles);
  const progress = useRef(new Animated.Value(0)).current;

  useEffect(() => {
    const pulse = Animated.loop(
      Animated.sequence([
        Animated.delay(delay),
        Animated.timing(progress, { toValue: 1, duration: 380, easing: Easing.out(Easing.quad), useNativeDriver: true }),
        Animated.timing(progress, { toValue: 0, duration: 380, easing: Easing.in(Easing.quad), useNativeDriver: true }),
        Animated.delay(DOT_COUNT * 120 - delay),
      ]),
    );
    pulse.start();
    return () => pulse.stop();
  }, [progress, delay]);

  return (
    <Animated.View
      style={[
        styles.dot,
        {
          opacity: progress.interpolate({ inputRange: [0, 1], outputRange: [0.3, 1] }),
          transform: [{ translateY: progress.interpolate({ inputRange: [0, 1], outputRange: [0, -3] }) }],
        },
      ]}
    />
  );
}

function Stage({ activity, last }: { activity: AgentActivity; last: boolean }) {
  const styles = useStyles(makeStyles);
  const entering = useRef(new Animated.Value(0)).current;
  const done = activity.status === "completed";
  const failed = activity.status === "failed";

  useEffect(() => {
    Animated.timing(entering, { toValue: 1, duration: 180, easing: Easing.out(Easing.cubic), useNativeDriver: true }).start();
  }, [entering]);

  return (
    <Animated.View
      style={[
        styles.stage,
        {
          opacity: entering,
          transform: [{ translateY: entering.interpolate({ inputRange: [0, 1], outputRange: [6, 0] }) }],
        },
      ]}
    >
      <View style={[styles.marker, done && styles.markerDone, failed && styles.markerFailed, last && !done && styles.markerLive]} />
      <Type size="meta" color={failed ? "danger" : done ? "muted" : "body"} style={{ flex: 1 }} numberOfLines={1}>
        {activity.label}
      </Type>
      {activity.durationMs > 0 ? <Type size="meta" color="muted" tabular>{formatDuration(activity.durationMs)}</Type> : null}
    </Animated.View>
  );
}

export const AgentActivityTrace = memo(function AgentActivityTrace({ activities, reasoningSummary = "" }: { activities: AgentActivity[]; reasoningSummary?: string }) {
  const styles = useStyles(makeStyles);
  // Only the tail is worth the height on a phone; the full trace is persisted
  // on the turn and readable there once the run lands.
  const visible = activities.slice(-4);
  const total = activities.at(-1)?.cumulativeMs ?? 0;

  return (
    <View style={styles.root} accessibilityLiveRegion="polite" accessibilityLabel="fyn AI is working on your message">
      <View style={styles.header}>
        <View style={styles.dots}>
          {Array.from({ length: DOT_COUNT }, (_, index) => <Dot key={index} delay={index * 120} />)}
        </View>
        <Type size="meta" weight="semibold" color="secondary" style={{ flex: 1 }}>
          {visible.at(-1)?.label ?? "Working"}
        </Type>
        {total > 0 ? <Type size="meta" color="muted" tabular>{formatDuration(total)}</Type> : null}
      </View>
      {visible.map((activity, index) => (
        <Stage key={activity.id} activity={activity} last={index === visible.length - 1} />
      ))}
      {reasoningSummary ? (
        <Type size="meta" color="muted" style={styles.reasoning}>
          {reasoningSummary}
        </Type>
      ) : null}
    </View>
  );
});

const makeStyles = (color: Palette) => StyleSheet.create({
  root: {
    gap: space.snug,
    padding: space.base,
    marginBottom: space.wide,
    borderRadius: radius.card,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: color.secondaryLine,
    backgroundColor: color.secondaryTint,
  },
  header: { flexDirection: "row", alignItems: "center", gap: space.snug },
  dots: { flexDirection: "row", gap: 3, alignItems: "center", height: 10 },
  dot: { width: 5, height: 5, borderRadius: 2.5, backgroundColor: color.secondary },
  stage: { flexDirection: "row", alignItems: "center", gap: space.snug },
  marker: { width: 6, height: 6, borderRadius: 3, backgroundColor: color.secondaryLine },
  markerDone: { backgroundColor: color.secondary },
  markerLive: { backgroundColor: color.secondary, opacity: 0.5 },
  markerFailed: { backgroundColor: color.danger },
  reasoning: {
    paddingTop: space.tight,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: color.secondaryLine,
  },
});
