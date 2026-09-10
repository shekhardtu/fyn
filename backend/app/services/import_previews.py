"""CSV preview workflow shared by legacy uploads and durable chat attachments."""
from __future__ import annotations

import hashlib
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain import FinancialSourceType, ImportRecordStatus, ImportStatus, WidgetActionId
from ..models import Conversation, ConversationAttachment, Import as ImportJob, ImportRecord, Message, User
from ..schemas import ImportResultOut, PendingAction, Widget, WidgetAction, WidgetType, WidgetUpdate
from .adapters import CSVAdapter, import_summary
from .agui import supersede_open_interrupts
from .conversation import persist_agent_response


def preview_csv_import(db: Session, user: User, conversation: Conversation, filename: str, content: bytes,
                       *, attachment: ConversationAttachment | None = None) -> ImportResultOut:
    file_hash = hashlib.sha256(content).hexdigest()
    existing = db.scalar(select(ImportJob).where(ImportJob.user_id == user.id, ImportJob.source_type == FinancialSourceType.CSV.value, ImportJob.file_hash == file_hash))
    if existing:
        widget_updates = supersede_open_interrupts(
            db,
            user,
            conversation,
            superseded_by="csv_upload",
        )
        result = import_summary(existing, idempotent_replay=True)
        return record_import_preview(db, conversation, filename, result, widget_updates=widget_updates, attachment=attachment)
    try:
        rows = CSVAdapter().adapt(content, timezone_name=user.timezone, default_currency=user.currency)
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(str(error)) from error
    widget_updates = supersede_open_interrupts(
        db,
        user,
        conversation,
        superseded_by="csv_upload",
    )
    job = ImportJob(user_id=user.id, source_type=FinancialSourceType.CSV.value, filename=filename, file_hash=file_hash, status=ImportStatus.AWAITING_CONFIRMATION, total_records=len(rows))
    db.add(job)
    # Keep the conversation/attachment locks through the entire preview.
    # The final reply commits the job, rows and attachment membership together.
    db.flush()
    high_confidence = review = duplicates = 0
    for row_number, observation, errors in rows:
        if not observation:
            db.add(ImportRecord(import_id=job.id, row_number=row_number, status=ImportRecordStatus.INVALID, errors=errors))
            review += 1
            continue
        high_confidence += 1
        db.add(ImportRecord(import_id=job.id, row_number=row_number, status=ImportRecordStatus.STAGED, errors=[], observation_payload=observation.model_dump(mode="json")))
    job.high_confidence_records = high_confidence
    job.review_records = review
    job.duplicate_records = duplicates
    job.status = ImportStatus.AWAITING_CONFIRMATION
    db.flush()
    result = import_summary(job, idempotent_replay=False)
    return record_import_preview(db, conversation, filename, result, widget_updates=widget_updates, attachment=attachment)


def record_import_preview(
    db: Session,
    conversation: Conversation,
    filename: str,
    result: dict,
    *,
    widget_updates: list[WidgetUpdate] | None = None,
    attachment: ConversationAttachment | None = None,
) -> ImportResultOut:
    user_message = Message(conversation_id=conversation.id, role="user", content=f"Uploaded {filename}", widgets=[], citations=[])
    db.add(user_message)
    db.flush()
    if attachment is not None and attachment.message_id is None:
        attachment.message_id = user_message.id
        attachment.expires_at = None
    widget = Widget(
        id=f"import-{result['importId']}-{uuid4()}",
        type=WidgetType.IMPORT_REVIEW,
        data={"title": filename, **result},
        actions=[] if result["status"] == ImportStatus.COMPLETED else [
            WidgetAction(id="import", label=f"Import {result['highConfidence']}", action=WidgetActionId.COMMIT_IMPORT, style="primary", payload={"importId": result["importId"]}),
            WidgetAction(id="cancel", label="Cancel", action=WidgetActionId.CANCEL_PENDING_ACTION, style="ghost", payload={"resourceId": result["importId"]}),
        ],
    )
    content = f"I found {result['total']} row{'s' if result['total'] != 1 else ''}. Review the summary before importing."
    agent_response = persist_agent_response(
        db,
        conversation,
        content,
        widgets=[widget],
        widget_updates=widget_updates,
        pending_action=PendingAction(
            action=WidgetActionId.COMMIT_IMPORT,
            resource_id=result["importId"],
        ) if widget.actions else None,
    )
    # Committed alongside the reply just above, so the ID is durable by here.
    agent_response.user_message_id = user_message.id
    response = ImportResultOut(
        import_id=result["importId"],
        status=result["status"],
        total=result["total"],
        high_confidence=result["highConfidence"],
        needs_review=result["needsReview"],
        duplicates=result["duplicates"],
        idempotent_replay=result["idempotentReplay"],
        agent_response=agent_response,
    )
    return response
