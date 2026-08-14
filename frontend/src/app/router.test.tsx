import { act, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter } from "react-router";
import { RouterProvider } from "react-router/dom";
import { describe, expect, it, vi } from "vitest";
import { appRoutes } from "@/app/router";
import { appPaths } from "@/routing/paths";

const shellLifecycle = vi.hoisted(() => ({ mounts: 0 }));

vi.mock("@/components/workspace", async () => {
  const React = await import("react");
  return {
    FynWorkspace: () => null,
    WorkspaceShell: ({ children }: { children: React.ReactNode }) => {
      React.useEffect(() => {
        shellLifecycle.mounts += 1;
      }, []);
      return <div data-testid="workspace-shell">{children}</div>;
    },
  };
});

describe("application routing", () => {
  it("keeps the conversation shell mounted when only the conversation ID changes", async () => {
    shellLifecycle.mounts = 0;
    const router = createMemoryRouter(appRoutes, { initialEntries: [appPaths.conversation("first")] });
    render(<RouterProvider router={router} />);

    const shell = await screen.findByTestId("workspace-shell");
    await act(() => router.navigate(appPaths.conversation("second")));
    await waitFor(() => expect(router.state.location.pathname).toBe("/c/second"));

    expect(screen.getByTestId("workspace-shell")).toBe(shell);
    expect(shellLifecycle.mounts).toBe(1);
  });

  it("encodes conversation IDs in generated URLs", () => {
    expect(appPaths.conversation("thread / 2")).toBe("/c/thread%20%2F%202");
  });
});
