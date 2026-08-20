from __future__ import annotations

import asyncio
import csv
from datetime import date, datetime
import io
from uuid import UUID, uuid4

import hashlib

from ag_ui.core import AgentCapabilities, RunAgentInput
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import and_, delete, func, or_, select
from pydantic import ValidationError
from sqlalchemy.orm import Session, selectinload, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

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
    Dashboard,
    DashboardTile,
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
from .services.spreadsheet import annotate_source_field, ensure_spreadsheet_manifest
from .schemas import (
    AgentInterruptOut,
    AgentRunOut,
    SourceAnnotationsIn,
    SourceAnnotationsOut,
    SpreadsheetColumnDraftOut,
    SpreadsheetSourceOut,
    AgentThreadStateOut,
    AffordabilityIn,
    AgentDiagnosticsOut,
    AgentSettingsIn,
    AgentSettingsOut,
    BootstrapResponse,
    CategoryDirectoryOut,
    CategoryDirectorySubcategoryOut,
    ConversationOut,
    ConversationCreatedOut,
    ConversationPage,
    ConversationRenameIn,
    ConversationSummaryOut,
    DashboardCreateIn,
    DashboardCreatedOut,
    DashboardListOut,
    DashboardOut,
    DashboardSummaryOut,
    DashboardTileCreateIn,
    DashboardTileCreatedOut,
    DashboardTileErrorOut,
    DashboardTileOut,
    DataDeletionIn,
    DataDeletionOut,
    FinancialMessageIn,
    FinancialMessageOut,
    HealthOut,
    InsightEvidenceOut,
    InsightLineageOut,
    InsightOut,
    InsightsOut,
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
    WidgetUpdate,
)
# Every route below is user-scoped through this one dependency, so protecting
# it protected all of them at once.
from .security import clear_session_cookie, current_user
from .services.analysis_harness import HarnessValidationError, execute_analysis_template
from .services.calculators import affordability, investment_projection, loan_with_prepayment
from .services.chart_widgets import ChartSpecError, build_chart_widget, dataset_id
from .services.intelligence import IntelligenceResult, tool_facing_rows
from .services.manifest import native_manifest_fingerprint
from .services.semantic import AnalysisPlan, AnalysisToolProposal
from .visualization_contracts import VisualEncodingContract, VisualFieldEncoding, VisualizationView
from .services.adapters import CSVAdapter, MessageAdapter, import_summary
from .services.conversation import (
    get_or_create_conversation,
    persist_agent_response,
    prepare_widget_action,
    user_conversation,
)
from .services.preferences import (
    AnswerStyle,
    AnswerValidationMode,
    answer_style,
    answer_validation_mode,
    set_answer_style,
    set_answer_validation_mode,
    set_user_preference,
    user_preference,
)
from .services.reconciliation import ingest_observation
from .services.overview import overview_snapshot
from .services.proactive import current_insights
from .services.taxonomy import TaxonomyRepository
from .services.transactions import (
    UNSET,
    canonical_transactions,
    create_manual_transaction as record_manual_transaction,
    remove_transaction as remove_active_transaction,
    restore_transaction as restore_removed_transaction,
    transaction_log,
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
    supersede_open_interrupts,
)


router = APIRouter(prefix="/api")

# Enough to fill the history rail past the fold on a tall screen, so the first
# lazy page is fetched while scrolling rather than immediately on load.
CONVERSATION_PAGE_SIZE = 25
# Every dashboard view re-executes every tile through the governed harness and
# writes its audit rows, so the tile count is a hard cost boundary, not taste.
MAX_TILES_PER_DASHBOARD = 12
_agui_tasks: set[asyncio.Task] = set()


def _agent_mode(settings) -> str:
    return "llm" if settings.primary_agent_enabled and bool(settings.openai_api_key) else "deterministic_fallback"


def _agent_models(settings) -> dict[str, str] | None:
    if _agent_mode(settings) != "llm":
        return None
    return {
        "operator": settings.operator_model,
        "planner": settings.planner_model,
        "validator": settings.validator_model,
        "reconciler": settings.reconciler_model,
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
    from .operations import operation_catalog
    catalog_health = operation_catalog().health()
    return HealthOut.model_validate({
        "status": "degraded" if catalog_health.status != "ok" else "ok",
        "time": now_utc().isoformat(),
        "database": "postgresql" if settings.database_url.startswith("postgresql") else "sqlite",
        "agent_mode": _agent_mode(settings),
        "models": _agent_models(settings),
        "operationCatalog": catalog_health.model_dump(mode="json", by_alias=True, exclude_none=True),
    })


@router.get("/diagnostics/agent", response_model=AgentDiagnosticsOut)
def agent_diagnostics(db: Session = Depends(get_db), user: User = Depends(current_user)) -> AgentDiagnosticsOut:
    settings = get_settings()
    actions = list(db.scalars(
        select(AIAction)
        .where(
            AIAction.user_id == user.id,
            AIAction.action_type == "operator_decision",
        )
        .order_by(AIAction.created_at.desc())
        .limit(20)
    ))
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


@router.get("/insights", response_model=InsightsOut)
def insights(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> InsightsOut:
    """This user's proactive insights, every one of them replayed on this read.

    The response body is not a cache read: each claim is regenerated from
    canonical data and then recomputed from its own key, so a stored insight the
    data no longer supports is stamped stale and never appears here. The commit
    persists that verification outcome — a read of this page records what it
    found.
    """
    today = local_now(user.timezone).date()
    verified_at = now_utc()
    verified = current_insights(db, user, today, checked_at=verified_at)
    body = InsightsOut(
        insights=[
            InsightOut(
                id=row.id,
                kind=row.insight_type,
                subject=row.subject,
                headline=row.title,
                evidence=InsightEvidenceOut.model_validate(row.evidence),
                # Built field by field rather than validated from the stored
                # dict: the lineage is written in camelCase and the aliases here
                # are serialization-only, so a model_validate would read every
                # stamp as missing and answer a lineage of nulls.
                lineage=InsightLineageOut(
                    manifest_hash=row.lineage["manifestHash"],
                    traits_computed_at=row.lineage["traitsComputedAt"],
                    computed_at=row.lineage["computedAt"],
                ),
                recompute_key=row.recompute_key,
                # The column is nullable, but `current_insights` stamps every
                # row it returns with the timestamp it was handed. Falling back
                # to that same value states the invariant instead of assuming
                # it, and invents nothing if the guarantee ever moves.
                verified_at=as_utc(row.verified_at or verified_at).isoformat(),
            )
            for row in verified
        ],
        verified_at=verified_at.isoformat(),
    )
    # Serialized before the commit: committing expires the rows, and re-reading
    # them to build the body would issue a second query per insight.
    db.commit()
    return body


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


@router.patch("/conversations/{conversation_id}", response_model=ConversationSummaryOut)
def rename_conversation(
    conversation_id: UUID,
    request: ConversationRenameIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ConversationSummaryOut:
    """Set the thread's title. The auto-title from the first message is only a
    default; the user's explicit choice replaces it and is never overwritten."""
    conversation = _owned_conversation(db, user, conversation_id)
    conversation.title = request.title
    # Renaming is housekeeping, not activity. Pinning `updated_at` to its
    # current value (an explicitly-set column skips `onupdate`) keeps the
    # thread in place in the recency-ordered rail instead of sending it to
    # the top.
    flag_modified(conversation, "updated_at")
    db.commit()
    db.refresh(conversation)
    return ConversationSummaryOut.model_validate(conversation)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: UUID, db: Session = Depends(get_db), user: User = Depends(current_user)) -> Response:
    """Erase one thread and everything that points at it.

    Deleting a conversation removes the thread, not the money: transactions it
    recorded are canonical financial history and survive. Everything that only
    exists because the thread existed does not — its messages and their widgets,
    drafts that never became transactions, Operator's decision log, and the
    per-run analysis traces. Shared analysis templates and the user's saved
    template associations are independent of any one thread, so those stay.

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

    open_interrupts = list(
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
    if open_interrupts and input_payload.get("kind") != "resume":
        action = input_payload.get("action") if input_payload.get("kind") == "action" else None
        target_widget_id = (
            action.get("widgetId") or action.get("widget_id")
            if isinstance(action, dict)
            else None
        )
        target_origin = None
        if target_widget_id and isinstance(action, dict):
            try:
                target_origin = prepare_widget_action(
                    db,
                    conversation,
                    str(target_widget_id),
                    str(action.get("action") or ""),
                )
            except (KeyError, ValueError):
                target_origin = None
        # A server-authored action on a different, newer widget is an explicit
        # change of direction. This also repairs conversations persisted by an
        # older client that uploaded a statement without retiring the previous
        # clarification first.
        can_supersede = target_origin is not None and all(
            interrupt.widget_id != target_widget_id
            and as_utc(target_origin[0].created_at) > as_utc(interrupt.created_at)
            for interrupt in open_interrupts
        )
        if can_supersede:
            updates = supersede_open_interrupts(
                db,
                user,
                conversation,
                superseded_by=f"widget:{target_widget_id}",
            )
            input_payload["supersededWidgetUpdates"] = [
                update.model_dump(mode="json", by_alias=True) for update in updates
            ]
        else:
            input_payload = {
                "kind": "protocol_error",
                "message": "Finish or cancel the current card before starting another request.",
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
        widget_updates = supersede_open_interrupts(
            db,
            user,
            conversation,
            superseded_by="csv_upload",
        )
        result = import_summary(existing, idempotent_replay=True)
        return _record_import_preview(db, conversation, file.filename, result, widget_updates=widget_updates)
    try:
        rows = CSVAdapter().adapt(content, user.timezone)
    except (UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    widget_updates = supersede_open_interrupts(
        db,
        user,
        conversation,
        superseded_by="csv_upload",
    )
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
    return _record_import_preview(db, conversation, file.filename, result, widget_updates=widget_updates)


def _record_import_preview(
    db: Session,
    conversation: Conversation,
    filename: str,
    result: dict,
    *,
    widget_updates: list[WidgetUpdate] | None = None,
) -> ImportResultOut:
    user_message = Message(conversation_id=conversation.id, role="user", content=f"Uploaded {filename}", widgets=[], citations=[])
    db.add(user_message)
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
        deleted_at=as_utc(item.deleted_at) if item.deleted_at else None,
    )


@router.get("/transactions", response_model=list[TransactionListItemOut])
def transactions(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=160),
    transaction_type: TransactionType | None = Query(default=None),
    include_removed: bool = Query(default=True, description="Include soft-deleted records, flagged by deletedAt"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[TransactionListItemOut]:
    statement = (
        (transaction_log(user.id) if include_removed else canonical_transactions(user.id))
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
            latitude=request.latitude,
            longitude=request.longitude,
            location_accuracy=request.location_accuracy,
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
    # Written out rather than splatted from a dict. A `**changes` call is
    # uncheckable — nothing relates its keys to the parameters on the other
    # side — which is how three fields were once accepted by this endpoint's
    # schema and silently dropped before the row was written. Named arguments
    # make that a type error instead of a quiet omission.
    expense = request.transaction_type == TransactionType.EXPENSE
    try:
        transaction = apply_transaction_update(
            db,
            user.id,
            transaction_id,
            amount_minor=request.amount_minor,
            merchant=request.merchant,
            transaction_at=request.transaction_at,
            transaction_type=request.transaction_type,
            spend_nature=request.spend_nature,
            location=request.location,
            latitude=request.latitude,
            longitude=request.longitude,
            location_accuracy=request.location_accuracy,
            # Category payloads are meaningful only for expenses. UNSET for
            # every other direction leaves the stored taxonomy to be normalized
            # from the new type, so a stale hidden form value cannot preserve
            # the prior expense category through a type change.
            category_id=request.category_id if expense else UNSET,
            subcategory_id=request.subcategory_id if expense else UNSET,
        )
    except ValueError as error:
        status_code = 404 if str(error) == "Unknown transaction" else 422
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    db.commit()
    db.refresh(transaction)
    return _saved_transaction_item(db, user.id, transaction)


@router.delete("/transactions/{transaction_id}", response_model=TransactionListItemOut)
def remove_transaction(
    transaction_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> TransactionListItemOut:
    try:
        transaction = remove_active_transaction(db, user.id, transaction_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    db.commit()
    db.refresh(transaction)
    return _saved_transaction_item(db, user.id, transaction)


@router.post("/transactions/{transaction_id}/restore", response_model=TransactionListItemOut)
def restore_transaction(
    transaction_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> TransactionListItemOut:
    try:
        transaction = restore_removed_transaction(db, user.id, transaction_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
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


@router.get("/agent-settings", response_model=AgentSettingsOut)
def agent_settings(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> AgentSettingsOut:
    return AgentSettingsOut(
        answer_validation_mode=answer_validation_mode(db, user.id).value,
        answer_style=answer_style(db, user.id).value,
    )


@router.patch("/agent-settings", response_model=AgentSettingsOut)
def update_agent_settings(
    request: AgentSettingsIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> AgentSettingsOut:
    changed: dict[str, str] = {}
    if request.answer_validation_mode is not None:
        mode = AnswerValidationMode(request.answer_validation_mode)
        set_answer_validation_mode(db, user.id, mode)
        changed["answerValidationMode"] = mode.value
    if request.answer_style is not None:
        style = AnswerStyle(request.answer_style)
        set_answer_style(db, user.id, style)
        changed["answerStyle"] = style.value
    db.add(AuditLog(
        user_id=user.id,
        action="agent.settings_updated",
        entity_type="user_preference",
        metadata_redacted=changed,
    ))
    db.commit()
    return AgentSettingsOut(
        answer_validation_mode=answer_validation_mode(db, user.id).value,
        answer_style=answer_style(db, user.id).value,
    )


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


def _spreadsheet_source_out(source, manifest) -> SpreadsheetSourceOut:
    document = manifest.document
    stated = document.get("annotations", {}).get("fields", {})
    roles = document["semantics"]["columns"]
    return SpreadsheetSourceOut(
        source_id=source.id,
        name=source.name,
        manifest_version=manifest.version,
        row_count=document["physical"]["row_count"],
        columns=[
            SpreadsheetColumnDraftOut(
                name=column["name"],
                inferred_type=column["type"],
                role=(
                    stated.get(column["name"], {}).get("role")
                    or roles.get(column["name"], {}).get("role", "text")
                ),
                confidence=(
                    1.0 if stated.get(column["name"], {}).get("role")
                    else roles.get(column["name"], {}).get("confidence", 0.0)
                ),
                user_stated=stated.get(column["name"], {}).get("statement"),
            )
            for column in document["physical"]["columns"]
        ],
    )


@router.post("/sources/spreadsheet", response_model=SpreadsheetSourceOut)
async def upload_spreadsheet_source(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> SpreadsheetSourceOut:
    """Store a raw tabular source and draft its manifest for confirmation.

    Deliberately distinct from /imports/csv: nothing here becomes canonical
    transactions. The sheet stays a foreign source with its own manifest.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=415, detail="Upload a CSV file; xlsx arrives with a later phase")
    content = await file.read(CSV_UPLOAD_MAX_BYTES + 1)
    if len(content) > CSV_UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="CSV is limited to 10 MB")
    try:
        text_content = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise HTTPException(status_code=422, detail="CSV must be UTF-8 encoded") from error
    try:
        dialect = csv.Sniffer().sniff(text_content[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    parsed = [row for row in csv.reader(io.StringIO(text_content), dialect)]
    if not parsed:
        raise HTTPException(status_code=422, detail="The file has no rows")
    headers, rows = [cell.strip() for cell in parsed[0]], parsed[1:]
    try:
        source, manifest = ensure_spreadsheet_manifest(
            db, user, (name or file.filename).strip()[:120], headers, rows
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _spreadsheet_source_out(source, manifest)


@router.post("/sources/spreadsheet/{source_id}/annotations", response_model=SourceAnnotationsOut)
def annotate_spreadsheet_source(
    source_id: UUID,
    request: SourceAnnotationsIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> SourceAnnotationsOut:
    manifest = None
    for item in request.annotations:
        try:
            manifest = annotate_source_field(
                db, user, source_id, item.field, item.statement, role=item.role
            )
        except ValueError as error:
            if "unknown_source" in str(error):
                raise HTTPException(status_code=404, detail="Source not found") from error
            raise HTTPException(status_code=422, detail=str(error)) from error
    if manifest is None:
        # Unreachable while SourceAnnotationsIn requires at least one
        # annotation. Stated here anyway: the loop below is the only thing that
        # binds `manifest`, and that requirement lives in another file.
        raise HTTPException(status_code=422, detail="At least one annotation is required")
    return SourceAnnotationsOut(
        source_id=source_id,
        manifest_version=manifest.version,
        annotated_fields=[item.field for item in request.annotations],
    )


# --- Live dashboards -----------------------------------------------------------
# A tile stores a bound AnalysisToolProposal, never a result: every dashboard
# read re-executes each plan through the governed harness, so the numbers on
# the page are exactly as fresh as the ledger. One broken tile degrades to its
# own typed error and never takes the page down with it.


def _owned_dashboard(db: Session, user: User, dashboard_id: UUID) -> Dashboard:
    dashboard = db.scalar(select(Dashboard).where(
        Dashboard.id == dashboard_id,
        Dashboard.user_id == user.id,
    ))
    if not dashboard:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return dashboard


def _synthesized_tile_view(result: dict) -> VisualizationView:
    """Deterministic fallback chart: a bar over the first grouped dimension."""
    dimensions = list(result.get("dimensions") or [])
    if not dimensions:
        raise ChartSpecError(
            "The first query result has no grouped dimension to draw a bar over",
            code="unknown_field",
        )
    is_count = "count" in str(result.get("metric", ""))
    return VisualizationView(
        id="tile_default_view",
        title=str(result.get("name") or "Analysis result")[:160],
        dataset=dataset_id(str(result.get("name") or "")),
        mark="bar",
        encoding=VisualEncodingContract(
            x=VisualFieldEncoding(field=dimensions[0], type="nominal", value_type="category"),
            y=VisualFieldEncoding(
                field="value" if is_count else "value_minor",
                type="quantitative",
                value_type="number" if is_count else "money_minor",
            ),
        ),
    )


def _tile_chart(plan: AnalysisPlan, result: IntelligenceResult, fallback_currency: str) -> dict:
    """Bind the tile's one chart to the rows this execution just produced."""
    if not result.query_results:
        raise ChartSpecError(
            "The analysis produced no query results to draw",
            code="empty_result",
        )
    by_dataset: dict[str, dict] = {}
    for item in result.query_results:
        name = str(item.get("name") or "")
        by_dataset.setdefault(name, item)
        by_dataset.setdefault(dataset_id(name), item)
    if plan.visualizations:
        view = plan.visualizations[0]
        dataset = by_dataset.get(view.dataset)
        if dataset is None:
            raise ChartSpecError(
                f"View {view.id} references dataset {view.dataset}, which matches no plan query",
                code="unknown_dataset",
            )
    else:
        dataset = result.query_results[0]
        view = _synthesized_tile_view(dataset)
    widget = build_chart_widget(
        view,
        tool_facing_rows(dataset),
        dataset.get("currency") or fallback_currency,
        {
            "origin": "dashboard",
            "manifestHash": native_manifest_fingerprint(),
            "executedAt": now_utc().isoformat(),
        },
    )
    return widget.data


def _executed_tile(db: Session, user: User, tile: DashboardTile, today: date) -> DashboardTileOut:
    """Re-execute one stored tile. A failing tile reports; it never raises."""
    executed_at = now_utc().isoformat()
    chart: dict | None = None
    error: DashboardTileErrorOut | None = None
    spec = tile.spec if isinstance(tile.spec, dict) else {}
    try:
        proposal: AnalysisToolProposal | None = None
        if spec.get("kind") != "plan":
            error = DashboardTileErrorOut(
                code="invalid_analysis_plan",
                detail="The stored tile spec is not a plan tile.",
            )
        else:
            try:
                proposal = AnalysisToolProposal.model_validate(spec.get("proposal"))
            except ValidationError as validation_error:
                error = DashboardTileErrorOut(
                    code="invalid_analysis_plan",
                    detail="; ".join(
                        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                        for item in validation_error.errors()[:8]
                    ),
                )
        if proposal is not None:
            outcome = execute_analysis_template(db, user.id, None, today, proposal)
            chart = _tile_chart(proposal.plan, outcome.result, user.currency)
    except HarnessValidationError as harness_error:
        error = DashboardTileErrorOut(code=harness_error.error_code, detail=str(harness_error))
    except ChartSpecError as chart_error:
        error = DashboardTileErrorOut(code=chart_error.code, detail=str(chart_error))
    except Exception as execution_error:  # noqa: BLE001 - a tile is a container:
        # one poisoned tile must report, never take the page or its siblings down.
        error = DashboardTileErrorOut(
            code="tile_execution_error",
            detail=f"{type(execution_error).__name__}: {execution_error}",
        )
    if error is not None:
        # The harness flags its FAILED run row before re-raising; committing
        # here keeps that audit trace durable instead of letting the request
        # teardown roll it back (loud-failure law).
        db.commit()
    return DashboardTileOut(
        id=tile.id,
        title=tile.title,
        position=tile.position,
        executed_at=executed_at,
        chart=chart,
        error=error,
    )


@router.post("/dashboards", response_model=DashboardCreatedOut)
def create_dashboard(
    request: DashboardCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> DashboardCreatedOut:
    dashboard = Dashboard(user_id=user.id, name=request.name)
    db.add(dashboard)
    db.commit()
    return DashboardCreatedOut(id=dashboard.id, name=dashboard.name)


@router.get("/dashboards", response_model=DashboardListOut)
def list_dashboards(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> DashboardListOut:
    tile_counts: dict[UUID, int] = dict(db.execute(  # type: ignore[arg-type]  # Row is a tuple at runtime; the annotation is the real shape
        select(DashboardTile.dashboard_id, func.count(DashboardTile.id))
        .where(DashboardTile.user_id == user.id)
        .group_by(DashboardTile.dashboard_id)
    ).all())
    dashboards = db.scalars(
        select(Dashboard)
        .where(Dashboard.user_id == user.id)
        .order_by(Dashboard.created_at, Dashboard.id)
    ).all()
    return DashboardListOut(dashboards=[
        DashboardSummaryOut(id=item.id, name=item.name, tile_count=tile_counts.get(item.id, 0))
        for item in dashboards
    ])


@router.post("/dashboards/{dashboard_id}/tiles", response_model=DashboardTileCreatedOut)
def add_dashboard_tile(
    dashboard_id: UUID,
    request: DashboardTileCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> DashboardTileCreatedOut:
    dashboard = _owned_dashboard(db, user, dashboard_id)
    # A tile that cannot even parse must be refused at the door; the page-level
    # error object exists for specs that rot after storage, not for bad input.
    try:
        proposal = AnalysisToolProposal.model_validate(request.proposal)
    except ValidationError as error:
        raise HTTPException(
            status_code=422,
            detail="invalid_analysis_plan: the proposal does not satisfy the AnalysisToolProposal contract",
        ) from error
    # Refuse tiles that can never render, instead of storing a permanent
    # per-view error: dedicated analysis types return no query results to
    # draw, and unmet missing_information fails validation on every read.
    if proposal.plan.analysis_type != "semantic_query":
        raise HTTPException(
            status_code=422,
            detail="not_chartable: only semantic_query plans produce chartable query results",
        )
    if proposal.plan.missing_information:
        raise HTTPException(
            status_code=422,
            detail="not_chartable: the plan declares missing_information and would fail on every view",
        )
    tile_count = db.scalar(
        select(func.count(DashboardTile.id)).where(DashboardTile.dashboard_id == dashboard.id)
    )
    if int(tile_count or 0) >= MAX_TILES_PER_DASHBOARD:
        raise HTTPException(
            status_code=422,
            detail=f"dashboard_full: a dashboard re-executes every tile per view and is capped at {MAX_TILES_PER_DASHBOARD} tiles",
        )
    if request.position is not None:
        position = request.position
    else:
        top = db.scalar(
            select(func.max(DashboardTile.position))
            .where(DashboardTile.dashboard_id == dashboard.id)
        )
        position = 0 if top is None else top + 1
    tile = DashboardTile(
        user_id=user.id,
        dashboard_id=dashboard.id,
        title=request.title,
        position=position,
        spec={"kind": "plan", "proposal": proposal.model_dump(mode="json")},
    )
    db.add(tile)
    db.commit()
    return DashboardTileCreatedOut(
        id=tile.id,
        dashboard_id=tile.dashboard_id,
        title=tile.title,
        position=tile.position,
    )


@router.get("/dashboards/{dashboard_id}", response_model=DashboardOut)
def get_dashboard(
    dashboard_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> DashboardOut:
    dashboard = _owned_dashboard(db, user, dashboard_id)
    today = local_now(user.timezone).date()
    tiles = db.scalars(
        select(DashboardTile)
        .where(
            DashboardTile.dashboard_id == dashboard.id,
            DashboardTile.user_id == user.id,
        )
        .order_by(DashboardTile.position, DashboardTile.created_at)
    ).all()
    rendered = [_executed_tile(db, user, tile, today) for tile in tiles]
    # The executions above wrote their durable audit rows (analysis_tool_runs,
    # template usage counters); a read of the page still commits that trail.
    db.commit()
    return DashboardOut(id=dashboard.id, name=dashboard.name, tiles=rendered)


@router.delete("/dashboards/{dashboard_id}/tiles/{tile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dashboard_tile(
    dashboard_id: UUID,
    tile_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    dashboard = _owned_dashboard(db, user, dashboard_id)
    tile = db.scalar(select(DashboardTile).where(
        DashboardTile.id == tile_id,
        DashboardTile.dashboard_id == dashboard.id,
        DashboardTile.user_id == user.id,
    ))
    if not tile:
        raise HTTPException(status_code=404, detail="Dashboard tile not found")
    db.delete(tile)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
