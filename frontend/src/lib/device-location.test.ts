import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fixForEntry, primeLocationPermission, useDeviceLocation } from "@/lib/device-location";

const HERE = { latitude: 12.971599, longitude: 77.594566, locationAccuracy: 18 };

function stubGeolocation(behaviour: (ok: PositionCallback, fail: PositionErrorCallback) => void) {
  const getCurrentPosition = vi.fn(behaviour);
  Object.defineProperty(navigator, "geolocation", { value: { getCurrentPosition }, configurable: true });
  return getCurrentPosition;
}

function position(coords: { latitude: number; longitude: number; accuracy: number }) {
  return { coords, timestamp: 0 } as GeolocationPosition;
}

afterEach(() => {
  Reflect.deleteProperty(navigator, "geolocation");
  vi.restoreAllMocks();
});

describe("useDeviceLocation", () => {
  it("never asks the device while the preference is off", () => {
    const ask = stubGeolocation(() => undefined);
    renderHook(() => useDeviceLocation(false));
    expect(ask).not.toHaveBeenCalled();
  });

  it("reports the position once it arrives, rounding accuracy to whole metres", async () => {
    stubGeolocation((ok) => ok(position({ latitude: 12.971599, longitude: 77.594566, accuracy: 17.6 })));
    const { result } = renderHook(() => useDeviceLocation(true));
    await waitFor(() => expect(result.current).toEqual(HERE));
  });

  it("stays empty when the person refuses, rather than surfacing an error", async () => {
    stubGeolocation((_ok, fail) => fail({ code: 1, message: "denied" } as GeolocationPositionError));
    const { result } = renderHook(() => useDeviceLocation(true));
    await waitFor(() => expect(result.current).toBeNull());
  });

  it("survives an origin with no geolocation API at all", () => {
    Reflect.deleteProperty(navigator, "geolocation");
    const { result } = renderHook(() => useDeviceLocation(true));
    expect(result.current).toBeNull();
  });
});

describe("fixForEntry", () => {
  it("attaches the browser fix to every new entry, including a backdated one", () => {
    expect(fixForEntry(HERE)).toEqual(HERE);
  });

  it("sends explicit nulls when no fix arrived, so the payload stays complete", () => {
    expect(fixForEntry(null)).toEqual({ latitude: null, longitude: null, locationAccuracy: null });
  });
});

describe("primeLocationPermission", () => {
  it("reports a grant when the device answers with a position", async () => {
    stubGeolocation((ok) => ok(position({ latitude: 1, longitude: 2, accuracy: 5 })));
    await expect(primeLocationPermission()).resolves.toBe("granted");
  });

  it("distinguishes a refusal from a device that simply could not answer", async () => {
    stubGeolocation((_ok, fail) => fail({ code: 1, PERMISSION_DENIED: 1, message: "denied" } as GeolocationPositionError));
    await expect(primeLocationPermission()).resolves.toBe("denied");

    stubGeolocation((_ok, fail) => fail({ code: 3, PERMISSION_DENIED: 1, message: "timeout" } as GeolocationPositionError));
    await expect(primeLocationPermission()).resolves.toBe("prompt");
  });

  it("says so when the origin has no geolocation API to ask", async () => {
    Reflect.deleteProperty(navigator, "geolocation");
    await expect(primeLocationPermission()).resolves.toBe("unsupported");
  });
});
