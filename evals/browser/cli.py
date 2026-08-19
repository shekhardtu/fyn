from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_SUITE = HERE / "suite.yaml"
SUITE_SCHEMA = HERE / "suite.schema.json"
REPORT_SCHEMA = HERE / "report.schema.json"
TIERS = ("easy", "medium", "hard", "complex")


class EvalValidationError(ValueError):
    """Raised when an eval suite or report violates its contract."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalValidationError(f"Could not read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvalValidationError(f"{path} must contain a JSON object")
    return value


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise EvalValidationError(f"Could not read YAML from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvalValidationError(f"{path} must contain a YAML object")
    return value


def _schema_errors(instance: dict[str, Any], schema_path: Path) -> list[str]:
    schema = _read_json(schema_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path))
    rendered = []
    for error in errors:
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        rendered.append(f"{location}: {error.message}")
    return rendered


def _computed_oracle() -> dict[str, Any]:
    backend = REPO_ROOT / "backend"
    sys.path.insert(0, str(backend))
    try:
        from app.demo_finances import DEMO_EXPENSES, DEMO_INCOMES, DEMO_MONTH_FACTORS
    finally:
        sys.path.pop(0)

    categories_by_period: dict[str, defaultdict[str, int]] = {
        period: defaultdict(int) for period in DEMO_MONTH_FACTORS
    }
    food_by_period: dict[str, defaultdict[str, int]] = {
        period: defaultdict(int) for period in DEMO_MONTH_FACTORS
    }
    merchant_amounts: dict[str, int] = {}
    ranked: list[tuple[str, int]] = []

    for index, (category, subcategory, merchant, amount_minor) in enumerate(DEMO_EXPENSES):
        for period, factors in DEMO_MONTH_FACTORS.items():
            period_minor = amount_minor * factors[index % len(factors)] // 100
            categories_by_period[period][category] += period_minor
            if category == "food":
                food_by_period[period][subcategory] += period_minor
        merchant_amounts[merchant] = amount_minor
        ranked.append((merchant, amount_minor))

    expense_totals = {period: sum(values.values()) for period, values in categories_by_period.items()}
    income_totals = {
        period: sum(amount_minor for _subcategory, _merchant, amount_minor in incomes)
        for period, incomes in DEMO_INCOMES.items()
    }
    selected_merchants = ("Air India", "BigBasket", "Home rent", "IndiGo")

    return {
        "currency": "INR",
        "currentExpenseTotalMinor": expense_totals["current"],
        "previousExpenseTotalMinor": expense_totals["previous"],
        "twoMonthsAgoExpenseTotalMinor": expense_totals["two_months_ago"],
        "currentIncomeTotalMinor": income_totals["current"],
        "previousIncomeTotalMinor": income_totals["previous"],
        "twoMonthsAgoIncomeTotalMinor": income_totals["two_months_ago"],
        "currentNetMinor": income_totals["current"] - expense_totals["current"],
        "previousNetMinor": income_totals["previous"] - expense_totals["previous"],
        "twoMonthsAgoNetMinor": income_totals["two_months_ago"] - expense_totals["two_months_ago"],
        "categoryTotalsMinor": {
            "current": dict(sorted(categories_by_period["current"].items())),
            "previous": dict(sorted(categories_by_period["previous"].items())),
            "twoMonthsAgo": dict(sorted(categories_by_period["two_months_ago"].items())),
        },
        "foodSubcategoryTotalsMinor": {
            "current": dict(sorted(food_by_period["current"].items())),
            "previous": dict(sorted(food_by_period["previous"].items())),
            "twoMonthsAgo": dict(sorted(food_by_period["two_months_ago"].items())),
        },
        "merchantAmountsMinor": {name: merchant_amounts[name] for name in selected_merchants},
        "topExpenseMerchants": [
            {"merchant": merchant, "amountMinor": amount_minor}
            for merchant, amount_minor in sorted(ranked, key=lambda item: item[1], reverse=True)[:3]
        ],
        **_computed_query_oracle(DEMO_EXPENSES, categories_by_period["current"], expense_totals["current"]),
    }


# Answers the compact query grammar cannot reach: a ranked read under several
# column filters at once, a per-group comparison against that group's own
# total, and a refinement chain whose final share depends on both earlier
# turns. Each is derived here so a demo-data change fails validation rather
# than quietly making a suite question wrong.
CONCENTRATION_FLOOR = 0.5
REFINEMENT_MERCHANT_FLOOR_MINOR = 800_000
TRANSPORT_FLOOR_MINOR = 50_000


def _computed_query_oracle(
    demo_expenses: Any, current_categories: Any, current_total: int
) -> dict[str, Any]:
    current = {
        (category, subcategory, merchant): amount_minor
        for category, subcategory, merchant, amount_minor in demo_expenses
    }
    transport = sorted(
        (
            (amount_minor, merchant)
            for (category, subcategory, merchant), amount_minor in current.items()
            if category == "transport"
            and subcategory != "flights"
            and amount_minor > TRANSPORT_FLOOR_MINOR
        ),
        reverse=True,
    )[:3]

    concentrated = []
    for category, category_total in sorted(current_categories.items()):
        amount_minor, merchant = max(
            (amount, name)
            for (this_category, _subcategory, name), amount in current.items()
            if this_category == category
        )
        if amount_minor > category_total * CONCENTRATION_FLOOR:
            concentrated.append({
                "category": category,
                "merchant": merchant,
                "amountMinor": amount_minor,
                "categoryTotalMinor": category_total,
                "sharePercent": round(amount_minor / category_total * 100, 1),
            })

    ranked_current = sorted(
        ((amount, merchant) for (_c, _s, merchant), amount in current.items()), reverse=True
    )[:5]
    kept = [
        {"merchant": merchant, "amountMinor": amount}
        for amount, merchant in ranked_current
        if merchant != "Home rent" and amount >= REFINEMENT_MERCHANT_FLOOR_MINOR
    ]
    kept_total = sum(item["amountMinor"] for item in kept)

    return {
        "topTransportExcludingFlights": [
            {"merchant": merchant, "amountMinor": amount_minor}
            for amount_minor, merchant in transport
        ],
        "concentratedCategories": concentrated,
        "refinementChain": {
            "topFive": [
                {"merchant": merchant, "amountMinor": amount}
                for amount, merchant in ranked_current
            ],
            "kept": kept,
            "keptTotalMinor": kept_total,
            "sharePercentOfMonth": round(kept_total / current_total * 100, 2),
        },
    }


def _computed_source_oracle() -> dict[str, Any]:
    """The oracle half the fixture's foreign sources own."""
    from evals.browser.fixture_sources import (
        budget_totals_minor,
        vendor_merchant_totals_minor,
        vendor_totals_minor,
    )

    return {
        "budgetSheetTotalsMinor": budget_totals_minor(),
        "vendorCategoryTotalsMinor": vendor_totals_minor(),
        "vendorTotalsMinor": vendor_merchant_totals_minor(),
    }


def validate_suite(suite: dict[str, Any]) -> None:
    errors = _schema_errors(suite, SUITE_SCHEMA)
    if errors:
        raise EvalValidationError("Suite schema validation failed:\n- " + "\n- ".join(errors))

    cases = suite["cases"]
    case_ids = [case["id"] for case in cases]
    duplicate_cases = sorted(case_id for case_id, count in Counter(case_ids).items() if count > 1)
    if duplicate_cases:
        raise EvalValidationError(f"Duplicate case IDs: {', '.join(duplicate_cases)}")

    tier_counts = Counter(case["tier"] for case in cases)
    missing_tiers = [tier for tier in TIERS if tier_counts[tier] == 0]
    if missing_tiers:
        raise EvalValidationError(f"Suite has no cases for: {', '.join(missing_tiers)}")

    for case in cases:
        if not case["id"].startswith(f"{case['tier']}."):
            raise EvalValidationError(f"{case['id']}: ID prefix must match tier {case['tier']}")
        assertion_ids = [assertion["id"] for assertion in case["assertions"]]
        duplicates = sorted(item for item, count in Counter(assertion_ids).items() if count > 1)
        if duplicates:
            raise EvalValidationError(f"{case['id']}: duplicate assertion IDs: {', '.join(duplicates)}")
        weight = sum(float(assertion["weight"]) for assertion in case["assertions"])
        if abs(weight - 10) > 1e-9:
            raise EvalValidationError(f"{case['id']}: assertion weights total {weight:g}, expected 10")
        if not any(assertion["critical"] for assertion in case["assertions"]):
            raise EvalValidationError(f"{case['id']}: at least one assertion must be critical")

    # Each oracle section is checked against the generator that owns it: the
    # ledger keys against demo_finances, the foreign-source keys against the
    # fixture's own source definitions. Comparing the whole object to one
    # generator would read every key the other owns as drift.
    oracle = suite["fixture"]["oracle"]
    expected_ledger = _computed_oracle()
    actual_ledger = {key: oracle[key] for key in expected_ledger if key in oracle}
    if actual_ledger != expected_ledger:
        expected = json.dumps(expected_ledger, indent=2, ensure_ascii=False, sort_keys=True)
        actual = json.dumps(actual_ledger, indent=2, ensure_ascii=False, sort_keys=True)
        raise EvalValidationError(
            "Fixture oracle has drifted from backend/app/demo_finances.py. "
            f"Update the questions and oracle together.\nExpected:\n{expected}\nActual:\n{actual}"
        )

    expected_sources = _computed_source_oracle()
    actual_sources = {key: oracle[key] for key in expected_sources if key in oracle}
    if actual_sources != expected_sources:
        expected = json.dumps(expected_sources, indent=2, ensure_ascii=False, sort_keys=True)
        actual = json.dumps(actual_sources, indent=2, ensure_ascii=False, sort_keys=True)
        raise EvalValidationError(
            "Fixture oracle has drifted from evals/browser/fixture_sources.py. "
            f"Update the questions and oracle together.\nExpected:\n{expected}\nActual:\n{actual}"
        )

    unknown = set(oracle) - set(expected_ledger) - set(expected_sources)
    if unknown:
        raise EvalValidationError(
            "Oracle carries values no generator produces: " + ", ".join(sorted(unknown))
        )


def load_suite(path: Path = DEFAULT_SUITE) -> dict[str, Any]:
    suite = _read_yaml(path)
    validate_suite(suite)
    return suite


def fixture_status(suite: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    backend = REPO_ROOT / "backend"
    previous_cwd = Path.cwd()
    sys.path.insert(0, str(backend))
    try:
        os.chdir(backend)
        from sqlalchemy import func, select

        from app.database import SessionLocal
        from app.demo_finances import DEMO_EXPENSES, DEMO_INCOMES, DEMO_MARKER
        from app.models import Transaction, User
    except Exception as exc:
        raise EvalValidationError(f"Could not initialize the fixture database check: {type(exc).__name__}: {exc}") from exc
    finally:
        os.chdir(previous_cwd)
        sys.path.pop(0)

    identifier = suite["fixture"]["account"]["identifier"]
    expected_demo = len(DEMO_EXPENSES) * len(DEMO_INCOMES) + sum(len(items) for items in DEMO_INCOMES.values())
    try:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.phone == identifier))
            if user is None:
                return 1, {
                    "status": "missing",
                    "account": identifier,
                    "message": "Sign in once with the pinned local account, then run the seed command.",
                }
            total = db.scalar(
                select(func.count()).select_from(Transaction).where(
                    Transaction.user_id == user.id,
                    Transaction.deleted_at.is_(None),
                )
            ) or 0
            demo = db.scalar(
                select(func.count()).select_from(Transaction).where(
                    Transaction.user_id == user.id,
                    Transaction.deleted_at.is_(None),
                    Transaction.notes.like(f"{DEMO_MARKER}:%"),
                )
            ) or 0
            all_total = db.scalar(
                select(func.count()).select_from(Transaction).where(Transaction.user_id == user.id)
            ) or 0
            all_demo = db.scalar(
                select(func.count()).select_from(Transaction).where(
                    Transaction.user_id == user.id,
                    Transaction.notes.like(f"{DEMO_MARKER}:%"),
                )
            ) or 0
    except Exception as exc:
        raise EvalValidationError(f"Could not inspect the fixture account: {type(exc).__name__}: {exc}") from exc

    non_demo = total - demo
    details = {
        "account": identifier,
        "demoTransactions": demo,
        "expectedDemoTransactions": expected_demo,
        "nonDemoTransactions": non_demo,
        "archivedNonDemoTransactions": (all_total - all_demo) - non_demo,
        "currency": user.currency,
        "timezone": user.timezone,
    }
    if non_demo:
        return 1, {
            "status": "contaminated",
            **details,
            "message": "Use a clean dedicated account; the eval harness never deletes user-entered records.",
        }
    if demo != expected_demo:
        return 1, {
            "status": "not_seeded",
            **details,
            "message": suite["fixture"]["seedCommand"],
        }
    if user.currency != suite["fixture"]["oracle"]["currency"] or user.timezone != suite["defaults"]["timezone"]:
        return 1, {
            "status": "profile_mismatch",
            **details,
            "message": "The fixture account currency/timezone must match the suite oracle.",
        }
    return 0, {"status": "ready", **details}


def _run_metrics_payload(run: Any, message: Any, identifier: str) -> dict[str, Any]:
    trace = next(
        (
            widget.get("data") or {}
            for widget in (message.widgets if message else [])
            if widget.get("type") == "agent_activity"
        ),
        {},
    )
    stages = [
        str(step.get("stageId") or step.get("id") or "").strip()
        for step in trace.get("steps") or []
    ]
    route_signature = " > ".join(stage for stage in stages if stage)
    metrics = dict(run.metrics or {})
    coverage = float(metrics.get("costCoverage") or 0)
    cost = metrics.get("costUsd")
    cost_basis = (
        "provider_usage"
        if coverage == 1 and cost is not None
        else "partial_provider_usage"
        if 0 < coverage < 1
        else "unavailable"
    )
    return {
        "status": "ready",
        "account": identifier,
        "runId": str(run.id),
        "conversationId": str(run.conversation_id),
        "runStatus": run.status,
        "taskStatus": run.task_status,
        "createdAt": run.created_at.isoformat(),
        "timeToFirstResponseMs": run.time_to_first_response_ms,
        "totalDurationMs": run.duration_ms,
        "modelDurationMs": metrics.get("modelDurationMs"),
        "modelPassCount": metrics.get("modelPasses", 0),
        "inputTokens": metrics.get("inputTokens", 0),
        "outputTokens": metrics.get("outputTokens", 0),
        "totalTokens": metrics.get("totalTokens", 0),
        "costUsd": cost,
        "costCoverage": coverage,
        "costBasis": cost_basis,
        "routeSignature": route_signature,
    }


def latest_run_metrics(suite: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Read telemetry only for the fixture account's newest durable turn."""
    backend = REPO_ROOT / "backend"
    previous_cwd = Path.cwd()
    sys.path.insert(0, str(backend))
    try:
        os.chdir(backend)
        from sqlalchemy import select

        from app.database import SessionLocal
        from app.models import AgentRun, Message, User
    except Exception as exc:
        raise EvalValidationError(f"Could not initialize the run-metrics check: {type(exc).__name__}: {exc}") from exc
    finally:
        os.chdir(previous_cwd)
        sys.path.pop(0)

    identifier = suite["fixture"]["account"]["identifier"]
    try:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.phone == identifier))
            if user is None:
                return 1, {"status": "missing_account", "account": identifier}
            run = db.scalar(
                select(AgentRun)
                .where(AgentRun.user_id == user.id)
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .limit(1)
            )
            if run is None:
                return 1, {"status": "missing_run", "account": identifier}
            message = db.get(Message, run.final_message_id) if run.final_message_id else None
            payload = _run_metrics_payload(run, message, identifier)
    except Exception as exc:
        raise EvalValidationError(f"Could not inspect the latest fixture run: {type(exc).__name__}: {exc}") from exc
    return 0, payload


def capture_all_runs(report: dict[str, Any], suite: dict[str, Any]) -> int:
    """Map every fixture message run since report start to its prompt slot."""
    backend = REPO_ROOT / "backend"
    previous_cwd = Path.cwd()
    sys.path.insert(0, str(backend))
    try:
        os.chdir(backend)
        from sqlalchemy import select

        from app.database import SessionLocal
        from app.models import AgentRun, Message, User
    except Exception as exc:
        raise EvalValidationError(f"Could not initialize bulk run capture: {type(exc).__name__}: {exc}") from exc
    finally:
        os.chdir(previous_cwd)
        sys.path.pop(0)

    identifier = suite["fixture"]["account"]["identifier"]
    started_at = datetime.fromisoformat(report["run"]["startedAt"].replace("Z", "+00:00"))
    cases_by_id = {case["id"]: case for case in suite["cases"]}
    slots = []
    for result in report["results"]:
        prompt_steps = [
            step["text"]
            for step in cases_by_id[result["caseId"]]["steps"]
            if step["kind"] == "prompt"
        ]
        for attempt in result["measurements"]["attempts"]:
            slots.extend(
                (result, attempt, turn, expected_prompt)
                for turn, expected_prompt in zip(attempt["turns"], prompt_steps)
            )
    try:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.phone == identifier))
            if user is None:
                raise EvalValidationError("The fixture account is missing")
            raw_candidates = [
                run
                for run in db.scalars(
                    select(AgentRun)
                    .where(AgentRun.user_id == user.id, AgentRun.created_at >= started_at)
                    .order_by(AgentRun.created_at, AgentRun.id)
                )
                if (run.input_payload or {}).get("kind") == "message"
            ]
            # A shared development account can receive unrelated manual turns
            # while the browser suite is running. Select only exact suite
            # prompts, and when an interrupted browser automation retries the
            # same prompt keep the newest expected number of repetitions.
            expected_counts = Counter(expected for *_, expected in slots)
            candidates_by_prompt: dict[str, list[Any]] = {
                prompt: [] for prompt in expected_counts
            }
            for run in raw_candidates:
                prompt = str((run.input_payload or {}).get("text") or "")
                if prompt in candidates_by_prompt:
                    candidates_by_prompt[prompt].append(run)
            candidates = []
            missing: list[str] = []
            for prompt, expected_count in expected_counts.items():
                matched = candidates_by_prompt[prompt]
                if len(matched) < expected_count:
                    missing.append(f"{prompt!r}: expected {expected_count}, found {len(matched)}")
                    continue
                candidates.extend(matched[-expected_count:])
            candidates.sort(key=lambda run: (run.created_at, run.id))
            if missing:
                raise EvalValidationError("Missing suite prompt runs: " + "; ".join(missing))
            if len(candidates) != len(slots):
                raise EvalValidationError(
                    f"Expected {len(slots)} selected suite message runs since report start, found {len(candidates)}"
                )
            for (result, attempt, turn, expected_prompt), run in zip(slots, candidates):
                actual_prompt = str((run.input_payload or {}).get("text") or "")
                if actual_prompt != expected_prompt:
                    raise EvalValidationError(
                        f"{result['caseId']}/attempt-{attempt['attempt']}/step-{turn['step']}: "
                        f"expected prompt {expected_prompt!r}, found {actual_prompt!r}"
                    )
                message = db.get(Message, run.final_message_id) if run.final_message_id else None
                payload = _run_metrics_payload(run, message, identifier)
                if turn["runId"] and turn["runId"] != payload["runId"]:
                    raise EvalValidationError(
                        f"{result['caseId']}/attempt-{attempt['attempt']}/step-{turn['step']}: "
                        "existing run ID does not match chronological capture"
                    )
                for field in (
                    "runId", "timeToFirstResponseMs", "totalDurationMs", "modelDurationMs",
                    "modelPassCount", "inputTokens", "outputTokens", "totalTokens", "costUsd",
                    "costCoverage", "costBasis",
                ):
                    turn[field] = payload[field]
                route = payload["routeSignature"] or "no visible activity route"
                turn["notes"] = f"routeSignature: {route}"
            for result in report["results"]:
                for attempt in result["measurements"]["attempts"]:
                    attempt["routeSignature"] = " || ".join(
                        turn["notes"].removeprefix("routeSignature: ")
                        for turn in attempt["turns"]
                    )
    except EvalValidationError:
        raise
    except Exception as exc:
        raise EvalValidationError(f"Could not capture fixture runs: {type(exc).__name__}: {exc}") from exc
    return len(slots)


def _case_map(suite: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {case["id"]: case for case in suite["cases"]}


def _git_revision() -> str:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{revision}+dirty" if dirty else revision


def _empty_summary(suite: dict[str, Any]) -> dict[str, Any]:
    counts = Counter(case["tier"] for case in suite["cases"])
    return {
        "status": "pending",
        "passed": 0,
        "failed": 0,
        "blocked": 0,
        "pending": len(suite["cases"]),
        "score": 0,
        "byTier": {
            tier: {"passed": 0, "total": counts[tier], "score": 0}
            for tier in TIERS
        },
        "metrics": {
            "latency": {
                "turnsMeasured": 0,
                "medianTimeToFirstResponseMs": None,
                "p95TimeToFirstResponseMs": None,
                "medianTotalDurationMs": None,
                "p95TotalDurationMs": None,
            },
            "correctness": {
                "score": 0,
                "passedCases": 0,
                "totalCases": len(suite["cases"]),
            },
            "predictability": {
                "casesMeasured": 0,
                "meanOutcomeConsistency": None,
                "meanRouteConsistency": None,
                "meanLatencyCv": None,
            },
            "cost": {
                "turnsMeasured": 0,
                "modelPasses": 0,
                "inputTokens": None,
                "outputTokens": None,
                "totalTokens": None,
                "costUsd": None,
                "costCoverage": 0,
            },
        },
    }


def new_report(
    suite: dict[str, Any],
    *,
    base_url: str,
    evaluator: str,
    model: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    started = now or datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    return {
        "schemaVersion": "fyn.browser-eval-report.v2",
        "suiteVersion": suite["suiteVersion"],
        "run": {
            "id": f"browser-eval-{stamp}",
            "startedAt": started.isoformat().replace("+00:00", "Z"),
            "finishedAt": None,
            "baseUrl": base_url,
            "commit": _git_revision(),
            "evaluator": evaluator,
            "model": model,
            "notes": "",
        },
        "results": [
            {
                "caseId": case["id"],
                "status": "pending",
                "observedResponse": "",
                "measurements": {
                    "attempts": [
                        {
                            "attempt": attempt,
                            "observedOutcome": "",
                            "routeSignature": "",
                            "turns": [
                                {
                                    "step": index,
                                    "runId": "",
                                    "timeToFirstResponseMs": None,
                                    "totalDurationMs": None,
                                    "modelDurationMs": None,
                                    "modelPassCount": None,
                                    "inputTokens": None,
                                    "outputTokens": None,
                                    "totalTokens": None,
                                    "costUsd": None,
                                    "costCoverage": None,
                                    "costBasis": "unavailable",
                                    "notes": "",
                                }
                                for index, step in enumerate(case["steps"], 1)
                                if step["kind"] == "prompt"
                            ],
                            "notes": "",
                        }
                        for attempt in range(1, int(suite["defaults"]["repetitions"]) + 1)
                    ]
                },
                "assertions": [
                    {"id": assertion["id"], "passed": None, "evidence": ""}
                    for assertion in case["assertions"]
                ],
                "artifacts": [],
                "notes": "",
                "blockedReason": "",
            }
            for case in suite["cases"]
        ],
        "summary": _empty_summary(suite),
    }


def _derived_case(case: dict[str, Any], result: dict[str, Any], suite: dict[str, Any]) -> tuple[str, float]:
    assertion_defs = {assertion["id"]: assertion for assertion in case["assertions"]}
    if result["blockedReason"]:
        return "blocked", 0
    if any(item["passed"] is None for item in result["assertions"]):
        return "pending", 0

    earned = sum(
        float(assertion_defs[item["id"]]["weight"])
        for item in result["assertions"]
        if item["passed"] is True
    )
    critical_passed = all(
        item["passed"] is True
        for item in result["assertions"]
        if assertion_defs[item["id"]]["critical"]
    )
    threshold_passed = earned >= float(suite["defaults"]["passThreshold"])
    passed = threshold_passed and (critical_passed or not suite["defaults"]["requireAllCritical"])
    return ("pass" if passed else "fail"), earned


def _rounded_mean(values: list[float], digits: int = 4) -> float | None:
    return round(sum(values) / len(values), digits) if values else None


def _percentile_95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 1)


def _consistency(values: list[str]) -> float | None:
    normalized = [" ".join(value.casefold().split()) for value in values if value.strip()]
    if not normalized:
        return None
    return max(Counter(normalized).values()) / len(values)


def _derive_metrics(report: dict[str, Any], correctness_score: float, passed_cases: int) -> dict[str, Any]:
    turns = [
        turn
        for result in report["results"]
        for attempt in result["measurements"]["attempts"]
        for turn in attempt["turns"]
    ]
    response_latencies = [
        float(turn["timeToFirstResponseMs"])
        for turn in turns
        if turn["timeToFirstResponseMs"] is not None
    ]
    total_latencies = [
        float(turn["totalDurationMs"])
        for turn in turns
        if turn["totalDurationMs"] is not None
    ]

    outcome_consistency: list[float] = []
    route_consistency: list[float] = []
    latency_cvs: list[float] = []
    cases_measured = 0
    for result in report["results"]:
        attempts = result["measurements"]["attempts"]
        outcomes = [attempt["observedOutcome"] for attempt in attempts]
        routes = [attempt["routeSignature"] for attempt in attempts]
        scenario_durations: list[float] = []
        complete_durations = True
        for attempt in attempts:
            durations = [turn["totalDurationMs"] for turn in attempt["turns"]]
            if not durations or any(value is None for value in durations):
                complete_durations = False
                break
            scenario_durations.append(sum(float(value) for value in durations))
        outcome_value = _consistency(outcomes)
        route_value = _consistency(routes)
        if outcome_value is not None and route_value is not None and complete_durations:
            cases_measured += 1
            outcome_consistency.append(outcome_value)
            route_consistency.append(route_value)
            mean_duration = statistics.mean(scenario_durations)
            latency_cvs.append(
                statistics.pstdev(scenario_durations) / mean_duration
                if mean_duration > 0
                else 0
            )

    usage_turns = [turn for turn in turns if turn["modelPassCount"] is not None]
    all_tokens_available = bool(usage_turns) and all(
        all(turn[field] is not None for field in ("inputTokens", "outputTokens", "totalTokens"))
        for turn in usage_turns
    )
    model_passes = sum(int(turn["modelPassCount"] or 0) for turn in usage_turns)
    weighted_cost_coverage = (
        sum(
            int(turn["modelPassCount"] or 0) * float(turn["costCoverage"] or 0)
            for turn in usage_turns
        )
        / model_passes
        if model_passes
        else 0
    )
    exact_cost_available = bool(usage_turns) and all(
        turn["costBasis"] == "provider_usage" and turn["costUsd"] is not None
        for turn in usage_turns
    )
    return {
        "latency": {
            "turnsMeasured": len(total_latencies),
            "medianTimeToFirstResponseMs": (
                round(statistics.median(response_latencies), 1) if response_latencies else None
            ),
            "p95TimeToFirstResponseMs": _percentile_95(response_latencies),
            "medianTotalDurationMs": (
                round(statistics.median(total_latencies), 1) if total_latencies else None
            ),
            "p95TotalDurationMs": _percentile_95(total_latencies),
        },
        "correctness": {
            "score": correctness_score,
            "passedCases": passed_cases,
            "totalCases": len(report["results"]),
        },
        "predictability": {
            "casesMeasured": cases_measured,
            "meanOutcomeConsistency": _rounded_mean(outcome_consistency),
            "meanRouteConsistency": _rounded_mean(route_consistency),
            "meanLatencyCv": _rounded_mean(latency_cvs),
        },
        "cost": {
            "turnsMeasured": len(usage_turns),
            "modelPasses": model_passes,
            "inputTokens": (
                sum(int(turn["inputTokens"]) for turn in usage_turns)
                if all_tokens_available
                else None
            ),
            "outputTokens": (
                sum(int(turn["outputTokens"]) for turn in usage_turns)
                if all_tokens_available
                else None
            ),
            "totalTokens": (
                sum(int(turn["totalTokens"]) for turn in usage_turns)
                if all_tokens_available
                else None
            ),
            "costUsd": (
                round(sum(float(turn["costUsd"]) for turn in usage_turns), 10)
                if exact_cost_available
                else None
            ),
            "costCoverage": round(weighted_cost_coverage, 4),
        },
    }


def derive_summary(report: dict[str, Any], suite: dict[str, Any]) -> dict[str, Any]:
    cases = _case_map(suite)
    statuses: Counter[str] = Counter()
    tier_scores: defaultdict[str, float] = defaultdict(float)
    tier_passes: Counter[str] = Counter()
    tier_totals: Counter[str] = Counter()
    total_score = 0.0

    for result in report["results"]:
        case = cases[result["caseId"]]
        status, score = _derived_case(case, result, suite)
        result["status"] = status
        statuses[status] += 1
        tier = case["tier"]
        tier_totals[tier] += 1
        tier_scores[tier] += score
        total_score += score
        if status == "pass":
            tier_passes[tier] += 1

    if statuses["pending"]:
        overall_status = "pending"
    elif statuses["fail"]:
        overall_status = "fail"
    elif statuses["blocked"]:
        overall_status = "blocked"
    else:
        overall_status = "pass"

    count = len(report["results"])
    score = round(total_score / count, 2) if count else 0
    return {
        "status": overall_status,
        "passed": statuses["pass"],
        "failed": statuses["fail"],
        "blocked": statuses["blocked"],
        "pending": statuses["pending"],
        "score": score,
        "byTier": {
            tier: {
                "passed": tier_passes[tier],
                "total": tier_totals[tier],
                "score": round(tier_scores[tier] / tier_totals[tier], 2) if tier_totals[tier] else 0,
            }
            for tier in TIERS
        },
        "metrics": _derive_metrics(report, score, statuses["pass"]),
    }


def validate_report(report: dict[str, Any], suite: dict[str, Any], *, require_derived: bool = True) -> None:
    errors = _schema_errors(report, REPORT_SCHEMA)
    if errors:
        raise EvalValidationError("Report schema validation failed:\n- " + "\n- ".join(errors))
    if report["suiteVersion"] != suite["suiteVersion"]:
        raise EvalValidationError(
            f"Report suiteVersion {report['suiteVersion']} does not match suiteVersion {suite['suiteVersion']}"
        )

    cases = _case_map(suite)
    result_ids = [result["caseId"] for result in report["results"]]
    expected_ids = [case["id"] for case in suite["cases"]]
    if result_ids != expected_ids:
        raise EvalValidationError("Report cases must appear exactly once and in suite order")

    for result in report["results"]:
        case = cases[result["caseId"]]
        expected_assertions = [assertion["id"] for assertion in case["assertions"]]
        actual_assertions = [assertion["id"] for assertion in result["assertions"]]
        if actual_assertions != expected_assertions:
            raise EvalValidationError(f"{result['caseId']}: report assertions must match suite order")
        if require_derived and result["status"] == "blocked" and not result["blockedReason"]:
            raise EvalValidationError(f"{result['caseId']}: blocked status requires blockedReason")
        if require_derived and result["blockedReason"] and result["status"] != "blocked":
            raise EvalValidationError(f"{result['caseId']}: blockedReason requires blocked status")

        attempts = result["measurements"]["attempts"]
        repetitions = int(suite["defaults"]["repetitions"])
        if [attempt["attempt"] for attempt in attempts] != list(range(1, repetitions + 1)):
            raise EvalValidationError(
                f"{result['caseId']}: measurements must contain attempts 1 through {repetitions} in order"
            )
        prompt_steps = [
            index
            for index, step in enumerate(case["steps"], 1)
            if step["kind"] == "prompt"
        ]
        fully_evaluated = all(assertion["passed"] is not None for assertion in result["assertions"])
        for attempt in attempts:
            turns = attempt["turns"]
            if [turn["step"] for turn in turns] != prompt_steps:
                raise EvalValidationError(
                    f"{result['caseId']}/attempt-{attempt['attempt']}: turn steps must match prompt steps {prompt_steps}"
                )
            if fully_evaluated and not result["blockedReason"]:
                if not attempt["observedOutcome"].strip() or not attempt["routeSignature"].strip():
                    raise EvalValidationError(
                        f"{result['caseId']}/attempt-{attempt['attempt']}: evaluated cases require outcome and route"
                    )
            for turn in turns:
                if fully_evaluated and not result["blockedReason"]:
                    required_values = (
                        "runId",
                        "timeToFirstResponseMs",
                        "totalDurationMs",
                        "modelPassCount",
                        "inputTokens",
                        "outputTokens",
                        "totalTokens",
                        "costCoverage",
                    )
                    missing = [field for field in required_values if turn[field] in (None, "")]
                    if missing:
                        raise EvalValidationError(
                            f"{result['caseId']}/attempt-{attempt['attempt']}/step-{turn['step']}: "
                            f"evaluated turns require {', '.join(missing)}"
                        )
                basis = turn["costBasis"]
                coverage = turn["costCoverage"]
                cost = turn["costUsd"]
                if basis == "provider_usage" and (cost is None or coverage != 1):
                    raise EvalValidationError(
                        f"{result['caseId']}/attempt-{attempt['attempt']}/step-{turn['step']}: "
                        "provider_usage requires exact costUsd and costCoverage=1"
                    )
                if basis == "partial_provider_usage" and (
                    cost is not None or coverage is None or not 0 < coverage < 1
                ):
                    raise EvalValidationError(
                        f"{result['caseId']}/attempt-{attempt['attempt']}/step-{turn['step']}: "
                        "partial_provider_usage requires null costUsd and partial costCoverage"
                    )
                if basis == "unavailable" and (cost is not None or coverage not in (None, 0)):
                    raise EvalValidationError(
                        f"{result['caseId']}/attempt-{attempt['attempt']}/step-{turn['step']}: "
                        "unavailable cost requires null costUsd and zero coverage"
                    )
                if all(turn[field] is not None for field in ("inputTokens", "outputTokens", "totalTokens")):
                    if turn["totalTokens"] != turn["inputTokens"] + turn["outputTokens"]:
                        raise EvalValidationError(
                            f"{result['caseId']}/attempt-{attempt['attempt']}/step-{turn['step']}: "
                            "totalTokens must equal inputTokens + outputTokens"
                        )
        for assertion in result["assertions"]:
            if assertion["passed"] is not None and not assertion["evidence"].strip():
                raise EvalValidationError(
                    f"{result['caseId']}/{assertion['id']}: evaluated assertions require concise evidence"
                )

    if require_derived:
        copied = json.loads(json.dumps(report))
        expected_summary = derive_summary(copied, suite)
        expected_statuses = [result["status"] for result in copied["results"]]
        actual_statuses = [result["status"] for result in report["results"]]
        if actual_statuses != expected_statuses:
            raise EvalValidationError("One or more case statuses do not match their assertion scores")
        if report["summary"] != expected_summary:
            raise EvalValidationError("Report summary is stale; run the finalize command")


def _render_case(case: dict[str, Any]) -> str:
    lines = [
        f"# {case['id']} — {case['title']}",
        "",
        f"Tier: {case['tier']}",
        f"Expected outcome: {case['expectedOutcome']}",
        f"Objective: {case['objective']}",
        "",
        "Steps:",
    ]
    for index, step in enumerate(case["steps"], 1):
        value = step.get("text") or step.get("instruction")
        lines.append(f"{index}. [{step['kind']}] {value}")
    lines.extend(["", "Assertions:"])
    for assertion in case["assertions"]:
        critical = "critical" if assertion["critical"] else "non-critical"
        lines.append(f"- {assertion['id']} ({assertion['weight']:g}, {critical}): {assertion['description']}")
    return "\n".join(lines)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def capture_latest_run(
    report: dict[str, Any],
    suite: dict[str, Any],
    *,
    case_id: str,
    attempt_number: int,
    step_number: int,
) -> dict[str, Any]:
    status, payload = latest_run_metrics(suite)
    if status:
        raise EvalValidationError(str(payload.get("status") or "Latest run metrics are unavailable"))
    if payload["runStatus"] not in {"succeeded", "interrupted", "failed", "cancelled"}:
        raise EvalValidationError("The latest fixture run is not terminal yet")
    result = next((item for item in report["results"] if item["caseId"] == case_id), None)
    if result is None:
        raise EvalValidationError(f"Unknown report case: {case_id}")
    attempts = result["measurements"]["attempts"]
    attempt = next((item for item in attempts if item["attempt"] == attempt_number), None)
    if attempt is None:
        raise EvalValidationError(f"{case_id}: unknown attempt {attempt_number}")
    turn = next((item for item in attempt["turns"] if item["step"] == step_number), None)
    if turn is None:
        raise EvalValidationError(f"{case_id}/attempt-{attempt_number}: step {step_number} is not a prompt")
    run_id = payload["runId"]
    used = {
        item["runId"]
        for case_result in report["results"]
        for measured_attempt in case_result["measurements"]["attempts"]
        for item in measured_attempt["turns"]
        if item["runId"]
    }
    if run_id in used and turn["runId"] != run_id:
        raise EvalValidationError(f"Run {run_id} is already assigned to another report turn")
    for field in (
        "runId",
        "timeToFirstResponseMs",
        "totalDurationMs",
        "modelDurationMs",
        "modelPassCount",
        "inputTokens",
        "outputTokens",
        "totalTokens",
        "costUsd",
        "costCoverage",
        "costBasis",
    ):
        turn[field] = payload[field]
    route = str(payload.get("routeSignature") or "").strip() or "no visible activity route"
    turn["notes"] = f"routeSignature: {route}"
    attempt["routeSignature"] = " || ".join(
        item["notes"].removeprefix("routeSignature: ")
        for item in attempt["turns"]
        if item["runId"] and item["notes"].startswith("routeSignature: ")
    )
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and score Fyn Browser-tool evaluations")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-suite")
    subparsers.add_parser("fixture-status")
    subparsers.add_parser("latest-run-metrics")

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--tier", choices=TIERS)

    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("case_id")

    report_parser = subparsers.add_parser("new-report")
    report_parser.add_argument("output", type=Path)
    report_parser.add_argument("--base-url")
    report_parser.add_argument("--evaluator", default="browser-agent")
    report_parser.add_argument("--model", default=os.getenv("OPERATOR_MODEL", "unknown"))

    validate_parser = subparsers.add_parser("validate-report")
    validate_parser.add_argument("report", type=Path)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("report", type=Path)
    finalize_parser.add_argument("--in-place", action="store_true")

    capture_parser = subparsers.add_parser("capture-latest")
    capture_parser.add_argument("report", type=Path)
    capture_parser.add_argument("case_id")
    capture_parser.add_argument("attempt", type=int)
    capture_parser.add_argument("step", type=int)
    capture_all_parser = subparsers.add_parser("capture-all")
    capture_all_parser.add_argument("report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command in {"fixture-status", "latest-run-metrics"}:
            previous_cwd = Path.cwd()
            try:
                os.chdir(REPO_ROOT / "backend")
                suite = load_suite(args.suite)
            finally:
                os.chdir(previous_cwd)
        else:
            suite = load_suite(args.suite)
        if args.command == "validate-suite":
            counts = Counter(case["tier"] for case in suite["cases"])
            rendered = ", ".join(f"{tier}={counts[tier]}" for tier in TIERS)
            print(f"valid: {len(suite['cases'])} browser cases ({rendered})")
            return 0
        if args.command == "fixture-status":
            status, payload = fixture_status(suite)
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return status
        if args.command == "latest-run-metrics":
            status, payload = latest_run_metrics(suite)
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return status
        if args.command == "list":
            for case in suite["cases"]:
                if args.tier is None or case["tier"] == args.tier:
                    print(f"{case['id']}\t{case['title']}")
            return 0
        if args.command == "show":
            case = _case_map(suite).get(args.case_id)
            if case is None:
                raise EvalValidationError(f"Unknown case: {args.case_id}")
            print(_render_case(case))
            return 0
        if args.command == "new-report":
            base_url = args.base_url or suite["defaults"]["baseUrl"]
            report = new_report(suite, base_url=base_url, evaluator=args.evaluator, model=args.model)
            validate_report(report, suite)
            _write_json(args.output, report)
            print(f"created: {args.output}")
            return 0
        if args.command == "validate-report":
            report = _read_json(args.report)
            validate_report(report, suite)
            print(f"valid: {args.report} ({report['summary']['status']}, score={report['summary']['score']:g}/10)")
            return 0
        if args.command == "capture-latest":
            report = _read_json(args.report)
            validate_report(report, suite)
            payload = capture_latest_run(
                report,
                suite,
                case_id=args.case_id,
                attempt_number=args.attempt,
                step_number=args.step,
            )
            report["summary"] = derive_summary(report, suite)
            _write_json(args.report, report)
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        if args.command == "capture-all":
            report = _read_json(args.report)
            validate_report(report, suite)
            count = capture_all_runs(report, suite)
            report["summary"] = derive_summary(report, suite)
            _write_json(args.report, report)
            print(f"captured: {count} prompt runs")
            return 0
        if args.command == "finalize":
            report = _read_json(args.report)
            validate_report(report, suite, require_derived=False)
            report["summary"] = derive_summary(report, suite)
            if report["summary"]["pending"] == 0:
                report["run"]["finishedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            validate_report(report, suite)
            if args.in_place:
                _write_json(args.report, report)
                print(f"updated: {args.report}")
            else:
                print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report["summary"]["status"] == "pass" else 1
    except EvalValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
