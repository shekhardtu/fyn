"""Conversation attachment lifecycle, authorization, and immutable message binding."""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import ATTACHMENTS_PER_MESSAGE as MAX_ATTACHMENTS
from ..event_time import as_utc, now_utc
from ..models import Conversation, ConversationAttachment, Message
from .file_cleanup import schedule_deletion
from .file_uploads import clean_filename


class AttachmentError(ValueError):
    pass


def owned_thread(db: Session, user_id: UUID, conversation_id: UUID, *, lock: bool = False) -> Conversation:
    query = select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user_id)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise AttachmentError("Conversation not found.")
    return row


def owned_attachment(db: Session, user_id: UUID, conversation_id: UUID, attachment_id: UUID, *, lock: bool = False) -> ConversationAttachment:
    query = select(ConversationAttachment).where(
        ConversationAttachment.id == attachment_id, ConversationAttachment.user_id == user_id,
        ConversationAttachment.conversation_id == conversation_id,
    )
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise AttachmentError("Attachment not found in this conversation.")
    return row


def reserve_upload(db: Session, user_id: UUID, conversation_id: UUID, filename: str, size: int) -> ConversationAttachment:
    owned_thread(db, user_id, conversation_id, lock=True)
    count_in_thread = db.scalar(select(func.count()).select_from(ConversationAttachment).where(
        ConversationAttachment.conversation_id == conversation_id,
    )) or 0
    if count_in_thread >= 100:
        raise AttachmentError("This conversation has reached its 100-file limit. Start a new conversation.")
    identifier = uuid4()
    key = f"chat/originals/{user_id}/{conversation_id}/{identifier}"
    row = ConversationAttachment(id=identifier, user_id=user_id, conversation_id=conversation_id,
        filename=clean_filename(filename), byte_size=size, storage_key=key, status="uploading",
        expires_at=now_utc() + timedelta(hours=24))
    db.add(row)
    # Commit cleanup before any external write. A crash/rollback after R2 accepts
    # bytes cannot strand an object; the cleaner retains live completed files.
    schedule_deletion(db, key, available_at=now_utc() + timedelta(hours=25))
    db.flush()
    return row


def bind_to_message(db: Session, user_id: UUID, conversation_id: UUID, ids: list[str], text: str) -> Message:
    if len(ids) > MAX_ATTACHMENTS or len(set(ids)) != len(ids):
        raise AttachmentError("Attach up to five different files.")
    rows = [owned_attachment(db, user_id, conversation_id, UUID(value), lock=True) for value in ids]
    if any(row.status != "ready" or row.message_id is not None for row in rows):
        raise AttachmentError("An attachment is still uploading or already belongs to a message.")
    if any(row.expires_at and as_utc(row.expires_at) <= now_utc() for row in rows):
        raise AttachmentError("An attachment expired. Attach the file again.")
    message = Message(conversation_id=conversation_id, role="user", content=text, widgets=[], citations=[])
    db.add(message)
    db.flush()
    for row in rows:
        row.message_id = message.id
        row.expires_at = None
    db.flush()
    return message


def remove_attachments(db: Session, rows: list[ConversationAttachment]) -> None:
    for row in rows:
        # A legacy signed staging URL may still be usable during an upgrade.
        delay = now_utc() + timedelta(minutes=10) if row.storage_key.startswith("chat/uploads/") else None
        schedule_deletion(db, row.storage_key, available_at=delay)
        db.delete(row)
    db.flush()
