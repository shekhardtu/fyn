from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import router as api_router
from app.api_auth import router as auth_router
from app.config import DEVELOPMENT_AUTH_SECRET, SESSION_COOKIE_NAME, Settings, get_settings, require_production_auth_config
from app.database import get_db
from app.domain import IdentityProvider
from app.models import Conversation, User, UserIdentity, UserSession
from app.seed import DEFAULT_USER_EMAIL
from app.services.google_identity import GoogleAccount
from app.services.identity import normalize_email, normalize_phone


PHONE = "+919876543210"
OTHER_PHONE = "+919000000001"
EMAIL = "person@example.com"


@pytest.fixture()
def settings(monkeypatch):
    """Local-development authentication settings.

    The debug echo is what lets a test complete a sign-in without an SMS
    account; `require_production_auth_config` refuses it against a real
    database. `secure` is off because TestClient speaks http, and a Secure
    cookie would never be sent back.
    """
    current = get_settings()
    monkeypatch.setattr(current, "otp_debug_echo", True)
    monkeypatch.setattr(current, "session_cookie_secure", False)
    monkeypatch.setattr(current, "otp_resend_interval_seconds", 0)
    monkeypatch.setattr(current, "google_client_id", "test-client.apps.googleusercontent.com")
    return current


@pytest.fixture()
def client(db, settings):
    application = FastAPI()
    application.include_router(auth_router)
    application.include_router(api_router)
    application.dependency_overrides[get_db] = lambda: db
    with TestClient(application) as test_client:
        yield test_client


def sign_in_with_phone(client, phone: str = PHONE) -> dict:
    """The whole passwordless round trip: ask for a code, then present it."""
    started = client.post("/api/auth/otp/start", json={"channel": "phone", "value": phone})
    assert started.status_code == 200, started.text
    body = started.json()
    verified = client.post(
        "/api/auth/otp/verify",
        json={"challengeId": body["challengeId"], "code": body["debugCode"]},
    )
    assert verified.status_code == 200, verified.text
    return verified.json()


def link_identifier(client, channel: str, value: str) -> tuple[int, dict]:
    started = client.post("/api/profile/identities/otp/start", json={"channel": channel, "value": value})
    if started.status_code != 200:
        return started.status_code, started.json()
    body = started.json()
    verified = client.post(
        "/api/profile/identities/otp/verify",
        json={"challengeId": body["challengeId"], "code": body["debugCode"]},
    )
    return verified.status_code, verified.json()


def google_account(monkeypatch, *, subject: str, email: str, name: str | None = "Signed In"):
    monkeypatch.setattr(
        "app.services.auth.verify_google_credential",
        lambda credential: GoogleAccount(subject=subject, email=email, display_name=name),
    )


# ── Startup configuration ────────────────────────────────────────────────────


PRODUCTION_READY = {
    "environment": "production",
    "auth_secret": "x" * 48,
    "otp_debug_echo": False,
    "msg91_auth_key": "key",
    "msg91_template_id": "template",
    "postmark_server_token": "token",
    "postmark_from_email": "login@fynai.com",
}


def _startup(**changes) -> str | None:
    """The refusal a deployment would get, or None if it would start."""
    try:
        require_production_auth_config(Settings(_env_file=None, **{**PRODUCTION_READY, **changes}))
        return None
    except RuntimeError as error:
        return str(error)


@pytest.mark.parametrize(
    ("label", "changes"),
    [
        # An env file line of `AUTH_SECRET=` reads as empty, not as unset, so it
        # never matches the placeholder — the case that has to be named directly.
        ("empty", {"auth_secret": ""}),
        ("whitespace", {"auth_secret": "   "}),
        ("placeholder", {"auth_secret": DEVELOPMENT_AUTH_SECRET}),
        ("too short", {"auth_secret": "abc123"}),
    ],
)
def test_production_refuses_a_secret_that_is_not_one(label, changes):
    refusal = _startup(**changes)
    assert refusal is not None, label
    assert "AUTH_SECRET" in refusal


def test_production_refuses_settings_that_compromise_every_channel():
    assert "OTP_DEBUG_ECHO" in (_startup(otp_debug_echo=True) or "")


def test_production_starts_without_a_provider_for_one_channel():
    """A channel with no provider disables that channel, not the deployment.

    Refusing to boot took down sign-in on the channel that *was* configured,
    and every other endpoint with it — for a condition only reachable by
    someone choosing that one way in.
    """
    assert _startup(msg91_auth_key=None) is None
    assert _startup(postmark_server_token=None) is None


def test_a_channel_without_a_provider_is_reported_at_startup(capsys):
    """Unavailable is not the same as unnoticed."""
    assert _startup(postmark_server_token=None) is None
    assert "email sign-in is unavailable" in capsys.readouterr().out


def test_a_channel_without_a_provider_is_refused_when_it_is_used(client, settings, monkeypatch):
    """The refusal lands on the person choosing that channel, not on the server.

    503 rather than 400: the caller did nothing wrong and cannot fix it by
    retrying — the same answer an unconfigured Google client gives.
    """
    current = get_settings()
    monkeypatch.setattr(current, "environment", "production")
    monkeypatch.setattr(current, "postmark_server_token", None)

    refused = client.post("/api/auth/otp/start", json={"channel": "email", "value": "someone@example.com"})

    assert refused.status_code == 503
    assert "Email sign-in is not available" in refused.json()["detail"]


def test_the_configured_channel_still_works_when_another_has_no_provider(client, settings, monkeypatch):
    """The whole point: one missing provider must not close the other door."""
    current = get_settings()
    monkeypatch.setattr(current, "environment", "production")
    monkeypatch.setattr(current, "postmark_server_token", None)
    monkeypatch.setattr(current, "msg91_auth_key", "key")
    monkeypatch.setattr(current, "msg91_template_id", "template")
    monkeypatch.setattr("app.services.auth.deliver_code", lambda *a, **k: None)

    started = client.post("/api/auth/otp/start", json={"channel": "phone", "value": PHONE})

    assert started.status_code == 200


def test_a_fully_configured_deployment_starts():
    assert _startup() is None


def test_development_reports_the_same_findings_without_refusing(capsys):
    """Local work must stay possible without an SMS account — but never quietly."""
    assert _startup(environment="development", auth_secret="", otp_debug_echo=True) is None
    printed = capsys.readouterr().out
    assert "AUTH_SECRET is empty" in printed
    assert "OTP_DEBUG_ECHO" in printed


# ── Normalisation ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("typed", ["9876543210", "+91 98765 43210", "098765-43210", "0091 98765 43210"])
def test_one_phone_number_written_four_ways_normalises_to_one_identifier(typed, settings):
    assert normalize_phone(typed).key == PHONE


def test_google_address_variants_resolve_to_one_mailbox(settings):
    """Google treats dots and +tags as the same mailbox, so uniqueness must too."""
    assert normalize_email("First.Last+receipts@googlemail.com").key == "firstlast@gmail.com"
    assert normalize_email("firstlast@gmail.com").key == "firstlast@gmail.com"
    # Only Google's own domains work that way; everyone else's dots are real.
    assert normalize_email("First.Last@example.com").key == "first.last@example.com"


# ── Signing in ───────────────────────────────────────────────────────────────


def test_development_phone_otp_is_returned_without_sending_sms(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "otp_debug_echo", False)

    def must_not_deliver(*_args, **_kwargs):
        raise AssertionError("development phone OTP must not call a delivery provider")

    monkeypatch.setattr("app.services.auth.deliver_code", must_not_deliver)

    started = client.post("/api/auth/otp/start", json={"channel": "phone", "value": PHONE})

    assert started.status_code == 200
    body = started.json()
    assert body["debugCode"]
    verified = client.post(
        "/api/auth/otp/verify",
        json={"challengeId": body["challengeId"], "code": body["debugCode"]},
    )
    assert verified.status_code == 200


def test_first_phone_sign_in_adopts_the_account_that_predates_authentication(client, db):
    """The seeded local user carries the data recorded before there was a login."""
    seeded = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    db.add(Conversation(user_id=seeded.id, title="Recorded before sign-in existed"))
    db.commit()

    profile = sign_in_with_phone(client)["profile"]

    assert profile["id"] == str(seeded.id)
    assert profile["phone"] == PHONE
    assert [item["provider"] for item in profile["identities"]] == ["phone"]
    # The placeholder address is gone rather than left as a phantom identifier.
    assert profile["email"] is None
    assert client.get("/api/conversations").json()["items"][0]["title"] == "Recorded before sign-in existed"


def test_returning_phone_sign_in_resolves_to_the_same_account(client, db):
    first = sign_in_with_phone(client)["profile"]
    client.post("/api/auth/signout")
    second = sign_in_with_phone(client)["profile"]

    assert first["id"] == second["id"]
    assert db.scalar(select(User).where(User.phone == PHONE)) is not None


def test_a_second_number_gets_its_own_account(client, db):
    first = sign_in_with_phone(client)["profile"]
    client.post("/api/auth/signout")
    second = sign_in_with_phone(client, OTHER_PHONE)["profile"]

    assert first["id"] != second["id"]


def test_a_wrong_code_is_refused_and_the_attempts_run_out(client, settings):
    monkeyed = client.post("/api/auth/otp/start", json={"channel": "phone", "value": PHONE}).json()
    for _ in range(settings.otp_max_attempts):
        rejected = client.post(
            "/api/auth/otp/verify",
            json={"challengeId": monkeyed["challengeId"], "code": "000000"},
        )
        assert rejected.status_code == 400

    # The correct code no longer helps once the budget is spent.
    exhausted = client.post(
        "/api/auth/otp/verify",
        json={"challengeId": monkeyed["challengeId"], "code": monkeyed["debugCode"]},
    )
    assert exhausted.status_code == 400
    assert "attempts" in exhausted.json()["detail"].lower()
    assert SESSION_COOKIE_NAME not in client.cookies


def test_only_the_newest_code_for_a_destination_still_works(client):
    """Resending must not widen the guessable space."""
    first = client.post("/api/auth/otp/start", json={"channel": "phone", "value": PHONE}).json()
    second = client.post("/api/auth/otp/start", json={"channel": "phone", "value": PHONE}).json()

    stale = client.post(
        "/api/auth/otp/verify",
        json={"challengeId": first["challengeId"], "code": first["debugCode"]},
    )
    assert stale.status_code == 400
    current = client.post(
        "/api/auth/otp/verify",
        json={"challengeId": second["challengeId"], "code": second["debugCode"]},
    )
    assert current.status_code == 200


def test_codes_are_rate_limited_per_destination(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "otp_resend_interval_seconds", 45)
    client.post("/api/auth/otp/start", json={"channel": "phone", "value": PHONE})
    throttled = client.post("/api/auth/otp/start", json={"channel": "phone", "value": PHONE})

    assert throttled.status_code == 429
    assert int(throttled.headers["retry-after"]) > 0

    monkeypatch.setattr(settings, "otp_resend_interval_seconds", 0)
    for _ in range(settings.otp_max_sends_per_hour):
        client.post("/api/auth/otp/start", json={"channel": "phone", "value": PHONE})
    hourly = client.post("/api/auth/otp/start", json={"channel": "phone", "value": PHONE})
    assert hourly.status_code == 429


def test_a_typed_number_is_rejected_before_any_message_is_sent(client):
    refused = client.post("/api/auth/otp/start", json={"channel": "phone", "value": "12"})
    assert refused.status_code == 422


def test_google_sign_in_creates_then_recognises_one_account(client, db, monkeypatch):
    google_account(monkeypatch, subject="google-subject-1", email="Person@Gmail.com", name="Person")

    first = client.post("/api/auth/google", json={"credential": "token"})
    assert first.status_code == 200
    profile = first.json()["profile"]
    assert profile["displayName"] == "Person"
    assert {item["provider"] for item in profile["identities"]} == {"google", "email"}
    # The Google row reports the address, not the subject nobody can read.
    assert {item["value"] for item in profile["identities"]} == {"person@gmail.com"}

    client.post("/api/auth/signout")
    again = client.post("/api/auth/google", json={"credential": "token"}).json()["profile"]
    assert again["id"] == profile["id"]
    assert db.scalar(select(User).where(User.email == "person@gmail.com")) is not None


def test_google_sign_in_is_refused_when_the_server_has_no_client_id(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", None)
    refused = client.post("/api/auth/google", json={"credential": "token"})
    assert refused.status_code == 503


# ── Linking the two methods together ─────────────────────────────────────────


def test_a_google_account_links_a_phone_number_and_both_then_sign_in(client, monkeypatch):
    """The merge the profile page promises, from the Google side."""
    google_account(monkeypatch, subject="google-subject-1", email="person@gmail.com")
    created = client.post("/api/auth/google", json={"credential": "token"}).json()["profile"]

    status, profile = link_identifier(client, "phone", PHONE)
    assert status == 200
    assert profile["phone"] == PHONE
    assert {item["provider"] for item in profile["identities"]} == {"google", "email", "phone"}

    client.post("/api/auth/signout")
    assert sign_in_with_phone(client)["profile"]["id"] == created["id"]


def test_a_phone_account_verifies_an_email_and_a_later_google_sign_in_joins_it(client, monkeypatch):
    """The same merge from the phone side, completed by Google adoption."""
    created = sign_in_with_phone(client)["profile"]

    status, profile = link_identifier(client, "email", "Person@gmail.com")
    assert status == 200
    assert profile["email"] == "person@gmail.com"

    client.post("/api/auth/signout")
    google_account(monkeypatch, subject="google-subject-1", email="person@gmail.com")
    adopted = client.post("/api/auth/google", json={"credential": "token"}).json()["profile"]

    # Adopted, not duplicated: one account now answers to phone, email and Google.
    assert adopted["id"] == created["id"]
    assert {item["provider"] for item in adopted["identities"]} == {"phone", "email", "google"}


def test_an_identifier_held_by_another_account_cannot_be_linked(client, db):
    sign_in_with_phone(client, OTHER_PHONE)
    client.post("/api/auth/signout")
    sign_in_with_phone(client, PHONE)

    status, body = link_identifier(client, "phone", OTHER_PHONE)
    assert status == 409
    assert "another account" in body["detail"]
    assert "delete" in body["detail"].lower()

    # Refused before sending, so the other person's phone stays quiet.
    assert db.scalar(select(UserIdentity).where(UserIdentity.identifier == OTHER_PHONE)).user_id != db.scalar(
        select(User).where(User.phone == PHONE)
    ).id


def test_deleting_the_holding_account_releases_its_identifiers(client, db, monkeypatch):
    monkeypatch.setattr("app.services.user_data.clear_user_memories", lambda _user_id: 0)
    monkeypatch.setattr("app.services.user_data.export_user_memories", lambda _user_id: [])

    sign_in_with_phone(client, OTHER_PHONE)
    client.request("DELETE", "/api/privacy/data", json={"confirmation": "DELETE MY DATA"})

    sign_in_with_phone(client, PHONE)
    status, profile = link_identifier(client, "phone", OTHER_PHONE)
    assert status == 200
    assert profile["phone"] == OTHER_PHONE


def test_verifying_a_new_number_replaces_the_previous_one(client, db):
    sign_in_with_phone(client, PHONE)

    status, profile = link_identifier(client, "phone", OTHER_PHONE)
    assert status == 200
    assert profile["phone"] == OTHER_PHONE
    assert [item["value"] for item in profile["identities"] if item["provider"] == "phone"] == [OTHER_PHONE]
    # The retired number is released rather than left reserved.
    assert db.scalar(select(UserIdentity).where(UserIdentity.identifier == PHONE)) is None


def test_an_address_owned_by_google_is_not_replaced_by_a_code(client, monkeypatch):
    google_account(monkeypatch, subject="google-subject-1", email="person@gmail.com")
    client.post("/api/auth/google", json={"credential": "token"})

    status, body = link_identifier(client, "email", "somewhere.else@example.com")
    assert status == 409
    assert "Google" in body["detail"]


def test_linking_an_identifier_the_account_already_holds_is_refused(client):
    sign_in_with_phone(client, PHONE)
    status, body = link_identifier(client, "phone", PHONE)
    assert status == 409
    assert "already on your account" in body["detail"]


# ── Removing a sign-in method ────────────────────────────────────────────────


def test_the_only_sign_in_method_cannot_be_removed(client):
    profile = sign_in_with_phone(client)["profile"]
    only = profile["identities"][0]["id"]

    refused = client.delete(f"/api/profile/identities/{only}")
    assert refused.status_code == 409
    assert client.get("/api/profile").status_code == 200


def test_one_of_two_sign_in_methods_can_be_removed(client):
    sign_in_with_phone(client)
    _, profile = link_identifier(client, "email", EMAIL)
    email_id = next(item["id"] for item in profile["identities"] if item["provider"] == "email")

    removed = client.delete(f"/api/profile/identities/{email_id}")
    assert removed.status_code == 200
    assert removed.json()["email"] is None
    assert {item["provider"] for item in removed.json()["identities"]} == {"phone"}


def test_a_sign_in_method_on_another_account_is_not_removable(client, db):
    sign_in_with_phone(client, OTHER_PHONE)
    stranger = db.scalar(
        select(UserIdentity).where(UserIdentity.identifier == OTHER_PHONE)
    ).id
    client.post("/api/auth/signout")
    sign_in_with_phone(client, PHONE)

    assert client.delete(f"/api/profile/identities/{stranger}").status_code == 404


# ── The session ──────────────────────────────────────────────────────────────


def test_financial_routes_are_closed_until_a_session_exists(client):
    """Refusal happens in the dependency, before any route body runs."""
    guarded = ("/api/bootstrap", "/api/categories", "/api/conversations", "/api/transactions", "/api/privacy", "/api/profile")
    for path in guarded:
        assert client.get(path).status_code == 401, path

    sign_in_with_phone(client)
    for path in guarded:
        assert client.get(path).status_code == 200, path


def test_the_session_cookie_is_not_readable_by_script(client):
    started = client.post("/api/auth/otp/start", json={"channel": "phone", "value": PHONE}).json()
    verified = client.post(
        "/api/auth/otp/verify",
        json={"challengeId": started["challengeId"], "code": started["debugCode"]},
    )
    cookie = verified.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    # The response carries the profile, never the raw session value in a body.
    assert client.cookies[SESSION_COOKIE_NAME] not in verified.text


def test_signing_out_ends_that_session_for_good(client, db):
    sign_in_with_phone(client)
    token_count = db.scalar(select(UserSession).where(UserSession.revoked_at.is_(None)))
    assert token_count is not None

    assert client.post("/api/auth/signout").json() == {"signedOut": True}
    assert client.get("/api/profile").status_code == 401
    assert client.get("/api/auth/session").json()["authenticated"] is False


def test_the_session_endpoint_answers_for_a_visitor_who_is_not_signed_in(client):
    anonymous = client.get("/api/auth/session")
    assert anonymous.status_code == 200
    assert anonymous.json() == {
        "authenticated": False,
        "profile": None,
        "googleSignInAvailable": True,
    }


def test_a_deleted_account_cannot_keep_using_its_cookie(client, monkeypatch):
    monkeypatch.setattr("app.services.user_data.clear_user_memories", lambda _user_id: 0)
    sign_in_with_phone(client)

    client.request("DELETE", "/api/privacy/data", json={"confirmation": "DELETE MY DATA"})
    assert client.get("/api/profile").status_code == 401


def test_one_account_never_sees_another_accounts_profile(client, db):
    sign_in_with_phone(client, OTHER_PHONE)
    first = client.get("/api/profile").json()
    client.post("/api/auth/signout")
    sign_in_with_phone(client, PHONE)
    second = client.get("/api/profile").json()

    assert first["id"] != second["id"]
    assert second["phone"] == PHONE
    assert db.scalar(
        select(UserIdentity).where(UserIdentity.provider == IdentityProvider.PHONE.value, UserIdentity.identifier == PHONE)
    ).user_id == db.scalar(select(User).where(User.phone == PHONE)).id


def test_a_token_for_another_client_is_refused(monkeypatch):
    from app.services import google_identity

    current = get_settings()
    monkeypatch.setattr(current, "google_client_id", "web.apps.googleusercontent.com")

    import google.oauth2.id_token as id_token_module

    def always_reject(credential, request, audience):
        raise ValueError("wrong audience")

    monkeypatch.setattr(id_token_module, "verify_oauth2_token", always_reject)

    with pytest.raises(google_identity.GoogleAuthError):
        google_identity.verify_google_credential("a-token")
