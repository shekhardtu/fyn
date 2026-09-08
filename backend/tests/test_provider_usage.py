from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from agno.models.message import Message
from openai import AsyncOpenAI, OpenAI

from app.services.agent_run_metrics import agent_metric_snapshot, begin_agent_metric_collection, end_agent_metric_collection
from app.services.provider_errors import CheckedOpenAIResponses, ProviderUnavailableError


def response(status="completed", usage=True):
    return {
        "id": "resp_usage", "object": "response", "created_at": 1,
        "status": status, "error": None, "model": "test-model", "output": [],
        "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "usage": {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60,
                  "input_tokens_details": {"cached_tokens": 20},
                  "output_tokens_details": {"reasoning_tokens": 3}} if usage else None,
    }


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status", ["completed", "failed", "incomplete"])
def test_native_usage_survives_agno_rejection_and_later_request_failure(stream, status):
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 2:
            return httpx.Response(429, json={"error": {"code": "insufficient_quota", "message": "private"}},
                                  headers={"x-request-id": "req_failed"})
        if stream:
            event = {"type": f"response.{status}", "sequence_number": 1, "response": response(status)}
            return httpx.Response(200, headers={"Content-Type": "text/event-stream", "x-request-id": "req_ok"},
                                  content=f"data: {json.dumps(event)}\n\n")
        return httpx.Response(200, json=response(status), headers={"x-request-id": "req_ok"})

    token = begin_agent_metric_collection()
    try:
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            model = CheckedOpenAIResponses(id="test-model", max_retries=0,
                client=OpenAI(api_key="test-only", max_retries=0, http_client=client))
            for _ in range(2):
                try:
                    if stream:
                        list(model.invoke_stream([Message(role="user", content="secret prompt")], Message(role="assistant")))
                    else:
                        model.invoke([Message(role="user", content="secret prompt")], Message(role="assistant"))
                except ProviderUnavailableError:
                    pass
        usage = agent_metric_snapshot()["requestUsage"]
        assert len(calls) == 2
        assert usage["requestCount"] == 2
        assert usage["reportedRequests"] == 1
        assert usage["coverage"] == "partial"
        assert usage["totalTokens"] == 60
        assert usage["inputTokens"] == 50
        assert usage["outputTokens"] == 10
        first, second = usage["requests"]
        assert first["status"] == status
        assert first["responseId"] == "resp_usage"
        assert first["requestId"] == "req_ok"
        assert first["cacheReadTokens"] == 20
        assert first["reasoningTokens"] == 3
        assert second["totalTokens"] is None
        assert second["requestId"] == "req_failed"
        assert "secret prompt" not in str(usage)
        assert "private" not in str(usage)
    finally:
        end_agent_metric_collection(token)


def test_zero_requests_is_distinct_from_response_without_usage():
    token = begin_agent_metric_collection()
    try:
        assert agent_metric_snapshot()["requestUsage"]["coverage"] == "not_used"
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response(usage=False)))) as client:
            model = CheckedOpenAIResponses(id="test-model", max_retries=0,
                client=OpenAI(api_key="test-only", max_retries=0, http_client=client))
            model.invoke([Message(role="user", content="Hi")], Message(role="assistant"))
        usage = agent_metric_snapshot()["requestUsage"]
        assert usage["coverage"] == "unavailable"
        assert usage["totalTokens"] is None
    finally:
        end_agent_metric_collection(token)


@pytest.mark.parametrize("stream", [False, True])
def test_async_native_usage_is_recorded_once(stream):
    async def check():
        def respond(_):
            if stream:
                event = {"type": "response.completed", "sequence_number": 1, "response": response()}
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                      content=f"data: {json.dumps(event)}\n\n")
            return httpx.Response(200, json=response())

        token = begin_agent_metric_collection()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                model = CheckedOpenAIResponses(id="test-model", max_retries=0,
                    async_client=AsyncOpenAI(api_key="test-only", max_retries=0, http_client=client))
                if stream:
                    async for _ in model.ainvoke_stream([Message(role="user", content="Hi")], Message(role="assistant")):
                        pass
                else:
                    await model.ainvoke([Message(role="user", content="Hi")], Message(role="assistant"))
            usage = agent_metric_snapshot()["requestUsage"]
            assert usage["coverage"] == "complete"
            assert usage["requestCount"] == 1
            assert usage["totalTokens"] == 60
        finally:
            end_agent_metric_collection(token)
    asyncio.run(check())


def test_nested_stages_and_replayed_snapshots_do_not_double_count():
    from app.services.agent_run_metrics import ProviderUsageAttempt, merge_agent_metric_snapshots, provider_usage_stage
    from app.services.provider_errors import checked_provider_call

    token = begin_agent_metric_collection()
    try:
        with provider_usage_stage("operator_response"):
            first = ProviderUsageAttempt("outer")
            first.observe(response())
            checked_provider_call(lambda: ProviderUsageAttempt("delegate").observe(response()), stage="analysis_delegate")
            third = ProviderUsageAttempt("outer")
            third.observe(response())
        snapshot = agent_metric_snapshot()
        merged = merge_agent_metric_snapshots(snapshot, snapshot)
        usage = merged["requestUsage"]
        assert usage["requestCount"] == 3
        assert usage["totalTokens"] == 180
        assert [item["stage"] for item in usage["requests"]] == ["operator_response", "analysis_delegate", "operator_response"]
        assert usage["coverage"] == "complete"
    finally:
        end_agent_metric_collection(token)


@pytest.mark.parametrize("quota", [False, True])
def test_embedding_retries_are_visible_and_quota_is_not_retried(monkeypatch, quota):
    from types import SimpleNamespace
    from app.services import template_retrieval

    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429 if quota else 503,
                json={"error": {"code": "insufficient_quota" if quota else "server_error", "message": "private"}})
        return httpx.Response(200, json={"object": "list", "model": "embedding-model", "data": [
            {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
        ],
            "usage": {"prompt_tokens": 25, "total_tokens": 25}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(template_retrieval, "OpenAI", lambda **kwargs: OpenAI(http_client=client, **kwargs))
        monkeypatch.setattr(template_retrieval.time, "sleep", lambda _: None)
        token = begin_agent_metric_collection()
        try:
            try:
                template_retrieval._create_embeddings(["secret"], SimpleNamespace(openai_api_key="test-only", embedding_model="embedding-model"))
            except Exception:
                assert quota
            usage = agent_metric_snapshot()["requestUsage"]
            assert len(calls) == (1 if quota else 2)
            assert usage["requestCount"] == len(calls)
            assert usage["coverage"] == ("unavailable" if quota else "partial")
            assert usage["inputTokens"] == (None if quota else 25)
            assert usage["outputTokens"] == (None if quota else 0)
            assert all(item["operation"] == "embeddings" for item in usage["requests"])
            assert all(item["stage"] == "template_embedding" for item in usage["requests"])
        finally:
            end_agent_metric_collection(token)


def test_truncated_stream_retains_response_identity_and_unknown_usage():
    created = {"type": "response.created", "sequence_number": 1, "response": response("in_progress", usage=False)}
    token = begin_agent_metric_collection()
    try:
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200,
            headers={"Content-Type": "text/event-stream"}, content=f"data: {json.dumps(created)}\n\n"))) as client:
            model = CheckedOpenAIResponses(id="test-model", max_retries=0,
                client=OpenAI(api_key="test-only", max_retries=0, http_client=client))
            with pytest.raises(ProviderUnavailableError):
                list(model.invoke_stream([Message(role="user", content="Hi")], Message(role="assistant")))
        usage = agent_metric_snapshot()["requestUsage"]
        assert usage["coverage"] == "unavailable"
        assert usage["requests"][0]["responseId"] == "resp_usage"
        assert usage["requests"][0]["status"] == "incomplete"
        assert usage["totalTokens"] is None
    finally:
        end_agent_metric_collection(token)


def test_snapshot_merge_updates_an_attempt_without_preserving_stale_missing_usage():
    from app.services.agent_run_metrics import ProviderUsageAttempt, merge_agent_metric_snapshots, interrupt_request_usage

    token = begin_agent_metric_collection()
    try:
        attempt = ProviderUsageAttempt("test-model")
        pending = agent_metric_snapshot()
        attempt.observe(response())
        complete = agent_metric_snapshot()
        merged = merge_agent_metric_snapshots(pending, complete)["requestUsage"]
        assert merged["requestCount"] == 1
        assert merged["totalTokens"] == 60
        assert merged["coverage"] == "complete"
        interrupted = merge_agent_metric_snapshots(interrupt_request_usage(pending), complete)["requestUsage"]
        assert interrupted["coverage"] == "interrupted"
        assert interrupted["totalTokens"] == 60
        assert interrupted["historyComplete"] is False
    finally:
        end_agent_metric_collection(token)


def test_shared_sdk_client_cannot_hide_automatic_retries():
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503, json={"error": {"code": "server_error", "message": "private"}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        # This injected client uses SDK defaults; the model boundary owns the
        # no-hidden-retry policy without mutating that shared client's settings.
        sdk = OpenAI(api_key="test-only", http_client=client)
        model = CheckedOpenAIResponses(id="test-model", client=sdk)
        with pytest.raises(ProviderUnavailableError):
            model.invoke([Message(role="user", content="Hi")], Message(role="assistant"))
        assert len(calls) == 1
        assert sdk.max_retries == 2
