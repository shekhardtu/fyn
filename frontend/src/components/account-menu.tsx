import { Menu } from "@base-ui/react/menu";
import { Bot, ChartColumn, ChevronsUpDown, HandCoins, LayoutDashboard, Loader2, LogOut, ReceiptText, SlidersHorizontal, Tags, UserRound } from "lucide-react";
import type { ComponentType } from "react";
import type { Bootstrap } from "@/lib/protocol";
import { appPaths } from "@/routing/paths";

type AccountMenuProps = {
  user: Bootstrap["user"] | null;
  personalLendingAvailable: boolean;
  signingOut: boolean;
  onNavigate: (path: string) => void;
  onSignOut: () => void;
};

type MenuDestination = {
  label: string;
  path: string;
  icon: ComponentType<{ size?: number; className?: string; "aria-hidden"?: boolean }>;
};

const QUICK_LINKS: MenuDestination[] = [
  { label: "Overview", path: appPaths.overview, icon: LayoutDashboard },
  { label: "Dashboards", path: appPaths.dashboards, icon: ChartColumn },
  { label: "Transactions", path: appPaths.transactions, icon: ReceiptText },
  { label: "Personal lending", path: appPaths.loans, icon: HandCoins },
  { label: "Categories", path: appPaths.categories, icon: Tags },
];

const ACCOUNT_LINKS: MenuDestination[] = [
  { label: "Profile & sign-in", path: appPaths.settings, icon: UserRound },
  { label: "Agent settings", path: appPaths.settingsAgent, icon: Bot },
  { label: "Appearance & app", path: appPaths.settingsApp, icon: SlidersHorizontal },
];

const menuItemClass = "flex min-h-9 cursor-default items-center gap-2.5 rounded-lg px-2.5 text-control font-medium text-ink-body outline-none transition-colors data-highlighted:bg-surface-sunken data-highlighted:text-ink";

function DestinationGroup({ label, items, onNavigate }: {
  label: string;
  items: MenuDestination[];
  onNavigate: (path: string) => void;
}) {
  return <Menu.Group>
    <Menu.GroupLabel className="px-2.5 pt-2 pb-1 text-meta font-semibold tracking-wide text-ink-muted uppercase">{label}</Menu.GroupLabel>
    {items.map((item) => <Menu.Item key={item.path} onClick={() => onNavigate(item.path)} className={menuItemClass}>
      <item.icon size={16} className="shrink-0 text-ink-muted" aria-hidden />
      <span>{item.label}</span>
    </Menu.Item>)}
  </Menu.Group>;
}

/** The rail's account anchor and its high-frequency destinations.
 *
 *  The trigger answers "whose workspace is this?". The popup then separates
 *  places from account controls, so signing out cannot be mistaken for another
 *  navigation row. Base UI owns focus return, arrow-key movement, Escape, and
 *  outside-click dismissal. */
export function AccountMenu({ user, personalLendingAvailable, signingOut, onNavigate, onSignOut }: AccountMenuProps) {
  const quickLinks = personalLendingAvailable
    ? QUICK_LINKS
    : QUICK_LINKS.filter((item) => item.path !== appPaths.loans);
  return <Menu.Root>
    <Menu.Trigger
      disabled={!user}
      aria-label={user ? `${user.name} account menu` : "Account menu"}
      className="group flex h-10 w-full min-w-0 items-center gap-2 rounded-lg px-2 text-left outline-none transition-colors duration-[110ms] ease-linear hover:bg-surface-sunken focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring active:scale-[.995] disabled:pointer-events-none data-popup-open:bg-surface-sunken"
    >
      <span className="ledger-stamp shrink-0">{user ? user.name.slice(0, 1) : ""}</span>
      {user
        ? <span className="min-w-0 flex-1 truncate text-control font-medium text-ink-body">{user.name}</span>
        : <span className="h-2.5 w-24 animate-pulse rounded-full bg-line" />}
      <ChevronsUpDown size={15} aria-hidden className="shrink-0 text-ink-muted transition-colors group-data-popup-open:text-secondary" />
    </Menu.Trigger>

    {user ? <Menu.Portal>
      <Menu.Positioner side="top" align="start" sideOffset={8} collisionPadding={12} className="z-50 outline-none">
        <Menu.Popup className="w-64 max-w-[calc(100vw-1.5rem)] origin-[var(--transform-origin)] rounded-xl border border-line-strong bg-surface p-1.5 text-ink shadow-[var(--shadow-overlay)] outline-none transition-[transform,opacity] duration-[var(--m-state)] ease-out data-starting-style:scale-[.98] data-starting-style:opacity-0 data-ending-style:scale-[.98] data-ending-style:opacity-0 motion-reduce:transition-none">
          <div role="presentation" className="mb-1 flex items-center gap-3 rounded-lg bg-surface-sunken px-3 py-2.5">
            <span className="ledger-stamp shrink-0">{user.name.slice(0, 1)}</span>
            <span className="min-w-0">
              <span className="block truncate text-control font-semibold text-ink">{user.name}</span>
              <span className="mt-0.5 block truncate text-meta text-ink-muted">{user.currency} · {user.timezone}</span>
            </span>
          </div>

          <DestinationGroup label="Quick access" items={quickLinks} onNavigate={onNavigate} />
          <div role="separator" className="my-1 border-t border-line" />
          <DestinationGroup label="Your account" items={ACCOUNT_LINKS} onNavigate={onNavigate} />
          <div role="separator" className="my-1 border-t border-line" />
          <Menu.Item disabled={signingOut} onClick={onSignOut} className={`${menuItemClass} text-danger-ink data-highlighted:bg-danger-tint`}>
            {signingOut ? <Loader2 size={16} aria-hidden className="animate-spin" /> : <LogOut size={16} aria-hidden />}
            <span>{signingOut ? "Signing out…" : "Sign out"}</span>
          </Menu.Item>
        </Menu.Popup>
      </Menu.Positioner>
    </Menu.Portal> : null}
  </Menu.Root>;
}
