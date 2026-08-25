"""Reusable collaboration, invitation, activity, and command primitives.

This module deliberately knows nothing about loans. A product aggregate owns
its financial rules and composes these primitives around one SharedRecord.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain import IdentityProvider, OtpChannel
from ..event_time import as_utc, now_utc
from ..models import (
    CommandReceipt,
    NotificationOutbox,
    SharedRecord,
    SharedRecordEvent,
    SharedRecordInvitation,
    SharedRecordParticipant,
    User,
    UserIdentity,
)
from .identity import mask, normalize_channel_value


class SharedRecordError(ValueError):
    """A collaboration command that cannot be applied as requested."""


class SharedRecordConflict(SharedRecordError):
    """A valid command based on stale or conflicting shared state."""


class SharedRecordNotFound(SharedRecordError):
    """A missing record or one the caller is not allowed to know exists."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _fernet() -> Fernet:
    material = hashlib.sha256(get_settings().auth_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(material))


def encrypt_destination(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_destination(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as error:
        raise SharedRecordError("The saved delivery address can no longer be read.") from error


def destination_hash(channel: OtpChannel, value: str) -> str:
    secret = get_settings().auth_secret.encode()
    return hmac.new(secret, f"shared-record:{channel.value}:{value}".encode(), hashlib.sha256).hexdigest()


def normalize_destination(channel: OtpChannel, raw: str) -> tuple[str, str, str]:
    normalized = normalize_channel_value(channel, raw)
    return normalized.key, destination_hash(channel, normalized.key), mask(channel, normalized.display)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_invitation(
    db: Session,
    *,
    record: SharedRecord,
    participant: SharedRecordParticipant,
    channel: OtpChannel,
    raw_destination: str,
    ttl_days: int = 14,
) -> tuple[SharedRecordInvitation, str]:
    normalized, hashed, masked = normalize_destination(channel, raw_destination)
    raw_token = secrets.token_urlsafe(32)
    invitation = SharedRecordInvitation(
        shared_record_id=record.id,
        participant_id=participant.id,
        channel=channel.value,
        destination_hash=hashed,
        destination_ciphertext=encrypt_destination(normalized),
        destination_masked=masked,
        token_hash=token_hash(raw_token),
        expires_at=now_utc() + timedelta(days=ttl_days),
        send_count=1,
        last_sent_at=now_utc(),
    )
    db.add(invitation)
    db.flush()
    return invitation, raw_token


def participant_for_user(
    db: Session,
    shared_record_id: UUID,
    user_id: UUID,
    *,
    lock: bool = False,
) -> SharedRecordParticipant:
    statement = select(SharedRecordParticipant).where(
        SharedRecordParticipant.shared_record_id == shared_record_id,
        SharedRecordParticipant.member_user_id == user_id,
        SharedRecordParticipant.hidden_at.is_(None),
    )
    if lock:
        statement = statement.with_for_update()
    participant = db.scalar(statement)
    if participant is None:
        raise SharedRecordNotFound("Shared record not found")
    return participant


def record_for_user(
    db: Session,
    shared_record_id: UUID,
    user_id: UUID,
    *,
    lock: bool = False,
) -> tuple[SharedRecord, SharedRecordParticipant]:
    participant = participant_for_user(db, shared_record_id, user_id, lock=lock)
    statement = select(SharedRecord).where(SharedRecord.id == shared_record_id)
    if lock:
        statement = statement.with_for_update()
    record = db.scalar(statement)
    if record is None:
        raise SharedRecordNotFound("Shared record not found")
    return record, participant


def other_participant(db: Session, shared_record_id: UUID, participant_id: UUID) -> SharedRecordParticipant:
    participant = db.scalar(
        select(SharedRecordParticipant).where(
            SharedRecordParticipant.shared_record_id == shared_record_id,
            SharedRecordParticipant.id != participant_id,
        )
    )
    if participant is None:
        raise SharedRecordError("This shared record has no counterparty.")
    return participant


def append_event(
    db: Session,
    record: SharedRecord,
    event_type: str,
    *,
    actor_participant_id: UUID | None,
    payload: dict[str, Any] | None = None,
) -> SharedRecordEvent:
    previous = db.scalar(
        select(SharedRecordEvent)
        .where(SharedRecordEvent.shared_record_id == record.id)
        .order_by(SharedRecordEvent.sequence.desc())
        .limit(1)
        .with_for_update()
    )
    sequence = (previous.sequence if previous else 0) + 1
    body = payload or {}
    digest = payload_hash({
        "record": str(record.id),
        "sequence": sequence,
        "type": event_type,
        "actor": str(actor_participant_id) if actor_participant_id else None,
        "payload": body,
        "previous": previous.event_hash if previous else None,
    })
    event = SharedRecordEvent(
        shared_record_id=record.id,
        sequence=sequence,
        event_type=event_type,
        actor_participant_id=actor_participant_id,
        payload=body,
        previous_hash=previous.event_hash if previous else None,
        event_hash=digest,
    )
    db.add(event)
    db.flush()
    return event


def queue_notification(
    db: Session,
    *,
    record: SharedRecord,
    recipient: SharedRecordParticipant,
    topic: str,
    channel: OtpChannel,
    destination: str,
    payload: dict[str, Any],
    dedupe_key: str,
    secret_context: str | None = None,
) -> NotificationOutbox:
    normalized, _hashed, masked = normalize_destination(channel, destination)
    existing = db.scalar(select(NotificationOutbox).where(NotificationOutbox.dedupe_key == dedupe_key))
    if existing is not None:
        return existing
    item = NotificationOutbox(
        shared_record_id=record.id,
        recipient_participant_id=recipient.id,
        topic=topic,
        channel=channel.value,
        destination_ciphertext=encrypt_destination(normalized),
        destination_masked=masked,
        context_ciphertext=encrypt_destination(secret_context) if secret_context else None,
        payload=payload,
        state="pending",
        dedupe_key=dedupe_key,
        available_at=now_utc(),
    )
    db.add(item)
    db.flush()
    return item


def begin_command(
    db: Session,
    *,
    actor_user_id: UUID,
    command_type: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
) -> CommandReceipt | None:
    receipt = db.scalar(
        select(CommandReceipt).where(
            CommandReceipt.actor_user_id == actor_user_id,
            CommandReceipt.command_type == command_type,
            CommandReceipt.idempotency_key == idempotency_key,
        )
    )
    if receipt is None:
        return None
    if receipt.request_hash != payload_hash(request_payload):
        raise SharedRecordConflict("That idempotency key was already used for a different request.")
    return receipt


def finish_command(
    db: Session,
    *,
    record: SharedRecord,
    actor_user_id: UUID,
    command_type: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
) -> CommandReceipt:
    receipt = CommandReceipt(
        shared_record_id=record.id,
        actor_user_id=actor_user_id,
        command_type=command_type,
        idempotency_key=idempotency_key,
        request_hash=payload_hash(request_payload),
        response_payload=response_payload,
    )
    db.add(receipt)
    db.flush()
    return receipt


def invitation_for_token(db: Session, raw_token: str, *, lock: bool = False) -> SharedRecordInvitation | None:
    statement = select(SharedRecordInvitation).where(SharedRecordInvitation.token_hash == token_hash(raw_token))
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def user_controls_invitation_destination(db: Session, user: User, invitation: SharedRecordInvitation) -> bool:
    channel = OtpChannel(invitation.channel)
    provider = IdentityProvider.PHONE if channel is OtpChannel.PHONE else IdentityProvider.EMAIL
    identities = list(db.scalars(select(UserIdentity).where(
        UserIdentity.user_id == user.id,
        UserIdentity.provider == provider.value,
    )))
    return any(destination_hash(channel, identity.identifier) == invitation.destination_hash for identity in identities)


def redeem_invitation(
    db: Session,
    *,
    raw_token: str,
    user: User,
) -> tuple[SharedRecordInvitation, SharedRecordParticipant, SharedRecord]:
    invitation = invitation_for_token(db, raw_token, lock=True)
    if (
        invitation is None
        or invitation.revoked_at is not None
        or as_utc(invitation.expires_at) <= now_utc()
    ):
        raise SharedRecordNotFound("That invitation is no longer available.")
    participant = db.scalar(
        select(SharedRecordParticipant)
        .where(SharedRecordParticipant.id == invitation.participant_id)
        .with_for_update()
    )
    record = db.scalar(select(SharedRecord).where(SharedRecord.id == invitation.shared_record_id).with_for_update())
    if participant is None or record is None:
        raise SharedRecordNotFound("That invitation is no longer available.")
    if invitation.redeemed_at is not None:
        if participant.member_user_id == user.id:
            return invitation, participant, record
        raise SharedRecordNotFound("That invitation is no longer available.")
    if not user_controls_invitation_destination(db, user, invitation):
        raise SharedRecordError("Sign in with the phone number or email address that received this invitation.")
    conflict = db.scalar(select(SharedRecordParticipant).where(
        SharedRecordParticipant.shared_record_id == record.id,
        SharedRecordParticipant.member_user_id == user.id,
        SharedRecordParticipant.id != participant.id,
    ))
    if conflict is not None:
        raise SharedRecordConflict("This account is already a participant in that record.")
    participant.member_user_id = user.id
    participant.state = "claimed"
    participant.verification_channel = invitation.channel
    participant.verification_claim = f"{invitation.channel}_control"
    participant.claimed_at = now_utc()
    invitation.redeemed_at = now_utc()
    record.row_version += 1
    append_event(
        db,
        record,
        "participant.claimed",
        actor_participant_id=participant.id,
        payload={"role": participant.role, "channel": invitation.channel},
    )
    return invitation, participant, record
