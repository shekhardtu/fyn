from __future__ import annotations

import copy
from collections import Counter
from datetime import datetime, timezone

import pytest

from evals.browser.cli import (
    DEFAULT_SUITE,
    EvalValidationError,
    derive_summary,
    load_suite,
    new_report,
    validate_report,
    validate_suite,
)


def _complete_measurements(report, suite):
    cases = {case["id"]: case for case in suite["cases"]}
    for result in report["results"]:
        case = cases[result["caseId"]]
        result["observedResponse"] = "Three visible repetitions produced the expected response."
        for attempt in result["measurements"]["attempts"]:
            attempt["observedOutcome"] = case["expectedOutcome"]
            attempt["routeSignature"] = "request > operator > validator > execution > grounding"
            for turn in attempt["turns"]:
                turn.update({
                    "runId": f"run-{result['caseId']}-{attempt['attempt']}-{turn['step']}",
                    "timeToFirstResponseMs": 800 + attempt["attempt"] * 10,
                    "totalDurationMs": 1200 + attempt["attempt"] * 20,
                    "modelDurationMs": 900,
                    "modelPassCount": 2,
                    "inputTokens": 100,
                    "outputTokens": 20,
                    "totalTokens": 120,
                    "costUsd": None,
                    "costCoverage": 0,
                    "costBasis": "unavailable",
                })


def test_suite_has_four_valid_cases_per_tier():
    suite = load_suite(DEFAULT_SUITE)

    # Every tier keeps at least its original four; the count grows as new
    # product surfaces earn cases, so the floor is asserted, not the total.
    tiers = Counter(case["tier"] for case in suite["cases"])
    assert len(suite["cases"]) == sum(tiers.values())
    assert all(tiers[tier] >= 4 for tier in ("easy", "medium", "hard", "complex")), tiers
    assert Counter(case["tier"] for case in suite["cases"]) == {
        "easy": tiers["easy"],
        "medium": tiers["medium"],
        "hard": tiers["hard"],
        "complex": tiers["complex"],
    }
    assert all(sum(item["weight"] for item in case["assertions"]) == 10 for case in suite["cases"])
    assert suite["fixture"]["account"] == {
        "identifierType": "phone",
        "identifier": "+919000000098",
        "dedicated": True,
    }


def test_suite_rejects_an_oracle_that_drifted_from_demo_finances():
    suite = copy.deepcopy(load_suite(DEFAULT_SUITE))
    suite["fixture"]["oracle"]["currentExpenseTotalMinor"] += 1

    with pytest.raises(EvalValidationError, match="oracle has drifted"):
        validate_suite(suite)


def test_new_report_is_complete_valid_and_pending():
    suite = load_suite(DEFAULT_SUITE)
    report = new_report(
        suite,
        base_url="http://localhost:3000",
        evaluator="test-browser-agent",
        model="test-model",
        now=datetime(2026, 8, 17, 5, 30, tzinfo=timezone.utc),
    )

    validate_report(report, suite)
    assert report["run"]["id"] == "browser-eval-20260817T053000Z"
    assert report["summary"]["pending"] == len(suite["cases"])
    assert report["summary"]["metrics"]["predictability"]["casesMeasured"] == 0
    assert [item["caseId"] for item in report["results"]] == [case["id"] for case in suite["cases"]]
    assert all(len(item["measurements"]["attempts"]) == 3 for item in report["results"])


def test_summary_requires_threshold_and_every_critical_assertion():
    suite = load_suite(DEFAULT_SUITE)
    report = new_report(
        suite,
        base_url="http://localhost:3000",
        evaluator="test-browser-agent",
        model="test-model",
    )
    _complete_measurements(report, suite)
    for result in report["results"]:
        for assertion in result["assertions"]:
            assertion["passed"] = True
            assertion["evidence"] = "Visible expected result."

    first = report["results"][0]
    first["assertions"][0]["passed"] = False
    first["assertions"][0]["evidence"] = "The visible amount was incorrect."
    report["summary"] = derive_summary(report, suite)

    assert first["status"] == "fail"
    total = len(suite["cases"])
    assert report["summary"]["failed"] == 1
    assert report["summary"]["passed"] == total - 1
    assert report["summary"]["status"] == "fail"
    assert report["summary"]["metrics"]["correctness"] == {
        "score": report["summary"]["score"],
        "passedCases": total - 1,
        "totalCases": total,
    }
    assert report["summary"]["metrics"]["predictability"]["casesMeasured"] == total
    assert report["summary"]["metrics"]["cost"]["costUsd"] is None
    assert report["summary"]["metrics"]["cost"]["totalTokens"] > 0
    validate_report(report, suite)


def test_evaluated_assertion_requires_visible_evidence():
    suite = load_suite(DEFAULT_SUITE)
    report = new_report(
        suite,
        base_url="http://localhost:3000",
        evaluator="test-browser-agent",
        model="test-model",
    )
    report["results"][0]["assertions"][0]["passed"] = True

    with pytest.raises(EvalValidationError, match="require concise evidence"):
        validate_report(report, suite, require_derived=False)


def test_blocked_reason_is_derived_into_blocked_status():
    suite = load_suite(DEFAULT_SUITE)
    report = new_report(
        suite,
        base_url="http://localhost:3000",
        evaluator="test-browser-agent",
        model="test-model",
    )
    report["results"][0]["blockedReason"] = "The dedicated fixture account is unavailable."

    validate_report(report, suite, require_derived=False)
    report["summary"] = derive_summary(report, suite)

    assert report["results"][0]["status"] == "blocked"
    assert report["summary"]["blocked"] == 1
    validate_report(report, suite)


def test_predictability_reports_repeated_outcome_route_and_latency_consistency():
    suite = load_suite(DEFAULT_SUITE)
    report = new_report(
        suite,
        base_url="http://localhost:3000",
        evaluator="test-browser-agent",
        model="test-model",
    )
    _complete_measurements(report, suite)
    for result in report["results"]:
        for assertion in result["assertions"]:
            assertion["passed"] = True
            assertion["evidence"] = "Visible in all three repetitions."
    first_attempts = report["results"][0]["measurements"]["attempts"]
    first_attempts[2]["routeSignature"] = "request > operator > clarification"

    report["summary"] = derive_summary(report, suite)

    predictability = report["summary"]["metrics"]["predictability"]
    assert predictability["casesMeasured"] == len(suite["cases"])
    assert predictability["meanOutcomeConsistency"] == 1
    # One of the three repetitions of one case took a different route, so the
    # mean is that single case's 2/3 averaged with a clean run everywhere else.
    total = len(suite["cases"])
    assert predictability["meanRouteConsistency"] == round(((total - 1) + 2 / 3) / total, 4)
    assert predictability["meanLatencyCv"] > 0
    validate_report(report, suite)


def test_completed_case_requires_all_three_attempt_measurements():
    suite = load_suite(DEFAULT_SUITE)
    report = new_report(
        suite,
        base_url="http://localhost:3000",
        evaluator="test-browser-agent",
        model="test-model",
    )
    for assertion in report["results"][0]["assertions"]:
        assertion["passed"] = True
        assertion["evidence"] = "Visible expected result."

    with pytest.raises(EvalValidationError, match="evaluated cases require outcome and route"):
        validate_report(report, suite, require_derived=False)
