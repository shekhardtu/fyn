import { Outlet } from "react-router";
import { SiteHeader, useAutoHideSiteHeader } from "@/components/ui/site-header";
import { useWorkspaceShell } from "@/components/workspace";

/** The settings sheet.
 *
 *  Its sections are indexed by the workspace rail, which swaps its
 *  conversation list for them while this page is open (`SettingsRailIndex`).
 *  So the page itself is one column of sheet — no navigation of its own to
 *  compete with the rail already standing beside it. */
export function SettingsPage() {
  const shell = useWorkspaceShell();
  const { headerVisible, updateHeaderForScroll } = useAutoHideSiteHeader();

  return <main
    id="main-content"
    onScroll={(event) => updateHeaderForScroll(event.currentTarget.scrollTop)}
    className="min-h-0 min-w-0 overflow-y-auto bg-ground"
  >
    <SiteHeader
      title="Settings"
      subtitle="Standing instructions for your account"
      subtitleClassName="hidden sm:block"
      hidden={!headerVisible}
      navOpen={shell.navOpen}
      onOpenNav={shell.openNav}
    />

    <div className="mx-auto w-full max-w-[var(--column-w)] px-4 py-7 pb-16 sm:px-6 sm:py-9 lg:px-8">
      <Outlet />
    </div>
  </main>;
}
