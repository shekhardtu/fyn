from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.event_time import from_local_parts
from app.models import Category, Transaction
from app.seed import default_user
from app.services.analysis_harness import _canonical_template, execute_analysis_template, validate_analysis_tool
from app.services.intelligence import monthly_comparison_data
from app.services.semantic import AnalysisPlan, AnalysisToolProposal, FinanceQueryPlan


TODAY = date(2026, 8, 17)


def occurred(day: date):
    return from_local_parts(day, None, "Asia/Kolkata")


def _dedicated_proposal(analysis_type: str, *, service_inputs: dict | None = None) -> AnalysisToolProposal:
    return AnalysisToolProposal(
        name=f"{analysis_type} check",
        description=f"Dedicated {analysis_type} domain service execution.",
        intent_signature=f"{analysis_type} dedicated service",
        plan=AnalysisPlan(
            objective="scenario" if analysis_type == "affordability" else "descriptive",
            analysis_type=analysis_type,
            service_inputs=service_inputs or {},
            safe_reasoning_summary=["Execute the deterministic domain service"],
        ),
    )


def test_recurring_expenses_runs_as_a_dedicated_type_through_the_harness(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    for day in (date(2026, 5, 10), date(2026, 6, 10), date(2026, 7, 10)):
        db.add(Transaction(
            user_id=user.id,
            transaction_type="expense",
            amount_minor=64_900,
            currency="INR",
            merchant_name="Netflix",
            category_id=food.id,
            transaction_at=occurred(day),
        ))
    db.flush()

    outcome = execute_analysis_template(
        db, user.id, uuid4(), TODAY, _dedicated_proposal("recurring_expenses")
    )

    assert outcome.run.status == "completed"
    assert "1 recurring expense pattern" in outcome.result.message
    assert outcome.result.widgets == []
    assert "| Netflix | monthly | 3 |" in outcome.result.message
    assert "₹649" in outcome.result.message
    assert outcome.result.citations


def test_affordability_reads_purchase_minor_from_service_inputs(db):
    user = default_user(db)
    db.add_all([
        Transaction(user_id=user.id, transaction_type="income", amount_minor=10_000_000, currency="INR", transaction_at=occurred(date(2026, 7, 1))),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=1_000_000, currency="INR", transaction_at=occurred(date(2026, 8, 5))),
    ])
    db.flush()

    outcome = execute_analysis_template(
        db, user.id, uuid4(), TODAY,
        _dedicated_proposal("affordability", service_inputs={"purchase_minor": 500_000}),
    )

    assert outcome.run.status == "completed"
    assert outcome.result.widgets == []
    assert "Can I afford ₹5,000?" in outcome.result.message
    assert "| Purchase | ₹5,000 |" in outcome.result.message
    assert "affordable" in outcome.result.message
    # The bound purchase amount is a template parameter, never template content.
    assert {"name": "service_inputs.purchase_minor", "type": "integer", "required": True} in outcome.template.parameter_schema
    assert outcome.run.parameters["service_inputs.purchase_minor"] == 500_000


def test_affordability_without_a_purchase_amount_is_rejected_statically():
    report = validate_analysis_tool(_dedicated_proposal("affordability"), TODAY)
    failing = {check["name"] for check in report["checks"] if not check["passed"]}
    assert report["passed"] is False
    assert failing == {"service_inputs"}


def test_service_inputs_are_rejected_on_semantic_query_plans():
    with pytest.raises(ValueError, match="dedicated analysis types"):
        AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            service_inputs={"purchase_minor": 1},
            safe_reasoning_summary=["x"],
            queries=[FinanceQueryPlan(name="q", metric="gross_spend", start_date=TODAY.replace(day=1), end_date=TODAY)],
        )


def test_semantic_query_templates_keep_their_hash_without_service_inputs():
    proposal = AnalysisToolProposal(
        name="Food spending",
        description="Summarize recorded food expenses.",
        intent_signature="monthly food spending",
        plan=AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Aggregate the validated period"],
            queries=[FinanceQueryPlan(name="Food", metric="gross_spend", start_date=TODAY.replace(day=1), end_date=TODAY)],
        ),
    )
    specification, bindings = _canonical_template(proposal)

    # Emitting an empty service_inputs key would silently change every stored
    # template hash and break reuse and replay for existing templates.
    assert "service_inputs" not in specification["planTemplate"]
    assert not [item for item in specification["parameterSchema"] if item["name"].startswith("service_inputs.")]
    assert not [name for name in bindings if name.startswith("service_inputs.")]


def test_monthly_comparison_compares_equal_elapsed_days_across_month_lengths(db):
    user = default_user(db)

    result = monthly_comparison_data(db, user.id, date(2026, 3, 31))

    assert result["current"] == {**result["current"], "start": "2026-03-01", "end": "2026-03-31"}
    # February has no day 31: the elapsed-day clamp lands on its final day.
    assert result["previous"]["start"] == "2026-02-01"
    assert result["previous"]["end"] == "2026-02-28"


def test_monthly_comparison_runs_as_a_dedicated_type_through_the_harness(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=30_000, currency="INR", category_id=food.id, transaction_at=occurred(date(2026, 8, 5))),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", category_id=food.id, transaction_at=occurred(date(2026, 7, 5))),
    ])
    db.flush()

    outcome = execute_analysis_template(
        db, user.id, uuid4(), TODAY, _dedicated_proposal("monthly_comparison")
    )

    assert outcome.run.status == "completed"
    assert "higher" in outcome.result.message
    assert outcome.result.widgets == []
    assert "| Previous period |" in outcome.result.message and "₹100 | 1 |" in outcome.result.message
    assert "| Current period |" in outcome.result.message and "₹300 | 1 |" in outcome.result.message
    assert outcome.result.citations
