"""Sign-in and account-linking decisions.

Two ways in — a one-time code, or a verified Google credential — and one rule
about what they may claim: an identifier belongs to a single account, and this
application never merges two accounts that already exist. Everything a caller
can do about a conflict is stated in the refusal itself.

Linking is how the two sign-in methods become one account: verify a phone from a
Google-created account, or an email from a phone-created account, and both then
resolve to the same user. A later Google sign-in whose address is already
verified here is adopted rather than turned into a second account.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from ..config import get_settings, unavailable_otp_channels
from ..domain import IDENTITY_CHANNELS, IdentityProvider, IdentitySource, OtpChannel, OtpPurpose
from ..models import User, UserIdentity
from .google_identity import GoogleAccount, verify_google_credential
from .identity import (
    IdentityConflict,
    assert_available,
    attach_identity,
    identity_of,
    identity_owner,
    mask,
    normalize_channel_value,
    normalize_email,
    record_sign_in,
    register_user,
)
from .otp import IssuedChallenge, OtpChannelUnavailable, start_challenge, verify_challenge
from .otp_delivery import deliver_code


@dataclass(frozen=True)
class SentCode:
    challenge_id: UUID
    channel: OtpChannel
    destination_masked: str
    expires_in_seconds: int
    resend_after_seconds: int
    # Present for local phone sign-in, or when OTP_DEBUG_ECHO is explicitly on.
    # Production startup refuses debug echo and never enters development mode.
    debug_code: str | None


def _identity_user(db: Session, identity: UserIdentity) -> User:
    user = db.get(User, identity.user_id)
    if user is None:
        raise RuntimeError("A sign-in identity references a missing user")
    return user


def _required_identity(
    db: Session,
    user: User,
    provider: IdentityProvider,
) -> UserIdentity:
    identity = identity_of(db, user.id, provider)
    if identity is None:
        raise RuntimeError(f"Registration did not create the {provider.value} identity")
    return identity


def _issued(issued: IssuedChallenge, channel: OtpChannel, destination: str) -> SentCode:
    settings = get_settings()
    development_phone = settings.environment == "development" and channel is OtpChannel.PHONE
    return SentCode(
        challenge_id=issued.challenge.id,
        channel=channel,
        destination_masked=mask(channel, destination),
        expires_in_seconds=settings.otp_ttl_seconds,
        resend_after_seconds=settings.otp_resend_interval_seconds,
        debug_code=issued.code if settings.otp_debug_echo or development_phone else None,
    )


def _send(db: Session, issued: IssuedChallenge, channel: OtpChannel, destination: str) -> SentCode:
    """Hand the code to its provider, releasing the challenge if that fails.

    An undelivered code should not hold the resend window shut for the next
    minute; the hourly ceiling still counts the attempt.
    """
    settings = get_settings()
    # A channel with no provider is refused here rather than at startup: the
    # deployment still serves every other channel and every other endpoint, and
    # the person who chose this one gets told why instead of meeting a dead API.
    # The challenge is released first so an undelivered code does not hold the
    # resend window shut, exactly as a provider failure would.
    unavailable = unavailable_otp_channels(settings)
    if settings.environment == "production" and channel.value in unavailable:
        issued.challenge.expires_at = issued.challenge.created_at
        db.commit()
        raise OtpChannelUnavailable(
            f"{channel.value.capitalize()} sign-in is not available on this server."
        )
    # Development phone authentication is an entirely local round trip: the
    # response carries the code and no SMS provider is called, even if local
    # credentials happen to be configured. Production keeps the real delivery
    # path and startup refuses response echo there.
    if not (settings.environment == "development" and channel is OtpChannel.PHONE):
        try:
            deliver_code(channel, destination, issued.code)
        except Exception:
            issued.challenge.expires_at = issued.challenge.created_at
            db.commit()
            raise
    return _issued(issued, channel, destination)


# ── Signing in ───────────────────────────────────────────────────────────────


def send_login_code(db: Session, channel: OtpChannel, raw_value: str) -> SentCode:
    """Send a sign-in code, revealing nothing about whether the account exists."""
    normalized = normalize_channel_value(channel, raw_value)
    issued = start_challenge(
        db,
        channel=channel,
        purpose=OtpPurpose.LOGIN,
        destination=normalized.key,
    )
    return _send(db, issued, channel, normalized.key)


def complete_login(db: Session, challenge_id: UUID, code: str) -> User:
    """Verify a sign-in code, creating the account if this is a first visit."""
    challenge = verify_challenge(
        db,
        challenge_id=challenge_id,
        code=code,
        purpose=OtpPurpose.LOGIN,
    )
    channel = OtpChannel(challenge.channel)
    provider = IDENTITY_CHANNELS[channel]
    identity = identity_owner(db, provider, challenge.destination)

    if identity is None:
        display = challenge.destination if provider is IdentityProvider.EMAIL else None
        user = register_user(
            db,
            provider=provider,
            identifier=challenge.destination,
            email=display,
        )
        identity = _required_identity(db, user, provider)
    else:
        user = _identity_user(db, identity)

    record_sign_in(db, identity)
    db.commit()
    return user


def sign_in_with_google(db: Session, credential: str) -> User:
    """Verify a Google credential and resolve it to exactly one account.

    The subject is the durable key. When it is unknown but the verified address
    already belongs to an account, that account adopts the Google method instead
    of a second one being created — this is the merge the profile page promises,
    arriving from the other direction.
    """
    account = verify_google_credential(credential)
    email = normalize_email(account.email)

    identity = identity_owner(db, IdentityProvider.GOOGLE, account.subject)
    if identity is not None:
        user = _identity_user(db, identity)
        _sync_google_email(db, user, account, email.key, email.display)
        record_sign_in(db, identity)
        db.commit()
        return user

    owner = identity_owner(db, IdentityProvider.EMAIL, email.key)
    if owner is not None:
        user = _identity_user(db, owner)
        identity = attach_identity(
            db,
            user,
            provider=IdentityProvider.GOOGLE,
            identifier=account.subject,
            email=email.display,
            source=IdentitySource.GOOGLE,
        )
        record_sign_in(db, identity)
        db.commit()
        return user

    user = register_user(
        db,
        provider=IdentityProvider.GOOGLE,
        identifier=account.subject,
        email=email.display,
        display_name=account.display_name,
        source=IdentitySource.GOOGLE,
    )
    attach_identity(
        db,
        user,
        provider=IdentityProvider.EMAIL,
        identifier=email.key,
        email=email.display,
        source=IdentitySource.GOOGLE,
    )
    identity = _required_identity(db, user, IdentityProvider.GOOGLE)
    record_sign_in(db, identity)
    db.commit()
    return user


def _sync_google_email(
    db: Session,
    user: User,
    account: GoogleAccount,
    key: str,
    display: str,
) -> None:
    """Follow a change of Google address, without overwriting a chosen one.

    An email the account verified for itself outranks whatever Google currently
    reports, and an address already held elsewhere is left where it is rather
    than failing an otherwise valid sign-in.
    """
    current = identity_of(db, user.id, IdentityProvider.EMAIL)
    if current is not None:
        if current.identifier == key or current.source != IdentitySource.GOOGLE.value:
            return
    existing = identity_owner(db, IdentityProvider.EMAIL, key)
    if existing is not None and existing.user_id != user.id:
        return
    attach_identity(
        db,
        user,
        provider=IdentityProvider.EMAIL,
        identifier=key,
        email=display,
        source=IdentitySource.GOOGLE,
    )


# ── Linking from the profile ─────────────────────────────────────────────────


def _assert_linkable(db: Session, user: User, channel: OtpChannel, key: str) -> None:
    provider = IDENTITY_CHANNELS[channel]
    current = identity_of(db, user.id, provider)
    if current is not None and current.identifier == key:
        subject = "phone number" if provider is IdentityProvider.PHONE else "email address"
        raise IdentityConflict(f"That {subject} is already on your account.")
    if (
        provider is IdentityProvider.EMAIL
        and current is not None
        and current.source == IdentitySource.GOOGLE.value
    ):
        raise IdentityConflict(
            "Your email address comes from your Google sign-in, so it's managed there. "
            "Change it in your Google account, or link a phone number here instead."
        )
    assert_available(db, provider, key, user_id=user.id)


def send_link_code(db: Session, user: User, channel: OtpChannel, raw_value: str) -> SentCode:
    """Send a code to an identifier this account wants to claim.

    The conflict is raised before the send, so a number that belongs to somebody
    else never receives an unexplained message.
    """
    normalized = normalize_channel_value(channel, raw_value)
    _assert_linkable(db, user, channel, normalized.key)
    issued = start_challenge(
        db,
        channel=channel,
        purpose=OtpPurpose.LINK,
        destination=normalized.key,
        user_id=user.id,
    )
    return _send(db, issued, channel, normalized.key)


def complete_link(db: Session, user: User, challenge_id: UUID, code: str) -> UserIdentity:
    """Attach a verified identifier to the signed-in account.

    Availability is checked again here: the gap between sending a code and
    entering it is long enough for another account to have claimed the same
    identifier in the meantime.
    """
    challenge = verify_challenge(
        db,
        challenge_id=challenge_id,
        code=code,
        purpose=OtpPurpose.LINK,
        user_id=user.id,
    )
    channel = OtpChannel(challenge.channel)
    _assert_linkable(db, user, channel, challenge.destination)

    provider = IDENTITY_CHANNELS[channel]
    display = challenge.destination if provider is IdentityProvider.EMAIL else None
    identity = attach_identity(
        db,
        user,
        provider=provider,
        identifier=challenge.destination,
        email=display,
    )
    db.commit()
    return identity
