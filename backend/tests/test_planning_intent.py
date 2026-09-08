from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import AgentEvent, AgentInterrupt, Budget, Goal, Message, User
from app.operation_types import ContextRelationship, RequestedEffect
from app.seed import DEFAULT_USER_EMAIL
from app.services import conversation as conversation_service
from app.services.agents import OperatorResult
from app.services.capabilities import capability_for_primitive, capability_spec
from app.services.conversation import get_or_create_conversation
from app.services.turn_policy import authorize_capability, resolve_turn_intent
from test_agui_runtime import _execute, _operator_proposal


@pytest.fixture
def agent_enabled(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("text", [
    "Let's set construction budget to 50k",
    "Let’s set construction budget to 50k",
    "Please lower my travel budget to 20k",
    "Could you please raise my food budget to 20k?",
    "Set my food budget to Rs. 20000.50",
])
def test_budget_routing_and_authorization_agree_on_explicit_requests(text):
    decision, extracted = conversation_service._fast_path_decision(text, date(2026, 9, 9))
    assert decision.tool == capability_for_primitive("budget.manage@1")
    assert extracted is None
    intent = resolve_turn_intent(text, ContextRelationship.STANDALONE)
    assert intent.requested_effect is RequestedEffect.MUTATION
    assert authorize_capability(intent, capability_spec(decision.tool)).allowed


@pytest.mark.parametrize("text", [
    "How to setup custom budget",
    "How can I set a custom budget?",
    "Can I create a vacation goal?",
    "I will set the goal but before that want to have a discussion",
    "Let's discuss how to set a budget",
    "Don't create a vacation goal",
    "Do not set a food budget",
    "I don't want to create a goal",
    "Set a food budget later, first let's discuss it",
    "If I set a 50k budget, how much could I save?",
])
def test_questions_negation_and_deferred_plans_do_not_request_planning_mutation(text):
    assert not conversation_service._looks_like_planning_command(text)
    assert not conversation_service._looks_like_budget_mutation_command(text)
    intent = resolve_turn_intent(text, ContextRelationship.STANDALONE)
    assert intent.requested_effect is RequestedEffect.NONE
    assert not authorize_capability(intent, capability_spec(capability_for_primitive("budget.manage@1"))).allowed


@pytest.mark.parametrize("text", [
    "Set a food budget to 50k and create a vacation goal",
    "Set a food budget to 50k. Delete my travel budget",
    "Set a food budget and a vacation goal to 50k",
])
def test_compound_planning_requests_are_not_partially_executed(text):
    assert not conversation_service._looks_like_planning_command(text)


def _thread(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    return user, get_or_create_conversation(db, user)


def _assert_terminal(db, run, live):
    events = list(db.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.sequence)))
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert [(event.sequence, event.payload) for event in events] == live
    assert [event.event_type for event in events if event.event_type in {"RUN_FINISHED", "RUN_ERROR"}] == ["RUN_FINISHED"]
    assert run.finished_at is not None


def test_original_budget_request_saves_with_explicit_authority(db):
    user, conversation = _thread(db)
    run, live = _execute(db, user, conversation, {"kind": "message", "text": "Let's set construction budget to 50k"})
    assert run.task_status == "succeeded"
    budgets = list(db.scalars(select(Budget).where(Budget.user_id == user.id)))
    assert len(budgets) == 1
    assert budgets[0].amount_minor == 5_000_000
    assert run.error_code is None
    _assert_terminal(db, run, live)


@pytest.mark.parametrize("text", [
    "How to setup custom budget",
    "I will set the goal but before that want to have a discussion",
])
def test_planning_discussion_accepts_operator_reply_without_opening_hitl(db, monkeypatch, agent_enabled, text):
    monkeypatch.setattr(conversation_service, "run_operator", lambda *_args, **_kwargs: OperatorResult(
        reply="We can discuss your priorities before choosing a target."
    ))
    user, conversation = _thread(db)
    run, live = _execute(db, user, conversation, {"kind": "message", "text": text})
    assert run.task_status == "succeeded"
    message = db.get(Message, run.final_message_id)
    assert message.content == "We can discuss your priorities before choosing a target."
    assert db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == run.id)) is None
    assert db.scalar(select(Budget)) is None
    assert db.scalar(select(Goal)) is None
    _assert_terminal(db, run, live)


def test_planning_view_does_not_execute_embedded_mutation_words(db):
    user, conversation = _thread(db)
    db.add(Budget(user_id=user.id, name="Monthly spending budget", amount_minor=5_000_000, currency="INR"))
    db.commit()
    run, live = _execute(db, user, conversation, {"kind": "message", "text": "Show my budget created yesterday"})
    assert run.task_status == "succeeded"
    assert db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == run.id)) is None
    assert db.scalar(select(Budget)).amount_minor == 5_000_000
    assert "active monthly budget" in db.get(Message, run.final_message_id).content
    _assert_terminal(db, run, live)


def test_incorrect_operator_planning_route_cannot_open_deferred_goal(db, monkeypatch, agent_enabled):
    capability = capability_for_primitive("planning.run@1")
    monkeypatch.setattr(conversation_service, "run_operator", lambda *_args, **_kwargs: _operator_proposal(
        capability.value, {}
    ))
    user, conversation = _thread(db)
    run, live = _execute(db, user, conversation, {
        "kind": "message", "text": "I will set the goal but before that want to have a discussion",
    })
    assert run.task_status == "failed"
    assert run.error_code == "planning_command_missing"
    assert db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == run.id)) is None
    assert db.scalar(select(Goal)) is None
    _assert_terminal(db, run, live)
