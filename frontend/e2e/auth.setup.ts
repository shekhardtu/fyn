import { existsSync, writeFileSync } from "node:fs";

import { expect, test as setup } from "@playwright/test";
import { API_URL, SHARED_THREAD_STATE, STORAGE_STATE, TEST_PHONE } from "./test-thread";

/**
 * Signs the browser suite in once and saves the session for every spec.
 *
 * Asking for a code is the last resort, not the first step. The server rate
 * limits codes per identifier — an hour's lockout after a handful — and a suite
 * that re-authenticates on every run will spend that budget and then fail for a
 * reason that has nothing to do with the code under test. So a stored session
 * that still works is reused, and a code is only requested when there is no
 * usable session left.
 *
 * When one is requested, it is read back from the API response rather than from
 * an SMS, which is what `OTP_DEBUG_ECHO` exists for. That setting is refused in
 * production, so the shortcut cannot be taken anywhere it would matter.
 */
setup("browser suite signs in", async ({ playwright }) => {
  const reuse = existsSync(STORAGE_STATE)
    ? await playwright.request.newContext({ baseURL: API_URL, storageState: STORAGE_STATE })
    : null;

  if (reuse) {
    const session = await reuse.get("/auth/session");
    if (session.ok() && (await session.json()).authenticated) {
      await recordSharedThread(reuse);
      await reuse.dispose();
      return;
    }
    await reuse.dispose();
  }

  const api = await playwright.request.newContext({ baseURL: API_URL });

  const started = await api.post("/auth/otp/start", {
    data: { channel: "phone", value: TEST_PHONE },
  });
  expect(
    started.ok(),
    `Sign-in code was refused: ${await started.text()}\nIf this is the rate limit, the stored session in ${STORAGE_STATE} has expired and the limit needs to lapse before the suite can sign in again.`,
  ).toBeTruthy();

  const { challengeId, debugCode } = await started.json();
  expect(
    debugCode,
    "The API did not return the one-time code. Set OTP_DEBUG_ECHO=true in backend/.env to run the browser suite.",
  ).toBeTruthy();

  const verified = await api.post("/auth/otp/verify", {
    data: { challengeId, code: debugCode },
  });
  expect(verified.ok(), `Sign-in failed: ${await verified.text()}`).toBeTruthy();

  await api.storageState({ path: STORAGE_STATE });
  await recordSharedThread(api);
  await api.dispose();
});

/**
 * Records the conversation every spec shares.
 *
 * Resolved rather than hardcoded, because a thread belongs to an account and
 * the account is the one signed in above. A literal id survives only until the
 * database it was copied from is rebuilt, and then every spec lands on
 * "Conversation unavailable" — the app being right about ownership, and the
 * fixture being wrong about it.
 */
async function recordSharedThread(api: { get: (path: string) => Promise<{ ok: () => boolean; text: () => Promise<string>; json: () => Promise<{ active_conversation?: { id?: string } }> }> }) {
  const opened = await api.get("/bootstrap");
  expect(opened.ok(), `Could not open the workspace: ${await opened.text()}`).toBeTruthy();
  const { active_conversation: conversation } = await opened.json();
  expect(conversation?.id, "Bootstrap returned no conversation to share across the suite.").toBeTruthy();
  writeFileSync(SHARED_THREAD_STATE, `${JSON.stringify({ id: conversation?.id }, null, 2)}\n`);
}
