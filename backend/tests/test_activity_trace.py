from sqlalchemy import select

from app.models import Message, User
from app.seed import DEFAULT_USER_EMAIL
from app.services.agui import _attach_activity_trace, _record_activity_event
from app.services.conversation import get_or_create_conversation, handle_chat


def test_terminal_trace_closes_any_stage_left_running(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Hi")
    activities = {
        "operator_repair": {
            "id": "operator_repair",
            "label": "Repairing the typed contract",
            "status": "running",
            "tool": "operator",
            "detail": None,
            "durationMs": 0.0,
            "cumulativeMs": 100.0,
        },
        "classification": {
            "id": "classification",
            "label": "Deterministic fallback selected",
            "status": "completed",
            "tool": "deterministic_fallback",
            "detail": "No valid route",
            "durationMs": 5.0,
            "cumulativeMs": 250.0,
        },
    }

    attached = _attach_activity_trace(db, response, activities)

    trace = attached.widgets[-1]
    assert trace.type == "agent_activity"
    assert trace.data["live"] is False
    assert trace.data["debugTrace"] is True
    repair = next(step for step in trace.data["steps"] if step["id"] == "operator_repair")
    assert repair["status"] == "failed"
    assert repair["durationMs"] == 150.0
    assert trace.data["summary"] == "This stage ended before producing a valid terminal result."
    persisted = db.get(Message, response.message_id)
    assert persisted.widgets[-1]["data"]["live"] is False


def test_failed_stage_overrides_earlier_model_reasoning_in_persisted_trace(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Hi")
    activities = {
        "classification": {
            "id": "classification",
            "label": "Analysis plan approved",
            "status": "completed",
            "detail": "Preserve the existing table presentation.",
            "durationMs": 10.0,
            "cumulativeMs": 10.0,
        },
        "tool_validation": {
            "id": "tool_validation",
            "label": "Generated tool was rejected",
            "status": "failed",
            "detail": "Analysis plan rejected: the plan did not include a governed financial-data query.",
            "durationMs": 5.0,
            "cumulativeMs": 15.0,
        },
    }

    attached = _attach_activity_trace(
        db,
        response,
        activities,
        "Apply the requested title and preserve the existing table.",
    )

    trace = attached.widgets[-1]
    expected = "Analysis plan rejected: the plan did not include a governed financial-data query."
    assert trace.data["summary"] == expected
    persisted = db.get(Message, response.message_id)
    assert persisted.widgets[-1]["data"]["summary"] == expected


def test_activity_trace_preserves_repeated_stages_and_both_tool_names():
    activities = {}
    open_activity_ids = {}
    occurrence_counts = {}

    _record_activity_event(activities, open_activity_ids, occurrence_counts, {
        "id": "operator",
        "label": "The Operator is reading the conversation",
        "status": "running",
        "tool": "operator",
        "input": {"text": "Show my transactions"},
        "durationMs": 0,
        "cumulativeMs": 1,
    })
    _record_activity_event(activities, open_activity_ids, occurrence_counts, {
        "id": "operator",
        "label": "The Operator selected a governed workflow",
        "status": "completed",
        "tool": "search_transactions",
        "output": {"workflow": "advanced_analysis"},
        "durationMs": 7000,
        "cumulativeMs": 7001,
    })
    _record_activity_event(activities, open_activity_ids, occurrence_counts, {
        "id": "handoff_compilation",
        "label": "Compiled the Operator handoff",
        "status": "completed",
        "tool": "search_transactions",
        "durationMs": 0,
        "cumulativeMs": 7002,
    })

    assert list(activities) == ["operator", "handoff_compilation"]
    assert activities["operator"]["stageId"] == "operator"
    assert activities["operator"]["tool"] == "operator"
    assert activities["operator"]["resultTool"] == "search_transactions"
    assert activities["operator"]["input"] == {"text": "Show my transactions"}
    assert activities["operator"]["output"] == {"workflow": "advanced_analysis"}
    assert activities["handoff_compilation"]["tool"] == "search_transactions"

    _record_activity_event(activities, open_activity_ids, occurrence_counts, {
        "id": "validator",
        "label": "Validating the typed contract",
        "status": "running",
        "tool": "validator",
        "durationMs": 0,
        # This nested stage reports time relative to its own start.
        "cumulativeMs": 0,
    })
    _record_activity_event(activities, open_activity_ids, occurrence_counts, {
        "id": "validator",
        "label": "Validated the typed contract",
        "status": "completed",
        "tool": "validator",
        "durationMs": 500,
        "cumulativeMs": 500,
    })

    assert activities["validator"]["cumulativeMs"] == 7502


def test_step_payloads_are_bounded_in_the_persisted_widget(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Hi")
    activities = {
        "model_pass_planner": {
            "id": "model_pass_planner",
            "label": "Analysis planning model pass",
            "status": "completed",
            "tool": "gpt-test",
            "detail": "planner",
            "durationMs": 5.0,
            "cumulativeMs": 10.0,
            "input": {"prompt": "x" * 20_000},
            "output": {"small": True},
        },
    }

    attached = _attach_activity_trace(db, response, activities)

    step = next(item for item in attached.widgets[-1].data["steps"] if item["id"] == "model_pass_planner")
    assert isinstance(step["input"], str)
    assert step["input"].endswith("full payload in agent_events]")
    assert len(step["input"]) < 6_200
    assert step["output"] == {"small": True}


def test_production_widgets_never_store_step_payloads(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "environment", "production")
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Hi")
    activities = {
        "execution": {
            "id": "execution",
            "label": "Running",
            "status": "completed",
            "tool": "conversation",
            "detail": None,
            "durationMs": 5.0,
            "cumulativeMs": 10.0,
            "input": {"secretish": "payload"},
            "output": {"large": "blob"},
        },
    }

    attached = _attach_activity_trace(db, response, activities)

    step = next(item for item in attached.widgets[-1].data["steps"] if item["id"] == "execution")
    assert "input" not in step
    assert "output" not in step
    assert attached.widgets[-1].data["debugTrace"] is False
