import { createAsyncStoragePersister } from "@tanstack/query-async-storage-persister";
import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { PersistQueryClientProvider } from "@tanstack/react-query-persist-client";
import { del, get, set } from "idb-keyval";
import { LucideProvider, WifiOff } from "lucide-react";
import { useState, useSyncExternalStore } from "react";
import { TooltipProvider } from "@/components/ui/tooltip";

/* Read queries survive a restart so an offline launch still shows the last
 * known ledger; mutations are deliberately never persisted — replaying a
 * financial change after a restart could commit it twice. Storage is IndexedDB
 * rather than localStorage because transcripts run to hundreds of kilobytes. */
const CACHE_KEY = "fyn.query-cache";
// jsdom has no IndexedDB; tests run with the plain provider below.
const persister = typeof indexedDB === "undefined" ? undefined : createAsyncStoragePersister({
  storage: {
    getItem: (key: string) => get<string>(key).then((value) => value ?? null),
    setItem: (key: string, value: string) => set(key, value),
    removeItem: (key: string) => del(key),
  },
  key: CACHE_KEY,
});

function useOnline() {
  return useSyncExternalStore(
    (notify) => onlineManager.subscribe(notify),
    () => onlineManager.isOnline(),
    () => true,
  );
}

/** One quiet strip, only while the network is actually gone. The data on
 *  screen is the persisted cache; the copy says so instead of pretending. */
function OfflineNotice() {
  const online = useOnline();
  if (online) return null;
  return <div role="status" className="fixed inset-x-0 top-0 z-100 flex items-center justify-center gap-2 bg-attention-tint px-4 pt-[calc(0.375rem+env(safe-area-inset-top))] pb-1.5 text-note font-medium text-attention">
    <WifiOff size={13} aria-hidden /> Offline — showing your last synced data.
  </div>;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(() => new QueryClient({
    defaultOptions: {
      // A transcript is the largest thing this app caches — hundreds of
      // messages with their widget payloads, which for a long conversation runs
      // to hundreds of kilobytes. Fifteen seconds of freshness keeps thread
      // switching instant; a day of garbage-collection lifetime is what gives
      // the persisted cache something to restore after an offline launch.
      queries: { staleTime: 15_000, gcTime: 24 * 60 * 60 * 1000, retry: 1 },
      mutations: { retry: 0 },
    },
  }));
  // One place decides how icons are drawn. Lucide's default 2px stroke reads
  // heavy beside 13px control text; 1.5 with the mitred joins set in
  // `globals.css` is what makes the set look engraved rather than sketched.
  // 15px matches the cap height of the body face, so an icon sitting inline
  // with a label lines up with it instead of overhanging.
  const shell = <LucideProvider size={15} strokeWidth={1.5}>
    <TooltipProvider>
      <OfflineNotice />
      {children}
    </TooltipProvider>
  </LucideProvider>;
  if (!persister) return <QueryClientProvider client={client}>{shell}</QueryClientProvider>;
  return <PersistQueryClientProvider
    client={client}
    persistOptions={{
      persister,
      maxAge: 24 * 60 * 60 * 1000,
      // Bump to discard every persisted entry on a breaking cache-shape change.
      buster: "v1",
      dehydrateOptions: {
        shouldDehydrateMutation: () => false,
      },
    }}
  >
    {shell}
  </PersistQueryClientProvider>;
}
