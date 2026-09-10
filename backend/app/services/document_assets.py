"""Private file assets bound immutably to shared-document revisions."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Iterable
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    DocumentAsset,
    DocumentRevision,
    DocumentRevisionAsset,
    SharedDocument,
    SharedRecordParticipant,
    User,
)
from .file_uploads import FileUploadError
from .object_storage import R2ObjectStore
from .shared_records import SharedRecordConflict, SharedRecordError, SharedRecordNotFound, payload_hash, record_for_user


ALLOWED_CLASSIFICATIONS = {
    "external_agreement",
    "assurance_item",
    "transfer_receipt",
    "identity_evidence",
    "witness_statement",
    "supporting_evidence",
}
DOCUMENT_MEDIA_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


class DocumentAssetError(FileUploadError):
    pass


def _r2_client(settings: Settings):
    from .object_storage import ObjectStorageError, r2_client
    try:
        return r2_client(settings)
    except ObjectStorageError as error:
        raise DocumentAssetError(str(error)) from error


def _object_key(storage_key: str, settings: Settings) -> str:
    from .object_storage import object_key
    return object_key(storage_key, settings)


def validate_media_type(filename: str, header: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    expected = DOCUMENT_MEDIA_TYPES.get(suffix)
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
        return R2ObjectStore(settings).read(asset.storage_key, asset.byte_size, digest=asset.sha256)
    data = stored_path(asset, settings).read_bytes()
    if len(data) != asset.byte_size or hashlib.sha256(data).hexdigest() != asset.sha256:
        raise SharedRecordConflict("The stored document no longer matches its recorded fingerprint.")
    return data
