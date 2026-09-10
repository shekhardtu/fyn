"""Application file resource. Domain records remain the authority for membership.

Chat originals and lending evidence keep their existing identities and retention
rules. This service owns their common reserve/write/read/delete lifecycle; domain
services own conversation admission and immutable shared-document revisions.
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO, Union
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..event_time import as_utc, now_utc
from ..models import ConversationAttachment, DocumentAsset, User
from ..schemas import FileCreateIn, FileOut
from . import attachments, document_assets
from .attachment_processing import process_file
from .file_cleanup import schedule_deletion
from .file_uploads import FileUploadError, clean_filename, read_upload
from .object_storage import R2ObjectStore
from .shared_records import SharedRecordConflict, SharedRecordNotFound

FileRecord = Union[ConversationAttachment, DocumentAsset]


def describe(row: FileRecord) -> FileOut:
    common: dict = dict(id=row.id, byte_size=row.byte_size, media_type=row.media_type,
                  sha256=row.sha256 or None, created_at=row.created_at, conversation_id=None,
                  message_id=None, read_mode="unavailable", read_error=None, content_metadata={},
                  classification=None, description=None, document_state=None)
    if isinstance(row, ConversationAttachment):
        common.update(purpose="conversation", filename=row.filename, status=row.status,
                      conversation_id=row.conversation_id, message_id=row.message_id,
                      read_mode=row.read_mode, read_error=row.read_error, content_metadata=row.content_metadata)
    else:
        common.update(purpose="document", filename=row.original_filename,
                      status="ready" if row.state in {"clean", "attached"} else "uploading",
                      classification=row.classification, description=row.description, document_state=row.state)
    return FileOut.model_validate(common)


def readable(db: Session, user: User, file_id: UUID, *, lock: bool = False) -> FileRecord:
    query = select(ConversationAttachment).where(ConversationAttachment.id == file_id)
    attachment = db.scalar(query.with_for_update() if lock else query)
    if attachment is not None:
        if attachment.user_id != user.id:
            raise SharedRecordNotFound("File not found.")
        return attachment
    if lock:
        db.scalar(select(DocumentAsset).where(DocumentAsset.id == file_id).with_for_update())
    return document_assets.readable_asset(db, file_id, user)


def require_owner(row: FileRecord, user: User) -> None:
    owner = row.user_id if isinstance(row, ConversationAttachment) else row.owner_user_id
    if owner != user.id:
        raise SharedRecordNotFound("File not found.")


def reserve(db: Session, user: User, request: FileCreateIn, settings: Settings) -> FileRecord:
    # Serialize both domains' reservations under the same owner lock.
    db.scalar(select(User.id).where(User.id == user.id).with_for_update())
    chat_bytes = db.scalar(select(func.sum(ConversationAttachment.byte_size)).where(ConversationAttachment.user_id == user.id)) or 0
    document_bytes = db.scalar(select(func.sum(DocumentAsset.byte_size)).where(DocumentAsset.owner_user_id == user.id)) or 0
    if chat_bytes + document_bytes + request.byte_size > 1024 * 1024 * 1024:
        raise FileUploadError("Your files have reached the 1 GB storage limit. Remove old files or conversations to free space.")
    chat_drafts = db.scalar(select(func.count()).select_from(ConversationAttachment).where(
        ConversationAttachment.user_id == user.id, ConversationAttachment.message_id.is_(None))) or 0
    document_uploads = db.scalar(select(func.count()).select_from(DocumentAsset).where(
        DocumentAsset.owner_user_id == user.id, DocumentAsset.state == "uploading")) or 0
    if chat_drafts + document_uploads >= 20:
        raise FileUploadError("Remove some unfinished uploads or unsent files before uploading more.")
    if request.purpose == "conversation":
        if request.conversation_id is None:
            raise FileUploadError("Choose a conversation for this attachment.")
        return attachments.reserve_upload(db, user.id, request.conversation_id, request.filename, request.byte_size)
    if request.conversation_id is not None:
        raise FileUploadError("A library document cannot belong to a conversation.")
    if request.classification not in document_assets.ALLOWED_CLASSIFICATIONS:
        raise FileUploadError("Choose a supported document category.")
    identifier = uuid4()
    filename = clean_filename(request.filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in document_assets.DOCUMENT_MEDIA_TYPES:
        raise FileUploadError("Upload a PDF, JPG, or PNG file.")
    key = f"drafts/{identifier.hex}{suffix}"
    row = DocumentAsset(id=identifier, owner_user_id=user.id, original_filename=filename,
                        byte_size=request.byte_size, media_type="application/octet-stream", sha256="",
                        storage_key=key, state="uploading", classification=request.classification,
                        description=(request.description or "").strip() or None)
    db.add(row)
    schedule_deletion(db, key, settings.document_storage_provider, available_at=now_utc() + timedelta(hours=25))
    db.flush()
    return row


def store_upload(db: Session, row: FileRecord, source: BinaryIO, settings: Settings) -> FileRecord:
    """The reservation and cleanup intent must be committed before entering here."""
    metadata = describe(row)
    if isinstance(row, ConversationAttachment):
        expired = row.expires_at is not None and as_utc(row.expires_at) <= now_utc()
    else:
        expired = row.state == "uploading" and as_utc(row.created_at) <= now_utc() - timedelta(hours=24)
    if expired:
        raise FileUploadError("This upload expired. Attach the file again.")
    content, digest = read_upload(source)
    if len(content) != row.byte_size:
        raise FileUploadError("The upload size does not match the reserved file. Attach the file again.")
    if metadata.status == "ready":
        if row.sha256 != digest:
            raise SharedRecordConflict("This file is already saved with different contents.")
        return row
    if isinstance(row, ConversationAttachment):
        validated = process_file(row.filename, content)
        media_type = validated.media_type
        # Existing unfinished reservations may still have a legacy staging key.
        row.storage_key = f"chat/originals/{row.user_id}/{row.conversation_id}/{row.id}"
    else:
        media_type = document_assets.validate_media_type(row.original_filename, content[:16])
    if isinstance(row, ConversationAttachment) or settings.document_storage_provider == "r2":
        R2ObjectStore(settings).write(row.storage_key, content, media_type)
    else:
        root = Path(settings.document_storage_path).resolve()
        target = root / row.storage_key
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix="upload-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as destination:
                destination.write(content)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    row.sha256, row.media_type = digest, media_type
    if isinstance(row, ConversationAttachment):
        row.status, row.read_mode = "ready", validated.read_mode
        row.read_error = validated.error[:300] if validated.error else None
        row.content_metadata = validated.metadata
    else:
        row.state, row.validated_at = "clean", now_utc()
    db.flush()
    return row


def read_content(row: FileRecord, settings: Settings) -> bytes:
    if describe(row).status != "ready":
        raise SharedRecordConflict("This file is still uploading.")
    if isinstance(row, DocumentAsset):
        return document_assets.read_asset_bytes(row, settings)
    return R2ObjectStore(settings).read(row.storage_key, row.byte_size, digest=row.sha256)


def remove(db: Session, row: FileRecord, user: User, settings: Settings) -> None:
    require_owner(row, user)
    if isinstance(row, ConversationAttachment):
        if row.message_id is not None:
            raise SharedRecordConflict("This file belongs to a sent message. Delete the conversation to remove it.")
        attachments.remove_attachments(db, [row])
        return
    if row.document_id is not None:
        raise SharedRecordConflict("This file is part of an immutable document revision.")
    schedule_deletion(db, row.storage_key, settings.document_storage_provider)
    db.delete(row)
    db.flush()
