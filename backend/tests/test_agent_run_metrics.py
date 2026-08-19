from __future__ import annotations

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
