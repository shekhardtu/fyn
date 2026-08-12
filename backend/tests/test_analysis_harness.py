from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import AnalysisTool, AnalysisToolRun, Budget, Category, Transaction, User
from app.seed import default_user
from app.services.analysis_harness import HarnessValidationError, discover_analysis_tools, execute_generated_tool
from app.services.intelligence import _semantic_message
from app.services.semantic import AnalysisPlan, AnalysisToolProposal, AnalysisTransform, FinanceFilter, FinanceQueryPlan, VisualEncoding, VisualEncodingSet, VisualizationSpec


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
        transaction_date=date.today(),
    ))
    db.flush()
    proposal = _proposal(date.today())

    first = execute_generated_tool(db, user.id, uuid4(), date.today(), proposal)
    assert first.reused is False
    assert first.tool.status == "active"
    assert first.tool.validation_report["passed"] is True
    assert first.tool.specification["semanticRegistry"]["version"] == "2026-08-12.2"
    assert len(first.tool.specification["semanticRegistry"]["schemaHash"]) == 64
    assert first.result.message.endswith("₹200.")
    assert first.result.citations
    assert first.run.status == "completed"

    second = execute_generated_tool(db, user.id, uuid4(), date.today(), proposal)
    assert second.reused is True
    assert second.tool.id == first.tool.id
    assert second.tool.success_count == 2
    assert len(list(db.scalars(select(AnalysisTool)))) == 1
    assert len(list(db.scalars(select(AnalysisToolRun)))) == 2


def test_generated_tool_never_reads_another_users_transactions(db):
    user = default_user(db)
    other = User(email="other@example.com", display_name="Other")
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add(other)
    db.flush()
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", category_id=food.id, transaction_date=date.today()),
        Transaction(user_id=other.id, transaction_type="expense", amount_minor=99_900, currency="INR", category_id=food.id, transaction_date=date.today()),
    ])
    db.flush()

    generated = execute_generated_tool(db, user.id, uuid4(), date.today(), _proposal(date.today()))
    rows = generated.result.widgets[0].data["queryResults"][0]["rows"]
    assert rows == [{"value": 10_000}]


def test_incomplete_or_future_generated_tool_is_rejected(db):
    user = default_user(db)
    proposal = _proposal(date.today())
    proposal.plan.missing_information = ["the loan interest rate"]
    with pytest.raises(HarnessValidationError):
        execute_generated_tool(db, user.id, uuid4(), date.today(), proposal)
    assert db.scalar(select(AnalysisTool)) is None

    future = _proposal(date.today())
    future.name = "Future spending"
    future.intent_signature = "future spending"
    future.plan.queries[0].start_date = date.today() + timedelta(days=1)
    future.plan.queries[0].end_date = date.today() + timedelta(days=7)
    with pytest.raises(HarnessValidationError):
        execute_generated_tool(db, user.id, uuid4(), date.today(), future)


def test_tool_discovery_returns_only_relevant_active_tools(db):
    user = default_user(db)
    generated = execute_generated_tool(db, user.id, uuid4(), date.today(), _proposal(date.today()))
    matches = discover_analysis_tools(db, user.id, "Show my monthly food spending")
    assert matches[0]["id"] == str(generated.tool.id)
    assert discover_analysis_tools(db, user.id, "home loan amortization") == []


def test_tool_without_current_semantic_registry_is_not_reused(db):
    user = default_user(db)
    generated = execute_generated_tool(db, user.id, uuid4(), date.today(), _proposal(date.today()))
    generated.tool.specification = {
        key: value for key, value in generated.tool.specification.items() if key != "semanticRegistry"
    }
    obsolete_id = generated.tool.id
    db.flush()
    assert discover_analysis_tools(db, user.id, "monthly food spending") == []
    assert db.get(AnalysisTool, obsolete_id) is None
    with pytest.raises(HarnessValidationError, match="not available"):
        execute_generated_tool(db, user.id, uuid4(), date.today(), None, obsolete_id)


def test_generated_comparison_is_calculated_by_the_harness(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    transport = db.scalar(select(Category).where(Category.slug == "transport"))
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=30_000, currency="INR", category_id=food.id, transaction_date=date.today()),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", category_id=transport.id, transaction_date=date.today()),
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
    generated = execute_generated_tool(db, user.id, uuid4(), date.today(), proposal)
    assert generated.result.message == "Food is larger at ₹300, compared with ₹100 for Transport; the difference is ₹200."
    transform = generated.result.widgets[0].data["transforms"][0]
    assert transform["leader"] == "Food"
    assert transform["difference"] == 20_000


def test_ranked_exclusion_preserves_limit_and_complete_query_lineage(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    transport = db.scalar(select(Category).where(Category.slug == "transport"))
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=30_000, currency="INR", category_id=food.id, transaction_date=date.today()),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=20_000, currency="INR", category_id=transport.id, transaction_date=date.today()),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", transaction_date=date.today()),
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

    generated = execute_generated_tool(db, user.id, uuid4(), date.today(), proposal)

    rows = generated.result.widgets[0].data["queryResults"][0]["rows"]
    assert rows == [{"category": "Transport", "value": 20_000}]
    lineage = generated.result.citations[0].query
    assert lineage["dimensions"] == ["category"]
    assert lineage["filters"] == [{"field": "category", "operator": "neq", "value": "food"}]
    assert lineage["order"] == "desc"
    assert lineage["limit"] == 1
    assert lineage["registry_version"] == "2026-08-12.2"


def test_transaction_amount_graph_uses_generic_validated_chart_protocol(db):
    user = default_user(db)
    entertainment = db.scalar(select(Category).where(Category.slug == "entertainment"))
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", merchant_name="Toit", category_id=entertainment.id, transaction_date=date.today()),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=50_000, currency="INR", merchant_name="Cinema", category_id=entertainment.id, transaction_date=date.today()),
    ])
    db.flush()
    proposal = AnalysisToolProposal(
        name="Entertainment transaction amount graph",
        description="Draw a graph of entertainment category transactions by their individual amounts.",
        intent_signature="graph entertainment transactions amount",
        plan=AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Filter Entertainment expenses", "Plot each transaction amount"],
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
            visualizations=[VisualizationSpec(
                name="Entertainment amounts",
                query_name="Entertainment transactions",
                mark="bar",
                encoding=VisualEncodingSet(
                    x=VisualEncoding(field="transaction", type="nominal", title="Transaction"),
                    y=VisualEncoding(field="value", type="quantitative", title="Amount", value_type="money_minor"),
                    tooltip=[VisualEncoding(field="merchant", type="nominal"), VisualEncoding(field="transaction_date", type="temporal")],
                ),
                title="Entertainment transactions by amount",
                rationale="A bar chart makes individual transaction amounts easy to compare.",
            )],
        ),
    )

    generated = execute_generated_tool(db, user.id, uuid4(), date.today(), proposal)

    assert generated.result.widgets[0].type == "data_visualization"
    visualization = generated.result.widgets[0].data
    view = visualization["views"][0]
    rows = visualization["datasets"][view["dataset"]]
    assert view["mark"] == "bar"
    assert view["encoding"]["x"]["field"] == "transaction"
    assert [item["field"] for item in view["encoding"]["tooltip"]] == ["merchant", "transaction_date"]
    assert [row["value"] for row in rows] == [50_000, 10_000]
    assert {row["merchant"] for row in rows} == {"Cinema", "Toit"}
    assert visualization["queryResults"][view["dataset"]]["name"] == "Entertainment transactions"
    verification = generated.tool.validation_report["result_verification"]
    assert next(check for check in verification["checks"] if check["name"] == "visualization_contract")["passed"] is True


def test_incomplete_comparison_tool_is_repaired_and_versioned(db):
    user = default_user(db)
    proposal = _proposal(date.today())
    proposal.name = "Compare spending categories"
    proposal.intent_signature = "compare monthly category spending"
    proposal.plan.queries[0].dimensions = ["category"]
    generated = execute_generated_tool(db, user.id, uuid4(), date.today(), proposal)
    assert generated.tool.version == 1
    assert generated.tool.status == "active"
    assert generated.tool.specification["plan"]["transforms"][0]["operation"] == "compare_totals"
    tools = list(db.scalars(select(AnalysisTool).order_by(AnalysisTool.version)))
    assert [tool.status for tool in tools] == ["active"]


def test_new_tool_for_same_intent_replaces_obsolete_variant(db):
    user = default_user(db)
    first = execute_generated_tool(db, user.id, uuid4(), date.today(), _proposal(date.today()))
    second_proposal = _proposal(date.today())
    second_proposal.name = "Food spending by category refined"
    second_proposal.description = "Summarize recorded food expenses with the refined current-month presentation."
    second = execute_generated_tool(db, user.id, uuid4(), date.today(), second_proposal)
    tools = list(db.scalars(select(AnalysisTool)))
    assert len(tools) == 1
    assert tools[0].id == second.tool.id
    assert tools[0].version == first.tool.version + 1


def test_cross_user_tool_id_cannot_be_reused(db):
    user = default_user(db)
    generated = execute_generated_tool(db, user.id, uuid4(), date.today(), _proposal(date.today()))
    other = User(email="isolated@example.com", display_name="Isolated")
    db.add(other)
    db.flush()
    with pytest.raises(HarnessValidationError):
        execute_generated_tool(db, other.id, uuid4(), date.today(), None, generated.tool.id)


def test_recommendation_requires_and_returns_user_planning_context(db):
    user = default_user(db)
    proposal = _proposal(date.today())
    proposal.name = "Recommend food allocation"
    proposal.intent_signature = "recommend food allocation"
    proposal.plan.objective = "recommendation"
    with pytest.raises(HarnessValidationError):
        execute_generated_tool(db, user.id, uuid4(), date.today(), proposal)

    proposal = _proposal(date.today())
    proposal.name = "Recommend food allocation with budget"
    proposal.intent_signature = "recommend food allocation using budget"
    proposal.plan.objective = "recommendation"
    proposal.plan.context_sources = ["budgets"]
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add(Budget(user_id=user.id, category_id=food.id, name="Food budget", amount_minor=100_000, currency="INR"))
    db.flush()
    generated = execute_generated_tool(db, user.id, uuid4(), date.today(), proposal)
    context = generated.result.widgets[0].data["context"]
    assert context["budgets"][0]["remainingMinor"] == 100_000
    assert any(citation.entity_type == "budget" for citation in generated.result.citations)


def test_change_drivers_are_computed_from_two_periods(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    transport = db.scalar(select(Category).where(Category.slug == "transport"))
    previous = (date.today().replace(day=1) - timedelta(days=1)).replace(day=10)
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", category_id=food.id, transaction_date=previous),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=5_000, currency="INR", category_id=transport.id, transaction_date=previous),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=35_000, currency="INR", category_id=food.id, transaction_date=date.today()),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", category_id=transport.id, transaction_date=date.today()),
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
    generated = execute_generated_tool(db, user.id, uuid4(), date.today(), proposal)
    driver = generated.result.widgets[0].data["transforms"][0]["values"][0]
    assert driver["label"] == "Food"
    assert driver["value"] == 25_000
    assert generated.result.message.startswith("Food is the largest recorded increase")


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
    generated = execute_generated_tool(db, user.id, uuid4(), date.today(), proposal)
    repaired_plan = generated.tool.specification["plan"]
    expected_start = date(date.today().year, date.today().month - 2, 1)
    assert repaired_plan["queries"][0]["start_date"] == expected_start.isoformat()
    assert repaired_plan["queries"][0]["end_date"] == date.today().isoformat()
    assert repaired_plan["transforms"][0]["dimension"] == "category"
    assert repaired_plan["transforms"][0]["period_dimension"] == "month"


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
