import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SiteHeader, useAutoHideSiteHeader } from "@/components/ui/site-header";

function Harness() {
  const { headerVisible, updateHeaderForScroll } = useAutoHideSiteHeader();
  return <div>
    <SiteHeader title="Transactions" subtitle="Ledger" hidden={!headerVisible} navOpen={false} onOpenNav={() => undefined} />
    <button onClick={() => updateHeaderForScroll(120)}>Content moves up</button>
    <button onClick={() => updateHeaderForScroll(116)}>Tiny reverse</button>
    <button onClick={() => updateHeaderForScroll(60)}>Content moves down</button>
  </div>;
}

describe("universal site header", () => {
  it("hides as content moves up and returns when the direction reverses", () => {
    render(<Harness />);
    const header = screen.getByRole("banner");
    fireEvent.click(screen.getByRole("button", { name: "Content moves up" }));
    expect(header).toHaveClass("-translate-y-full");
    fireEvent.click(screen.getByRole("button", { name: "Content moves down" }));
    expect(header).not.toHaveClass("-translate-y-full");
  });

  it("ignores a trackpad-sized direction reversal while hidden", () => {
    render(<Harness />);
    const header = screen.getByRole("banner");
    fireEvent.click(screen.getByRole("button", { name: "Content moves up" }));
    fireEvent.click(screen.getByRole("button", { name: "Tiny reverse" }));
    expect(header).toHaveClass("-translate-y-full");
  });

  it("keeps a fixed page heading inert: no rename handler, no editing", () => {
    render(<SiteHeader title="Transactions" subtitle="Ledger" navOpen={false} onOpenNav={() => undefined} />);
    fireEvent.doubleClick(screen.getByRole("heading", { name: "Transactions" }));
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("renames a thread title on double-click, committing only real changes", () => {
    const onRenameTitle = vi.fn();
    render(<SiteHeader title="Page 2" subtitle="Ledger" navOpen={false} onOpenNav={() => undefined} onRenameTitle={onRenameTitle} />);
    fireEvent.doubleClick(screen.getByRole("heading", { name: "Page 2" }));
    const input = screen.getByRole("textbox", { name: "Rename conversation: Page 2" }) as HTMLInputElement;
    // The old caption arrives selected, so typing replaces it outright.
    fireEvent.focus(input);
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe("Page 2".length);
    fireEvent.change(input, { target: { value: "  Groceries   plan " } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onRenameTitle).toHaveBeenCalledWith("Groceries plan");
    // Escape and unchanged text both leave the title alone.
    fireEvent.doubleClick(screen.getByRole("heading", { name: "Page 2" }));
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Escape" });
    fireEvent.doubleClick(screen.getByRole("heading", { name: "Page 2" }));
    fireEvent.blur(screen.getByRole("textbox"));
    expect(onRenameTitle).toHaveBeenCalledTimes(1);
  });
});
