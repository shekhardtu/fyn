import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { usePlainKey } from "@/lib/shortcuts";

function Harness({ onKey }: { onKey: () => void }) {
  usePlainKey("/", onKey);
  return <div>
    <input aria-label="Field" />
    <p>content</p>
  </div>;
}

describe("usePlainKey", () => {
  it("fires on the bare key outside text entry", () => {
    const onKey = vi.fn();
    render(<Harness onKey={onKey} />);
    fireEvent.keyDown(document.body, { key: "/" });
    expect(onKey).toHaveBeenCalledTimes(1);
  });

  it("stays a literal character inside a field and under modifiers", () => {
    const onKey = vi.fn();
    render(<Harness onKey={onKey} />);
    fireEvent.keyDown(screen.getByLabelText("Field"), { key: "/" });
    fireEvent.keyDown(document.body, { key: "/", ctrlKey: true });
    expect(onKey).not.toHaveBeenCalled();
  });

  it("yields to an open dialog", () => {
    const onKey = vi.fn();
    render(<>
      <Harness onKey={onKey} />
      <div role="dialog" aria-label="Drawer" />
    </>);
    fireEvent.keyDown(document.body, { key: "/" });
    expect(onKey).not.toHaveBeenCalled();
  });
});
