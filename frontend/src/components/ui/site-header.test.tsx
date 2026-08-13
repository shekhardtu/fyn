import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SiteHeader, useAutoHideSiteHeader } from "@/components/ui/site-header";

function Harness() {
  const { headerVisible, updateHeaderForScroll } = useAutoHideSiteHeader();
  return <div>
    <SiteHeader title="Transactions" subtitle="Ledger" hidden={!headerVisible} navOpen={false} onOpenNav={() => undefined} />
    <button onClick={() => updateHeaderForScroll(120)}>Content moves up</button>
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
});
