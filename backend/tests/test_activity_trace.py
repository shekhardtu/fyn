from sqlalchemy import select

from app.api import _attach_activity_trace
from app.models import Message, User
from app.seed import DEFAULT_USER_EMAIL
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
    reroute = next(step for step in trace.data["steps"] if step["id"] == "reroute")
    assert reroute["status"] == "failed"
    assert reroute["durationMs"] == 150.0
    persisted = db.get(Message, response.message_id)
    assert persisted.widgets[-1]["data"]["live"] is False
