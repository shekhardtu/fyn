import * as AuthSession from "expo-auth-session";
import * as Google from "expo-auth-session/providers/google";
import * as WebBrowser from "expo-web-browser";
import { useEffect } from "react";
import { Platform } from "react-native";

import { Button } from "@/components/ui";
import { signInWithGoogle } from "@/lib/api";

/**
 * Google sign-in, the way a phone has to do it.
 *
 * The web app loads Google's Identity Services script, which renders its own
 * button and hands back an ID token. That script is browser-only, so a native
 * app opens the system browser through `expo-auth-session` instead, and Google
 * returns the token to the app's URL scheme.
 *
 * The consequence worth knowing: the token comes back with the audience of
 * whichever OAuth client asked for it. A phone asks as the iOS or Android
 * client, not the web one, so the server has to accept all three — see
 * `google_audiences` in the backend settings. Without that, every native Google
 * sign-in fails verification, and the error deliberately will not say why.
 */

// Lets the auth session close the browser tab it opened rather than leaving the
// person to dismiss it by hand.
WebBrowser.maybeCompleteAuthSession();

const CLIENT_IDS = {
  iosClientId: process.env.EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID,
  androidClientId: process.env.EXPO_PUBLIC_GOOGLE_ANDROID_CLIENT_ID,
  webClientId: process.env.EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID,
};

/**
 * Where Google sends the result.
 *
 * Left to infer, `expo-auth-session` derives this from the running environment
 * — and inside a development build that environment is the dev client, which
 * produces a URI carrying an `expo-development-client` path that Google has no
 * record of and rejects as `redirect_uri_mismatch`.
 *
 * Google's iOS clients accept exactly one shape, derived from the client ID, so
 * it is stated here rather than guessed. The same value works in a dev build,
 * a release build and TestFlight, because it does not depend on how the app was
 * launched.
 */
function iosRedirectUri() {
  const clientId = CLIENT_IDS.iosClientId;
  if (!clientId) return undefined;
  const reversed = `com.googleusercontent.apps.${clientId.replace(/\.apps\.googleusercontent\.com$/, "")}`;
  return AuthSession.makeRedirectUri({ native: `${reversed}:/oauthredirect` });
}

/**
 * Whether this build can offer Google at all.
 *
 * Checked per platform, not globally: `useIdTokenAuthRequest` throws outright
 * if the client ID for the running platform is missing, and a hook cannot be
 * called conditionally — so the decision has to happen before the component
 * that owns the hook is mounted, which is why the button below is a component
 * rather than a hook the screen calls.
 */
export function googleSignInConfigured() {
  if (Platform.OS === "ios") return Boolean(CLIENT_IDS.iosClientId);
  if (Platform.OS === "android") return Boolean(CLIENT_IDS.androidClientId);
  return Boolean(CLIENT_IDS.webClientId);
}

export function GoogleSignInButton({ onSignedIn, onProblem }: {
  onSignedIn: () => void;
  onProblem: (message: string) => void;
}) {
  // `id_token` is deliberate: the server verifies an ID token against Google's
  // keys and needs nothing else. Asking for an access token would hand this app
  // a credential it has no use for and would then have to be trusted not to keep.
  const [request, response, promptAsync] = Google.useIdTokenAuthRequest({
    ...CLIENT_IDS,
    ...(Platform.OS === "ios" ? { redirectUri: iosRedirectUri() } : {}),
  });

  useEffect(() => {
    if (!response) return;
    if (response.type === "dismiss" || response.type === "cancel") return;
    if (response.type === "error") {
      onProblem("That Google sign-in didn’t complete. Try again.");
      return;
    }
    if (response.type !== "success") return;

    const credential = response.params?.id_token;
    if (!credential) {
      onProblem("Google didn’t return a usable sign-in. Try again, or use a code instead.");
      return;
    }
    // The server is the only thing that verifies this; nothing here reads a
    // single claim out of it first.
    signInWithGoogle(credential).then(onSignedIn).catch((error: Error) => onProblem(error.message));
  }, [response, onSignedIn, onProblem]);

  return (
    <Button block size="field" variant="outline" onPress={() => void promptAsync()} disabled={!request}>
      Continue with Google
    </Button>
  );
}
