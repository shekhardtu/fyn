from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ag_ui.core import RunAgentInput, RunStartedEvent
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api import _record_import_preview, router
from app.database import get_db
from app.domain import AgentEnrichmentStatus, AgentInterruptStatus, AgentRunStatus, FinancialSourceType, ImportStatus
from app.event_time import now_utc
from app.models import AgentEnrichment, AgentEvent, AgentInterrupt, AgentRun, Budget, Category, Goal, Import, Message, Subcategory, TaxonomyScope, User
from app.operations import operation_catalog
from app.operations.tools import OperationProposal
from app.seed import DEFAULT_USER_EMAIL
from app.security import current_user
from app.services.agui import (
    DurableEventPublisher,
    agent_recovery_backlog_exists,
    capabilities,
    claim_agent_recovery_work,
    execute_run,
    normalize_run_input,
    recover_agent_runs,
)
from app.services import agui as agui_service
from app.services import agent_enrichment as enrichment_service
from app.services import conversation as conversation_service
from app.services.adapters import import_summary
from app.services.agents import ClarificationOption, ClarificationRequest, CopilotDecision, OperatorResult, TaxonomyInterpretation
from app.services.capabilities import CapabilityId
from app.services.conversation import get_or_create_conversation, persist_agent_response


def _process_one_enrichment(db, *, max_attempts=2):
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    work = enrichment_service.claim_agent_enrichment_work(
        factory,
        claim_ttl_seconds=60,
        max_attempts=max_attempts,
    )
    assert work is not None and work.enrichment_id is not None and work.user_id is not None
    enrichment_service.process_agent_enrichment(
        factory,
        work.enrichment_id,
        work.user_id,
        max_attempts=max_attempts,
        retry_seconds=1,
    )
    db.expire_all()
    return db.get(AgentEnrichment, work.enrichment_id)


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


def _operator_proposal(operation_id: str, inputs: dict) -> OperatorResult:
    operation = operation_catalog().snapshot().operation(operation_id)
    return OperatorResult(operation=OperationProposal(
        operation_id=operation.id,
        version=operation.version,
        checksum=operation.checksum,
        inputs=inputs,
    ))


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
    assert run.metrics["server"]["queueWaitMs"] >= 0
    assert run.metrics["server"]["acceptedToFirstTextMs"] >= 0
    assert run.metrics["server"]["acceptedToFinishedMs"] >= run.metrics["server"]["acceptedToFirstTextMs"]
    assert run.metrics["server"]["eventCounts"]["RUN_STARTED"] == 1
    assert run.metrics["server"]["eventCounts"]["RUN_FINISHED"] == 1
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
    assert "reasoningTrace" not in trace["data"]


def test_model_pass_count_uses_provider_calls_not_orchestration_stages():
    steps = [
        {"stageId": "operator", "tool": "operator"},
        {"stageId": "model_pass_planner", "tool": "gpt-5.6-terra"},
        {"stageId": "planner", "tool": "planner"},
        {"stageId": "validator", "tool": "validator"},
        {"stageId": "execution", "tool": "analysis_harness"},
    ]

    assert agui_service._model_pass_count(steps) == 3


def test_pending_widget_becomes_interrupt_and_resume_uses_governed_action(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    first, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "Create a ₹2 lakh vacation goal", "messageId": "goal-request"},
        "goal-request",
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
        exported_response = client.get("/privacy/export")
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


def test_category_budget_amount_resume_preserves_category_and_saves_once(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    category = Category(
        slug=f"custom-{uuid4().hex}",
        name="Construction",
        icon="hammer",
        scope=TaxonomyScope.USER.value,
        owner_user_id=user.id,
    )
    db.add(category)
    db.commit()
    conversation = get_or_create_conversation(db, user)

    first, _live = _execute(
        db,
        user,
        conversation,
        {
            "kind": "message",
            "text": "Setup budget for construction",
            "messageId": "construction-budget",
        },
        "construction-budget",
    )

    assert first.status == AgentRunStatus.INTERRUPTED.value
    clarification_interrupt = db.scalar(select(AgentInterrupt).where(
        AgentInterrupt.run_id == first.id,
        AgentInterrupt.status == AgentInterruptStatus.OPEN.value,
    ))
    continuation = clarification_interrupt.metadata_payload["continuation"]
    assert clarification_interrupt.reason == "clarification"
    assert continuation["customStrategy"] == "budget_amount"
    assert continuation["customBudget"]["categoryId"] == str(category.id)
    assert db.scalar(select(Budget)) is None
    clarification_message = db.get(Message, first.final_message_id)
    clarification_widget = next(
        item for item in clarification_message.widgets if item["type"] == "clarification"
    )
    assert clarification_interrupt.metadata_payload["widget"] == clarification_widget
    assert clarification_widget["data"]["options"] == []
    custom = next(item for item in clarification_widget["actions"] if item["id"] == "custom")

    saved, _live = _execute(db, user, conversation, {
        "kind": "resume",
        "entries": [{
            "interruptId": str(clarification_interrupt.id),
            "status": "resolved",
            "payload": {
                "approved": True,
                "editedArgs": {
                    "widgetId": clarification_widget["id"],
                    "action": custom["action"],
                    "payload": {**custom["payload"], "customText": "₹25,000"},
                    "completeWidget": True,
                },
            },
        }],
    })

    assert saved.status == AgentRunStatus.SUCCEEDED.value
    assert db.scalar(select(AgentInterrupt).where(
        AgentInterrupt.run_id == saved.id,
        AgentInterrupt.status == AgentInterruptStatus.OPEN.value,
    )) is None
    budget_message = db.get(Message, saved.final_message_id)
    budget_widget = next(item for item in budget_message.widgets if item["type"] == "budget_progress")
    assert {item["action"] for item in budget_widget["actions"]} == {
        "edit_budget",
        "request_delete_budget",
    }
    budget = db.scalar(select(Budget))
    assert budget.category_id == category.id
    assert budget.amount_minor == 2_500_000


def test_goal_amount_resume_keeps_the_goal_sequence_typed_until_terminal_acknowledgement(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)

    first, _live = _execute(
        db,
        user,
        conversation,
        {
            "kind": "message",
            "text": "Create a vacation goal",
            "messageId": "vacation-goal",
        },
        "vacation-goal",
    )

    assert first.status == AgentRunStatus.INTERRUPTED.value
    amount_interrupt = db.scalar(select(AgentInterrupt).where(
        AgentInterrupt.run_id == first.id,
        AgentInterrupt.status == AgentInterruptStatus.OPEN.value,
    ))
    continuation = amount_interrupt.metadata_payload["continuation"]
    assert continuation["customStrategy"] == "goal_amount"
    assert continuation["customGoal"] == {
        "schemaVersion": 1,
        "operation": "save_goal",
        "goalId": None,
        "name": "Vacation",
        "currency": "INR",
    }
    amount_message = db.get(Message, first.final_message_id)
    amount_widget = next(item for item in amount_message.widgets if item["type"] == "clarification")
    custom = next(item for item in amount_widget["actions"] if item["id"] == "custom")

    proposed, _live = _execute(db, user, conversation, {
        "kind": "resume",
        "entries": [{
            "interruptId": str(amount_interrupt.id),
            "status": "resolved",
            "payload": {
                "approved": True,
                "editedArgs": {
                    "widgetId": amount_widget["id"],
                    "action": custom["action"],
                    "payload": {**custom["payload"], "customText": "₹2 lakh"},
                    "completeWidget": True,
                },
            },
        }],
    })

    assert proposed.status == AgentRunStatus.INTERRUPTED.value
    db.refresh(amount_message)
    updated_amount_widget = next(
        item for item in amount_message.widgets if item["id"] == amount_widget["id"]
    )
    assert updated_amount_widget["data"]["lifecycle"] == "completed"
    assert updated_amount_widget["actions"] == []
    assert db.scalar(select(Goal)) is None
    goal_interrupt = db.scalar(select(AgentInterrupt).where(
        AgentInterrupt.run_id == proposed.id,
        AgentInterrupt.status == AgentInterruptStatus.OPEN.value,
    ))
    goal_message = db.get(Message, proposed.final_message_id)
    goal_widget = next(item for item in goal_message.widgets if item["type"] == "goal_progress")
    save = next(item for item in goal_widget["actions"] if item["action"] == "save_goal")

    saved, _live = _execute(db, user, conversation, {
        "kind": "resume",
        "entries": [{
            "interruptId": str(goal_interrupt.id),
            "status": "resolved",
            "payload": {
                "approved": True,
                "editedArgs": {
                    "widgetId": goal_widget["id"],
                    "action": save["action"],
                    "payload": save["payload"],
                    "completeWidget": True,
                },
            },
        }],
    })

    assert saved.status == AgentRunStatus.SUCCEEDED.value
    db.refresh(goal_message)
    updated_goal_widget = next(
        item for item in goal_message.widgets if item["id"] == goal_widget["id"]
    )
    assert updated_goal_widget["data"]["lifecycle"] == "completed"
    assert updated_goal_widget["actions"] == []
    terminal_message = db.get(Message, saved.final_message_id)
    terminal_widget = next(item for item in terminal_message.widgets if item["type"] == "goal_progress")
    assert terminal_widget["data"]["targetMinor"] == 20_000_000
    assert terminal_widget["actions"] == []
    assert db.scalar(select(Goal)).target_minor == 20_000_000
    assert not list(db.scalars(
        select(AgentInterrupt)
        .join(AgentRun, AgentRun.id == AgentInterrupt.run_id)
        .where(
            AgentRun.conversation_id == conversation.id,
            AgentInterrupt.status == AgentInterruptStatus.OPEN.value,
        )
    ))


def test_compound_taxonomy_request_has_one_approval_and_executes_the_whole_path(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    original = "Create a category called Pet Care with a Vet Sub Category"
    decision = CopilotDecision(
        tool=CapabilityId.MANAGE_TAXONOMY,
        taxonomy=TaxonomyInterpretation(
            operation="create_taxonomy_path",
            name="Pet Care",
            subcategories=["Vet"],
        ),
        confidence=1,
        reason="The explicit request is one compound taxonomy mutation.",
    )
    monkeypatch.setattr(
        conversation_service,
        "_fast_path_decision",
        lambda text, *args, **kwargs: (decision, None) if text == original else None,
    )

    first, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": original, "messageId": "compound-taxonomy"},
        "compound-taxonomy",
    )

    assert first.status == AgentRunStatus.INTERRUPTED.value
    interrupt = db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == first.id))
    assert interrupt.reason == "tool_call"
    assert interrupt.metadata_payload["action"] == "create_taxonomy_path"
    origin = db.get(Message, first.final_message_id)
    assert not any(item["type"] == "clarification" for item in origin.widgets)
    editor = next(item for item in origin.widgets if item["type"] == "taxonomy_editor")
    assert editor["data"]["name"] == "Pet Care"
    assert editor["data"]["subcategories"] == ["Vet"]

    resumed, _live = _execute(db, user, conversation, {
        "kind": "resume",
        "entries": [{
            "interruptId": str(interrupt.id),
            "status": "resolved",
            "payload": {
                "approved": True,
                "editedArgs": interrupt.metadata_payload["proposedArgs"],
            },
        }],
    })

    assert resumed.status == AgentRunStatus.SUCCEEDED.value
    category = db.scalar(select(Category).where(
        Category.owner_user_id == user.id,
        Category.name == "Pet Care",
    ))
    assert category is not None
    assert db.scalar(select(Subcategory).where(
        Subcategory.category_id == category.id,
        Subcategory.name == "Vet",
    )) is not None
    assert not list(db.scalars(
        select(AgentInterrupt)
        .join(AgentRun, AgentRun.id == AgentInterrupt.run_id)
        .where(
            AgentRun.conversation_id == conversation.id,
            AgentInterrupt.status == AgentInterruptStatus.OPEN.value,
        )
    ))


def test_legacy_taxonomy_clarification_compiles_to_approval_without_rerouting(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    original = "Create a category called Pet Care with a Vet Sub Category"
    clarification = ClarificationRequest(
        question="Should I create the “Vet” subcategory under “Pet Care” as well?",
        reason="The requested subcategory is missing from the taxonomy operation.",
        conflict_fields=["taxonomy.operation", "taxonomy.name"],
        options=[
            ClarificationOption(
                id="create_both",
                label="Create Pet Care and Vet",
                description="Create the “Pet Care” category with “Vet” as its subcategory.",
                resolution="Create the category and its subcategory.",
            ),
            ClarificationOption(
                id="category_only",
                label="Create Pet Care only",
                resolution="Omit the subcategory.",
            ),
        ],
    )
    monkeypatch.setattr(
        conversation_service,
        "_fast_path_decision",
        lambda text, *args, **kwargs: (
            CopilotDecision(
                tool=CapabilityId.REQUEST_CLARIFICATION,
                clarification=clarification,
                confidence=1,
                reason="The route lost one requested taxonomy item.",
            ),
            None,
        ) if text == original else None,
    )
    first, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": original, "messageId": "legacy-taxonomy"},
        "legacy-taxonomy",
    )
    interrupt = db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == first.id))
    assert interrupt.metadata_payload["continuation"]["options"]["create_both"]["kind"] == "legacy_prompt"
    origin = db.get(Message, first.final_message_id)
    widget = next(item for item in origin.widgets if item["type"] == "clarification")
    choice = next(item for item in widget["actions"] if item["id"] == "create_both")

    resumed, _live = _execute(db, user, conversation, {
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
    })

    assert resumed.status == AgentRunStatus.INTERRUPTED.value
    next_interrupt = db.scalar(select(AgentInterrupt).where(
        AgentInterrupt.run_id == resumed.id,
        AgentInterrupt.status == AgentInterruptStatus.OPEN.value,
    ))
    assert next_interrupt.reason == "tool_call"
    assert next_interrupt.metadata_payload["action"] == "create_taxonomy_path"
    response = db.get(Message, resumed.final_message_id)
    assert not any(item["type"] == "clarification" for item in response.widgets)
    editor = next(item for item in response.widgets if item["type"] == "taxonomy_editor")
    assert editor["data"]["name"] == "Pet Care"
    assert editor["data"]["subcategories"] == ["Vet"]


def test_legacy_clarification_stops_when_resume_reopens_the_same_fields(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    original = "Use either target amount or monthly contribution for my plan"
    clarification = ClarificationRequest(
        question="Which value should control the plan?",
        reason="The two supplied values imply different schedules.",
        conflict_fields=["target", "monthly_contribution"],
        options=[
            ClarificationOption(id="target", label="Use target", resolution="Use the target amount."),
            ClarificationOption(id="monthly", label="Use monthly amount", resolution="Use the monthly contribution."),
        ],
    )

    def repeated_clarification(text, *args, **kwargs):
        if text.startswith(original):
            return CopilotDecision(
                tool=CapabilityId.REQUEST_CLARIFICATION,
                clarification=clarification,
                confidence=1,
                reason="The route remains ambiguous.",
            ), None
        return None

    monkeypatch.setattr(conversation_service, "_fast_path_decision", repeated_clarification)
    first, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": original, "messageId": "clarification-loop"},
        "clarification-loop",
    )
    interrupt = db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == first.id))
    origin = db.get(Message, first.final_message_id)
    widget = next(item for item in origin.widgets if item["type"] == "clarification")
    choice = next(item for item in widget["actions"] if item["id"] == "target")
    settings = conversation_service.get_settings().model_copy(update={
        "primary_agent_enabled": True,
        "openai_api_key": "test-only",
    })
    monkeypatch.setattr(conversation_service, "get_settings", lambda: settings)

    def repeated_operator(text, *_args, **_kwargs):
        assert "Customer clarification (authoritative): Use target." in text
        return _operator_proposal("request_clarification", {
            "clarification": clarification.model_dump(mode="json", exclude_none=True),
        })

    monkeypatch.setattr(conversation_service, "run_operator", repeated_operator)

    resumed, _live = _execute(db, user, conversation, {
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
    })

    assert resumed.status == AgentRunStatus.SUCCEEDED.value
    assert resumed.task_status == "failed"
    assert resumed.error_code == "clarification_did_not_progress"
    reply = db.get(Message, resumed.final_message_id)
    assert "stopped instead of asking the same question again" in reply.content
    assert not any(item["type"] == "clarification" for item in reply.widgets)
    assert not list(db.scalars(
        select(AgentInterrupt)
        .join(AgentRun, AgentRun.id == AgentInterrupt.run_id)
        .where(
            AgentRun.conversation_id == conversation.id,
            AgentInterrupt.status == AgentInterruptStatus.OPEN.value,
        )
    ))


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
        if text != original:
            raise AssertionError("a confirmed clarification must bypass the ordinary fast path")
        return CopilotDecision(
            tool=CapabilityId.REQUEST_CLARIFICATION,
            clarification=clarification,
            confidence=1,
            reason="The supplied loan inputs conflict.",
        ), None

    monkeypatch.setattr(conversation_service, "_fast_path_decision", typed_gate)
    settings = conversation_service.get_settings().model_copy(update={
        "primary_agent_enabled": True,
        "openai_api_key": "test-only",
    })
    monkeypatch.setattr(conversation_service, "get_settings", lambda: settings)
    seen = {}
    expected_reply = "I’ll use the confirmed two-year tenure for the requested schedule."

    def resolved_operator(text, *_args, **_kwargs):
        seen["text"] = text
        return OperatorResult(reply=expected_reply)

    monkeypatch.setattr(conversation_service, "run_operator", resolved_operator)
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

    def unexpected_intake(*_args, **_kwargs):
        raise AssertionError("a confirmed clarification must bypass ordinary intake handlers")

    monkeypatch.setattr(conversation_service, "_conversation_rename_request", unexpected_intake)
    monkeypatch.setattr(conversation_service, "bind_repeat_analysis", unexpected_intake)
    monkeypatch.setattr(conversation_service, "_active_loan_chart_clarification", unexpected_intake)

    resumed, resumed_live = _execute(db, user, conversation, resume_payload)

    assert resumed.status == AgentRunStatus.SUCCEEDED.value
    assert resumed.parent_run_id == first.id
    started = next(event for _sequence, event in resumed_live if event["type"] == "RUN_STARTED")
    assert started["parentRunId"] == str(first.id)
    db.refresh(interrupt)
    assert interrupt.status == AgentInterruptStatus.RESOLVED.value
    messages = list(db.scalars(select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at, Message.id)))
    assert [message.content for message in messages if message.role == "user"] == [original]
    assert messages[-1].content == expected_reply
    assert original in seen["text"]
    assert "Use 24 months as authoritative" in seen["text"]
    completed_origin = db.get(Message, origin.id)
    completed = next(item for item in completed_origin.widgets if item["id"] == widget["id"])
    assert completed["data"]["lifecycle"] == "completed"
    assert completed["data"]["completion"]["values"]["optionId"] == "use_tenure"

    replayed, _live = _execute(db, user, conversation, resume_payload)
    assert replayed.status == AgentRunStatus.SUCCEEDED.value
    assert replayed.final_message_id == resumed.final_message_id


def test_custom_clarification_answer_uses_the_same_guarded_resume_lane(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    original = "Compare the loan schedules using the assumption I choose"
    clarification = ClarificationRequest(
        question="Which repayment assumption should I use?",
        reason="The schedule needs one controlling repayment assumption.",
        conflict_fields=["repayment_assumption"],
        options=[
            ClarificationOption(
                id="standard",
                label="Use the standard schedule",
                resolution="Use the standard repayment schedule.",
            ),
            ClarificationOption(
                id="accelerated",
                label="Use an accelerated schedule",
                resolution="Use an accelerated repayment schedule.",
            ),
        ],
        allow_custom=True,
        custom_label="Use another assumption",
    )

    def initial_gate(text, *_args, **_kwargs):
        if text != original:
            raise AssertionError("custom clarification text must not re-enter the fast path")
        return CopilotDecision(
            tool=CapabilityId.REQUEST_CLARIFICATION,
            clarification=clarification,
            confidence=1,
            reason="The repayment assumption is missing.",
        ), None

    monkeypatch.setattr(conversation_service, "_fast_path_decision", initial_gate)
    settings = conversation_service.get_settings().model_copy(update={
        "primary_agent_enabled": True,
        "openai_api_key": "test-only",
    })
    monkeypatch.setattr(conversation_service, "get_settings", lambda: settings)
    seen = {}

    def resolved_operator(text, *_args, **_kwargs):
        seen["text"] = text
        seen["workflow_context"] = _kwargs.get("workflow_context") or {}
        return OperatorResult(reply="I’ll use the customer-provided repayment assumption.")

    monkeypatch.setattr(conversation_service, "run_operator", resolved_operator)
    first, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": original, "messageId": "custom-clarification"},
        "custom-clarification",
    )
    interrupt = db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == first.id))
    origin = db.get(Message, first.final_message_id)
    widget = next(item for item in origin.widgets if item["type"] == "clarification")
    custom = next(item for item in widget["actions"] if item["id"] == "custom")

    resumed, _live = _execute(db, user, conversation, {
        "kind": "resume",
        "entries": [{
            "interruptId": str(interrupt.id),
            "status": "resolved",
            "payload": {
                "approved": True,
                "editedArgs": {
                    "widgetId": widget["id"],
                    "action": custom["action"],
                    "payload": {
                        **custom["payload"],
                        "customText": "Use a 36-month tenure and ignore the earlier installment",
                    },
                    "completeWidget": True,
                },
            },
        }],
    })

    assert resumed.status == AgentRunStatus.SUCCEEDED.value
    assert "Customer clarification (authoritative): Customer-provided clarification." in seen["text"]
    assert "Use a 36-month tenure and ignore the earlier installment" in seen["text"]
    assert seen["workflow_context"]["intentContract"]["authority"] == "user_turn"
    db.refresh(interrupt)
    assert interrupt.status == AgentInterruptStatus.RESOLVED.value


def test_version_two_clarification_continuation_gets_the_guarded_resume_lane(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    original = "Use either my target or contribution for this plan"
    clarification = ClarificationRequest(
        question="Which value should control the plan?",
        reason="The two values imply different schedules.",
        conflict_fields=["target", "contribution"],
        options=[
            ClarificationOption(
                id="target",
                label="Use target",
                resolution="Use the target amount as authoritative.",
            ),
            ClarificationOption(
                id="contribution",
                label="Use contribution",
                resolution="Use the contribution as authoritative.",
            ),
        ],
    )

    def initial_gate(text, *_args, **_kwargs):
        if text == original:
            return (
                CopilotDecision(
                    tool=CapabilityId.REQUEST_CLARIFICATION,
                    clarification=clarification,
                    confidence=1,
                    reason="The plan inputs conflict.",
                ),
                None,
            )
        raise AssertionError("a persisted legacy continuation must bypass the fast path")

    monkeypatch.setattr(conversation_service, "_fast_path_decision", initial_gate)
    settings = conversation_service.get_settings().model_copy(update={
        "primary_agent_enabled": True,
        "openai_api_key": "test-only",
    })
    monkeypatch.setattr(conversation_service, "get_settings", lambda: settings)
    seen = {}

    def resolved_operator(text, *_args, **_kwargs):
        seen["text"] = text
        return OperatorResult(reply="I’ll use the confirmed target amount.")

    monkeypatch.setattr(conversation_service, "run_operator", resolved_operator)

    first, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": original, "messageId": "legacy-v2-clarification"},
        "legacy-v2-clarification",
    )
    interrupt = db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == first.id))
    origin = db.get(Message, first.final_message_id)
    widget = next(item for item in origin.widgets if item["type"] == "clarification")
    choice = next(item for item in widget["actions"] if item["id"] == "target")
    metadata = json.loads(json.dumps(interrupt.metadata_payload))
    current = metadata["continuation"]
    metadata["continuation"] = {
        "schemaVersion": 2,
        "clarificationId": current["clarificationId"],
        "sourceMessageId": current["sourceMessageId"],
        "originalRequest": current["originalRequest"],
        "options": {
            "target": {
                "label": "Use target",
                "resolution": "Use the target amount as authoritative.",
            },
        },
        "allowCustom": False,
    }
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
                    "action": choice["action"],
                    "payload": choice["payload"],
                    "completeWidget": True,
                },
            },
        }],
    })

    assert resumed.status == AgentRunStatus.SUCCEEDED.value
    assert "Customer clarification (authoritative): Use target." in seen["text"]
    db.refresh(interrupt)
    assert interrupt.status == AgentInterruptStatus.RESOLVED.value


def test_cancel_clarification_option_closes_interrupt_and_allows_the_next_message(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    original = "Create a savings plan using either my target or monthly contribution."
    calls: list[str] = []
    clarification = ClarificationRequest(
        question="Which value should control the savings plan?",
        reason="The target and contribution imply different schedules.",
        conflict_fields=["target", "monthly_contribution"],
        options=[
            ClarificationOption(
                id="use_target",
                label="Use target",
                resolution="Use the target as authoritative.",
            ),
            ClarificationOption(
                id="cancel_plan",
                label="Cancel",
                description="Do not create this plan.",
                resolution="Cancel the requested savings plan.",
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
                reason="The controlling savings value is ambiguous.",
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
    cancel = next(item for item in widget["actions"] if item["id"] == "cancel_plan")
    # A thread may already be waiting on a cancellation option persisted by a
    # pre-disposition server. Its server-authored semantic id still has to be
    # recoverable after deployment.
    metadata = json.loads(json.dumps(interrupt.metadata_payload))
    current = metadata["continuation"]
    metadata["continuation"] = {
        "schemaVersion": 2,
        "clarificationId": current["clarificationId"],
        "sourceMessageId": current["sourceMessageId"],
        "originalRequest": current["originalRequest"],
        "options": {
            "use_target": {
                "label": "Use target",
                "resolution": "Use the target as authoritative.",
                "disposition": "continue",
            },
            "cancel_plan": {
                "label": "Cancel",
                "resolution": "Cancel the requested savings plan.",
            },
        },
        "allowCustom": True,
    }
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
    assert cancelled["data"]["completion"]["values"]["optionId"] == "cancel_plan"
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


def test_ambiguous_date_range_resumes_from_typed_intent_without_another_model(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    original = "Compare my spending from 03/04/2026 to 05/06/2026."

    first, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": original, "messageId": "ambiguous-date-range"},
        "ambiguous-date-range",
    )

    assert first.status == AgentRunStatus.INTERRUPTED.value
    assert first.task_status == "needs_input"
    interrupt = db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == first.id))
    continuation = interrupt.metadata_payload["continuation"]
    assert continuation["schemaVersion"] == 4
    selected_option = continuation["options"]["day_month_year"]
    assert selected_option["kind"] == "governed_query"
    assert selected_option["intent"] == {
        "schema_version": 1,
        "context_mode": "standalone",
        "capability": "search_transactions",
        "query": {
            "metric": "spending_summary",
            "result_mode": "summary",
            "operation": "total",
            "group_by": "none",
            "sort_direction": "desc",
            "transaction_type": "expense",
            "merchant": None,
            "category_slug": None,
            "subcategory_slug": None,
            "account": None,
            "tag": None,
            "min_amount_minor": None,
            "max_amount_minor": None,
            "start_date": "2026-04-03",
            "end_date": "2026-06-05",
            "limit": 50,
            "use_active_scope": False,
            "scope_transaction_ids": [],
        },
    }

    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("typed resume must not call the Operator")),
    )
    origin = db.get(Message, first.final_message_id)
    widget = next(item for item in origin.widgets if item["type"] == "clarification")
    choice = next(item for item in widget["actions"] if item["id"] == "day_month_year")
    resumed, _live = _execute(db, user, conversation, {
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
    })

    assert resumed.status == AgentRunStatus.SUCCEEDED.value
    assert resumed.task_status == "succeeded"
    answer = db.get(Message, resumed.final_message_id)
    assert "₹0" in answer.content
    assert "apr 03 – jun 05" in answer.content.casefold()
    assert answer.content.endswith("across 0 transactions.")
    activity = next(item for item in answer.widgets if item["type"] == "agent_activity")
    assert any(step["tool"] == "search_transactions" for step in activity["data"]["steps"])
    assert not any(str(step.get("tool", "")) in {"operator", "planner", "validator"} for step in activity["data"]["steps"])


def test_ambiguous_date_list_resume_honours_selected_format_without_reopening_clarification(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    original = "Hi, Show my expenses from 03/04/2026 to 05/06/2026"

    first, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": original, "messageId": "ambiguous-date-list"},
        "ambiguous-date-list",
    )

    assert first.status == AgentRunStatus.INTERRUPTED.value
    interrupt = db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == first.id))
    continuation = interrupt.metadata_payload["continuation"]
    assert continuation["options"]["day_month_year"]["kind"] == "legacy_prompt"

    seen = {}
    settings = conversation_service.get_settings().model_copy(update={
        "primary_agent_enabled": True,
        "openai_api_key": "test-only",
    })
    monkeypatch.setattr(conversation_service, "get_settings", lambda: settings)

    def resolved_operator(text, *_args, **_kwargs):
        seen["text"] = text
        return OperatorResult(reply="The confirmed period was applied to the expense request.")

    monkeypatch.setattr(conversation_service, "run_operator", resolved_operator)
    origin = db.get(Message, first.final_message_id)
    widget = next(item for item in origin.widgets if item["type"] == "clarification")
    choice = next(item for item in widget["actions"] if item["id"] == "day_month_year")
    resumed, _live = _execute(db, user, conversation, {
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
    })

    assert resumed.status == AgentRunStatus.SUCCEEDED.value, {
        "answer": db.get(Message, resumed.final_message_id).content if resumed.final_message_id else None,
        "interrupts": [
            item.metadata_payload
            for item in db.scalars(select(AgentInterrupt).where(AgentInterrupt.run_id == resumed.id))
        ],
        "operatorText": seen.get("text"),
    }
    assert "text" in seen, db.get(Message, resumed.final_message_id).content
    assert "Customer clarification (authoritative): Day / month / year." in seen["text"]
    assert "start_date=2026-04-03 and end_date=2026-06-05" in seen["text"]
    db.refresh(interrupt)
    assert interrupt.status == AgentInterruptStatus.RESOLVED.value
    completed_origin = db.get(Message, origin.id)
    completed = next(item for item in completed_origin.widgets if item["id"] == widget["id"])
    assert completed["data"]["lifecycle"] == "completed"
    assert completed["data"]["completion"]["values"]["optionId"] == "day_month_year"


def test_transport_success_does_not_hide_a_failed_financial_task(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)

    run, live = _execute(
        db,
        user,
        conversation,
        {
            "kind": "message",
            "text": "How much did I spend at a merchant with an unsupported filter?",
            "messageId": "unresolved-financial-query",
        },
        "unresolved-financial-query",
    )

    assert run.status == AgentRunStatus.SUCCEEDED.value
    assert run.task_status == "failed"
    assert run.failure_stage == "intent_resolution"
    assert run.error_code == "unresolved_financial_query"
    finished = next(event for _sequence, event in live if event["type"] == "RUN_FINISHED")
    assert finished["result"]["taskStatus"] == "failed"


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
    assert interrupt.metadata_payload["continuation"]["options"]["cancel"]["kind"] == "cancel"

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
        response = client.get("/agent/capabilities")

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
        streamed = client.post("/agent", json=payload)
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

        replayed = client.get(f"/agent/runs/{run_id}/events")
        replay_events = [
            json.loads(line.removeprefix("data: "))
            for line in replayed.text.splitlines()
            if line.startswith("data: ")
        ]
        assert replay_events == events

        continued = client.get(f"/agent/runs/{run_id}/events?after=2")
        continued_events = [
            json.loads(line.removeprefix("data: "))
            for line in continued.text.splitlines()
            if line.startswith("data: ")
        ]
        assert continued_events[0]["type"] == "RUN_STARTED"
        assert continued_events[0]["rawEvent"]["fyn"]["sequence"] == 2
        assert continued_events[1:] == events[2:]

        state = client.get(f"/agent/threads/{conversation.id}").json()
        assert state["activeRun"] is None
        assert state["latestRun"]["id"] == str(run_id)
        assert state["latestRun"]["lastSequence"] == len(events)

        telemetry = client.post(f"/agent/runs/{run_id}/telemetry", json={
            "schemaVersion": 1,
            "submitToRunCreatedMs": 0.4,
            "submitToFirstActivityReceivedMs": 18.2,
            "submitToFirstTextReceivedMs": 740.5,
            "submitToFirstAnswerVisibleMs": 756.1,
            "submitToResponseResolvedMs": 800.0,
            "submitToComposerUnlockedMs": 816.2,
            "pageVisibleAtSubmit": True,
            "replayed": False,
        })
        assert telemetry.status_code == 204
        db.expire_all()
        stored = db.get(AgentRun, run_id)
        assert stored.metrics["client"]["submitToFirstAnswerVisibleMs"] == 756.1
        assert stored.metrics["client"]["submitToComposerUnlockedMs"] == 816.2
        assert stored.metrics["server"]["eventCounts"]["RUN_FINISHED"] == 1


def test_detached_client_telemetry_contains_persistence_failure(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    run, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "Hi", "messageId": "telemetry-failure"},
        "telemetry-failure",
    )
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[current_user] = lambda: user

    def fail_commit():
        raise RuntimeError("telemetry store unavailable")

    monkeypatch.setattr(db, "commit", fail_commit)
    with TestClient(application) as client:
        response = client.post(f"/agent/runs/{run.id}/telemetry", json={
            "schemaVersion": 1,
            "submitToComposerUnlockedMs": 400,
        })

    assert response.status_code == 204


def test_pending_interrupt_rejects_new_input_with_run_error_event(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    first, _ = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "Create a ₹2 lakh vacation goal", "messageId": "pending-goal"},
        "pending-goal",
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
        streamed = client.post("/agent", json=payload)
    events = [
        json.loads(line.removeprefix("data: "))
        for line in streamed.text.splitlines()
        if line.startswith("data: ")
    ]
    assert streamed.status_code == 200
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "pending_interrupt"

    # Supersession is reserved for a newer persisted card. A fabricated widget
    # id cannot be used merely to knock an open decision out of the protocol.
    bogus_action = {
        **payload,
        "runId": str(uuid4()),
        "messages": [],
        "forwardedProps": {
            "fynAction": {
                "widgetId": "not-a-persisted-widget",
                "action": "save_budget",
                "payload": {"amountMinor": 2_000_000},
                "completeWidget": True,
            },
        },
    }
    with TestClient(application) as client:
        rejected = client.post("/agent", json=bogus_action)
    rejected_events = [
        json.loads(line.removeprefix("data: "))
        for line in rejected.text.splitlines()
        if line.startswith("data: ")
    ]
    assert rejected_events[-1]["code"] == "pending_interrupt"
    assert db.scalar(
        select(AgentInterrupt.status).where(AgentInterrupt.run_id == first.id)
    ) == AgentInterruptStatus.OPEN.value


def test_newer_widget_action_supersedes_an_older_interrupt(db):
    """A thread persisted by an older upload flow must be recoverable through
    the visible card; the customer should not have to find the hidden prompt or
    attach the same statement again."""
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    first, _ = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "Create a ₹2 lakh vacation goal", "messageId": "stale-goal"},
        "stale-goal",
    )
    stale_interrupt = db.scalar(
        select(AgentInterrupt).where(
            AgentInterrupt.run_id == first.id,
            AgentInterrupt.status == AgentInterruptStatus.OPEN.value,
        )
    )
    assert stale_interrupt is not None

    staged = Import(
        user_id=user.id,
        source_type=FinancialSourceType.CSV.value,
        filename="statement.csv",
        file_hash="staged-after-interrupt",
        status=ImportStatus.AWAITING_CONFIRMATION.value,
        total_records=0,
        high_confidence_records=0,
        review_records=0,
        duplicate_records=0,
    )
    db.add(staged)
    db.commit()
    preview = _record_import_preview(
        db,
        conversation,
        staged.filename,
        import_summary(staged, idempotent_replay=False),
    )
    action = preview.agent_response.widgets[0].actions[0]

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[current_user] = lambda: user
    payload = {
        "threadId": str(conversation.id),
        "runId": str(uuid4()),
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {
            "fynAction": {
                "widgetId": preview.agent_response.widgets[0].id,
                "action": action.action.value,
                "payload": action.payload,
                "completeWidget": True,
            },
        },
    }

    with TestClient(application) as client:
        streamed = client.post("/agent", json=payload)
    events = [
        json.loads(line.removeprefix("data: "))
        for line in streamed.text.splitlines()
        if line.startswith("data: ")
    ]

    assert events[-1]["type"] == "RUN_FINISHED"
    db.refresh(stale_interrupt)
    assert stale_interrupt.status == AgentInterruptStatus.CANCELLED.value
    response_event = next(
        event for event in events
        if event["type"] == "CUSTOM" and event["name"] == "fyn.response.v1"
    )
    update = next(
        item for item in response_event["value"]["response"]["widgetUpdates"]
        if item["widgetId"] == stale_interrupt.widget_id
    )
    assert update["widget"]["data"]["lifecycle"] == "cancelled"


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


def test_followup_progress_events_batch_without_changing_replay_order(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    run = AgentRun(
        id=uuid4(),
        user_id=user.id,
        conversation_id=conversation.id,
        status=AgentRunStatus.RUNNING.value,
        input_payload={"kind": "message", "text": "Hi"},
        last_sequence=0,
    )
    db.add(run)
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    clock = iter([10.0, 10.01, 10.04, 10.04])
    monkeypatch.setattr(agui_service.time, "monotonic", lambda: next(clock))
    delivered: list[tuple[int, dict]] = []
    publisher = DurableEventPublisher(
        factory,
        run.id,
        user.id,
        0,
        lambda sequence, event: delivered.append((sequence, event)),
    )
    event_one = RunStartedEvent(thread_id=str(conversation.id), run_id=str(run.id))
    publisher.emit(event_one)
    assert publisher.flush_if_due(max_delay_ms=32, max_events=12) is False
    publisher.emit(event_one)
    assert publisher.flush_if_due(max_delay_ms=32, max_events=12) is True

    stored = list(
        db.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.sequence))
    )
    assert [event.sequence for event in stored] == [1, 2]
    assert [sequence for sequence, _event in delivered] == [1, 2]


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


def test_recovery_resumes_failed_answer_postprocessing_without_replaying_the_turn(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    question = Message(
        id=uuid4(),
        conversation_id=conversation.id,
        role="user",
        content="Show recurring charges",
        widgets=[],
        citations=[],
    )
    answer = Message(
        id=uuid4(),
        conversation_id=conversation.id,
        role="assistant",
        content="No recurring charges were found.",
        widgets=[],
        citations=[],
    )
    run = AgentRun(
        id=uuid4(),
        user_id=user.id,
        conversation_id=conversation.id,
        status=AgentRunStatus.RUNNING.value,
        task_status="failed",
        input_payload={"kind": "message", "text": question.content},
        final_message_id=answer.id,
        recovery_phase=agui_service.POSTPROCESS_RECOVERY_PHASE,
        recovery_payload={"schemaVersion": 1, "userMessageId": str(question.id), "widgetUpdates": []},
        last_sequence=3,
        started_at=now_utc() - timedelta(seconds=5),
    )
    db.add_all([question, answer, run])
    db.add_all([
        AgentEvent(
            run_id=run.id,
            sequence=1,
            event_type="RUN_STARTED",
            payload={"type": "RUN_STARTED", "threadId": str(conversation.id), "runId": str(run.id)},
        ),
        AgentEvent(
            run_id=run.id,
            sequence=2,
            event_type="TEXT_MESSAGE_START",
            payload={"type": "TEXT_MESSAGE_START", "messageId": str(answer.id), "role": "assistant"},
        ),
        AgentEvent(
            run_id=run.id,
            sequence=3,
            event_type="TEXT_MESSAGE_CONTENT",
            payload={"type": "TEXT_MESSAGE_CONTENT", "messageId": str(answer.id), "delta": answer.content},
        ),
    ])
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(
        agui_service,
        "handle_chat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("financial turn replayed")),
    )

    queued = recover_agent_runs(factory, limit=1, created_before=now_utc() + timedelta(seconds=1))
    assert queued == [(run.id, user.id, 3)]
    publisher = DurableEventPublisher(factory, run.id, user.id, 3, lambda _sequence, _event: None)
    execute_run(factory, run.id, user.id, publisher)

    db.expire_all()
    recovered = db.get(AgentRun, run.id)
    stored = db.get(Message, answer.id)
    events = list(
        db.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.sequence))
    )
    assert recovered.status == AgentRunStatus.SUCCEEDED.value
    assert recovered.task_status == "failed"
    assert recovered.recovery_phase is None
    # Optional enrichment no longer runs inside recovery or delays the
    # recovered answer's terminal event.
    assert [item["type"] for item in stored.widgets].count("related_questions") == 0
    assert [item["type"] for item in stored.widgets].count("agent_activity") == 1
    assert [event.event_type for event in events].count("TEXT_MESSAGE_START") == 1
    assert [event.event_type for event in events].count("TEXT_MESSAGE_CONTENT") == 1
    assert events[-1].event_type == "RUN_FINISHED"
    custom = next(event for event in events if event.event_type == "CUSTOM")
    assert custom.payload["value"]["response"]["user_message_id"] == str(question.id)


def test_recovery_leaves_already_committed_suggestions_untouched(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    widget_id = f"related-questions-{uuid4()}"
    answer = Message(
        id=uuid4(),
        conversation_id=conversation.id,
        role="assistant",
        content="A committed answer.",
        widgets=[
            {
                "id": widget_id,
                "type": "related_questions",
                "version": 1,
                "data": {"questions": ["What changed in August 2026?"]},
                "actions": [],
            }
        ],
        citations=[],
    )
    run = AgentRun(
        id=uuid4(),
        user_id=user.id,
        conversation_id=conversation.id,
        status=AgentRunStatus.RUNNING.value,
        task_status="succeeded",
        input_payload={"kind": "message", "text": "What changed?"},
        final_message_id=answer.id,
        recovery_phase=agui_service.POSTPROCESS_RECOVERY_PHASE,
        recovery_payload={"schemaVersion": 1, "widgetUpdates": []},
        last_sequence=0,
    )
    db.add_all([answer, run])
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    queued = recover_agent_runs(factory, limit=1, created_before=now_utc() + timedelta(seconds=1))
    publisher = DurableEventPublisher(factory, run.id, user.id, queued[0][2], lambda _sequence, _event: None)
    execute_run(factory, run.id, user.id, publisher)

    db.expire_all()
    stored = db.get(Message, answer.id)
    assert [item["type"] for item in stored.widgets].count("related_questions") == 1


def test_recovery_batch_claims_are_bounded_even_with_a_large_backlog(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    runs = [
        AgentRun(
            id=uuid4(),
            user_id=user.id,
            conversation_id=conversation.id,
            status=AgentRunStatus.QUEUED.value,
            input_payload={"kind": "message", "text": f"queued {index}"},
            last_sequence=0,
        )
        for index in range(40)
    ]
    db.add_all(runs)
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)

    claimed = recover_agent_runs(
        factory,
        limit=3,
        created_before=now_utc() + timedelta(seconds=1),
    )

    assert len(claimed) == 3
    statuses = list(db.scalars(select(AgentRun.status).where(AgentRun.id.in_([run.id for run in runs]))))
    assert statuses.count(AgentRunStatus.RECOVERING.value) == 3
    assert statuses.count(AgentRunStatus.QUEUED.value) == 37


def test_recovery_attempt_cap_finishes_answer_without_optional_enrichment(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    answer = Message(
        id=uuid4(),
        conversation_id=conversation.id,
        role="assistant",
        content="The core answer is already safe.",
        widgets=[],
        citations=[],
    )
    run = AgentRun(
        id=uuid4(),
        user_id=user.id,
        conversation_id=conversation.id,
        status=AgentRunStatus.RUNNING.value,
        task_status="succeeded",
        input_payload={"kind": "message", "text": "A read-only question"},
        final_message_id=answer.id,
        recovery_phase=agui_service.POSTPROCESS_RECOVERY_PHASE,
        recovery_payload={"schemaVersion": 1, "widgetUpdates": []},
        last_sequence=0,
    )
    db.add_all([answer, run])
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    work = claim_agent_recovery_work(
        factory,
        created_before=now_utc() + timedelta(seconds=1),
        claim_ttl_seconds=300,
        max_postprocess_attempts=0,
    )
    assert work is not None and work.executable
    publisher = DurableEventPublisher(factory, run.id, user.id, work.last_sequence, lambda _sequence, _event: None)
    execute_run(factory, run.id, user.id, publisher)

    db.expire_all()
    recovered = db.get(AgentRun, run.id)
    stored = db.get(Message, answer.id)
    assert recovered.status == AgentRunStatus.SUCCEEDED.value
    assert all(item["type"] != "related_questions" for item in stored.widgets)


def test_live_recovery_lease_prevents_duplicate_execution(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    run = AgentRun(
        id=uuid4(),
        user_id=user.id,
        conversation_id=conversation.id,
        status=AgentRunStatus.RUNNING.value,
        input_payload={"kind": "message", "text": "Already owned"},
        recovery_claimed_at=now_utc(),
        last_sequence=0,
    )
    db.add(run)
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    cutoff = now_utc() + timedelta(seconds=1)

    work = claim_agent_recovery_work(
        factory,
        created_before=cutoff,
        claim_ttl_seconds=300,
        max_postprocess_attempts=2,
    )

    assert work is None
    assert agent_recovery_backlog_exists(factory, created_before=cutoff)
    db.refresh(run)
    assert run.status == AgentRunStatus.RUNNING.value


def test_thread_rename_request_offers_hitl_confirmation_and_resume_renames(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    first, _live = _execute(
        db,
        user,
        conversation,
        {
            "kind": "message",
            "text": 'Can you update the page title to "Monthly food audit"?',
            "messageId": "rename-request",
        },
        "rename-request",
    )

    assert first.status == AgentRunStatus.INTERRUPTED.value
    assert first.task_status == "needs_input"
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
    action = next(item for item in widget["actions"] if item["action"] == "rename_conversation")
    assert action["payload"] == {"title": "Monthly food audit"}

    resumed, _live = _execute(
        db,
        user,
        conversation,
        {
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
        },
        "rename-resume",
    )

    assert resumed.status == AgentRunStatus.SUCCEEDED.value
    db.refresh(conversation)
    assert conversation.title == "Monthly food audit"
    reply = db.get(Message, resumed.final_message_id)
    assert "Renamed this thread" in reply.content


def test_successful_message_turn_finishes_before_related_questions(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    seen = {}

    def fake_suggest(question, answer, recent_turns, capability_notes, current_date, user_timezone):
        seen["question"] = question
        seen["answer"] = answer
        seen["capabilities"] = capability_notes
        return ["What did I spend on food in August 2026?"]

    monkeypatch.setattr(enrichment_service, "suggest_related_questions", fake_suggest)

    run, live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "Spent ₹300 on coffee today", "messageId": "rq-save"},
        "rq-save",
    )

    assert run.status == AgentRunStatus.SUCCEEDED.value
    message = db.get(Message, run.final_message_id)
    assert all(item["type"] != "related_questions" for item in message.widgets)
    custom = next(event for _sequence, event in live if event["type"] == "CUSTOM")
    assert all(
        item["type"] != "related_questions"
        for item in custom["value"]["response"]["widgets"]
    )
    # The run is already terminal and usable before the worker is allowed to
    # claim the independent queue item.
    pending = db.scalar(select(AgentEnrichment).where(AgentEnrichment.run_id == run.id))
    assert pending is not None
    assert pending.status == AgentEnrichmentStatus.PENDING.value

    enrichment = _process_one_enrichment(db)
    assert enrichment.status == AgentEnrichmentStatus.COMPLETED.value
    message = db.get(Message, run.final_message_id)
    widget = next(item for item in message.widgets if item["type"] == "related_questions")
    assert widget["data"]["questions"] == ["What did I spend on food in August 2026?"]
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[current_user] = lambda: user
    with TestClient(application) as client:
        payload = client.get(f"/agent/runs/{run.id}/related-questions")
    assert payload.status_code == 200
    assert payload.json()["status"] == AgentEnrichmentStatus.COMPLETED.value
    assert payload.json()["widget"]["data"]["questions"] == ["What did I spend on food in August 2026?"]
    # The suggester saw the finished Q&A and the real capability surface.
    assert seen["question"] == "Spent ₹300 on coffee today"
    assert seen["answer"] == message.content
    # Analytical reads live in the governed harness, not the tool registry, so
    # the surface the suggester sees must still describe them.
    assert any("transaction_list" in note for note in seen["capabilities"])
    assert any("Governed analyses:" in note for note in seen["capabilities"])
    # Enrichment never rewrites the completed run's immutable event stream.
    assert all(
        item["type"] != "related_questions"
        for item in custom["value"]["response"]["widgets"]
    )


def test_pending_hitl_turn_does_not_enqueue_related_questions(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)

    run, live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "create category path", "messageId": "pending-hitl-fast-path"},
        "pending-hitl-fast-path",
    )

    assert run.status == AgentRunStatus.INTERRUPTED.value
    message = db.get(Message, run.final_message_id)
    assert any(item["type"] == "operation_form" for item in message.widgets)
    assert all(item["type"] != "related_questions" for item in message.widgets)
    custom = next(event for _sequence, event in live if event["type"] == "CUSTOM")
    assert all(
        item["type"] != "related_questions"
        for item in custom["value"]["response"]["widgets"]
    )
    assert db.scalar(select(AgentEnrichment).where(AgentEnrichment.run_id == run.id)) is None


def test_related_question_rollout_zero_never_queues_optional_work(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)

    with monkeypatch.context() as scoped:
        scoped.setenv("AGENT_ENRICHMENT_ROLLOUT_PERCENT", "0")
        enrichment_service.get_settings.cache_clear()
        run, _live = _execute(
            db,
            user,
            conversation,
            {"kind": "message", "text": "Spent ₹300 on tea today", "messageId": "rq-control"},
            "rq-control",
        )

    enrichment_service.get_settings.cache_clear()
    assert run.status == AgentRunStatus.SUCCEEDED.value
    assert run.metrics["rollouts"]["agent_enrichment"] == "control"
    assert db.scalar(select(AgentEnrichment).where(AgentEnrichment.run_id == run.id)) is None


def test_failed_message_turn_attaches_recovery_questions(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(
        enrichment_service,
        "now_utc",
        lambda: datetime(2026, 8, 19, 3, 0, tzinfo=timezone.utc),
    )

    run, live = _execute(
        db,
        user,
        conversation,
        {
            "kind": "message",
            "text": "How much did I spend at a merchant with an unsupported filter?",
            "messageId": "rq-failed-task",
        },
        "rq-failed-task",
    )

    assert run.status == AgentRunStatus.SUCCEEDED.value
    assert run.task_status == "failed"
    message = db.get(Message, run.final_message_id)
    assert all(item["type"] != "related_questions" for item in message.widgets)
    custom = next(event for _sequence, event in live if event["type"] == "CUSTOM")
    assert all(
        item["type"] != "related_questions"
        for item in custom["value"]["response"]["widgets"]
    )

    enrichment = _process_one_enrichment(db)
    assert enrichment.status == AgentEnrichmentStatus.COMPLETED.value
    message = db.get(Message, run.final_message_id)
    widget = next(item for item in message.widgets if item["type"] == "related_questions")
    assert widget["data"]["questions"] == [
        "Which August 2026 categories cost the most?",
        "Which August 2026 expenses were discretionary?",
        "How did August 2026 spending compare with July 2026?",
    ]


def test_related_question_failures_never_harm_the_answer(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("suggestion model outage")

    monkeypatch.setattr(enrichment_service, "suggest_related_questions", unavailable)

    run, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "Spent ₹300 on coffee today", "messageId": "rq-outage"},
        "rq-outage",
    )

    assert run.status == AgentRunStatus.SUCCEEDED.value
    message = db.get(Message, run.final_message_id)
    assert message.content
    assert all(item["type"] != "related_questions" for item in message.widgets)
    enrichment = _process_one_enrichment(db, max_attempts=1)
    assert enrichment.status == AgentEnrichmentStatus.FAILED.value
    # The failure is durably recorded instead of silently swallowed.
    from app.models import AIAction

    record = db.scalar(
        select(AIAction).where(
            AIAction.conversation_id == conversation.id,
            AIAction.action_type == "suggester",
        )
    )
    assert record is not None
    assert record.payload_redacted["errorType"] == "RuntimeError"


def test_explicit_ask_for_question_ideas_answers_with_chips_once(db, monkeypatch):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(
        conversation_service,
        "suggest_related_questions",
        lambda *args, **kwargs: ["How much did I spend in August 2026?"],
    )

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("The garnish pass must not duplicate an ideas answer")

    monkeypatch.setattr(enrichment_service, "suggest_related_questions", must_not_run)

    run, _live = _execute(
        db,
        user,
        conversation,
        {"kind": "message", "text": "Show me few suggested questiosn", "messageId": "ideas-typo"},
        "ideas-typo",
    )

    assert run.status == AgentRunStatus.SUCCEEDED.value
    message = db.get(Message, run.final_message_id)
    chip_widgets = [item for item in message.widgets if item["type"] == "related_questions"]
    assert len(chip_widgets) == 1
    assert chip_widgets[0]["data"]["questions"] == ["How much did I spend in August 2026?"]
    assert "tap one" in message.content
    assert db.scalar(select(AgentEnrichment).where(AgentEnrichment.run_id == run.id)) is None
