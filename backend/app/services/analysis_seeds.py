"""Curated seed templates for the shared analysis-template pool.

Seeds are ordinary templates with a curated origin: each is built as a full
``AnalysisToolProposal``, validated by the same static policy checks as a
Planner-authored plan, canonicalized by ``_canonical_template`` (so every date,
limit, and service input becomes a ``$parameter``), and stored by its
structural fingerprint. Retrieval, binding, replay, and execution treat them
exactly like organically created templates — the curated part is only the
retrieval text.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain import AnalysisToolStatus
from ..models import AnalysisToolTemplate
from .analysis_harness import (
    _canonical_template,
    _specification_hash,
    validate_analysis_tool,
)
from .finance_time import shift_month
from .semantic import (
    AnalysisPlan,
    AnalysisToolProposal,
    AnalysisTransform,
    FinanceQueryPlan,
)


@dataclass(frozen=True)
class AnalysisSeed:
    capability_name: str
    capability_description: str
    capability_signature: str
    build: Callable[[date], AnalysisToolProposal]


def _proposal(seed_name: str, description: str, intent: str, plan: AnalysisPlan) -> AnalysisToolProposal:
    return AnalysisToolProposal(
        name=seed_name,
        description=description,
        intent_signature=intent,
        plan=plan,
    )


def _spending_summary(today: date) -> AnalysisToolProposal:
    start = today.replace(day=1)
    total = FinanceQueryPlan(
        name="Total spend",
        metric="gross_spend",
        start_date=start,
        end_date=today,
        limit=1,
    )
    by_category = FinanceQueryPlan(
        name="Spend by category",
        metric="gross_spend",
        dimensions=["category"],
        start_date=start,
        end_date=today,
        limit=50,
    )
    plan = AnalysisPlan(
        objective="descriptive",
        analysis_type="semantic_query",
        queries=[total, by_category],
        safe_reasoning_summary=[
            "Aggregate canonical expenses for the requested period",
            "Group the same period by category",
            "Report only recorded totals",
        ],
    )
    return _proposal(
        "Spending summary with category breakdown",
        "Total recorded spending for a period alongside the same period grouped by category.",
        "spending summary total by category for period",
        plan,
    )


def _monthly_comparison(today: date) -> AnalysisToolProposal:
    plan = AnalysisPlan(
        objective="descriptive",
        analysis_type="monthly_comparison",
        safe_reasoning_summary=[
            "Compare month-to-date spending with the same elapsed days last month",
            "Use the equal-elapsed-days product policy",
            "Report only recorded totals",
        ],
    )
    return _proposal(
        "Month-to-date spending versus last month",
        "Month-to-date expense total against the same elapsed days of the previous month, using the equal-elapsed-days policy.",
        "monthly spending versus last month same point",
        plan,
    )


def _recurring_expenses(today: date) -> AnalysisToolProposal:
    plan = AnalysisPlan(
        objective="descriptive",
        analysis_type="recurring_expenses",
        safe_reasoning_summary=[
            "Group repeated merchant charges by normalized merchant and amount",
            "Detect weekly and monthly cadences deterministically",
            "Report only observed patterns",
        ],
    )
    return _proposal(
        "Recurring expense patterns",
        "Detected recurring expenses and subscriptions: merchant, typical amount, cadence, and last occurrence.",
        "recurring expenses subscriptions detected patterns",
        plan,
    )


def _affordability(today: date) -> AnalysisToolProposal:
    plan = AnalysisPlan(
        objective="scenario",
        analysis_type="affordability",
        service_inputs={"purchase_minor": 1_000_000},
        safe_reasoning_summary=[
            "Load recorded income, expenses, and net cash position",
            "Apply the six-month emergency reserve rule deterministically",
            "Report the affordability verdict with its gap and timeline",
        ],
    )
    return _proposal(
        "Affordability check for a purchase",
        "Whether a purchase amount is affordable from recorded cash position while preserving a six-month expense reserve.",
        "can i afford purchase amount affordability",
        plan,
    )


def _change_drivers(today: date) -> AnalysisToolProposal:
    start = shift_month(today.replace(day=1), -1)
    query = FinanceQueryPlan(
        name="Monthly category spending drivers",
        metric="gross_spend",
        dimensions=["month", "category"],
        start_date=start,
        end_date=today,
        limit=100,
    )
    plan = AnalysisPlan(
        objective="diagnostic",
        analysis_type="semantic_query",
        queries=[query],
        transforms=[AnalysisTransform(
            name="Category change drivers",
            operation="change_drivers",
            query_name=query.name,
            dimension="category",
            period_dimension="month",
        )],
        safe_reasoning_summary=[
            "Compare this month with the prior month",
            "Calculate category-level changes",
            "Report only recorded drivers",
        ],
    )
    return _proposal(
        "Monthly spending change drivers",
        "Which categories drove the change between this month and last month.",
        "why spending changed category change drivers",
        plan,
    )


SEED_SPECS: tuple[AnalysisSeed, ...] = (
    AnalysisSeed(
        "Spending summary with category breakdown",
        "Total recorded spending for a period alongside the same period grouped by category. Answers phrasings like: how much did I spend, spend summary, spending breakdown by category.",
        "objective descriptive | analysis semantic_query | metric gross_spend | dimension category | spend summary breakdown",
        _spending_summary,
    ),
    AnalysisSeed(
        "Month-to-date spending versus last month",
        "Month-to-date expense total against the same elapsed days of the previous month. Answers phrasings like: compare this month with last month, am I spending more than last month.",
        "objective descriptive | analysis monthly_comparison | compare month spending versus previous",
        _monthly_comparison,
    ),
    AnalysisSeed(
        "Recurring expense patterns",
        "Detected recurring expenses and subscriptions with merchant, typical amount, cadence, and last occurrence. Answers phrasings like: recurring expenses, my subscriptions, repeated charges.",
        "objective descriptive | analysis recurring_expenses | recurring subscription repeated charges",
        _recurring_expenses,
    ),
    AnalysisSeed(
        "Affordability check for a purchase",
        "Whether a purchase amount is affordable from recorded cash position while preserving a six-month expense reserve. Answers phrasings like: can I afford, is it safe to buy.",
        "objective scenario | analysis affordability | afford purchase reserve",
        _affordability,
    ),
    AnalysisSeed(
        "Monthly spending change drivers",
        "Which categories drove the change between this month and last month. Answers phrasings like: why did I spend more, what changed in my spending.",
        "objective diagnostic | analysis semantic_query | metric gross_spend | transform change_drivers | why spending changed",
        _change_drivers,
    ),
)


def seed_analysis_templates(db: Session, *, today: date | None = None) -> int:
    """Idempotently install the curated seed templates; returns rows created.

    A seed whose structure already exists (organically or from a prior seed
    run) is not duplicated — its curated retrieval text is re-asserted so the
    best description always wins the pool.
    """
    created = 0
    anchor = today or date.today()
    for seed in SEED_SPECS:
        proposal = seed.build(anchor)
        report = validate_analysis_tool(proposal, anchor)
        if not report["passed"]:
            failed = [check["name"] for check in report["checks"] if not check["passed"]]
            raise RuntimeError(f"Seed template {seed.capability_name!r} failed validation: {failed}")
        specification, _bindings = _canonical_template(proposal)
        fingerprint = _specification_hash(specification)
        template = db.scalar(
            select(AnalysisToolTemplate).where(AnalysisToolTemplate.template_hash == fingerprint)
        )
        if template is None:
            manifest_identity = specification["sourceManifest"]
            template = AnalysisToolTemplate(
                capability_name=seed.capability_name,
                capability_description=seed.capability_description,
                capability_signature=seed.capability_signature,
                template_version=specification["templateVersion"],
                status=AnalysisToolStatus.ACTIVE,
                semantic_registry_version=manifest_identity["semanticVersion"],
                source_manifest_hash=manifest_identity["hash"],
                parameter_schema=specification["parameterSchema"],
                plan_template=specification["planTemplate"],
                template_hash=fingerprint,
                validation_report={
                    "passed": True,
                    "checks": [
                        {"name": check["name"], "passed": check["passed"]}
                        for check in report["checks"]
                    ],
                },
                created_by_user_id=None,
            )
            db.add(template)
            created += 1
        else:
            template.capability_name = seed.capability_name
            template.capability_description = seed.capability_description
            template.capability_signature = seed.capability_signature
            template.status = AnalysisToolStatus.ACTIVE
    db.commit()
    return created
