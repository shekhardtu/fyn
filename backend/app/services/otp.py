"""One-time codes: issue, rate-limit, and verify.

A six-digit code is only as good as the limits around it. Three of them matter,
and all three are enforced here rather than at the transport:

  · a code lives for ten minutes and dies when a newer one is sent,
  · a challenge accepts five wrong guesses and then stops accepting any,
  · a destination accepts a handful of sends an hour, spaced apart.

Codes are stored as an HMAC keyed by the server's `AUTH_SECRET`. The digest is
bound to the challenge id, so the same code issued twice produces different
rows and a stolen digest cannot be replayed against another challenge.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain import OtpChannel, OtpPurpose
from ..event_time import as_utc, now_utc
from ..models import OtpChallenge


class OtpError(Exception):
    """A challenge that cannot be completed as presented."""


class OtpChannelUnavailable(OtpError):
    """The channel asked for has no delivery provider on this deployment.

    Separate from OtpError because it is not a bad request: the caller did
    nothing wrong and retrying will not help until the server is configured.
    """


class OtpRateLimited(OtpError):
    """Too many codes asked for, too fast."""

    def __init__(self, message: str, retry_after_seconds: int):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class IssuedChallenge:
    challenge: OtpChallenge
    code: str


def _now() -> datetime:
    return now_utc()


def _hash_code(challenge_id: UUID, code: str) -> str:
    secret = get_settings().auth_secret.encode()
    return hmac.new(secret, f"{challenge_id}:{code}".encode(), sha256).hexdigest()


def _generate_code(length: int) -> str:
    # Uniform over the whole space, including codes with leading zeros.
    return str(secrets.randbelow(10 ** length)).zfill(length)


def _enforce_send_rate(db: Session, destination: str) -> None:
    settings = get_settings()
    now = _now()

    latest = db.scalar(
        select(OtpChallenge)
        .where(OtpChallenge.destination == destination)
        .order_by(OtpChallenge.created_at.desc())
        .limit(1)
    )
    if latest is not None:
        elapsed = (now - as_utc(latest.created_at)).total_seconds()
        if elapsed < settings.otp_resend_interval_seconds:
            wait = int(settings.otp_resend_interval_seconds - elapsed) + 1
            raise OtpRateLimited(
                f"A code was just sent. Wait {wait} seconds before asking for another.",
                wait,
            )

    window_start = now - timedelta(hours=1)
    recent = db.scalar(
        select(func.count())
        .select_from(OtpChallenge)
        .where(
            OtpChallenge.destination == destination,
            OtpChallenge.created_at >= window_start,
        )
    ) or 0
    if recent >= settings.otp_max_sends_per_hour:
        raise OtpRateLimited(
            "Too many codes have been requested for this number or address. Try again in an hour.",
            3600,
        )


def start_challenge(
    db: Session,
    *,
    channel: OtpChannel,
    purpose: OtpPurpose,
    destination: str,
    user_id: UUID | None = None,
) -> IssuedChallenge:
    """Issue a code, retiring any earlier one for the same destination.

    Only the newest code works. Leaving older ones live would multiply the
    guessable space by however many times the person pressed resend.
    """
    settings = get_settings()
    _enforce_send_rate(db, destination)

    now = _now()
    db.execute(
        OtpChallenge.__table__.update()
        .where(
            OtpChallenge.destination == destination,
            OtpChallenge.consumed_at.is_(None),
            OtpChallenge.expires_at > now,
        )
        .values(expires_at=now)
    )

    challenge_id = uuid4()
    code = _generate_code(settings.otp_code_length)
    challenge = OtpChallenge(
        id=challenge_id,
        user_id=user_id,
        purpose=purpose.value,
        channel=channel.value,
        destination=destination,
        code_hash=_hash_code(challenge_id, code),
        attempts_remaining=settings.otp_max_attempts,
        expires_at=now + timedelta(seconds=settings.otp_ttl_seconds),
    )
    db.add(challenge)
    db.commit()
    db.refresh(challenge)
    return IssuedChallenge(challenge=challenge, code=code)


def verify_challenge(
    db: Session,
    *,
    challenge_id: UUID,
    code: str,
    purpose: OtpPurpose,
    user_id: UUID | None = None,
) -> OtpChallenge:
    """Consume a code, or explain why it will not be consumed.

    A challenge issued for one account cannot be completed by another, and a
    link challenge cannot be redeemed as a sign-in: the purpose and the owner
    are both re-checked here, not just at the route that created it.
    """
    challenge = db.get(OtpChallenge, challenge_id)
    if (
        challenge is None
        or challenge.purpose != purpose.value
        or challenge.user_id != user_id
    ):
        raise OtpError("That code is no longer valid. Ask for a new one.")
    if challenge.consumed_at is not None:
        raise OtpError("That code has already been used. Ask for a new one.")
    if as_utc(challenge.expires_at) <= _now():
        raise OtpError("That code has expired. Ask for a new one.")
    if challenge.attempts_remaining <= 0:
        raise OtpError("Too many incorrect attempts. Ask for a new code.")

    submitted = (code or "").strip()
    if not hmac.compare_digest(challenge.code_hash, _hash_code(challenge.id, submitted)):
        challenge.attempts_remaining -= 1
        db.commit()
        if challenge.attempts_remaining <= 0:
            raise OtpError("That code is incorrect, and there are no attempts left. Ask for a new code.")
        remaining = challenge.attempts_remaining
        plural = "attempt" if remaining == 1 else "attempts"
        raise OtpError(f"That code is incorrect. {remaining} {plural} left.")

    challenge.consumed_at = _now()
    db.flush()
    return challenge
