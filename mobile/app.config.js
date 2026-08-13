const base = require("./app.json");

/**
 * The static config, plus the one thing it cannot express.
 *
 * Google's iOS sign-in returns its result to a URL scheme derived from the
 * OAuth client ID — `com.googleusercontent.apps.<id>` — so the scheme is not
 * knowable until an installation has its own client. `app.json` cannot read the
 * environment; this can, which keeps the client ID in `.env` with the others
 * rather than committed into the config.
 *
 * With no client ID configured, nothing is added and the sign-in screen hides
 * the Google button. That is the correct state for an installation that only
 * wants phone and email sign-in.
 */
function reversedClientScheme(clientId) {
  if (!clientId) return null;
  // Google publishes the iOS client as `<digits>-<hash>.apps.googleusercontent.com`
  // and expects the redirect scheme to be that, reversed.
  const withoutSuffix = clientId.replace(/\.apps\.googleusercontent\.com$/, "");
  return `com.googleusercontent.apps.${withoutSuffix}`;
}

module.exports = ({ config }) => {
  const merged = { ...base.expo, ...config };
  const scheme = reversedClientScheme(process.env.EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID);
  if (!scheme) return merged;

  // `scheme` accepts an array, and expo-router's own scheme has to stay first
  // so deep links into the app keep resolving to it.
  const schemes = Array.isArray(merged.scheme) ? merged.scheme : [merged.scheme].filter(Boolean);
  return {
    ...merged,
    scheme: schemes.includes(scheme) ? schemes : [...schemes, scheme],
  };
};
