import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Scratchpad } from "@/components/scratchpad";

const values = new Map<string, string>();
vi.stubGlobal("localStorage", {
  get length() { return values.size; },
  clear: () => values.clear(),
  getItem: (key: string) => values.get(key) ?? null,
  key: (index: number) => [...values.keys()][index] ?? null,
  removeItem: (key: string) => values.delete(key),
  setItem: (key: string, value: string) => values.set(key, value),
});

function scratchpadKey() {
  return [...values.keys()].find((key) => key.startsWith("fyn.scratchpad.session.v2:")) ?? "";
}

function tabState() {
  for (let index = 0; index < sessionStorage.length; index += 1) {
    const key = sessionStorage.key(index);
    if (key?.startsWith("fyn.scratchpad.tab.v2:")) return JSON.parse(sessionStorage.getItem(key) ?? "{}");
  }
  return {};
}

function clearSessionCookie() {
  document.cookie = "fyn_scratchpad_session=; Max-Age=0; Path=/";
}

describe("Scratchpad", () => {
  beforeEach(() => {
    localStorage.clear();
    window.sessionStorage.clear();
    document.body.style.userSelect = "";
    clearSessionCookie();
  });

  it("starts as a notebook button and restores the note after a refresh-like remount", () => {
    const view = render(<Scratchpad storageScope="user-1" />);

    expect(screen.queryByRole("textbox", { name: "Scratchpad note" })).not.toBeInTheDocument();
    const launcher = screen.getByRole("button", { name: "Open scratchpad" });
    expect(launcher).toHaveClass("opacity-60", "hover:opacity-100", "focus-visible:opacity-100");
    fireEvent.click(launcher);
    expect(screen.getByRole("region", { name: "Scratchpad" })).toHaveClass("opacity-60", "hover:opacity-100", "focus-within:opacity-100");
    fireEvent.change(screen.getByRole("textbox", { name: "Scratchpad note" }), { target: { value: "Review the travel budget" } });

    expect(JSON.parse(localStorage.getItem(scratchpadKey()) ?? "{}")).toEqual({ note: "Review the travel budget" });
    expect(tabState()).toMatchObject({ open: true });

    view.unmount();
    render(<Scratchpad storageScope="user-1" />);
    expect(screen.getByRole("textbox", { name: "Scratchpad note" })).toHaveValue("Review the travel budget");
  });

  it("can be closed with Escape without discarding the note", () => {
    render(<Scratchpad storageScope="user-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Open scratchpad" }));
    const note = screen.getByRole("textbox", { name: "Scratchpad note" });
    fireEvent.change(note, { target: { value: "Try a 15% savings target" } });
    fireEvent.keyDown(note, { key: "Escape" });

    expect(screen.getByRole("button", { name: "Open scratchpad" })).toHaveFocus();
    expect(JSON.parse(localStorage.getItem(scratchpadKey()) ?? "{}")).toEqual({ note: "Try a 15% savings target" });
    expect(tabState()).toMatchObject({ open: false });
  });

  it("persists a drag position inside the current tab", () => {
    render(<Scratchpad storageScope="user-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Open scratchpad" }));

    const header = screen.getByRole("heading", { name: "Scratchpad" }).closest("header")!;
    fireEvent.pointerDown(header, { button: 0, pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 220, clientY: 180 });
    fireEvent.pointerUp(window, { pointerId: 1 });

    expect(tabState().position).toEqual({ x: 210, y: 170 });
  });

  it("receives note changes made in another tab", () => {
    render(<Scratchpad storageScope="user-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Open scratchpad" }));
    const key = scratchpadKey();
    const shared = JSON.stringify({ note: "Shared from the other tab" });

    localStorage.setItem(key, shared);
    fireEvent(window, new StorageEvent("storage", { key, newValue: shared }));

    expect(screen.getByRole("textbox", { name: "Scratchpad note" })).toHaveValue("Shared from the other tab");
  });

  it("does not let another tab overwrite this tab's drag position", () => {
    render(<Scratchpad storageScope="user-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Open scratchpad" }));
    const header = screen.getByRole("heading", { name: "Scratchpad" }).closest("header")!;
    fireEvent.pointerDown(header, { button: 0, pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 520, clientY: 280 });
    fireEvent.pointerUp(window, { pointerId: 1 });
    const card = screen.getByRole("region", { name: "Scratchpad" });
    expect(card).toHaveStyle({ left: "510px", top: "270px" });

    const key = scratchpadKey();
    const fromNarrowerTab = JSON.stringify({ note: "Still shared", position: { x: 40, y: 40 } });
    localStorage.setItem(key, fromNarrowerTab);
    fireEvent(window, new StorageEvent("storage", { key, newValue: fromNarrowerTab }));

    expect(card).toHaveStyle({ left: "510px", top: "270px" });
    expect(screen.getByRole("textbox", { name: "Scratchpad note" })).toHaveValue("Still shared");
  });

  it("resizes from its grip and persists dimensions for this tab", () => {
    render(<Scratchpad storageScope="user-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Open scratchpad" }));
    const card = screen.getByRole("region", { name: "Scratchpad" });
    vi.spyOn(card, "getBoundingClientRect").mockReturnValue({
      x: 100, y: 100, left: 100, top: 100, right: 420, bottom: 452,
      width: 320, height: 352, toJSON: () => ({}),
    });

    const grip = screen.getByRole("button", { name: "Resize scratchpad" });
    fireEvent.pointerDown(grip, { button: 0, pointerId: 7, clientX: 420, clientY: 452 });
    fireEvent.pointerMove(window, { pointerId: 7, clientX: 500, clientY: 512 });
    fireEvent.pointerUp(window, { pointerId: 7 });

    expect(tabState()).toMatchObject({
      position: { x: 100, y: 100 },
      size: { width: 400, height: 412 },
    });
    expect(document.body.style.userSelect).toBe("");
  });

  it("cleans up an unfinished pointer interaction when it unmounts", () => {
    const cancelFrame = vi.spyOn(window, "cancelAnimationFrame");
    const view = render(<Scratchpad storageScope="user-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Open scratchpad" }));
    const header = screen.getByRole("heading", { name: "Scratchpad" }).closest("header")!;
    fireEvent.pointerDown(header, { button: 0, pointerId: 9, clientX: 20, clientY: 20 });
    fireEvent.pointerMove(window, { pointerId: 9, clientX: 100, clientY: 100 });
    expect(document.body.style.userSelect).toBe("none");

    view.unmount();

    expect(document.body.style.userSelect).toBe("");
    expect(cancelFrame).toHaveBeenCalled();
    cancelFrame.mockRestore();
  });

  it("does not restore the previous browser session's note", () => {
    const view = render(<Scratchpad storageScope="user-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Open scratchpad" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Scratchpad note" }), { target: { value: "Only for this browser session" } });
    const oldKey = scratchpadKey();
    view.unmount();

    clearSessionCookie();
    render(<Scratchpad storageScope="user-1" />);

    expect(localStorage.getItem(oldKey)).toBeNull();
    expect(screen.getByRole("button", { name: "Open scratchpad" })).toBeInTheDocument();
  });
});
