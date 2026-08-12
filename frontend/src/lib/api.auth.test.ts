import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, getAuthStatus, getProfile, isUnauthorized, startLinkCode, startSignInCode, verifySignInCode } from "@/lib/api";

/** The session lives in an httpOnly cookie, so these calls carry it only by
 *  asking fetch to include credentials. Nothing in the module can read the
 *  cookie to check its own work, which is exactly why the flag is asserted
 *  here: an omitted `credentials` would not fail loudly, it would silently
 *  make every request anonymous. */

const fetchMock = vi.fn();

function respond(body: unknown, init: { status?: number; headers?: Record<string, string> } = {}) {
  const status = init.status ?? 200;
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(init.headers ?? {}),
    json: async () => body,
  };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const PROFILE = {
  id: "5f1f0f6f-2b3a-4a4a-9d1a-3f8f9a0b1c2d",
  displayName: "You",
  currency: "INR",
  timezone: "Asia/Kolkata",
  email: null,
  phone: "+919876543210",
  identities: [{
    id: "8a2f0f6f-2b3a-4a4a-9d1a-3f8f9a0b1c2d",
    provider: "phone",
    value: "+919876543210",
    source: "otp",
    verifiedAt: "2026-08-12T04:13:16.273252Z",
    lastLoginAt: null,
  }],
  googleSignInAvailable: true,
};

const SENT = {
  challengeId: "1b9c1e2a-4d5f-4c6a-9b8e-7f6a5d4c3b2a",
  channel: "phone",
  destinationMasked: "+91•••••210",
  expiresInSeconds: 600,
  resendAfterSeconds: 45,
  debugCode: null,
};

describe("session-carrying requests", () => {
  it("includes credentials on every authenticated call", async () => {
    fetchMock.mockResolvedValue(respond({ authenticated: true, profile: PROFILE, googleSignInAvailable: true }));
    await getAuthStatus();
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: "include" });

    fetchMock.mockResolvedValue(respond(PROFILE));
    await getProfile();
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ credentials: "include" });

    fetchMock.mockResolvedValue(respond(SENT));
    await startSignInCode("phone", "9876543210");
    expect(fetchMock.mock.calls[2][1]).toMatchObject({ credentials: "include", method: "POST" });
  });

  it("sends the identifier as typed, leaving normalisation to the server", async () => {
    fetchMock.mockResolvedValue(respond(SENT));
    await startSignInCode("phone", " 98765 43210 ");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ channel: "phone", value: " 98765 43210 " });
  });

  it("separates signing in from linking, so a link cannot be issued unauthenticated", async () => {
    fetchMock.mockResolvedValue(respond(SENT));
    await startSignInCode("email", "person@example.com");
    await startLinkCode("email", "person@example.com");
    expect(fetchMock.mock.calls[0][0]).toContain("/api/auth/otp/start");
    expect(fetchMock.mock.calls[1][0]).toContain("/api/profile/identities/otp/start");
  });
});

describe("refusals", () => {
  it("carries the status so a lost session can be told from a real failure", async () => {
    fetchMock.mockResolvedValue(respond({ detail: "Sign in to continue." }, { status: 401 }));
    const error = await getProfile().catch((cause) => cause);

    expect(error).toBeInstanceOf(ApiError);
    expect(isUnauthorized(error)).toBe(true);
    expect(error.message).toBe("Sign in to continue.");
  });

  it("does not mistake a conflict for a lost session", async () => {
    fetchMock.mockResolvedValue(respond(
      { detail: "That phone number is already linked to another account." },
      { status: 409 },
    ));
    const error = await startLinkCode("phone", "+919876543210").catch((cause) => cause);

    expect(isUnauthorized(error)).toBe(false);
    expect(error.status).toBe(409);
    // The server's sentence reaches the reader unchanged; it names the remedy.
    expect(error.message).toContain("already linked to another account");
  });

  it("keeps Retry-After, which is what the resend countdown is set from", async () => {
    fetchMock.mockResolvedValue(respond(
      { detail: "A code was just sent. Wait 39 seconds before asking for another." },
      { status: 429, headers: { "Retry-After": "39" } },
    ));
    const error = await startSignInCode("phone", "9876543210").catch((cause) => cause);

    expect(error.status).toBe(429);
    expect(error.retryAfterSeconds).toBe(39);
  });

  it("leaves retryAfterSeconds null when the server sends no header", async () => {
    fetchMock.mockResolvedValue(respond({ detail: "That code is incorrect. 4 attempts left." }, { status: 400 }));
    const error = await verifySignInCode(SENT.challengeId, "000000").catch((cause) => cause);

    expect(error.retryAfterSeconds).toBeNull();
  });

  it("reports an unreachable API as a network problem, not a rejection", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const error = await getAuthStatus().catch((cause) => cause);

    expect(error).not.toBeInstanceOf(ApiError);
    expect(error.message).toContain("can’t reach");
  });
});

describe("contract validation", () => {
  it("rejects a profile the backend contract would not have produced", async () => {
    fetchMock.mockResolvedValue(respond({ ...PROFILE, identities: [{ provider: "carrier-pigeon" }] }));
    await expect(getProfile()).rejects.toThrow();
  });
});
