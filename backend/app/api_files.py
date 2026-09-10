"""The application's only file transport: browser → authenticated API → storage."""
from __future__ import annotations

import tempfile
import threading
from contextlib import contextmanager
from typing import Literal
from urllib.parse import quote
from uuid import UUID

import anyio
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import Select, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .config import FILE_UPLOAD_MAX_BYTES, Settings, get_settings
from .database import get_db
from .models import ConversationAttachment, DocumentAsset, User
from .schemas import FileCreateIn, FileOut
from .security import current_user
from .services import attachments, files
from .services.object_storage import ObjectStorageError
from .services.shared_records import SharedRecordConflict, SharedRecordNotFound

router = APIRouter(prefix="/files", tags=["files"])
_uploads = threading.BoundedSemaphore(4)


@contextmanager
def file_errors(db: Session):
    try:
        yield
    except SharedRecordNotFound as error:
        db.rollback()
        raise HTTPException(404, str(error)) from error
    except SharedRecordConflict as error:
        db.rollback()
        raise HTTPException(409, str(error)) from error
    except (ObjectStorageError, BotoCoreError, ClientError, OSError) as error:
        db.rollback()
        raise HTTPException(503, "File storage is temporarily unavailable. Try again.") from error
    except ValueError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error


@router.post("", response_model=FileOut, status_code=201)
def reserve_file(request: FileCreateIn, db: Session = Depends(get_db), user: User = Depends(current_user), settings: Settings = Depends(get_settings)):
    with file_errors(db):
        row = files.reserve(db, user, request, settings)
        result = files.describe(row)
        db.commit()
        return result


@router.get("", response_model=list[FileOut])
def list_files(purpose: Literal["conversation", "document"] = "document", conversation_id: UUID | None = None,
               limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0),
               db: Session = Depends(get_db), user: User = Depends(current_user), settings: Settings = Depends(get_settings)):
    with file_errors(db):
        query: Select
        if purpose == "conversation":
            if conversation_id is None:
                raise HTTPException(422, "Choose a conversation.")
            try:
                attachments.owned_thread(db, user.id, conversation_id)
            except attachments.AttachmentError as error:
                raise SharedRecordNotFound(str(error)) from error
            query = select(ConversationAttachment).where(ConversationAttachment.user_id == user.id,
                        ConversationAttachment.conversation_id == conversation_id).order_by(ConversationAttachment.created_at, ConversationAttachment.id)
        else:
            if conversation_id is not None:
                raise HTTPException(422, "Library files do not have a conversation filter.")
            query = select(DocumentAsset).where(DocumentAsset.owner_user_id == user.id,
                        DocumentAsset.document_id.is_(None), DocumentAsset.state == "clean").order_by(DocumentAsset.created_at.desc(), DocumentAsset.id.desc())
        return [files.describe(row) for row in db.scalars(query.limit(limit).offset(offset))]


@router.get("/{file_id}", response_model=FileOut)
def get_file(file_id: UUID, db: Session = Depends(get_db), user: User = Depends(current_user), settings: Settings = Depends(get_settings)):
    with file_errors(db):
        return files.describe(files.readable(db, user, file_id))


@router.post("/{file_id}/content", response_model=FileOut)
async def upload_content(file_id: UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user), settings: Settings = Depends(get_settings)):
    # Authenticate/authorize before consuming bytes. No multipart parser can
    # spool an unbounded request before application size checks get a chance.
    with file_errors(db):
        row = files.readable(db, user, file_id)
        files.require_owner(row, user)
        expected_size = row.byte_size
        db.rollback()  # Do not hold database locks while the network is slow.
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
            raise HTTPException(415, "Upload the original file bytes as application/octet-stream.")
        declared = request.headers.get("content-length")
        if declared is not None and int(declared) > FILE_UPLOAD_MAX_BYTES:
            raise HTTPException(413, "Choose a file up to 10 MB.")
        if not _uploads.acquire(blocking=False):
            raise HTTPException(429, "Uploads are busy. Try again shortly.", headers={"Retry-After": "5"})
        try:
            # Disk spooling plus a per-worker admission bound keeps concurrent
            # uploads from multiplying the API worker's memory footprint.
            with tempfile.TemporaryFile() as source:
                received = 0
                try:
                    with anyio.fail_after(120):
                        async for chunk in request.stream():
                            received += len(chunk)
                            if received > FILE_UPLOAD_MAX_BYTES:
                                raise HTTPException(413, "Choose a file up to 10 MB.")
                            if received > expected_size:
                                raise HTTPException(422, "The upload size does not match the reserved file.")
                            await run_in_threadpool(source.write, chunk)
                except ClientDisconnect as error:
                    raise HTTPException(400, "The upload was interrupted. Try again.") from error
                except TimeoutError as error:
                    raise HTTPException(408, "The upload timed out. Try again.") from error
                source.seek(0)

                def save() -> FileOut:
                    locked = files.readable(db, user, file_id, lock=True)
                    files.require_owner(locked, user)
                    files.store_upload(db, locked, source, settings)
                    result = files.describe(locked)
                    db.commit()
                    return result

                return await run_in_threadpool(save)
        finally:
            _uploads.release()


@router.get("/{file_id}/content")
def download_file(file_id: UUID, inline: bool = False, db: Session = Depends(get_db), user: User = Depends(current_user), settings: Settings = Depends(get_settings)):
    with file_errors(db):
        row = files.readable(db, user, file_id)
        metadata = files.describe(row)
        content = files.read_content(row, settings)
        disposition = "inline" if inline and row.media_type in {"application/pdf", "image/png", "image/jpeg", "image/webp"} else "attachment"
        return Response(content, media_type=row.media_type, headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(metadata.filename, safe='')}",
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        })


@router.delete("/{file_id}", status_code=204)
def delete_file(file_id: UUID, db: Session = Depends(get_db), user: User = Depends(current_user), settings: Settings = Depends(get_settings)):
    with file_errors(db):
        files.remove(db, files.readable(db, user, file_id, lock=True), user, settings)
        db.commit()
        return Response(status_code=204)
