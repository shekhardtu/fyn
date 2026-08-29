import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createMemoryRouter } from "react-router";
import { RouterProvider } from "react-router/dom";
import { ProfilePanel } from "@/components/profile";

const PROFILE = {
  id: "5f1f0f6f-2b3a-4a4a-9d1a-3f8f9a0b1c2d",
  displayName: "Shekhar",
  currency: "INR",
  timezone: "Asia/Kolkata",
  email: "shekhardtu@gmail.com",
  phone: "+918800832389",
  identities: [
    {
      id: "8a2f0f6f-2b3a-4a4a-9d1a-3f8f9a0b1c2d",
      provider: "phone",
      value: "+918800832389",
      source: "otp",
      verifiedAt: "2026-08-20T04:13:16.273252Z",
      lastLoginAt: null,
    },
    {
      id: "9b3f0f6f-2b3a-4a4a-9d1a-3f8f9a0b1c2d",
      provider: "email",
      value: "shekhardtu@gmail.com",
      source: "otp",
      verifiedAt: "2026-08-29T04:13:16.273252Z",
      lastLoginAt: null,
    },
  ],
  googleSignInAvailable: false,
} as const;

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function renderProfile(profile: object = PROFILE) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.endsWith("/api/profile")) {
      if (init?.method === "PATCH") {
        const changes = JSON.parse(String(init.body));
        return jsonResponse({ ...profile, ...changes });
      }
      return jsonResponse(profile);
    }
    if (url.endsWith("/api/document-assets")) return jsonResponse([]);
    throw new Error(`Unexpected request: ${url}`);
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([
    { path: "/profile", element: <ProfilePanel /> },
    { path: "/login", element: <p>Sign in</p> },
  ], { initialEntries: ["/profile"] });

  render(<QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider>);
}

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

describe("ProfilePanel sign-in methods", () => {
  it("edits each existing identifier from its row instead of showing detached change buttons", async () => {
    renderProfile();

    const editPhone = await screen.findByRole("button", { name: "Edit phone number +918800832389" });
    expect(screen.getByRole("button", { name: "Edit email address shekhardtu@gmail.com" })).toBeVisible();
    expect(screen.queryByRole("button", { name: /change your (phone number|email address)/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add (a phone number|an email address)/i })).not.toBeInTheDocument();

    editPhone.click();

    expect(await screen.findByText("Edit your phone number")).toBeVisible();
    expect(screen.getByText("Your current phone number will keep working until the new one is verified.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeVisible();
    expect(editPhone).toHaveAttribute("aria-expanded", "true");
  });

  it("offers add only for a sign-in method that is not linked", async () => {
    renderProfile({
      ...PROFILE,
      email: null,
      identities: [PROFILE.identities[0]],
      googleSignInAvailable: true,
    });

    expect(await screen.findByRole("button", { name: "Edit phone number +918800832389" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Add an email address" })).toBeVisible();
    expect(screen.queryByRole("button", { name: /edit email address/i })).not.toBeInTheDocument();
  });

  it("identifies a Google-managed email without offering a local edit action", async () => {
    renderProfile({
      ...PROFILE,
      identities: [{ ...PROFILE.identities[1], source: "google" }],
      googleSignInAvailable: false,
    });

    expect(await screen.findByText("Email address · managed by Google")).toBeVisible();
    expect(screen.queryByRole("button", { name: /edit email address/i })).not.toBeInTheDocument();
  });
});

describe("ProfilePanel regional defaults", () => {
  it("updates the selected currency and sends both application defaults", async () => {
    renderProfile();

    const currency = await screen.findByRole("combobox", { name: "Default currency" });
    expect(currency).toHaveTextContent("Indian rupee (INR)");
    expect(screen.getByRole("combobox", { name: "Timezone" })).toHaveTextContent("Asia/Kolkata");
    expect(screen.getByRole("button", { name: "Update defaults" })).toBeDisabled();

    fireEvent.click(currency);
    fireEvent.click(screen.getByRole("option", { name: "US dollar (USD)" }));
    fireEvent.click(screen.getByRole("button", { name: "Update defaults" }));

    await waitFor(() => {
      const patch = vi.mocked(globalThis.fetch).mock.calls.find((call) => call[1]?.method === "PATCH");
      expect(patch).toBeDefined();
      expect(JSON.parse(String(patch?.[1]?.body))).toEqual({
        displayName: "Shekhar",
        currency: "USD",
        timezone: "Asia/Kolkata",
      });
    });
    expect(currency).toHaveTextContent("US dollar (USD)");
  });
});
