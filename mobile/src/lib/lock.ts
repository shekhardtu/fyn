import * as LocalAuthentication from "expo-local-authentication";
import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";

/**
 * The app lock.
 *
 * A finance app that opens straight to your spending on an unlocked, borrowed,
 * or briefly-unattended phone is below the bar every Indian banking app sets.
 * The session itself already lives in the Keychain; this is the second gate in
 * front of reading it.
 *
 * Deliberately not a passcode of our own. The device already has one, the OS
 * already knows how to prove it, and inventing a second secret would be a worse
 * one — stored by us, forgotten by the user, and recoverable only by signing
 * out. `expo-local-authentication` falls back from Face ID to Touch ID to the
 * device passcode on its own.
 */

const KEY = "financial-copilot.lock";

/** How long the app may sit in the background before it locks again.
 *
 *  Zero would be safest and unusable: switching to the SMS app to check an
 *  amount and coming straight back must not demand a face. A minute is long
 *  enough for that round trip and far short of walking away from the phone. */
export const GRACE_MS = 60_000;

export type LockCapability = {
  /** Hardware exists and at least one biometric or passcode is enrolled. */
  available: boolean;
  /** What to call it on this device, for the button and the setting. */
  label: string;
};

export async function lockCapability(): Promise<LockCapability> {
  if (Platform.OS === "web") return { available: false, label: "App lock" };
  const [hasHardware, enrolled, types] = await Promise.all([
    LocalAuthentication.hasHardwareAsync(),
    LocalAuthentication.isEnrolledAsync(),
    LocalAuthentication.supportedAuthenticationTypesAsync(),
  ]);
  if (!hasHardware || !enrolled) return { available: false, label: "App lock" };

  const { FACIAL_RECOGNITION, FINGERPRINT } = LocalAuthentication.AuthenticationType;
  const label = types.includes(FACIAL_RECOGNITION)
    ? Platform.OS === "ios" ? "Face ID" : "Face unlock"
    : types.includes(FINGERPRINT)
      ? Platform.OS === "ios" ? "Touch ID" : "Fingerprint"
      : "Device passcode";
  return { available: true, label };
}

export async function isLockEnabled(): Promise<boolean> {
  if (Platform.OS === "web") return false;
  try {
    return (await SecureStore.getItemAsync(KEY)) === "on";
  } catch {
    return false;
  }
}

export async function setLockEnabled(enabled: boolean): Promise<void> {
  if (Platform.OS === "web") return;
  try {
    if (enabled) await SecureStore.setItemAsync(KEY, "on");
    else await SecureStore.deleteItemAsync(KEY);
  } catch {
    // A preference that will not persist is not worth failing a sign-in over.
  }
}

/** Asks the OS to prove it is still the owner holding the phone. */
export async function unlock(label: string): Promise<boolean> {
  if (Platform.OS === "web") return true;
  try {
    const result = await LocalAuthentication.authenticateAsync({
      promptMessage: `Unlock fyn AI with ${label}`,
      // The device passcode is the floor, not a bypass: a face that will not
      // scan must still have a way in that is not "sign out and start again".
      disableDeviceFallback: false,
      fallbackLabel: "Use passcode",
      cancelLabel: "Cancel",
    });
    return result.success;
  } catch {
    return false;
  }
}
