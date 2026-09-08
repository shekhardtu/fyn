"""Native request usage plus compatible Agno logical-pass diagnostics.

A turn can make several provider requests around tools, delegates and repairs.
Capture each request independently of the final ``RunOutput``. Run and optional
enrichment workers own separate collections and their existing durable commits;
model helpers never take ownership of the financial persistence boundary.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
from typing import Any
from uuid import uuid4


@dataclass
class _MetricPass:
    stage: str
    model: str
    provider: str | None
    reasoning_profile: str | None
    prompt_characters: int | None
    prompt_components: dict[str, int]
    mounted_tools: list[str]
    tool_calls: list[dict[str, Any]]
    provider_requests: list[dict[str, Any]]
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
    requests: list[dict[str, Any]] = field(default_factory=list)


_active_collection: ContextVar[_MetricCollection | None] = ContextVar(
    "fyn_agent_metric_collection",
    default=None,
)
_active_stage: ContextVar[str] = ContextVar("fyn_provider_usage_stage", default="model")


@contextmanager
def provider_usage_stage(stage: str) -> Iterator[None]:
    token = _active_stage.set(stage)
    try:
        yield
    finally:
        _active_stage.reset(token)


_TOKEN_FIELDS = ("inputTokens", "outputTokens", "totalTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens")


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _reported_tokens(value: Any) -> int | None:
    # Missing usage is not a zero-cost request. Reject malformed counters too.
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class ProviderUsageAttempt:
    """One SDK request attempt; only whitelisted scalars enter the snapshot.

    Capture is in memory. The existing run/enrichment checkpoint owns durability,
    so telemetry never commits or rolls back a financial transaction.
    """

    def __init__(self, model: str, operation: str = "responses") -> None:
        self.started = time.monotonic()
        self.data: dict[str, Any] = {
            "attemptId": str(uuid4()), "stage": _active_stage.get()[:80],
            "operation": operation, "model": str(model)[:160],
            "provider": "OpenAI", "status": "started",
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "requestId": None, "responseId": None, "durationMs": None,
            **{key: None for key in _TOKEN_FIELDS},
        }
        collection = _active_collection.get()
        if collection is not None:
            collection.requests.append(self.data)

    def observe(self, response: Any) -> None:
        try:
            for source, target in (("_request_id", "requestId"), ("id", "responseId"), ("model", "model")):
                value = _field(response, source)
                if isinstance(value, str):
                    self.data[target] = value[:160]
            status = _field(response, "status")
            if self.data["operation"] == "embeddings":
                status = "completed"
            if status in {"completed", "failed", "incomplete", "cancelled"}:
                self.data["status"] = status
                self.data["durationMs"] = round((time.monotonic() - self.started) * 1000, 1)
            usage = _field(response, "usage")
            if usage is None:
                return
            embedding = self.data["operation"] == "embeddings"
            values = {
                "inputTokens": _field(usage, "prompt_tokens" if embedding else "input_tokens"),
                "outputTokens": 0 if embedding else _field(usage, "output_tokens"),
                "totalTokens": _field(usage, "total_tokens"),
                "cacheReadTokens": _field(_field(usage, "input_tokens_details"), "cached_tokens"),
                "cacheWriteTokens": _field(_field(usage, "input_tokens_details"), "cache_write_tokens"),
                "reasoningTokens": _field(_field(usage, "output_tokens_details"), "reasoning_tokens"),
            }
            for key, value in values.items():
                reported = _reported_tokens(value)
                if reported is not None:
                    self.data[key] = reported
        except Exception:
            # A future SDK shape must never turn observability into a failure.
            return

    def request_id(self, value: Any) -> None:
        if isinstance(value, str):
            self.data["requestId"] = value[:160]

    def finish(self, error: BaseException | None = None) -> None:
        self.data["durationMs"] = round((time.monotonic() - self.started) * 1000, 1)
        if self.data["status"] == "started":
            self.data["status"] = "failed" if error is not None else "incomplete"
        if error is not None:
            try:
                self.request_id(getattr(error, "request_id", None))
            except Exception:
                pass


def request_usage_snapshot(requests: list[dict[str, Any]], *, uncertain: str | None = None) -> dict[str, Any]:
    # Updates to a streamed response share an attempt ID. Recovery/retry merging
    # must not count that same observation twice; a new HTTP call has a new ID.
    unique = {item["attemptId"]: dict(item) for item in requests}
    values = list(unique.values())
    reported = [item for item in values if all(item.get(key) is not None for key in _TOKEN_FIELDS[:3])]
    coverage = (
        "interrupted" if uncertain == "interrupted" else
        "partial" if uncertain and reported else
        "unavailable" if uncertain else
        "not_used" if not values else
        "complete" if len(reported) == len(values) else
        "partial" if reported else "unavailable"
    )
    totals = {}
    for key in _TOKEN_FIELDS:
        known = [item[key] for item in values if item.get(key) is not None]
        totals[key] = sum(known) if known else 0 if coverage == "not_used" else None
    return {
        "coverage": coverage, "requestCount": len(values), "reportedRequests": len(reported),
        "historyComplete": uncertain is None,
        **totals, "requests": values,
    }


def merge_request_usage(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    snapshots = [base.get("requestUsage"), extra.get("requestUsage")]
    uncertain = None
    for owner, usage in zip((base, extra), snapshots):
        if (usage and not usage.get("historyComplete", True)) or (not usage and owner):
            uncertain = "interrupted" if usage and usage.get("coverage") == "interrupted" else uncertain or "unknown"
    requests = [item for usage in snapshots if usage for item in usage.get("requests", [])]
    return request_usage_snapshot(requests, uncertain=uncertain)


def interrupt_request_usage(metrics: dict[str, Any]) -> dict[str, Any]:
    """A lost process/lease cannot claim that its persisted subtotal is complete."""
    return {**metrics, "requestUsage": request_usage_snapshot(
        (metrics.get("requestUsage") or {}).get("requests", []), uncertain="interrupted",
    )}


def merge_agent_metric_snapshots(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Accumulate restart/retry evidence; native requests are deduplicated by ID."""
    passes = [*(base.get("passes") or []), *(extra.get("passes") or [])]
    costs = [item.get("costUsd") for item in passes if item.get("costUsd") is not None]
    first_token = base.get("firstModelTimeToFirstTokenMs")
    if first_token is None:
        first_token = extra.get("firstModelTimeToFirstTokenMs")
    merged = {
        "source": "agno_run_output",
        "modelPasses": len(passes),
        "providerRequestCount": sum(len(item.get("providerRequests") or []) for item in passes),
        **{key: sum(int(item.get(key) or 0) for item in passes) for key in _TOKEN_FIELDS},
        "modelDurationMs": non_overlapping_model_duration_ms(passes),
        "firstModelTimeToFirstTokenMs": first_token,
        "costUsd": round(sum(float(value) for value in costs), 10) if passes and len(costs) == len(passes) else None,
        "costCoverage": round(len(costs) / len(passes), 4) if passes else 0.0,
        "passes": passes,
        "requestUsage": merge_request_usage(base, extra),
    }
    for key in ("server", "client", "rollouts"):
        value = extra.get(key) or base.get(key)
        if value is not None:
            merged[key] = value
    if "durationMs" in base or "durationMs" in extra:
        merged["durationMs"] = sum(float(item.get("durationMs") or 0) for item in (base, extra))
    return merged


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


def _non_negative_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(max(0.0, float(value)), 1)
    except (TypeError, ValueError):
        return None


def _character_count(value: Any) -> int:
    try:
        if value is None:
            return 0
        if isinstance(value, str):
            return len(value)
        if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
            return sum(len(item) for item in value)
        # Never serialize arbitrary prompt/evidence objects merely to observe
        # them. Their provider token count and total prompt size still remain.
        return 0
    except Exception:
        return 0


def agent_instructions(agent: Any) -> Any:
    """Return instruction data without letting instrumentation introspection raise."""
    try:
        return getattr(agent, "instructions", None)
    except Exception:
        return None


def agent_reasoning_profile(agent: Any) -> str | None:
    """Read the provider profile after construction without influencing it."""
    try:
        value = getattr(getattr(agent, "model", None), "reasoning_effort", None)
        return str(value)[:32] if value else None
    except Exception:
        return None


def mounted_tool_names(agent: Any) -> list[str]:
    """Read mounted tool names defensively from an Agno Agent-like object."""
    try:
        tools = getattr(agent, "tools", None) or []
        values = tools.values() if isinstance(tools, dict) else tools
        names = []
        for tool in values:
            try:
                name = str(getattr(tool, "name", "") or "").strip()
                if name:
                    names.append(name[:160])
            except Exception:
                continue
        return list(dict.fromkeys(names))[:64]
    except Exception:
        return []


def _tool_call_metrics(run_output: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for execution in list(getattr(run_output, "tools", None) or [])[:64]:
        try:
            name = str(getattr(execution, "tool_name", "") or "").strip()
            if not name:
                continue
            native_metrics = getattr(execution, "metrics", None)
            calls.append({
                "name": name[:160],
                "durationMs": _milliseconds(getattr(native_metrics, "duration", None)),
                "failed": bool(getattr(execution, "tool_call_error", False)),
            })
        except Exception:
            # Telemetry is deliberately lossy. One unusual framework tool
            # object must never affect the financial run it is observing.
            continue
    return calls


def _provider_request_metrics(
    requests: list[dict[str, Any]] | None,
    *,
    default_model: str,
) -> list[dict[str, Any]]:
    """Sanitize content-free request events without serializing provider data."""

    values: list[dict[str, Any]] = []
    for item in (requests or [])[:32]:
        try:
            if not isinstance(item, dict):
                continue
            input_tokens = _non_negative_int(item.get("inputTokens"))
            output_tokens = _non_negative_int(item.get("outputTokens"))
            total_tokens = _non_negative_int(item.get("totalTokens"))
            if total_tokens == 0 and input_tokens + output_tokens:
                total_tokens = input_tokens + output_tokens
            values.append({
                "model": str(item.get("model") or default_model)[:160],
                "provider": str(item.get("provider") or "").strip()[:80] or None,
                "durationMs": _non_negative_float(item.get("durationMs")),
                "timeToFirstTokenMs": _non_negative_float(item.get("timeToFirstTokenMs")),
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "totalTokens": total_tokens,
                "cacheReadTokens": _non_negative_int(item.get("cacheReadTokens")),
                "cacheWriteTokens": _non_negative_int(item.get("cacheWriteTokens")),
                "reasoningTokens": _non_negative_int(item.get("reasoningTokens")),
            })
        except Exception:
            continue
    return values


def record_agno_run_metrics(
    run_output: Any,
    *,
    stage: str,
    model: str,
    reasoning_profile: str | None = None,
    prompt_characters: int | None = None,
    prompt_components: dict[str, Any] | None = None,
    mounted_tools: list[str] | None = None,
    provider_requests: list[dict[str, Any]] | None = None,
) -> None:
    """Add one completed Agno ``RunOutput.metrics`` to the active turn.

    Outside an AG-UI run no collection exists, so batch jobs and isolated unit
    calls retain their existing behavior. Missing provider fields stay missing;
    in particular, a cost of ``None`` is never silently estimated.
    """
    try:
        collection = _active_collection.get()
        metrics = getattr(run_output, "metrics", None)
        if collection is None or metrics is None:
            return
        input_tokens = _non_negative_int(getattr(metrics, "input_tokens", 0))
        output_tokens = _non_negative_int(getattr(metrics, "output_tokens", 0))
        total_tokens = _non_negative_int(getattr(metrics, "total_tokens", 0))
        if total_tokens == 0 and input_tokens + output_tokens:
            total_tokens = input_tokens + output_tokens
        safe_components = {
            str(key)[:80]: (
                _non_negative_int(value)
                if isinstance(value, int) and not isinstance(value, bool)
                else _character_count(value)
            )
            for key, value in (prompt_components or {}).items()
        }
        safe_tools = list(dict.fromkeys(
            str(name).strip()[:160]
            for name in (mounted_tools or [])
            if str(name).strip()
        ))[:64]
        resolved_model = str(getattr(run_output, "model", None) or model)[:160]
        collection.passes.append(_MetricPass(
            stage=str(stage)[:80],
            model=resolved_model,
            provider=(
                str(getattr(run_output, "model_provider", "")).strip()[:80] or None
            ),
            reasoning_profile=str(reasoning_profile)[:32] if reasoning_profile else None,
            prompt_characters=(
                _non_negative_int(prompt_characters)
                if prompt_characters is not None
                else None
            ),
            prompt_components=safe_components,
            mounted_tools=safe_tools,
            tool_calls=_tool_call_metrics(run_output),
            provider_requests=_provider_request_metrics(
                provider_requests,
                default_model=resolved_model,
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
    except Exception:
        # Metrics are diagnostic evidence, never a dependency of execution.
        return


def non_overlapping_model_duration_ms(passes: list[Any]) -> float | None:
    """Sum provider-pass durations without double-counting nested delegates.

    Agno's outer Operator duration already spans a synchronous delegate tool
    call. The delegate remains a separate pass for tokens, cost, TTFT, and
    diagnostics, but adding both durations would report more time than the
    customer actually waited. Independent validation/repair passes remain
    additive because they execute after the Operator returns.
    """
    try:
        def field(item: Any, name: str) -> Any:
            return item.get(name) if isinstance(item, dict) else getattr(item, name, None)

        has_outer_operator = any(
            field(item, "stage") == "operator_response" for item in passes
        )
        durations = [
            float(duration)
            for item in passes
            if not (
                has_outer_operator
                and field(item, "stage") == "analysis_delegate"
            )
            and (duration := field(item, "duration_ms") if not isinstance(item, dict)
                 else item.get("durationMs")) is not None
        ]
        return round(sum(durations), 1) if durations else None
    except Exception:
        # Aggregation is observational only. If a future framework object is
        # unusual, omit this field instead of affecting the customer run.
        return None


def agent_metric_snapshot() -> dict[str, Any]:
    """Return the current turn's JSON-safe aggregate and per-pass evidence."""
    collection = _active_collection.get()
    passes = list(collection.passes) if collection is not None else []
    # A nested delegate completes and records before the outer Operator can
    # finish, even though the Operator was the first provider pass to start.
    # Restore chronological presentation so first-model TTFT and the execution
    # trace continue to mean the customer's first pass, not the first pass that
    # happened to return.
    operator_index = next(
        (index for index, item in enumerate(passes) if item.stage == "operator_response"),
        None,
    )
    if operator_index is not None:
        delegates = [item for item in passes if item.stage == "analysis_delegate"]
        if delegates:
            passes = [item for item in passes if item.stage != "analysis_delegate"]
            operator_index = next(
                index for index, item in enumerate(passes) if item.stage == "operator_response"
            )
            passes[operator_index + 1:operator_index + 1] = delegates
    costs = [item.cost_usd for item in passes if item.cost_usd is not None]
    first_token = next(
        (item.time_to_first_token_ms for item in passes if item.time_to_first_token_ms is not None),
        None,
    )
    cost_coverage = len(costs) / len(passes) if passes else 0.0
    exact_cost = round(sum(costs), 10) if passes and len(costs) == len(passes) else None
    return {
        "source": "agno_run_output",
        "requestUsage": request_usage_snapshot(
            collection.requests if collection is not None else [],
            uncertain="legacy" if passes and not (collection and collection.requests) else None,
        ),
        "modelPasses": len(passes),
        "providerRequestCount": sum(len(item.provider_requests) for item in passes),
        "inputTokens": sum(item.input_tokens for item in passes),
        "outputTokens": sum(item.output_tokens for item in passes),
        "totalTokens": sum(item.total_tokens for item in passes),
        "cacheReadTokens": sum(item.cache_read_tokens for item in passes),
        "cacheWriteTokens": sum(item.cache_write_tokens for item in passes),
        "reasoningTokens": sum(item.reasoning_tokens for item in passes),
        "modelDurationMs": non_overlapping_model_duration_ms(passes),
        "firstModelTimeToFirstTokenMs": first_token,
        "costUsd": exact_cost,
        "costCoverage": round(cost_coverage, 4),
        "passes": [
            {
                "stage": item.stage,
                "model": item.model,
                "provider": item.provider,
                "reasoningProfile": item.reasoning_profile,
                "promptCharacters": item.prompt_characters,
                "promptComponents": item.prompt_components,
                "mountedToolCount": len(item.mounted_tools),
                "mountedTools": item.mounted_tools,
                "toolCalls": item.tool_calls,
                "providerRequests": item.provider_requests,
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
