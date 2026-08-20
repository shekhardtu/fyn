import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, Download, Loader2, MapPin, Trash2 } from "lucide-react";
import { useState } from "react";
import { NotLiveStamp, PanelHeading, SettingSwitch, SettingsGroup, settingsProblem, settingsSaved } from "@/components/settings-parts";
import { InstallAppSetting } from "@/components/install-app";
import { primeLocationPermission } from "@/lib/device-location";
import { Button } from "@/components/ui/button";
import { deleteAllData, downloadDataExport, getPrivacyStatus, revokeSource, setLocationEnabled } from "@/lib/api";
import { useThemePreference, type ThemePreference } from "@/lib/theme";
import { cn } from "@/lib/utils";

const THEME_OPTIONS: Array<{ value: ThemePreference; label: string }> = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

/** Nothing here reaches a server yet, so the switches are inert rather than
 *  optimistic. The list is what the product could notify you about today. */
const NOTIFICATION_TOPICS = [
  { id: "run_failed", label: "An answer couldn’t be verified", description: "When a reply is delivered without passing its checks." },
  { id: "large_entry", label: "An unusually large entry", description: "When something is filed well outside your normal range." },
  { id: "weekly", label: "Weekly summary", description: "Where your money went, once a week." },
];

function AppearanceControl() {
  const [preference, setPreference] = useThemePreference();
  return <div role="radiogroup" aria-label="Color theme" className="inline-flex rounded-lg border border-line bg-surface-sunken p-0.5">
    {THEME_OPTIONS.map((option) => <button
      key={option.value}
      type="button"
      role="radio"
      aria-checked={preference === option.value}
      onClick={() => setPreference(option.value)}
      className={cn(
        "h-8 rounded-md px-3.5 text-control font-medium transition-colors",
        preference === option.value ? "bg-surface text-ink shadow-[0_1px_2px_rgb(0_0_0_/_0.08)]" : "text-ink-muted hover:text-ink-body",
      )}
    >{option.label}</button>)}
  </div>;
}

export function AppSettingsPanel({ onDeleted }: { onDeleted: () => void }) {
  const queryClient = useQueryClient();
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [confirmingRevoke, setConfirmingRevoke] = useState<string | null>(null);
  const [busyControl, setBusyControl] = useState<string | null>(null);
  const privacy = useQuery({ queryKey: ["privacy"], queryFn: getPrivacyStatus });

  const run = useMutation({
    mutationFn: async ({ kind, value }: { kind: "location" | "revoke" | "export" | "delete"; value?: string | boolean }) => {
      if (kind === "location") {
        await setLocationEnabled(Boolean(value));
        // Ask the browser here, while the person is looking at the switch they
        // just moved. The alternative is a prompt that ambushes them mid-entry,
        // or none at all — and a switch reading "on" over a browser that says no.
        return value ? await primeLocationPermission() : "off";
      }
      if (kind === "revoke") return revokeSource(String(value));
      if (kind === "export") return downloadDataExport();
      return deleteAllData();
    },
    onMutate: ({ kind, value }) => setBusyControl(kind === "revoke" ? `revoke:${value}` : kind),
    onSuccess: async (result, variables) => {
      setBusyControl(null);
      if (variables.kind === "delete") { onDeleted(); return; }
      if (variables.kind === "export") settingsSaved(`Saved ${typeof result === "string" ? result : "your export"} to your downloads.`);
      if (variables.kind === "location") {
        if (!variables.value) settingsSaved("Off — new transactions won’t record a location.");
        else if (result === "denied") settingsProblem("Saved, but your browser is blocking location for this site. Allow it in your browser’s site settings, then try again.");
        else if (result === "unsupported") settingsProblem("Saved, but this browser can’t report a location here. That needs a secure (https) connection.");
        else if (result === "prompt") settingsProblem("Saved, but your device didn’t return a position just now. New entries will try again.");
        else settingsSaved("On — new transactions will record where you add them.");
      }
      if (variables.kind === "revoke") { setConfirmingRevoke(null); settingsSaved(`${String(variables.value).toUpperCase()} can no longer add transactions.`); }
      await queryClient.invalidateQueries({ queryKey: ["privacy"] });
    },
    onError: (cause: Error) => { setBusyControl(null); settingsProblem(cause.message); },
  });

  const locationEnabled = privacy.data?.locationEnabled ?? false;
  const sources = Object.entries(privacy.data?.sources ?? {});

  return <div>
    <PanelHeading title="Settings" blurb="How the app looks, what it may collect, and what happens to the data it keeps." />

    <SettingsGroup title="Appearance" description="System follows your device; Light and Dark stay put.">
      <AppearanceControl />
    </SettingsGroup>

    <SettingsGroup title="Install" description="fyn runs as an app on phones, tablets and desktops — same account, same data, no store.">
      <InstallAppSetting />
    </SettingsGroup>

    <SettingsGroup title="Notifications" stamp={<NotLiveStamp />} description="What fyn would tell you about outside a conversation. Nothing is sent yet and these switches aren’t stored.">
      <div className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-surface">
        {NOTIFICATION_TOPICS.map((topic) => <SettingSwitch
          key={topic.id}
          variant="row"
          label={topic.label}
          description={topic.description}
          checked={false}
          disabled
          icon={<Bell size={17} />}
          onChange={() => undefined}
        />)}
      </div>
    </SettingsGroup>

    <SettingsGroup title="Privacy" description="Nothing is collected until you switch it on.">
      {privacy.isError ? <p role="alert" className="rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note leading-5 text-danger-ink">
        Your privacy settings couldn’t be loaded, so they’re hidden rather than shown wrong. <button type="button" onClick={() => privacy.refetch()} className="font-semibold underline">Load them again</button>
      </p> : null}
      {privacy.isLoading ? <div role="status" aria-label="Loading privacy settings" className="space-y-3">{[0, 1, 2].map((row) => <div key={row} className="h-16 animate-pulse rounded-lg bg-line/70" />)}</div> : null}
      {privacy.data ? <SettingSwitch
        label="Record location"
        description="Saves where you are when you add a transaction, as the coordinates your device reports. New entries only — editing an older one never changes where it says it happened. Your browser will ask permission the first time."
        checked={locationEnabled}
        disabled={run.isPending}
        busy={busyControl === "location"}
        icon={<MapPin size={17} />}
        onChange={(next) => run.mutate({ kind: "location", value: next })}
      /> : null}
    </SettingsGroup>

    {privacy.data ? <>
      <SettingsGroup title="Where transactions can come from" description="Revoking a source keeps everything it already recorded; it just can’t add more.">
        <div className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-surface">
          {sources.map(([source, active]) => <div key={source} className="px-4 py-3">
            <div className="flex items-center gap-3">
              <div className="min-w-0 flex-1">
                <p className="text-control font-medium uppercase text-ink-body">{source}</p>
                <p className="mt-0.5 text-note leading-5 text-ink-muted">{active ? "Allowed to add transactions" : "Revoked — it can no longer add transactions"}</p>
              </div>
              {active
                ? <Button type="button" variant="outline" size="sm" disabled={run.isPending} onClick={() => setConfirmingRevoke(source)}>Revoke</Button>
                : <span className="shrink-0 text-note font-semibold text-danger-ink">Revoked</span>}
            </div>
            {confirmingRevoke === source ? <div className="mt-3 rounded-lg bg-surface-sunken p-3">
              <p className="text-note leading-5 text-ink-body">Revoke {source.toUpperCase()}? Transactions already recorded stay; this source just can’t add more.</p>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button type="button" size="lg" variant="danger" disabled={run.isPending} onClick={() => run.mutate({ kind: "revoke", value: source })}>{busyControl === `revoke:${source}` ? <Loader2 size={14} className="animate-spin" /> : null}Revoke {source.toUpperCase()}</Button>
                <Button type="button" variant="ghost" size="lg" onClick={() => setConfirmingRevoke(null)}>Keep it on</Button>
              </div>
            </div> : null}
          </div>)}
          {!sources.length ? <p className="px-4 py-4 text-note text-ink-muted">No sources are connected yet.</p> : null}
        </div>
      </SettingsGroup>

      <SettingsGroup title="Your data" description="Everything fyn holds for this account, in one file or out of existence.">
        <Button type="button" variant="outline" size="lg" disabled={run.isPending} onClick={() => run.mutate({ kind: "export" })} className="w-full sm:w-auto">
          {busyControl === "export" ? <Loader2 className="animate-spin" /> : <Download />}
          {busyControl === "export" ? "Preparing your export…" : "Export my data"}
        </Button>

        <div className="mt-4 rounded-lg border border-danger-line bg-danger-tint p-4">
          <div className="flex gap-3">
            <Trash2 className="mt-0.5 shrink-0 text-danger" />
            <div>
              <p className="text-control font-semibold text-danger-ink">Delete all data</p>
              <p className="mt-1 text-note leading-5 text-danger-ink/85">Permanently removes conversations, transactions, observations, goals, budgets, and preferences. This cannot be undone.</p>
            </div>
          </div>
          <input
            value={deleteConfirmation}
            onChange={(event) => setDeleteConfirmation(event.target.value)}
            placeholder="Type DELETE MY DATA"
            aria-label="Deletion confirmation"
            className="manual-field manual-field-danger mt-4 h-[var(--h-field)] w-full rounded-lg border border-danger-line bg-surface px-3 text-body text-ink outline-none transition-colors duration-[110ms] ease-linear"
          />
          <Button type="button" variant="danger" size="lg" disabled={deleteConfirmation !== "DELETE MY DATA" || run.isPending} onClick={() => run.mutate({ kind: "delete" })} className="mt-2 w-full">
            {busyControl === "delete" ? <Loader2 className="animate-spin" /> : null}
            {busyControl === "delete" ? "Deleting everything…" : "Delete permanently"}
          </Button>
        </div>
      </SettingsGroup>
    </> : null}
  </div>;
}
