from datetime import date
from types import SimpleNamespace

from agno.models.response import ToolExecution
from agno.run.agent import ReasoningContentDeltaEvent, RunContentEvent, RunOutput, ToolCallCompletedEvent

from app.config import get_settings
from app.operations.tools import build_operation_proposal_tool, operation_tool_name
from app.services import agents
from app.services.agents import AIAssistedMatch, build_reconciler
from app.services.preferences import AnswerStyle


def test_reconciler_is_optional_without_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    assert build_reconciler() is None


def test_internal_analysis_diagnostic_detection_matches_planner_trace_language():
    assert agents.contains_internal_analysis_diagnostic([
        "The governed transform catalog has no prorating or arbitrary scenario-calculation "
        "transform, so this cannot be computed in this semantic plan."
    ])
    assert not agents.contains_internal_analysis_diagnostic(["the annual interest rate"])


def test_reconciler_constructs_typed_advice_without_network_call(monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setenv("OPENAI_API_KEY", "test-only")
        scoped.setenv("PRIMARY_AGENT_ENABLED", "true")
        scoped.setenv("RECONCILER_MODEL", "gpt-5.6-luna")
        get_settings.cache_clear()
        assistant = build_reconciler()
        assert assistant.name == "Reconciler"
        assert assistant.output_schema is AIAssistedMatch
        assert assistant.telemetry is False
    get_settings.cache_clear()


def test_operator_disables_blocking_vendor_telemetry(monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setenv("OPENAI_API_KEY", "test-only")
        scoped.setenv("PRIMARY_AGENT_ENABLED", "true")
        get_settings.cache_clear()
        operator = agents.build_operator([], date(2026, 8, 19), "Asia/Kolkata")
        assert operator.telemetry is False
    get_settings.cache_clear()


def test_operator_answer_style_changes_verbosity_and_writing_contract(monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setenv("OPENAI_API_KEY", "test-only")
        scoped.setenv("PRIMARY_AGENT_ENABLED", "true")
        get_settings.cache_clear()

        explained = agents.build_operator(
            [], date(2026, 8, 19), "Asia/Kolkata", answer_style=AnswerStyle.EXPLAINED
        )
        concise = agents.build_operator(
            [], date(2026, 8, 19), "Asia/Kolkata", answer_style=AnswerStyle.CONCISE
        )

        assert explained.model.verbosity == "medium"
        assert concise.model.verbosity == "low"
        assert any("direct lookup, record list" in rule for rule in explained.instructions)
        assert any("1 to 2 useful" in rule for rule in explained.instructions)
        assert any("must not be left to interpret" in rule for rule in explained.instructions)
        assert any("smallest useful evidence" in rule for rule in concise.instructions)
    get_settings.cache_clear()


def test_operator_places_the_selected_style_contract_after_the_current_message(monkeypatch):
    captured = {}

    class StubOperator:
        def run(self, prompt, **_kwargs):
            captured["prompt"] = prompt
            return iter([RunOutput(content="One matching transaction was returned.")])

    monkeypatch.setattr(agents, "build_operator", lambda *args, **kwargs: StubOperator())

    result = agents.run_operator(
        "Show BigBasket transactions in June 2026",
        [],
        date(2026, 8, 19),
        "Asia/Kolkata",
        [],
        answer_style=AnswerStyle.EXPLAINED,
    )

    assert result.reply == "One matching transaction was returned."
    prompt = captured["prompt"]
    assert prompt.index("Current user message:") < prompt.index(
        "User-selected answer presentation contract"
    )
    assert "EXPLAINED" in prompt
    assert "Even a simple lookup" not in prompt
    assert "1 to 2" in prompt
    assert "adjacent plain-language interpretation" in prompt


def test_active_transaction_card_keeps_the_edit_operation_in_the_bounded_tool_set(monkeypatch):
    captured = {}

    class StubOperator:
        def run(self, *_args, **_kwargs):
            return iter([RunOutput(content="I need the typed edit operation.")])

    def build_stub(*_args, **kwargs):
        captured["operation_ids"] = {
            operation.id for operation in kwargs["operation_candidates"]
        }
        return StubOperator()

    monkeypatch.setattr(agents, "build_operator", build_stub)

    agents.run_operator(
        "Make it 640000",
        [],
        date(2026, 8, 19),
        "Asia/Kolkata",
        [],
        workflow_context={
            "kind": "saved_transaction_card",
            "transactionCardCount": 1,
            "intentContract": {"requested_effect": "mutation"},
        },
    )

    assert "edit_transaction" in captured["operation_ids"]


def test_related_question_suggester_disables_blocking_vendor_telemetry(monkeypatch):
    captured = {}

    class StubSuggester:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, *_args, **_kwargs):
            return SimpleNamespace(
                content=agents.RelatedQuestionSuggestions(questions=[]),
            )

    with monkeypatch.context() as scoped:
        scoped.setenv("OPENAI_API_KEY", "test-only")
        scoped.setenv("PRIMARY_AGENT_ENABLED", "true")
        scoped.setattr(agents, "Agent", StubSuggester)
        get_settings.cache_clear()
        assert agents.suggest_related_questions(
            "What did I spend?",
            "You spent ₹1,000.",
            [],
            [],
            date(2026, 8, 19),
            "Asia/Kolkata",
        ) == []
        assert captured["telemetry"] is False
    get_settings.cache_clear()


def test_operator_keeps_tool_answer_and_evidence_in_one_run(monkeypatch):
    execution = ToolExecution(
        tool_name="spending_summary",
        tool_args={"start": "2026-08-01", "end": "2026-08-14", "category_slug": None},
        result=(
            '{"total_minor":125000,"count":3,"currency":"INR",'
            '"start":"2026-08-01","end":"2026-08-14"}'
        ),
    )

    class StubOperator:
        def run(self, *_args, **_kwargs):
            def events():
                yield ToolCallCompletedEvent(tool=execution)
                yield RunContentEvent(content="You spent ₹1,250 ")
                yield RunContentEvent(content="across 3 transactions this month.")
                yield RunOutput(
                    content="You spent ₹1,250 across 3 transactions this month.",
                    tools=[execution],
                )
                raise AssertionError("terminal RunOutput must stop stream consumption")

            return events()

    monkeypatch.setattr(agents, "build_operator", lambda *args, **kwargs: StubOperator())
    deltas = []
    result = agents.run_operator(
        "How much did I spend this month?",
        [],
        date(2026, 8, 14),
        "Asia/Kolkata",
        [],
        runtime_tools=[SimpleNamespace(name="spending_summary")],
        on_delta=deltas.append,
        allow_live_deltas=False,
    )

    assert result.reply == "You spent ₹1,250 across 3 transactions this month."
    assert result.tool_grounding[0].name == "spending_summary"
    assert result.tool_grounding[0].result.data["total_minor"] == 125000
    assert result.streamed_live is False
    assert deltas == []


def test_operator_streams_and_retains_provider_reasoning(monkeypatch):
    class StubOperator:
        def run(self, *_args, **_kwargs):
            return iter([
                ReasoningContentDeltaEvent(reasoning_content="Read the follow-up context. "),
                RunContentEvent(reasoning_content="Keep the prior Housing filter. "),
                RunContentEvent(content="Here is the contextual answer."),
                RunOutput(
                    content="Here is the contextual answer.",
                    reasoning_content="Read the follow-up context. Keep the prior Housing filter. ",
                ),
            ])

    monkeypatch.setattr(agents, "build_operator", lambda *args, **kwargs: StubOperator())
    reasoning = []

    result = agents.run_operator(
        "What about July?",
        [],
        date(2026, 8, 14),
        "Asia/Kolkata",
        [],
        on_reasoning_delta=reasoning.append,
    )

    assert reasoning == ["Read the follow-up context. ", "Keep the prior Housing filter. "]
    assert result.reasoning_trace == "".join(reasoning)
    assert result.reply == "Here is the contextual answer."


def test_operator_stops_on_strict_filesystem_operation(monkeypatch):
    operation = agents.operation_catalog().snapshot().operation("create_transaction_draft")
    execution = ToolExecution(
        tool_name=operation_tool_name(operation),
        tool_args={
            "transaction_type": "expense",
            "amount_minor": 50000,
            "currency": "INR",
            "merchant": "lunch",
            "source_account": None,
            "destination_account": None,
            "transaction_date": None,
            "category_slug": None,
            "subcategory_slug": None,
            "tags": None,
            "spend_nature": None,
            "explicit_fields": ["transaction_type", "amount_minor", "merchant"],
        },
        result="proposal accepted",
        stop_after_tool_call=True,
    )

    class StubOperator:
        def run(self, *_args, **_kwargs):
            return iter([
                ToolCallCompletedEvent(tool=execution),
                RunOutput(content=None, tools=[execution]),
            ])

    monkeypatch.setattr(agents, "build_operator", lambda *args, **kwargs: StubOperator())
    result = agents.run_operator(
        "Add ₹500 for lunch",
        [],
        date(2026, 8, 14),
        "Asia/Kolkata",
        [],
    )

    assert result.reply is None
    assert result.operation.operation_id == "create_transaction_draft"
    assert result.operation.inputs["amount_minor"] == 50000
    assert result.tool_grounding == []


def test_filesystem_operation_schema_is_strict_and_nullable():
    operation = agents.operation_catalog().snapshot().operation("create_transaction_draft")
    tool = build_operation_proposal_tool(operation)
    schema = tool.parameters

    assert tool.strict is True
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert any(
        item.get("type") == "null"
        for item in schema["properties"]["amount_minor"]["anyOf"]
    )


def test_operation_proposal_tool_cannot_author_operation_identity_or_open_inputs():
    operation = agents.operation_catalog().snapshot().operation("manage_taxonomy")
    operation_properties = build_operation_proposal_tool(operation).parameters["properties"]
    assert "operation_id" not in operation_properties
    assert "operation_inputs" not in operation_properties















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









def test_non_chart_harness_decision_sheds_chart_bindings():
    from datetime import date

    from app.services.agents import CopilotDecision, PresentationIntent
    from app.services.semantic import AnalysisPlan, AnalysisToolProposal, FinanceQueryPlan

    def proposal(visualizations):
        return AnalysisToolProposal(
            name="Income-to-expense ratio",
            description="Recorded income divided by recorded gross expenses.",
            intent_signature="income to expense ratio",
            plan=AnalysisPlan(
                objective="descriptive",
                analysis_type="semantic_query",
                safe_reasoning_summary=["Aggregate income and expenses", "Divide the totals"],
                queries=[FinanceQueryPlan(
                    name="Income this month",
                    metric="income",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 16),
                )],
                visualizations=visualizations,
            ),
        )

    summary = CopilotDecision(
        tool="run_analysis_harness",
        analysis_tool=proposal([]),
        presentation=PresentationIntent(
            mode="summary",
            visual_goal="comparison",
            y_fields=["income", "expense"],
            color_field="transaction_type",
            value_semantics="percentage",
            unit_of_analysis="transaction_type",
        ),
        confidence=0.9,
        reason="Ratio request.",
    )
    # Chart-only noise is gone; the analytical grain and value semantics stay.
    assert summary.presentation.y_fields == []
    assert summary.presentation.color_field is None
    assert summary.presentation.visual_goal == "auto"
    assert summary.presentation.value_semantics == "percentage"
    assert summary.presentation.unit_of_analysis == "transaction_type"
