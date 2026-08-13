import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProfilePanel } from "@/components/profile";

const { push, replace } = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace }),
}));

afterEach(() => {
  vi.restoreAllMocks();
  push.mockReset();
  replace.mockReset();
});

describe("ProfilePanel authentication guard", () => {
  it("redirects after an unauthorized render without updating Router during render", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ detail: "Sign in to continue." }),
      { status: 401, headers: { "Content-Type": "application/json" } },
    ));
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={queryClient}><ProfilePanel /></QueryClientProvider>);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(consoleError.mock.calls.flat().join(" ")).not.toContain("Cannot update a component");
  });
});
