import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { AppState, StyleSheet, View, type AppStateStatus } from "react-native";

import { Button, Type } from "@/components/ui";
import { GRACE_MS, isLockEnabled, lockCapability, unlock } from "@/lib/lock";
import { hasStoredSession } from "@/lib/session";
import { space, type Palette } from "@/lib/theme";
import { useStyles } from "@/lib/appearance";

/**
 * Holds the app behind the device's own authentication.
 *
 * Wrapped around the navigator rather than pushed as a route, for two reasons:
 * a route can be navigated past, and a route unmounts the screen underneath it
 * — so coming back would refetch the whole workspace every time the phone was
 * pocketed. This keeps the tree mounted and simply refuses to show it.
 */
export function AppLock({ children }: { children: ReactNode }) {
  const styles = useStyles(makeStyles);
  const [required, setRequired] = useState<boolean | null>(null);
  const [locked, setLocked] = useState(true);
  const [label, setLabel] = useState("Face ID");
  const [refused, setRefused] = useState(false);

  // When the app was last confirmed to be in the owner's hands.
  const backgroundedAt = useRef<number | null>(null);
  // Guards against the prompt being asked for twice — iOS will not show a
  // second one while the first is up, and the request simply fails.
  const asking = useRef(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const [enabled, capability] = await Promise.all([isLockEnabled(), lockCapability()]);
      if (cancelled) return;
      const needed = enabled && capability.available && hasStoredSession();
      setLabel(capability.label);
      setRequired(needed);
      setLocked(needed);
    })();
    return () => { cancelled = true; };
  }, []);

  const attempt = useCallback(async () => {
    if (asking.current) return;
    asking.current = true;
    setRefused(false);
    const passed = await unlock(label);
    asking.current = false;
    if (passed) setLocked(false);
    else setRefused(true);
  }, [label]);

  // Ask as soon as the gate goes up, so the common case is one glance rather
  // than a screen with a button on it.
  useEffect(() => {
    if (locked && required) void attempt();
  }, [locked, required, attempt]);

  useEffect(() => {
    if (!required) return;
    const subscription = AppState.addEventListener("change", (next: AppStateStatus) => {
      if (next === "background" || next === "inactive") {
        backgroundedAt.current ??= Date.now();
        return;
      }
      if (next !== "active") return;
      const since = backgroundedAt.current;
      backgroundedAt.current = null;
      // Switching to the messaging app to check an amount and coming straight
      // back must not demand a face; leaving the phone must.
      if (since !== null && Date.now() - since > GRACE_MS) setLocked(true);
    });
    return () => subscription.remove();
  }, [required]);

  // Nothing is rendered until it is known whether a gate is needed — a flash of
  // the transcript before the lock appears would defeat the whole feature.
  if (required === null) return <View style={styles.blank} />;
  if (!required || !locked) return <>{children}</>;

  return (
    <View style={styles.lock}>
      <Type size="display" weight="semibold" color="ink" style={{ letterSpacing: -0.8 }}>fyn AI</Type>
      <Type size="body" color="muted" style={styles.line}>
        {refused
          ? `${label} didn’t match. Try again to see your finances.`
          : "Locked. Your finances are behind your device’s own authentication."}
      </Type>
      <Button size="field" onPress={() => void attempt()} style={{ marginTop: space.gutter, minWidth: 200 }}>
        {`Unlock with ${label}`}
      </Button>
    </View>
  );
}

const makeStyles = (color: Palette) => StyleSheet.create({
  blank: { flex: 1, backgroundColor: color.surface },
  lock: { flex: 1, alignItems: "center", justifyContent: "center", padding: space.loose, backgroundColor: color.surface },
  line: { marginTop: space.snug, textAlign: "center", maxWidth: 320 },
});
