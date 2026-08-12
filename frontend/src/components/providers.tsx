"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LucideProvider } from "lucide-react";
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
  // One place decides how icons are drawn. Lucide's default 2px stroke reads
  // heavy beside 13px control text; 1.5 with the mitred joins set in
  // `globals.css` is what makes the set look engraved rather than sketched.
  // 15px matches the cap height of the body face, so an icon sitting inline
  // with a label lines up with it instead of overhanging.
  return <QueryClientProvider client={client}>
    <LucideProvider size={15} strokeWidth={1.5}>
      <TooltipProvider>{children}</TooltipProvider>
    </LucideProvider>
  </QueryClientProvider>;
}
