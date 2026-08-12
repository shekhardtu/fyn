from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

import hashlib
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session, selectinload

from .database import SessionLocal, get_db
from .event_time import as_utc, local_date_string, now_utc
from .config import CSV_UPLOAD_MAX_BYTES, get_settings
from .contracts import STREAM_EVENT_MODELS
from .domain import ExecutionStatus, FinancialSourceType, ImportRecordStatus, ImportStatus, REVOCABLE_SOURCE_TYPES, ReconciliationOutcome, WidgetActionId
from .models import (
    AIAction,
    AnalysisToolRun,
    AuditLog,
    Conversation,
    Import as ImportJob,
    ImportRecord,
    Message,
    ReconciliationCandidate,
    Transaction,
    TransactionDraft,
    User,
    UserPreference,
)
from .schemas import (
    ActionRequest,
    AgentActivityEvent,
    AffordabilityIn,
    AgentDiagnosticsOut,
    AgentResponse,
    BootstrapResponse,
    ChatRequest,
    ConversationOut,
    ConversationCreatedOut,
    ConversationPage,
    ConversationSummaryOut,
    DataDeletionIn,
    DataDeletionOut,
    FinancialMessageIn,
    FinancialMessageOut,
    HealthOut,
    InvestmentProjectionIn,
    ImportResultOut,
    LoanCalculationIn,
    LocationPreferenceIn,
    LocationPreferenceOut,
    ObservationIn,
    PendingAction,
    ReconciliationResultOut,
    ReconciliationReviewOut,
    PrivacyStatusOut,
    SourceRevocationOut,
    TransactionOut,
    Widget,
    WidgetAction,
    WidgetLifecycle,
    WidgetType,
)
# Every route below is user-scoped through this one dependency, so protecting
# it protected all of them at once.
from .security import clear_session_cookie, current_user
from .services.calculators import affordability, investment_projection, loan_with_prepayment
from .services.adapters import CSVAdapter, MessageAdapter, import_summary
from .services.conversation import get_or_create_conversation, handle_action, handle_chat, persist_agent_response, prepare_widget_action, resolve_widget_action, user_conversation
from .services.reconciliation import ingest_observation
from .services.preferences import set_user_preference, user_preference
from .services.transactions import canonical_transactions
from .services.user_data import delete_user_data, export_user_data
from .services.tool_models import AffordabilityResult, InvestmentProjectionResult, LoanPrepaymentResult


router = APIRouter(prefix="/api")

# Enough to fill the history rail past the fold on a tall screen, so the first
# lazy page is fetched while scrolling rather than immediately on load.
CONVERSATION_PAGE_SIZE = 25


def _agent_mode(settings) -> str:
    return "llm" if settings.primary_agent_enabled and bool(settings.openai_api_key) else "deterministic_fallback"


def _agent_models(settings) -> dict[str, str] | None:
    if _agent_mode(settings) != "llm":
        return None
    return {
        "router": settings.router_model,
        "transaction": settings.transaction_model,
        "analysis": settings.analysis_model,
        "validator": settings.validator_model,
        "reconciliation": settings.reconciliation_model,
    }


def _ensure_source_active(db: Session, user_id: UUID, source_type: str) -> None:
    preference = user_preference(db, user_id, f"source:{source_type}:revoked")
    if preference and preference.value.get("revoked") is True:
        raise HTTPException(status_code=403, detail=f"{source_type.upper()} access is revoked in privacy settings")


def _owned_conversation(
    db: Session,
    user: User,
    conversation_id: UUID,
    *,
    with_messages: bool = False,
) -> Conversation:
    conversation = user_conversation(
        db,
        user.id,
        conversation_id,
        with_messages=with_messages,
    )
    if not conversation:
        # A foreign identifier and a missing identifier are intentionally
        # indistinguishable at the API boundary.
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    settings = get_settings()
    return HealthOut.model_validate({
        "status": "ok",
        "time": now_utc().isoformat(),
        "database": "postgresql" if settings.database_url.startswith("postgresql") else "sqlite",
        "agent_mode": _agent_mode(settings),
        "models": _agent_models(settings),
    })


@router.get("/diagnostics/agent", response_model=AgentDiagnosticsOut)
def agent_diagnostics(db: Session = Depends(get_db), user: User = Depends(current_user)) -> AgentDiagnosticsOut:
    settings = get_settings()
    actions = list(db.scalars(select(AIAction).where(AIAction.user_id == user.id, AIAction.action_type == "primary_router").order_by(AIAction.created_at.desc()).limit(20)))
    return AgentDiagnosticsOut.model_validate({
        "mode": _agent_mode(settings),
        "models": _agent_models(settings),
        "recent_decisions": [
            {
                "tool": action.payload_redacted.get("tool"),
                "confidence": action.payload_redacted.get("confidence"),
                "status": action.status,
                "created_at": action.created_at.isoformat(),
            }
            for action in actions
        ],
    })


@router.get("/bootstrap", response_model=BootstrapResponse)
def bootstrap(db: Session = Depends(get_db), user: User = Depends(current_user)) -> BootstrapResponse:
    latest = db.scalar(
        select(Conversation)
        .where(Conversation.user_id == user.id, Conversation.archived.is_(False))
        .order_by(Conversation.updated_at.desc())
        .limit(1)
    )
    conversation = get_or_create_conversation(db, user, latest.id if latest else None)
    loaded = _owned_conversation(db, user, conversation.id, with_messages=True)
    return BootstrapResponse(
        user={"id": str(user.id), "name": user.display_name, "currency": user.currency, "timezone": user.timezone},
        active_conversation=ConversationOut.model_validate(loaded),
    )


@router.get("/conversations", response_model=ConversationPage)
def list_conversations(
    cursor: str | None = None,
    limit: int = Query(default=CONVERSATION_PAGE_SIZE, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ConversationPage:
    """One page of conversation history, newest first.

    Paged by keyset rather than OFFSET. The rail is ordered by a column that
    moves while you are reading it — using a thread bumps its `updated_at` — and
    an offset would then hand back threads the reader has already scrolled past,
    or skip ones that slid down a page.
    """
    query = select(Conversation).where(Conversation.user_id == user.id, Conversation.archived.is_(False))
    if cursor:
        try:
            marker, marker_id = cursor.rsplit("|", 1)
            edge_updated, edge_id = datetime.fromisoformat(marker), UUID(marker_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Malformed conversation cursor") from error
        query = query.where(or_(Conversation.updated_at < edge_updated, and_(Conversation.updated_at == edge_updated, Conversation.id < edge_id)))
    # One row past the page tells us whether there is a next page without a
    # second count query.
    rows = list(db.scalars(query.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(limit + 1)))
    items = rows[:limit]
    return ConversationPage(
        items=[ConversationSummaryOut.model_validate(item) for item in items],
        next_cursor=f"{items[-1].updated_at.isoformat()}|{items[-1].id}" if len(rows) > limit else None,
    )


@router.post("/conversations", response_model=ConversationCreatedOut)
def new_conversation(db: Session = Depends(get_db), user: User = Depends(current_user)) -> ConversationCreatedOut:
    conversation = get_or_create_conversation(db, user)
    return ConversationCreatedOut(id=conversation.id)


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
def get_conversation(conversation_id: UUID, db: Session = Depends(get_db), user: User = Depends(current_user)) -> ConversationOut:
    conversation = _owned_conversation(db, user, conversation_id, with_messages=True)
    return ConversationOut.model_validate(conversation)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: UUID, db: Session = Depends(get_db), user: User = Depends(current_user)) -> Response:
    """Erase one thread and everything that points at it.

    Deleting a conversation removes the thread, not the money: transactions it
    recorded are canonical financial history and survive. Everything that only
    exists because the thread existed does not — its messages and their widgets,
    drafts that never became transactions, the router's decision log, and the
    per-run analysis traces. The generated tools themselves are a user-level
    registry shared by every thread, so those stay.

    SQLite only honours `ON DELETE CASCADE` with `PRAGMA foreign_keys` on, so
    each dependent table is cleared explicitly rather than trusted to cascade;
    `analysis_tool_runs` would in any case be orphaned rather than removed by
    its `SET NULL` rule, and keep a trace of the deleted thread.
    """
    conversation = _owned_conversation(db, user, conversation_id)
    for model in (Message, TransactionDraft, AIAction, AnalysisToolRun):
        db.execute(delete(model).where(model.conversation_id == conversation_id))
    db.delete(conversation)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/chat", response_model=AgentResponse)
def chat(request: ChatRequest, db: Session = Depends(get_db), user: User = Depends(current_user)) -> AgentResponse:
    conversation = (
        _owned_conversation(db, user, request.conversation_id)
        if request.conversation_id
        else get_or_create_conversation(db, user)
    )
    activities: dict[str, dict] = {}
    response = handle_chat(db, user, conversation, request.text, lambda event: activities.__setitem__(event["id"], event))
    return _attach_activity_trace(db, response, activities)


def _attach_activity_trace(db: Session, response: AgentResponse, activities: dict[str, dict]) -> AgentResponse:
    terminal_ms = max((float(step.get("cumulativeMs", 0)) for step in activities.values()), default=0)
    steps = []
    for raw_step in activities.values():
        step = dict(raw_step)
        if step.get("status") == ExecutionStatus.RUNNING:
            started_ms = float(step.get("cumulativeMs", 0))
            step.update({
                "status": ExecutionStatus.FAILED,
                "detail": step.get("detail") or "This stage ended before producing a valid terminal result.",
                "durationMs": max(float(step.get("durationMs", 0)), round(terminal_ms - started_ms, 1)),
                "cumulativeMs": terminal_ms,
            })
        steps.append(step)
    steps.sort(key=lambda step: float(step.get("cumulativeMs", 0)))
    total_ms = max((float(step.get("cumulativeMs", 0)) for step in steps), default=0)
    settings = get_settings()
    used_agno = any(str(step.get("tool", "")).startswith("agno_") or str(step.get("label", "")).startswith("Agno") for step in steps)
    used_analysis = any(str(step.get("tool", "")) in {"analysis_harness", "agno_reroute"} for step in steps)
    model_path = f"{settings.router_model} → {settings.analysis_model + ' → ' if used_analysis else ''}{settings.validator_model}"
    widget = Widget(
        id=f"agent-activity-{response.message_id}",
        type=WidgetType.AGENT_ACTIVITY,
        data={
            "title": "Agno agent run" if used_agno else "Copilot fast path",
            "engine": "Agno harness" if used_agno else "Deterministic domain",
            "model": model_path if used_agno else "no model call",
            "steps": steps,
            "totalMs": total_ms,
            "live": False,
        },
    )
    response.widgets.append(widget)
    message = db.get(Message, response.message_id)
    if message:
        message.widgets = [*message.widgets, widget.model_dump(mode="json")]
        db.commit()
    return response


# One turn at a time per conversation. A second message sent while a turn is
# still running waits for it rather than racing it — the "enqueue" policy, and
# the reason a reply can no longer be filed against the wrong question.
#
# The client disables its composer while a turn runs, but that flag lives in a
# mounted React component: it does not survive a reload, a second tab, or
# navigating away and back, and the stream it started keeps running regardless.
# Admission control has to be here, where every caller passes through.
#
# Scope: one process. `uvicorn app.main:app` runs single-process, so this holds
# for the deployed topology. Running with `--workers` would need the same gate
# on a shared resource — a PostgreSQL advisory lock keyed on the conversation.
def _sse(kind: str, payload: dict) -> str:
    model = STREAM_EVENT_MODELS[kind]
    validated = model.model_validate(payload).model_dump(mode="json", by_alias=True)
    return f"event: {kind}\ndata: {json.dumps(validated, separators=(',', ':'))}\n\n"


_conversation_locks: dict[UUID, asyncio.Lock] = {}
_conversation_lock_waiters: dict[UUID, int] = {}
_locks_guard = asyncio.Lock()

# How long a queued turn will wait before giving up. Without a ceiling, one
# wedged run makes the conversation permanently unusable — the failure mode
# reported repeatedly against thread-locking agent APIs.
QUEUE_WAIT_SECONDS = 90


class TurnQueueTimeout(Exception):
    """Waited too long behind another turn. Reported as a stream error rather
    than an HTTP status: by the time this can happen the response has already
    begun, so the status line is long since sent."""


@asynccontextmanager
async def _conversation_turn(conversation_id: UUID | None) -> AsyncIterator[bool]:
    """Holds the conversation for one turn, yielding whether it had to wait.

    A brand-new conversation has no id yet and nothing to contend with."""
    if conversation_id is None:
        yield False
        return
    async with _locks_guard:
        lock = _conversation_locks.setdefault(conversation_id, asyncio.Lock())
        _conversation_lock_waiters[conversation_id] = _conversation_lock_waiters.get(conversation_id, 0) + 1
    queued = lock.locked()
    try:
        try:
            await asyncio.wait_for(lock.acquire(), timeout=QUEUE_WAIT_SECONDS)
        # Spelled out rather than caught as the builtin: on Python 3.9 these are
        # two different classes, and the builtin catches nothing here.
        except asyncio.TimeoutError as error:
            raise TurnQueueTimeout from error
        try:
            yield queued
        finally:
            lock.release()
    finally:
        async with _locks_guard:
            remaining = _conversation_lock_waiters.get(conversation_id, 1) - 1
            if remaining > 0:
                _conversation_lock_waiters[conversation_id] = remaining
            else:
                # Nobody else is holding or waiting, so the entry would only
                # accumulate — one per conversation the user ever opened.
                _conversation_lock_waiters.pop(conversation_id, None)
                _conversation_locks.pop(conversation_id, None)


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> StreamingResponse:
    """Stream safe agent activity events, then the validated structured response."""
    if request.conversation_id:
        _owned_conversation(db, user, request.conversation_id)
    user_id = user.id
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

    def publish(kind: str, payload: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

    def run_chat() -> None:
        activities: dict[str, dict] = {}

        def on_activity(event: dict) -> None:
            activities[event["id"]] = event
            publish("activity", event)

        try:
            with SessionLocal() as worker_db:
                worker_user = worker_db.get(User, user_id)
                if not worker_user:
                    raise ValueError("User not found")
                conversation = get_or_create_conversation(worker_db, worker_user, request.conversation_id)
                response = handle_chat(worker_db, worker_user, conversation, request.text, on_activity)
                response = _attach_activity_trace(worker_db, response, activities)
                publish("result", response.model_dump(mode="json", by_alias=True))
        except Exception as error:
            publish("error", {"message": "fyn AI could not complete this request.", "errorType": type(error).__name__})

    async def events():
        try:
            # The gate is held across the whole stream, so it is released only
            # once this turn's result has been written and sent.
            async with _conversation_turn(request.conversation_id) as queued:
                async for chunk in _run_turn_stream(queued):
                    yield chunk
        except TurnQueueTimeout:
            # The client turns an error event into a retryable banner, which is
            # the honest offer here: the message was never sent to the copilot.
            yield _sse("error", {
                "message": "The previous message in this conversation is still running. Try sending this again in a moment.",
                "errorType": "TurnQueueTimeout",
            })

    async def _run_turn_stream(queued: bool):
        if queued:
            # A silent stream reads as a hung one. Say what it is waiting for.
            queued_event = AgentActivityEvent(
                id="queued",
                label="Waiting for the previous message to finish",
                status=ExecutionStatus.COMPLETED,
                detail="One message is answered at a time so replies stay with their questions.",
                duration_ms=0.0,
                cumulative_ms=0.0,
            )
            yield _sse(
                "activity",
                queued_event.model_dump(mode="json", by_alias=True),
            )
        task = asyncio.create_task(asyncio.to_thread(run_chat))
        try:
            while True:
                kind, payload = await queue.get()
                yield _sse(kind, payload)
                if kind in {"result", "error"}:
                    break
        finally:
            await task

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/actions", response_model=AgentResponse)
def action(request: ActionRequest, db: Session = Depends(get_db), user: User = Depends(current_user)) -> AgentResponse:
    conversation = _owned_conversation(db, user, request.conversation_id)
    try:
        origin = prepare_widget_action(db, conversation, request.widget_id, request.action)
        response = handle_action(db, user, conversation, request.action, request.payload)
        if request.complete_widget:
            lifecycle = WidgetLifecycle.CANCELLED if request.action.startswith("cancel_") else WidgetLifecycle.COMPLETED
            update = resolve_widget_action(
                db,
                origin,
                lifecycle=lifecycle,
                action=request.action,
                payload=request.payload,
            )
            if update:
                response.widget_updates.append(update)
                db.commit()
        return response
    except (ValueError, KeyError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/observations", response_model=ReconciliationResultOut)
def create_observation(request: ObservationIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> ReconciliationResultOut:
    _ensure_source_active(db, user.id, request.source_type)
    return ingest_observation(db, user.id, request)


@router.post("/ingest/message", response_model=FinancialMessageOut)
def ingest_message(request: FinancialMessageIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> FinancialMessageOut:
    _ensure_source_active(db, user.id, request.source_type)
    adapted = MessageAdapter(request.source_type).adapt_message(request.text, request.message_id, request.observed_at, user.timezone)
    if not adapted.relevant or not adapted.observation:
        return FinancialMessageOut(classification=adapted.classification, relevant=False, reason=adapted.reason)
    result = ingest_observation(db, user.id, adapted.observation)
    return FinancialMessageOut(classification=adapted.classification, relevant=True, reason=adapted.reason, reconciliation=result)


@router.post("/imports/csv", response_model=ImportResultOut)
async def import_csv(conversation_id: UUID = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(current_user)) -> ImportResultOut:
    _ensure_source_active(db, user.id, FinancialSourceType.CSV)
    conversation = _owned_conversation(db, user, conversation_id)
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=415, detail="Upload a CSV file")
    content = await file.read(CSV_UPLOAD_MAX_BYTES + 1)
    if len(content) > CSV_UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="CSV is limited to 10 MB")
    file_hash = hashlib.sha256(content).hexdigest()
    existing = db.scalar(select(ImportJob).where(ImportJob.user_id == user.id, ImportJob.source_type == FinancialSourceType.CSV.value, ImportJob.file_hash == file_hash))
    if existing:
        result = import_summary(existing, idempotent_replay=True)
        return _record_import_preview(db, conversation, file.filename, result)
    try:
        rows = CSVAdapter().adapt(content, user.timezone)
    except (UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    job = ImportJob(user_id=user.id, source_type=FinancialSourceType.CSV.value, filename=file.filename, file_hash=file_hash, status=ImportStatus.AWAITING_CONFIRMATION, total_records=len(rows))
    db.add(job)
    db.commit()
    db.refresh(job)
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
    db.commit()
    result = import_summary(job, idempotent_replay=False)
    return _record_import_preview(db, conversation, file.filename, result)


def _record_import_preview(db: Session, conversation: Conversation, filename: str, result: dict) -> dict:
    db.add(Message(conversation_id=conversation.id, role="user", content=f"Uploaded {filename}", widgets=[], citations=[]))
    widget = Widget(
        id=f"import-{result['importId']}",
        type=WidgetType.IMPORT_REVIEW,
        data={"title": filename, **result},
        actions=[] if result["status"] == ImportStatus.COMPLETED else [WidgetAction(id="import", label=f"Import {result['highConfidence']}", action=WidgetActionId.COMMIT_IMPORT, style="primary", payload={"importId": result["importId"]})],
    )
    content = f"I found {result['total']} row{'s' if result['total'] != 1 else ''}. Review the summary before importing."
    agent_response = persist_agent_response(
        db,
        conversation,
        content,
        widgets=[widget],
        pending_action=PendingAction(
            action=WidgetActionId.COMMIT_IMPORT,
            resource_id=result["importId"],
        ) if widget.actions else None,
    )
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


@router.get("/transactions", response_model=list[TransactionOut])
def transactions(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[TransactionOut]:
    statement = (
        canonical_transactions(user.id)
        .options(selectinload(Transaction.sources))
        .order_by(Transaction.transaction_at.desc(), Transaction.created_at.desc())
    )
    rows = list(db.scalars(statement))
    return [TransactionOut(
        id=item.id,
        transaction_type=item.transaction_type,
        amount_minor=item.amount_minor,
        currency=item.currency,
        merchant_name=item.merchant_name,
        transaction_at=as_utc(item.transaction_at),
        status=item.status,
        source_count=len(item.sources),
    ) for item in rows]


@router.get("/reconciliation/reviews", response_model=list[ReconciliationReviewOut])
def reconciliation_reviews(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[ReconciliationReviewOut]:
    candidates = list(db.scalars(select(ReconciliationCandidate).where(ReconciliationCandidate.user_id == user.id, ReconciliationCandidate.decision == ReconciliationOutcome.NEEDS_REVIEW).order_by(ReconciliationCandidate.score.desc())))
    return [ReconciliationReviewOut(id=item.id, observation_id=item.observation_id, transaction_id=item.transaction_id, score=float(item.score), signals=item.matching_signals) for item in candidates]


# These three return their model rather than a dump of it. Dumping `by_alias`
# here produced the camelCase body the client wants, then handed it back to
# FastAPI, which re-validates against the same `response_model` — and that only
# reads field names, so every one of these answered 500. Returning the model
# lets one layer own serialization, which is what the rest of this router does.
@router.get("/privacy", response_model=PrivacyStatusOut)
def privacy_status(db: Session = Depends(get_db), user: User = Depends(current_user)) -> PrivacyStatusOut:
    preferences = list(db.scalars(select(UserPreference).where(UserPreference.user_id == user.id)))
    mapped = {item.key: item.value for item in preferences}
    sources = {
        source.value: not mapped.get(f"source:{source.value}:revoked", {}).get("revoked", False)
        for source in sorted(REVOCABLE_SOURCE_TYPES, key=lambda item: item.value)
    }
    return PrivacyStatusOut(location_enabled=mapped.get("location:enabled", {}).get("enabled", False), sources=sources, retention="until_deleted")


@router.patch("/privacy/location", response_model=LocationPreferenceOut)
def set_location_preference(request: LocationPreferenceIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> LocationPreferenceOut:
    set_user_preference(db, user.id, "location:enabled", {"enabled": request.enabled})
    db.add(AuditLog(user_id=user.id, action="privacy.location_updated", entity_type="user_preference", metadata_redacted={"enabled": request.enabled}))
    db.commit()
    return LocationPreferenceOut(location_enabled=request.enabled)


@router.post("/privacy/sources/{source_type}/revoke", response_model=SourceRevocationOut)
def revoke_source(source_type: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> SourceRevocationOut:
    if source_type not in {item.value for item in REVOCABLE_SOURCE_TYPES}:
        raise HTTPException(status_code=404, detail="Unknown financial source")
    set_user_preference(db, user.id, f"source:{source_type}:revoked", {"revoked": True, "revokedAt": now_utc().isoformat()})
    db.add(AuditLog(user_id=user.id, action="privacy.source_revoked", entity_type="financial_source", metadata_redacted={"sourceType": source_type}))
    db.commit()
    return SourceRevocationOut(source_type=source_type, active=False)


@router.get("/privacy/export")
def export_data(db: Session = Depends(get_db), user: User = Depends(current_user)) -> Response:
    payload = {
        "exportedAt": now_utc().isoformat(),
        **export_user_data(db, user),
    }
    export_day = local_date_string(now_utc(), user.timezone)
    return JSONResponse(payload, headers={"Content-Disposition": f'attachment; filename="fyn-ai-export-{export_day}.json"'})


@router.delete("/privacy/data", response_model=DataDeletionOut)
def delete_data(request: DataDeletionIn, response: Response, db: Session = Depends(get_db), user: User = Depends(current_user)) -> DataDeletionOut:
    delete_user_data(db, user)
    # The account and its sessions are gone, so the browser is holding a cookie
    # that can only ever produce a 401. It is also what frees the phone number
    # and email address for a different account to link.
    clear_session_cookie(response)
    return DataDeletionOut(deleted=True)


@router.post("/calculators/affordability", response_model=AffordabilityResult)
def calculate_affordability(request: AffordabilityIn) -> AffordabilityResult:
    return AffordabilityResult.model_validate(affordability(**request.model_dump()))


@router.post("/calculators/loan", response_model=LoanPrepaymentResult)
def calculate_loan(request: LoanCalculationIn) -> LoanPrepaymentResult:
    return LoanPrepaymentResult.model_validate(loan_with_prepayment(**request.model_dump()))


@router.post("/calculators/investment", response_model=InvestmentProjectionResult)
def calculate_investment(request: InvestmentProjectionIn) -> InvestmentProjectionResult:
    return InvestmentProjectionResult.model_validate(investment_projection(**request.model_dump()))
    FinancialMessageIn,
