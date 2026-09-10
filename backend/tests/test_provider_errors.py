from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from agno.agent import Agent
from agno.metrics import RunMetrics
from agno.models.message import Message
from agno.run.agent import RunErrorEvent, RunOutput
from agno.run.base import RunStatus
from openai import AsyncOpenAI, OpenAI, RateLimitError

from app.services import provider_errors
from app.services.provider_errors import ProviderUnavailableError, checked_provider_stream
from app.services.agent_run_metrics import agent_metric_snapshot, begin_agent_metric_collection, end_agent_metric_collection, record_agno_run_metrics


@pytest.mark.parametrize(("code", "expected"), [
    ("credit_balance_exhausted", "provider_quota_exhausted"),
    ("project_spend_limit_exceeded", "provider_quota_exhausted"),
    ("organization_usage_limit_exceeded", "provider_quota_exhausted"),
    ("rate_limit_exceeded", "provider_rate_limited"),
    ("invalid_api_key", "provider_authentication_failed"),
    ("context_length_exceeded", "provider_context_too_large"),
])
def test_structured_provider_codes_are_classified_without_exposing_diagnostics(code, expected):
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    error = RateLimitError("private upstream diagnostic", response=response, body={"code": code})
    failure = ProviderUnavailableError.from_error(error)
    assert failure.code == expected
    assert "private upstream" not in str(failure)
    assert failure.retryable is (expected == "provider_rate_limited")


def test_framework_guardrail_failure_is_not_a_provider_outage():
    with pytest.raises(provider_errors.AgentExecutionError) as caught:
        list(checked_provider_stream(lambda: iter([
            RunErrorEvent(error_type="input_check_error", content="private rejected input"),
        ])))
    assert caught.value.code == "agent_validation_failed"
    assert "private rejected input" not in str(caught.value)


def test_failed_structured_output_is_checked_before_pydantic_validation():
    output = RunOutput(status=RunStatus.error, content="You have no credits remaining")
    with pytest.raises(ProviderUnavailableError) as caught:
        provider_errors.checked_provider_call(lambda: output)
    assert caught.value.code == "provider_quota_exhausted"


@pytest.mark.parametrize("event_type", ["response.failed", "response.incomplete", "error"])
def test_native_failure_events_cannot_disappear_in_agno(event_type):
    model = provider_errors.CheckedOpenAIResponses(id="test-model", api_key="test-only")
    event = SimpleNamespace(
        type=event_type,
        code="server_error",
        message="private upstream diagnostic",
        response=SimpleNamespace(
            status="failed" if event_type == "response.failed" else "incomplete",
            error=SimpleNamespace(code="server_error", message="private upstream diagnostic"),
            usage=None,
        ),
    )
    with pytest.raises(ProviderUnavailableError) as caught:
        model._parse_provider_response_delta(event, Message(role="assistant"), {})
    assert "private upstream" not in str(caught.value)
    if event_type == "response.incomplete":
        assert caught.value.code == "provider_response_incomplete"


def test_native_nonstream_incomplete_response_is_not_a_success():
    model = provider_errors.CheckedOpenAIResponses(id="test-model", api_key="test-only")
    with pytest.raises(ProviderUnavailableError) as caught:
        model._parse_provider_response(SimpleNamespace(status="incomplete", error=None))
    assert caught.value.code == "provider_response_incomplete"


@pytest.mark.parametrize("status", [RunStatus.running, RunStatus.pending, RunStatus.paused, RunStatus.cancelled])
def test_only_completed_agno_outputs_are_accepted_as_terminal_success(status):
    with pytest.raises(provider_errors.AgentExecutionError) as caught:
        provider_errors.checked_provider_call(lambda: RunOutput(status=status, content="Partial answer"))
    assert caught.value.code == "agent_response_incomplete"


@pytest.mark.parametrize("stream", [False, True])
def test_real_sdk_and_agno_preserve_quota_identity_without_retrying(stream):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(429, json={"error": {
            "type": "insufficient_quota", "code": "credit_balance_exhausted",
            "message": "private diagnostic, not a quota keyword",
        }})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        model = provider_errors.CheckedOpenAIResponses(
            id="test-model", api_key="test-only", max_retries=0,
            client=OpenAI(api_key="test-only", max_retries=0, http_client=http_client),
        )
        agent = Agent(model=model, telemetry=False)
        with pytest.raises(ProviderUnavailableError) as caught:
            if stream:
                list(checked_provider_stream(lambda: agent.run("Hi", stream=True, stream_events=True, yield_run_output=True)))
            else:
                provider_errors.checked_provider_call(lambda: agent.run("Hi"))
    assert caught.value.code == "provider_quota_exhausted"
    assert "private diagnostic" not in str(caught.value)
    assert len(requests) == 1


def test_stream_without_provider_terminal_event_is_incomplete():
    def respond(_request):
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=(
            'data: {"type":"response.output_text.delta","item_id":"item-1",'
            '"output_index":0,"content_index":0,"sequence_number":1,"delta":"Hello"}\n\n'
        ))

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        model = provider_errors.CheckedOpenAIResponses(
            id="test-model", api_key="test-only", max_retries=0,
            client=OpenAI(api_key="test-only", max_retries=0, http_client=http_client),
        )
        with pytest.raises(ProviderUnavailableError) as caught:
            list(model.invoke_stream([Message(role="user", content="Hi")], Message(role="assistant")))
    assert caught.value.code == "provider_response_incomplete"


def test_failed_output_retains_any_usage_that_agno_returned():
    token = begin_agent_metric_collection()
    try:
        output = RunOutput(
            status=RunStatus.error,
            content="You have no credits remaining",
            metrics=RunMetrics(input_tokens=123, output_tokens=7, total_tokens=130),
        )
        with pytest.raises(ProviderUnavailableError):
            provider_errors.checked_provider_call(
                lambda: output,
                on_output=lambda result: record_agno_run_metrics(result, stage="suggester", model="test-model"),
            )
        snapshot = agent_metric_snapshot()
        assert snapshot["inputTokens"] == 123
        assert snapshot["outputTokens"] == 7
    finally:
        end_agent_metric_collection(token)


def _response(status):
    return {
        "id": "resp_test", "object": "response", "created_at": 1,
        "status": status, "error": None, "model": "test-model", "output": [],
        "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
    }


def test_completed_stream_is_accepted_and_completion_does_not_leak_to_next_request():
    calls = []

    def respond(_request):
        calls.append(True)
        content = ""
        if len(calls) == 1:
            event = {"type": "response.completed", "sequence_number": 1, "response": _response("completed")}
            content = f"data: {json.dumps(event)}\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=content)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        model = provider_errors.CheckedOpenAIResponses(
            id="test-model", api_key="test-only", max_retries=0,
            client=OpenAI(api_key="test-only", max_retries=0, http_client=http_client),
        )
        output = list(model.invoke_stream([Message(role="user", content="Hi")], Message(role="assistant")))
        assert output[-1].response_usage.total_tokens == 6
        with pytest.raises(ProviderUnavailableError) as caught:
            list(model.invoke_stream([Message(role="user", content="Hi")], Message(role="assistant")))
        assert caught.value.code == "provider_response_incomplete"


@pytest.mark.parametrize("stream", [False, True])
def test_async_provider_boundary_preserves_authentication_failure(stream):
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(401, json={"error": {
            "code": "invalid_api_key", "message": "private diagnostic", "type": "invalid_request_error",
        }}))) as http_client:
            model = provider_errors.CheckedOpenAIResponses(
                id="test-model", api_key="test-only", max_retries=0,
                async_client=AsyncOpenAI(api_key="test-only", max_retries=0, http_client=http_client),
            )
            with pytest.raises(ProviderUnavailableError) as caught:
                if stream:
                    async for _event in model.ainvoke_stream([Message(role="user", content="Hi")], Message(role="assistant")):
                        pass
                else:
                    await model.ainvoke([Message(role="user", content="Hi")], Message(role="assistant"))
            assert caught.value.code == "provider_authentication_failed"
            assert "private diagnostic" not in str(caught.value)
    asyncio.run(check())
