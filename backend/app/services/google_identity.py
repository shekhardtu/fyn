"""Verification of the Google credential presented at sign-in.

The browser hands over an ID token. Everything inside it is attacker-controlled
until the signature, the issuer, the audience, and the expiry have been checked
against Google's published keys, so none of its claims are read before that.

`google-auth` performs the check and caches the key set. Its absence is treated
as "Google sign-in is not available" rather than as a crash, so an installation
that only wants phone sign-in has nothing extra to install.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import get_settings


GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


class GoogleAuthError(Exception):
    """The credential is missing, malformed, or not for this application."""


class GoogleUnavailable(Exception):
    """Google sign-in is not configured or its verifier is not installed."""


@dataclass(frozen=True)
class GoogleAccount:
    subject: str
    email: str
    display_name: str | None


def google_sign_in_enabled() -> bool:
    return bool(get_settings().google_audience)


def verify_google_credential(credential: str) -> GoogleAccount:
    """Return the verified Google account behind an ID token."""
    audience = get_settings().google_audience
    if not audience:
        raise GoogleUnavailable("Google sign-in is not configured on this server.")
    if not credential:
        raise GoogleAuthError("Google did not return a credential.")

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
    except ImportError as error:  # pragma: no cover - depends on the install
        raise GoogleUnavailable(
            "Google sign-in needs the google-auth package on the server."
        ) from error

    try:
        claims = id_token.verify_oauth2_token(credential, google_requests.Request(), audience)
    except Exception:
        # google-auth raises ValueError for every rejection reason — bad
        # signature, wrong audience, expired. None of them are distinguishable
        # to the caller, and saying which one failed only helps an attacker.
        raise GoogleAuthError("That Google sign-in could not be verified. Try again.")

    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise GoogleAuthError("That Google sign-in could not be verified. Try again.")

    subject = claims.get("sub")
    email = claims.get("email")
    if not subject or not email:
        raise GoogleAuthError("That Google account did not share an email address.")
    if not claims.get("email_verified"):
        # An unverified address would let a Google account claim a mailbox its
        # owner never proved, which is exactly what the uniqueness rule protects.
        raise GoogleAuthError("That Google account's email address is not verified.")

    return GoogleAccount(
        subject=str(subject),
        email=str(email),
        display_name=(claims.get("name") or claims.get("given_name") or None),
    )
