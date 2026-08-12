"""Who an account belongs to, and the rules for claiming an identifier.

One phone number and one email address each belong to exactly one account. That
is enforced by a unique constraint rather than by a check-then-write, so two
simultaneous link attempts cannot both succeed; the constraint violation is
translated back into the same refusal the pre-check would have produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain import IdentityProvider, IdentitySource, OtpChannel
from ..models import User, UserIdentity
from ..seed import DEFAULT_USER_EMAIL
from .repositories import UserScopedRepository


# Google canonicalises its own addresses, so a user who types the dotted or
# plus-tagged form of their own Gmail must not land on a second account.
GOOGLE_MAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
E164_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")
PHONE_TRIM_PATTERN = re.compile(r"[\s\-().]")


class IdentityError(ValueError):
    """An identifier that cannot be accepted as written."""


class IdentityConflict(Exception):
    """An identifier that already belongs to somebody else."""


@dataclass(frozen=True)
class NormalizedIdentifier:
    """The stored key and the form shown back to the person who typed it."""

    key: str
    display: str


def normalize_phone(raw: str) -> NormalizedIdentifier:
    """Reduce a typed phone number to E.164.

    A bare national number is read against the configured default prefix, which
    is where this application's users are; anything else must arrive with its
    own country code.
    """
    cleaned = PHONE_TRIM_PATTERN.sub("", (raw or "").strip())
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not cleaned.startswith("+"):
        national = cleaned.lstrip("0")
        if not national.isdigit():
            raise IdentityError("Enter a phone number using digits only, with or without a country code.")
        cleaned = f"{get_settings().default_phone_prefix}{national}"
    if not E164_PATTERN.match(cleaned):
        raise IdentityError("That doesn't look like a valid phone number. Include the country code, like +91 98765 43210.")
    return NormalizedIdentifier(key=cleaned, display=cleaned)


def normalize_email(raw: str) -> NormalizedIdentifier:
    """Reduce a typed address to one uniqueness key, keeping the typed form.

    Case is never significant in a domain and is not significant in the mailbox
    of any provider this talks to. Dots and `+tags` are collapsed only for
    Google's own domains, where the provider itself treats them as one mailbox.
    """
    address = (raw or "").strip().lower()
    if not EMAIL_PATTERN.match(address):
        raise IdentityError("Enter a valid email address.")
    local, _, domain = address.partition("@")
    if domain in GOOGLE_MAIL_DOMAINS:
        local = local.split("+", 1)[0].replace(".", "")
        if not local:
            raise IdentityError("Enter a valid email address.")
        return NormalizedIdentifier(key=f"{local}@gmail.com", display=address)
    return NormalizedIdentifier(key=address, display=address)


def normalize_channel_value(channel: OtpChannel, raw: str) -> NormalizedIdentifier:
    if channel is OtpChannel.PHONE:
        return normalize_phone(raw)
    return normalize_email(raw)


def mask(channel: OtpChannel, value: str) -> str:
    """A confirmation of where a code went that is not itself a disclosure."""
    if channel is OtpChannel.PHONE:
        return f"{value[:3]}•••••{value[-3:]}" if len(value) > 6 else value
    local, _, domain = value.partition("@")
    head = local[0] if local else ""
    return f"{head}{'•' * max(len(local) - 1, 1)}@{domain}"


def identity_owner(db: Session, provider: IdentityProvider, identifier: str) -> UserIdentity | None:
    """The single row that reserves an identifier, across every account."""
    return db.scalar(
        select(UserIdentity).where(
            UserIdentity.provider == provider.value,
            UserIdentity.identifier == identifier,
        )
    )


def identities_of(db: Session, user_id: UUID) -> list[UserIdentity]:
    repository = UserScopedRepository(db, user_id)
    return list(db.scalars(
        repository.statement(UserIdentity).order_by(UserIdentity.created_at)
    ))


def identity_of(db: Session, user_id: UUID, provider: IdentityProvider) -> UserIdentity | None:
    repository = UserScopedRepository(db, user_id)
    return db.scalar(
        repository.statement(UserIdentity).where(UserIdentity.provider == provider.value)
    )


def owned_identity(db: Session, user_id: UUID, identity_id: UUID) -> UserIdentity | None:
    return UserScopedRepository(db, user_id).get(UserIdentity, identity_id)


def assert_available(db: Session, provider: IdentityProvider, identifier: str, *, user_id: UUID | None) -> None:
    """Refuse an identifier that is spoken for, before a code is ever sent.

    The message is deliberately explicit about the remedy: this application
    never merges two existing accounts, so the only way to move an identifier is
    to delete the account currently holding it.
    """
    existing = identity_owner(db, provider, identifier)
    if existing is None or existing.user_id == user_id:
        return
    subject = "phone number" if provider is IdentityProvider.PHONE else "email address"
    raise IdentityConflict(
        f"That {subject} is already linked to another account. "
        f"Sign in to that account and delete it before linking the {subject} here."
    )


def _mirror_contact_columns(db: Session, user: User) -> None:
    """Keep the denormalized columns on `users` in step with the identity rows."""
    email = identity_of(db, user.id, IdentityProvider.EMAIL)
    phone = identity_of(db, user.id, IdentityProvider.PHONE)
    user.email = email.email if email else None
    user.phone = phone.identifier if phone else None
    db.flush()


def attach_identity(
    db: Session,
    user: User,
    *,
    provider: IdentityProvider,
    identifier: str,
    email: str | None = None,
    source: IdentitySource = IdentitySource.OTP,
) -> UserIdentity:
    """Link an identifier to an account, replacing this account's previous one.

    Replacement is what "update my phone number" means here: an account holds at
    most one identifier per provider, so verifying a new one retires the old.
    """
    assert_available(db, provider, identifier, user_id=user.id)
    existing = identity_of(db, user.id, provider)
    if existing is not None:
        if existing.identifier == identifier:
            existing.email = email or existing.email
            existing.verified_at = datetime.now(timezone.utc)
            db.flush()
            _mirror_contact_columns(db, user)
            return existing
        db.delete(existing)
        db.flush()

    identity = UserIdentity(
        user_id=user.id,
        provider=provider.value,
        identifier=identifier,
        email=email,
        source=source.value,
        verified_at=datetime.now(timezone.utc),
    )
    db.add(identity)
    try:
        db.flush()
    except IntegrityError as error:
        # Lost the race against a concurrent link of the same identifier. The
        # unique constraint is the authority; report it as the same refusal.
        db.rollback()
        raise IdentityConflict(
            "That identifier was just linked to another account. Try a different one."
        ) from error
    _mirror_contact_columns(db, user)
    return identity


def detach_identity(db: Session, user: User, identity: UserIdentity) -> None:
    """Unlink a sign-in method, never the last one.

    Removing the only way back in would lock the account out of its own
    financial history, which no confirmation dialog can undo.
    """
    if len(identities_of(db, user.id)) <= 1:
        raise IdentityConflict(
            "This is the only way to sign in to your account, so it can't be removed. "
            "Add another sign-in method first."
        )
    db.delete(identity)
    db.flush()
    _mirror_contact_columns(db, user)


def claimable_seeded_user(db: Session) -> User | None:
    """The pre-authentication local account, if nobody has signed in yet.

    Before this feature the application served one seeded user. Handing that row
    to the first real sign-in keeps its conversations and transactions reachable
    instead of stranding them behind an identity nobody can present.
    """
    if not get_settings().claim_seeded_user_on_first_login:
        return None
    if db.scalar(select(func.count()).select_from(UserIdentity)):
        return None
    return db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))


def register_user(
    db: Session,
    *,
    provider: IdentityProvider,
    identifier: str,
    email: str | None = None,
    display_name: str | None = None,
    source: IdentitySource = IdentitySource.OTP,
) -> User:
    """Create the account behind a first successful verification."""
    user = claimable_seeded_user(db)
    if user is None:
        user = User(display_name=display_name or "You")
        db.add(user)
        db.flush()
    elif display_name:
        user.display_name = display_name
    attach_identity(db, user, provider=provider, identifier=identifier, email=email, source=source)
    return user


def record_sign_in(db: Session, identity: UserIdentity) -> None:
    identity.last_login_at = datetime.now(timezone.utc)
    db.flush()
