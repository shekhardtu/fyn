import { ArrowLeft, Bot, SlidersHorizontal, UserRound } from "lucide-react";
import type { ReactNode } from "react";
import { NavLink } from "react-router";
import { toast } from "@/components/ui/toast";
import { appPaths } from "@/routing/paths";
import { cn } from "@/lib/utils";

/** Settings reports every change through the workspace toast stack rather
 *  than a banner inside the sheet.
 *
 *  A saved setting is not news you need to keep reading — it is news you need
 *  once, near where your eye already is. A banner pinned to the top of a
 *  panel you have scrolled past reports to nobody, and three panels each
 *  growing their own banner is the same message written three times.
 *
 *  `WorkspaceShell` renders these; the type tells its toast list to draw a
 *  status slip rather than the undo slip a struck entry gets. */
export const SETTINGS_TOAST_SAVED = "settings-saved";
export const SETTINGS_TOAST_PROBLEM = "settings-problem";

/** Says what changed, in the past tense, naming the thing you just operated:
 *  "Answer style saved", never "Preferences updated". */
export function settingsSaved(what: string) {
  toast.add({ type: SETTINGS_TOAST_SAVED, title: what, timeout: 4000 });
}

/** The failure carries the server's own sentence: it is the only thing that
 *  knows why, and a generic "something went wrong" would be a downgrade. */
export function settingsProblem(why: string) {
  toast.add({ type: SETTINGS_TOAST_PROBLEM, title: why, timeout: 7000 });
}


/** Settings is the ledger's front matter: the standing instructions that
 *  govern every entry and every answer that follows. Three fixed destinations,
 *  indexed the way the rail already indexes fixed destinations. */
export const SETTINGS_SECTIONS: Array<{ to: string; label: string; icon: typeof UserRound; end?: boolean }> = [
  // `end` only on the index: without it every nested section would also mark
  // Profile as the open one.
  { to: appPaths.settings, label: "Profile", icon: UserRound, end: true },
  { to: appPaths.settingsAgent, label: "Agent settings", icon: Bot },
  { to: appPaths.settingsApp, label: "Settings", icon: SlidersHorizontal },
];

/** The rail's contents while settings is open.
 *
 *  It replaces the conversation index rather than standing beside it: a second
 *  column of menu next to the one already there is two navigations competing
 *  for one job. Nothing about the rail changes on the way in except what it is
 *  an index of — same rows, same mark on the open one — and the way back out
 *  is the first row you meet.
 *
 *  The three destinations wear the compact icon rows the money pages use,
 *  because that is what they are: fixed places, not a list that grows. */
/** `onNavigate` closes the drawer on a phone, where the rail sits over the
 *  sheet it is an index of — the money rows have always done this, and a
 *  section that navigated behind a drawer still covering it looked broken. */
export function SettingsRailIndex({ onLeave, onNavigate }: { onLeave: () => void; onNavigate: () => void }) {
  return <div className="rail-body flex min-h-0 flex-col">
    <div className="settings-leave-band">
      <button type="button" onClick={onLeave} className="ledger-entry money-entry">
        <ArrowLeft size={16} className="shrink-0" />
        <span>Back to your workspace</span>
      </button>
    </div>

    <nav aria-label="Settings sections" className="panel-scroll min-h-0 flex-1 overflow-y-auto py-2">
      <p className="ledger-meta px-3 pt-1 pb-2">Settings</p>
      {SETTINGS_SECTIONS.map((section) => <div key={section.to} className="ledger-row">
        <NavLink to={section.to} end={section.end} onClick={onNavigate} className="ledger-entry money-entry">
          <span aria-hidden className="ledger-mark" />
          <section.icon size={16} className="shrink-0" />
          <span>{section.label}</span>
        </NavLink>
      </div>)}
    </nav>
  </div>;
}

/** One section of a settings panel. The heading carries a rule out to the
 *  right margin — the same device the conversation index uses for its own
 *  dividers, so a settings sheet reads as another page of the same book. */
export function SettingsGroup({ title, description, stamp, children, className }: {
  title: string;
  description?: ReactNode;
  /** Marks a group whose controls do not save yet. Never omit it on one. */
  stamp?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return <section className={cn("mt-8 first:mt-0", className)}>
    <div className="settings-head">
      <h3 className="settings-head-label">{title}</h3>
      {stamp}
    </div>
    {description ? <p className="mt-2 max-w-prose text-note leading-5 text-ink-muted">{description}</p> : null}
    <div className="mt-3">{children}</div>
  </section>;
}

/** Says the one thing a disabled control cannot say for itself: pressing this
 *  would change nothing, and nothing here has been saved to your account. */
export function NotLiveStamp() {
  return <span className="settings-stamp">Not live yet</span>;
}

/** The heading every panel opens with, so all three sections start on the
 *  same line whichever one you land on. */
export function PanelHeading({ title, blurb }: { title: string; blurb: string }) {
  return <header className="mb-7">
    <h2 className="font-heading text-title font-semibold tracking-[-0.015em] text-ink">{title}</h2>
    <p className="mt-1 max-w-prose text-control leading-6 text-ink-muted">{blurb}</p>
  </header>;
}

/** A choice list: one row per option, the chosen one filled. Used live for
 *  answer checking and inert for the controls that are still placeholders,
 *  so a placeholder never invents a shape the real control will not have. */
export function ChoiceRows<T extends string>({ label, value, options, disabled = false, onChange }: {
  label: string;
  value: T;
  options: Array<{ value: T; label: string; description: string; note?: string }>;
  disabled?: boolean;
  onChange?: (value: T) => void;
}) {
  return <div role="radiogroup" aria-label={label} className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-surface">
    {options.map((option) => {
      const selected = value === option.value;
      return <button
        key={option.value}
        type="button"
        role="radio"
        aria-checked={selected}
        disabled={disabled}
        onClick={() => { if (!selected) onChange?.(option.value); }}
        className={cn(
          "flex w-full items-start gap-3 px-4 py-3 text-left transition-colors disabled:cursor-default disabled:opacity-60",
          selected ? "bg-secondary-tint" : "not-disabled:hover:bg-surface-sunken",
        )}
      >
        <span aria-hidden className={cn("mt-1 grid size-4 shrink-0 place-items-center rounded-full border", selected ? "border-secondary bg-secondary" : "border-line-strong")}>
          <span className={cn("size-1.5 rounded-full bg-surface", !selected && "hidden")} />
        </span>
        <span className="min-w-0">
          <span className="flex flex-wrap items-center gap-2 text-control font-semibold text-ink-body">
            {option.label}
            {option.note ? <span className="rounded-full bg-surface px-2 py-0.5 text-meta font-semibold text-secondary">{option.note}</span> : null}
          </span>
          <span className="mt-0.5 block text-note leading-5 text-ink-muted">{option.description}</span>
        </span>
      </button>;
    })}
  </div>;
}

/** The system's switch. One shape for every on/off in settings.
 *
 *  `row` drops the box so several can stack inside one bordered list without
 *  drawing a second border a pixel inside the first. */
export function SettingSwitch({ label, description, checked, disabled = false, busy = false, variant = "card", icon, onChange }: {
  label: string;
  description: string;
  checked: boolean;
  disabled?: boolean;
  busy?: boolean;
  variant?: "card" | "row";
  icon?: ReactNode;
  onChange: (next: boolean) => void;
}) {
  return <div className={cn("flex items-center gap-3 bg-surface p-4", variant === "card" && "rounded-lg border border-line")}>
    {icon ? <span className="shrink-0 text-secondary">{icon}</span> : null}
    <div className="min-w-0">
      <p className="text-control font-semibold text-ink-body">{label}</p>
      <p className="mt-0.5 text-note leading-5 text-ink-muted">{description}</p>
    </div>
    <button
      type="button"
      role="switch"
      aria-label={label}
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn("ml-auto grid h-11 w-14 shrink-0 place-items-center rounded-full disabled:cursor-default disabled:opacity-45", busy && "opacity-70")}
    >
      <span className={cn("flex h-6 w-11 items-center rounded-full p-0.5 transition-colors", checked ? "bg-secondary" : "bg-line-strong")}>
        <span className={cn("block size-5 rounded-full bg-surface ring-1 ring-black/5 transition-transform duration-[110ms] ease-linear", checked && "translate-x-5")} />
      </span>
    </button>
  </div>;
}
