from __future__ import annotations

import json
import time
from collections.abc import Callable
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
from ..domain import AgentInterruptStatus, AgentRunStatus, ExecutionStatus, WidgetActionId
from ..event_time import now_utc
from ..models import AgentEvent, AgentInterrupt, AgentRun, Conversation, Message, User
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
    persist_agent_response,
    prepare_widget_action,
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
    origin = prepare_widget_action(db, conversation, interrupt.widget_id, "cancel_interrupt")
    response = persist_agent_response(db, conversation, "No changes were made.")
    update = resolve_widget_action(
        db,
        origin,
        lifecycle=WidgetLifecycle.CANCELLED,
        action="cancel_interrupt",
        payload={},
    )
    if update:
        response.widget_updates.append(update)
        db.commit()
    return response


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
    stored = AgentInterrupt(
        id=interrupt_id,
        run_id=run.id,
        tool_call_id=tool_call_id,
        widget_id=widget.id,
        reason="tool_call",
        message=action.label,
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
    used_agno = any(
        str(step.get("tool", "")).startswith("agno_")
        or str(step.get("label", "")).startswith("Agno")
        for step in steps
    )
    used_analysis = any(
        str(step.get("tool", "")) in {"analysis_harness", "agno_reroute"}
        for step in steps
    )
    model_path = (
        f"{settings.router_model} → "
        f"{settings.analysis_model + ' → ' if used_analysis else ''}"
        f"{settings.validator_model}"
    )
    widget = Widget(
        id=f"agent-activity-{response.message_id}",
        type=WidgetType.AGENT_ACTIVITY,
        data={
            "title": "AG-UI agent run",
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
    completed_activity_labels: list[str],
) -> bool:
    if completed_activity_labels:
        reasoning_id = f"{run.id}-reasoning"
        summary = "Completed: " + "; ".join(dict.fromkeys(completed_activity_labels)) + "."
        publisher.emit(ReasoningStartEvent(message_id=reasoning_id, timestamp=timestamp_ms()))
        publisher.emit(ReasoningMessageStartEvent(message_id=reasoning_id, role="reasoning", timestamp=timestamp_ms()))
        publisher.emit(ReasoningMessageContentEvent(message_id=reasoning_id, delta=summary, timestamp=timestamp_ms()))
        publisher.emit(ReasoningMessageEndEvent(message_id=reasoning_id, timestamp=timestamp_ms()))
        publisher.emit(ReasoningEndEvent(message_id=reasoning_id, timestamp=timestamp_ms()))

    message_id = str(response.message_id)
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

        completed_activity_labels: list[str] = []
        activities: dict[str, dict[str, Any]] = {}
        resumed_interrupts: list[AgentInterrupt] = []
        with session_factory() as db:
            run = _owned_run(db, run_id, user_id)
            user = db.scalar(select(User).where(User.id == user_id))
            if not user:
                raise ValueError("User not found")
            conversation = _owned_conversation(db, run.conversation_id, user_id)

            def on_activity(event: dict[str, Any]) -> None:
                activities[str(event["id"])] = event
                if (
                    event.get("status") == ExecutionStatus.RUNNING.value
                    and event.get("id") != "grounding"
                    and publisher.cancellation_requested()
                ):
                    raise RunCancelled
                if event.get("status") == ExecutionStatus.COMPLETED.value:
                    completed_activity_labels.append(str(event.get("label") or event.get("id")))
                publisher.emit(
                    ActivitySnapshotEvent(
                        message_id=f"{run.id}-activity-{event['id']}",
                        activity_type=FYN_ACTIVITY_TYPE,
                        content=event,
                        replace=True,
                        timestamp=timestamp_ms(),
                    )
                )
                if publisher.supports_incremental_flush:
                    publisher.flush()

            command = run.input_payload
            kind = command.get("kind")
            if kind == "message":
                response = handle_chat(db, user, conversation, str(command["text"]), on_activity)
                response = _attach_activity_trace(db, response, activities)
            elif kind == "action":
                response = _action_response(db, user, conversation, dict(command["action"]))
            elif kind == "resume":
                response, resumed_interrupts = _resume_response(
                    db,
                    user,
                    conversation,
                    run,
                    list(command.get("entries") or []),
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
            interrupted = _emit_response(db, run, response, publisher, completed_activity_labels)

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
            "reasoning": {"supported": True, "streaming": False, "encrypted": False},
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
                }
            },
        }
    )
