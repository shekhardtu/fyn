import { Platform } from "react-native";
import * as SecureStore from "expo-secure-store";

/**
 * Where this device keeps its session.
 *
 * On a phone there is no cookie jar the platform will manage across reinstalls
 * and process death, so the token lives in the Keychain on iOS and the
 * EncryptedSharedPreferences-backed Keystore on Android, and rides on the
 * `Authorization` header. `expo-secure-store` is the storage the OS gives a
 * password manager; it is not `AsyncStorage`, which is a plaintext file.
 *
 * On the web target there is no secure store at all — and there does not need
 * to be one. The server sets the same session as an `httponly` cookie the
 * browser carries by itself, which is strictly safer than anything this module
 * could do: a token in `localStorage` is readable by any script that gets onto
 * the page, which is the exact exposure the cookie exists to prevent. So on web
 * every function here is deliberately a no-op and the requests fall back to
 * `credentials: "include"`.
 */
const KEY = "financial-copilot.session";

/** True where a session token is ours to hold. */
export const HOLDS_TOKEN = Platform.OS !== "web";

/** Read once at startup and then kept here, because every request needs it and
 *  a Keychain round trip is a real syscall — not something to pay per fetch. */
let cached: string | null = null;
let loaded = false;

export async function loadSession(): Promise<string | null> {
  if (!HOLDS_TOKEN) { loaded = true; return null; }
  if (loaded) return cached;
  try {
    cached = await SecureStore.getItemAsync(KEY);
  } catch {
    // A Keychain that will not open is a device problem, not a session: treat
    // it as signed out rather than blocking the app from starting.
    cached = null;
  }
  loaded = true;
  return cached;
}

export function currentSession(): string | null {
  return cached;
}

/** Whether the app believes it is signed in.
 *
 *  On native that is "we hold a token". On web the cookie is unreadable by
 *  design, so the only honest answer comes from asking the server, and the
 *  caller is expected to do that rather than guess from here. */
export function hasStoredSession(): boolean {
  return HOLDS_TOKEN ? cached !== null : false;
}

export async function saveSession(token: string): Promise<void> {
  if (!HOLDS_TOKEN) return;
  cached = token;
  loaded = true;
  try {
    await SecureStore.setItemAsync(KEY, token, {
      // The session should not survive to a device where the passcode has been
      // removed, and it has no business syncing to the user's other hardware.
      keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
    });
  } catch {
    // Held in memory for this launch even if the write failed; the alternative
    // is refusing a sign-in that the server has already granted.
  }
}

export async function clearSession(): Promise<void> {
  cached = null;
  loaded = true;
  if (!HOLDS_TOKEN) return;
  try {
    await SecureStore.deleteItemAsync(KEY);
  } catch {
    // Already gone is the outcome we wanted.
  }
}
