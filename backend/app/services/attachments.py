"""Conversation attachment lifecycle, authorization, and immutable message binding."""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import ATTACHMENT_UPLOAD_MAX_BYTES as MAX_FILE_BYTES, ATTACHMENTS_PER_MESSAGE as MAX_ATTACHMENTS, Settings
from ..event_time import as_utc, now_utc
from ..models import Conversation, ConversationAttachment, Message, ObjectDeletion, User
from .attachment_processing import process_file
from .object_storage import R2ObjectStore


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


def stage_upload(db: Session, user_id: UUID, conversation_id: UUID, filename: str, size: int, settings: Settings) -> tuple[ConversationAttachment, str]:
    # Serialize quota reservations across this user's threads and API workers.
    db.scalar(select(User.id).where(User.id == user_id).with_for_update())
    owned_thread(db, user_id, conversation_id, lock=True)
    if not 0 < size <= MAX_FILE_BYTES:
        raise AttachmentError("Choose a non-empty file up to 10 MB.")
    count = db.scalar(select(func.count()).select_from(ConversationAttachment).where(
        ConversationAttachment.user_id == user_id, ConversationAttachment.message_id.is_(None),
    )) or 0
    if count >= 20:
        raise AttachmentError("Remove some unsent attachments before uploading more.")
    bytes_used = db.scalar(select(func.sum(ConversationAttachment.byte_size)).where(ConversationAttachment.user_id == user_id)) or 0
    if bytes_used + size > 1024 * 1024 * 1024:
        raise AttachmentError("Your attachments have reached the 1 GB storage limit. Remove old conversations to free space.")
    count_in_thread = db.scalar(select(func.count()).select_from(ConversationAttachment).where(
        ConversationAttachment.conversation_id == conversation_id,
    )) or 0
    if count_in_thread >= 100:
        raise AttachmentError("This conversation has reached its 100-file limit. Start a new conversation.")
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename.replace("\\", "/").rsplit("/", 1)[-1]).strip()[:240] or "attachment"
    identifier = uuid4()
    key = f"chat/uploads/{user_id}/{conversation_id}/{identifier}"
    url = R2ObjectStore(settings).upload_url(key, size)
    row = ConversationAttachment(id=identifier, user_id=user_id, conversation_id=conversation_id,
        filename=filename, byte_size=size, storage_key=key, status="uploading",
        expires_at=now_utc() + timedelta(hours=24))
    db.add(row)
    # Signed staging URLs can be reused until expiry. Delete staging objects
    # after that boundary, even if the browser closes before completing upload.
    db.add(ObjectDeletion(storage_key=key, available_at=now_utc() + timedelta(minutes=10)))
    # Reap a sealed original if object write succeeds but database completion
    # rolls back. The cleaner retains any object referenced by a live row.
    db.add(ObjectDeletion(storage_key=f"chat/originals/{user_id}/{conversation_id}/{identifier}",
                          available_at=now_utc() + timedelta(hours=25)))
    db.flush()
    return row, url


def complete_upload(db: Session, row: ConversationAttachment, settings: Settings) -> ConversationAttachment:
    if row.status == "ready":
        return row
    if row.expires_at is None or as_utc(row.expires_at) <= now_utc():
        raise AttachmentError("This upload expired. Attach the file again.")
    store = R2ObjectStore(settings)
    content = store.read(row.storage_key, min(row.byte_size, MAX_FILE_BYTES))
    if len(content) != row.byte_size:
        raise AttachmentError("The upload is incomplete. Attach the file again.")
    validated = process_file(row.filename, content)
    sealed_key = f"chat/originals/{row.user_id}/{row.conversation_id}/{row.id}"
    # Seal the validated bytes at a key the browser can never write. Copying
    # a mutable staging key would allow a replacement between validation/copy.
    store.write(sealed_key, content, validated.media_type)
    row.storage_key = sealed_key
    row.sha256 = hashlib.sha256(content).hexdigest()
    row.status = "ready"
    row.media_type = validated.media_type
    row.read_mode = validated.read_mode
    row.read_error = validated.error[:300] if validated.error else None
    row.content_metadata = validated.metadata
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
        # Staging already has a delayed outbox entry. Immutable originals need
        # their own entry, committed atomically with removal of access metadata.
        if row.status == "ready":
            pending = db.scalar(select(ObjectDeletion).where(ObjectDeletion.storage_key == row.storage_key))
            if pending:
                pending.available_at = now_utc()
            else:
                db.add(ObjectDeletion(storage_key=row.storage_key))
        db.delete(row)
    db.flush()


def cleanup_attachments(db: Session, settings: Settings) -> int:
    """Bounded restart-safe cleanup, called by the application maintenance loop."""
    expired = list(db.scalars(select(ConversationAttachment).where(
        ConversationAttachment.message_id.is_(None), ConversationAttachment.expires_at <= now_utc(),
    ).limit(100).with_for_update(skip_locked=True)))
    remove_attachments(db, expired)
    db.commit()
    due = list(db.scalars(select(ObjectDeletion).where(ObjectDeletion.available_at <= now_utc())
                         .order_by(ObjectDeletion.available_at).limit(100).with_for_update(skip_locked=True)))
    if not due:
        return 0
    store = R2ObjectStore(settings)
    count = 0
    for item in due:
        try:
            referenced = db.scalar(select(ConversationAttachment.id).where(
                ConversationAttachment.storage_key == item.storage_key, ConversationAttachment.status == "ready",
            ))
            if referenced:
                db.delete(item)
                continue
            store.delete(item.storage_key)
            db.delete(item)
            count += 1
        except Exception:
            item.attempts += 1
            item.available_at = now_utc() + timedelta(seconds=min(3600, 30 * 2 ** min(item.attempts, 7)))
    db.commit()
    return count
