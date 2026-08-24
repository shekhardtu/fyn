from __future__ import annotations

from types import SimpleNamespace

from agno.metrics import RunMetrics
from agno.run.agent import RunOutput

from app.services.agent_run_metrics import (
    agent_metric_snapshot,
    begin_agent_metric_collection,
    end_agent_metric_collection,
    record_agno_run_metrics,
)


def _output(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration: float,
    first_token: float,
    cost: float | None,
) -> RunOutput:
    return RunOutput(
        model=model,
        model_provider="OpenAI",
        metrics=RunMetrics(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cache_read_tokens=2,
            reasoning_tokens=1,
            duration=duration,
            time_to_first_token=first_token,
            cost=cost,
        ),
    )


def test_agno_metrics_are_aggregated_across_every_model_pass():
    token = begin_agent_metric_collection()
    try:
        record_agno_run_metrics(
            _output(
                model="operator-model",
                input_tokens=100,
                output_tokens=20,
                duration=1.2,
                first_token=0.3,
                cost=0.004,
            ),
            stage="operator_decision",
            model="fallback-operator",
        )
        record_agno_run_metrics(
            _output(
                model="validator-model",
                input_tokens=60,
                output_tokens=10,
                duration=0.8,
                first_token=0.2,
                cost=0.002,
            ),
            stage="validator",
            model="fallback-validator",
        )

        snapshot = agent_metric_snapshot()
    finally:
        end_agent_metric_collection(token)

    assert snapshot["modelPasses"] == 2
    assert snapshot["inputTokens"] == 160
    assert snapshot["outputTokens"] == 30
    assert snapshot["totalTokens"] == 190
    assert snapshot["cacheReadTokens"] == 4
    assert snapshot["reasoningTokens"] == 2
    assert snapshot["modelDurationMs"] == 2000
    assert snapshot["firstModelTimeToFirstTokenMs"] == 300
    assert snapshot["costUsd"] == 0.006
    assert snapshot["costCoverage"] == 1
    assert [item["stage"] for item in snapshot["passes"]] == ["operator_decision", "validator"]


def test_exact_cost_is_null_when_any_provider_pass_omits_cost():
    token = begin_agent_metric_collection()
    try:
        record_agno_run_metrics(
            _output(
                model="operator-model",
                input_tokens=10,
                output_tokens=5,
                duration=1,
                first_token=0.1,
                cost=None,
            ),
            stage="operator_decision",
            model="operator-model",
        )
        snapshot = agent_metric_snapshot()
    finally:
        end_agent_metric_collection(token)

    assert snapshot["costUsd"] is None
    assert snapshot["costCoverage"] == 0
    assert snapshot["totalTokens"] == 15


def test_metrics_are_not_collected_outside_a_durable_run():
    record_agno_run_metrics(
        _output(
            model="operator-model",
            input_tokens=10,
            output_tokens=5,
            duration=1,
            first_token=0.1,
            cost=0.001,
        ),
        stage="operator_decision",
        model="operator-model",
    )

    assert agent_metric_snapshot()["modelPasses"] == 0


def test_content_free_prompt_capability_and_tool_timings_are_retained():
    output = _output(
        model="operator-model",
        input_tokens=10,
        output_tokens=5,
        duration=1,
        first_token=0.1,
        cost=0.001,
    )
    output.tools = [SimpleNamespace(
        tool_name="transaction_summary",
        tool_call_error=False,
        metrics=SimpleNamespace(duration=0.125),
    )]
    token = begin_agent_metric_collection()
    try:
        record_agno_run_metrics(
            output,
            stage="operator_response",
            model="operator-model",
            reasoning_profile="low",
            prompt_characters=840,
            prompt_components={"currentMessage": 12, "recentContext": 400},
            mounted_tools=["transaction_summary", "transaction_list"],
        )
        snapshot = agent_metric_snapshot()
    finally:
        end_agent_metric_collection(token)

    metric_pass = snapshot["passes"][0]
    assert metric_pass["reasoningProfile"] == "low"
    assert metric_pass["promptCharacters"] == 840
    assert metric_pass["promptComponents"] == {"currentMessage": 12, "recentContext": 400}
    assert metric_pass["mountedToolCount"] == 2
    assert metric_pass["mountedTools"] == ["transaction_summary", "transaction_list"]
    assert metric_pass["toolCalls"] == [{
        "name": "transaction_summary",
        "durationMs": 125,
        "failed": False,
    }]


def test_broken_framework_metric_objects_are_dropped_without_escaping():
    class BrokenOutput:
        @property
        def metrics(self):
            raise RuntimeError("metrics unavailable")

    token = begin_agent_metric_collection()
    try:
        record_agno_run_metrics(BrokenOutput(), stage="operator", model="operator-model")
        assert agent_metric_snapshot()["modelPasses"] == 0
    finally:
        end_agent_metric_collection(token)
