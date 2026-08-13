import { useQuery } from "@tanstack/react-query";
import { Redirect } from "expo-router";
import { ActivityIndicator, StyleSheet, View } from "react-native";

import { Banner, Button, Type } from "@/components/ui";
import { bootstrap, isUnauthorized } from "@/lib/api";
import { HOLDS_TOKEN, clearSession, hasStoredSession } from "@/lib/session";
import { space, type Palette } from "@/lib/theme";
import { useStyles, useTheme } from "@/lib/appearance";

/**
 * The first decision the app makes: sign-in, or straight into the conversation.
 *
 * `/api/bootstrap` answers both questions in one round trip — who you are and
 * which thread you were last in — so this does not ask twice. A stored token
 * that the server no longer honours is indistinguishable from no token at all
 * from here, and both end in the same place.
 */
export default function Index() {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  // On native, no stored token means there is nothing to try and the trip to
  // sign-in can be made without a round trip. On web the session is an
  // httpOnly cookie this code cannot read, so the only honest way to find out
  // is to ask — `/api/bootstrap` answers with a 401 if the cookie is dead.
  const worthTrying = !HOLDS_TOKEN || hasStoredSession();

  const initial = useQuery({
    queryKey: ["bootstrap"],
    queryFn: bootstrap,
    enabled: worthTrying,
    retry: false,
  });

  if (!worthTrying) return <Redirect href="/sign-in" />;

  if (initial.isPending) {
    return (
      <View style={styles.centre}>
        <ActivityIndicator color={color.secondary} />
      </View>
    );
  }

  if (initial.isError) {
    // A dead session is not a failure to report — it is a trip back to sign-in.
    if (isUnauthorized(initial.error)) {
      void clearSession();
      return <Redirect href="/sign-in" />;
    }
    return (
      <View style={[styles.centre, { padding: space.loose, gap: space.gutter }]}>
        <Banner>{(initial.error as Error).message}</Banner>
        <Button variant="outline" onPress={() => void initial.refetch()}>Try again</Button>
      </View>
    );
  }

  return <Redirect href={{ pathname: "/c/[conversationId]", params: { conversationId: initial.data.active_conversation.id } }} />;
}

/** Shown while the very first bootstrap is in flight, and nowhere else. */
export function Splash() {
  const styles = useStyles(makeStyles);
  return (
    <View style={styles.centre}>
      <Type size="title" weight="semibold" color="ink">fyn AI</Type>
    </View>
  );
}

const makeStyles = (color: Palette) => StyleSheet.create({
  centre: { flex: 1, alignItems: "center", justifyContent: "center", backgroundColor: color.surface },
});
