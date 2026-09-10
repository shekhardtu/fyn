"""One transactional deletion outbox for private files in every domain."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..event_time import now_utc
from ..models import ConversationAttachment, DocumentAsset, ObjectDeletion
from .object_storage import R2ObjectStore


def schedule_deletion(db: Session, key: str, provider: str = "r2", *, available_at: datetime | None = None) -> None:
    pending = db.scalar(select(ObjectDeletion).where(ObjectDeletion.storage_key == key))
    when = available_at or now_utc()
    if pending:
        pending.available_at = when
    else:
        db.add(ObjectDeletion(storage_key=key, storage_provider=provider, available_at=when))


def cleanup_files(db: Session, settings: Settings) -> int:
    """Bounded, retryable cleanup. Metadata removal commits before byte deletion."""
    expired = list(db.scalars(select(ConversationAttachment).where(
        ConversationAttachment.message_id.is_(None), ConversationAttachment.expires_at <= now_utc(),
    ).limit(100).with_for_update(skip_locked=True)))
    for row in expired:
        # Legacy staging capabilities are no longer issued; their delayed
        # outbox entries still protect cleanup across an upgrade.
        schedule_deletion(db, row.storage_key)
        db.delete(row)
    unfinished = list(db.scalars(select(DocumentAsset).where(DocumentAsset.state == "uploading",
        DocumentAsset.created_at <= now_utc() - timedelta(hours=24)).limit(100).with_for_update(skip_locked=True)))
    for asset in unfinished:
        schedule_deletion(db, asset.storage_key, settings.document_storage_provider)
        db.delete(asset)
    db.commit()
    due = list(db.scalars(select(ObjectDeletion).where(ObjectDeletion.available_at <= now_utc())
                         .order_by(ObjectDeletion.available_at).limit(100).with_for_update(skip_locked=True)))
    store = None
    count = 0
    for item in due:
        try:
            referenced = db.scalar(select(ConversationAttachment.id).where(
                ConversationAttachment.storage_key == item.storage_key, ConversationAttachment.status == "ready"))
            referenced_document = db.scalar(select(DocumentAsset.id).where(
                DocumentAsset.storage_key == item.storage_key, DocumentAsset.state.in_(["clean", "attached"])))
            if referenced or referenced_document:
                db.delete(item)
                continue
            if item.storage_provider == "local":
                root = Path(settings.document_storage_path).resolve()
                target = (root / item.storage_key).resolve()
                if root not in target.parents:
                    raise ValueError("Invalid stored file path.")
                target.unlink(missing_ok=True)
            else:
                if store is None:
                    store = R2ObjectStore(settings)
                store.delete(item.storage_key)
            db.delete(item)
            count += 1
        except Exception:
            item.attempts += 1
            item.available_at = now_utc() + timedelta(seconds=min(3600, 30 * 2 ** min(item.attempts, 7)))
    db.commit()
    return count
