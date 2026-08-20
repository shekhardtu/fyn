import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { isIosSafari, useInstallOffer } from "@/lib/install-prompt";

function setAgent(userAgent: string, maxTouchPoints = 0) {
  Object.defineProperty(navigator, "userAgent", { value: userAgent, configurable: true });
  Object.defineProperty(navigator, "maxTouchPoints", { value: maxTouchPoints, configurable: true });
}

function fireInstallPrompt(outcome: "accepted" | "dismissed" = "accepted") {
  // Cancelable, as the real event is — preventDefault() is what suppresses
  // Chromium's own mini-infobar.
  const event = Object.assign(new Event("beforeinstallprompt", { cancelable: true }), {
    prompt: vi.fn().mockResolvedValue(undefined),
    userChoice: Promise.resolve({ outcome }),
  });
  act(() => { window.dispatchEvent(event); });
  return event;
}

afterEach(() => vi.restoreAllMocks());

describe("isIosSafari", () => {
  it("recognises an iPhone, and an iPad reporting itself as a touch Mac", () => {
    setAgent("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) AppleWebKit/605.1.15 Version/18.0 Safari/604.1");
    expect(isIosSafari()).toBe(true);
    setAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15", 5);
    expect(isIosSafari()).toBe(true);
  });

  it("excludes the iOS browsers with no Add to Home Screen to point at", () => {
    setAgent("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) CriOS/120.0 Mobile/15E148 Safari/604.1");
    expect(isIosSafari()).toBe(false);
  });

  it("excludes a desktop Mac, which has no touch points", () => {
    setAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15", 0);
    expect(isIosSafari()).toBe(false);
  });
});

describe("useInstallOffer", () => {
  it("offers nothing it cannot deliver on a browser that never asks", () => {
    setAgent("Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/130.0");
    const { result } = renderHook(() => useInstallOffer());
    expect(result.current.kind).toBe("unavailable");
  });

  it("offers Safari the Share sheet, which is its only route", () => {
    setAgent("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) AppleWebKit/605.1.15 Version/18.0 Safari/604.1");
    const { result } = renderHook(() => useInstallOffer());
    expect(result.current.kind).toBe("instructions");
  });

  it("holds Chromium's prompt, suppresses its own bar, and replays it once", async () => {
    setAgent("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126.0 Mobile Safari/537.36");
    const { result } = renderHook(() => useInstallOffer());
    const event = fireInstallPrompt("accepted");

    expect(event.defaultPrevented).toBe(true);
    expect(result.current.kind).toBe("prompt");

    let outcome: string | undefined;
    await act(async () => {
      if (result.current.kind === "prompt") outcome = await result.current.install();
    });
    expect(outcome).toBe("accepted");
    expect(event.prompt).toHaveBeenCalledOnce();
    // Spent events are dropped, so no button survives that would do nothing.
    expect(result.current.kind).not.toBe("prompt");
  });

  it("withdraws the offer once the app reports itself installed", () => {
    setAgent("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126.0 Mobile Safari/537.36");
    const { result } = renderHook(() => useInstallOffer());
    fireInstallPrompt();
    expect(result.current.kind).toBe("prompt");
    act(() => { window.dispatchEvent(new Event("appinstalled")); });
    expect(result.current.kind).toBe("installed");
  });
});
