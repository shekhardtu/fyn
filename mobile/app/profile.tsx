import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { router } from "expo-router";
import { useState } from "react";
import { ActivityIndicator, ScrollView, StyleSheet, Switch, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { Banner, Button, Card, CardHeader, Divider, Type } from "@/components/ui";
import {
  deleteAllData,
  getPrivacyStatus,
  getProfile,
  revokeSource,
  setLocationEnabled,
  signOut,
} from "@/lib/api";
import { formatDimension } from "@/lib/format";
import { isLockEnabled, lockCapability, setLockEnabled, unlock } from "@/lib/lock";
import { clearSession } from "@/lib/session";
import { space, type Palette } from "@/lib/theme";
import { useStyles, useTheme } from "@/lib/appearance";

/**
 * Who you are signed in as, and the switches that decide what the app is
 * allowed to keep.
 *
 * Deleting everything is deliberately not a one-tap action, and it is the only
 * control on this screen that asks twice.
 */
export default function ProfileScreen() {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  const insets = useSafeAreaInsets();
  const queryClient = useQueryClient();
  const [problem, setProblem] = useState<string | null>(null);
  const [confirmingErase, setConfirmingErase] = useState(false);

  const profile = useQuery({ queryKey: ["profile"], queryFn: getProfile });
  const privacy = useQuery({ queryKey: ["privacy"], queryFn: getPrivacyStatus });

  const capability = useQuery({ queryKey: ["lock-capability"], queryFn: lockCapability });
  const lockOn = useQuery({ queryKey: ["lock-enabled"], queryFn: isLockEnabled });

  const lock = useMutation({
    // Turning the lock ON proves the device can actually open it first. A
    // switch that enables a gate nobody can pass would lock the owner out of
    // their own finances until they reinstalled.
    mutationFn: async (enabled: boolean) => {
      if (enabled && !(await unlock(capability.data?.label ?? "your device"))) {
        throw new Error("That didn’t verify, so the lock is still off.");
      }
      await setLockEnabled(enabled);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["lock-enabled"] }),
    onError: (cause: Error) => setProblem(cause.message),
  });

  const location = useMutation({
    mutationFn: (enabled: boolean) => setLocationEnabled(enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["privacy"] }),
    onError: (cause: Error) => setProblem(cause.message),
  });

  const revoke = useMutation({
    mutationFn: (source: string) => revokeSource(source),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["privacy"] }),
    onError: (cause: Error) => setProblem(cause.message),
  });

  const leave = useMutation({
    mutationFn: async () => { await signOut(); await clearSession(); },
    onSuccess: () => { queryClient.clear(); router.replace("/sign-in"); },
  });

  const erase = useMutation({
    mutationFn: async () => { await deleteAllData(); await signOut(); await clearSession(); },
    onSuccess: () => { queryClient.clear(); router.replace("/sign-in"); },
    onError: (cause: Error) => { setConfirmingErase(false); setProblem(cause.message); },
  });

  return (
    <View style={styles.root}>
      <View style={[styles.header, { paddingTop: insets.top + space.snug }]}>
        <Button variant="ghost" size="control" onPress={() => router.back()} accessibilityLabel="Back">← Back</Button>
        <Type size="control" weight="semibold" color="ink" style={{ flex: 1, textAlign: "center" }}>Settings</Type>
        <View style={{ width: 64 }} />
      </View>
      <Divider />

      <ScrollView contentContainerStyle={{ padding: space.gutter, gap: space.gutter, paddingBottom: insets.bottom + space.section }}>
        {problem ? <Banner>{problem}</Banner> : null}

        {profile.isPending ? (
          <ActivityIndicator color={color.secondary} />
        ) : profile.isError ? (
          <Banner>{(profile.error as Error).message}</Banner>
        ) : (
          <Card>
            <CardHeader title={profile.data.displayName} body={profile.data.email ?? profile.data.phone ?? undefined} />
            <View style={styles.body}>
              <Detail label="Currency" value={profile.data.currency} />
              <Detail label="Timezone" value={profile.data.timezone} />
              <Detail label="Sign-in methods" value={profile.data.identities.map((identity) => formatDimension(identity.provider)).join(", ") || "—"} />
            </View>
          </Card>
        )}

        <Card>
          <CardHeader title="Your money" body="The same figures the conversation works from." />
          <View style={styles.body}>
            <Button variant="outline" block onPress={() => router.push("/overview")}>This month’s overview</Button>
            <Button variant="outline" block onPress={() => router.push("/transactions")}>All transactions</Button>
            {__DEV__ ? (
              <Button variant="ghost" block onPress={() => router.push("/gallery")}>Widget gallery (dev)</Button>
            ) : null}
          </View>
        </Card>

        {privacy.data ? (
          <Card>
            <CardHeader title="Privacy" body="What fyn AI is allowed to keep about you." />
            <View style={styles.body}>
              <View style={styles.switchRow}>
                <View style={{ flex: 1 }}>
                  <Type size="control" color="ink">Location enrichment</Type>
                  <Type size="meta" color="muted">Off by default. No location is ever invented.</Type>
                </View>
                <Switch
                  value={privacy.data.locationEnabled}
                  onValueChange={(next) => location.mutate(next)}
                  disabled={location.isPending}
                  trackColor={{ true: color.secondary, false: color.lineStrong }}
                />
              </View>
              {Object.entries(privacy.data.sources).map(([source, active]) => (
                <View key={source} style={styles.switchRow}>
                  <Type size="control" color="ink" style={{ flex: 1 }}>{formatDimension(source)}</Type>
                  {active ? (
                    <Button size="control" variant="outline" onPress={() => revoke.mutate(source)} busy={revoke.isPending}>Revoke</Button>
                  ) : (
                    <Type size="meta" color="muted">Revoked</Type>
                  )}
                </View>
              ))}
            </View>
          </Card>
        ) : null}

        <Card>
          <CardHeader title="This device" />
          <View style={styles.body}>
            {capability.data?.available ? (
              <View style={styles.switchRow}>
                <View style={{ flex: 1 }}>
                  <Type size="control" color="ink">{`Require ${capability.data.label}`}</Type>
                  <Type size="meta" color="muted">Asks again after a minute away from the app.</Type>
                </View>
                <Switch
                  value={lockOn.data ?? false}
                  onValueChange={(next) => lock.mutate(next)}
                  disabled={lock.isPending}
                  trackColor={{ true: color.secondary, false: color.lineStrong }}
                />
              </View>
            ) : capability.isSuccess ? (
              <Type size="meta" color="muted">
                Set up a passcode or biometrics on this device to lock the app.
              </Type>
            ) : null}
            <Button variant="outline" block onPress={() => leave.mutate()} busy={leave.isPending}>Sign out</Button>
            <Type size="meta" color="muted">
              Signs out this device only. Your other signed-in devices are untouched.
            </Type>
          </View>
        </Card>

        <Card style={{ borderColor: color.dangerLine }}>
          <CardHeader title="Delete everything" caution body="Erases every transaction, conversation, and preference. This cannot be undone." />
          <View style={styles.body}>
            {confirmingErase ? (
              <View style={{ gap: space.snug }}>
                <Banner>This permanently erases all of your financial data. There is no undo.</Banner>
                <Button variant="danger" block onPress={() => erase.mutate()} busy={erase.isPending}>Yes, delete everything</Button>
                <Button variant="ghost" block onPress={() => setConfirmingErase(false)}>Keep my data</Button>
              </View>
            ) : (
              <Button variant="danger" block onPress={() => setConfirmingErase(true)}>Delete all my data</Button>
            )}
          </View>
        </Card>
      </ScrollView>
    </View>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  const styles = useStyles(makeStyles);
  return (
    <View style={styles.detail}>
      <Type size="note" color="muted">{label}</Type>
      <Type size="note" color="ink" weight="medium">{value}</Type>
    </View>
  );
}

const makeStyles = (color: Palette) => StyleSheet.create({
  root: { flex: 1, backgroundColor: color.surface },
  header: { flexDirection: "row", alignItems: "center", paddingHorizontal: space.snug, paddingBottom: space.snug },
  body: { gap: space.base, paddingHorizontal: space.gutter, paddingVertical: space.base },
  detail: { flexDirection: "row", justifyContent: "space-between", gap: space.base },
  switchRow: { flexDirection: "row", alignItems: "center", gap: space.base, minHeight: 44 },
});
