import { defineConfig, devices } from "@playwright/test";
import { STORAGE_STATE } from "./e2e/test-thread";

const APP_URL = process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:3000";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: APP_URL,
    channel: "chrome",
    permissions: ["clipboard-read", "clipboard-write"],
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  // Every page in the app is behind a session now, so the suite signs in once
  // and the scenarios inherit it rather than each re-entering a code.
  projects: [
    { name: "setup", testMatch: /.*\.setup\.ts/ },
    {
      name: "browser",
      testIgnore: [/.*\.setup\.ts/, /mobile\.spec\.ts/],
      dependencies: ["setup"],
      use: { storageState: STORAGE_STATE },
    },
    // The responsive layout was previously exercised by no test at any phone
    // size; this project keeps the drawer nav and no-sideways-scroll rules
    // honest on a real device profile.
    {
      name: "mobile",
      testMatch: /mobile\.spec\.ts/,
      dependencies: ["setup"],
      use: { ...devices["Pixel 7"], storageState: STORAGE_STATE },
    },
  ],
});
