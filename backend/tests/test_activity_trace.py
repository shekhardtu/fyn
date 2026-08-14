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
        "reroute": {
            "id": "reroute",
            "label": "Rerouting with stronger model",
            "status": "running",
            "tool": "agno_reroute",
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
    reroute = next(step for step in trace.data["steps"] if step["id"] == "reroute")
    assert reroute["status"] == "failed"
    assert reroute["durationMs"] == 150.0
    persisted = db.get(Message, response.message_id)
    assert persisted.widgets[-1]["data"]["live"] is False


def test_activity_trace_preserves_repeated_stages_and_both_tool_names():
    activities = {}
    open_activity_ids = {}
    occurrence_counts = {}

    _record_activity_event(activities, open_activity_ids, occurrence_counts, {
        "id": "classification",
        "label": "The unified agent is reading the conversation",
        "status": "running",
        "tool": "unified_read_agent",
        "durationMs": 0,
        "cumulativeMs": 1,
    })
    _record_activity_event(activities, open_activity_ids, occurrence_counts, {
        "id": "classification",
        "label": "The unified agent selected a governed workflow",
        "status": "completed",
        "tool": "search_transactions",
        "durationMs": 7000,
        "cumulativeMs": 7001,
    })
    _record_activity_event(activities, open_activity_ids, occurrence_counts, {
        "id": "classification",
        "label": "Compiled the unified typed handoff",
        "status": "completed",
        "tool": "search_transactions",
        "durationMs": 0,
        "cumulativeMs": 7002,
    })

    assert list(activities) == ["classification", "classification-2"]
    assert activities["classification"]["stageId"] == "classification"
    assert activities["classification"]["tool"] == "unified_read_agent"
    assert activities["classification"]["resultTool"] == "search_transactions"
    assert activities["classification-2"]["tool"] == "search_transactions"

    _record_activity_event(activities, open_activity_ids, occurrence_counts, {
        "id": "response_synthesis",
        "label": "Writing the contextual final answer",
        "status": "running",
        "tool": "gpt-5.6-luna",
        "durationMs": 0,
        # This nested stage reports time relative to its own start.
        "cumulativeMs": 0,
    })
    _record_activity_event(activities, open_activity_ids, occurrence_counts, {
        "id": "response_synthesis",
        "label": "Wrote the contextual final answer",
        "status": "completed",
        "tool": "gpt-5.6-luna",
        "durationMs": 500,
        "cumulativeMs": 500,
    })

    assert activities["response_synthesis"]["cumulativeMs"] == 7502
