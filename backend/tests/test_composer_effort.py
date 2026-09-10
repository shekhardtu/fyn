from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import pytest
from ag_ui.core import RunAgentInput

from app.config import get_settings
from app.services.agents import build_operator
from app.services.agui import InvalidAgentInput, normalize_run_input
from app.services.composer_effort import composer_effort, reasoning_effort


def run_input(effort):
    return RunAgentInput.model_validate({
        "threadId": str(uuid4()), "runId": str(uuid4()), "state": {}, "tools": [], "context": [],
        "messages": [{"id": "new", "role": "user", "content": "Compare my spending"}],
        "forwardedProps": {"fynEffort": effort},
    })


@pytest.mark.parametrize("effort", ["quick", "thorough"])
def test_effort_is_persisted_without_modifying_message(effort):
    payload, message_id = normalize_run_input(run_input(effort))
    assert payload == {"kind": "message", "text": "Compare my spending", "messageId": "new", "effort": effort}
    assert message_id == "new"


@pytest.mark.parametrize("effort", ["max", None, {}, 1])
def test_unknown_effort_is_rejected(effort):
    with pytest.raises(InvalidAgentInput):
        normalize_run_input(run_input(effort))


def test_effort_resets_after_failure_and_does_not_cross_threads():
    with pytest.raises(RuntimeError), composer_effort("thorough"):
        assert reasoning_effort("medium") == "high"
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(reasoning_effort, "medium").result() == "medium"
        raise RuntimeError("provider failed")
    assert reasoning_effort("medium") == "medium"


@pytest.mark.parametrize(("effort", "expected"), [("quick", "low"), ("thorough", "high")])
def test_selection_reaches_operator_model(monkeypatch, effort, expected):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with composer_effort(effort):
            operator = build_operator([], date(2026, 9, 9), "Asia/Kolkata")
        assert operator.model.reasoning_effort == expected
    finally:
        get_settings.cache_clear()
