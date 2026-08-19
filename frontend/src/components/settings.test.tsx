import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentSettingsPanel } from "@/components/settings-agent";
import { SettingsRailIndex } from "@/components/settings-parts";

afterEach(() => {
  vi.restoreAllMocks();
});

function renderPanel(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{node}</QueryClientProvider>);
}

describe("agent settings panel", () => {
  it("names the boundaries that have no control, and gives them no control", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ answerValidationMode: "full", answerStyle: "explained" }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));

    renderPanel(<AgentSettingsPanel />);

    // The signature of this page: the four guarantees are stated at the same
    // weight as the switches, and none of them is a switch.
    const fixed = screen.getByRole("heading", { name: "Fixed, whatever you choose" }).closest("section");
    expect(fixed).not.toBeNull();
    for (const guarantee of ["Your account only", "Reads, never writes", "Published tables only", "One statement, no side effects"]) {
      expect(within(fixed!).getByText(guarantee)).toBeInTheDocument();
    }
    expect(within(fixed!).queryAllByRole("switch")).toHaveLength(0);
    expect(within(fixed!).queryAllByRole("radio")).toHaveLength(0);
    expect(within(fixed!).queryAllByRole("button")).toHaveLength(0);

    // The one live control on the panel reflects the saved mode.
    await waitFor(() => expect(screen.getByRole("radio", { name: /Full/ })).toHaveAttribute("aria-checked", "true"));
    expect(screen.getByRole("radio", { name: /Explained/ })).toHaveAttribute("aria-checked", "true");
  });

  it("never leaves a placeholder control pressable", () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ answerValidationMode: "full", answerStyle: "explained" }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));

    renderPanel(<AgentSettingsPanel />);

    // The honesty contract for everything shipped ahead of its backend: a
    // group that cannot save says so, and every control inside it is inert.
    // A stamp over live controls, or a live-looking control under a stamp,
    // both read as "this was saved" when nothing was.
    for (const stamp of screen.getAllByText("Not live yet")) {
      const group = stamp.closest("section");
      expect(group).not.toBeNull();
      const controls = [...within(group!).queryAllByRole("switch"), ...within(group!).queryAllByRole("radio")];
      expect(controls.length).toBeGreaterThan(0);
      for (const control of controls) expect(control).toBeDisabled();
    }
  });

  it("persists an answer-style change without changing answer checking", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ answerValidationMode: "off", answerStyle: "explained" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ))
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ answerValidationMode: "off", answerStyle: "concise" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ));

    renderPanel(<AgentSettingsPanel />);
    await waitFor(() => expect(screen.getByRole("radio", { name: /Explained/ })).toHaveAttribute("aria-checked", "true"));

    screen.getByRole("radio", { name: /Concise/ }).click();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      method: "PATCH",
      body: JSON.stringify({ answerStyle: "concise" }),
    });
    await waitFor(() => expect(screen.getByRole("radio", { name: /Concise/ })).toHaveAttribute("aria-checked", "true"));
    expect(screen.getByRole("radio", { name: /Off/ })).toHaveAttribute("aria-checked", "true");
  });
});

describe("settings section index in the rail", () => {
  it("marks the open section and offers the way back out", () => {
    const onLeave = vi.fn();
    const onNavigate = vi.fn();
    const router = createMemoryRouter([{
      path: "/settings/*",
      element: <SettingsRailIndex onLeave={onLeave} onNavigate={onNavigate} />,
    }], { initialEntries: ["/settings/agent"] });

    render(<RouterProvider router={router} />);

    const rail = screen.getByRole("navigation", { name: "Settings sections" });
    expect(within(rail).getByRole("link", { name: "Agent settings" })).toHaveAttribute("aria-current", "page");
    // Profile owns the index path, so every nested section would light it up
    // too without the exact match.
    expect(within(rail).getByRole("link", { name: "Profile" })).not.toHaveAttribute("aria-current");
    expect(within(rail).getByRole("link", { name: "Settings" })).not.toHaveAttribute("aria-current");

    // On a phone the rail sits over the sheet, so choosing a section has to
    // put it away again.
    within(rail).getByRole("link", { name: "Profile" }).click();
    expect(onNavigate).toHaveBeenCalled();

    screen.getByRole("button", { name: "Back to your workspace" }).click();
    expect(onLeave).toHaveBeenCalled();
  });
});
