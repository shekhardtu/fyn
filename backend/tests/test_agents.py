from datetime import date
from types import SimpleNamespace

from agno.metrics import RunMetrics
from agno.models.response import ToolExecution
from agno.run.agent import ModelRequestCompletedEvent, ModelRequestStartedEvent, ReasoningContentDeltaEvent, RunContentEvent, RunOutput, ToolCallCompletedEvent

from app.config import get_settings
from app.operations.tools import build_operation_proposal_tool, operation_tool_name
from app.services import agents
from app.services.agent_run_metrics import agent_metric_snapshot, begin_agent_metric_collection, end_agent_metric_collection
from app.services.agents import AIAssistedMatch, build_reconciler
from app.services.answer_validation import validate_evidence
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
        assert operator.model.request_params["prompt_cache_key"].startswith("fyn-operator-v1-")
        assert operator.model.request_params["prompt_cache_options"] == {
            "mode": "implicit",
            "ttl": "30m",
        }
    get_settings.cache_clear()


def test_complex_analysis_delegate_is_one_call_read_only_and_promotes_evidence(monkeypatch):
    captured = {}
    nested_execution = ToolExecution(
        tool_name="run_governed_sql",
        tool_args={"sql": "SELECT total_minor FROM governed_view"},
        result={
            "kind": "governed_sql",
            "rows": [{"total_minor": 125000, "currency": "INR"}],
        },
    )

    class StubDelegate:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.model = kwargs["model"]
            self.tools = kwargs["tools"]

        def run(self, *_args, **_kwargs):
            return RunOutput(
                content="The constrained result is ₹1,250.",
                tools=[nested_execution],
            )

    with monkeypatch.context() as scoped:
        scoped.setenv("OPENAI_API_KEY", "test-only")
        scoped.setenv("PRIMARY_AGENT_ENABLED", "true")
        scoped.setenv("ANALYSIS_DELEGATION_ENABLED", "true")
        scoped.setenv("ANALYSIS_DELEGATION_ROLLOUT_PERCENT", "100")
        scoped.setenv("ANALYSIS_DELEGATE_MODEL", "gpt-5.6-terra")
        scoped.setattr(agents, "Agent", StubDelegate)
        get_settings.cache_clear()
        read_tool = SimpleNamespace(name="run_governed_sql")
        operation_tool = SimpleNamespace(name="propose_operation__edit_transaction")
        delegate_tool = agents.build_analysis_delegate_tool(
            "Find the constrained optimum.",
            date(2026, 8, 19),
            "Asia/Kolkata",
            [],
            read_tools=[read_tool, operation_tool],
        )

        assert delegate_tool.name == agents.ANALYSIS_DELEGATE_TOOL_NAME
        assert delegate_tool.strict is True
        assert delegate_tool.parameters["required"] == ["analysis_focus"]
        payload = delegate_tool.entrypoint(analysis_focus="Optimize across the verified rows.")
        assert payload["kind"] == "delegated_financial_analysis"
        assert payload["message"] == "The constrained result is ₹1,250."
        assert [tool.name for tool in captured["tools"]] == ["run_governed_sql"]
        assert captured["telemetry"] is False
        assert captured["model"].id == "gpt-5.6-terra"
        assert captured["model"].reasoning_effort == "medium"
        assert delegate_tool.entrypoint(analysis_focus="Try again.")["error"]["code"] == "delegate_call_limit"

        outer_execution = ToolExecution(
            tool_name=agents.ANALYSIS_DELEGATE_TOOL_NAME,
            tool_args={"analysis_focus": "Optimize across the verified rows."},
            result=payload,
        )
        grounding = agents._runtime_tool_grounding(
            SimpleNamespace(tools=[outer_execution]),
            [delegate_tool],
        )
        assert [item.name for item in grounding] == ["run_governed_sql"]
        assert grounding[0].arguments == nested_execution.tool_args
        assert grounding[0].result.data["rows"][0]["total_minor"] == 125000
        assert validate_evidence("The constrained result is ₹1,250.", grounding).passed
        assert not validate_evidence("The constrained result is ₹1,300.", grounding).passed
    get_settings.cache_clear()


def test_complex_analysis_delegate_failure_is_a_tool_error_not_an_exception(monkeypatch):
    class UnavailableDelegate:
        def __init__(self, **_kwargs):
            pass

        def run(self, *_args, **_kwargs):
            raise TimeoutError("provider timed out")

    with monkeypatch.context() as scoped:
        scoped.setenv("OPENAI_API_KEY", "test-only")
        scoped.setenv("PRIMARY_AGENT_ENABLED", "true")
        scoped.setenv("ANALYSIS_DELEGATION_ENABLED", "true")
        scoped.setenv("ANALYSIS_DELEGATION_ROLLOUT_PERCENT", "100")
        scoped.setattr(agents, "Agent", UnavailableDelegate)
        get_settings.cache_clear()
        delegate_tool = agents.build_analysis_delegate_tool(
            "Find the constrained optimum.",
            date(2026, 8, 19),
            "Asia/Kolkata",
            [],
            read_tools=[SimpleNamespace(name="run_governed_sql")],
        )

        result = delegate_tool.entrypoint(analysis_focus="Optimize across verified rows.")
        assert result["error"]["stage"] == "delegation"
        assert result["error"]["code"] == "delegate_unavailable"
        assert "TimeoutError" in result["error"]["detail"]
    get_settings.cache_clear()


def test_operator_fetches_taxonomy_only_when_the_model_needs_it(monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setenv("OPENAI_API_KEY", "test-only")
        scoped.setenv("PRIMARY_AGENT_ENABLED", "true")
        get_settings.cache_clear()
        operator = agents.build_operator(
            [{"slug": "secret-custom", "name": "Secret custom", "subcategories": []}],
            date(2026, 8, 19),
            "Asia/Kolkata",
        )

        instructions = "\n".join(str(item) for item in operator.instructions)
        assert "secret-custom" not in instructions
        assert "read_user_expense_taxonomy" in instructions
        assert operator.model.reasoning_summary == "concise"
    get_settings.cache_clear()


def test_operator_prefers_optional_delegate_only_for_named_complexity(monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setenv("OPENAI_API_KEY", "test-only")
        scoped.setenv("PRIMARY_AGENT_ENABLED", "true")
        get_settings.cache_clear()
        operator = agents.build_operator(
            [],
            date(2026, 8, 19),
            "Asia/Kolkata",
            analysis_tools=[
                SimpleNamespace(name="run_governed_sql"),
                SimpleNamespace(name=agents.ANALYSIS_DELEGATE_TOOL_NAME),
            ],
        )

        instructions = "\n".join(str(item) for item in operator.instructions)
        assert "Prefer delegate_complex_analysis" in instructions
        assert "no exact semantic tool matches" in instructions
        assert "Never call both delegate_complex_analysis and run_governed_sql" in instructions
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


def test_read_operator_omits_effectful_operation_prompt_rules(monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setenv("OPENAI_API_KEY", "test-only")
        scoped.setenv("PRIMARY_AGENT_ENABLED", "true")
        get_settings.cache_clear()
        snapshot = agents.operation_catalog().snapshot()
        operator = agents.build_operator(
            [],
            date(2026, 8, 19),
            "Asia/Kolkata",
            operation_candidates=[
                snapshot.operation("search_transactions"),
                snapshot.operation("request_clarification"),
            ],
        )

        instructions = "\n".join(str(item) for item in operator.instructions)
        assert "strict clarification operation" in instructions
        assert "Populate the selected operation's typed fields" not in instructions
        assert "Never claim a mutation succeeded" not in instructions
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


def test_explicit_read_turn_does_not_mount_unrelated_draft_operations(monkeypatch):
    captured = {}

    class StubOperator:
        def run(self, *_args, **_kwargs):
            return iter([RunOutput(content="The read remained agentic.")])

    def build_stub(*_args, **kwargs):
        captured["operation_ids"] = {
            operation.id for operation in kwargs["operation_candidates"]
        }
        return StubOperator()

    monkeypatch.setattr(agents, "build_operator", build_stub)

    agents.run_operator(
        "How much did I spend this month?",
        [],
        date(2026, 8, 19),
        "Asia/Kolkata",
        [],
        workflow_context={
            "intentContract": {
                "requested_access": "read",
                "requested_effect": "none",
                "read_evidence": ["financial_read_request"],
            },
        },
    )

    assert "search_transactions" in captured["operation_ids"]
    assert "request_clarification" not in captured["operation_ids"]
    assert "edit_transaction" not in captured["operation_ids"]
    assert "planning" not in captured["operation_ids"]


def test_analysis_turn_omits_duplicate_search_operation(monkeypatch):
    captured = {}

    class StubOperator:
        def run(self, *_args, **_kwargs):
            return iter([RunOutput(content="The analysis stayed agent-selected.")])

    def build_stub(*_args, **kwargs):
        captured["operation_ids"] = {
            operation.id for operation in kwargs["operation_candidates"]
        }
        return StubOperator()

    monkeypatch.setattr(agents, "build_operator", build_stub)

    agents.run_operator(
        "Compare my spending by category.",
        [],
        date(2026, 8, 19),
        "Asia/Kolkata",
        [],
        workflow_context={
            "intentContract": {
                "requested_access": "read",
                "requested_effect": "none",
                "read_evidence": ["financial_read_request"],
            },
        },
        analysis_tools=[SimpleNamespace(name="run_governed_sql")],
    )

    assert "search_transactions" not in captured["operation_ids"]


def test_complete_calculator_scenario_mounts_no_operation_proposals(monkeypatch):
    captured = {}

    class StubOperator:
        def run(self, *_args, **_kwargs):
            return iter([RunOutput(content="The calculator remains agent-selected.")])

    def build_stub(*_args, **kwargs):
        captured["operation_ids"] = {
            operation.id for operation in kwargs["operation_candidates"]
        }
        return StubOperator()

    monkeypatch.setattr(agents, "build_operator", build_stub)

    agents.run_operator(
        "What is the EMI for this numeric scenario?",
        [],
        date(2026, 8, 19),
        "Asia/Kolkata",
        [],
        workflow_context={
            "kind": "calculator_scenario",
            "intentContract": {
                "requested_access": "read",
                "requested_effect": "none",
                "read_evidence": ["financial_read_request"],
            },
        },
    )

    assert captured["operation_ids"] == set()


def test_social_conversation_keeps_model_and_mounts_no_finance_operations(monkeypatch):
    captured = {}

    class StubOperator:
        def run(self, *_args, **_kwargs):
            return iter([RunOutput(content="Doing well—glad you're here.")])

    def build_stub(*_args, **kwargs):
        captured["operation_ids"] = {
            operation.id for operation in kwargs["operation_candidates"]
        }
        return StubOperator()

    monkeypatch.setattr(agents, "build_operator", build_stub)

    result = agents.run_operator(
        "How are you doing?",
        [],
        date(2026, 8, 19),
        "Asia/Kolkata",
        [],
        workflow_context={"kind": "conversation_only"},
    )

    assert result.reply == "Doing well—glad you're here."
    assert captured["operation_ids"] == set()


def test_explicit_no_record_explanation_mounts_no_operations(monkeypatch):
    captured = {}

    class StubOperator:
        def run(self, *_args, **_kwargs):
            return iter([RunOutput(content="Principal is the amount borrowed.")])

    def build_stub(*_args, **kwargs):
        captured["operation_ids"] = {
            operation.id for operation in kwargs["operation_candidates"]
        }
        return StubOperator()

    monkeypatch.setattr(agents, "build_operator", build_stub)

    agents.run_operator(
        "Explain principal versus interest without using my records.",
        [],
        date(2026, 8, 19),
        "Asia/Kolkata",
        [],
        workflow_context={"kind": "knowledge_only"},
    )

    assert captured["operation_ids"] == set()


def test_related_question_suggester_disables_blocking_vendor_telemetry(monkeypatch):
    captured = {}

    class StubSuggester:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, *_args, **_kwargs):
            return SimpleNamespace(
                content=agents.RelatedQuestionSuggestions(),
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
        instructions = "\n".join(captured["instructions"])
        assert "contextual_drill_down" in instructions
        assert "behavioral_pattern" in instructions
        assert "strategic_outlook" in instructions
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


def test_operator_records_each_provider_request_inside_its_tool_loop(monkeypatch):
    class StubOperator:
        def run(self, *_args, **_kwargs):
            return iter([
                ModelRequestStartedEvent(model="operator-model", model_provider="OpenAI"),
                ModelRequestCompletedEvent(
                    model="operator-model",
                    model_provider="OpenAI",
                    input_tokens=80,
                    output_tokens=20,
                    total_tokens=100,
                    time_to_first_token=0.35,
                    reasoning_tokens=4,
                    cache_read_tokens=30,
                ),
                RunOutput(
                    content="Here is the answer.",
                    model="operator-model",
                    model_provider="OpenAI",
                    metrics=RunMetrics(
                        input_tokens=80,
                        output_tokens=20,
                        total_tokens=100,
                        duration=0.9,
                    ),
                ),
            ])

    monotonic = iter([10.0, 10.8])
    monkeypatch.setattr(agents, "build_operator", lambda *args, **kwargs: StubOperator())
    monkeypatch.setattr(agents, "perf_counter", monotonic.__next__)
    token = begin_agent_metric_collection()
    try:
        agents.run_operator(
            "Explain this.",
            [],
            date(2026, 8, 14),
            "Asia/Kolkata",
            [],
        )
        snapshot = agent_metric_snapshot()
    finally:
        end_agent_metric_collection(token)

    assert snapshot["modelPasses"] == 1
    assert snapshot["providerRequestCount"] == 1
    assert snapshot["passes"][0]["providerRequests"] == [{
        "model": "operator-model",
        "provider": "OpenAI",
        "durationMs": 800.0,
        "timeToFirstTokenMs": 350.0,
        "inputTokens": 80,
        "outputTokens": 20,
        "totalTokens": 100,
        "cacheReadTokens": 30,
        "cacheWriteTokens": 0,
        "reasoningTokens": 4,
    }]


def test_operator_recovers_completed_tool_result_missing_from_terminal_copy(monkeypatch):
    completed = ToolExecution(
        tool_call_id="call_sql_1",
        tool_name="run_governed_sql",
        tool_args={"purpose": "Compare the months", "sql": "SELECT 1"},
        result={
            "kind": "governed_sql",
            "rows": [],
            "row_count": 0,
            "empty_result": True,
        },
    )
    terminal_copy = ToolExecution(
        tool_call_id="call_sql_1",
        tool_name="run_governed_sql",
        tool_args=completed.tool_args,
        result=None,
    )

    class StubOperator:
        def run(self, *_args, **_kwargs):
            return iter([
                ToolCallCompletedEvent(tool=completed),
                RunOutput(
                    content="No recorded expenses were found, so there is nothing to compare.",
                    tools=[terminal_copy],
                ),
            ])

    monkeypatch.setattr(agents, "build_operator", lambda *args, **kwargs: StubOperator())

    result = agents.run_operator(
        "Compare the last three months.",
        [],
        date(2026, 8, 14),
        "Asia/Kolkata",
        [],
        analysis_tools=[SimpleNamespace(name="run_governed_sql")],
    )

    assert [item.name for item in result.tool_grounding] == ["run_governed_sql"]
    assert result.tool_grounding[0].result.data["empty_result"] is True


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
