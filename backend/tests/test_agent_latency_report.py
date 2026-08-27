from __future__ import annotations

from types import SimpleNamespace

from app.services.agent_latency_report import (
    distribution,
    parse_iso_datetime,
    percentile,
    summarize_agent_latency,
)


def run(
    text: str,
    first_text: float,
    *,
    model_passes: int = 1,
    tool_names: tuple[str, ...] = (),
    mounted_tools: int = 4,
    input_tokens: int = 1_000,
    cache_read_tokens: int = 500,
    status: str = "completed",
    task_status: str = "succeeded",
    failure_stage: str | None = None,
    error_code: str | None = None,
    provider_requests: tuple[tuple[float, float], ...] = ((1_500, 500),),
):
    passes = [
        {
            "stage": "operator_response",
            "mountedToolCount": mounted_tools,
            "toolCalls": [
                {"name": name, "durationMs": 125, "failed": False}
                for name in tool_names
            ],
            "providerRequests": [
                {"durationMs": duration, "timeToFirstTokenMs": first_token}
                for duration, first_token in provider_requests
            ],
        }
    ] if model_passes else []
    return SimpleNamespace(
        input_payload={"kind": "message", "text": text},
        status=status,
        task_status=task_status,
        failure_stage=failure_stage,
        error_code=error_code,
        metrics={
            "modelPasses": model_passes,
            "providerRequestCount": len(provider_requests) if model_passes else 0,
            "passes": passes,
            "inputTokens": input_tokens,
            "cacheReadTokens": cache_read_tokens,
            "reasoningTokens": 10,
            "modelDurationMs": 2_000,
            "firstModelTimeToFirstTokenMs": 500,
            "server": {
                "queueWaitMs": 8,
                "startedToFirstActivityMs": 20,
                "acceptedToFirstTextMs": first_text,
                "acceptedToFinishedMs": first_text + 500,
            },
            "client": {
                "submitToRunCreatedMs": 10,
                "submitToFirstActivityReceivedMs": 40,
                "submitToFirstAnswerVisibleMs": first_text + 20,
                "submitToResponseResolvedMs": first_text + 500,
                "submitToComposerUnlockedMs": first_text + 540,
            },
        },
    )


def test_percentiles_and_empty_distributions_are_stable():
    assert percentile([10, 20, 30], 0.5) == 20
    assert distribution([]) == {"count": 0, "p50": None, "p95": None, "max": None}


def test_browser_utc_cohort_timestamp_is_accepted_on_python_39():
    parsed = parse_iso_datetime("2026-08-27T08:21:40.047Z")

    assert parsed.isoformat() == "2026-08-27T08:21:40.047000+00:00"


def test_report_buckets_short_prompts_and_never_emits_content():
    report = summarize_agent_latency([
        run("Hi", 900),
        run("How are you doing?", 2_000),
        run("This is a substantially longer financial analysis request", 4_000),
    ])

    assert report["runs"] == 3
    assert report["modelBacked"]["acceptedToFirstTextMs"]["p50"] == 2_000
    assert report["promptLengthBuckets"]["0-10"]["runs"] == 1
    assert report["promptLengthBuckets"]["11-25"]["runs"] == 1
    assert report["client"]["submitToFirstActivityReceivedMs"]["p95"] == 40
    assert report["client"]["answerVisibleToComposerUnlockedMs"]["p50"] == 520
    assert report["client"]["responseResolvedToComposerUnlockedMs"]["p50"] == 40
    assert report["modelBacked"]["aggregateCacheReadShare"] == 0.5
    assert report["modelBacked"]["providerRequestCount"]["p50"] == 1
    assert report["modelBacked"]["providerRequestDurationMs"]["p50"] == 1_500
    serialized = str(report)
    assert "How are you doing" not in serialized
    assert "financial analysis request" not in serialized


def test_streaming_duration_does_not_fail_post_response_unlock_gate():
    item = run("Explain it", 900)
    item.metrics["client"]["submitToFirstAnswerVisibleMs"] = 1_000
    item.metrics["client"]["submitToResponseResolvedMs"] = 6_000
    item.metrics["client"]["submitToComposerUnlockedMs"] = 6_040

    report = summarize_agent_latency([item])

    assert report["client"]["answerVisibleToComposerUnlockedMs"]["p95"] == 5_040
    assert report["gates"]["client.responseResolvedToComposerUnlockedMs.p95"]["passed"] is True


def test_gates_stay_unknown_until_the_browser_has_samples():
    item = run("Hi", 900)
    item.metrics.pop("client")
    report = summarize_agent_latency([item])

    assert report["gates"]["client.submitToFirstAnswerVisibleMs.p95"]["passed"] is None
    assert report["gates"]["modelBacked.acceptedToFirstTextMs.p50"]["passed"] is True
    assert report["releaseReadiness"]["passed"] is False
    assert "client.submitToFirstActivityReceivedMs.count" in report["releaseReadiness"]["failedGates"]


def test_report_groups_content_free_execution_scenarios_and_tool_health():
    report = summarize_agent_latency([
        run("Hello", 700, tool_names=()),
        run("Total", 1_100, tool_names=("transaction_list",), mounted_tools=7),
        run("EMI", 1_300, tool_names=("run_financial_calculator",), mounted_tools=8),
        run("Compare", 2_500, tool_names=("run_governed_sql",), mounted_tools=8),
    ])

    assert report["scenarios"]["conversation_only"]["runs"] == 1
    assert report["scenarios"]["runtime_read"]["runs"] == 1
    assert report["scenarios"]["calculator"]["runs"] == 1
    assert report["scenarios"]["governed_analysis"]["runs"] == 1
    assert report["commonRead"]["mountedToolCount"]["p95"] == 8
    assert report["tools"]["calls"] == 3
    assert report["tools"]["failedCalls"] == 0
    assert report["tools"]["usage"] == {
        "run_financial_calculator": 1,
        "run_governed_sql": 1,
        "transaction_list": 1,
    }
    assert report["gates"]["commonRead.mountedToolCount.p95"]["passed"] is True


def test_report_counts_authenticated_semantic_reads_as_governed_analysis():
    report = summarize_agent_latency([
        run("Monthly total", 1_100, tool_names=("analyze_month_to_date_spending",)),
        run("Elapsed comparison", 1_300, tool_names=("analyze_elapsed_month_category_comparison",)),
        run("Volatility", 1_500, tool_names=("analyze_category_volatility",)),
        run("Cap", 1_700, tool_names=("analyze_discretionary_spending_cap",)),
    ])

    assert report["scenarios"]["governed_analysis"]["runs"] == 4
    assert report["scenarios"]["other_tool"]["runs"] == 0
    assert report["commonRead"]["toolCalls"]["count"] == 4


def test_report_exposes_serial_provider_round_trips_inside_one_agent_invocation():
    report = summarize_agent_latency([
        run("EMI", 6_000, provider_requests=((1_200, 400), (4_100, 700))),
    ])

    assert report["modelBacked"]["providerRequestCount"]["p50"] == 2
    assert report["modelBacked"]["providerRequestDurationMs"]["p95"] == 3_955
    assert report["modelBacked"]["providerRequestTimeToFirstTokenMs"]["p50"] == 550
    assert report["modelBacked"]["providerRequestTotalDurationMs"]["p50"] == 5_300


def test_telemetry_or_enrichment_can_never_be_a_run_failure_gate():
    report = summarize_agent_latency([
        run(
            "Hello",
            700,
            status="failed",
            task_status="failed",
            failure_stage="telemetry",
            error_code="telemetry_store_failed",
        ),
    ])

    assert report["failures"]["failedRuns"] == 1
    assert report["failures"]["telemetryOrEnrichmentCausedRuns"] == 1
    assert report["gates"]["failures.telemetryOrEnrichmentCausedRuns"]["passed"] is False
