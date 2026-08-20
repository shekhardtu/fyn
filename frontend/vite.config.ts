import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { execFileSync } from "node:child_process";
import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import packageJson from "./package.json" with { type: "json" };
import { API_MOUNT_PATH } from "./src/config/api-path.ts";

const BUILD_VERSION_PLACEHOLDER = "__FYN_BUILD_VERSION__";
const BUILD_COMMIT_ENV_KEYS = [
  "YOFIX_GIT_COMMIT_SHA",
  "YOFIX_COMMIT_SHA",
  "GITHUB_SHA",
  "SOURCE_VERSION",
  "COMMIT_SHA",
] as const;

function resolveBuildCommit(): string {
  const environmentCommit = BUILD_COMMIT_ENV_KEYS
    .map((key) => process.env[key]?.trim())
    .find(Boolean);

  if (environmentCommit) {
    return environmentCommit.slice(0, 12);
  }

  try {
    return execFileSync("git", ["rev-parse", "--short=12", "HEAD"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return "unknown";
  }
}

const buildVersion = `${packageJson.version}+${resolveBuildCommit()}`;

/* The browser talks to the app's own origin and this proxy relays /api to the
   backend, mirroring nginx.conf in production. Same-origin is what keeps the
   httpOnly session cookie first-party — installed-PWA standalone mode and
   third-party-cookie phase-outs both punish the cross-origin alternative. */
const BACKEND_ORIGIN = process.env.BACKEND_ORIGIN ?? "http://localhost:8000";
const API_PROXY_CONTEXT = `^${API_MOUNT_PATH}(?:/|$)`;
const apiProxy = {
  [API_PROXY_CONTEXT]: {
    target: BACKEND_ORIGIN,
    changeOrigin: true,
    rewrite: (path: string) => path.slice(API_MOUNT_PATH.length) || "/",
  },
} as const;

export default defineConfig({
  plugins: [
    {
      name: "fyn-build-version",
      transformIndexHtml(html) {
        return html.replace(BUILD_VERSION_PLACEHOLDER, buildVersion);
      },
    },
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    host: "0.0.0.0",
    port: 3000,
    strictPort: true,
    proxy: apiProxy,
  },
  preview: {
    host: "0.0.0.0",
    port: 3000,
    strictPort: true,
    proxy: apiProxy,
  },
});
