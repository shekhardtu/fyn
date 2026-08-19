import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TileCard } from "@/components/dashboards";
import type { DashboardTile } from "@/lib/protocol";

const tile: DashboardTile = {
  id: "tile-1",
  title: "Monthly travel spend",
  position: 0,
  executedAt: "2026-08-19T03:00:00Z",
  chart: null,
  error: { code: "test", detail: "No chart needed for this interaction test." },
};

describe("dashboard tile HITL", () => {
  it("requires an explicit second decision before removal", () => {
    const onRequestRemove = vi.fn();
    const onKeep = vi.fn();
    const onRemove = vi.fn();
    const view = render(<TileCard
      tile={tile}
      confirming={false}
      removing={false}
      onRequestRemove={onRequestRemove}
      onKeep={onKeep}
      onRemove={onRemove}
    />);

    fireEvent.click(screen.getByRole("button", { name: "Remove tile: Monthly travel spend" }));
    expect(onRequestRemove).toHaveBeenCalledOnce();
    expect(onRemove).not.toHaveBeenCalled();

    view.rerender(<TileCard
      tile={tile}
      confirming
      removing={false}
      onRequestRemove={onRequestRemove}
      onKeep={onKeep}
      onRemove={onRemove}
    />);
    expect(screen.getByRole("group", { name: "Remove Monthly travel spend?" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Keep" }));
    expect(onKeep).toHaveBeenCalledOnce();
    expect(onRemove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(onRemove).toHaveBeenCalledOnce();
  });
});
