"""Content-free latency summaries and regression gates for agent runs."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ..models import AgentEnrichment, AgentRun


LATENCY_TARGETS_MS = {
    "client.submitToFirstActivityReceivedMs.p95": 250.0,
    "server.queueWaitMs.p95": 50.0,
    "modelBacked.providerTimeToFirstTokenMs.p50": 1_500.0,
    "modelBacked.providerTimeToFirstTokenMs.p95": 3_000.0,
    "modelBacked.acceptedToFirstTextMs.p50": 3_000.0,
    "modelBacked.acceptedToFirstTextMs.p95": 8_000.0,
    "promptLengthBuckets.0-10.acceptedToFirstTextMs.p50": 2_500.0,
    "promptLengthBuckets.0-10.acceptedToFirstTextMs.p95": 5_000.0,
    "promptLengthBuckets.11-25.acceptedToFirstTextMs.p50": 2_500.0,
    "promptLengthBuckets.11-25.acceptedToFirstTextMs.p95": 5_000.0,
    "client.submitToFirstAnswerVisibleMs.p95": 8_000.0,
    "client.submitToComposerUnlockedMs.p95": 8_500.0,
    "client.responseResolvedToComposerUnlockedMs.p95": 500.0,
    "enrichment.durationMs.p95": 3_000.0,
}

MINIMUM_TARGETS = {
    "modelBacked.aggregateCacheReadShare": 0.40,
}

MAXIMUM_COUNT_TARGETS = {
    "commonRead.mountedToolCount.p95": 8.0,
    "failures.telemetryOrEnrichmentCausedRuns": 0.0,
}

MINIMUM_SAMPLE_TARGETS = {
    "client.submitToFirstActivityReceivedMs.count": 30.0,
    "modelBacked.acceptedToFirstTextMs.count": 30.0,
    "promptLengthBuckets.0-10.acceptedToFirstTextMs.count": 10.0,
    "promptLengthBuckets.11-25.acceptedToFirstTextMs.count": 20.0,
    "scenarios.conversation_only.acceptedToFirstTextMs.count": 20.0,
    "scenarios.runtime_read.acceptedToFirstTextMs.count": 20.0,
    "scenarios.calculator.acceptedToFirstTextMs.count": 10.0,
    "scenarios.governed_analysis.acceptedToFirstTextMs.count": 20.0,
}

GOVERNED_SEMANTIC_ANALYSIS_TOOLS = {
    "analyze_category_volatility",
    "analyze_discretionary_spending_cap",
    "analyze_elapsed_month_category_comparison",
    "analyze_month_to_date_spending",
}


def parse_iso_datetime(value: str) -> datetime:
    """Parse browser ISO-8601 timestamps on every supported Python version."""
    normalized = f"{value[:-1]}+00:00" if value.upper().endswith("Z") else value
    return datetime.fromisoformat(normalized)


def percentile(values: Iterable[float], quantile: float) -> float | None:
    """Linear percentile compatible with small benchmark samples."""
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)) and float(value) >= 0)
    if not ordered:
        return None
    if len(ordered) == 1:
        return round(ordered[0], 1)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 1)


def distribution(values: Iterable[float | int | None]) -> dict[str, float | int | None]:
    observed = [float(value) for value in values if value is not None]
    return {
        "count": len(observed),
        "p50": percentile(observed, 0.50),
        "p95": percentile(observed, 0.95),
        "max": round(max(observed), 1) if observed else None,
    }


def _nested(metrics: dict[str, Any], *path: str) -> float | None:
    value: Any = metrics
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _prompt_bucket(run: AgentRun) -> str:
    payload = run.input_payload if isinstance(run.input_payload, dict) else {}
    text = payload.get("text")
    length = len(text) if isinstance(text, str) else 0
    if length <= 10:
        return "0-10"
    if length <= 25:
        return "11-25"
    if length <= 100:
        return "26-100"
    return "101+"


def _value_at(report: dict[str, Any], path: str) -> Any:
    value: Any = report
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _maximum_gate(report: dict[str, Any], path: str, target: float) -> dict[str, Any]:
    value = _value_at(report, path)
    return {
        "targetMaxMs": target,
        "actualMs": value,
        "passed": None if value is None else value <= target,
    }


def _minimum_gate(report: dict[str, Any], path: str, target: float) -> dict[str, Any]:
    value = _value_at(report, path)
    return {
        "targetMin": target,
        "actual": value,
        "passed": None if value is None else value >= target,
    }


def _maximum_count_gate(report: dict[str, Any], path: str, target: float) -> dict[str, Any]:
    value = _value_at(report, path)
    return {
        "targetMax": target,
        "actual": value,
        "passed": None if value is None else value <= target,
    }


def _attribute(item: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(item, name, default)
    except Exception:
        return default


def _passes(run: AgentRun) -> list[dict[str, Any]]:
    metrics = _attribute(run, "metrics", {})
    values = metrics.get("passes") if isinstance(metrics, dict) else None
    return [item for item in (values or []) if isinstance(item, dict)]


def _tool_calls(run: AgentRun) -> list[dict[str, Any]]:
    return [
        call
        for item in _passes(run)
        for call in (item.get("toolCalls") or [])
        if isinstance(call, dict)
    ]


def _provider_requests(run: AgentRun) -> list[dict[str, Any]]:
    return [
        request
        for item in _passes(run)
        for request in (item.get("providerRequests") or [])
        if isinstance(request, dict)
    ]


def _provider_request_count(run: AgentRun) -> int | None:
    try:
        metrics = _attribute(run, "metrics", {}) or {}
        if not isinstance(metrics, dict) or "providerRequestCount" not in metrics:
            return None
        return max(0, int(metrics.get("providerRequestCount") or 0))
    except (TypeError, ValueError):
        return None


def _provider_request_total_duration(run: AgentRun) -> float | None:
    try:
        requests = _provider_requests(run)
        durations = [
            float(item["durationMs"])
            for item in requests
            if item.get("durationMs") is not None
        ]
        return round(sum(durations), 1) if durations else None
    except (TypeError, ValueError):
        return None


def _scenario_class(run: AgentRun) -> str:
    """Classify by executed capability names, never request or result content."""

    if _nested(_attribute(run, "metrics", {}) or {}, "modelPasses") in (None, 0):
        return "non_model"
    names = {
        str(call.get("name") or "").strip().casefold()
        for call in _tool_calls(run)
        if str(call.get("name") or "").strip()
    }
    if any("delegat" in name for name in names):
        return "delegated_analysis"
    if any(
        name == "run_governed_sql"
        or name == "run_financial_analysis"
        or name.startswith("bind_template__")
        or any(marker in name for marker in ("spreadsheet", "federat", "python_analysis", "external_"))
        for name in names
    ) or names & GOVERNED_SEMANTIC_ANALYSIS_TOOLS:
        return "governed_analysis"
    if any("calculator" in name or name.startswith("loan_") for name in names):
        return "calculator"
    if names & {
        "transaction_list",
        "read_user_expense_taxonomy",
        "spending_summary",
        "transaction_summary",
    }:
        return "runtime_read"
    if not names:
        return "conversation_only"
    return "other_tool"


def _mounted_tool_count(run: AgentRun) -> int | None:
    counts = [
        int(item.get("mountedToolCount") or 0)
        for item in _passes(run)
        if item.get("mountedToolCount") is not None
    ]
    return max(counts) if counts else None


def _cache_share(run: AgentRun) -> float | None:
    metrics = _attribute(run, "metrics", {}) or {}
    input_tokens = _nested(metrics, "inputTokens")
    cache_tokens = _nested(metrics, "cacheReadTokens")
    if input_tokens is None or input_tokens <= 0 or cache_tokens is None:
        return None
    return round(min(cache_tokens / input_tokens, 1.0), 4)


def _difference(run: AgentRun, left: tuple[str, ...], right: tuple[str, ...]) -> float | None:
    metrics = _attribute(run, "metrics", {}) or {}
    left_value = _nested(metrics, *left)
    right_value = _nested(metrics, *right)
    if left_value is None or right_value is None or right_value < left_value:
        return None
    return right_value - left_value


def summarize_agent_latency(
    runs: Iterable[AgentRun],
    enrichments: Iterable[AgentEnrichment] = (),
) -> dict[str, Any]:
    """Aggregate timings without returning prompts, answers, or tool data."""
    run_rows = list(runs)
    enrichment_rows = list(enrichments)
    model_backed = [
        run
        for run in run_rows
        if _nested(_attribute(run, "metrics", {}) or {}, "modelPasses") not in (None, 0)
    ]

    def values(rows: list[AgentRun], *path: str) -> list[float | None]:
        return [_nested(_attribute(run, "metrics", {}) or {}, *path) for run in rows]

    def scenario_summary(rows: list[AgentRun]) -> dict[str, Any]:
        return {
            "runs": len(rows),
            "acceptedToFirstTextMs": distribution(values(rows, "server", "acceptedToFirstTextMs")),
            "acceptedToFinishedMs": distribution(values(rows, "server", "acceptedToFinishedMs")),
            "inputTokens": distribution(values(rows, "inputTokens")),
            "cacheReadShare": distribution(_cache_share(run) for run in rows),
            "mountedToolCount": distribution(_mounted_tool_count(run) for run in rows),
            "modelPasses": distribution(values(rows, "modelPasses")),
            "providerRequestCount": distribution(_provider_request_count(run) for run in rows),
            "toolCalls": distribution(len(_tool_calls(run)) for run in rows),
        }

    buckets: dict[str, dict[str, Any]] = {}
    for name in ("0-10", "11-25", "26-100", "101+"):
        rows = [run for run in model_backed if _prompt_bucket(run) == name]
        buckets[name] = {
            "runs": len(rows),
            "acceptedToFirstTextMs": distribution(values(rows, "server", "acceptedToFirstTextMs")),
        }

    status_counts = Counter(item.status for item in enrichment_rows)
    scenario_names = (
        "conversation_only",
        "runtime_read",
        "calculator",
        "governed_analysis",
        "delegated_analysis",
        "other_tool",
        "non_model",
    )
    scenarios = {
        name: scenario_summary([run for run in run_rows if _scenario_class(run) == name])
        for name in scenario_names
    }
    rollout_features = sorted({
        str(feature)
        for run in run_rows
        for feature in (
            (_attribute(run, "metrics", {}) or {}).get("rollouts", {})
        )
    })
    rollout_cohorts = {
        feature: {
            label: scenario_summary([
                run
                for run in run_rows
                if ((_attribute(run, "metrics", {}) or {}).get("rollouts", {})).get(feature)
                == label
            ])
            for label in sorted({
                str(((_attribute(run, "metrics", {}) or {}).get("rollouts", {})).get(feature))
                for run in run_rows
                if ((_attribute(run, "metrics", {}) or {}).get("rollouts", {})).get(feature)
            })
        }
        for feature in rollout_features
    }
    common_read_rows = [
        run
        for run in model_backed
        if _scenario_class(run) in {
            "runtime_read",
            "calculator",
            "governed_analysis",
        }
    ]
    all_tool_calls = [call for run in run_rows for call in _tool_calls(run)]
    tool_usage = Counter(
        str(call.get("name") or "").strip()
        for call in all_tool_calls
        if str(call.get("name") or "").strip()
    )
    failed_tool_calls = sum(bool(call.get("failed")) for call in all_tool_calls)
    tool_durations = [
        call.get("durationMs")
        for call in all_tool_calls
        if call.get("durationMs") is not None
    ]
    provider_requests = [
        request for run in model_backed for request in _provider_requests(run)
    ]
    model_input_tokens = sum(
        int(_nested(_attribute(run, "metrics", {}) or {}, "inputTokens") or 0)
        for run in model_backed
    )
    model_cache_tokens = sum(
        int(_nested(_attribute(run, "metrics", {}) or {}, "cacheReadTokens") or 0)
        for run in model_backed
    )
    aggregate_cache_share = (
        round(min(model_cache_tokens / model_input_tokens, 1.0), 4)
        if model_input_tokens > 0
        else None
    )
    run_status_counts = Counter(str(_attribute(run, "status", "unknown")) for run in run_rows)
    task_status_counts = Counter(str(_attribute(run, "task_status", "unknown")) for run in run_rows)
    failed_runs = [
        run
        for run in run_rows
        if _attribute(run, "status") == "failed" or _attribute(run, "task_status") == "failed"
    ]
    telemetry_caused_failures = sum(
        str(_attribute(run, "failure_stage", "") or "").casefold() in {"telemetry", "enrichment"}
        or str(_attribute(run, "error_code", "") or "").casefold().startswith(("telemetry_", "enrichment_"))
        for run in failed_runs
    )
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "runs": len(run_rows),
        "modelBackedRuns": len(model_backed),
        "server": {
            "queueWaitMs": distribution(values(run_rows, "server", "queueWaitMs")),
            "startedToFirstActivityMs": distribution(values(run_rows, "server", "startedToFirstActivityMs")),
            "startedToFirstReasoningMs": distribution(values(run_rows, "server", "startedToFirstReasoningMs")),
            "startedToFirstToolCallMs": distribution(values(run_rows, "server", "startedToFirstToolCallMs")),
            "acceptedToFirstTextMs": distribution(values(run_rows, "server", "acceptedToFirstTextMs")),
            "acceptedToFinishedMs": distribution(values(run_rows, "server", "acceptedToFinishedMs")),
        },
        "modelBacked": {
            "acceptedToFirstTextMs": distribution(values(model_backed, "server", "acceptedToFirstTextMs")),
            "acceptedToFinishedMs": distribution(values(model_backed, "server", "acceptedToFinishedMs")),
            "providerTimeToFirstTokenMs": distribution(values(model_backed, "firstModelTimeToFirstTokenMs")),
            "providerRequestCount": distribution(
                _provider_request_count(run) for run in model_backed
            ),
            "providerRequestDurationMs": distribution(
                item.get("durationMs") for item in provider_requests
            ),
            "providerRequestTimeToFirstTokenMs": distribution(
                item.get("timeToFirstTokenMs") for item in provider_requests
            ),
            "providerRequestTotalDurationMs": distribution(
                _provider_request_total_duration(run) for run in model_backed
            ),
            "modelDurationMs": distribution(values(model_backed, "modelDurationMs")),
            "inputTokens": distribution(values(model_backed, "inputTokens")),
            "cacheReadTokens": distribution(values(model_backed, "cacheReadTokens")),
            "cacheReadShare": distribution(_cache_share(run) for run in model_backed),
            "aggregateCacheReadShare": aggregate_cache_share,
            "reasoningTokens": distribution(values(model_backed, "reasoningTokens")),
            "mountedToolCount": distribution(_mounted_tool_count(run) for run in model_backed),
            "toolCalls": distribution(len(_tool_calls(run)) for run in model_backed),
        },
        "client": {
            "submitToRunCreatedMs": distribution(values(run_rows, "client", "submitToRunCreatedMs")),
            "submitToFirstActivityReceivedMs": distribution(values(run_rows, "client", "submitToFirstActivityReceivedMs")),
            "submitToFirstReasoningReceivedMs": distribution(values(run_rows, "client", "submitToFirstReasoningReceivedMs")),
            "submitToFirstTextReceivedMs": distribution(values(run_rows, "client", "submitToFirstTextReceivedMs")),
            "submitToFirstAnswerVisibleMs": distribution(values(run_rows, "client", "submitToFirstAnswerVisibleMs")),
            "submitToResponseResolvedMs": distribution(values(run_rows, "client", "submitToResponseResolvedMs")),
            "submitToComposerUnlockedMs": distribution(values(run_rows, "client", "submitToComposerUnlockedMs")),
            "answerVisibleToComposerUnlockedMs": distribution(
                _difference(
                    run,
                    ("client", "submitToFirstAnswerVisibleMs"),
                    ("client", "submitToComposerUnlockedMs"),
                )
                for run in run_rows
            ),
            "responseResolvedToComposerUnlockedMs": distribution(
                _difference(
                    run,
                    ("client", "submitToResponseResolvedMs"),
                    ("client", "submitToComposerUnlockedMs"),
                )
                for run in run_rows
            ),
        },
        "commonRead": scenario_summary(common_read_rows),
        "scenarios": scenarios,
        "rollouts": rollout_cohorts,
        "tools": {
            "calls": len(all_tool_calls),
            "failedCalls": failed_tool_calls,
            "failureRate": round(failed_tool_calls / len(all_tool_calls), 4) if all_tool_calls else None,
            "durationMs": distribution(tool_durations),
            "usage": dict(sorted(tool_usage.items())),
        },
        "failures": {
            "runStatusCounts": dict(sorted(run_status_counts.items())),
            "taskStatusCounts": dict(sorted(task_status_counts.items())),
            "failedRuns": len(failed_runs),
            "telemetryOrEnrichmentCausedRuns": telemetry_caused_failures,
        },
        "promptLengthBuckets": buckets,
        "enrichment": {
            "scheduled": len(enrichment_rows),
            "statusCounts": dict(sorted(status_counts.items())),
            "durationMs": distribution(
                _nested(item.metrics or {}, "enrichmentDurationMs")
                for item in enrichment_rows
            ),
        },
    }
    report["gates"] = {
        **{
            path: _maximum_gate(report, path, target)
            for path, target in LATENCY_TARGETS_MS.items()
        },
        **{
            path: _minimum_gate(report, path, target)
            for path, target in MINIMUM_TARGETS.items()
        },
        **{
            path: _maximum_count_gate(report, path, target)
            for path, target in MAXIMUM_COUNT_TARGETS.items()
        },
        **{
            path: _minimum_gate(report, path, target)
            for path, target in MINIMUM_SAMPLE_TARGETS.items()
        },
    }
    failed_gates = sorted(
        path for path, gate in report["gates"].items() if gate["passed"] is False
    )
    unknown_gates = sorted(
        path for path, gate in report["gates"].items() if gate["passed"] is None
    )
    report["releaseReadiness"] = {
        "passed": not failed_gates and not unknown_gates,
        "failedGates": failed_gates,
        "unknownGates": unknown_gates,
    }
    return report
