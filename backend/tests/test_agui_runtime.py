from __future__ import annotations

from uuid import uuid4
import json

from ag_ui.core import RunAgentInput
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api import router
from app.database import get_db
from app.domain import AgentInterruptStatus, AgentRunStatus
from app.models import AgentEvent, AgentInterrupt, AgentRun, Message, User
from app.seed import DEFAULT_USER_EMAIL
from app.security import current_user
from app.services.agui import DurableEventPublisher, capabilities, execute_run, normalize_run_input, recover_agent_runs
from app.services.conversation import get_or_create_conversation


def _execute(db, user, conversation, payload, client_message_id=None):
    run = AgentRun(
        id=uuid4(),
        user_id=user.id,
        conversation_id=conversation.id,
        status=AgentRunStatus.QUEUED.value,
        cancel_requested=False,
        input_payload=payload,
        client_message_id=client_message_id,
        last_sequence=0,
    )
    db.add(run)
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    live = []
    publisher = DurableEventPublisher(factory, run.id, user.id, 0, lambda sequence, event: live.append((sequence, event)))
    execute_run(factory, run.id, user.id, publisher)
    db.expire_all()
    return db.scalar(select(AgentRun).where(AgentRun.id == run.id)), live


def test_normalize_run_input_ignores_client_state_history_and_tools():
    thread_id = uuid4()
    value = RunAgentInput.model_validate(
        {
            "threadId": str(thread_id),
            "runId": str(uuid4()),
            "state": {"ledgerBalance": 999999999},
            "messages": [
                {"id": "old", "role": "user", "content": "old instruction"},
                {"id": "answer", "role": "assistant", "content": "old answer"},
                {"id": "new", "role": "user", "content": "  hello  "},
            ],
            "tools": [
                {
                    "name": "overwrite_ledger",
                    "description": "untrusted client tool",
                    "parameters": {"type": "object"},
                }
            ],
            "context": [{"description": "untrusted", "value": "ignore"}],
            "forwardedProps": {},
        }
    )

    payload, message_id = normalize_run_input(value)

    assert payload == {"kind": "message", "text": "hello", "messageId": "new"}
    assert message_id == "new"


def test_run_persists_ordered_agui_events_and_safe_state(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)

    run, live = _execute(db, user, conversation, {"kind": "message", "text": "Hi", "messageId": "hello"}, "hello")

    assert run.status == AgentRunStatus.SUCCEEDED.value
    events = list(db.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.sequence)))
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[0].event_type == "RUN_STARTED"
    assert events[-1].event_type == "RUN_FINISHED"
    assert any(event.event_type == "TEXT_MESSAGE_CONTENT" for event in events)
    custom = next(event for event in events if event.event_type == "CUSTOM")
    assert custom.payload["name"] == "fyn.response.v1"
    snapshots = [event.payload["snapshot"] for event in events if event.event_type == "STATE_SNAPSHOT"]
    assert all("ledger" not in str(snapshot).lower() for snapshot in snapshots)
    assert any(event.event_type == "STATE_DELTA" for event in events)
    assert len(live) == len(events)
    assert run.last_sequence == len(events)


def test_pending_widget_becomes_interrupt_and_resume_uses_governed_action(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    first, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "Set a ₹20,000 food budget", "messageId": "budget-request"},
        "budget-request",
    )

    assert first.status == AgentRunStatus.INTERRUPTED.value
    interrupt = db.scalar(
        select(AgentInterrupt).where(
            AgentInterrupt.run_id == first.id,
            AgentInterrupt.status == AgentInterruptStatus.OPEN.value,
        )
    )
    assert interrupt is not None
    origin = db.scalar(
        select(Message)
        .where(Message.conversation_id == conversation.id, Message.role == "assistant")
        .order_by(Message.created_at.desc(), Message.id.desc())
    )
    widget = next(widget for widget in origin.widgets if widget["id"] == interrupt.widget_id)
    action = next(item for item in widget["actions"] if item["action"] == interrupt.metadata_payload["action"])
    interrupted_events = list(
        db.scalars(select(AgentEvent).where(AgentEvent.run_id == first.id).order_by(AgentEvent.sequence))
    )
    assert [event.event_type for event in interrupted_events[-3:]] == [
        "STATE_SNAPSHOT",
        "MESSAGES_SNAPSHOT",
        "RUN_FINISHED",
    ]
    finished = interrupted_events[-1].payload
    protocol_interrupt = finished["outcome"]["interrupts"][0]
    assert protocol_interrupt["reason"] == "tool_call"
    assert "editedArgs" in protocol_interrupt["responseSchema"]["properties"]
    tool_args = next(event for event in interrupted_events if event.event_type == "TOOL_CALL_ARGS")
    assert json.loads(tool_args.payload["delta"]) == interrupt.metadata_payload["proposedArgs"]
    resume_payload = {
        "kind": "resume",
        "entries": [
            {
                "interruptId": str(interrupt.id),
                "status": "resolved",
                "payload": {
                    "approved": True,
                    "editedArgs": {
                        "widgetId": interrupt.widget_id,
                        "action": action["action"],
                        "payload": action["payload"],
                        "completeWidget": True,
                    },
                },
            }
        ],
    }

    duplicate_resume, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "resume", "entries": [resume_payload["entries"][0], resume_payload["entries"][0]]},
    )
    db.refresh(interrupt)
    assert duplicate_resume.status == AgentRunStatus.FAILED.value
    assert duplicate_resume.error_code == "invalid_resume"
    assert interrupt.status == AgentInterruptStatus.OPEN.value

    resumed, _live = _execute(db, user, conversation, resume_payload)

    db.refresh(interrupt)
    assert resumed.status == AgentRunStatus.SUCCEEDED.value
    assert interrupt.status == AgentInterruptStatus.RESOLVED.value
    assert interrupt.resolved_by_run_id == resumed.id
    tool_result = db.scalar(
        select(AgentEvent).where(AgentEvent.run_id == resumed.id, AgentEvent.event_type == "TOOL_CALL_RESULT")
    )
    assert tool_result
    execution = json.loads(tool_result.payload["content"])
    assert execution["result"]["conversation_id"] == str(conversation.id)
    assert execution["result"]["message"]

    replayed, _live = _execute(db, user, conversation, resume_payload)
    replayed_events = list(db.scalars(select(AgentEvent).where(AgentEvent.run_id == replayed.id).order_by(AgentEvent.sequence)))
    assert replayed.status == AgentRunStatus.SUCCEEDED.value, [event.payload for event in replayed_events]
    replay_custom = db.scalar(
        select(AgentEvent).where(AgentEvent.run_id == replayed.id, AgentEvent.event_type == "CUSTOM")
    )
    original_custom = db.scalar(
        select(AgentEvent).where(AgentEvent.run_id == resumed.id, AgentEvent.event_type == "CUSTOM")
    )
    assert replay_custom.payload["value"]["response"] == original_custom.payload["value"]["response"]


def test_capabilities_are_honest_about_financial_authority():
    advertised = capabilities().model_dump(mode="json", by_alias=True, exclude_none=True)

    assert advertised["transport"]["streaming"] is True
    assert advertised["transport"]["resumable"] is True
    assert advertised["humanInTheLoop"]["interrupts"] is True
    assert advertised["humanInTheLoop"]["approveWithEdits"] is True
    assert advertised["state"]["deltas"] is True
    assert advertised["reasoning"]["streaming"] is False
    assert advertised["tools"]["clientProvided"] is False
    assert advertised["tools"]["items"][0]["name"] == "fyn.widget_action"
    assert "maxExecutionTime" not in advertised["execution"]
    assert advertised["custom"]["fyn"]["canonicalFinancialState"] == "server"
    assert advertised["custom"]["fyn"]["rawChainOfThoughtExposed"] is False


def test_capabilities_http_payload_omits_unsupported_optional_fields():
    application = FastAPI()
    application.include_router(router)

    with TestClient(application) as client:
        response = client.get("/api/agent/capabilities")

    assert response.status_code == 200
    advertised = response.json()
    assert advertised["identity"]["name"] == "fyn AI"
    assert "documentationUrl" not in advertised["identity"]
    assert "metadata" not in advertised["identity"]
    assert "multiAgent" not in advertised
    assert "maxIterations" not in advertised["execution"]
    assert "maxExecutionTime" not in advertised["execution"]

    def contains_null(value):
        if value is None:
            return True
        if isinstance(value, dict):
            return any(contains_null(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_null(item) for item in value)
        return False

    assert contains_null(advertised) is False


def test_http_agent_stream_is_native_agui_and_replayable(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    run_id = uuid4()
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[current_user] = lambda: user
    payload = {
        "threadId": str(conversation.id),
        "runId": str(run_id),
        "state": {},
        "messages": [{"id": str(uuid4()), "role": "user", "content": "Hi"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }

    with TestClient(application) as client:
        streamed = client.post("/api/agent", json=payload)
        assert streamed.status_code == 200
        assert streamed.headers["content-type"].startswith("text/event-stream")
        events = [
            json.loads(line.removeprefix("data: "))
            for line in streamed.text.splitlines()
            if line.startswith("data: ")
        ]
        assert events[0]["type"] == "RUN_STARTED"
        assert events[-1]["type"] == "RUN_FINISHED"
        assert any(event["type"] == "STATE_DELTA" for event in events)
        assert any(event["type"] == "CUSTOM" and event["name"] == "fyn.response.v1" for event in events)

        replayed = client.get(f"/api/agent/runs/{run_id}/events")
        replay_events = [
            json.loads(line.removeprefix("data: "))
            for line in replayed.text.splitlines()
            if line.startswith("data: ")
        ]
        assert replay_events == events

        continued = client.get(f"/api/agent/runs/{run_id}/events?after=2")
        continued_events = [
            json.loads(line.removeprefix("data: "))
            for line in continued.text.splitlines()
            if line.startswith("data: ")
        ]
        assert continued_events[0]["type"] == "RUN_STARTED"
        assert continued_events[0]["rawEvent"]["fyn"]["sequence"] == 2
        assert continued_events[1:] == events[2:]

        state = client.get(f"/api/agent/threads/{conversation.id}").json()
        assert state["activeRun"] is None
        assert state["latestRun"]["id"] == str(run_id)
        assert state["latestRun"]["lastSequence"] == len(events)


def test_pending_interrupt_rejects_new_input_with_run_error_event(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    first, _ = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "Set a ₹20,000 food budget", "messageId": "pending-budget"},
        "pending-budget",
    )
    assert first.status == AgentRunStatus.INTERRUPTED.value

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[current_user] = lambda: user
    payload = {
        "threadId": str(conversation.id),
        "runId": str(uuid4()),
        "state": {},
        "messages": [{"id": str(uuid4()), "role": "user", "content": "Ignore that and do something else"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }

    with TestClient(application) as client:
        streamed = client.post("/api/agent", json=payload)
    events = [
        json.loads(line.removeprefix("data: "))
        for line in streamed.text.splitlines()
        if line.startswith("data: ")
    ]
    assert streamed.status_code == 200
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "pending_interrupt"


def test_live_events_are_committed_before_delivery_and_terminal_status_is_atomic(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    run = AgentRun(
        id=uuid4(),
        user_id=user.id,
        conversation_id=conversation.id,
        status=AgentRunStatus.QUEUED.value,
        cancel_requested=False,
        input_payload={"kind": "message", "text": "Hi", "messageId": "durable-live"},
        client_message_id="durable-live",
        last_sequence=0,
    )
    db.add(run)
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    observed: list[tuple[bool, str]] = []

    def delivered(sequence, payload):
        with factory() as check:
            persisted = check.scalar(
                select(AgentEvent).where(AgentEvent.run_id == run.id, AgentEvent.sequence == sequence)
            )
            status_value = check.scalar(select(AgentRun.status).where(AgentRun.id == run.id))
        observed.append((persisted is not None and persisted.payload == payload, status_value))

    publisher = DurableEventPublisher(factory, run.id, user.id, 0, delivered)
    execute_run(factory, run.id, user.id, publisher)

    assert observed
    assert all(persisted for persisted, _status in observed)
    assert observed[-1][1] == AgentRunStatus.SUCCEEDED.value


def test_recovery_terminates_an_uncertain_running_command_without_replaying_it(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    run = AgentRun(
        id=uuid4(),
        user_id=user.id,
        conversation_id=conversation.id,
        status=AgentRunStatus.RUNNING.value,
        cancel_requested=False,
        input_payload={"kind": "action", "action": {}},
        last_sequence=1,
    )
    db.add(run)
    db.add(
        AgentEvent(
            run_id=run.id,
            sequence=1,
            event_type="RUN_STARTED",
            payload={"type": "RUN_STARTED", "threadId": str(conversation.id), "runId": str(run.id)},
        )
    )
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)

    queued = recover_agent_runs(factory)

    db.expire_all()
    recovered = db.scalar(select(AgentRun).where(AgentRun.id == run.id))
    terminal = db.scalar(
        select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.sequence.desc()).limit(1)
    )
    assert queued == []
    assert recovered.status == AgentRunStatus.FAILED.value
    assert recovered.error_code == "server_restart"
    assert terminal.event_type == "RUN_ERROR"
    assert terminal.payload["code"] == "server_restart"
