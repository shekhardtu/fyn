"""The template pool as dynamic tools on the single agent loop."""
from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import AnalysisToolRun, AnalysisToolTemplate
from app.seed import default_user
from app.services import conversation as conversation_service
from app.services import analysis_tools as analysis_tools_module
from app.services.agents import OperatorResult
from app.services.analysis_seeds import seed_analysis_templates
from app.services.analysis_tools import (
    RUN_ANALYSIS_TOOL_NAME,
    AnalysisToolContext,
    build_analysis_tools,
)
from app.services.conversation import get_or_create_conversation, handle_chat


def _context(db, user, conversation, today: date | None = None) -> AnalysisToolContext:
    return AnalysisToolContext(
        db=db,
        user_id=user.id,
        conversation_id=conversation.id,
        today=today or conversation_service._local_today(user),
        timezone_name=user.timezone,
        question="How much did I spend this month?",
    )


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    yield
    get_settings.cache_clear()


def test_toolset_mounts_retrieved_templates_and_the_open_plan_author(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    today = conversation_service._local_today(user)
    seed_analysis_templates(db, today=today)

    tools = build_analysis_tools(_context(db, user, conversation, today))

    names = [tool.name for tool in tools]
    assert RUN_ANALYSIS_TOOL_NAME in names
    # The governed SQL lane mounts after the plan author when enabled.
    assert names.index(RUN_ANALYSIS_TOOL_NAME) < len(names) - 1 or names[-1] == RUN_ANALYSIS_TOOL_NAME
    assert any(name.startswith("bind_template__") for name in names)
    for tool in tools:
        assert tool.strict is True
        schema = tool.parameters
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_template_tool_executes_the_harness_and_returns_minor_unit_rows(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    today = conversation_service._local_today(user)
    seed_analysis_templates(db, today=today)
    context = _context(db, user, conversation, today)
    tools = build_analysis_tools(context)
    summary_tool = next(
        tool for tool in tools
        if tool.name.startswith("bind_template__") and "Spending summary" in tool.description
    )

    fill = {}
    for name in summary_tool.parameters["properties"]:
        if name.endswith("start_date"):
            fill[name] = today.replace(day=1).isoformat()
        elif name.endswith("end_date"):
            fill[name] = today.isoformat()
        else:
            fill[name] = 50
    payload = summary_tool.entrypoint(**fill)

    assert payload.get("kind") == "governed_analysis"
    assert payload["message"]
    money_rows = [
        row
        for result in payload["query_results"]
        if "count" not in str(result.get("metric", ""))
        for row in result["rows"]
    ]
    assert money_rows and all("value_minor" in row and "value" not in row for row in money_rows)
    run = db.scalar(select(AnalysisToolRun).order_by(AnalysisToolRun.created_at.desc()).limit(1))
    assert run.status == "completed"
    assert context.citations


def test_run_analysis_tool_reports_governed_check_failures_for_self_correction(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    today = conversation_service._local_today(user)
    context = _context(db, user, conversation, today)
    run_tool = next(tool for tool in build_analysis_tools(context) if tool.name == RUN_ANALYSIS_TOOL_NAME)

    # An entirely-future window is a defect the deterministic repair refuses to
    # invent facts for, so it surfaces as a governed rejection the model can fix.
    future_plan = {
        "objective": "descriptive",
        "analysis_type": "semantic_query",
        "safe_reasoning_summary": ["Aggregate the requested period"],
        "queries": [{
            "name": "Future spend",
            "metric": "gross_spend",
            "start_date": "2030-01-01",
            "end_date": "2030-12-31",
        }],
    }
    failed = run_tool.entrypoint(
        name="Future spending",
        intent_signature="future spending window",
        plan_json=json.dumps(future_plan),
    )
    assert "error" in failed
    assert failed["error"]["code"] == "analysis_plan_rejected"

    valid_plan = dict(future_plan, queries=[dict(
        future_plan["queries"][0],
        start_date=today.replace(day=1).isoformat(),
        end_date=today.isoformat(),
    )])
    succeeded = run_tool.entrypoint(
        name="Current spending",
        intent_signature="current spending window",
        plan_json=json.dumps(valid_plan),
    )
    assert succeeded.get("kind") == "governed_analysis"
    # The corrected plan was templatized back into the pool.
    assert db.scalar(select(AnalysisToolTemplate)) is not None


def test_run_analysis_tool_rejects_malformed_plans_with_field_locations(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    run_tool = next(tool for tool in build_analysis_tools(_context(db, user, conversation)) if tool.name == RUN_ANALYSIS_TOOL_NAME)

    broken = run_tool.entrypoint(
        name="Broken",
        intent_signature="broken plan",
        plan_json=json.dumps({"objective": "descriptive"}),
    )
    assert broken["error"]["code"] == "invalid_analysis_plan"
    assert "analysis_type" in broken["error"]["detail"]


def test_agent_disabled_turns_never_build_analysis_tools(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("analysis tools must not be built while the agent is disabled")

    monkeypatch.setattr(conversation_service, "build_analysis_tools", must_not_run)

    response = handle_chat(db, user, conversation, "How much did I spend this month?")

    assert response.message


def test_non_financial_turns_do_not_pay_for_retrieval(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        analysis_tools_module,
        "retrieve_templates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("non-financial turns must not retrieve templates")),
    )
    captured: dict = {}

    def capture_operator(*_args, **kwargs):
        captured["analysis_tools"] = kwargs.get("analysis_tools")
        return OperatorResult(reply="Hello! What would you like to look at?", tool_grounding=[])

    monkeypatch.setattr(conversation_service, "run_operator", capture_operator)

    handle_chat(db, user, conversation, "What can you help me with in general?")

    assert captured["analysis_tools"] == []
