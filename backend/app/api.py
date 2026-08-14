from __future__ import annotations

import asyncio
from datetime import date, datetime
from uuid import UUID, uuid4

import hashlib
import json

from ag_ui.core import AgentCapabilities, RunAgentInput
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import and_, delete, or_, select
from pydantic import ValidationError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .database import get_db
from .event_time import as_utc, local_date_string, local_now, now_utc
from .config import CSV_UPLOAD_MAX_BYTES, get_settings
from .domain import AgentInterruptStatus, AgentRunStatus, FinancialSourceType, ImportRecordStatus, ImportStatus, REVOCABLE_SOURCE_TYPES, ReconciliationOutcome, TransactionType, WidgetActionId
from .models import (
    AIAction,
    AgentEvent,
    AgentInterrupt,
    AgentRun,
    AnalysisToolRun,
    AuditLog,
    Category,
    Conversation,
    Import as ImportJob,
    ImportRecord,
    Message,
    ReconciliationCandidate,
    Subcategory,
    Transaction,
    TransactionDraft,
    User,
    UserPreference,
)
from .schemas import (
    AgentInterruptOut,
    AgentRunOut,
    AgentThreadStateOut,
    AffordabilityIn,
    AgentDiagnosticsOut,
    AgentResponse,
    BootstrapResponse,
    CategoryDirectoryOut,
    CategoryDirectorySubcategoryOut,
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
    OverviewOut,
    PendingAction,
    ReconciliationResultOut,
    ReconciliationReviewOut,
    PrivacyStatusOut,
    SourceRevocationOut,
    TaxonomyCreateIn,
    TransactionCategoryHintIn,
    TransactionCategoryHintOut,
    TransactionListItemOut,
    TransactionUpdateIn,
    Widget,
    WidgetAction,
    WidgetType,
)
# Every route below is user-scoped through this one dependency, so protecting
# it protected all of them at once.
from .security import clear_session_cookie, current_user
from .services.calculators import affordability, investment_projection, loan_with_prepayment
from .services.adapters import CSVAdapter, MessageAdapter, import_summary
from .services.conversation import get_or_create_conversation, persist_agent_response, user_conversation
from .services.reconciliation import ingest_observation
from .services.preferences import set_user_preference, user_preference
from .services.overview import overview_snapshot
from .services.taxonomy import TaxonomyRepository
from .services.transactions import (
    canonical_transactions,
    create_manual_transaction as record_manual_transaction,
    update_saved_transaction as apply_transaction_update,
)
from .services.user_data import delete_user_data, export_user_data
from .services.tool_models import AffordabilityResult, InvestmentProjectionResult, LoanPrepaymentResult
from .services.agui import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    DurableEventPublisher,
    capabilities as agui_capabilities,
    execute_run as execute_agui_run,
    normalize_run_input,
    sse_event as agui_sse_event,
)


router = APIRouter(prefix="/api")

# Enough to fill the history rail past the fold on a tall screen, so the first
# lazy page is fetched while scrolling rather than immediately on load.
CONVERSATION_PAGE_SIZE = 25
_agui_tasks: set[asyncio.Task] = set()


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


@router.get("/overview", response_model=OverviewOut)
def overview(
    month: date | None = Query(default=None, description="Any date in the calendar month to summarize"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> OverviewOut:
    today = local_now(user.timezone).date()
    selected_month = (month or today).replace(day=1)
    try:
        return OverviewOut.model_validate(overview_snapshot(db, user.id, selected_month, today))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


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
    run_ids = select(AgentRun.id).where(AgentRun.conversation_id == conversation_id)
    db.execute(delete(AgentInterrupt).where(AgentInterrupt.run_id.in_(run_ids)))
    db.execute(delete(AgentEvent).where(AgentEvent.run_id.in_(run_ids)))
    db.execute(delete(AgentRun).where(AgentRun.conversation_id == conversation_id))
    for model in (Message, TransactionDraft, AIAction, AnalysisToolRun):
        db.execute(delete(model).where(model.conversation_id == conversation_id))
    db.delete(conversation)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _agui_session_factory(db: Session) -> sessionmaker[Session]:
    """Create worker sessions against the same engine used by this request.

    Deriving the factory from the injected session keeps the runtime compatible
    with the isolated SQLite engines used by the test suite as well as the
    production PostgreSQL engine.
    """
    return sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)


def _agui_stream_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }


def _agui_replay_response(
    session_factory: sessionmaker[Session],
    run_id: UUID,
    user_id: UUID,
    *,
    after: int = 0,
) -> StreamingResponse:
    async def events():
        cursor = max(after, 0)
        if cursor > 0:
            with session_factory() as replay_db:
                run_started = replay_db.scalar(
                    select(AgentEvent)
                    .where(AgentEvent.run_id == run_id, AgentEvent.event_type == "RUN_STARTED")
                    .order_by(AgentEvent.sequence)
                    .limit(1)
                )
            if run_started is not None:
                # Each HTTP continuation is independently verifiable by the
                # official client. The synthetic boundary is not persisted and
                # does not advance the durable cursor.
                yield agui_sse_event(cursor, run_started.payload)
        while True:
            with session_factory() as replay_db:
                rows = list(
                    replay_db.scalars(
                        select(AgentEvent)
                        .join(AgentRun, AgentRun.id == AgentEvent.run_id)
                        .where(
                            AgentEvent.run_id == run_id,
                            AgentRun.user_id == user_id,
                            AgentEvent.sequence > cursor,
                        )
                        .order_by(AgentEvent.sequence)
                    )
                )
                run_state = replay_db.execute(
                    select(AgentRun.status, AgentRun.last_sequence).where(
                        AgentRun.id == run_id,
                        AgentRun.user_id == user_id,
                    )
                ).one_or_none()
            for event in rows:
                cursor = event.sequence
                yield agui_sse_event(event.sequence, event.payload)
            if run_state is None:
                return
            run_status, last_sequence = run_state
            if run_status in TERMINAL_RUN_STATUSES and cursor >= last_sequence:
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers=_agui_stream_headers(),
    )


@router.get(
    "/agent/capabilities",
    response_model=AgentCapabilities,
    response_model_exclude_none=True,
)
def agent_capabilities() -> AgentCapabilities:
    return agui_capabilities()


@router.get("/agent/threads/{thread_id}", response_model=AgentThreadStateOut)
def agent_thread_state(
    thread_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> AgentThreadStateOut:
    _owned_conversation(db, user, thread_id)
    runs = list(
        db.scalars(
            select(AgentRun)
            .where(AgentRun.user_id == user.id, AgentRun.conversation_id == thread_id)
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(20)
        )
    )
    latest = runs[0] if runs else None
    active = next((run for run in runs if run.status in ACTIVE_RUN_STATUSES), None)
    interrupts = list(
        db.scalars(
            select(AgentInterrupt)
            .join(AgentRun, AgentRun.id == AgentInterrupt.run_id)
            .where(
                AgentRun.user_id == user.id,
                AgentRun.conversation_id == thread_id,
                AgentInterrupt.status == AgentInterruptStatus.OPEN.value,
            )
            .order_by(AgentInterrupt.created_at, AgentInterrupt.id)
        )
    )
    return AgentThreadStateOut(
        thread_id=thread_id,
        active_run=AgentRunOut.model_validate(active) if active else None,
        latest_run=AgentRunOut.model_validate(latest) if latest else None,
        interrupts=[AgentInterruptOut.model_validate(interrupt) for interrupt in interrupts],
    )


@router.post("/agent", response_class=StreamingResponse)
async def run_agent(
    request: RunAgentInput,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> StreamingResponse:
    try:
        thread_id = UUID(request.thread_id)
        run_id = UUID(request.run_id)
        parent_run_id = UUID(request.parent_run_id) if request.parent_run_id else None
    except ValueError as error:
        raise HTTPException(status_code=422, detail="threadId, runId and parentRunId must be UUIDs") from error
    if parent_run_id is not None:
        raise HTTPException(
            status_code=422,
            detail="parentRunId branching is not supported by this governed financial agent",
        )

    # The conversation row is the admission lock. Across API workers this
    # makes predecessor selection deterministic and establishes a durable run
    # chain instead of relying on a process-local asyncio lock.
    conversation = db.scalar(
        select(Conversation)
        .where(Conversation.id == thread_id, Conversation.user_id == user.id)
        .with_for_update()
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    session_factory = _agui_session_factory(db)

    existing = db.scalar(select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == user.id))
    if existing:
        if existing.conversation_id != thread_id:
            raise HTTPException(status_code=409, detail="runId already belongs to a different conversation")
        db.commit()
        return _agui_replay_response(session_factory, existing.id, user.id)

    try:
        input_payload, client_message_id = normalize_run_input(request)
    except (ValueError, ValidationError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    open_interrupt = db.scalar(
        select(AgentInterrupt.id)
        .join(AgentRun, AgentRun.id == AgentInterrupt.run_id)
        .where(
            AgentRun.user_id == user.id,
            AgentRun.conversation_id == thread_id,
            AgentInterrupt.status == AgentInterruptStatus.OPEN.value,
        )
        .limit(1)
    )
    if open_interrupt and input_payload.get("kind") != "resume":
        input_payload = {
            "kind": "protocol_error",
            "message": "Resolve the current agent interrupt before starting another run.",
            "code": "pending_interrupt",
        }

    if client_message_id:
        prior_message_run = db.scalar(
            select(AgentRun).where(
                AgentRun.user_id == user.id,
                AgentRun.client_message_id == client_message_id,
            )
        )
        if prior_message_run:
            if prior_message_run.conversation_id != thread_id:
                raise HTTPException(status_code=409, detail="Message id already belongs to a different conversation")
            db.commit()
            return _agui_replay_response(session_factory, prior_message_run.id, user.id)

    blocker = db.scalar(
        select(AgentRun)
        .where(
            AgentRun.user_id == user.id,
            AgentRun.conversation_id == thread_id,
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(1)
    )
    run = AgentRun(
        id=run_id,
        user_id=user.id,
        conversation_id=thread_id,
        parent_run_id=parent_run_id,
        blocked_by_run_id=blocker.id if blocker else None,
        client_message_id=client_message_id,
        status=AgentRunStatus.QUEUED.value,
        cancel_requested=False,
        input_payload=input_payload,
        last_sequence=0,
    )
    db.add(run)
    db.commit()

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[int, dict] | None] = asyncio.Queue()

    def publish_live(sequence: int, payload: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (sequence, payload))

    publisher = DurableEventPublisher(session_factory, run.id, user.id, 0, publish_live)

    async def execute() -> None:
        try:
            await asyncio.to_thread(execute_agui_run, session_factory, run.id, user.id, publisher)
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(execute())
    _agui_tasks.add(task)
    task.add_done_callback(_agui_tasks.discard)

    async def events():
        while True:
            item = await queue.get()
            if item is None:
                return
            sequence, payload = item
            yield agui_sse_event(sequence, payload)
            if payload.get("type") in {"RUN_FINISHED", "RUN_ERROR"}:
                return

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers=_agui_stream_headers(),
    )


@router.get("/agent/runs/{run_id}/events")
def replay_agent_run(
    run_id: UUID,
    after: int = Query(default=0, ge=0),
    last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> StreamingResponse:
    run = db.scalar(select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == user.id))
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    cursor = max(after, last_event_id or 0)
    return _agui_replay_response(_agui_session_factory(db), run.id, user.id, after=cursor)


@router.post("/agent/runs/{run_id}/cancel", response_model=AgentRunOut)
def cancel_agent_run(
    run_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> AgentRunOut:
    run = db.scalar(select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == user.id))
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.status in ACTIVE_RUN_STATUSES:
        run.cancel_requested = True
        db.commit()
        db.refresh(run)
    return AgentRunOut.model_validate(run)


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


def _transaction_list_item(item: Transaction, category: str | None, subcategory: str | None) -> TransactionListItemOut:
    return TransactionListItemOut(
        id=item.id,
        transaction_type=item.transaction_type,
        amount_minor=item.amount_minor,
        currency=item.currency,
        merchant=item.merchant_name,
        transaction_at=as_utc(item.transaction_at),
        status=item.status,
        category_id=item.category_id,
        category=category,
        subcategory_id=item.subcategory_id,
        subcategory=subcategory,
        spend_nature=item.spend_nature,
        location=item.location_label,
        source_count=len(item.sources),
    )


@router.get("/transactions", response_model=list[TransactionListItemOut])
def transactions(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=160),
    transaction_type: TransactionType | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[TransactionListItemOut]:
    statement = (
        canonical_transactions(user.id)
        .add_columns(Category.name, Subcategory.name)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .outerjoin(Subcategory, Subcategory.id == Transaction.subcategory_id)
        .options(selectinload(Transaction.sources))
        .order_by(Transaction.transaction_at.desc(), Transaction.created_at.desc(), Transaction.id.desc())
        .offset(offset)
        .limit(limit)
    )
    if transaction_type is not None:
        statement = statement.where(Transaction.transaction_type == transaction_type.value)
    search = q.strip() if q else ""
    if search:
        pattern = f"%{search}%"
        statement = statement.where(or_(
            Transaction.merchant_name.ilike(pattern),
            Transaction.transaction_type.ilike(pattern),
            Category.name.ilike(pattern),
            Subcategory.name.ilike(pattern),
        ))
    return [_transaction_list_item(item, category, subcategory) for item, category, subcategory in db.execute(statement)]


def _saved_transaction_item(db: Session, user_id: UUID, transaction: Transaction) -> TransactionListItemOut:
    taxonomy = TaxonomyRepository(db, user_id)
    category = taxonomy.category(transaction.category_id) if transaction.category_id else None
    subcategory = taxonomy.subcategory(
        transaction.subcategory_id,
        category_id=transaction.category_id,
    ) if transaction.subcategory_id else None
    return _transaction_list_item(
        transaction,
        category.name if category else None,
        subcategory.name if subcategory else None,
    )


def _subcategory_directory_entry(taxonomy: TaxonomyRepository, subcategory: Subcategory) -> CategoryDirectorySubcategoryOut:
    return CategoryDirectorySubcategoryOut(
        id=subcategory.id,
        slug=subcategory.slug,
        label=subcategory.name,
        editable=taxonomy.can_edit(subcategory),
    )


def _category_directory_entry(taxonomy: TaxonomyRepository, category: Category) -> CategoryDirectoryOut:
    subcategories = taxonomy.subcategories(category.id)
    subcategories_by_id = {item.id: item for item in subcategories}
    return CategoryDirectoryOut(
        id=category.id,
        slug=category.slug,
        label=category.name,
        icon=category.icon,
        subcategories=[_subcategory_directory_entry(taxonomy, item) for item in subcategories],
        editable=taxonomy.can_edit(category),
        hints=[
            TransactionCategoryHintOut(
                id=hint.id,
                merchant=hint.merchant_pattern,
                category_id=hint.category_id,
                subcategory_id=hint.subcategory_id,
                subcategory=subcategories_by_id[hint.subcategory_id].name if hint.subcategory_id in subcategories_by_id else None,
            )
            for hint in taxonomy.hints(category.id)
        ],
    )


def _raise_taxonomy_error(error: ValueError) -> None:
    detail = str(error)
    if detail.startswith("Unknown"):
        status_code = 404
    elif "used by" in detail or "already exists" in detail:
        status_code = 409
    elif detail.startswith("Built-in"):
        status_code = 403
    else:
        status_code = 422
    raise HTTPException(status_code=status_code, detail=detail) from error


@router.get("/categories", response_model=list[CategoryDirectoryOut])
def category_directory(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[CategoryDirectoryOut]:
    taxonomy = TaxonomyRepository(db, user.id)
    return [_category_directory_entry(taxonomy, category) for category in taxonomy.expense_categories()]


@router.post("/categories", response_model=CategoryDirectoryOut)
def create_category(
    request: TaxonomyCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> CategoryDirectoryOut:
    """Create a user-scoped expense category.

    Naming one that already exists returns the existing entry rather than
    failing: the caller asked for that category to be selectable, and it is.
    Mirrors the conversation flow's CREATE_CATEGORY semantics.
    """
    taxonomy = TaxonomyRepository(db, user.id)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Category name must be between 1 and 80 characters")
    existing = next((item for item in taxonomy.expense_categories() if item.name.casefold() == name.casefold()), None)
    if existing:
        return _category_directory_entry(taxonomy, existing)
    category = taxonomy.create_category(name, "circle-ellipsis", f"custom-{uuid4().hex}")
    taxonomy.create_subcategory(category, "Other", "other")
    db.commit()
    return _category_directory_entry(taxonomy, category)


@router.patch("/categories/{category_id}", response_model=CategoryDirectoryOut)
def rename_category(
    category_id: UUID,
    request: TaxonomyCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> CategoryDirectoryOut:
    taxonomy = TaxonomyRepository(db, user.id)
    try:
        category = taxonomy.rename_category(category_id, request.name)
    except ValueError as error:
        _raise_taxonomy_error(error)
    db.commit()
    return _category_directory_entry(taxonomy, category)


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(
    category_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    taxonomy = TaxonomyRepository(db, user.id)
    try:
        taxonomy.delete_category(category_id)
    except ValueError as error:
        _raise_taxonomy_error(error)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/categories/{category_id}/subcategories", response_model=CategoryDirectorySubcategoryOut)
def create_subcategory(
    category_id: UUID,
    request: TaxonomyCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> CategoryDirectorySubcategoryOut:
    taxonomy = TaxonomyRepository(db, user.id)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Subcategory name must be between 1 and 80 characters")
    category = taxonomy.category(category_id, expense_only=True)
    if not category:
        raise HTTPException(status_code=404, detail="Unknown category")
    existing = next((item for item in taxonomy.subcategories(category.id) if item.name.casefold() == name.casefold()), None)
    if not existing:
        existing = taxonomy.create_subcategory(category, name, f"custom-{uuid4().hex}")
        db.commit()
    return _subcategory_directory_entry(taxonomy, existing)


@router.patch("/categories/{category_id}/subcategories/{subcategory_id}", response_model=CategoryDirectorySubcategoryOut)
def rename_subcategory(
    category_id: UUID,
    subcategory_id: UUID,
    request: TaxonomyCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> CategoryDirectorySubcategoryOut:
    taxonomy = TaxonomyRepository(db, user.id)
    try:
        subcategory = taxonomy.rename_subcategory(category_id, subcategory_id, request.name)
    except ValueError as error:
        _raise_taxonomy_error(error)
    db.commit()
    return _subcategory_directory_entry(taxonomy, subcategory)


@router.delete("/categories/{category_id}/subcategories/{subcategory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_subcategory(
    category_id: UUID,
    subcategory_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    taxonomy = TaxonomyRepository(db, user.id)
    try:
        taxonomy.delete_subcategory(category_id, subcategory_id)
    except ValueError as error:
        _raise_taxonomy_error(error)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/categories/{category_id}/hints", response_model=TransactionCategoryHintOut, status_code=status.HTTP_201_CREATED)
def create_transaction_category_hint(
    category_id: UUID,
    request: TransactionCategoryHintIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> TransactionCategoryHintOut:
    taxonomy = TaxonomyRepository(db, user.id)
    try:
        hint = taxonomy.save_hint(category_id, request.merchant, request.subcategory_id)
    except ValueError as error:
        _raise_taxonomy_error(error)
    db.commit()
    subcategory = taxonomy.subcategory(hint.subcategory_id, category_id=category_id)
    return TransactionCategoryHintOut(id=hint.id, merchant=hint.merchant_pattern, category_id=hint.category_id, subcategory_id=hint.subcategory_id, subcategory=subcategory.name if subcategory else None)


@router.patch("/categories/{category_id}/hints/{hint_id}", response_model=TransactionCategoryHintOut)
def update_transaction_category_hint(
    category_id: UUID,
    hint_id: UUID,
    request: TransactionCategoryHintIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> TransactionCategoryHintOut:
    taxonomy = TaxonomyRepository(db, user.id)
    try:
        hint = taxonomy.save_hint(category_id, request.merchant, request.subcategory_id, hint_id=hint_id)
    except ValueError as error:
        _raise_taxonomy_error(error)
    db.commit()
    subcategory = taxonomy.subcategory(hint.subcategory_id, category_id=category_id)
    return TransactionCategoryHintOut(id=hint.id, merchant=hint.merchant_pattern, category_id=hint.category_id, subcategory_id=hint.subcategory_id, subcategory=subcategory.name if subcategory else None)


@router.delete("/categories/{category_id}/hints/{hint_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_transaction_category_hint(
    category_id: UUID,
    hint_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    taxonomy = TaxonomyRepository(db, user.id)
    hint = taxonomy.hint(hint_id)
    if not hint or hint.category_id != category_id:
        raise HTTPException(status_code=404, detail="Unknown transaction hint")
    taxonomy.delete_hint(hint_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/transactions", response_model=TransactionListItemOut, status_code=status.HTTP_201_CREATED)
def create_manual_transaction(
    request: TransactionUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> TransactionListItemOut:
    try:
        transaction = record_manual_transaction(
            db,
            user.id,
            currency=user.currency,
            amount_minor=request.amount_minor,
            merchant=request.merchant,
            transaction_at=request.transaction_at,
            transaction_type=request.transaction_type,
            category_id=request.category_id,
            subcategory_id=request.subcategory_id,
            spend_nature=request.spend_nature,
            location=request.location,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    db.commit()
    db.refresh(transaction)
    return _saved_transaction_item(db, user.id, transaction)


@router.patch("/transactions/{transaction_id}", response_model=TransactionListItemOut)
def update_transaction(
    transaction_id: UUID,
    request: TransactionUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> TransactionListItemOut:
    try:
        changes = {
            "amount_minor": request.amount_minor,
            "merchant": request.merchant,
            "transaction_at": request.transaction_at,
            "transaction_type": request.transaction_type,
            "spend_nature": request.spend_nature,
            "location": request.location,
        }
        if request.transaction_type == TransactionType.EXPENSE:
            changes.update(category_id=request.category_id, subcategory_id=request.subcategory_id)
        transaction = apply_transaction_update(db, user.id, transaction_id, **changes)
    except ValueError as error:
        status_code = 404 if str(error) == "Unknown transaction" else 422
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    db.commit()
    db.refresh(transaction)
    return _saved_transaction_item(db, user.id, transaction)


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
