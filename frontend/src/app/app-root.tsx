import { Outlet } from "react-router";
import { Providers } from "@/components/providers";
import { FocusModality } from "@/components/ui/focus-modality";

/** Application-wide composition only. Feature state belongs below the router. */
export function AppRoot() {
  return <>
    <FocusModality />
    <a href="#main-content" className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-100 focus:rounded-lg focus:bg-secondary focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:text-on-secondary">
      Skip to main content
    </a>
    <Providers><Outlet /></Providers>
  </>;
}
