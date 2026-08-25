"""Private file assets bound immutably to shared-document revisions."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterable
from uuid import UUID, uuid4

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..event_time import now_utc
from ..models import (
    DocumentAsset,
    DocumentRevision,
    DocumentRevisionAsset,
    SharedDocument,
    SharedRecordParticipant,
    User,
)
from .shared_records import SharedRecordConflict, SharedRecordError, SharedRecordNotFound, payload_hash, record_for_user


ALLOWED_CLASSIFICATIONS = {
    "external_agreement",
    "assurance_item",
    "transfer_receipt",
    "identity_evidence",
    "witness_statement",
    "supporting_evidence",
}
_TYPE_BY_SUFFIX = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._() -]+")


class DocumentAssetError(ValueError):
    pass


def _r2_client(settings: Settings):
    values = (settings.r2_account_id, settings.r2_bucket, settings.r2_access_key_id, settings.r2_secret_access_key)
    if not all(values):
        raise DocumentAssetError("Cloudflare R2 document storage is not configured.")
    import boto3  # type: ignore[import-untyped]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )


def _object_key(storage_key: str, settings: Settings) -> str:
    prefix = settings.r2_object_prefix.strip("/")
    return f"{prefix}/{storage_key}" if prefix else storage_key


def _clean_filename(raw: str | None) -> str:
    name = Path(raw or "document").name.strip()
    name = _SAFE_NAME.sub("_", name)[:240].strip(" .")
    return name or "document"


def _detected_media_type(filename: str, header: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    expected = _TYPE_BY_SUFFIX.get(suffix)
    if expected is None:
        raise DocumentAssetError("Upload a PDF, JPG, or PNG file.")
    detected = None
    if header.startswith(b"%PDF-"):
        detected = "application/pdf"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif header.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    if detected != expected:
        raise DocumentAssetError("The file contents do not match its filename. Choose the original PDF, JPG, or PNG.")
    return detected


def _copy_limited(source: BinaryIO, destination: BinaryIO, limit: int) -> tuple[int, str, bytes]:
    size = 0
    digest = hashlib.sha256()
    header = b""
    while True:
        chunk = source.read(min(1024 * 1024, limit + 1 - size))
        if not chunk:
            break
        if not header:
            header = chunk[:16]
        size += len(chunk)
        if size > limit:
            raise DocumentAssetError(f"That file is larger than the {limit // (1024 * 1024)} MB limit.")
        digest.update(chunk)
        destination.write(chunk)
    if size == 0:
        raise DocumentAssetError("The selected file is empty.")
    return size, digest.hexdigest(), header


def store_upload(
    db: Session,
    *,
    user: User,
    upload: UploadFile,
    classification: str,
    description: str | None,
    settings: Settings,
) -> DocumentAsset:
    if classification not in ALLOWED_CLASSIFICATIONS:
        raise DocumentAssetError("Choose a supported document category.")
    filename = _clean_filename(upload.filename)
    storage_root = Path(settings.document_storage_path).resolve()
    storage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    storage_key = f"drafts/{uuid4().hex}{Path(filename).suffix.lower()}"
    target = storage_root / storage_key
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix="upload-", dir=storage_root)
    try:
        with os.fdopen(fd, "wb") as destination:
            size, digest, header = _copy_limited(upload.file, destination, settings.document_upload_max_bytes)
        media_type = _detected_media_type(filename, header)
        os.chmod(temporary_name, 0o600)
        if settings.document_storage_provider == "r2":
            with open(temporary_name, "rb") as source:
                _r2_client(settings).put_object(
                    Bucket=settings.r2_bucket,
                    Key=_object_key(storage_key, settings),
                    Body=source,
                    ContentType=media_type,
                    Metadata={"sha256": digest},
                )
            os.unlink(temporary_name)
        else:
            os.replace(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

    asset = DocumentAsset(
        owner_user_id=user.id,
        original_filename=filename,
        media_type=media_type,
        byte_size=size,
        sha256=digest,
        storage_key=storage_key,
        state="clean",
        classification=classification,
        description=(description or "").strip() or None,
        validated_at=now_utc(),
    )
    db.add(asset)
    db.flush()
    return asset


def asset_dict(asset: DocumentAsset) -> dict:
    return {
        "id": asset.id,
        "originalFilename": asset.original_filename,
        "mediaType": asset.media_type,
        "byteSize": asset.byte_size,
        "sha256": asset.sha256,
        "state": asset.state,
        "classification": asset.classification,
        "description": asset.description,
        "createdAt": asset.created_at,
    }


def revision_assets(db: Session, revision_id: UUID) -> list[DocumentAsset]:
    return list(db.scalars(
        select(DocumentAsset)
        .join(DocumentRevisionAsset, DocumentRevisionAsset.asset_id == DocumentAsset.id)
        .where(DocumentRevisionAsset.revision_id == revision_id)
        .order_by(DocumentRevisionAsset.display_order, DocumentAsset.created_at, DocumentAsset.id)
    ))


def library_assets(db: Session, user: User) -> list[DocumentAsset]:
    return list(db.scalars(
        select(DocumentAsset)
        .where(
            DocumentAsset.owner_user_id == user.id,
            DocumentAsset.document_id.is_(None),
            DocumentAsset.state == "clean",
        )
        .order_by(DocumentAsset.created_at.desc(), DocumentAsset.id.desc())
    ))


def _manifest(assets: Iterable[DocumentAsset]) -> list[dict]:
    return [{
        "id": str(asset.id),
        "filename": asset.original_filename,
        "mediaType": asset.media_type,
        "byteSize": asset.byte_size,
        "sha256": asset.sha256,
        "classification": asset.classification,
        "description": asset.description,
    } for asset in assets]


def refresh_revision_evidence(db: Session, revision: DocumentRevision) -> None:
    assets = revision_assets(db, revision.id)
    revision.manifest_hash = payload_hash(_manifest(assets))
    revision.evidence_hash = payload_hash({"contentHash": revision.content_hash, "manifestHash": revision.manifest_hash})
    db.flush()


def attach_draft_assets(
    db: Session,
    *,
    document: SharedDocument,
    revision: DocumentRevision,
    participant: SharedRecordParticipant,
    user: User,
    asset_ids: list[UUID],
    settings: Settings,
) -> list[DocumentAsset]:
    if revision.state != "proposed":
        raise SharedRecordConflict("Supporting documents can only be attached before this revision is acknowledged.")
    if len(asset_ids) != len(set(asset_ids)):
        raise DocumentAssetError("The same supporting document was selected more than once.")
    assets: list[DocumentAsset] = []
    existing_count = db.scalar(select(func.count()).select_from(DocumentRevisionAsset).where(DocumentRevisionAsset.revision_id == revision.id)) or 0
    for order, asset_id in enumerate(asset_ids, start=existing_count):
        asset = db.scalar(select(DocumentAsset).where(DocumentAsset.id == asset_id).with_for_update())
        if asset is None or asset.owner_user_id != user.id or asset.document_id is not None:
            raise DocumentAssetError("One of the selected supporting documents is no longer available.")
        if asset.state != "clean":
            raise DocumentAssetError("One of the selected supporting documents has not passed validation.")
        destination_key = f"revisions/{document.id}/{revision.id}/{uuid4().hex}{Path(asset.storage_key).suffix.lower()}"
        if settings.document_storage_provider == "r2":
            client = _r2_client(settings)
            source_key = _object_key(asset.storage_key, settings)
            client.copy_object(
                Bucket=settings.r2_bucket,
                Key=_object_key(destination_key, settings),
                CopySource={"Bucket": settings.r2_bucket, "Key": source_key},
                MetadataDirective="COPY",
            )
        else:
            source = stored_path(asset, settings)
            destination = (Path(settings.document_storage_path).resolve() / destination_key).resolve()
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(source, destination)
        bound_asset = DocumentAsset(
            owner_user_id=user.id,
            document_id=document.id,
            uploaded_by_participant_id=participant.id,
            original_filename=asset.original_filename,
            media_type=asset.media_type,
            byte_size=asset.byte_size,
            sha256=asset.sha256,
            storage_key=destination_key,
            state="attached",
            classification=asset.classification,
            description=asset.description,
            validated_at=asset.validated_at,
        )
        db.add(bound_asset)
        db.flush()
        db.add(DocumentRevisionAsset(revision_id=revision.id, asset_id=bound_asset.id, display_order=order))
        assets.append(bound_asset)
    db.flush()
    refresh_revision_evidence(db, revision)
    return assets


def carry_forward_revision_assets(db: Session, *, base_revision_id: UUID, revision: DocumentRevision) -> list[DocumentAsset]:
    if revision.base_revision_id != base_revision_id or revision.state != "proposed":
        raise SharedRecordConflict("Supporting documents can only be carried into the direct replacement revision.")
    assets = revision_assets(db, base_revision_id)
    for order, asset in enumerate(assets):
        db.add(DocumentRevisionAsset(revision_id=revision.id, asset_id=asset.id, display_order=order))
    db.flush()
    refresh_revision_evidence(db, revision)
    return assets


def owned_draft_asset(db: Session, asset_id: UUID, user: User) -> DocumentAsset:
    asset = db.get(DocumentAsset, asset_id)
    if asset is None or asset.owner_user_id != user.id or asset.document_id is not None:
        raise SharedRecordNotFound("Document not found")
    return asset


def readable_asset(db: Session, asset_id: UUID, user: User) -> DocumentAsset:
    asset = db.get(DocumentAsset, asset_id)
    if asset is None:
        raise SharedRecordNotFound("Document not found")
    if asset.document_id is None:
        if asset.owner_user_id != user.id:
            raise SharedRecordNotFound("Document not found")
        return asset
    document = db.get(SharedDocument, asset.document_id)
    if document is None:
        raise SharedRecordNotFound("Document not found")
    record_for_user(db, document.shared_record_id, user.id)
    return asset


def stored_path(asset: DocumentAsset, settings: Settings) -> Path:
    if settings.document_storage_provider != "local":
        raise SharedRecordError("This document is stored remotely.")
    root = Path(settings.document_storage_path).resolve()
    candidate = (root / asset.storage_key).resolve()
    if root not in candidate.parents:
        raise SharedRecordError("The stored document path is invalid.")
    if not candidate.is_file():
        raise SharedRecordNotFound("The stored document is unavailable.")
    return candidate


def read_asset_bytes(asset: DocumentAsset, settings: Settings) -> bytes:
    if settings.document_storage_provider == "r2":
        response = _r2_client(settings).get_object(Bucket=settings.r2_bucket, Key=_object_key(asset.storage_key, settings))
        body = response["Body"].read(settings.document_upload_max_bytes + 1)
        if len(body) != asset.byte_size or hashlib.sha256(body).hexdigest() != asset.sha256:
            raise SharedRecordConflict("The stored document no longer matches its recorded fingerprint.")
        return body
    data = stored_path(asset, settings).read_bytes()
    if len(data) != asset.byte_size or hashlib.sha256(data).hexdigest() != asset.sha256:
        raise SharedRecordConflict("The stored document no longer matches its recorded fingerprint.")
    return data


def presigned_download_url(asset: DocumentAsset, settings: Settings) -> str | None:
    if settings.document_storage_provider != "r2":
        return None
    return _r2_client(settings).generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.r2_bucket,
            "Key": _object_key(asset.storage_key, settings),
            "ResponseContentType": asset.media_type,
            "ResponseContentDisposition": f'attachment; filename="{asset.original_filename}"',
        },
        ExpiresIn=settings.r2_presign_seconds,
    )


def delete_draft_asset(db: Session, asset: DocumentAsset, settings: Settings) -> None:
    path = stored_path(asset, settings) if settings.document_storage_provider == "local" else None
    db.delete(asset)
    db.flush()
    if settings.document_storage_provider == "r2":
        _r2_client(settings).delete_object(Bucket=settings.r2_bucket, Key=_object_key(asset.storage_key, settings))
    elif path is not None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
