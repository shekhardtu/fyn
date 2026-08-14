import { StrictMode } from "react";
import { act, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

describe("GoogleSignInButton", () => {
  afterEach(() => {
    document.querySelector('script[src="https://accounts.google.com/gsi/client"]')?.remove();
    Reflect.deleteProperty(window, "google");
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("initializes Google Identity once under React Strict Mode", async () => {
    vi.stubEnv("VITE_GOOGLE_CLIENT_ID", "web-client-id");
    vi.resetModules();

    const initialize = vi.fn();
    const renderButton = vi.fn((holder: HTMLElement) => {
      holder.textContent = "Continue with Google";
    });
    Object.defineProperty(window, "google", {
      configurable: true,
      value: { accounts: { id: { initialize, renderButton } } },
    });
    const script = document.createElement("script");
    script.src = "https://accounts.google.com/gsi/client";
    script.dataset.loaded = "true";
    document.head.append(script);

    const { GoogleSignInButton } = await import("@/components/sign-in");
    const onCredential = vi.fn();
    render(<StrictMode><GoogleSignInButton onCredential={onCredential} onProblem={vi.fn()} /></StrictMode>);

    await waitFor(() => expect(renderButton).toHaveBeenCalled());
    expect(initialize).toHaveBeenCalledTimes(1);

    const options = initialize.mock.calls[0][0] as { callback: (response: { credential: string }) => void };
    act(() => options.callback({ credential: "signed-google-credential" }));
    expect(onCredential).toHaveBeenCalledWith("signed-google-credential");
  });
});
