import AsyncStorage from "@react-native-async-storage/async-storage";
import { createAsyncStoragePersister } from "@tanstack/query-async-storage-persister";
import { QueryClient } from "@tanstack/react-query";
import { PersistQueryClientProvider } from "@tanstack/react-query-persist-client";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { useEffect, useRef, useState } from "react";
import { ActivityIndicator, View } from "react-native";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { SafeAreaProvider } from "react-native-safe-area-context";

import { AppLock } from "@/components/app-lock";
import { startConnectivityWatch } from "@/lib/connectivity";
import { AppearanceProvider, useAppearance } from "@/lib/appearance";
import { isUnauthorized } from "@/lib/api";
import { clearSession, loadSession } from "@/lib/session";

/** One client for the app's lifetime.
 *
 *  Financial state is not something to re-fetch on every glance: a transcript
 *  that has already been read does not change behind the reader, and the screens
 *  that do change say so themselves by invalidating. `retry` deliberately skips
 *  401 — a dead session is not a flaky network, and retrying it three times only
 *  delays the trip back to sign-in. */
function buildQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        // Outlives the process so a cold start on a train shows the last
        // transcript instead of a spinner. The persister below is what makes
        // this more than a per-launch cache.
        gcTime: 24 * 60 * 60_000,
        retry: (failureCount, error) => !isUnauthorized(error) && failureCount < 2,
        refetchOnWindowFocus: false,
      },
      mutations: { retry: false },
    },
  });
}

/**
 * Where the cache is kept between launches.
 *
 * Financial history does not change behind the reader, so a transcript that has
 * already been fetched is worth showing immediately — and worth showing at all
 * when the network is gone. What is deliberately *not* persisted is anything
 * mutating: a queued action that replayed after a restart could commit a
 * financial change twice, and the server serialises one turn per conversation
 * precisely so ordering stays honest.
 */
const persister = createAsyncStoragePersister({
  storage: AsyncStorage,
  key: "financial-copilot.cache",
  throttleTime: 2000,
});

export default function RootLayout() {
  startConnectivityWatch();
  return (
    <AppearanceProvider>
      <Shell />
    </AppearanceProvider>
  );
}

function Shell() {
  const { color, scheme } = useAppearance();
  const client = useRef<QueryClient>(undefined);
  client.current ??= buildQueryClient();

  // The Keychain read is a real syscall, and every request needs its result, so
  // the app holds the first frame until it knows whether it has a session. It
  // is single-digit milliseconds — far less than the flash of a sign-in screen
  // that a signed-in user should never have seen.
  const [ready, setReady] = useState(false);
  useEffect(() => {
    let cancelled = false;
    void loadSession().finally(() => { if (!cancelled) setReady(true); });
    return () => { cancelled = true; };
  }, []);

  return (
    <GestureHandlerRootView style={{ flex: 1, backgroundColor: color.surface }}>
      <SafeAreaProvider>
        <PersistQueryClientProvider
          client={client.current}
          persistOptions={{
            persister,
            maxAge: 24 * 60 * 60_000,
            // Only the read-side is restored. A mutation is a financial action
            // and has to be re-taken deliberately, never replayed from disk.
            dehydrateOptions: { shouldDehydrateMutation: () => false },
          }}
        >
          <StatusBar style={scheme === "dark" ? "light" : "dark"} />
          {ready ? (
            <AppLock>
            <Stack
              screenOptions={{
                headerShown: false,
                contentStyle: { backgroundColor: color.surface },
                // The platform's own push, not a JS approximation of one.
                animation: "slide_from_right",
              }}
            >
              <Stack.Screen name="index" />
              <Stack.Screen name="sign-in" options={{ animation: "fade" }} />
              <Stack.Screen name="c/[conversationId]" />
              <Stack.Screen name="conversations" />
              <Stack.Screen name="profile" />
              <Stack.Screen name="overview" />
              <Stack.Screen name="transactions" />
              <Stack.Screen name="gallery" />
            </Stack>
            </AppLock>
          ) : (
            <View style={{ flex: 1, alignItems: "center", justifyContent: "center", backgroundColor: color.surface }}>
              <ActivityIndicator color={color.secondary} />
            </View>
          )}
        </PersistQueryClientProvider>
      </SafeAreaProvider>
    </GestureHandlerRootView>
  );
}

/** A 401 anywhere means the stored token is no longer a session. Dropping it
 *  here rather than at each call site is what keeps every screen's error path
 *  to "show the banner". */
export async function forgetDeadSession() {
  await clearSession();
}
