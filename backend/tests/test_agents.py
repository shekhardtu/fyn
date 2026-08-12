from datetime import date, timedelta
from types import SimpleNamespace

from app.config import get_settings
from app.services import agents
from app.services.agents import AIAssistedMatch, CopilotRouteDecision, PresentationIntent, build_reconciliation_assistant
from app.services.semantic import AnalysisPlan, AnalysisToolProposal, FinanceFilter, FinanceQueryPlan, VisualEncoding, VisualEncodingSet, VisualizationSpec


def test_agno_assistant_is_optional_without_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    assert build_reconciliation_assistant() is None


def test_agno_assistant_constructs_typed_advice_without_network_call(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    monkeypatch.setenv("RECONCILIATION_MODEL", "gpt-5.6-luna")
    get_settings.cache_clear()
    assistant = build_reconciliation_assistant()
    assert assistant.name == "Reconciliation evaluator"
    assert assistant.output_schema is AIAssistedMatch
    get_settings.cache_clear()


def test_chart_presentation_forces_analysis_factory_before_transaction_search(monkeypatch):
    today = date(2026, 8, 11)
    route = CopilotRouteDecision(
        route="analysis",
        query={
            "metric": "transaction_summary",
            "result_mode": "transaction_list",
            "operation": "list",
            "category_slug": "entertainment",
            "transaction_type": "expense",
            "start_date": "2026-08-01",
            "end_date": "2026-08-11",
        },
        # Even if a model proposes an ungrounded merchant grain, the explicit
        # `by Transactions` phrase binds the governed presentation contract.
        presentation=PresentationIntent(mode="chart", chart_type="bar", unit_of_analysis="merchant", value_semantics="amount"),
        confidence=0.99,
        reason="The user requested a transaction amount graph.",
    )
    proposal = AnalysisToolProposal(
        name="Entertainment transaction graph",
        description="Graph individual Entertainment transactions by amount.",
        intent_signature="entertainment transaction amount graph",
        plan=AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Filter Entertainment", "Plot transaction amounts"],
            queries=[FinanceQueryPlan(
                name="Entertainment transactions",
                metric="gross_spend",
                dimensions=["transaction", "merchant", "transaction_date"],
                filters=[FinanceFilter(field="category", value="entertainment")],
                start_date=today.replace(day=1),
                end_date=today,
            )],
            visualizations=[VisualizationSpec(
                name="Entertainment amounts",
                query_name="Entertainment transactions",
                mark="bar",
                encoding=VisualEncodingSet(
                    x=VisualEncoding(field="transaction", type="nominal"),
                    y=VisualEncoding(field="value", type="quantitative", value_type="money_minor"),
                    tooltip=[VisualEncoding(field="merchant", type="nominal"), VisualEncoding(field="transaction_date", type="temporal")],
                ),
                title="Entertainment transactions by amount",
                rationale="Compare each recorded transaction amount.",
            )],
        ),
    )

    class StubAgent:
        def __init__(self, content):
            self.content = content

        def run(self, prompt):
            return SimpleNamespace(content=self.content)

    monkeypatch.setattr(agents, "build_financial_copilot", lambda *args, **kwargs: StubAgent(route))
    monkeypatch.setattr(agents, "build_analysis_tool_factory", lambda *args, **kwargs: StubAgent(proposal))

    decision = agents.interpret_with_financial_copilot(
        "Draw graph of entertainment category by Transactions amount",
        [],
        today,
        "Asia/Kolkata",
        [],
    )

    assert decision.tool == "run_analysis_harness"
    assert decision.presentation.mode == "chart"
    assert decision.presentation.unit_of_analysis == "transaction"
    assert decision.analysis_tool.plan.visualizations[0].mark == "bar"


def test_chart_contract_rejects_merchant_grouping_for_transaction_grain():
    today = date(2026, 8, 11)
    proposal = AnalysisToolProposal(
        name="Incorrect merchant graph",
        description="Incorrectly groups transaction amounts by merchant.",
        intent_signature="transaction amount graph",
        plan=AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Plot the data"],
            queries=[FinanceQueryPlan(
                name="Merchant totals",
                metric="gross_spend",
                dimensions=["merchant"],
                filters=[FinanceFilter(field="category", value="entertainment")],
                start_date=today.replace(day=1),
                end_date=today,
            )],
            visualizations=[VisualizationSpec(
                name="Merchant totals",
                query_name="Merchant totals",
                mark="bar",
                encoding=VisualEncodingSet(
                    x=VisualEncoding(field="merchant", type="nominal"),
                    y=VisualEncoding(field="value", type="quantitative", value_type="money_minor"),
                ),
                title="Entertainment by merchant",
                rationale="Compare merchant totals.",
            )],
        ),
    )
    decision = agents.CopilotDecision(
        tool="run_analysis_harness",
        analysis_tool=proposal,
        presentation=PresentationIntent(mode="chart", chart_type="bar", unit_of_analysis="transaction", value_semantics="amount"),
        confidence=0.99,
        reason="Chart request",
    )

    issues = agents._presentation_contract_issues(decision)

    assert "Chart query must preserve transaction as its unit of analysis." in issues
    assert "Chart axis must use transaction, not merchant." in issues


def test_time_chart_compiler_uses_compositional_day_bucket():
    today = date(2026, 8, 11)
    route = CopilotRouteDecision(
        route="analysis",
        query={
            "metric": "transaction_summary",
            "result_mode": "complex_analysis",
            "operation": "breakdown",
            "category_slug": "entertainment",
            "transaction_type": "expense",
            "start_date": "2026-08-01",
            "end_date": "2026-08-11",
        },
        presentation=PresentationIntent(mode="chart", chart_type="line", unit_of_analysis="date", value_semantics="amount"),
        confidence=0.99,
        reason="Plot the prior records by time.",
    )

    proposal = agents._compile_governed_chart(route, today, "Asia/Kolkata")

    assert proposal.plan.queries[0].dimensions == []
    assert proposal.plan.queries[0].time_grouping.grain == "day"
    assert proposal.plan.queries[0].time_grouping.timezone == "Asia/Kolkata"
    assert proposal.plan.queries[0].filters[0].field == "category"
    assert proposal.plan.queries[0].filters[0].value == "entertainment"
    assert proposal.plan.visualizations[0].mark == "line"
    assert proposal.plan.visualizations[0].encoding.x.field == "time_bucket"


def test_temporal_binding_and_compiler_support_rolling_hour_without_amount_confusion():
    today = date(2026, 8, 11)
    presentation = agents._bind_explicit_presentation_unit(
        "Draw all transactions by hour in the last 24 hours",
        PresentationIntent(mode="chart", unit_of_analysis="auto"),
    )
    route = CopilotRouteDecision(
        route="analysis",
        query={
            "metric": "transaction_summary",
            "result_mode": "complex_analysis",
            "operation": "breakdown",
            "start_date": "2026-08-10",
            "end_date": "2026-08-11",
        },
        presentation=presentation,
        confidence=0.99,
        reason="Plot all transaction counts by hour.",
    )

    proposal = agents._compile_governed_chart(route, today, "Asia/Kolkata")
    query = proposal.plan.queries[0]

    assert presentation.time_grain == "hour"
    assert presentation.rolling_value == 24
    assert presentation.rolling_unit == "hour"
    assert query.metric == "transaction_amount"
    assert query.dimensions == ["transaction_type"]
    assert proposal.plan.visualizations[0].encoding.color.field == "transaction_type"
    assert query.time_grouping.grain == "hour"
    assert query.start_datetime is not None
    assert query.end_datetime - query.start_datetime == timedelta(hours=24)
    assert proposal.plan.visualizations[0].encoding.x.field == "time_bucket"


def test_all_transactions_resets_stale_financial_filters_before_compilation():
    stale = agents.QueryInterpretation(
        metric="gross_spend",
        transaction_type="expense",
        merchant="Toit",
        category_slug="entertainment",
        account="HDFC",
        tag="weekend",
        min_amount_minor=10_000,
        use_active_scope=True,
    )

    query = agents._bind_explicit_universal_scope(
        "Draw all transaction on graph by time by hours in last 24 hours",
        stale,
    )

    assert query.metric == "transaction_summary"
    assert query.transaction_type is None
    assert query.merchant is None
    assert query.category_slug is None
    assert query.account is None
    assert query.tag is None
    assert query.min_amount_minor is None
    assert query.use_active_scope is False

    created = agents._bind_explicit_universal_scope(
        "Draw all transactions by hour",
        None,
    )
    assert created.metric == "transaction_summary"
    assert created.result_mode == "complex_analysis"
    assert created.limit == 100


def test_temporal_compiler_supports_week_quarter_and_year_without_new_dimensions():
    today = date(2026, 8, 11)
    for grain in ("week", "quarter", "year"):
        route = CopilotRouteDecision(
            route="analysis",
            query={"metric": "gross_spend", "result_mode": "complex_analysis", "operation": "breakdown"},
            presentation=PresentationIntent(mode="chart", unit_of_analysis="date", time_grain=grain),
            confidence=0.99,
            reason=f"Plot spending by {grain}.",
        )
        proposal = agents._compile_governed_chart(route, today, "Asia/Kolkata")
        query = proposal.plan.queries[0]
        assert query.dimensions == []
        assert query.time_grouping.grain == grain
        assert proposal.plan.visualizations[0].encoding.x.field == "time_bucket"


def test_heatmap_compiler_uses_governed_day_by_hour_pivot():
    today = date(2026, 8, 11)
    presentation = agents._bind_explicit_presentation_unit(
        "Show a heatmap by timeshift of last 3 days",
        PresentationIntent(mode="chart", unit_of_analysis="auto"),
    )
    route = CopilotRouteDecision(
        route="analysis",
        query={"metric": "transaction_summary", "result_mode": "complex_analysis", "operation": "breakdown"},
        presentation=presentation,
        confidence=0.99,
        reason="Render a temporal heatmap.",
    )

    proposal = agents._compile_governed_chart(route, today, "Asia/Kolkata")
    query = proposal.plan.queries[0]
    chart = proposal.plan.visualizations[0]

    assert presentation.chart_type == "heatmap"
    assert presentation.rolling_value == 3
    assert presentation.rolling_unit == "day"
    assert query.time_grouping is None
    assert query.time_pivot.row_grain == "day"
    assert query.time_pivot.column_component == "hour_of_day"
    assert query.dimensions == ["transaction_type"]
    assert chart.mark == "rect"
    assert chart.encoding.x.field == "time_segment"
    assert chart.encoding.y.field == "time_bucket"
    assert chart.encoding.row.field == "transaction_type"


def test_composition_compiler_derives_shares_without_losing_money_evidence():
    today = date(2026, 8, 11)
    route = CopilotRouteDecision(
        route="analysis",
        query={
            "metric": "spending_summary",
            "result_mode": "complex_analysis",
            "operation": "breakdown",
            "group_by": "category",
            "transaction_type": "expense",
            "start_date": "2026-08-01",
            "end_date": "2026-08-11",
        },
        presentation=PresentationIntent(
            mode="chart",
            visual_goal="composition",
            requested_mark="arc",
            unit_of_analysis="category",
            value_semantics="percentage",
        ),
        confidence=0.99,
        reason="Show month-to-date spending composition.",
    )

    proposal = agents._compile_governed_chart(route, today, "Asia/Kolkata")
    query = proposal.plan.queries[0]
    transform = proposal.plan.transforms[0]
    chart = proposal.plan.visualizations[0]

    assert query.metric == "gross_spend"
    assert query.dimensions == ["category"]
    assert transform.operation == "share_of_total"
    assert transform.dimension == "category"
    assert chart.transform_name == transform.name
    assert chart.mark == "arc"
    assert chart.encoding.color.field == "label"
    assert chart.encoding.theta.field == "basis_points"
    assert chart.encoding.theta.value_type == "percentage"
    assert any(item.field == "value" and item.value_type == "money_minor" for item in chart.encoding.tooltip)
    assert agents._presentation_contract_issues(agents.CopilotDecision(
        tool="run_analysis_harness",
        analysis_tool=proposal,
        presentation=route.presentation,
        confidence=0.99,
        reason="Validated composition",
    )) == []


def test_dashboard_contract_requires_coverage_not_one_axis_for_every_view():
    today = date(2026, 8, 11)
    proposal = AnalysisToolProposal(
        name="Monthly finance dashboard",
        description="Compose a time trend and category composition.",
        intent_signature="monthly trend and category composition dashboard",
        plan=AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Build independently governed dashboard views"],
            queries=[
                FinanceQueryPlan(
                    name="Daily spending",
                    metric="gross_spend",
                    dimensions=[],
                    start_date=today.replace(day=1),
                    end_date=today,
                    time_grouping={"grain": "day", "timezone": "Asia/Kolkata"},
                ),
                FinanceQueryPlan(
                    name="Category spending",
                    metric="gross_spend",
                    dimensions=["category"],
                    start_date=today.replace(day=1),
                    end_date=today,
                ),
            ],
            visualizations=[
                VisualizationSpec(
                    name="Daily trend",
                    query_name="Daily spending",
                    mark="line",
                    encoding=VisualEncodingSet(
                        x=VisualEncoding(field="time_bucket", type="temporal", value_type="datetime"),
                        y=VisualEncoding(field="value", type="quantitative", value_type="money_minor"),
                    ),
                    title="Daily spending",
                    rationale="Preserve ordered daily values.",
                ),
                VisualizationSpec(
                    name="Category composition",
                    query_name="Category spending",
                    mark="arc",
                    encoding=VisualEncodingSet(
                        color=VisualEncoding(field="category", type="nominal", value_type="category"),
                        theta=VisualEncoding(field="value", type="quantitative", value_type="money_minor"),
                    ),
                    title="Spending by category",
                    rationale="Preserve the category composition.",
                ),
            ],
        ),
    )
    decision = agents.CopilotDecision(
        tool="run_analysis_harness",
        analysis_tool=proposal,
        presentation=PresentationIntent(
            mode="chart",
            visual_goal="composition",
            unit_of_analysis="category",
            value_semantics="amount",
        ),
        confidence=0.99,
        reason="Build a multi-view dashboard.",
    )

    assert agents._presentation_contract_issues(decision) == []


def test_explicit_dashboard_forces_multi_view_factory_and_rejects_single_chart():
    today = date(2026, 8, 11)
    presentation = agents._bind_explicit_presentation_unit(
        "Build a BI dashboard with trends and category composition",
        PresentationIntent(mode="auto", unit_of_analysis="category"),
    )
    route = CopilotRouteDecision(
        route="analysis",
        query={"metric": "gross_spend", "result_mode": "complex_analysis", "operation": "breakdown"},
        presentation=presentation,
        confidence=0.99,
        reason="Build a dashboard.",
    )

    assert presentation.mode == "chart"
    assert presentation.layout == "dashboard"
    assert agents._compile_governed_chart(route, today, "Asia/Kolkata") is None

    single = AnalysisToolProposal(
        name="Single chart",
        description="One chart cannot satisfy a dashboard.",
        intent_signature="single chart",
        plan=AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Render one chart"],
            queries=[FinanceQueryPlan(
                name="Category spending",
                metric="gross_spend",
                dimensions=["category"],
                start_date=today.replace(day=1),
                end_date=today,
            )],
            visualizations=[VisualizationSpec(
                name="Category spending",
                query_name="Category spending",
                mark="bar",
                encoding=VisualEncodingSet(
                    x=VisualEncoding(field="category", type="nominal", value_type="category"),
                    y=VisualEncoding(field="value", type="quantitative", value_type="money_minor"),
                ),
                title="Category spending",
                rationale="Show category totals.",
            )],
        ),
    )
    decision = agents.CopilotDecision(
        tool="run_analysis_harness",
        analysis_tool=single,
        presentation=presentation,
        confidence=0.99,
        reason="Invalid single-view dashboard.",
    )
    assert agents._presentation_contract_issues(decision) == [
        "Dashboard presentation requires multiple independently governed views."
    ]
