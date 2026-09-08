from __future__ import annotations

import json

import pytest
from agno.run.agent import RunContentEvent, RunErrorEvent, RunOutput
from agno.run.base import RunStatus
from sqlalchemy import select

from app.config import get_settings
from app.models import AgentEvent, Message, Transaction, User
from app.seed import DEFAULT_USER_EMAIL
from app.services import agent_enrichment, agents, conversation as conversation_service
from app.services.conversation import get_or_create_conversation
from app.services.provider_errors import ProviderUnavailableError
from test_agui_runtime import _execute, _process_one_enrichment


@pytest.fixture
def provider_stream(monkeypatch):
    events = [RunErrorEvent(error_type="model_provider_error", content="You have no credits remaining")]

    class StubOperator:
        def run(self, *_args, **_kwargs):
            return iter(events)

    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setattr(agents, "build_operator", lambda *_args, **_kwargs: StubOperator())
    get_settings.cache_clear()
    yield events
    get_settings.cache_clear()


def _thread(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    return user, get_or_create_conversation(db, user)


def _assert_terminal(db, run, live, event_type):
    stored = list(db.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.sequence)))
    assert [item.sequence for item in stored] == list(range(1, len(stored) + 1))
    assert [(item.sequence, item.payload) for item in stored] == live
    assert [item.event_type for item in stored if item.event_type in {"RUN_FINISHED", "RUN_ERROR"}] == [event_type]
    assert run.finished_at is not None


def test_quota_failure_has_durable_notice_and_correct_task_cause(db, provider_stream):
    user, conversation = _thread(db)
    run, live = _execute(db, user, conversation, {"kind": "message", "text": "700 spend on food yesterday"})
    assert run.task_status == "failed"
    assert run.failure_stage == "provider"
    assert run.error_code == "provider_quota_exhausted"
    message = db.get(Message, run.final_message_id)
    notices = [item for item in message.widgets if item["type"] == "insight_card"]
    assert len(notices) == 1
    assert notices[0]["data"]["title"] == "AI service unavailable"
    assert notices[0]["data"]["tone"] == "caution"
    assert "transaction forms" in notices[0]["data"]["body"]
    assert "unavailable" in message.content
    custom = next(event for _, event in live if event["type"] == "CUSTOM")
    assert notices[0] in custom["value"]["response"]["widgets"]
    assert db.scalar(select(Transaction)) is None
    _assert_terminal(db, run, live, "RUN_FINISHED")


def test_partial_stream_failure_is_terminal_without_replacement_or_diagnostics(db, provider_stream):
    provider_stream[:] = [
        RunContentEvent(content="Hello"),
        RunErrorEvent(error_type="provider_authentication_failed", content="private upstream credential detail"),
    ]
    user, conversation = _thread(db)
    run, live = _execute(db, user, conversation, {"kind": "message", "text": "Hi"})
    assert run.status == "failed"
    assert run.error_code == "provider_authentication_failed"
    assert [event["delta"] for _, event in live if event["type"] == "TEXT_MESSAGE_CONTENT"] == ["Hello"]
    assert not any(event["type"] == "CUSTOM" for _, event in live)
    assert "private upstream" not in json.dumps(live)
    _assert_terminal(db, run, live, "RUN_ERROR")


def test_guardrail_failure_never_enters_financial_fallback(db, provider_stream):
    provider_stream[:] = [RunErrorEvent(error_type="input_check_error", content="private guardrail detail")]
    user, conversation = _thread(db)
    run, live = _execute(db, user, conversation, {"kind": "message", "text": "700 spend on food yesterday"})
    assert run.failure_stage == "agent_validation"
    assert run.error_code == "agent_validation_failed"
    assert "AI service unavailable" not in json.dumps(live)
    assert "private guardrail" not in json.dumps(live)
    assert db.scalar(select(Transaction)) is None
    _assert_terminal(db, run, live, "RUN_ERROR")


def test_provider_notice_does_not_leak_into_next_healthy_turn(db, provider_stream):
    user, conversation = _thread(db)
    _execute(db, user, conversation, {"kind": "message", "text": "700 spend on food yesterday"})
    provider_stream[:] = [RunOutput(status=RunStatus.completed, content="Hello!")]
    run, live = _execute(db, user, conversation, {"kind": "message", "text": "Hi"})
    assert run.task_status == "succeeded"
    message = db.get(Message, run.final_message_id)
    assert message.content == "Hello!"
    assert all(item.get("data", {}).get("title") != "AI service unavailable" for item in message.widgets)
    _assert_terminal(db, run, live, "RUN_FINISHED")


def test_service_notice_does_not_relabel_a_successful_financial_result(db):
    _user, conversation = _thread(db)
    with conversation_service._reply_reservation(db):
        conversation_service._provider_failure.set(ProviderUnavailableError("provider_quota_exhausted"))
        response = conversation_service.persist_agent_response(db, conversation, "The transaction was saved.")
    assert response.task_status == "succeeded"
    assert response.message == "The transaction was saved."
    assert response.widgets[0].data["title"] == "AI service unavailable"


def test_service_notice_does_not_mask_an_independent_domain_failure(db):
    _user, conversation = _thread(db)
    with conversation_service._reply_reservation(db):
        conversation_service._provider_failure.set(ProviderUnavailableError("provider_quota_exhausted"))
        response = conversation_service.persist_agent_response(
            db, conversation, "The requested date range is invalid.", task_status="failed",
            failure_stage="analysis", error_code="invalid_date_range",
        )
    assert response.error_code == "invalid_date_range"
    assert response.failure_stage == "analysis"
    assert response.message == "The requested date range is invalid."


@pytest.mark.parametrize(("code", "expected_status"), [
    ("provider_quota_exhausted", "failed"),
    ("provider_authentication_failed", "failed"),
    ("provider_rate_limited", "pending"),
])
def test_enrichment_only_retries_transient_provider_failures(db, monkeypatch, code, expected_status):
    def unavailable(*_args, **_kwargs):
        raise ProviderUnavailableError(code)

    monkeypatch.setattr(agent_enrichment, "suggest_related_questions", unavailable)
    user, conversation = _thread(db)
    run, _live = _execute(db, user, conversation, {"kind": "message", "text": "Spent ₹300 on coffee today"})
    message = db.get(Message, run.final_message_id)
    original_answer = message.content
    item = _process_one_enrichment(db, max_attempts=2)
    assert item.status == expected_status
    assert item.error_code == code
    assert item.attempts == 1
    assert run.task_status == "succeeded"
    assert db.get(Message, message.id).content == original_answer
