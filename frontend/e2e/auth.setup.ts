import { expect, test as setup } from "@playwright/test";
import { API_URL, STORAGE_STATE, TEST_PHONE } from "./test-thread";

/**
 * Signs the browser suite in once and saves the session for every spec.
 *
 * The code is read back from the API rather than from an SMS, which is what
 * `OTP_DEBUG_ECHO` exists for. That setting is refused in production, so this
 * shortcut cannot be taken anywhere it would matter.
 */
setup("browser suite signs in", async ({ playwright }) => {
  const api = await playwright.request.newContext({ baseURL: API_URL });

  const started = await api.post("/api/auth/otp/start", {
    data: { channel: "phone", value: TEST_PHONE },
  });
  expect(started.ok(), `Sign-in code was refused: ${await started.text()}`).toBeTruthy();

  const { challengeId, debugCode } = await started.json();
  expect(
    debugCode,
    "The API did not return the one-time code. Set OTP_DEBUG_ECHO=true in backend/.env to run the browser suite.",
  ).toBeTruthy();

  const verified = await api.post("/api/auth/otp/verify", {
    data: { challengeId, code: debugCode },
  });
  expect(verified.ok(), `Sign-in failed: ${await verified.text()}`).toBeTruthy();

  await api.storageState({ path: STORAGE_STATE });
  await api.dispose();
});
