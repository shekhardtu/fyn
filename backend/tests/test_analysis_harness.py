from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import AnalysisToolRun, AnalysisToolTemplate, Budget, Category, Transaction, User, UserAnalysisTool
from app.event_time import from_local_parts
from app.seed import default_user
from app.services import conversation as conversation_service
from app.services.agents import CopilotDecision
from app.services.analysis_harness import HarnessValidationError, ReplayDisposition, bind_repeat_analysis, discover_analysis_templates, execute_analysis_template
from app.services.intelligence import _presentation_labels, _semantic_message
from app.services.semantic import AnalysisPlan, AnalysisToolProposal, AnalysisTransform, FinanceFilter, FinanceQueryPlan


def occurred(day: date):
    return from_local_parts(day, None, "Asia/Kolkata")


def _proposal(today: date) -> AnalysisToolProposal:
    return AnalysisToolProposal(
        name="Food spending by category",
        description="Summarize recorded food expenses for the current month.",
        intent_signature="monthly food spending",
        plan=AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Filter recorded expenses to food", "Aggregate the validated period"],
            queries=[FinanceQueryPlan(
                name="Food spending this month",
                metric="gross_spend",
                filters=[FinanceFilter(field="category", value="food")],
                start_date=today.replace(day=1),
                end_date=today,
            )],
        ),
    )


def test_generated_tool_is_validated_saved_executed_and_reused(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add(Transaction(
        user_id=user.id,
        transaction_type="expense",
        amount_minor=20_000,
        currency="INR",
        merchant_name="Ice Cream Shop",
        category_id=food.id,
        transaction_at=occurred(date.today()),
    ))
    db.flush()
    proposal = _proposal(date.today())

    first = execute_analysis_template(db, user.id, uuid4(), date.today(), proposal)
    assert first.reused is False
    assert first.template.status == "active"
    assert first.template.validation_report["passed"] is True
    assert first.template.semantic_registry_version == "2026-08-12.2"
    assert len(first.template.source_manifest_hash) == 64
    assert first.result.message.endswith("₹200.")
    assert first.result.citations
    assert first.run.status == "completed"

    second = execute_analysis_template(db, user.id, uuid4(), date.today(), proposal)
    assert second.reused is True
    assert second.template.id == first.template.id
    assert second.template.success_count == 2
    assert len(list(db.scalars(select(AnalysisToolTemplate)))) == 1
    assert len(list(db.scalars(select(UserAnalysisTool)))) == 1
    assert len(list(db.scalars(select(AnalysisToolRun)))) == 2


def test_generated_tool_never_reads_another_users_transactions(db):
    user = default_user(db)
    other = User(email="other@example.com", display_name="Other")
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add(other)
    db.flush()
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", category_id=food.id, transaction_at=occurred(date.today())),
        Transaction(user_id=other.id, transaction_type="expense", amount_minor=99_900, currency="INR", category_id=food.id, transaction_at=occurred(date.today())),
    ])
    db.flush()

    generated = execute_analysis_template(db, user.id, uuid4(), date.today(), _proposal(date.today()))
    assert generated.result.query_results[0]["rows"] == [{"value": 10_000}]
    assert "₹999" not in generated.result.message


def test_template_is_shared_while_bindings_and_saved_tools_remain_user_scoped(db):
    user = default_user(db)
    first_proposal = _proposal(date(2026, 8, 16))
    first = execute_analysis_template(
        db, user.id, uuid4(), date(2026, 8, 16), first_proposal
    )

    second_proposal = _proposal(date(2026, 8, 15))
    second_proposal.plan.queries[0].filters[0].value = "transport"
    second = execute_analysis_template(
        db, user.id, uuid4(), date(2026, 8, 16), second_proposal
    )

    assert second.reused is True
    assert second.template.id == first.template.id
    serialized_template = str({
        "parameters": second.template.parameter_schema,
        "plan": second.template.plan_template,
    })
    assert "2026-08-16" not in serialized_template
    assert "2026-08-15" not in serialized_template
    assert "food" not in serialized_template
    assert "transport" not in serialized_template
    assert second.template.plan_template["queries"][0]["start_date"] == {
        "$parameter": "query_1.start_date"
    }
    binding_trace = next(
        item for item in second.run.trace if item["stage"] == "parameter_binding"
    )
    assert binding_trace["values"]["query_1.filter_1.value"] == "transport"
    assert binding_trace["values"]["query_1.end_date"] == "2026-08-15"
    assert second.run.parameters == binding_trace["values"]

    other = User(email="template-user@example.com", display_name="Template user")
    db.add(other)
    db.flush()
    other_run = execute_analysis_template(
        db, other.id, uuid4(), date(2026, 8, 16), second_proposal
    )
    assert other_run.template.id == second.template.id
    assert other_run.user_tool.id != second.user_tool.id
    assert other_run.user_tool.user_id == other.id
    assert other_run.run.user_id == other.id
    assert other_run.run.parameters == second.run.parameters


def test_governed_prorate_composes_with_difference_as_system_primitives(db):
    user = default_user(db)
    db.add_all([
        Transaction(
            user_id=user.id,
            transaction_type="income",
            amount_minor=1_000_000,
            currency="INR",
            merchant_name="Income source",
            transaction_at=occurred(date(2026, 8, 5)),
        ),
        Transaction(
            user_id=user.id,
            transaction_type="expense",
            amount_minor=440_000,
            currency="INR",
            merchant_name="Observed expenses",
            transaction_at=occurred(date(2026, 8, 16)),
        ),
    ])
    db.flush()
    proposal = AnalysisToolProposal(
        name="Projected remaining amount",
        description="Prorate an observed expense total and subtract it from recorded income.",
        intent_signature="prorate expenses and calculate projected savings",
        plan=AnalysisPlan(
            objective="scenario",
            analysis_type="semantic_query",
            safe_reasoning_summary=[
                "Read recorded income and expenses",
                "Project the expense run rate",
                "Subtract projected expenses from income",
            ],
            queries=[
                FinanceQueryPlan(
                    name="Recorded income",
                    metric="income",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 16),
                ),
                FinanceQueryPlan(
                    name="Observed expenses",
                    metric="gross_spend",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 16),
                ),
            ],
            transforms=[
                AnalysisTransform(
                    name="Prorated expenses",
                    operation="prorate",
                    query_name="Observed expenses",
                    target_start_date=date(2026, 8, 1),
                    target_end_date=date(2026, 8, 31),
                ),
                AnalysisTransform(
                    name="Projected savings",
                    operation="difference",
                    query_name="Recorded income",
                    secondary_transform_name="Prorated expenses",
                ),
            ],
        ),
    )

    generated = execute_analysis_template(
        db, user.id, uuid4(), date(2026, 8, 16), proposal
    )

    assert generated.result.message.startswith(
        "Projected savings: ₹1,475. Over 1–16 Aug 2026 (16 days), Observed expenses "
        "came to ₹4,400 — a pace that projects to ₹8,525 across all 31 days. "
        "Set against ₹10,000 of Recorded income for 1–16 Aug 2026 (actual, not projected), "
        "that leaves ₹1,475."
    )
    # The executed transform values render as grounded markdown, not widgets.
    assert generated.result.widgets == []
    assert "₹8,525" in generated.result.message
    assert "₹1,475" in generated.result.message
    stored = generated.template.plan_template["transforms"]
    assert stored[0]["target_end_date"] == {
        "$parameter": "transform_1.target_end_date"
    }


def test_custom_declarative_plan_cannot_be_discarded_by_a_domain_service_label(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add(Transaction(
        user_id=user.id,
        transaction_type="expense",
        amount_minor=20_000,
        currency="INR",
        category_id=food.id,
        transaction_at=occurred(date.today()),
    ))
    db.flush()
    proposal = _proposal(date.today())
    proposal.plan.analysis_type = "three_month_allocation"

    generated = execute_analysis_template(
        db, user.id, uuid4(), date.today(), proposal
    )

    assert generated.template.plan_template["analysis_type"] == "semantic_query"
    assert generated.result.query_results[0]["rows"] == [{"value": 20_000}]
    assert any(
        item["stage"] == "template_repair" and item["status"] == "completed"
        for item in generated.run.trace
    )


def test_incomplete_or_future_generated_tool_is_rejected(db):
    user = default_user(db)
    proposal = _proposal(date.today())
    proposal.plan.missing_information = ["the loan interest rate"]
    with pytest.raises(HarnessValidationError):
        execute_analysis_template(db, user.id, uuid4(), date.today(), proposal)
    assert db.scalar(select(AnalysisToolTemplate)) is None
    failed_run = db.scalar(select(AnalysisToolRun))
    assert failed_run.status == "failed"
    assert failed_run.error_code == "analysis_plan_rejected"

    future = _proposal(date.today())
    future.name = "Future spending"
    future.intent_signature = "future spending"
    future.plan.queries[0].start_date = date.today() + timedelta(days=1)
    future.plan.queries[0].end_date = date.today() + timedelta(days=7)
    with pytest.raises(HarnessValidationError):
        execute_analysis_template(db, user.id, uuid4(), date.today(), future)


def test_rejected_tool_trace_explains_the_exact_failed_check(db):
    user = default_user(db)
    proposal = _proposal(date.today())
    proposal.plan.queries = []
    events = []

    with pytest.raises(HarnessValidationError):
        execute_analysis_template(
            db,
            user.id,
            uuid4(),
            date.today(),
            proposal,
            callback=lambda stage, label, status, detail: events.append(
                {"stage": stage, "label": label, "status": status, "detail": detail}
            ),
        )

    failure = next(event for event in events if event["status"] == "failed")
    assert failure == {
        "stage": "template_validation",
        "label": "The analysis plan was rejected",
        "status": "failed",
        "detail": "Analysis plan rejected: The plan did not include a governed financial-data query.",
    }


def test_rejected_analysis_marks_the_task_failed_at_template_validation(db):
    user = default_user(db)
    conversation = conversation_service.get_or_create_conversation(db, user)
    proposal = _proposal(date.today())
    proposal.plan.queries = []
    decision = CopilotDecision(
        tool="run_analysis_harness",
        analysis_tool=proposal,
        confidence=0.99,
        reason="Run the generated analysis plan.",
    )

    response = conversation_service._analysis_harness_response(
        db,
        user,
        conversation,
        decision,
    )

    assert response.task_status == "failed"
    assert response.failure_stage == "template_validation"
    assert response.error_code == "analysis_plan_rejected"
    assert "execution trace" in response.message


def test_internal_planner_limitation_is_never_echoed_or_duplicated_to_customer(db):
    user = default_user(db)
    conversation = conversation_service.get_or_create_conversation(db, user)
    proposal = _proposal(date.today())
    proposal.plan.missing_information = [
        "The governed transform catalog has no prorating transform for this semantic plan."
    ]
    decision = CopilotDecision(
        tool="run_analysis_harness",
        analysis_tool=proposal,
        confidence=0.99,
        reason="Run the generated analysis plan.",
    )

    response = conversation_service._analysis_harness_response(
        db,
        user,
        conversation,
        decision,
    )

    assert response.task_status == "failed"
    assert response.failure_stage == "planning"
    assert response.error_code == "analysis_capability_unavailable"
    assert response.widgets == []
    assert "transform" not in response.message.casefold()
    assert "semantic" not in response.message.casefold()


def test_customer_resolvable_missing_input_is_asked_once_in_plain_language(db):
    user = default_user(db)
    conversation = conversation_service.get_or_create_conversation(db, user)
    proposal = _proposal(date.today())
    proposal.plan.missing_information = ["the annual interest rate"]
    decision = CopilotDecision(
        tool="run_analysis_harness",
        analysis_tool=proposal,
        confidence=0.99,
        reason="Run the generated analysis plan.",
    )

    response = conversation_service._analysis_harness_response(
        db,
        user,
        conversation,
        decision,
    )

    assert response.task_status == "needs_input"
    assert response.message == "Before I calculate this, please provide the annual interest rate."
    assert response.widgets == []


def test_template_discovery_returns_only_relevant_active_templates(db):
    user = default_user(db)
    generated = execute_analysis_template(db, user.id, uuid4(), date.today(), _proposal(date.today()))
    matches = discover_analysis_templates(db, user.id, "Show my monthly food spending")
    assert matches[0]["template_id"] == str(generated.template.id)
    assert discover_analysis_templates(db, user.id, "home loan amortization") == []


def test_template_without_current_semantic_registry_is_not_reused(db):
    user = default_user(db)
    generated = execute_analysis_template(db, user.id, uuid4(), date.today(), _proposal(date.today()))
    generated.template.source_manifest_hash = "obsolete"
    obsolete_id = generated.template.id
    db.flush()
    assert discover_analysis_templates(db, user.id, "monthly food spending") == []
    assert db.get(AnalysisToolTemplate, obsolete_id) is None
    # Under the template model a stored tool cannot execute without the
    # current request bound into a complete proposal, so the obsolete id is
    # unusable regardless of which precondition reports it first.
    with pytest.raises(HarnessValidationError, match="bound analysis tool proposal"):
        execute_analysis_template(db, user.id, uuid4(), date.today(), None, obsolete_id)


def test_generated_comparison_is_calculated_by_the_harness(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    transport = db.scalar(select(Category).where(Category.slug == "transport"))
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=30_000, currency="INR", category_id=food.id, transaction_at=occurred(date.today())),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", category_id=transport.id, transaction_at=occurred(date.today())),
    ])
    db.flush()
    proposal = AnalysisToolProposal(
        name="Compare food and transport",
        description="Compare total recorded food and transport expenses.",
        intent_signature="compare food transport spending",
        plan=AnalysisPlan(
            objective="diagnostic",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Aggregate both categories", "Compare totals deterministically"],
            queries=[FinanceQueryPlan(
                name="Category comparison",
                metric="gross_spend",
                dimensions=["category"],
                filters=[FinanceFilter(field="category", operator="in", value=["food", "transport"])],
                start_date=date.today().replace(day=1),
                end_date=date.today(),
            )],
            transforms=[AnalysisTransform(
                name="Food versus transport",
                operation="compare_totals",
                query_name="Category comparison",
                dimension="category",
            )],
        ),
    )
    generated = execute_analysis_template(db, user.id, uuid4(), date.today(), proposal)
    assert generated.result.message.startswith("Food is larger at ₹300, compared with ₹100 for Transport; the difference is ₹200.")
    # The comparison table renders as grounded markdown rows.
    assert "| Food | ₹300 |" in generated.result.message
    assert "| Transport | ₹100 |" in generated.result.message


def test_ranked_exclusion_preserves_limit_and_complete_query_lineage(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    transport = db.scalar(select(Category).where(Category.slug == "transport"))
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=30_000, currency="INR", category_id=food.id, transaction_at=occurred(date.today())),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=20_000, currency="INR", category_id=transport.id, transaction_at=occurred(date.today())),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", transaction_at=occurred(date.today())),
    ])
    db.flush()
    proposal = AnalysisToolProposal(
        name="Highest non-food category",
        description="Return the highest spending category other than Food this month.",
        intent_signature="highest category excluding food",
        plan=AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Exclude Food", "Return the highest remaining category"],
            queries=[FinanceQueryPlan(
                name="Highest non-food category",
                metric="gross_spend",
                dimensions=["category"],
                filters=[FinanceFilter(field="category", operator="neq", value="food")],
                start_date=date.today().replace(day=1),
                end_date=date.today(),
                order="desc",
                limit=1,
            )],
        ),
    )

    generated = execute_analysis_template(db, user.id, uuid4(), date.today(), proposal)

    rows = generated.result.query_results[0]["rows"]
    assert rows == [{"category": "Transport", "value": 20_000}]
    assert "| Transport | ₹200 |" in generated.result.message
    lineage = generated.result.citations[0].query
    assert lineage["dimensions"] == ["category"]
    assert lineage["filters"] == [{"field": "category", "operator": "neq", "value": "food"}]
    assert lineage["order"] == "desc"
    assert lineage["limit"] == 1
    assert lineage["registry_version"] == "2026-08-12.2"


def test_transaction_grain_results_render_as_grounded_markdown_rows(db):
    user = default_user(db)
    entertainment = db.scalar(select(Category).where(Category.slug == "entertainment"))
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", merchant_name="Toit", category_id=entertainment.id, transaction_at=occurred(date.today())),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=50_000, currency="INR", merchant_name="Cinema", category_id=entertainment.id, transaction_at=occurred(date.today())),
    ])
    db.flush()
    proposal = AnalysisToolProposal(
        name="Entertainment transaction amounts",
        description="List entertainment category transactions by their individual amounts.",
        intent_signature="entertainment transactions amount",
        plan=AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Filter Entertainment expenses", "List each transaction amount"],
            queries=[FinanceQueryPlan(
                name="Entertainment transactions",
                metric="gross_spend",
                dimensions=["transaction", "merchant", "transaction_date"],
                filters=[FinanceFilter(field="category", value="entertainment")],
                start_date=date.today().replace(day=1),
                end_date=date.today(),
                order="desc",
                limit=100,
            )],
        ),
    )

    generated = execute_analysis_template(db, user.id, uuid4(), date.today(), proposal)

    # Transaction-grain results render as the markdown table of the governed rows.
    assert generated.result.widgets == []
    rows = generated.result.query_results[0]["rows"]
    assert [row["value"] for row in rows] == [50_000, 10_000]
    assert {row["merchant"] for row in rows} == {"Cinema", "Toit"}
    assert "| Transaction | Merchant | Transaction date | Value |" in generated.result.message
    assert "Cinema" in generated.result.message


def test_incomplete_comparison_plan_is_repaired_before_template_creation(db):
    user = default_user(db)
    proposal = _proposal(date.today())
    proposal.name = "Compare spending categories"
    proposal.intent_signature = "compare monthly category spending"
    proposal.plan.queries[0].dimensions = ["category"]
    generated = execute_analysis_template(db, user.id, uuid4(), date.today(), proposal)
    assert generated.template.status == "active"
    assert generated.template.plan_template["transforms"][0]["operation"] == "compare_totals"
    templates = list(db.scalars(select(AnalysisToolTemplate)))
    assert [template.status for template in templates] == ["active"]


def test_presentation_text_does_not_change_template_identity(db):
    user = default_user(db)
    first = execute_analysis_template(db, user.id, uuid4(), date.today(), _proposal(date.today()))
    second_proposal = _proposal(date.today())
    second_proposal.name = "Food spending by category refined"
    second_proposal.description = "Summarize recorded food expenses with the refined current-month presentation."
    second = execute_analysis_template(db, user.id, uuid4(), date.today(), second_proposal)
    templates = list(db.scalars(select(AnalysisToolTemplate)))
    # Name and description are presentation, not identity: an identical
    # parameterized plan template is reused rather than re-versioned.
    assert len(templates) == 1
    assert templates[0].id == first.template.id == second.template.id
    assert second.reused is True


def test_cross_user_template_reuse_never_reuses_user_scope(db):
    user = default_user(db)
    generated = execute_analysis_template(db, user.id, uuid4(), date.today(), _proposal(date.today()))
    other = User(email="isolated@example.com", display_name="Isolated")
    db.add(other)
    db.flush()
    other_run = execute_analysis_template(
        db,
        other.id,
        uuid4(),
        date.today(),
        _proposal(date.today()),
        generated.template.id,
    )
    assert other_run.template.id == generated.template.id
    assert other_run.user_tool.user_id == other.id
    assert other_run.user_tool.id != generated.user_tool.id


def test_recommendation_requires_and_returns_user_planning_context(db):
    user = default_user(db)
    proposal = _proposal(date.today())
    proposal.name = "Recommend food allocation"
    proposal.intent_signature = "recommend food allocation"
    proposal.plan.objective = "recommendation"
    with pytest.raises(HarnessValidationError):
        execute_analysis_template(db, user.id, uuid4(), date.today(), proposal)

    proposal = _proposal(date.today())
    proposal.name = "Recommend food allocation with budget"
    proposal.intent_signature = "recommend food allocation using budget"
    proposal.plan.objective = "recommendation"
    proposal.plan.context_sources = ["budgets"]
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add(Budget(user_id=user.id, category_id=food.id, name="Food budget", amount_minor=100_000, currency="INR"))
    db.flush()
    generated = execute_analysis_template(db, user.id, uuid4(), date.today(), proposal)
    assert "**Budgets**" in generated.result.message
    assert "Food budget" in generated.result.message
    assert any(citation.entity_type == "budget" for citation in generated.result.citations)


def test_change_drivers_are_computed_from_two_periods(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    transport = db.scalar(select(Category).where(Category.slug == "transport"))
    previous = (date.today().replace(day=1) - timedelta(days=1)).replace(day=10)
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", category_id=food.id, transaction_at=occurred(previous)),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=5_000, currency="INR", category_id=transport.id, transaction_at=occurred(previous)),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=35_000, currency="INR", category_id=food.id, transaction_at=occurred(date.today())),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", category_id=transport.id, transaction_at=occurred(date.today())),
    ])
    db.flush()
    proposal = AnalysisToolProposal(
        name="Spending change drivers",
        description="Find category drivers of the month-over-month spending change.",
        intent_signature="monthly spending change drivers",
        plan=AnalysisPlan(
            objective="diagnostic",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Aggregate expenses by month and category", "Calculate category deltas"],
            queries=[FinanceQueryPlan(
                name="Monthly category spending",
                metric="gross_spend",
                dimensions=["month", "category"],
                start_date=previous.replace(day=1),
                end_date=date.today(),
            )],
            transforms=[AnalysisTransform(
                name="Category drivers",
                operation="change_drivers",
                query_name="Monthly category spending",
                dimension="category",
                period_dimension="month",
            )],
        ),
    )
    generated = execute_analysis_template(db, user.id, uuid4(), date.today(), proposal)
    assert generated.result.message.startswith("Food is the largest recorded increase")
    assert "| Food | ₹250 |" in generated.result.message


def test_tool_repair_corrects_relative_month_window_and_driver_axes(db):
    user = default_user(db)
    wrong_start = date.today().replace(day=1) - timedelta(days=100)
    wrong_end = date.today().replace(day=1) - timedelta(days=1)
    proposal = AnalysisToolProposal(
        name="Compare last three months change drivers",
        description="Compare food and transport over the last three months and find change drivers.",
        intent_signature="compare last three months change drivers",
        plan=AnalysisPlan(
            objective="diagnostic",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Compare categories", "Find change drivers"],
            queries=[FinanceQueryPlan(
                name="Monthly category spending",
                metric="gross_spend",
                dimensions=["month", "category"],
                start_date=wrong_start,
                end_date=wrong_end,
            )],
            transforms=[AnalysisTransform(
                name="Drivers",
                operation="change_drivers",
                query_name="Monthly category spending",
                dimension="month",
                period_dimension="category",
            )],
        ),
    )
    from app.services.analysis_harness import _repair_incomplete_analysis

    expected_start = date(date.today().year, date.today().month - 2, 1)
    repaired = _repair_incomplete_analysis(proposal, date.today())
    assert repaired is not None
    assert repaired.plan.queries[0].start_date == expected_start
    assert repaired.plan.queries[0].end_date == date.today()

    generated = execute_analysis_template(db, user.id, uuid4(), date.today(), proposal)
    template = generated.template.plan_template
    # Dates are runtime parameters in the stored template; the transform axes
    # are structural and stay literal.
    assert template["queries"][0]["start_date"] == {"$parameter": "query_1.start_date"}
    assert template["transforms"][0]["dimension"] == "category"
    assert template["transforms"][0]["period_dimension"] == "month"


def test_dashboard_narrative_summarizes_all_grounded_metrics_and_composition():
    shared = {"start": "2026-08-01", "end": "2026-08-11", "dimensions": ["time_bucket"], "currency": "INR"}
    message = _semantic_message(
        [
            {**shared, "name": "Income trend", "metric": "income", "rows": [{"time_bucket": "2026-08-01", "value": 300_000}]},
            {**shared, "name": "Spending trend", "metric": "gross_spend", "rows": [{"time_bucket": "2026-08-01", "value": 120_000}]},
            {**shared, "name": "Cash-flow trend", "metric": "net_cash_flow", "rows": [{"time_bucket": "2026-08-01", "value": 180_000}]},
            {**shared, "name": "Category spending", "metric": "gross_spend", "rows": [{"category": "Food", "value": 80_000}]},
        ],
        [{
            "operation": "share_of_total",
            "values": [{"label": "Food", "value": 80_000, "basis_points": 6_667}],
        }],
    )

    assert "recorded income is ₹3,000" in message
    assert "recorded spending is ₹1,200" in message
    assert "net cash flow is ₹1,800" in message
    assert "Food is the largest recorded share at 66.67% (₹800)" in message


def test_a_multi_period_narrative_never_dates_every_figure_to_one_window():
    """A comparison plan runs its queries over deliberately different windows.

    Printing one period in front of all of them would date figures to a window
    they were never computed over, which is a false statement about recorded
    money rather than a wording nit.
    """
    message = _semantic_message(
        [
            {
                "name": "This month", "metric": "gross_spend", "currency": "INR",
                "start": "2026-08-01", "end": "2026-08-19", "dimensions": ["category"],
                "rows": [{"category": "Housing", "value": 120_000}],
            },
            {
                "name": "Baseline income", "metric": "income", "currency": "INR",
                "start": "2026-05-01", "end": "2026-07-31", "dimensions": ["category"],
                "rows": [{"category": "Salary", "value": 900_000}],
            },
        ],
        [],
    )

    # Each figure is dated to the window it was actually computed over, and the
    # August window is never asserted over the May-July figure.
    assert message == (
        "Recorded spending is ₹1,200 for 1–19 Aug 2026 "
        "and recorded income is ₹9,000 for 1 May 2026–31 Jul 2026."
    )


def test_projection_narrative_humanizes_labels_and_explains_the_chain():
    # The production shape from run 92c7708b: planner-authored snake_case
    # names must never surface verbatim, and the prorate step that answers
    # "at my current pace" must be narrated, not hidden behind the final
    # difference.
    shared = {"currency": "INR", "start": "2026-08-01", "end": "2026-08-16", "dimensions": ["currency"]}
    message = _semantic_message(
        [
            {**shared, "name": "august_mtd_income", "metric": "income", "rows": [{"currency": "INR", "value": 75_150_000}]},
            {**shared, "name": "august_mtd_expenses", "metric": "gross_spend", "rows": [{"currency": "INR", "value": 6_378_600}]},
        ],
        [
            {
                "name": "projected_august_expenses",
                "operation": "prorate",
                "queryName": "august_mtd_expenses",
                "metric": "gross_spend",
                "sourceValue": 6_378_600,
                "sourceStartDate": "2026-08-01",
                "sourceEndDate": "2026-08-16",
                "sourceDays": 16,
                "targetStartDate": "2026-08-01",
                "targetEndDate": "2026-08-31",
                "targetDays": 31,
                "value": 12_358_538,
                "values": [{"label": "projected_august_expenses", "value": 12_358_538}],
            },
            {
                "name": "projected_august_savings",
                "operation": "difference",
                "queryName": "august_mtd_income",
                "secondaryQueryName": None,
                "secondaryTransformName": "projected_august_expenses",
                "metric": "income",
                "primaryValue": 75_150_000,
                "secondaryValue": 12_358_538,
                "value": 62_791_462,
                "ratioBasisPoints": None,
                "values": [],
            },
        ],
    )

    assert message.startswith("Projected August savings: ₹6,27,914.62.")
    assert "projected_august_savings" not in message
    assert "August month-to-date expenses came to ₹63,786" in message
    assert "projects to ₹1,23,585.38 across all 31 days" in message
    assert "₹7,51,500 of August month-to-date income for 1–16 Aug 2026 (actual, not projected)" in message


def _projection_proposal() -> AnalysisToolProposal:
    return AnalysisToolProposal(
        name="Projected remaining amount",
        description="Prorate an observed expense total and subtract it from recorded income.",
        intent_signature="prorate expenses and calculate projected savings",
        plan=AnalysisPlan(
            objective="scenario",
            analysis_type="semantic_query",
            safe_reasoning_summary=[
                "Read recorded income and expenses",
                "Project the expense run rate",
                "Subtract projected expenses from income",
            ],
            queries=[
                FinanceQueryPlan(name="Recorded income", metric="income", start_date=date(2026, 8, 1), end_date=date(2026, 8, 16)),
                FinanceQueryPlan(name="Observed expenses", metric="gross_spend", start_date=date(2026, 8, 1), end_date=date(2026, 8, 16)),
            ],
            transforms=[
                AnalysisTransform(name="Prorated expenses", operation="prorate", query_name="Observed expenses", target_start_date=date(2026, 8, 1), target_end_date=date(2026, 8, 31)),
                AnalysisTransform(name="Projected savings", operation="difference", query_name="Recorded income", secondary_transform_name="Prorated expenses"),
            ],
        ),
    )


def test_exact_repeat_question_replays_the_template_without_a_planner(db):
    user = default_user(db)
    db.add_all([
        Transaction(user_id=user.id, transaction_type="income", amount_minor=1_000_000, currency="INR", merchant_name="Income source", transaction_at=occurred(date(2026, 8, 5))),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=440_000, currency="INR", merchant_name="Observed expenses", transaction_at=occurred(date(2026, 8, 16))),
    ])
    db.flush()
    question = "At my current spending pace, how much can I save by the end of this month?"
    first = execute_analysis_template(
        db, user.id, uuid4(), date(2026, 8, 16), _projection_proposal(), question=question
    )
    assert first.run.question_hash is not None
    assert len(first.run.question_hash) == 64
    assert first.run.run_date == date(2026, 8, 16)
    assert first.run.display_names["query_1"] == "Recorded income"
    assert first.run.display_names["transform_2"] == "Projected savings"

    replay = bind_repeat_analysis(db, user.id, question, date(2026, 9, 3))

    assert replay is not None
    assert replay.disposition is ReplayDisposition.COMPOSE
    assert replay.template_id == first.template.id
    queries = {query.name: query for query in replay.proposal.plan.queries}
    assert set(queries) == {"Recorded income", "Observed expenses"}
    # Month-start and run-day anchors re-derive relative windows to the new day.
    assert queries["Recorded income"].start_date == date(2026, 9, 1)
    assert queries["Recorded income"].end_date == date(2026, 9, 3)
    prorate = next(item for item in replay.proposal.plan.transforms if item.operation == "prorate")
    assert prorate.name == "Prorated expenses"
    assert prorate.target_start_date == date(2026, 9, 1)
    assert prorate.target_end_date == date(2026, 9, 30)
    difference = next(item for item in replay.proposal.plan.transforms if item.operation == "difference")
    assert difference.secondary_transform_name == "Prorated expenses"

    executed = execute_analysis_template(
        db, user.id, uuid4(), date(2026, 9, 3), replay.proposal, replay.template_id, question=question
    )
    assert executed.reused is True
    assert executed.template.id == first.template.id


def test_replay_declines_novel_absolute_or_foreign_questions(db):
    user = default_user(db)
    question = "At my current spending pace, how much can I save by the end of this month?"
    # Nothing to replay before a successful run exists.
    assert bind_repeat_analysis(db, user.id, question, date(2026, 9, 3)) is None

    execute_analysis_template(db, user.id, uuid4(), date(2026, 8, 16), _projection_proposal(), question=question)
    # Absolute month or year wording pins its window; re-anchoring it to a
    # later day would silently answer a different question.
    assert bind_repeat_analysis(db, user.id, "How much did I save in August 2026?", date(2026, 9, 3)) is None
    # Another user's identical words never replay this user's run.
    other = User(email="replay-other@example.com", display_name="Replay other")
    db.add(other)
    db.flush()
    assert bind_repeat_analysis(db, other.id, question, date(2026, 9, 3)) is None
    # The original asker replays.
    assert bind_repeat_analysis(db, user.id, question, date(2026, 9, 3)) is not None
    prior = db.scalar(
        select(AnalysisToolRun).where(AnalysisToolRun.user_id == user.id).limit(1)
    )
    prior.parameters = {**prior.parameters, "query_1.start_date": "not-a-date"}
    db.flush()
    # Corrupt or incomplete persisted bindings fall back to planning instead of
    # turning a safe optimization into a failed customer request.
    assert bind_repeat_analysis(db, user.id, question, date(2026, 9, 3)) is None


def test_widget_payload_carries_display_labels_beside_contract_names():
    # The analysis widget shows plan-authored names; the human rendering must
    # ride along as displayLabel so the client never prints a slug, while the
    # contract names stay untouched for template matching. Dimension values
    # (categories, dates) are not names and must pass through unlabeled.
    results, transforms = _presentation_labels(
        [{"name": "august_mtd_income", "metric": "income", "rows": []}],
        [
            {
                "name": "projected_august_savings",
                "operation": "difference",
                "values": [
                    {"label": "august_mtd_income", "value": 1},
                    {"label": "Food", "value": 2},
                ],
            },
        ],
    )

    assert results[0]["name"] == "august_mtd_income"
    assert results[0]["displayLabel"] == "August month-to-date income"
    assert transforms[0]["displayLabel"] == "Projected August savings"
    assert transforms[0]["values"][0]["displayLabel"] == "August month-to-date income"
    assert "displayLabel" not in transforms[0]["values"][1]


def test_final_prorate_summary_states_its_basis_in_prose():
    message = _semantic_message(
        [{
            "name": "august_mtd_expenses", "metric": "gross_spend", "currency": "INR",
            "start": "2026-08-01", "end": "2026-08-16", "dimensions": ["currency"],
            "rows": [{"currency": "INR", "value": 6_378_600}],
        }],
        [{
            "name": "projected_august_expenses",
            "operation": "prorate",
            "queryName": "august_mtd_expenses",
            "metric": "gross_spend",
            "sourceValue": 6_378_600,
            "sourceStartDate": "2026-08-01",
            "sourceEndDate": "2026-08-16",
            "sourceDays": 16,
            "targetStartDate": "2026-08-01",
            "targetEndDate": "2026-08-31",
            "targetDays": 31,
            "value": 12_358_538,
            "values": [{"label": "projected_august_expenses", "value": 12_358_538}],
        }],
    )

    assert message == (
        "Projected August expenses: ₹1,23,585.38 — ₹63,786 recorded over "
        "1–16 Aug 2026 (16 days), projected across all 31 days at the same daily pace."
    )


def test_harness_refusal_codes_route_to_accurate_run_metadata():
    assert HarnessValidationError("x", code="rejected").failure_stage == "template_validation"
    assert HarnessValidationError("x", code="rejected").error_code == "analysis_plan_rejected"
    assert HarnessValidationError("x", code="unverifiable").failure_stage == "result_verification"
    assert HarnessValidationError("x", code="unverifiable").error_code == "analysis_result_unverifiable"
    assert HarnessValidationError("x", code="tool_not_available").failure_stage == "template_candidates"
    assert HarnessValidationError("x", code="no_proposal").error_code == "analysis_proposal_missing"
    # Unknown codes collapse to the rejected family instead of crashing routing.
    assert HarnessValidationError("x", code="mystery").code == "rejected"


def test_unverifiable_result_marks_the_task_failed_at_result_verification(db, monkeypatch):
    user = default_user(db)
    conversation = conversation_service.get_or_create_conversation(db, user)
    proposal = _proposal(date.today())
    decision = CopilotDecision(
        tool="run_analysis_harness",
        analysis_tool=proposal,
        confidence=0.9,
        reason="Run the generated analysis plan.",
    )

    def refuse(*args, **kwargs):
        raise HarnessValidationError("Generated tool returned an unverifiable result", code="unverifiable")

    monkeypatch.setattr(conversation_service, "execute_analysis_template", refuse)

    response = conversation_service._analysis_harness_response(db, user, conversation, decision)

    assert response.task_status == "failed"
    assert response.failure_stage == "result_verification"
    assert response.error_code == "analysis_result_unverifiable"


def test_time_bucket_dimension_repairs_to_the_month_dimension(db):
    from app.services.analysis_harness import _repair_incomplete_analysis

    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add(Transaction(
        user_id=user.id,
        transaction_type="expense",
        amount_minor=20_000,
        currency="INR",
        merchant_name="Grocery Mart",
        category_id=food.id,
        transaction_at=occurred(date.today()),
    ))
    db.flush()
    proposal = AnalysisToolProposal(
        name="Compare grocery spending: July vs August 2026",
        description="Compare grocery expense totals by month and currency.",
        intent_signature="grocery spending july vs august 2026",
        plan=AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Retrieve grocery totals", "Bucket by month"],
            queries=[FinanceQueryPlan(
                name="Grocery spending by month and currency",
                metric="gross_spend",
                dimensions=["time_bucket", "currency"],
                filters=[FinanceFilter(field="category", value="food")],
                start_date=date.today().replace(day=1),
                end_date=date.today(),
            )],
        ),
    )

    repaired = _repair_incomplete_analysis(proposal, date.today())
    assert repaired is not None
    assert repaired.plan.queries[0].dimensions == ["month", "currency"]

    # The full harness path: reject → deterministic repair → validated & runs.
    events = []
    generated = execute_analysis_template(
        db,
        user.id,
        uuid4(),
        date.today(),
        proposal,
        callback=lambda stage, label, status, detail: events.append((stage, status)),
    )
    assert generated.template.status == "active"
    assert ("template_repair", "completed") in events


def test_full_month_comparison_with_a_future_end_clamps_and_gains_a_transform(db):
    from calendar import monthrange

    from app.services.analysis_harness import _repair_incomplete_analysis
    from app.services.finance_time import shift_month
    from app.services.semantic import TimeGrouping

    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    today = date.today()
    previous_month_day = shift_month(today.replace(day=1), -1).replace(day=5)
    db.add_all([
        Transaction(
            user_id=user.id, transaction_type="expense", amount_minor=20_000,
            currency="INR", merchant_name="Grocery Mart", category_id=food.id,
            transaction_at=occurred(today),
        ),
        Transaction(
            user_id=user.id, transaction_type="expense", amount_minor=30_000,
            currency="INR", merchant_name="Grocery Mart", category_id=food.id,
            transaction_at=occurred(previous_month_day),
        ),
    ])
    db.flush()
    proposal = AnalysisToolProposal(
        name="Monthly grocery expense comparison",
        description="Compare canonical food expense spending by month, in chronological order.",
        intent_signature="compare total grocery expense spending by month chronological",
        plan=AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Retrieve monthly totals", "Compare the two months"],
            queries=[FinanceQueryPlan(
                name="Monthly grocery expense spending",
                metric="gross_spend",
                dimensions=["currency"],
                filters=[FinanceFilter(field="category", value="food")],
                start_date=previous_month_day.replace(day=1),
                # The full current month — a future end when asked mid-month.
                end_date=today.replace(day=monthrange(today.year, today.month)[1]),
                time_grouping=TimeGrouping(field="event_time", grain="month"),
                order="asc",
            )],
        ),
    )

    repaired = _repair_incomplete_analysis(proposal, today)
    assert repaired is not None
    assert repaired.plan.queries[0].end_date == today
    assert repaired.plan.transforms[0].operation == "compare_totals"
    assert repaired.plan.transforms[0].dimension == "time_bucket"

    events = []
    generated = execute_analysis_template(
        db,
        user.id,
        uuid4(),
        today,
        proposal,
        callback=lambda stage, label, status, detail: events.append((stage, status)),
    )
    assert generated.template.status == "active"
    assert ("template_repair", "completed") in events
