from __future__ import annotations

import asyncio
import io
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api_files import router
from app.config import FILE_UPLOAD_MAX_BYTES, Settings, get_settings
from app.database import get_db
from app.event_time import now_utc
from app.models import Conversation, ConversationAttachment, DocumentAsset, ObjectDeletion, User
from app.security import current_user
from app.services import files
from app.services.file_cleanup import cleanup_files
from app.services.file_uploads import clean_filename, read_upload
from file_helpers import upload_file


@pytest.fixture()
def api(db, tmp_path, file_store):
    user = db.scalar(select(User))
    conversation = Conversation(user_id=user.id, title="File lifecycle")
    db.add(conversation)
    db.commit()
    settings = Settings(document_storage_provider="local", document_storage_path=str(tmp_path))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[get_settings] = lambda: settings
    return SimpleNamespace(db=db, user=user, thread=conversation, client=TestClient(app), settings=settings, store=file_store)


def reserve(api, size=4, purpose="conversation", filename=None):
    response = api.client.post("/files", json={
        "purpose": purpose, "conversation_id": str(api.thread.id) if purpose == "conversation" else None,
        "filename": filename or ("file.txt" if purpose == "conversation" else "file.pdf"), "byte_size": size,
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_upload_requires_authentication_before_consuming_content(api):
    identifier = reserve(api)
    api.client.app.dependency_overrides.pop(current_user)
    result = api.client.post(f"/files/{identifier}/content", content=b"data", headers={"Content-Type": "application/octet-stream"})
    assert result.status_code == 401
    assert api.store.objects == {}


@pytest.mark.parametrize("content,headers,status", [
    (b"data", {"Content-Type": "text/plain"}, 415),
    (b"data", {"Content-Type": "application/octet-stream", "Content-Length": str(FILE_UPLOAD_MAX_BYTES + 1)}, 413),
    (b"dat", {"Content-Type": "application/octet-stream"}, 422),
    (b"data!", {"Content-Type": "application/octet-stream"}, 422),
    (b"", {"Content-Type": "application/octet-stream"}, 422),
])
def test_invalid_bytes_never_become_ready(api, content, headers, status):
    identifier = reserve(api)
    result = api.client.post(f"/files/{identifier}/content", content=content, headers=headers)
    assert result.status_code == status, result.text
    assert api.client.get(f"/files/{identifier}").json()["status"] == "uploading"
    assert api.store.objects == {}


def test_actual_stream_is_bounded_without_content_length(api):
    identifier = reserve(api, FILE_UPLOAD_MAX_BYTES)
    async def send():
        async def body():
            for _ in range(11):
                yield b"x" * (1024 * 1024)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.client.app), base_url="http://test") as client:
            return await client.post(f"/files/{identifier}/content", content=body(), headers={"Content-Type": "application/octet-stream"})
    result = asyncio.run(send())
    assert result.status_code == 413
    assert api.store.objects == {}
    # The admission slot is released after failure.
    other = reserve(api)
    assert api.client.post(f"/files/{other}/content", content=b"data", headers={"Content-Type": "application/octet-stream"}).status_code == 200


def test_exact_ten_mb_file_is_accepted(api):
    result = upload_file(api.client, "data.bin", b"x" * FILE_UPLOAD_MAX_BYTES, conversation_id=api.thread.id)
    assert result.status_code == 200, result.text
    assert result.json()["byte_size"] == FILE_UPLOAD_MAX_BYTES


def test_foreign_owner_and_foreign_thread_do_not_expose_files(api):
    result = upload_file(api.client, "note.txt", b"data", conversation_id=api.thread.id)
    identifier = result.json()["id"]
    other = User(email="file-outsider@example.test", display_name="Outsider")
    api.db.add(other)
    api.db.commit()
    api.client.app.dependency_overrides[current_user] = lambda: other
    for suffix in ("", "/content"):
        assert api.client.get(f"/files/{identifier}{suffix}").status_code == 404
    assert api.client.delete(f"/files/{identifier}").status_code == 404
    assert api.client.get("/files", params={"purpose": "conversation", "conversation_id": str(api.thread.id)}).status_code == 404


def test_failed_completion_keeps_durable_cleanup_intent(api, monkeypatch):
    identifier = reserve(api)
    row = api.db.get(ConversationAttachment, UUID(identifier))
    key = row.storage_key
    original_flush = api.db.flush
    def fail_flush(*args, **kwargs):
        raise RuntimeError("Database unavailable after object write")
    with monkeypatch.context() as patch:
        patch.setattr(api.db, "flush", fail_flush)
        with pytest.raises(RuntimeError, match="Database unavailable"):
            files.store_upload(api.db, row, io.BytesIO(b"data"), api.settings)
    api.db.rollback()
    assert api.store.objects[key] == b"data"
    assert api.db.get(ConversationAttachment, UUID(identifier)).status == "uploading"
    deletion = api.db.scalar(select(ObjectDeletion).where(ObjectDeletion.storage_key == key))
    assert deletion is not None
    deletion.available_at = now_utc() - timedelta(seconds=1)
    api.db.commit()
    assert cleanup_files(api.db, api.settings) == 1
    assert key not in api.store.objects
    assert api.db.flush == original_flush


def test_local_document_deletion_is_atomic_and_retried_from_outbox(api):
    uploaded = upload_file(api.client, "note.pdf", b"%PDF-1.4\nDocument")
    assert uploaded.status_code == 200, uploaded.text
    row = api.db.get(DocumentAsset, UUID(uploaded.json()["id"]))
    from pathlib import Path
    original = Path(api.settings.document_storage_path) / row.storage_key
    assert original.is_file()
    files.remove(api.db, row, api.user, api.settings)
    api.db.rollback()
    assert original.is_file()
    assert api.client.get(f"/files/{row.id}/content").status_code == 200
    assert api.client.delete(f"/files/{row.id}").status_code == 204
    assert original.is_file()  # The request commits revocation before deleting bytes.
    cleanup_files(api.db, api.settings)
    assert not original.exists()


def test_cleanup_retains_live_documents_and_expires_abandoned_uploads(api):
    good = upload_file(api.client, "note.pdf", b"%PDF-1.4\nDocument").json()
    abandoned = reserve(api, purpose="document")
    pending = api.db.get(DocumentAsset, UUID(abandoned))
    pending.created_at = now_utc() - timedelta(hours=26)
    for deletion in api.db.scalars(select(ObjectDeletion)):
        deletion.available_at = now_utc() - timedelta(seconds=1)
    api.db.commit()
    cleanup_files(api.db, api.settings)
    assert api.db.get(DocumentAsset, UUID(abandoned)) is None
    assert api.client.get(f"/files/{good['id']}/content").status_code == 200
    assert api.db.scalar(select(ObjectDeletion)) is None


def test_account_deletion_schedules_private_library_bytes(api, monkeypatch):
    from app.services.user_data import delete_user_data
    monkeypatch.setattr("app.services.user_data.clear_user_memories", lambda _id: 0)
    monkeypatch.setattr("app.config.get_settings", lambda: api.settings)
    uploaded = upload_file(api.client, "note.pdf", b"%PDF-1.4\nDocument")
    row = api.db.get(DocumentAsset, UUID(uploaded.json()["id"]))
    key = row.storage_key
    delete_user_data(api.db, api.user)
    pending = api.db.scalar(select(ObjectDeletion).where(ObjectDeletion.storage_key == key))
    assert pending is not None and pending.storage_provider == "local"
    cleanup_files(api.db, api.settings)
    from pathlib import Path
    assert not (Path(api.settings.document_storage_path) / key).exists()


def test_shared_filename_policy_and_bounded_reader_preserve_original_bytes():
    assert clean_filename("C:\\fakepath\\मेरा statement.csv\r\n") == "मेरा statement.csv"
    assert read_upload(io.BytesIO(b"original"))[0] == b"original"
    with pytest.raises(ValueError, match="larger"):
        read_upload(io.BytesIO(b"too long"), limit=4)
