import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, ChartColumn, RotateCcw, Trash2, TriangleAlert } from "lucide-react";
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { SiteHeader, useAutoHideSiteHeader } from "@/components/ui/site-header";
import { ChartView } from "@/components/widget-library/chart";
import { useWorkspaceShell } from "@/components/workspace";
import { useUserDefaults } from "@/components/user-defaults";
import { deleteDashboardTile, listDashboards, loadDashboard } from "@/lib/api";
import { formatCount, formatInstant } from "@/lib/format";
import type { DashboardTile } from "@/lib/protocol";
import { appPaths } from "@/routing/paths";

function DashboardsSkeleton() {
  return <div role="status" aria-label="Loading your dashboards" className="grid gap-5 xl:grid-cols-2">
    {[0, 1, 2, 3].map((item) => <div key={item} className="h-80 animate-pulse rounded-xl border border-line bg-surface" />)}
  </div>;
}

/** The invitation both empty states share: a dashboard is filled from a
 *  conversation, never from this page, so the way forward is always the same. */
function SaveChartInvitation({ title, detail, canOpen, onOpen }: { title: string; detail: string; canOpen: boolean; onOpen: () => void }) {
  return <div className="rounded-xl border border-line bg-surface px-6 py-12 text-center">
    <span className="mx-auto grid size-11 place-items-center rounded-xl bg-secondary-tint text-secondary"><ChartColumn /></span>
    <h2 className="mt-4 font-heading text-title font-semibold text-ink">{title}</h2>
    <p className="mx-auto mt-2 max-w-md text-control leading-6 text-ink-muted">{detail}</p>
    <Button type="button" className="mt-5" disabled={!canOpen} onClick={onOpen}>Open a conversation <ArrowRight /></Button>
  </div>;
}

export function TileCard({ tile, confirming, removing, onRequestRemove, onKeep, onRemove }: {
  tile: DashboardTile;
  confirming: boolean;
  removing: boolean;
  onRequestRemove: () => void;
  onKeep: () => void;
  onRemove: () => void;
}) {
  const { timeZone } = useUserDefaults();
  const stamp = formatInstant(tile.executedAt, timeZone) || tile.executedAt;
  return <section aria-label={`${tile.title} tile`} className="min-w-0 overflow-hidden rounded-xl border border-line bg-surface">
    <div className="flex items-start gap-3 border-b border-line px-5 py-4">
      <div className="min-w-0 flex-1">
        <h3 className="truncate font-heading text-body font-semibold text-ink">{tile.title}</h3>
        <p className="mt-1 font-mono text-meta text-ink-muted">as of {stamp}</p>
      </div>
      {confirming ? <div role="group" aria-label={`Remove ${tile.title}?`} className="flex shrink-0 items-center gap-1">
        <Button type="button" variant="ghost" size="sm" disabled={removing} onClick={onKeep}>Keep</Button>
        <Button type="button" variant="danger" size="sm" disabled={removing} onClick={onRemove}>{removing ? "Removing…" : "Remove"}</Button>
      </div> : <Button type="button" variant="ghost" size="icon-sm" disabled={removing} aria-label={`Remove tile: ${tile.title}`} title="Remove tile" onClick={onRequestRemove} className="shrink-0 text-ink-muted hover:bg-danger-tint hover:text-danger"><Trash2 /></Button>}
    </div>
    {tile.chart
      ? <div className="px-3 pt-3 pb-2"><ChartView data={tile.chart} embedded /></div>
      : <div className="flex items-start gap-2.5 px-5 py-6">
        <TriangleAlert size={15} className="mt-0.5 shrink-0 text-ink-muted" />
        <div className="min-w-0 text-note leading-5 text-ink-muted">
          <p>This tile couldn’t be refreshed with live data. Its analysis stays saved — it will try again next time the page opens.</p>
          {tile.error ? <p className="mt-1 truncate font-mono text-meta">{tile.error.code} · {tile.error.detail}</p> : null}
        </div>
      </div>}
  </section>;
}

export function DashboardsPage() {
  const navigate = useNavigate();
  const shell = useWorkspaceShell();
  const queryClient = useQueryClient();
  const { headerVisible, updateHeaderForScroll } = useAutoHideSiteHeader();
  const [params, setParams] = useSearchParams();
  const [confirmingTileId, setConfirmingTileId] = useState<string | null>(null);

  const dashboards = useQuery({ queryKey: ["dashboards"], queryFn: listDashboards });
  const requestedId = params.get("d");
  const selectedId = dashboards.data?.some((entry) => entry.id === requestedId)
    ? requestedId as string
    : dashboards.data?.[0]?.id ?? null;
  const detail = useQuery({
    queryKey: ["dashboard", selectedId],
    queryFn: () => loadDashboard(selectedId ?? ""),
    enabled: selectedId !== null,
  });
  const removeTile = useMutation({
    mutationFn: (input: { dashboardId: string; tileId: string }) => deleteDashboardTile(input.dashboardId, input.tileId),
    onSuccess: () => setConfirmingTileId(null),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const latestConversation = shell.conversations[0];
  const openConversation = () => latestConversation && navigate(appPaths.conversation(latestConversation.id));
  function selectDashboard(next: string) {
    setParams((previous) => {
      const merged = new URLSearchParams(previous);
      if (next === dashboards.data?.[0]?.id) merged.delete("d"); else merged.set("d", next);
      return merged;
    });
  }

  const selected = dashboards.data?.find((entry) => entry.id === selectedId) ?? null;
  const tiles = detail.data ? [...detail.data.tiles].sort((left, right) => left.position - right.position) : [];

  return <main id="main-content" onScroll={(event) => updateHeaderForScroll(event.currentTarget.scrollTop)} className="min-h-0 min-w-0 overflow-y-auto bg-ground">
    <SiteHeader title="Dashboards" subtitle="Saved charts, re-run live" subtitleClassName="hidden sm:block" hidden={!headerVisible} navOpen={shell.navOpen} onOpenNav={shell.openNav} end={dashboards.data && dashboards.data.length > 1 ? <Combobox
      aria-label="Dashboard"
      value={selectedId ?? ""}
      onValueChange={selectDashboard}
      options={dashboards.data.map((entry) => ({ value: entry.id, label: entry.name }))}
      searchPlaceholder="Search dashboards"
      triggerClassName="h-9 w-auto font-medium text-ink-body"
    /> : undefined} />

    <div className="mx-auto w-full max-w-[86rem] px-4 py-7 sm:px-6 sm:py-9 lg:px-8">
      <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="ledger-meta">Saved analyses{selected ? ` · ${selected.name}` : ""}</p>
          <h2 className="mt-2 font-heading text-[clamp(1.7rem,4vw,2.35rem)] leading-tight font-semibold tracking-[-0.045em] text-ink">Charts that stay current.</h2>
          <p className="mt-2 max-w-xl text-body leading-6 text-ink-muted">Every tile re-runs its saved analysis against your live records each time this page opens.</p>
        </div>
        {selected ? <p className="text-note text-ink-muted">{formatCount(selected.tileCount)} tile{selected.tileCount === 1 ? "" : "s"}</p> : null}
      </div>

      {dashboards.isPending ? <DashboardsSkeleton /> : dashboards.isError ? <div role="alert" className="rounded-xl border border-danger-line bg-surface px-6 py-10 text-center">
        <h2 className="font-heading text-title font-semibold text-ink">We couldn’t open your dashboards</h2>
        <p className="mt-2 text-control text-ink-muted">Your saved analyses are safe. Try loading them again.</p>
        <Button type="button" variant="outline" className="mt-5" onClick={() => dashboards.refetch()}><RotateCcw /> Try again</Button>
      </div> : !dashboards.data?.length ? <SaveChartInvitation
        title="No dashboards yet"
        detail="Ask fyn for an analysis in a conversation, then save its chart to a dashboard. It lands here and refreshes on your live records."
        canOpen={Boolean(latestConversation)}
        onOpen={openConversation}
      /> : detail.isPending ? <DashboardsSkeleton /> : detail.isError ? <div role="alert" className="rounded-xl border border-danger-line bg-surface px-6 py-10 text-center">
        <h2 className="font-heading text-title font-semibold text-ink">This dashboard couldn’t be loaded</h2>
        <p className="mt-2 text-control text-ink-muted">Its tiles are safe. Try loading it again.</p>
        <Button type="button" variant="outline" className="mt-5" onClick={() => detail.refetch()}><RotateCcw /> Try again</Button>
      </div> : !tiles.length ? <SaveChartInvitation
        title="This dashboard is waiting for its first chart"
        detail="Save a chart from any conversation and it takes its place here, re-run live every time you open the page."
        canOpen={Boolean(latestConversation)}
        onOpen={openConversation}
      /> : <div className="grid items-start gap-5 xl:grid-cols-2">
        {tiles.map((tile) => <TileCard
          key={tile.id}
          tile={tile}
          confirming={confirmingTileId === tile.id}
          removing={removeTile.isPending && removeTile.variables?.tileId === tile.id}
          onRequestRemove={() => setConfirmingTileId(tile.id)}
          onKeep={() => setConfirmingTileId(null)}
          onRemove={() => selectedId && removeTile.mutate({ dashboardId: selectedId, tileId: tile.id })}
        />)}
      </div>}
    </div>
  </main>;
}
