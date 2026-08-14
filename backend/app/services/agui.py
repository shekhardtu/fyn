from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from ag_ui.core import (
    ActivitySnapshotEvent,
    AgentCapabilities,
    CustomEvent,
    Interrupt,
    MessagesSnapshotEvent,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings
from ..domain import AgentInterruptStatus, AgentRunStatus, DraftState, ExecutionStatus, WidgetActionId
from ..event_time import now_utc
from ..models import AgentEvent, AgentInterrupt, AgentRun, Conversation, Message, TransactionDraft, User
from ..schemas import (
    ACTION_PAYLOAD_MODELS,
    ActionRequest,
    AgentResponse,
    ChatRequest,
    Widget,
    WidgetLifecycle,
    WidgetType,
)
from .conversation import (
    handle_action,
    handle_chat,
    handle_clarification_resolution,
    persist_agent_response,
    prepare_widget_action,
    prepare_widget_cancellation,
    resolve_widget_action,
)


FYN_RESPONSE_EVENT = "fyn.response.v1"
FYN_ACTIVITY_TYPE = "fyn.agent_activity.v1"
FYN_ACTION_TOOL = "fyn.widget_action"

ACTIVE_RUN_STATUSES = frozenset({AgentRunStatus.QUEUED.value, AgentRunStatus.RUNNING.value})
TERMINAL_RUN_STATUSES = frozenset(
    {
        AgentRunStatus.INTERRUPTED.value,
        AgentRunStatus.SUCCEEDED.value,
        AgentRunStatus.FAILED.value,
        AgentRunStatus.CANCELLED.value,
    }
)


def _one_line_reasoning(value: str, limit: int = 320) -> str:
    """Make streamed reasoning readable as one stable transcript line."""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _activity_reasoning_summary(steps: list[dict[str, Any]]) -> str:
    """Choose the decision behind a run, never a transport lifecycle label."""
    for step in reversed(steps):
        if step.get("id") == "classification" and step.get("detail"):
            return _one_line_reasoning(str(step["detail"]))
    for step in reversed(steps):
        if step.get("detail"):
            return _one_line_reasoning(str(step["detail"]))
    for step in reversed(steps):
        if step.get("id") != "request" and step.get("label"):
            return _one_line_reasoning(str(step["label"]))
    return "Preparing a contextual answer"


def _record_activity_event(
    activities: dict[str, dict[str, Any]],
    open_activity_ids: dict[str, str],
    occurrence_counts: dict[str, int],
    event: dict[str, Any],
) -> dict[str, Any]:
    """Give every logical activity occurrence a stable trace identity.

    A running/completed pair replaces one live row. A later occurrence of the
    same stage receives a suffixed id instead of erasing the earlier decision.
    When a model stage finishes by selecting a domain tool, retain both names:
    the model remains the internal tool and its selected tool is the result.
    """
    stage_id = str(event["id"])
    status = str(event.get("status", ""))

    def allocate_id() -> str:
        occurrence = occurrence_counts.get(stage_id, 0) + 1
        occurrence_counts[stage_id] = occurrence
        return stage_id if occurrence == 1 else f"{stage_id}-{occurrence}"

    if status == ExecutionStatus.RUNNING.value:
        trace_id = open_activity_ids.get(stage_id)
        if trace_id is None:
            trace_id = allocate_id()
            open_activity_ids[stage_id] = trace_id
    else:
        trace_id = open_activity_ids.pop(stage_id, None) or allocate_id()

    traced = dict(event)
    traced["id"] = trace_id
    traced["stageId"] = stage_id
    previous = activities.get(trace_id)
    prior_elapsed = max(
        (float(item.get("cumulativeMs", 0)) for key, item in activities.items() if key != trace_id),
        default=0.0,
    )
    reported_elapsed = float(traced.get("cumulativeMs", 0))
    if status == ExecutionStatus.RUNNING.value:
        # Some stages are timed inside a nested operation and report elapsed
        # time relative to that operation. At the trace boundary cumulative
        # always means elapsed since the beginning of the complete run.
        traced["cumulativeMs"] = max(reported_elapsed, prior_elapsed)
    elif previous:
        stage_started_at = float(previous.get("cumulativeMs", prior_elapsed))
        traced["cumulativeMs"] = max(
            reported_elapsed,
            stage_started_at + float(traced.get("durationMs", 0)),
        )
    else:
        traced["cumulativeMs"] = max(reported_elapsed, prior_elapsed)
    if previous:
        started_tool = str(previous.get("tool") or "").strip()
        finished_tool = str(traced.get("tool") or "").strip()
        if started_tool and finished_tool and started_tool != finished_tool:
            traced["tool"] = started_tool
            traced["resultTool"] = finished_tool
        elif started_tool and not finished_tool:
            traced["tool"] = started_tool
    activities[trace_id] = traced
    return traced


class RunCancelled(Exception):
    pass


class InvalidAgentInput(ValueError):
    pass


class ProtocolRunError(ValueError):
    """A valid AG-UI run that must terminate with a protocol-level RunError."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def timestamp_ms() -> int:
    return int(now_utc().timestamp() * 1000)


def event_payload(event: Any) -> dict[str, Any]:
    return event.model_dump(mode="json", by_alias=True, exclude_none=True)


def sse_event(sequence: int, payload: dict[str, Any]) -> str:
    """Encode one ordered AG-UI event using the protocol's data-only SSE shape."""
    wire_payload = dict(payload)
    raw_event = dict(wire_payload.get("rawEvent") or {})
    raw_event["fyn"] = {
        "sequence": sequence,
        "replaySafe": wire_payload.get("type")
        not in {
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TOOL_CALL_START",
            "TOOL_CALL_ARGS",
            "REASONING_START",
            "REASONING_MESSAGE_START",
            "REASONING_MESSAGE_CONTENT",
            "REASONING_MESSAGE_END",
        },
    }
    wire_payload["rawEvent"] = raw_event
    return f"id: {sequence}\ndata: {json.dumps(wire_payload, separators=(',', ':'), default=str)}\n\n"


class DurableEventPublisher:
    """Publish live events and retain them in sequence for later replay.

    Events never cross the live boundary before their database commit. PostgreSQL
    can flush progress independently while the finance transaction is open;
    SQLite buffers those progress events until the governed turn releases its
    connection. Terminal events and terminal run state commit atomically.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        run_id: UUID,
        user_id: UUID,
        start_sequence: int,
        publish_live: Callable[[int, dict[str, Any]], None],
    ) -> None:
        self._session_factory = session_factory
        self._run_id = run_id
        self._user_id = user_id
        self._sequence = start_sequence
        self._pending: list[tuple[int, dict[str, Any]]] = []
        self._publish_live = publish_live
        self._final_message_id: UUID | None = None
        self._delivery_mode: str | None = None

    @property
    def supports_incremental_flush(self) -> bool:
        bind = self._session_factory.kw.get("bind")
        return bool(bind is not None and bind.dialect.name != "sqlite")

    @property
    def sequence(self) -> int:
        return self._sequence

    def emit(self, event: Any) -> int:
        self._sequence += 1
        payload = event_payload(event)
        self._pending.append((self._sequence, payload))
        return self._sequence

    def bind_final_message(self, message_id: UUID) -> None:
        """Link this execution to the canonical, complete assistant reply."""
        self._final_message_id = message_id

    def bind_delivery_mode(self, delivery_mode: str) -> None:
        self._delivery_mode = delivery_mode

    def _commit(self, terminal_status: AgentRunStatus | None = None, *, error_code: str | None = None) -> None:
        if not self._pending:
            if terminal_status is not None:
                _update_run(
                    self._session_factory,
                    self._run_id,
                    self._user_id,
                    terminal_status,
                    error_code=error_code,
                )
            return
        committed = list(self._pending)
        with self._session_factory() as db:
            run = db.scalar(
                select(AgentRun).where(AgentRun.id == self._run_id, AgentRun.user_id == self._user_id)
            )
            if not run:
                raise ValueError("Agent run no longer exists")
            for sequence, payload in committed:
                db.add(
                    AgentEvent(
                        run_id=run.id,
                        sequence=sequence,
                        event_type=str(payload["type"]),
                        payload=payload,
                    )
                )
            run.last_sequence = committed[-1][0]
            if self._final_message_id is not None:
                run.final_message_id = self._final_message_id
            if self._delivery_mode is not None:
                run.delivery_mode = self._delivery_mode
            if run.first_response_at is None:
                first_response = next(
                    (
                        payload
                        for _sequence, payload in committed
                        if payload.get("type") == "TEXT_MESSAGE_CONTENT"
                    ),
                    None,
                )
                if first_response is not None:
                    raw_timestamp = first_response.get("timestamp")
                    run.first_response_at = (
                        datetime.fromtimestamp(float(raw_timestamp) / 1000, tz=timezone.utc)
                        if isinstance(raw_timestamp, (int, float))
                        else now_utc()
                    )
            if terminal_status is not None:
                run.status = terminal_status.value
                run.finished_at = now_utc()
                run.error_code = error_code
            db.commit()
        del self._pending[: len(committed)]
        for sequence, payload in committed:
            self._publish_live(sequence, payload)

    def flush(self) -> None:
        self._commit()

    def finish(self, status: AgentRunStatus, *, error_code: str | None = None) -> None:
        if status.value not in TERMINAL_RUN_STATUSES:
            raise ValueError("finish requires a terminal agent run status")
        self._commit(status, error_code=error_code)

    def cancellation_requested(self) -> bool:
        bind = self._session_factory.kw.get("bind")
        # SQLite's development/test pools commonly hand nested sessions the
        # same DB-API connection. Reading from a second session while a finance
        # turn owns that connection could roll back the outer transaction when
        # the reader closes. Queued runs still cancel immediately; an active
        # SQLite run stops at its next safe outer boundary.
        if bind is not None and bind.dialect.name == "sqlite":
            return False
        with self._session_factory() as db:
            return bool(
                db.scalar(
                    select(AgentRun.cancel_requested).where(
                        AgentRun.id == self._run_id,
                        AgentRun.user_id == self._user_id,
                    )
                )
            )


def normalize_run_input(value: RunAgentInput) -> tuple[dict[str, Any], str | None]:
    """Reduce untrusted AG-UI input to a governed Fyn command.

    Client state, conversation history, context and tool declarations are never
    treated as authority. Only the newest user text, a typed Fyn action, or an
    AG-UI interrupt resume crosses into the finance application layer.
    """
    if value.resume:
        return {
            "kind": "resume",
            "entries": [entry.model_dump(mode="json", by_alias=True) for entry in value.resume],
        }, None

    forwarded = value.forwarded_props if isinstance(value.forwarded_props, dict) else {}
    raw_action = forwarded.get("fynAction")
    if raw_action is not None:
        if not isinstance(raw_action, dict):
            raise InvalidAgentInput("fynAction must be an object")
        action = _action_request({**raw_action, "conversation_id": value.thread_id})
        return {
            "kind": "action",
            "action": action.model_dump(mode="json", by_alias=True),
        }, None

    user_message = next((message for message in reversed(value.messages) if message.role == "user"), None)
    if user_message is None:
        raise InvalidAgentInput("A new user message, Fyn action, or interrupt resume is required")
    content = user_message.content
    if isinstance(content, str):
        text = content
    else:
        text_parts: list[str] = []
        for part in content:
            if getattr(part, "type", None) != "text":
                raise InvalidAgentInput("This Fyn endpoint does not yet accept non-text AG-UI message parts")
            text_parts.append(str(part.text))
        text = "\n".join(text_parts)
    validated = ChatRequest(text=text, conversation_id=UUID(value.thread_id))
    return {
        "kind": "message",
        "text": validated.text,
        "messageId": user_message.id,
    }, user_message.id


def _owned_run(db: Session, run_id: UUID, user_id: UUID) -> AgentRun:
    run = db.scalar(select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == user_id))
    if not run:
        raise ValueError("Agent run not found")
    return run


def _owned_conversation(db: Session, conversation_id: UUID, user_id: UUID) -> Conversation:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
    )
    if not conversation:
        raise ValueError("Conversation not found")
    return conversation


def _action_request(command: dict[str, Any]) -> ActionRequest:
    """Accept Fyn's camel-case AG-UI command while retaining the API model."""
    normalized = {
        "conversation_id": command.get("conversation_id") or command.get("conversationId"),
        "widget_id": command.get("widget_id") or command.get("widgetId"),
        "action": command.get("action"),
        "payload": command.get("payload") or {},
        "completeWidget": command.get("completeWidget", command.get("complete_widget", True)),
    }
    return ActionRequest.model_validate(normalized)


def _wait_for_blocker(
    session_factory: sessionmaker[Session],
    run_id: UUID,
    user_id: UUID,
    *,
    timeout_seconds: int = 300,
) -> None:
    started = time.monotonic()
    while True:
        with session_factory() as db:
            run = _owned_run(db, run_id, user_id)
            if run.cancel_requested:
                raise RunCancelled
            if run.blocked_by_run_id is None:
                return
            blocker_status = db.scalar(
                select(AgentRun.status).where(
                    AgentRun.id == run.blocked_by_run_id,
                    AgentRun.user_id == user_id,
                )
            )
            if blocker_status is None or blocker_status in TERMINAL_RUN_STATUSES:
                return
        if time.monotonic() - started >= timeout_seconds:
            raise TimeoutError("The preceding agent run did not finish in time")
        time.sleep(0.25)


def _update_run(
    session_factory: sessionmaker[Session],
    run_id: UUID,
    user_id: UUID,
    status: AgentRunStatus,
    *,
    error_code: str | None = None,
) -> None:
    with session_factory() as db:
        run = _owned_run(db, run_id, user_id)
        now = now_utc()
        run.status = status.value
        if status is AgentRunStatus.RUNNING:
            run.started_at = now
        if status.value in TERMINAL_RUN_STATUSES:
            run.finished_at = now
        run.error_code = error_code
        db.commit()


def _action_response(
    db: Session,
    user: User,
    conversation: Conversation,
    command: dict[str, Any],
) -> AgentResponse:
    request = _action_request(command)
    if request.conversation_id != conversation.id:
        raise ValueError("Widget action belongs to a different conversation")
    origin = prepare_widget_action(db, conversation, request.widget_id, request.action)
    response = handle_action(db, user, conversation, request.action, request.payload)
    if request.complete_widget:
        lifecycle = WidgetLifecycle.CANCELLED if request.action.value.startswith("cancel_") else WidgetLifecycle.COMPLETED
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


def execute_widget_action(
    request: ActionRequest,
    db: Session,
    user: User,
) -> AgentResponse:
    """Exercise Fyn's governed action path without exposing a legacy route."""
    conversation = _owned_conversation(db, request.conversation_id, user.id)
    return _action_response(
        db,
        user,
        conversation,
        request.model_dump(mode="json", by_alias=True),
    )


def _cancelled_interrupt_response(
    db: Session,
    conversation: Conversation,
    interrupt: AgentInterrupt,
) -> AgentResponse:
    origin = prepare_widget_cancellation(db, conversation, interrupt.widget_id)
    if origin:
        widget = origin[2]
        draft_id = widget.data.get("draftId")
        if draft_id:
            try:
                draft_uuid = UUID(str(draft_id))
            except ValueError:
                draft_uuid = None
            draft = db.scalar(select(TransactionDraft).where(
                TransactionDraft.id == draft_uuid,
                TransactionDraft.conversation_id == conversation.id,
            )) if draft_uuid else None
            if draft and draft.state != DraftState.COMMITTED.value:
                draft.state = DraftState.CANCELLED.value
    response = persist_agent_response(db, conversation, "No changes were made.", commit=False)
    update = resolve_widget_action(
        db,
        origin,
        lifecycle=WidgetLifecycle.CANCELLED,
        action="cancel_interrupt",
        payload={},
    )
    if update:
        response.widget_updates.append(update)
    return response


def _clarification_option_cancels(option_id: str, option: dict[str, Any]) -> bool:
    """Recognize typed cancellation and old server-authored cancel choices."""
    disposition = str(option.get("disposition") or "").strip().casefold()
    if disposition:
        return disposition == "cancel"
    # Clarifications persisted before disposition existed used semantic option
    # ids such as `cancel_transfer`. The id is read from the server's durable
    # continuation map, never trusted directly from the client.
    return option_id.casefold() == "cancel" or option_id.casefold().startswith("cancel_")


def _resume_entry_payload(entry: dict[str, Any]) -> dict[str, Any] | None:
    payload = entry.get("payload")
    return payload if isinstance(payload, dict) else None


def _response_from_resolving_run(db: Session, interrupt: AgentInterrupt) -> AgentResponse:
    if interrupt.resolved_by_run_id is None:
        raise ProtocolRunError("The interrupt has no recorded resolution run.", "invalid_resume")
    events = list(
        db.scalars(
            select(AgentEvent)
            .where(
                AgentEvent.run_id == interrupt.resolved_by_run_id,
                AgentEvent.event_type == "CUSTOM",
            )
            .order_by(AgentEvent.sequence.desc())
        )
    )
    for event in events:
        payload = event.payload
        if payload.get("name") == FYN_RESPONSE_EVENT:
            value = payload.get("value")
            if isinstance(value, dict) and isinstance(value.get("response"), dict):
                return AgentResponse.model_validate(value["response"])
    raise ProtocolRunError("The prior interrupt resolution is not replayable.", "resume_replay_unavailable")


def _replay_resolved_resume(
    db: Session,
    conversation: Conversation,
    entries: list[dict[str, Any]],
) -> tuple[AgentResponse, list[AgentInterrupt]]:
    ids = [str(entry.get("interruptId")) for entry in entries]
    if len(ids) != len(set(ids)):
        raise ProtocolRunError("A resume cannot contain duplicate interrupt ids.", "invalid_resume")
    try:
        interrupt_ids = [UUID(value) for value in ids]
    except ValueError as error:
        raise ProtocolRunError("The resume references an invalid interrupt id.", "unknown_interrupt") from error
    interrupts = list(
        db.scalars(
            select(AgentInterrupt)
            .join(AgentRun, AgentRun.id == AgentInterrupt.run_id)
            .where(
                AgentRun.conversation_id == conversation.id,
                AgentInterrupt.id.in_(interrupt_ids),
            )
        )
    )
    by_id = {str(interrupt.id): interrupt for interrupt in interrupts}
    if set(ids) != set(by_id):
        raise ProtocolRunError("The resume references an unknown interrupt.", "unknown_interrupt")
    resolving_runs: set[UUID] = set()
    for entry in entries:
        interrupt = by_id[str(entry.get("interruptId"))]
        requested_status = entry.get("status")
        if requested_status == "cancelled":
            same = interrupt.status == AgentInterruptStatus.CANCELLED.value and entry.get("payload") is None
        else:
            same = (
                requested_status == "resolved"
                and interrupt.status == AgentInterruptStatus.RESOLVED.value
                and _resume_entry_payload(entry) == interrupt.response_payload
            )
        if not same or interrupt.resolved_by_run_id is None:
            raise ProtocolRunError(
                "This interrupt was already resolved with a different response.",
                "resume_conflict",
            )
        resolving_runs.add(interrupt.resolved_by_run_id)
    if len(resolving_runs) != 1:
        raise ProtocolRunError("The resume does not identify one prior resolution.", "resume_conflict")
    first = interrupts[0]
    return _response_from_resolving_run(db, first), interrupts


def _resume_response(
    db: Session,
    user: User,
    conversation: Conversation,
    run: AgentRun,
    entries: list[dict[str, Any]],
    activity_callback: Callable[[dict[str, Any]], None] | None = None,
    text_delta_callback: Callable[[UUID, str], None] | None = None,
    reasoning_delta_callback: Callable[[str], None] | None = None,
) -> tuple[AgentResponse, list[AgentInterrupt]]:
    entry_ids = [str(entry.get("interruptId")) for entry in entries]
    if len(entry_ids) != len(set(entry_ids)):
        raise ProtocolRunError("A resume cannot contain duplicate interrupt ids.", "invalid_resume")
    open_interrupts = list(
        db.scalars(
            select(AgentInterrupt)
            .join(AgentRun, AgentRun.id == AgentInterrupt.run_id)
            .where(
                AgentRun.user_id == user.id,
                AgentRun.conversation_id == conversation.id,
                AgentInterrupt.status == AgentInterruptStatus.OPEN.value,
            )
            .order_by(AgentInterrupt.created_at, AgentInterrupt.id)
        )
    )
    by_id = {str(interrupt.id): interrupt for interrupt in open_interrupts}
    supplied = {str(entry.get("interruptId")): entry for entry in entries}
    if not by_id:
        return _replay_resolved_resume(db, conversation, entries)
    if set(supplied) != set(by_id):
        raise ProtocolRunError(
            "A resume must address every open interrupt in this conversation.",
            "incomplete_resume",
        )
    if len(open_interrupts) != 1:
        raise ProtocolRunError(
            "fyn AI currently resolves one interactive widget at a time.",
            "unsupported_interrupt_set",
        )

    interrupt = open_interrupts[0]
    entry = supplied[str(interrupt.id)]
    if interrupt.expires_at is not None and interrupt.expires_at <= now_utc():
        raise ProtocolRunError("This request has expired. Please start again.", "interrupt_expired")
    if entry.get("status") == "cancelled":
        if entry.get("payload") is not None:
            raise ProtocolRunError("A cancelled resume must omit its payload.", "invalid_resume")
        response = _cancelled_interrupt_response(db, conversation, interrupt)
        interrupt.status = AgentInterruptStatus.CANCELLED.value
        interrupt.response_payload = None
    elif interrupt.reason == "clarification":
        payload = entry.get("payload")
        if not isinstance(payload, dict) or payload.get("approved") is not True:
            raise ProtocolRunError("Choose one clarification option to continue.", "invalid_resume")
        if set(payload) - {"approved", "editedArgs"}:
            raise ProtocolRunError("The clarification response contains unsupported fields.", "invalid_resume")
        edited_args = payload.get("editedArgs")
        if not isinstance(edited_args, dict) or set(edited_args) - {"widgetId", "action", "payload", "completeWidget"}:
            raise ProtocolRunError("The clarification response is incomplete.", "invalid_resume")
        if edited_args.get("widgetId") != interrupt.widget_id or edited_args.get("action") != WidgetActionId.RESOLVE_CLARIFICATION.value:
            raise ProtocolRunError("The clarification response targets a different request.", "invalid_resume")
        try:
            selected = ACTION_PAYLOAD_MODELS[WidgetActionId.RESOLVE_CLARIFICATION].model_validate(
                edited_args.get("payload") or {}
            ).model_dump(mode="json", by_alias=True, exclude_unset=True)
        except ValueError as error:
            raise ProtocolRunError(str(error), "invalid_resume_payload") from error
        continuation = interrupt.metadata_payload.get("continuation")
        if not isinstance(continuation, dict):
            raise ProtocolRunError("The clarification continuation is unavailable.", "invalid_resume")
        if selected.get("clarificationId") != continuation.get("clarificationId"):
            raise ProtocolRunError("The clarification response targets a different request.", "invalid_resume")
        option_id = str(selected.get("optionId") or "")
        options = continuation.get("options")
        if not isinstance(options, dict):
            raise ProtocolRunError("The clarification choices are unavailable.", "invalid_resume")
        if option_id == "custom":
            custom_text = str(selected.get("customText") or "").strip()
            if not continuation.get("allowCustom") or not custom_text:
                raise ProtocolRunError("Enter the clarification you want fyn AI to use.", "invalid_resume_payload")
            selected_label = "Customer-provided clarification"
            resolution = custom_text
        else:
            option = options.get(option_id)
            if not isinstance(option, dict):
                raise ProtocolRunError("Choose one of the available clarification options.", "invalid_resume_payload")
            selected_label = str(option.get("label") or option_id)
            resolution = str(option.get("resolution") or "").strip()
            if not resolution:
                raise ProtocolRunError("The selected clarification has no continuation.", "invalid_resume")
        try:
            source_message_id = UUID(str(continuation.get("sourceMessageId")))
        except ValueError as error:
            raise ProtocolRunError("The original request is unavailable.", "invalid_resume") from error
        origin = prepare_widget_action(
            db,
            conversation,
            interrupt.widget_id,
            WidgetActionId.RESOLVE_CLARIFICATION.value,
        )
        if option_id != "custom" and _clarification_option_cancels(option_id, option):
            response = persist_agent_response(
                db,
                conversation,
                "The request was cancelled. No changes were made.",
                commit=False,
            )
            lifecycle = WidgetLifecycle.CANCELLED
        else:
            response = handle_clarification_resolution(
                db,
                user,
                conversation,
                original_request=str(continuation.get("originalRequest") or ""),
                selected_label=selected_label,
                resolution=resolution,
                source_message_id=source_message_id,
                activity_callback=activity_callback,
                text_delta_callback=text_delta_callback,
                reasoning_delta_callback=reasoning_delta_callback,
            )
            lifecycle = WidgetLifecycle.COMPLETED
        update = resolve_widget_action(
            db,
            origin,
            lifecycle=lifecycle,
            action=WidgetActionId.RESOLVE_CLARIFICATION.value,
            payload=selected,
        )
        if update:
            response.widget_updates.append(update)
        interrupt.status = AgentInterruptStatus.RESOLVED.value
        interrupt.response_payload = payload
    else:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            raise ProtocolRunError("A resolved Fyn interrupt requires a response payload.", "invalid_resume")
        approved = payload.get("approved")
        if not isinstance(approved, bool):
            raise ProtocolRunError("The interrupt response must declare approved true or false.", "invalid_resume")
        if set(payload) - {"approved", "editedArgs"}:
            raise ProtocolRunError("The interrupt response contains unsupported fields.", "invalid_resume")
        if approved:
            edited_args = payload.get("editedArgs", interrupt.metadata_payload.get("proposedArgs"))
            if not isinstance(edited_args, dict):
                raise ProtocolRunError("Approved actions require complete editedArgs.", "invalid_resume")
            if set(edited_args) - {"widgetId", "action", "payload", "completeWidget"}:
                raise ProtocolRunError("editedArgs contains unsupported fields.", "invalid_resume")
            try:
                response = _action_response(
                    db,
                    user,
                    conversation,
                    {**edited_args, "conversation_id": str(conversation.id)},
                )
            except (ValueError, KeyError) as error:
                raise ProtocolRunError(str(error), "invalid_resume_payload") from error
        else:
            if payload.get("editedArgs") is not None:
                raise ProtocolRunError("A denied action cannot include editedArgs.", "invalid_resume")
            response = _cancelled_interrupt_response(db, conversation, interrupt)
        interrupt.status = AgentInterruptStatus.RESOLVED.value
        interrupt.response_payload = payload
    interrupt.resolved_by_run_id = run.id
    db.commit()
    return response, open_interrupts


def _pending_interrupt(
    db: Session,
    run: AgentRun,
    response: AgentResponse,
) -> tuple[AgentInterrupt, Interrupt] | None:
    pending = response.pending_action
    if pending is None:
        return None
    match = next(
        (
            (widget, action)
            for widget in response.widgets
            for action in widget.actions
            if action.action == pending.action
        ),
        None,
    )
    if match is None:
        return None
    widget, action = match
    interrupt_id = uuid4()
    tool_call_id = f"fyn-action-{uuid4()}"
    proposed_args = {
        "widgetId": widget.id,
        "action": action.action.value,
        "payload": action.payload,
        "completeWidget": True,
    }
    edited_variants = []
    for widget_action in widget.actions:
        payload_schema = ACTION_PAYLOAD_MODELS[WidgetActionId(widget_action.action)].model_json_schema(
            mode="validation",
            by_alias=True,
        )
        edited_variants.append(
            {
                "type": "object",
                "required": ["widgetId", "action", "payload"],
                "properties": {
                    "widgetId": {"const": widget.id},
                    "action": {"const": widget_action.action.value},
                    "payload": payload_schema,
                    "completeWidget": {"type": "boolean", "default": True},
                },
                "additionalProperties": False,
            }
        )
    edited_args_schema = {"oneOf": edited_variants}
    response_schema = {
        "type": "object",
        "required": ["approved"],
        "properties": {
            "approved": {"type": "boolean"},
            "editedArgs": edited_args_schema,
        },
        "additionalProperties": False,
    }
    metadata = {
        "schemaVersion": 1,
        "namespace": "fyn",
        "widgetId": widget.id,
        "widgetType": widget.type.value,
        "action": action.action.value,
        "actionLabel": action.label,
        "proposedArgs": proposed_args,
    }
    if pending.action is WidgetActionId.RESOLVE_CLARIFICATION:
        metadata["continuation"] = pending.continuation
    stored = AgentInterrupt(
        id=interrupt_id,
        run_id=run.id,
        tool_call_id=tool_call_id,
        widget_id=widget.id,
        reason=(
            "clarification"
            if pending.action is WidgetActionId.RESOLVE_CLARIFICATION
            else "tool_call"
        ),
        message=(
            response.message
            if pending.action is WidgetActionId.RESOLVE_CLARIFICATION
            else action.label
        ),
        response_schema=response_schema,
        metadata_payload=metadata,
        status=AgentInterruptStatus.OPEN.value,
    )
    db.add(stored)
    db.commit()
    protocol = Interrupt(
        id=str(interrupt_id),
        reason=stored.reason,
        message=stored.message,
        tool_call_id=tool_call_id,
        response_schema=response_schema,
        metadata=metadata,
    )
    return stored, protocol


def _attach_activity_trace(
    db: Session,
    response: AgentResponse,
    activities: dict[str, dict[str, Any]],
    reasoning_trace: str = "",
) -> AgentResponse:
    """Retain the completed live AG-UI trace on the assistant message.

    Live progress crosses the wire as standard AG-UI activity events. This
    widget is the same information at rest, rendered by the existing Fyn card
    system when a thread is opened after the run has completed.
    """
    terminal_ms = max(
        (float(step.get("cumulativeMs", 0)) for step in activities.values()),
        default=0,
    )
    steps: list[dict[str, Any]] = []
    for raw_step in activities.values():
        step = dict(raw_step)
        if step.get("status") == ExecutionStatus.RUNNING.value:
            started_ms = float(step.get("cumulativeMs", 0))
            step.update(
                {
                    "status": ExecutionStatus.FAILED.value,
                    "detail": step.get("detail")
                    or "This stage ended before producing a valid terminal result.",
                    "durationMs": max(
                        float(step.get("durationMs", 0)),
                        round(terminal_ms - started_ms, 1),
                    ),
                    "cumulativeMs": terminal_ms,
                }
            )
        steps.append(step)
    steps.sort(key=lambda step: float(step.get("cumulativeMs", 0)))
    total_ms = max(
        (float(step.get("cumulativeMs", 0)) for step in steps),
        default=0,
    )
    settings = get_settings()
    used_unified = any(
        str(step.get("tool", "")) == "unified_read_agent"
        for step in steps
    )
    used_agno = used_unified or any(
        str(step.get("tool", "")).startswith("agno_")
        or str(step.get("label", "")).startswith("Agno")
        for step in steps
    )
    used_analysis = any(
        str(step.get("tool", "")) in {"analysis_harness", "agno_reroute"}
        for step in steps
    )
    model_path = settings.router_model if used_unified and not used_analysis else (
        f"{settings.router_model} → "
        f"{settings.analysis_model + ' → ' if used_analysis else ''}"
        f"{settings.validator_model}"
    )
    summary = _one_line_reasoning(reasoning_trace) or _activity_reasoning_summary(steps)
    widget_data: dict[str, Any] = {
        "title": "AG-UI agent run",
        "engine": "Agno harness" if used_agno else "Deterministic domain",
        "model": model_path if used_agno else "no model call",
        "summary": summary,
        "debugTrace": settings.environment != "production",
        "steps": steps,
        "totalMs": total_ms,
        "live": False,
    }
    if settings.environment != "production" and reasoning_trace:
        # Development keeps the complete provider-emitted reasoning for run
        # inspection even though the transcript deliberately renders one line.
        widget_data["reasoningTrace"] = reasoning_trace
    widget = Widget(
        id=f"agent-activity-{response.message_id}",
        type=WidgetType.AGENT_ACTIVITY,
        data=widget_data,
    )
    response.widgets.append(widget)
    message = db.get(Message, response.message_id)
    if message:
        message.widgets = [*message.widgets, widget.model_dump(mode="json")]
        db.commit()
    return response


def _messages_snapshot(
    db: Session,
    response: AgentResponse,
    *,
    tool_call_id: str,
    tool_args: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == response.conversation_id)
            .order_by(Message.created_at, Message.id)
        )
    )
    messages: list[dict[str, Any]] = []
    for row in rows:
        if row.role not in {"user", "assistant"}:
            continue
        message: dict[str, Any] = {"id": str(row.id), "role": row.role, "content": row.content}
        if row.id == response.message_id:
            message["toolCalls"] = [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": FYN_ACTION_TOOL,
                        "arguments": json.dumps(tool_args, separators=(",", ":")),
                    },
                }
            ]
        messages.append(message)
    return messages


def _emit_response(
    db: Session,
    run: AgentRun,
    response: AgentResponse,
    publisher: DurableEventPublisher,
    streamed_message_id: UUID | None = None,
    streamed_text: str = "",
) -> bool:
    publisher.bind_final_message(response.message_id)
    message_id = str(response.message_id)
    if streamed_message_id is not None:
        if streamed_message_id != response.message_id or streamed_text != response.message:
            raise ValueError("Streamed assistant text does not match the canonical persisted reply")
    else:
        publisher.bind_delivery_mode("verified_final")
        publisher.emit(TextMessageStartEvent(message_id=message_id, role="assistant", timestamp=timestamp_ms()))
        if response.message:
            publisher.emit(TextMessageContentEvent(message_id=message_id, delta=response.message, timestamp=timestamp_ms()))
    publisher.emit(TextMessageEndEvent(message_id=message_id, timestamp=timestamp_ms()))
    publisher.emit(
        CustomEvent(
            name=FYN_RESPONSE_EVENT,
            value={
                "schemaVersion": 1,
                "runId": str(run.id),
                "response": response.model_dump(mode="json", by_alias=True),
            },
            timestamp=timestamp_ms(),
        )
    )

    pending = _pending_interrupt(db, run, response)
    if pending:
        stored, interrupt = pending
        args = dict(stored.metadata_payload["proposedArgs"])
        publisher.emit(
            ToolCallStartEvent(
                tool_call_id=stored.tool_call_id,
                tool_call_name=FYN_ACTION_TOOL,
                parent_message_id=message_id,
                timestamp=timestamp_ms(),
            )
        )
        publisher.emit(
            ToolCallArgsEvent(
                tool_call_id=stored.tool_call_id,
                delta=json.dumps(args, separators=(",", ":")),
                timestamp=timestamp_ms(),
            )
        )
        publisher.emit(ToolCallEndEvent(tool_call_id=stored.tool_call_id, timestamp=timestamp_ms()))
        publisher.emit(
            StateSnapshotEvent(
                snapshot={
                    "fyn": {
                        "threadId": str(run.conversation_id),
                        "runId": str(run.id),
                        "phase": "interrupted",
                        "messageId": message_id,
                        "interruptIds": [str(stored.id)],
                    }
                },
                timestamp=timestamp_ms(),
            )
        )
        publisher.emit(
            MessagesSnapshotEvent(
                messages=_messages_snapshot(
                    db,
                    response,
                    tool_call_id=stored.tool_call_id,
                    tool_args=args,
                ),
                timestamp=timestamp_ms(),
            )
        )
        publisher.emit(
            RunFinishedEvent(
                thread_id=str(run.conversation_id),
                run_id=str(run.id),
                result={"messageId": message_id},
                outcome=RunFinishedInterruptOutcome(interrupts=[interrupt]),
                timestamp=timestamp_ms(),
            )
        )
        return True

    publisher.emit(
        StateDeltaEvent(
            delta=[
                {"op": "replace", "path": "/fyn/phase", "value": "succeeded"},
                {"op": "add", "path": "/fyn/messageId", "value": message_id},
                {"op": "replace", "path": "/fyn/interruptIds", "value": []},
            ],
            timestamp=timestamp_ms(),
        )
    )
    publisher.emit(
        RunFinishedEvent(
            thread_id=str(run.conversation_id),
            run_id=str(run.id),
            result={"messageId": message_id},
            outcome=RunFinishedSuccessOutcome(),
            timestamp=timestamp_ms(),
        )
    )
    return False


def execute_run(
    session_factory: sessionmaker[Session],
    run_id: UUID,
    user_id: UUID,
    publisher: DurableEventPublisher,
) -> None:
    """Execute one persisted run. The caller may detach without stopping it."""
    try:
        _wait_for_blocker(session_factory, run_id, user_id)
        _update_run(session_factory, run_id, user_id, AgentRunStatus.RUNNING)
        with session_factory() as start_db:
            start_run = _owned_run(start_db, run_id, user_id)
            publisher.emit(
                RunStartedEvent(
                    thread_id=str(start_run.conversation_id),
                    run_id=str(start_run.id),
                    parent_run_id=str(start_run.parent_run_id) if start_run.parent_run_id else None,
                    timestamp=timestamp_ms(),
                )
            )
            publisher.emit(
                StateSnapshotEvent(
                    snapshot={
                        "fyn": {
                            "threadId": str(start_run.conversation_id),
                            "runId": str(start_run.id),
                            "phase": "running",
                            "interruptIds": [],
                        }
                    },
                    timestamp=timestamp_ms(),
                )
            )
        publisher.flush()

        activities: dict[str, dict[str, Any]] = {}
        open_activity_ids: dict[str, str] = {}
        activity_occurrence_counts: dict[str, int] = {}
        resumed_interrupts: list[AgentInterrupt] = []
        streamed_message_id: UUID | None = None
        streamed_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_started = False
        reasoning_closed = False
        with session_factory() as db:
            run = _owned_run(db, run_id, user_id)
            user = db.scalar(select(User).where(User.id == user_id))
            if not user:
                raise ValueError("User not found")
            conversation = _owned_conversation(db, run.conversation_id, user_id)

            def on_activity(event: dict[str, Any]) -> None:
                stage_id = str(event["id"])
                traced_event = _record_activity_event(
                    activities,
                    open_activity_ids,
                    activity_occurrence_counts,
                    event,
                )
                if (
                    event.get("status") == ExecutionStatus.RUNNING.value
                    and stage_id != "grounding"
                    and publisher.cancellation_requested()
                ):
                    raise RunCancelled
                publisher.emit(
                    ActivitySnapshotEvent(
                        message_id=f"{run.id}-activity-{traced_event['id']}",
                        activity_type=FYN_ACTIVITY_TYPE,
                        content=traced_event,
                        replace=True,
                        timestamp=timestamp_ms(),
                    )
                )
                if publisher.supports_incremental_flush:
                    publisher.flush()

            def close_reasoning() -> None:
                nonlocal reasoning_closed
                if not reasoning_started or reasoning_closed:
                    return
                reasoning_id = f"{run.id}-reasoning"
                publisher.emit(ReasoningMessageEndEvent(message_id=reasoning_id, timestamp=timestamp_ms()))
                publisher.emit(ReasoningEndEvent(message_id=reasoning_id, timestamp=timestamp_ms()))
                reasoning_closed = True
                if publisher.supports_incremental_flush:
                    publisher.flush()

            def on_reasoning_delta(delta: str) -> None:
                nonlocal reasoning_started
                if not delta:
                    return
                reasoning_parts.append(delta)
                # AG-UI reasoning must precede assistant text. Late decision
                # details are still retained in the persisted development
                # trace, but are not emitted out of protocol order.
                if streamed_message_id is not None or reasoning_closed:
                    return
                reasoning_id = f"{run.id}-reasoning"
                if not reasoning_started:
                    publisher.emit(ReasoningStartEvent(message_id=reasoning_id, timestamp=timestamp_ms()))
                    publisher.emit(ReasoningMessageStartEvent(
                        message_id=reasoning_id,
                        role="reasoning",
                        timestamp=timestamp_ms(),
                    ))
                    reasoning_started = True
                publisher.emit(ReasoningMessageContentEvent(
                    message_id=reasoning_id,
                    delta=delta,
                    timestamp=timestamp_ms(),
                ))
                if publisher.supports_incremental_flush:
                    publisher.flush()

            def on_text_delta(message_id: UUID, delta: str) -> None:
                nonlocal streamed_message_id
                if not delta:
                    return
                if streamed_message_id is None:
                    close_reasoning()
                    streamed_message_id = message_id
                    publisher.bind_delivery_mode("model_delta")
                    publisher.emit(
                        TextMessageStartEvent(
                            message_id=str(message_id),
                            role="assistant",
                            timestamp=timestamp_ms(),
                        )
                    )
                elif streamed_message_id != message_id:
                    raise ValueError("One run attempted to stream more than one assistant message")
                streamed_parts.append(delta)
                publisher.emit(
                    TextMessageContentEvent(
                        message_id=str(message_id),
                        delta=delta,
                        timestamp=timestamp_ms(),
                    )
                )
                if publisher.supports_incremental_flush:
                    publisher.flush()

            command = run.input_payload
            kind = command.get("kind")
            if kind == "message":
                response = handle_chat(
                    db,
                    user,
                    conversation,
                    str(command["text"]),
                    on_activity,
                    on_text_delta,
                    on_reasoning_delta,
                )
                reasoning_trace = "".join(reasoning_parts)
                if not reasoning_trace:
                    fallback_summary = _activity_reasoning_summary(list(activities.values()))
                    on_reasoning_delta(fallback_summary)
                    reasoning_trace = fallback_summary
                close_reasoning()
                response = _attach_activity_trace(
                    db,
                    response,
                    activities,
                    reasoning_trace,
                )
            elif kind == "action":
                response = _action_response(db, user, conversation, dict(command["action"]))
            elif kind == "resume":
                response, resumed_interrupts = _resume_response(
                    db,
                    user,
                    conversation,
                    run,
                    list(command.get("entries") or []),
                    on_activity,
                    on_text_delta,
                    on_reasoning_delta,
                )
                if activities:
                    reasoning_trace = "".join(reasoning_parts)
                    if not reasoning_trace:
                        fallback_summary = _activity_reasoning_summary(list(activities.values()))
                        on_reasoning_delta(fallback_summary)
                        reasoning_trace = fallback_summary
                    close_reasoning()
                    response = _attach_activity_trace(
                        db,
                        response,
                        activities,
                        reasoning_trace,
                    )
            elif kind == "protocol_error":
                raise ProtocolRunError(
                    str(command.get("message") or "This AG-UI input is not valid for the current thread."),
                    str(command.get("code") or "invalid_input"),
                )
            else:
                raise ValueError("Unknown persisted agent command")

            for interrupt in resumed_interrupts:
                publisher.emit(
                    ToolCallResultEvent(
                        message_id=f"{run.id}-tool-result-{interrupt.id}",
                        tool_call_id=interrupt.tool_call_id,
                        content=json.dumps(
                            {
                                "status": interrupt.status,
                                "resume": interrupt.response_payload,
                                "result": response.model_dump(mode="json", by_alias=True),
                            },
                            separators=(",", ":"),
                        ),
                        role="tool",
                        timestamp=timestamp_ms(),
                    )
                )
            interrupted = _emit_response(
                db,
                run,
                response,
                publisher,
                streamed_message_id,
                "".join(streamed_parts),
            )

        publisher.finish(AgentRunStatus.INTERRUPTED if interrupted else AgentRunStatus.SUCCEEDED)
    except RunCancelled:
        publisher.emit(RunErrorEvent(message="This run was stopped.", code="cancelled", timestamp=timestamp_ms()))
        publisher.finish(AgentRunStatus.CANCELLED, error_code="cancelled")
    except ProtocolRunError as error:
        publisher.emit(RunErrorEvent(message=str(error), code=error.code, timestamp=timestamp_ms()))
        publisher.finish(AgentRunStatus.FAILED, error_code=error.code)
    except Exception as error:
        publisher.emit(
            RunErrorEvent(
                message="fyn AI could not complete this request.",
                code=type(error).__name__,
                timestamp=timestamp_ms(),
            )
        )
        publisher.finish(AgentRunStatus.FAILED, error_code=type(error).__name__)


def recover_agent_runs(
    session_factory: sessionmaker[Session],
) -> list[tuple[UUID, UUID, int]]:
    """Repair runs left active by a process exit and return safe queued work.

    A queued run has not begun its governed command and is safe to start again.
    A running command may already have crossed a financial side-effect boundary,
    so it is never replayed blindly; it receives one durable terminal RunError.
    """
    queued: list[tuple[UUID, UUID, int]] = []
    with session_factory() as db:
        active = list(
            db.scalars(
                select(AgentRun)
                .where(AgentRun.status.in_(ACTIVE_RUN_STATUSES))
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        )
        for run in active:
            if run.status == AgentRunStatus.QUEUED.value:
                queued.append((run.id, run.user_id, run.last_sequence))
                continue
            last_event = db.scalar(
                select(AgentEvent)
                .where(AgentEvent.run_id == run.id)
                .order_by(AgentEvent.sequence.desc())
                .limit(1)
            )
            if last_event and last_event.event_type == "RUN_FINISHED":
                outcome = last_event.payload.get("outcome") or {}
                run.status = (
                    AgentRunStatus.INTERRUPTED.value
                    if outcome.get("type") == "interrupt"
                    else AgentRunStatus.SUCCEEDED.value
                )
                run.finished_at = now_utc()
                continue
            if last_event and last_event.event_type == "RUN_ERROR":
                run.status = (
                    AgentRunStatus.CANCELLED.value
                    if last_event.payload.get("code") == "cancelled"
                    else AgentRunStatus.FAILED.value
                )
                run.finished_at = now_utc()
                run.error_code = str(last_event.payload.get("code") or "recovered_error")
                continue
            sequence = run.last_sequence + 1
            payload = event_payload(
                RunErrorEvent(
                    message="fyn AI stopped because the server restarted. No operation was replayed.",
                    code="server_restart",
                    timestamp=timestamp_ms(),
                )
            )
            db.add(
                AgentEvent(
                    run_id=run.id,
                    sequence=sequence,
                    event_type="RUN_ERROR",
                    payload=payload,
                )
            )
            run.last_sequence = sequence
            run.status = AgentRunStatus.FAILED.value
            run.finished_at = now_utc()
            run.error_code = "server_restart"
        db.commit()
    return queued


def capabilities() -> AgentCapabilities:
    """Advertise implemented behavior, including Fyn-specific safety semantics."""
    settings = get_settings()
    return AgentCapabilities.model_validate(
        {
            "identity": {
                "name": "fyn AI",
                "type": "governed-financial-agent",
                "description": "A stateful personal-finance agent with typed, auditable actions.",
                "version": "1.0.0-agui",
                "provider": "fyn",
            },
            "transport": {
                "streaming": True,
                "websocket": False,
                "httpBinary": False,
                "pushNotifications": False,
                "resumable": True,
            },
            "tools": {
                "supported": True,
                "items": [
                    {
                        "name": FYN_ACTION_TOOL,
                        "description": "Resolve a governed Fyn widget action after explicit user approval.",
                        "parameters": {
                            "type": "object",
                            "required": ["widgetId", "action", "payload"],
                            "properties": {
                                "widgetId": {"type": "string"},
                                "action": {"type": "string"},
                                "payload": {"type": "object"},
                                "completeWidget": {"type": "boolean", "default": True},
                            },
                            "additionalProperties": False,
                        },
                    }
                ],
                "parallelCalls": False,
                "clientProvided": False,
            },
            "output": {
                "structuredOutput": True,
                "supportedMimeTypes": ["text/plain", "application/json"],
            },
            "state": {
                "snapshots": True,
                "deltas": True,
                "memory": True,
                "persistentState": True,
            },
            "reasoning": {"supported": True, "streaming": True, "encrypted": False},
            "multimodal": {
                "input": {
                    "image": False,
                    "audio": False,
                    "video": False,
                    "pdf": False,
                    "file": False,
                },
                "output": {"image": False, "audio": False},
            },
            "execution": {
                "codeExecution": False,
                "sandboxed": False,
            },
            "humanInTheLoop": {
                "supported": True,
                "approvals": True,
                "interventions": False,
                "feedback": False,
                "interrupts": True,
                "approveWithEdits": True,
            },
            "custom": {
                "fyn": {
                    "responseEvent": FYN_RESPONSE_EVENT,
                    "activityType": FYN_ACTIVITY_TYPE,
                    "widgetActionTool": FYN_ACTION_TOOL,
                    "canonicalFinancialState": "server",
                    "clientToolsAreAuthority": False,
                    "rawChainOfThoughtExposed": False,
                    "reasoningTraceMode": (
                        "full_provider_events"
                        if settings.environment != "production"
                        else "provider_summary"
                    ),
                }
            },
        }
    )
