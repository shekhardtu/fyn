"""Authenticated control plane for direct-to-R2 conversation attachments."""
from __future__ import annotations

from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .security import current_user
from .config import get_settings
from .database import get_db
from .models import ConversationAttachment, User
from .schemas import AttachmentOut, AttachmentUploadIn, AttachmentUploadOut
from .schemas import ImportResultOut
from .services import attachments
from .services.object_storage import ObjectStorageError, R2ObjectStore

router = APIRouter(prefix="/conversations/{conversation_id}/attachments", tags=["attachments"])


def _owned(db: Session, user: User, conversation_id: UUID, attachment_id: UUID, *, lock: bool = False) -> ConversationAttachment:
    try:
        return attachments.owned_attachment(db, user.id, conversation_id, attachment_id, lock=lock)
    except attachments.AttachmentError as error:
        raise HTTPException(404, str(error)) from error


@router.post("", response_model=AttachmentUploadOut, status_code=201)
def initiate_upload(conversation_id: UUID, request: AttachmentUploadIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        row, url = attachments.stage_upload(db, user.id, conversation_id, request.filename, request.byte_size, get_settings())
        db.commit()
    except (ObjectStorageError, ClientError, BotoCoreError) as error:
        raise HTTPException(503, "Attachment storage is temporarily unavailable. Try again.") from error
    except attachments.AttachmentError as error:
        raise HTTPException(422, str(error)) from error
    return AttachmentUploadOut(attachment=AttachmentOut.model_validate(row), upload_url=url,
                               headers={"Content-Type": "application/octet-stream"}, expires_in=300)


@router.post("/{attachment_id}/complete", response_model=AttachmentOut)
def complete_upload(conversation_id: UUID, attachment_id: UUID, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = _owned(db, user, conversation_id, attachment_id, lock=True)
    try:
        attachments.complete_upload(db, row, get_settings())
        db.commit()
    except (ClientError, BotoCoreError) as error:
        raise HTTPException(503, "The uploaded file is not available yet. Retry the upload.") from error
    except (ValueError, ObjectStorageError) as error:
        raise HTTPException(422, str(error)) from error
    return AttachmentOut.model_validate(row)


@router.get("", response_model=list[AttachmentOut])
def list_attachments(conversation_id: UUID, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        attachments.owned_thread(db, user.id, conversation_id)
    except attachments.AttachmentError as error:
        raise HTTPException(404, str(error)) from error
    return list(db.scalars(select(ConversationAttachment).where(
        ConversationAttachment.user_id == user.id, ConversationAttachment.conversation_id == conversation_id,
    ).order_by(ConversationAttachment.created_at)))


@router.post("/{attachment_id}/import", response_model=ImportResultOut)
def preview_attachment_import(conversation_id: UUID, attachment_id: UUID, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from .services.import_previews import preview_csv_import
    from .services.preferences import user_preference
    preference = user_preference(db, user.id, "source:csv:revoked")
    if preference and preference.value.get("revoked") is True:
        raise HTTPException(403, "CSV access is revoked in privacy settings")
    # Match message admission's lock order: conversation, then attachment.
    try:
        conversation = attachments.owned_thread(db, user.id, conversation_id, lock=True)
    except attachments.AttachmentError as error:
        raise HTTPException(404, str(error)) from error
    row = _owned(db, user, conversation_id, attachment_id, lock=True)
    if row.status != "ready" or row.media_type != "text/csv":
        raise HTTPException(422, "Choose a saved CSV file to preview transactions.")
    try:
        content = R2ObjectStore(get_settings()).read(row.storage_key, row.byte_size, digest=row.sha256)
        return preview_csv_import(db, user, conversation, row.filename, content, attachment=row)
    except (ClientError, BotoCoreError) as error:
        raise HTTPException(503, "The saved file is temporarily unavailable.") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/{attachment_id}/content")
def download_attachment(conversation_id: UUID, attachment_id: UUID, inline: bool = False, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = _owned(db, user, conversation_id, attachment_id)
    if row.status != "ready":
        raise HTTPException(409, "This file is still uploading.")
    try:
        url = R2ObjectStore(get_settings()).download_url(row.storage_key, row.filename, row.media_type,
                inline=inline and row.media_type in {"image/png", "image/jpeg", "image/webp", "application/pdf"})
    except (ObjectStorageError, ClientError, BotoCoreError) as error:
        raise HTTPException(503, "The saved file is temporarily unavailable.") from error
    return RedirectResponse(url, status_code=307, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.delete("/{attachment_id}", status_code=204)
def remove_attachment(conversation_id: UUID, attachment_id: UUID, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = _owned(db, user, conversation_id, attachment_id, lock=True)
    if row.message_id is not None:
        raise HTTPException(409, "This file belongs to a sent message. Delete the conversation to remove it.")
    attachments.remove_attachments(db, [row])
    db.commit()
    return Response(status_code=204)
