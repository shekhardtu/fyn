/**
 * One device fix for a transaction as it is written down.
 *
 * The browser's geolocation API is the only way to learn this, and it is
 * asynchronous, refusable, and sometimes slow. So nothing here ever blocks a
 * save: the fix is requested when the entry drawer opens and attached at
 * submit only if it has arrived by then. A person filling in an amount should
 * never wait on a GPS lock, and a refusal should cost them nothing.
 *
 * The `location:enabled` preference gates whether we ask at all. The server
 * checks it a second time before storing anything, because a tab left open
 * across a settings change would otherwise still be sending coordinates.
 */
import { useEffect, useState } from "react";

/** A position the device actually reported. */
export type DeviceFix = {
  latitude: number;
  longitude: number;
  locationAccuracy: number | null;
};

/** The location fields of a save payload, which may carry no position at all. */
export type LocationFields = {
  latitude: number | null;
  longitude: number | null;
  locationAccuracy: number | null;
};

const NO_FIX: LocationFields = { latitude: null, longitude: null, locationAccuracy: null };

// A minute-old fix is the same place for this purpose, and the platform
// returns it instantly from cache — which is the common case of logging two
// entries at one counter.
const MAX_FIX_AGE_MS = 60_000;
// Long enough for a cold lock indoors, short enough that the answer still
// concerns this transaction. Nothing waits on it either way.
const FIX_TIMEOUT_MS = 10_000;
// A fix describes where the device is now, so it can only be claimed as the
// place a transaction happened while the entry is about now. Logging Tuesday's
// lunch on Thursday must not stamp it with Thursday's coordinates — a wrong
// location is worse than none, and this is the one thing the person cannot see
// to correct.
export const RECENT_ENTRY_MS = 2 * 60 * 60 * 1000;

/**
 * Requests one position while `enabled` holds, and reports it once it lands.
 *
 * Failure is silent by design. Denied, unavailable, and timed out all mean the
 * same thing to the caller — save without a location — and none of them is
 * worth an error the person did not ask to see.
 */
export function useDeviceLocation(enabled: boolean): DeviceFix | null {
  const [fix, setFix] = useState<DeviceFix | null>(null);

  useEffect(() => {
    if (!enabled) return;
    // Absent on an insecure origin, which is a normal state for a LAN test
    // build rather than a fault worth reporting.
    if (typeof navigator === "undefined" || !navigator.geolocation) return;
    let live = true;
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        if (!live) return;
        setFix({
          latitude: coords.latitude,
          longitude: coords.longitude,
          locationAccuracy: Number.isFinite(coords.accuracy) ? Math.round(coords.accuracy) : null,
        });
      },
      () => undefined,
      { enableHighAccuracy: true, timeout: FIX_TIMEOUT_MS, maximumAge: MAX_FIX_AGE_MS },
    );
    return () => { live = false; };
  }, [enabled]);

  return fix;
}

/**
 * The location fields for an entry timestamped `transactionAt`.
 *
 * Returns nothing but nulls when there is no fix, or when the entry is not
 * about the present — see `RECENT_ENTRY_MS`. The keys are always present
 * because the payload type requires them; absence is expressed as null.
 */
export function fixForEntry(fix: DeviceFix | null, transactionAt: string, now = Date.now()): LocationFields {
  if (!fix) return NO_FIX;
  const when = Date.parse(transactionAt);
  if (!Number.isFinite(when) || Math.abs(when - now) > RECENT_ENTRY_MS) return NO_FIX;
  return fix;
}


/**
 * Provokes the browser's permission prompt at the moment the person asks for
 * the feature, and reports what they answered.
 *
 * Without this the switch would save silently and the prompt would ambush them
 * at their next entry — or never appear, leaving a setting that reads "on"
 * while the browser quietly refuses. Those are two independent permissions and
 * the settings page has to be able to say when they disagree.
 *
 * "prompt" means the question went unanswered: a timeout, or a device that
 * could not produce a position. It is not a refusal, and not a grant.
 */
export async function primeLocationPermission(): Promise<PermissionState | "unsupported"> {
  if (typeof navigator === "undefined" || !navigator.geolocation) return "unsupported";
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      () => resolve("granted"),
      (error) => resolve(error.code === error.PERMISSION_DENIED ? "denied" : "prompt"),
      { timeout: FIX_TIMEOUT_MS, maximumAge: MAX_FIX_AGE_MS },
    );
  });
}
