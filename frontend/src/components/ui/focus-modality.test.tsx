import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FocusModality } from "@/components/ui/focus-modality";

describe("FocusModality", () => {
  it("enables app focus rings only for Tab navigation", () => {
    const view = render(<FocusModality />);

    expect(document.documentElement).not.toHaveAttribute("data-focus-modality");
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.documentElement).toHaveAttribute("data-focus-modality", "keyboard");

    fireEvent.pointerDown(document.body);
    expect(document.documentElement).toHaveAttribute("data-focus-modality", "pointer");

    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(document.documentElement).toHaveAttribute("data-focus-modality", "keyboard");

    view.unmount();
    expect(document.documentElement).not.toHaveAttribute("data-focus-modality");
  });

  it("does not treat ordinary typing as keyboard navigation", () => {
    render(<FocusModality />);
    fireEvent.keyDown(window, { key: "a" });
    expect(document.documentElement).not.toHaveAttribute("data-focus-modality");
  });
});
