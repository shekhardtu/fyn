"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { TooltipProvider } from "@/components/ui/tooltip";

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(() => new QueryClient({
    defaultOptions: {
      // A transcript is the largest thing this app caches — hundreds of
      // messages with their widget payloads, which for a long conversation runs
      // to hundreds of kilobytes. Two minutes is long enough that stepping
      // between two threads stays instant, and short enough that an afternoon
      // of browsing does not keep every one of them resident.
      queries: { staleTime: 15_000, gcTime: 120_000, retry: 1 },
      mutations: { retry: 0 },
    },
  }));
  return <QueryClientProvider client={client}><TooltipProvider>{children}</TooltipProvider></QueryClientProvider>;
}
