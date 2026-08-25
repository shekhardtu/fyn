"""Privacy-bounded contact lookup by a verified sign-in identifier.

Partial lookup is intentionally restricted to people who already share a
record with the caller. A complete email address or phone number may resolve an
exact Fyn account. This gives a useful typeahead without turning authentication
identities into an enumerable public directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from ..config import get_settings
from ..domain import IdentityProvider, OtpChannel
from ..models import SharedRecordParticipant, User, UserIdentity
from .identity import IdentityError, PHONE_TRIM_PATTERN, normalize_channel_value


ContactChannel = Literal["email", "phone"]


@dataclass(frozen=True)
class ContactSuggestion:
    channel: ContactChannel
    identifier: str
    display_name: str
    match_kind: Literal["exact", "previously_shared"]


def _search_prefix(channel: ContactChannel, raw_query: str) -> str:
    query = (raw_query or "").strip().casefold()
    if sum(character.isalnum() for character in query) < 3:
        raise IdentityError("Enter at least three letters or digits to search.")
    if channel == "email":
        if any(character.isspace() for character in query):
            raise IdentityError("Enter an email address without spaces.")
        return query

    cleaned = PHONE_TRIM_PATTERN.sub("", query)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not cleaned.startswith("+"):
        cleaned = f"{get_settings().default_phone_prefix}{cleaned.lstrip('0')}"
    if not cleaned[1:].isdigit():
        raise IdentityError("Enter a phone number using digits only, with or without a country code.")
    return cleaned


def _exact_identifier(channel: ContactChannel, raw_query: str) -> str | None:
    otp_channel = OtpChannel.EMAIL if channel == "email" else OtpChannel.PHONE
    try:
        return normalize_channel_value(otp_channel, raw_query).key
    except IdentityError:
        return None


def search_contacts(
    db: Session,
    *,
    user_id: UUID,
    channel: ContactChannel,
    query: str,
    limit: int = 6,
) -> list[ContactSuggestion]:
    """Return exact matches plus prefix matches from prior relationships."""
    prefix = _search_prefix(channel, query)
    exact_identifier = _exact_identifier(channel, query)
    provider = IdentityProvider.EMAIL.value if channel == "email" else IdentityProvider.PHONE.value

    mine = aliased(SharedRecordParticipant)
    other = aliased(SharedRecordParticipant)
    known_user_ids = set(db.scalars(
        select(other.member_user_id)
        .join(mine, mine.shared_record_id == other.shared_record_id)
        .where(
            mine.member_user_id == user_id,
            mine.hidden_at.is_(None),
            other.member_user_id.is_not(None),
            other.member_user_id != user_id,
            other.hidden_at.is_(None),
        )
        .distinct()
    ))

    candidates: dict[UUID, ContactSuggestion] = {}
    if known_user_ids:
        rows = db.execute(
            select(UserIdentity, User)
            .join(User, User.id == UserIdentity.user_id)
            .where(
                UserIdentity.provider == provider,
                UserIdentity.user_id.in_(known_user_ids),
                UserIdentity.identifier.startswith(prefix, autoescape=True),
            )
            .order_by(User.display_name, UserIdentity.identifier)
            .limit(limit)
        )
        for identity, user in rows:
            candidates[user.id] = ContactSuggestion(
                channel=channel,
                identifier=identity.identifier,
                display_name=user.display_name,
                match_kind="previously_shared",
            )

    if exact_identifier is not None:
        exact = db.execute(
            select(UserIdentity, User)
            .join(User, User.id == UserIdentity.user_id)
            .where(
                UserIdentity.provider == provider,
                UserIdentity.identifier == exact_identifier,
                UserIdentity.user_id != user_id,
            )
        ).one_or_none()
        if exact is not None:
            identity, user = exact
            candidates[user.id] = ContactSuggestion(
                channel=channel,
                identifier=identity.identifier,
                display_name=user.display_name,
                match_kind="exact",
            )

    return sorted(
        candidates.values(),
        key=lambda item: (item.match_kind != "exact", item.display_name.casefold(), item.identifier),
    )[:limit]
