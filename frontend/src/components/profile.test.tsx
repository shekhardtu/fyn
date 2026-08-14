import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createMemoryRouter } from "react-router";
import { RouterProvider } from "react-router/dom";
import { ProfilePanel } from "@/components/profile";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ProfilePanel authentication guard", () => {
  it("redirects after an unauthorized render without updating Router during render", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: "Sign in to continue." }),
      { status: 401, headers: { "Content-Type": "application/json" } },
    ));
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const router = createMemoryRouter([
      { path: "/profile", element: <ProfilePanel /> },
      { path: "/login", element: <p>Sign in</p> },
    ], { initialEntries: ["/profile"] });

    render(<QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider>);

    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(consoleError.mock.calls.flat().join(" ")).not.toContain("Cannot update a component");
  });
});
