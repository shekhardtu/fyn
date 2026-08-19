"""Request-scoped aggregation of Agno's native run metrics.

One customer turn may invoke the Operator, Planner, Validator, Binder, repair
passes, and the optional related-question Suggester. Agno reports usage on each
``RunOutput``; this module preserves those measurements as one durable turn
summary without coupling the model helpers to the AG-UI persistence layer.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _MetricPass:
    stage: str
    model: str
    provider: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    duration_ms: float | None
    time_to_first_token_ms: float | None
    cost_usd: float | None


@dataclass
class _MetricCollection:
    passes: list[_MetricPass] = field(default_factory=list)


_active_collection: ContextVar[_MetricCollection | None] = ContextVar(
    "fyn_agent_metric_collection",
    default=None,
)


def begin_agent_metric_collection() -> Token:
    """Start an isolated metric collection for one durable agent run."""
    return _active_collection.set(_MetricCollection())


def end_agent_metric_collection(token: Token) -> None:
    """Restore the surrounding context after a durable run ends."""
    _active_collection.reset(token)


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _milliseconds(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, seconds) * 1000, 1)


def _cost(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(max(0.0, float(value)), 10)
    except (TypeError, ValueError):
        return None


def record_agno_run_metrics(run_output: Any, *, stage: str, model: str) -> None:
    """Add one completed Agno ``RunOutput.metrics`` to the active turn.

    Outside an AG-UI run no collection exists, so batch jobs and isolated unit
    calls retain their existing behavior. Missing provider fields stay missing;
    in particular, a cost of ``None`` is never silently estimated.
    """
    collection = _active_collection.get()
    metrics = getattr(run_output, "metrics", None)
    if collection is None or metrics is None:
        return
    input_tokens = _non_negative_int(getattr(metrics, "input_tokens", 0))
    output_tokens = _non_negative_int(getattr(metrics, "output_tokens", 0))
    total_tokens = _non_negative_int(getattr(metrics, "total_tokens", 0))
    if total_tokens == 0 and input_tokens + output_tokens:
        total_tokens = input_tokens + output_tokens
    collection.passes.append(_MetricPass(
        stage=stage,
        model=str(getattr(run_output, "model", None) or model),
        provider=(
            str(getattr(run_output, "model_provider", "")).strip() or None
        ),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_tokens=_non_negative_int(getattr(metrics, "cache_read_tokens", 0)),
        cache_write_tokens=_non_negative_int(getattr(metrics, "cache_write_tokens", 0)),
        reasoning_tokens=_non_negative_int(getattr(metrics, "reasoning_tokens", 0)),
        duration_ms=_milliseconds(getattr(metrics, "duration", None)),
        time_to_first_token_ms=_milliseconds(getattr(metrics, "time_to_first_token", None)),
        cost_usd=_cost(getattr(metrics, "cost", None)),
    ))


def agent_metric_snapshot() -> dict[str, Any]:
    """Return the current turn's JSON-safe aggregate and per-pass evidence."""
    collection = _active_collection.get()
    passes = list(collection.passes) if collection is not None else []
    costs = [item.cost_usd for item in passes if item.cost_usd is not None]
    durations = [item.duration_ms for item in passes if item.duration_ms is not None]
    first_token = next(
        (item.time_to_first_token_ms for item in passes if item.time_to_first_token_ms is not None),
        None,
    )
    cost_coverage = len(costs) / len(passes) if passes else 0.0
    exact_cost = round(sum(costs), 10) if passes and len(costs) == len(passes) else None
    return {
        "source": "agno_run_output",
        "modelPasses": len(passes),
        "inputTokens": sum(item.input_tokens for item in passes),
        "outputTokens": sum(item.output_tokens for item in passes),
        "totalTokens": sum(item.total_tokens for item in passes),
        "cacheReadTokens": sum(item.cache_read_tokens for item in passes),
        "cacheWriteTokens": sum(item.cache_write_tokens for item in passes),
        "reasoningTokens": sum(item.reasoning_tokens for item in passes),
        "modelDurationMs": round(sum(durations), 1) if durations else None,
        "firstModelTimeToFirstTokenMs": first_token,
        "costUsd": exact_cost,
        "costCoverage": round(cost_coverage, 4),
        "passes": [
            {
                "stage": item.stage,
                "model": item.model,
                "provider": item.provider,
                "inputTokens": item.input_tokens,
                "outputTokens": item.output_tokens,
                "totalTokens": item.total_tokens,
                "cacheReadTokens": item.cache_read_tokens,
                "cacheWriteTokens": item.cache_write_tokens,
                "reasoningTokens": item.reasoning_tokens,
                "durationMs": item.duration_ms,
                "timeToFirstTokenMs": item.time_to_first_token_ms,
                "costUsd": item.cost_usd,
            }
            for item in passes
        ],
    }
