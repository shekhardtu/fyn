import NetInfo from "@react-native-community/netinfo";
import { onlineManager } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Platform } from "react-native";

/**
 * Whether the app can currently reach anything.
 *
 * A phone loses the network constantly — lifts, tunnels, the metro — and the
 * failure it produces is not a clean refusal but a socket that accepts and then
 * never answers. Knowing the radio is down is what lets the app say "you are
 * offline" instead of spending its ten-second deadline on a request that cannot
 * work and then blaming the server.
 *
 * There is one source of truth: react-query's `onlineManager`. On native it is
 * fed from NetInfo, which knows about the radio. On web it is left alone,
 * because the browser already reports this through `window`'s online/offline
 * events and NetInfo's web shim is a thinner wrapper over the same thing with
 * more ways to be wrong. Everything else in the app reads the manager rather
 * than either source, so the answer cannot differ between two callers.
 */

let started = false;

export function startConnectivityWatch() {
  if (started) return;
  started = true;
  // The browser's own events are already what react-query listens to.
  if (Platform.OS === "web") return;

  onlineManager.setEventListener((setOnline) =>
    NetInfo.addEventListener((state) => {
      // `isInternetReachable` is preferred over `isConnected`: a captive hotel
      // portal reports a connection and routes nothing, which is exactly the
      // case a naive check gets wrong. It is null until the first probe
      // resolves, and a null is treated as online — refusing to work before we
      // know anything would be worse than trying and failing.
      setOnline(state.isInternetReachable ?? state.isConnected ?? true);
    }),
  );
}

export function useIsOffline(): boolean {
  const [offline, setOffline] = useState(() => !onlineManager.isOnline());

  useEffect(() => {
    // Re-read on mount: a screen that mounts during a dropout must not render
    // as online for a frame.
    setOffline(!onlineManager.isOnline());
    return onlineManager.subscribe((online) => setOffline(!online));
  }, []);

  return offline;
}
