from __future__ import annotations

from uuid import uuid4
import json

from ag_ui.core import RunAgentInput
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api import router
from app.api import agent_thread_metrics
from app.database import get_db
from app.domain import AgentInterruptStatus, AgentRunStatus
from app.models import AgentEvent, AgentInterrupt, AgentRun, Message, User
from app.seed import DEFAULT_USER_EMAIL
from app.security import current_user
from app.services.agui import DurableEventPublisher, capabilities, execute_run, normalize_run_input, recover_agent_runs
from app.services.agent_observability import evaluate_agent_reply
from app.services import agui as agui_service
from app.services import conversation as conversation_service
from app.services.agents import ClarificationOption, ClarificationRequest, CopilotDecision
from app.services.capabilities import CapabilityId
from app.services.conversation import get_or_create_conversation, persist_agent_response


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
    final_message = db.get(Message, run.final_message_id)
    assert final_message is not None
    assert custom.payload["value"]["response"]["message_id"] == str(final_message.id)
    assert custom.payload["value"]["response"]["message"] == final_message.content
    assert run.input_payload["text"] == "Hi"
    assert run.first_response_at is not None
    assert run.time_to_first_response_ms is not None
    assert run.time_to_first_response_ms >= 0
    assert run.duration_ms is not None
    assert run.duration_ms >= run.time_to_first_response_ms
    snapshots = [event.payload["snapshot"] for event in events if event.event_type == "STATE_SNAPSHOT"]
    assert all("ledger" not in str(snapshot).lower() for snapshot in snapshots)
    assert any(event.event_type == "STATE_DELTA" for event in events)
    assert len(live) == len(events)
    assert run.last_sequence == len(events)


def test_safe_conversation_model_deltas_are_durable_and_match_the_reply(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)

    def streamed_chat(
        db,
        user,
        conversation,
        text,
        activity_callback=None,
        text_delta_callback=None,
        reasoning_delta_callback=None,
    ):
        response = persist_agent_response(db, conversation, "A clear answer.")
        assert text_delta_callback is not None
        text_delta_callback(response.message_id, "A clear ")
        text_delta_callback(response.message_id, "answer.")
        return response

    monkeypatch.setattr(agui_service, "handle_chat", streamed_chat)

    run, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "What can you do?", "messageId": "stream-me"},
        "stream-me",
    )

    events = list(db.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.sequence)))
    content = [event.payload["delta"] for event in events if event.event_type == "TEXT_MESSAGE_CONTENT"]
    assert content == ["A clear ", "answer."]
    assert run.delivery_mode == "model_delta"
    assert db.get(Message, run.final_message_id).content == "".join(content)


def test_provider_reasoning_stream_is_durable_and_persisted_as_one_line_summary(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)

    def reasoned_chat(
        db,
        user,
        conversation,
        text,
        activity_callback=None,
        text_delta_callback=None,
        reasoning_delta_callback=None,
    ):
        response = persist_agent_response(db, conversation, "A contextual answer.")
        assert reasoning_delta_callback is not None
        reasoning_delta_callback("Read the previous Housing result. ")
        reasoning_delta_callback("Kept July and removed the merchant filter.")
        return response

    monkeypatch.setattr(agui_service, "handle_chat", reasoned_chat)

    run, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "What about the other ones?", "messageId": "reason-me"},
        "reason-me",
    )

    events = list(db.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.sequence)))
    event_types = [event.event_type for event in events]
    reasoning = "".join(
        event.payload["delta"]
        for event in events
        if event.event_type == "REASONING_MESSAGE_CONTENT"
    )
    assert reasoning == "Read the previous Housing result. Kept July and removed the merchant filter."
    assert event_types.index("REASONING_END") < event_types.index("TEXT_MESSAGE_START")
    message = db.get(Message, run.final_message_id)
    trace = next(widget for widget in message.widgets if widget["type"] == "agent_activity")
    assert trace["data"]["summary"] == reasoning
    assert trace["data"]["reasoningTrace"] == reasoning


def test_thread_metrics_report_measured_latency_and_labeled_evaluation(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    run, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "Hi", "messageId": "metrics-hi"},
        "metrics-hi",
    )

    metrics = agent_thread_metrics(conversation.id, db, user)

    assert metrics.sample_size == 1
    assert metrics.completion_rate == 1
    assert metrics.average_duration_ms == run.duration_ms
    assert metrics.average_time_to_first_response_ms == run.time_to_first_response_ms
    assert metrics.evidence_pass_rate == 1
    assert metrics.recent_runs[0].evaluation.correctness_basis == "claim_integrity"


def test_quality_signal_rejects_a_templated_acknowledgement_loop(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    run = AgentRun(
        user_id=user.id,
        conversation_id=conversation.id,
        input_payload={"kind": "message", "text": "OK"},
    )
    reply = Message(
        conversation_id=conversation.id,
        role="assistant",
        content="What would you like to look at next?",
        widgets=[],
        citations=[],
    )

    evaluation = evaluate_agent_reply(
        run,
        reply,
        previous_assistant_text="I’m doing well. How can I help you today?",
    )

    assert evaluation.contextual is False
    assert evaluation.quality_score == 80
    assert "acknowledgement reopened the same question" in evaluation.signals


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

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[current_user] = lambda: user
    with TestClient(application) as client:
        exported_response = client.get("/api/privacy/export")
    assert exported_response.status_code == 200, exported_response.text
    exported_interrupt = next(
        item for item in exported_response.json()["agentInterrupts"] if item["id"] == str(interrupt.id)
    )
    assert exported_interrupt["metadata"] == interrupt.metadata_payload

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


def test_generic_clarification_interrupt_resumes_original_request_without_duplicate_user_turn(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    original = "₹1,00,000 at 10% for 2 years with a ₹2,000 installment"
    clarification = ClarificationRequest(
        question="Should the two-year tenure or the ₹2,000 installment control the schedule?",
        reason="Those inputs produce different repayment schedules, so silently choosing one could give the wrong answer.",
        conflict_fields=["tenure", "installment"],
        options=[
            ClarificationOption(
                id="use_tenure",
                label="Keep the 2-year tenure",
                description="Calculate the required installment for 24 months.",
                resolution="Use 24 months as authoritative and calculate the required monthly installment.",
            ),
            ClarificationOption(
                id="use_installment",
                label="Keep the ₹2,000 installment",
                description="Calculate the resulting repayment tenure.",
                resolution="Use ₹2,000 as the authoritative monthly installment and calculate the resulting tenure.",
            ),
        ],
        allow_custom=True,
    )

    def typed_gate(text, *args, **kwargs):
        if "Customer clarification (authoritative)" in text:
            assert original in text
            assert "Use 24 months as authoritative" in text
            return CopilotDecision(
                tool=CapabilityId.CONVERSATION,
                reply="Using the two-year tenure, I can now calculate and render the requested schedule.",
                confidence=1,
                reason="The customer resolved the conflicting loan inputs.",
            ), None
        return CopilotDecision(
            tool=CapabilityId.REQUEST_CLARIFICATION,
            clarification=clarification,
            confidence=1,
            reason="The supplied loan inputs conflict.",
        ), None

    monkeypatch.setattr(conversation_service, "_fast_path_decision", typed_gate)
    first, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": original, "messageId": "loan-conflict"},
        "loan-conflict",
    )

    assert first.status == AgentRunStatus.INTERRUPTED.value
    interrupt = db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == first.id))
    assert interrupt.reason == "clarification"
    assert interrupt.metadata_payload["continuation"]["originalRequest"] == original
    origin = db.get(Message, first.final_message_id)
    widget = next(item for item in origin.widgets if item["type"] == "clarification")
    choice = next(item for item in widget["actions"] if item["id"] == "use_tenure")
    resume_payload = {
        "kind": "resume",
        "entries": [{
            "interruptId": str(interrupt.id),
            "status": "resolved",
            "payload": {
                "approved": True,
                "editedArgs": {
                    "widgetId": widget["id"],
                    "action": choice["action"],
                    "payload": choice["payload"],
                    "completeWidget": True,
                },
            },
        }],
    }

    tampered_payload = json.loads(json.dumps(resume_payload))
    tampered_payload["entries"][0]["payload"]["editedArgs"]["payload"]["optionId"] = "injected_option"
    rejected, _live = _execute(db, user, conversation, tampered_payload)
    assert rejected.status == AgentRunStatus.FAILED.value
    assert rejected.error_code == "invalid_resume_payload"
    db.refresh(interrupt)
    assert interrupt.status == AgentInterruptStatus.OPEN.value

    resumed, _live = _execute(db, user, conversation, resume_payload)

    assert resumed.status == AgentRunStatus.SUCCEEDED.value
    db.refresh(interrupt)
    assert interrupt.status == AgentInterruptStatus.RESOLVED.value
    messages = list(db.scalars(select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at, Message.id)))
    assert [message.content for message in messages if message.role == "user"] == [original]
    assert messages[-1].content == "Using the two-year tenure, I can now calculate and render the requested schedule."
    completed_origin = db.get(Message, origin.id)
    completed = next(item for item in completed_origin.widgets if item["id"] == widget["id"])
    assert completed["data"]["lifecycle"] == "completed"
    assert completed["data"]["completion"]["values"]["optionId"] == "use_tenure"

    replayed, _live = _execute(db, user, conversation, resume_payload)
    assert replayed.status == AgentRunStatus.SUCCEEDED.value
    assert replayed.final_message_id == resumed.final_message_id


def test_cancel_clarification_option_closes_interrupt_and_allows_the_next_message(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    original = "Transfer ₹25,000 from my account."
    calls: list[str] = []
    clarification = ClarificationRequest(
        question="Which account should receive the transfer?",
        reason="A destination account is required before a transfer can be prepared.",
        conflict_fields=["destination_account"],
        options=[
            ClarificationOption(
                id="provide_destination",
                label="Specify destination",
                resolution="Use the customer-provided destination account.",
            ),
            ClarificationOption(
                id="cancel_transfer",
                label="Cancel",
                description="Do not create this transfer.",
                resolution="Cancel the requested transfer.",
                disposition="cancel",
            ),
        ],
        allow_custom=True,
    )

    def typed_gate(text, *args, **kwargs):
        calls.append(text)
        if text == original:
            return CopilotDecision(
                tool=CapabilityId.REQUEST_CLARIFICATION,
                clarification=clarification,
                confidence=1,
                reason="The transfer destination is missing.",
            ), None
        return CopilotDecision(
            tool=CapabilityId.CONVERSATION,
            reply="Ready for the next request.",
            confidence=1,
            reason="The new message is independent.",
        ), None

    monkeypatch.setattr(conversation_service, "_fast_path_decision", typed_gate)
    first, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": original, "messageId": "cancel-transfer"},
        "cancel-transfer",
    )
    interrupt = db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == first.id))
    origin = db.get(Message, first.final_message_id)
    widget = next(item for item in origin.widgets if item["type"] == "clarification")
    cancel = next(item for item in widget["actions"] if item["id"] == "cancel_transfer")
    # A thread may already be waiting on a cancellation option persisted by a
    # pre-disposition server. Its server-authored semantic id still has to be
    # recoverable after deployment.
    metadata = json.loads(json.dumps(interrupt.metadata_payload))
    del metadata["continuation"]["options"]["cancel_transfer"]["disposition"]
    interrupt.metadata_payload = metadata
    db.commit()

    resumed, _live = _execute(db, user, conversation, {
        "kind": "resume",
        "entries": [{
            "interruptId": str(interrupt.id),
            "status": "resolved",
            "payload": {
                "approved": True,
                "editedArgs": {
                    "widgetId": widget["id"],
                    "action": cancel["action"],
                    "payload": cancel["payload"],
                    "completeWidget": True,
                },
            },
        }],
    })

    db.refresh(interrupt)
    db.refresh(origin)
    assert resumed.status == AgentRunStatus.SUCCEEDED.value
    assert interrupt.status == AgentInterruptStatus.RESOLVED.value
    assert interrupt.resolved_by_run_id == resumed.id
    assert calls == [original]
    assert db.get(Message, resumed.final_message_id).content == "The request was cancelled. No changes were made."
    cancelled = next(item for item in origin.widgets if item["id"] == widget["id"])
    assert cancelled["data"]["lifecycle"] == "cancelled"
    assert cancelled["data"]["completion"]["values"]["optionId"] == "cancel_transfer"
    assert not list(db.scalars(
        select(AgentInterrupt)
        .join(AgentRun, AgentRun.id == AgentInterrupt.run_id)
        .where(
            AgentRun.conversation_id == conversation.id,
            AgentInterrupt.status == AgentInterruptStatus.OPEN.value,
        )
    ))

    following, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "How are you?", "messageId": "after-cancel"},
        "after-cancel",
    )
    assert following.status == AgentRunStatus.SUCCEEDED.value
    assert db.get(Message, following.final_message_id).content == "Ready for the next request."


def test_protocol_cancel_closes_a_clarification_widget(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    clarification = ClarificationRequest(
        question="Which interpretation should be used?",
        reason="The request has two materially different interpretations.",
        options=[
            ClarificationOption(id="first", label="First", resolution="Use the first interpretation."),
            ClarificationOption(id="second", label="Second", resolution="Use the second interpretation."),
        ],
    )
    monkeypatch.setattr(
        conversation_service,
        "_fast_path_decision",
        lambda *args, **kwargs: (
            CopilotDecision(
                tool=CapabilityId.REQUEST_CLARIFICATION,
                clarification=clarification,
                confidence=1,
                reason="The request is ambiguous.",
            ),
            None,
        ),
    )
    first, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "Ambiguous request", "messageId": "protocol-cancel"},
        "protocol-cancel",
    )
    interrupt = db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == first.id))
    origin = db.get(Message, first.final_message_id)
    pending_widget = next(item for item in origin.widgets if item["type"] == "clarification")
    cancel_action = next(item for item in pending_widget["actions"] if item["id"] == "cancel")
    assert cancel_action["payload"]["optionId"] == "cancel"
    assert interrupt.metadata_payload["continuation"]["options"]["cancel"]["disposition"] == "cancel"

    cancelled, _live = _execute(db, user, conversation, {
        "kind": "resume",
        "entries": [{"interruptId": str(interrupt.id), "status": "cancelled"}],
    })

    db.refresh(interrupt)
    db.refresh(origin)
    assert cancelled.status == AgentRunStatus.SUCCEEDED.value
    assert interrupt.status == AgentInterruptStatus.CANCELLED.value
    assert db.get(Message, cancelled.final_message_id).content == "No changes were made."
    widget = next(item for item in origin.widgets if item["type"] == "clarification")
    assert widget["data"]["lifecycle"] == "cancelled"


def test_capabilities_are_honest_about_financial_authority():
    advertised = capabilities().model_dump(mode="json", by_alias=True, exclude_none=True)

    assert advertised["transport"]["streaming"] is True
    assert advertised["transport"]["resumable"] is True
    assert advertised["humanInTheLoop"]["interrupts"] is True
    assert advertised["humanInTheLoop"]["approveWithEdits"] is True
    assert advertised["state"]["deltas"] is True
    assert advertised["reasoning"]["streaming"] is True
    assert advertised["tools"]["clientProvided"] is False
    assert advertised["tools"]["items"][0]["name"] == "fyn.widget_action"
    assert "maxExecutionTime" not in advertised["execution"]
    assert advertised["custom"]["fyn"]["canonicalFinancialState"] == "server"
    assert advertised["custom"]["fyn"]["rawChainOfThoughtExposed"] is False
    assert advertised["custom"]["fyn"]["reasoningTraceMode"] == "full_provider_events"


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
