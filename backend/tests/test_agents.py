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

    proposal = agents._compile_governed_chart(route, today, "Asia/Kolkata").proposal

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

    proposal = agents._compile_governed_chart(route, today, "Asia/Kolkata").proposal
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
        proposal = agents._compile_governed_chart(route, today, "Asia/Kolkata").proposal
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

    proposal = agents._compile_governed_chart(route, today, "Asia/Kolkata").proposal
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

    proposal = agents._compile_governed_chart(route, today, "Asia/Kolkata").proposal
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


def _compile(text: str, presentation: PresentationIntent, query=None, today: date = date(2026, 8, 12)):
    """Compile a prompt the way the router does, so the test sees real inputs."""
    bound_presentation = agents._bind_explicit_presentation_unit(text, presentation)
    bound_query = agents._bind_explicit_universal_scope(text, query or agents.QueryInterpretation(
        metric="transaction_summary",
        result_mode="complex_analysis",
        operation="breakdown",
        limit=100,
    ))
    route = CopilotRouteDecision(
        route="analysis",
        query=bound_query,
        presentation=bound_presentation,
        confidence=0.99,
        reason=text,
    )
    return agents._compile_governed_chart(route, today, "Asia/Kolkata", text)


def _decision(compilation, reason="Compiled chart"):
    return agents.CopilotDecision(
        tool="run_analysis_harness",
        analysis_tool=compilation.proposal,
        presentation=compilation.presentation,
        assumptions=list(compilation.assumptions),
        confidence=0.99,
        reason=reason,
    )


def test_all_transaction_composition_resolves_to_direction_instead_of_a_meaningless_whole():
    text = "Draw a breakdown of all transaction in donut form"
    compilation = _compile(text, PresentationIntent(
        mode="chart",
        visual_goal="composition",
        requested_mark="arc",
        unit_of_analysis="category",
        value_semantics="amount",
    ))

    query = compilation.proposal.plan.queries[0]
    chart = compilation.proposal.plan.visualizations[0]
    # Expenses and income share no total, so a category pie of "all
    # transactions" cannot be drawn honestly. Direction is the one grain that
    # does partition every record exactly once.
    assert compilation.presentation.unit_of_analysis == "transaction_type"
    assert query.metric == "transaction_amount"
    assert query.dimensions == ["transaction_type"]
    assert chart.mark == "arc"
    assert {item.code for item in compilation.assumptions} == {"direction_composed", "defaulted_period"}
    assert agents._presentation_contract_issues(_decision(compilation), text) == []


def test_named_grain_keeps_its_grain_and_declares_the_direction_it_had_to_drop():
    text = "Draw a breakdown of all transactions by category in donut form"
    compilation = _compile(text, PresentationIntent(
        mode="chart",
        visual_goal="composition",
        requested_mark="arc",
        unit_of_analysis="category",
        value_semantics="amount",
    ))

    query = compilation.proposal.plan.queries[0]
    # A grain the user typed is a constraint, so the narrowing moves to the
    # direction instead — and is declared rather than applied in silence.
    assert compilation.presentation.unit_of_analysis == "category"
    assert query.metric == "gross_spend"
    restriction = next(item for item in compilation.assumptions if item.code == "direction_restricted")
    assert "income" in restriction.detail
    assert agents._presentation_contract_issues(_decision(compilation), text) == []


def test_undirected_request_keeps_direction_separable_at_a_non_temporal_grain():
    text = "Chart all transactions by merchant"
    compilation = _compile(text, PresentationIntent(
        mode="chart",
        visual_goal="comparison",
        unit_of_analysis="merchant",
        value_semantics="amount",
    ))

    query = compilation.proposal.plan.queries[0]
    chart = compilation.proposal.plan.visualizations[0]
    # The direction-preserving metric used to be reachable only for temporal
    # charts, which silently turned every categorical all-transaction chart
    # into an expenses-only one.
    assert query.metric == "transaction_amount"
    assert query.dimensions == ["merchant", "transaction_type"]
    assert chart.encoding.color.field == "transaction_type"
    assert agents._presentation_contract_issues(_decision(compilation), text) == []


def test_unstated_period_is_declared_and_all_records_are_not_reduced_to_month_to_date():
    today = date(2026, 8, 12)
    universal = _compile("Chart all transactions by merchant", PresentationIntent(
        mode="chart", visual_goal="comparison", unit_of_analysis="merchant", value_semantics="amount",
    ), today=today)
    scoped = _compile("Chart my spending by merchant", PresentationIntent(
        mode="chart", visual_goal="comparison", unit_of_analysis="merchant", value_semantics="amount",
    ), query=agents.QueryInterpretation(metric="gross_spend", transaction_type="expense"), today=today)

    assert universal.proposal.plan.queries[0].start_date < today.replace(day=1)
    assert scoped.proposal.plan.queries[0].start_date == today.replace(day=1)
    for compilation in (universal, scoped):
        assert any(item.code == "defaulted_period" for item in compilation.assumptions)


def test_fidelity_gate_catches_an_undeclared_narrowing_but_allows_a_declared_one():
    text = "Draw a breakdown of all transaction in donut form"
    compilation = _compile("Draw a breakdown of my spending by category in donut form", PresentationIntent(
        mode="chart",
        visual_goal="composition",
        requested_mark="arc",
        unit_of_analysis="category",
        value_semantics="amount",
    ), query=agents.QueryInterpretation(metric="gross_spend", transaction_type="expense"))

    undeclared = agents.CopilotDecision(
        tool="run_analysis_harness",
        analysis_tool=compilation.proposal,
        presentation=compilation.presentation,
        assumptions=[],
        confidence=0.99,
        reason="A model-authored plan that declared nothing.",
    )
    assert agents._presentation_contract_issues(undeclared, text) == [
        "Query “Spending by Category” restricts an all-transaction request to one financial direction without declaring it."
    ]
    # The same plan is fine once the narrowing is disclosed to the user.
    declared = undeclared.model_copy(update={"assumptions": [agents.CompilationAssumption(
        code="direction_restricted",
        detail="Restricted to expenses so the category shares total 100%.",
    )]})
    assert agents._presentation_contract_issues(declared, text) == []
    # And it is not an issue at all when the prompt never asked for every direction.
    assert agents._presentation_contract_issues(undeclared, "Draw a breakdown of my spending by category") == []


def test_fidelity_gate_rejects_an_undeclared_mark_substitution():
    text = "Draw a donut of my spending by transaction"
    compilation = _compile(text, PresentationIntent(
        mode="chart",
        visual_goal="composition",
        requested_mark="arc",
        unit_of_analysis="transaction",
        value_semantics="amount",
    ), query=agents.QueryInterpretation(metric="gross_spend", transaction_type="expense"))

    # Individual records have no shared total, so the pie becomes a bar. That
    # substitution is legitimate, but only because it is declared.
    assert compilation.proposal.plan.visualizations[0].mark == "bar"
    assert any(item.code == "mark_substituted" for item in compilation.assumptions)
    assert agents._presentation_contract_issues(_decision(compilation), text) == []

    silent = _decision(compilation).model_copy(update={"assumptions": []})
    assert agents._presentation_contract_issues(silent, text) == [
        "Chart must use the requested arc mark, not bar, unless the substitution is declared."
    ]


def test_count_composition_is_not_labelled_as_money():
    text = "Draw a breakdown of all transactions by type in donut form"
    compilation = _compile(text, PresentationIntent(
        mode="chart",
        visual_goal="composition",
        requested_mark="arc",
        unit_of_analysis="transaction_type",
        value_semantics="count",
    ))

    chart = compilation.proposal.plan.visualizations[0]
    value_tooltip = next(item for item in chart.encoding.tooltip if item.field == "value")
    # The arc branch used to hard code money_minor, so every count donut
    # claimed its slices were currency.
    assert compilation.proposal.plan.queries[0].metric == "transaction_count"
    assert value_tooltip.value_type == "number"


def test_universal_scope_releases_an_inherited_period_but_keeps_a_stated_one():
    inherited = agents.QueryInterpretation(
        metric="gross_spend",
        transaction_type="expense",
        start_date=date(2026, 8, 11),
        end_date=date(2026, 8, 12),
    )

    released = agents._bind_explicit_universal_scope("Draw a breakdown of all transaction in donut form", inherited)
    kept = agents._bind_explicit_universal_scope("Draw all transactions last month", inherited)

    # Dates are a filter like any other: an unqualified universal request must
    # not silently answer over the previous turn's two-day window.
    assert released.start_date is None and released.end_date is None
    assert kept.start_date == date(2026, 8, 11)


def test_explicit_period_detection_separates_stated_windows_from_bare_requests():
    assert not agents.states_explicit_period("Draw a breakdown of all transaction in donut form")
    assert not agents.states_explicit_period("Chart all transactions by merchant")
    for prompt in (
        "all transactions last month",
        "all transactions in July",
        "all transactions since 2024",
        "all transactions by hour in the last 24 hours",
        "all transactions today",
    ):
        assert agents.states_explicit_period(prompt), prompt


def test_scope_release_is_broader_than_direction_release():
    """Widening the population and dropping the direction are different acts."""
    spending = "Draw chart for all my spendings"
    records = "Draw a breakdown of all transaction in donut form"

    # Both walk away from the records on screen...
    assert agents.releases_prior_scope(spending)
    assert agents.releases_prior_scope(records)
    assert agents.releases_prior_scope("Chart everything I spent")
    assert not agents.releases_prior_scope("Show the coffee ones")
    # ...but only a request over records drops the financial direction.
    assert agents.states_universal_scope(records)
    assert not agents.states_universal_scope(spending)

    stale = agents.QueryInterpretation(
        metric="gross_spend",
        transaction_type="expense",
        category_slug="food",
        subcategory_slug="delivery",
        use_active_scope=True,
        start_date=date(2026, 8, 11),
        end_date=date(2026, 8, 12),
    )
    widened = agents._bind_explicit_universal_scope(spending, stale)

    # The prior food/delivery scope and its window go; the expense direction
    # the user actually asked about stays.
    assert widened.category_slug is None and widened.subcategory_slug is None
    assert widened.use_active_scope is False
    assert widened.start_date is None
    assert widened.metric == "gross_spend"
    assert widened.transaction_type == "expense"


def test_comparing_two_directions_binds_the_only_shape_that_keeps_both():
    text = "Show bar chart for earning and expenses"
    assert agents.names_multiple_directions(text)
    assert not agents.names_multiple_directions("Show bar chart for my expenses")
    assert not agents.names_multiple_directions("Chart my spending by category")

    # A directional metric answers for one direction by construction, so the
    # router proposing income-only would silently drop half the question.
    income_only = agents.QueryInterpretation(metric="income", transaction_type="income")
    released = agents._bind_multi_direction_scope(text, income_only)
    assert released.metric == "transaction_summary"
    assert released.transaction_type is None

    compilation = _compile(text, PresentationIntent(
        mode="chart", visual_goal="comparison", unit_of_analysis="category", value_semantics="amount",
    ), query=released)
    query = compilation.proposal.plan.queries[0]
    chart = compilation.proposal.plan.visualizations[0]

    # The comparison the prompt asked for is itself the grain.
    assert compilation.presentation.unit_of_analysis == "transaction_type"
    assert query.metric == "transaction_amount"
    assert query.dimensions == ["transaction_type"]
    assert chart.mark == "bar"
    assert agents._presentation_contract_issues(_decision(compilation), text) == []


def test_chart_request_is_read_from_the_prompt_not_left_to_the_router():
    """A successful scalar tool call must not be able to answer a chart request."""
    assert agents.requests_chart("Draw chart for all my spendings")
    assert agents.requests_chart("Show bar chart for earning and expenses")
    assert agents.requests_chart("Draw a breakdown of all transaction in donut form")
    assert not agents.requests_chart("How much did I spend on coffee?")
    # Ordinary English in a spending transcript must not read as a chart request.
    assert not agents.requests_chart("How much did I spend at the bar?")

    bound = agents._bind_explicit_presentation_unit(
        "Draw chart for all my spendings", PresentationIntent(),
    )
    assert bound.mode == "chart"


def test_unchartable_tool_result_does_not_satisfy_a_chart_request():
    """spending_summary returns one scalar; no axis can be bound to it."""
    scalar = agents.ToolGrounding(
        name="spending_summary",
        arguments={},
        result={"tool": "spending_summary", "data": {"kind": "summary", "total_minor": 254737}},
    )
    dataset = agents.ToolGrounding(
        name="loan_amortization_schedule",
        arguments={},
        result={"tool": "loan_amortization_schedule", "data": {
            "kind": "computed_dataset",
            "rows": [{"period": 1, "interest": 100}],
            "fields": [
                {"name": "period", "role": "dimension", "type": "ordinal"},
                {"name": "interest", "role": "measure", "value_type": "money_minor"},
            ],
        }},
    )
    assert agents._chartable_grounding([scalar]) is None
    assert agents._chartable_grounding([scalar, dataset]) is dataset


def test_chart_surface_is_derived_from_the_semantic_registry_not_hand_listed():
    """The manifest must grow with the registry, not cap the product."""
    from app.services.semantic_registry import semantic_schema_registry

    grains = {capability.grain for capability in agents.CHART_CAPABILITIES}
    registry_grains = {
        dimension.name
        for dimension in semantic_schema_registry().dimensions
        if dimension.base_entity == "transactions"
    } - {"transaction_date", "month", "posted_date"}

    # Every governed transaction dimension is chartable the day it is defined.
    assert registry_grains <= grains
    assert {"date", "month"} <= grains
    # Dimensions that only exist for calculator datasets are deliberately absent,
    # so compiling them is declined instead of emitting an unbindable query.
    assert "installment" not in grains and "calculation_step" not in grains

    for capability in agents.CHART_CAPABILITIES:
        assert capability.preferred_mark in capability.marks
    # Individual records share no total, so no part-to-whole at that grain.
    assert "arc" not in agents.chart_capability("transaction").marks
    assert "arc" in agents.chart_capability("category").marks
    # Ordered series need a real axis.
    assert "line" not in agents.chart_capability("category").marks
    assert "line" in agents.chart_capability("date").marks


def test_unsupported_grain_is_declined_instead_of_compiled_into_an_unbindable_query():
    presentation = PresentationIntent(
        mode="chart", visual_goal="comparison", unit_of_analysis="installment", value_semantics="amount",
    )
    assert agents.resolve_chart_shape(presentation) is None

    route = CopilotRouteDecision(
        route="analysis",
        query=agents.QueryInterpretation(metric="gross_spend", transaction_type="expense"),
        presentation=presentation,
        confidence=0.99,
        reason="Installments belong to a calculator dataset, not a database chart.",
    )
    assert agents._compile_governed_chart(route, date(2026, 8, 12), "Asia/Kolkata", "chart by installment") is None


def test_mark_substitution_comes_from_the_manifest_and_is_declared():
    shape = agents.resolve_chart_shape(PresentationIntent(
        mode="chart", requested_mark="arc", unit_of_analysis="transaction", value_semantics="amount",
    ))
    assert shape.mark == "bar"
    assert [item.code for item in shape.assumptions] == ["mark_substituted"]

    honoured = agents.resolve_chart_shape(PresentationIntent(
        mode="chart", requested_mark="arc", unit_of_analysis="category", value_semantics="amount",
    ))
    assert honoured.mark == "arc"
    assert honoured.assumptions == ()


def test_newly_reachable_registry_grain_compiles_end_to_end():
    """tag was bindable by the query compiler all along but unreachable."""
    text = "Chart my spending by tag"
    compilation = _compile(text, PresentationIntent(
        mode="chart", visual_goal="comparison", unit_of_analysis="auto", value_semantics="amount",
    ), query=agents.QueryInterpretation(metric="gross_spend", transaction_type="expense"))

    query = compilation.proposal.plan.queries[0]
    assert compilation.presentation.unit_of_analysis == "tag"
    assert query.dimensions == ["tag"]
    assert agents._presentation_contract_issues(_decision(compilation), text) == []
