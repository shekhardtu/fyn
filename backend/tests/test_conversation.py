from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import Uuid, func, select

from app.api import delete_conversation, list_conversations
from app.config import get_settings
from app.database import Base
from app.models import AIAction, Account, AnalysisTool, AnalysisToolRun, Budget, Category, Conversation, DraftState, Goal, GoalContribution, Message, Subcategory, Tag, TaxonomyScope, Transaction, TransactionDraft, TransactionFieldValue, TransactionTag, User
from app.seed import default_user
from app.services.agents import ClarificationOption, ClarificationRequest, CopilotDecision, CopilotDecisionValidation, GovernedWorkflowHandoff, PresentationIntent, QueryBundleInterpretation, QueryInterpretation, QueryView, TaxonomyInterpretation, ToolGrounding, TransactionInterpretation, UnifiedReadResult
from app.services import conversation as conversation_service
from app.services.agui import execute_widget_action
from app.services.calculators import loan_amortization_schedule
from app.schemas import ActionRequest, PendingAction, Widget, WidgetAction, WidgetType
from app.services.conversation import get_or_create_conversation, handle_action, handle_chat


def test_missing_date_defaults_to_current_utc_instant_and_survives_amount_clarification(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    current = datetime(2026, 8, 12, 7, 55, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(conversation_service, "now_utc", lambda: current)

    draft = conversation_service._create_draft(db, user, conversation, "Add Transactions")
    assert draft.transaction_at == current

    response = handle_action(db, user, conversation, "update_transaction_draft", {
        "draftId": str(draft.id),
        "amountMinor": 50_000,
    })

    assert draft.transaction_at == current
    assert response.widgets[0].type == "transaction_type_selector"


def test_bare_amount_complete_conversation(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)

    response = handle_chat(db, user, conversation, "₹2,000")
    assert response.widgets[0].type == "transaction_type_selector"
    draft = db.scalar(select(TransactionDraft).where(TransactionDraft.conversation_id == conversation.id))
    assert draft.state == DraftState.NEEDS_CLARIFICATION.value
    assert draft.transaction_type == "unknown"

    response = execute_widget_action(ActionRequest(
        conversation_id=conversation.id,
        widget_id=response.widgets[0].id,
        action="select_transaction_type",
        payload={"draftId": str(draft.id), "optionId": "expense"},
    ), db, user)
    assert response.widgets[0].type == "category_selector"

    category_id = next(option["id"] for option in response.widgets[0].data["options"] if option["slug"] == "food")
    response = execute_widget_action(ActionRequest(
        conversation_id=conversation.id,
        widget_id=response.widgets[0].id,
        action="select_category",
        payload={"draftId": str(draft.id), "categoryId": category_id},
    ), db, user)
    assert response.widget_updates[0].widget.data["lifecycle"] == "completed"
    assert response.widget_updates[0].widget.data["completion"]["values"]["categoryId"] == category_id
    assert response.widgets[0].type == "subcategory_selector"

    subcategory_id = next(option["id"] for option in response.widgets[0].data["options"] if option["slug"] == "dining")
    response = execute_widget_action(ActionRequest(
        conversation_id=conversation.id,
        widget_id=response.widgets[0].id,
        action="select_subcategory",
        payload={"draftId": str(draft.id), "subcategoryId": subcategory_id},
    ), db, user)
    assert response.widget_updates[0].widget.data["lifecycle"] == "completed"
    assert response.widgets[0].type == "transaction_preview"
    assert "Added ₹2,000" in response.message
    assert [action.label for action in response.widgets[0].actions] == ["Edit", "Remove"]
    transaction = db.scalar(select(Transaction))
    assert transaction.amount_minor == 200_000
    assert transaction.status == "provisional"
    assert len(transaction.sources) == 1


def test_conflicting_loan_lineage_requires_human_choice_before_charting(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    conversation.active_analysis_state = {
        "sourceMessageId": str(uuid4()),
        "entityType": "calculator",
        "query": {
            "source_kind": "calculator",
            "tool": "loan_payment",
            "arguments": {
                "principal_minor": 10_000_000,
                "annual_rate_percent": 10,
                "tenure_months": 24,
            },
            "result_summary": {"emi_minor": 461_449, "tenure_months": 24},
        },
        "queries": [
            {
                "source_kind": "calculator",
                "tool": "loan_payment",
                "arguments": {
                    "principal_minor": 10_000_000,
                    "annual_rate_percent": 10,
                    "tenure_months": 24,
                },
                "result_summary": {"emi_minor": 461_449, "tenure_months": 24},
            },
            {
                "source_kind": "calculator",
                "tool": "amortize_with_fixed_payment",
                "arguments": {
                    "principal_minor": 10_000_000,
                    "annual_rate_percent": 10,
                    "payment_minor": 200_000,
                    "max_months": 1200,
                },
            },
        ],
        "resultShapes": [],
    }
    db.commit()

    response = handle_chat(
        db,
        user,
        conversation,
        "Can you draw on a chart with diminishing principal amount along with installment?",
    )

    assert response.widgets[0].type == "clarification"
    assert response.pending_action.action == "resolve_clarification"
    assert response.widgets[0].data["conflictFields"] == ["tenure", "monthly installment"]
    assert [option["id"] for option in response.widgets[0].data["options"]] == [
        "use_tenure",
        "use_installment",
        "compare_scenarios",
    ]
    assert "₹2,000" in response.widgets[0].data["reason"]
    assert "₹4,614.49" in response.widgets[0].data["reason"]


def test_active_draft_semantically_routes_subcategory_creation_with_state_context(db, monkeypatch):
    user = default_user(db)
    db.add(Category(slug="construction", name="Construction", icon="hammer"))
    db.commit()
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "₹500")
    draft = db.scalar(select(TransactionDraft).where(TransactionDraft.conversation_id == conversation.id))
    response = handle_action(db, user, conversation, "select_transaction_type", {"draftId": str(draft.id), "optionId": "expense"})
    construction_id = next(option["id"] for option in response.widgets[0].data["options"] if option["slug"] == "construction")
    handle_action(db, user, conversation, "select_category", {"draftId": str(draft.id), "categoryId": construction_id})
    captured = {}

    def semantic_router(*args, **kwargs):
        captured.update(kwargs.get("workflow_context") or {})
        return CopilotDecision(
            tool="manage_taxonomy",
            taxonomy=TaxonomyInterpretation(operation="create_subcategory", parent_category="Construction"),
            confidence=0.99,
            reason="The active draft needs a new subcategory.",
        )

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", semantic_router)
    response = handle_chat(db, user, conversation, "make a new type under this one")

    assert captured["missingFields"] == ["subcategory"]
    assert captured["selectedCategory"] == "Construction"
    assert "create_subcategory" in captured["allowedActions"]
    assert response.widgets[0].type == "taxonomy_editor"
    assert response.widgets[0].data["parentCategory"] == "Construction"

    response = execute_widget_action(ActionRequest(
        conversation_id=conversation.id,
        widget_id=response.widgets[0].id,
        action="create_subcategory",
        payload={
            "draftId": str(draft.id),
            "categoryId": construction_id,
            "name": "Materials",
        },
    ), db, user)
    assert response.widgets[0].type == "transaction_preview"
    assert response.widget_updates[0].widget.data["lifecycle"] == "completed"
    assert response.widget_updates[0].widget.data["name"] == "Materials"
    assert response.widget_updates[0].widget.actions == []
    persisted_editor = next(
        widget
        for message in conversation.messages
        for widget in message.widgets
        if widget.get("type") == "taxonomy_editor"
    )
    assert persisted_editor["data"]["name"] == "Materials"
    assert persisted_editor["data"]["lifecycle"] == "completed"
    transaction = db.scalar(select(Transaction))
    assert db.get(Subcategory, transaction.subcategory_id).name == "Materials"


def test_every_semantic_turn_receives_five_complete_unclipped_turns(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    routed_contexts = []
    long_reply = "Complete answer: " + ("context " * 120)

    def router(*args, **kwargs):
        routed_contexts.append(list(args[4]))
        reply = long_reply if args[0] == "sixth" else f"Answer to {args[0]}."
        return CopilotDecision(tool="conversation", reply=reply, confidence=0.99, reason="Conversation turn.")

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", router)
    monkeypatch.setattr(
        conversation_service,
        "validate_copilot_decision",
        lambda *args, **kwargs: pytest.fail("Low-risk conversation turns must not call the model validator"),
    )

    for prompt in ("first", "second", "third", "fourth", "fifth", "sixth", "seventh"):
        handle_chat(db, user, conversation, prompt)

    expected = [
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "Answer to second."},
        {"role": "user", "content": "third"},
        {"role": "assistant", "content": "Answer to third."},
        {"role": "user", "content": "fourth"},
        {"role": "assistant", "content": "Answer to fourth."},
        {"role": "user", "content": "fifth"},
        {"role": "assistant", "content": "Answer to fifth."},
        {"role": "user", "content": "sixth"},
        {"role": "assistant", "content": long_reply},
    ]
    assert routed_contexts[-1] == expected
    assert len(routed_contexts[-1][-1]["content"]) > 500


def test_category_count_uses_authenticated_runtime_taxonomy_tool(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    captured = {}

    def router(*args, **kwargs):
        taxonomy_tool = next(
            tool
            for tool in kwargs["runtime_tools"]
            if tool.name == "read_user_expense_taxonomy"
        )
        taxonomy = taxonomy_tool.entrypoint()
        captured["tool_schema"] = taxonomy_tool.parameters
        return CopilotDecision(
            tool="conversation",
            # Deliberately wrong: the persisted answer must be derived from the
            # authenticated result envelope, not trusted model prose.
            reply="You have 999 expense categories.",
            tool_grounding=[ToolGrounding(
                name=taxonomy_tool.name,
                arguments={},
                result=str(taxonomy),
            )],
            confidence=0.99,
            reason="The authenticated taxonomy tool returned the visible category inventory.",
        )

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", router)
    monkeypatch.setattr(
        conversation_service,
        "validate_copilot_decision",
        lambda *args, **kwargs: pytest.fail("Runtime-grounded answers must not be rerouted by a second model"),
    )

    response = handle_chat(db, user, conversation, "How many categories are there?")

    assert response.message == (
        "You have 11 expense categories: Bills, Education, Entertainment, Food, Health, Housing, Other, "
        "Personal care, Shopping, Transport, Travel."
    )
    assert response.citations[0].entity_type == "runtime_tool"
    assert response.citations[0].label == "Read User Expense Taxonomy result"
    assert captured["tool_schema"]["properties"] == {}


def test_runtime_grounded_reply_replaces_a_number_missing_from_tool_evidence(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(conversation_service, "_fast_path_decision", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="conversation",
            reply="You spent ₹999 across 2 transactions.",
            tool_grounding=[ToolGrounding(
                name="spending_summary",
                arguments={"start": "2026-08-01", "end": "2026-08-14", "category_slug": None},
                result={
                    "total_minor": 12_000,
                    "count": 2,
                    "currency": "INR",
                    "start": "2026-08-01",
                    "end": "2026-08-14",
                    "category": None,
                },
            )],
            confidence=0.99,
            reason="Authenticated spending summary.",
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "validate_copilot_decision",
        lambda *args, **kwargs: pytest.fail("Runtime-grounded answers use deterministic evidence policy"),
    )

    response = handle_chat(db, user, conversation, "How much did I spend?")

    assert "₹999" not in response.message
    assert "₹120" in response.message
    assert "2 transactions" in response.message
    assert response.citations[0].query["tool"] == "spending_summary"


def test_computed_calculator_dataset_renders_through_generic_visualization(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    result = loan_amortization_schedule(520_000_000, 7.2, 240)

    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="visualize_computation",
            presentation=PresentationIntent(
                mode="chart",
                unit_of_analysis="installment",
                requested_mark="line",
                x_field="installment",
                y_fields=["principal_payment_minor", "remaining_principal_minor"],
            ),
            tool_grounding=[ToolGrounding(
                name="loan_amortization_schedule",
                arguments={
                    "principal_minor": 520_000_000,
                    "annual_rate_percent": 7.2,
                    "tenure_months": 240,
                },
                result=str(result),
            )],
            confidence=0.99,
            reason="The authenticated calculator returned the requested installment dataset.",
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "validate_copilot_decision",
        lambda *args, **kwargs: pytest.fail("Authenticated computed datasets use runtime tool policy"),
    )

    response = handle_chat(db, user, conversation, "Chart principal paid and balance after every EMI")

    assert response.widgets[0].type == "data_visualization"
    data = response.widgets[0].data
    assert len(next(iter(data["datasets"].values()))) == 480
    view = data["views"][0]
    assert view["mark"] == "line"
    assert view["encoding"]["x"]["field"] == "installment"
    assert view["encoding"]["y"]["field"] == "value"
    assert view["encoding"]["color"]["field"] == "measure"
    assert response.citations[0].entity_type == "calculator"
    assert response.citations[0].query["arguments"]["tenure_months"] == 240
    db.refresh(conversation)
    assert conversation.active_analysis_state["query"]["source_kind"] == "calculator"
    assert conversation.active_analysis_state["query"]["tool"] == "loan_amortization_schedule"


def test_calculator_state_is_available_to_the_next_semantic_turn_without_phrase_gating(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    captured_contexts = []
    calls = 0

    def router(*args, **kwargs):
        nonlocal calls
        calls += 1
        captured_contexts.append(kwargs.get("workflow_context"))
        if calls == 1:
            return CopilotDecision(
                tool="conversation",
                reply="The EMI is ₹10,000.",
                tool_grounding=[ToolGrounding(
                    name="loan_payment",
                    arguments={"principal_minor": 1_200_000, "annual_rate_percent": 0, "tenure_months": 12},
                    result=str({
                        "emi_minor": 100_000,
                        "total_payment_minor": 1_200_000,
                        "total_interest_minor": 0,
                        "tenure_months": 12,
                    }),
                )],
                confidence=0.99,
                reason="Authenticated calculation.",
            )
        return CopilotDecision(tool="conversation", reply="Ready.", confidence=0.99, reason="Context check.")

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", router)
    monkeypatch.setattr(
        conversation_service,
        "validate_copilot_decision",
        lambda *args, **kwargs: CopilotDecisionValidation(outcome="approve", confidence=0.99, summary="Valid."),
    )

    handle_chat(db, user, conversation, "Calculate this loan")
    handle_chat(db, user, conversation, "Create a useful visual")

    state = captured_contexts[-1]["activeAnalysisState"]
    assert state["query"]["source_kind"] == "calculator"
    assert state["query"]["arguments"]["tenure_months"] == 12


def test_ungrounded_model_financial_figure_is_never_approved(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="conversation",
            reply="Your EMI is ₹99,999.",
            confidence=0.99,
            reason="Model-authored calculation without a tool.",
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "validate_copilot_decision",
        lambda *args, **kwargs: CopilotDecisionValidation(outcome="approve", confidence=0.99, summary="Incorrect approval."),
    )

    response = handle_chat(db, user, conversation, "Calculate my EMI")

    assert "₹99,999" not in response.message
    assert "couldn’t safely" in response.message or "couldn’t validate" in response.message
    assert response.citations == []


def test_rejected_read_only_analysis_never_falls_through_to_transaction_creation(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    before = db.scalar(select(func.count()).select_from(Transaction).where(Transaction.user_id == user.id))

    def router(*args, **kwargs):
        return CopilotDecision(
            tool="run_analysis_harness",
            confidence=0.99,
            reason="The user requested a read-only heatmap.",
        )

    def reject(*args, **kwargs):
        return CopilotDecisionValidation(
            outcome="reject",
            confidence=0.99,
            issues=["Heatmap contract is incomplete."],
            summary="Reject the incomplete read-only analysis.",
        )

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", router)
    monkeypatch.setattr(conversation_service, "validate_copilot_decision", reject)

    response = handle_chat(db, user, conversation, "Show a heatmap by timeshift of last 3 days")
    after = db.scalar(select(func.count()).select_from(Transaction).where(Transaction.user_id == user.id))

    assert after == before
    assert response.widgets == []
    # The refusal has to name what actually failed. The generic version of this
    # message left users retyping the same prompt with nothing to correct.
    assert "heatmap contract is incomplete" in response.message.casefold()
    assert "Nothing was created or changed." in response.message


def test_ambiguous_addition_prefers_hitl_transaction_type_selector(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="create_transaction_draft",
            transaction=TransactionInterpretation(
                transaction_type="unknown",
                amount_minor=50_000,
                transaction_date=date.today(),
                explicit_fields=["amount"],
                confidence=0.8,
            ),
            confidence=0.9,
            reason="The amount is known but the event type requires HITL.",
        ),
    )

    response = handle_chat(db, user, conversation, "Add 500")

    assert response.widgets[0].type == "transaction_type_selector"
    assert response.pending_action.action == "select_transaction_type"
    assert db.scalar(select(Transaction)) is None
    draft_id = response.widgets[0].data["draftId"]

    response = handle_action(db, user, conversation, "select_transaction_type", {"draftId": draft_id, "optionId": "income"})
    assert response.widgets[0].type == "transaction_preview"
    transaction = db.scalar(select(Transaction))
    assert transaction.transaction_type == "income"
    assert transaction.amount_minor == 50_000


def test_greeting_never_creates_a_transaction_draft_or_calls_llm(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM should not be called")),
    )
    activity = []

    response = handle_chat(db, user, conversation, "Hi", activity.append)

    assert "Hi" in response.message
    assert response.widgets == []
    assert db.scalar(select(TransactionDraft)) is None
    assert next(event for event in activity if event["id"] == "classification")["durationMs"] == 0


def test_model_enabled_acknowledgement_uses_recent_context_instead_of_template(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Hi")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    captured = {}

    def unified_reader(text, _taxonomy, _today, _timezone, recent_context, **kwargs):
        captured["text"] = text
        captured["context"] = recent_context
        kwargs["on_delta"]("Got it — I’ll wait for your next request.")
        return UnifiedReadResult(
            reply="Got it — I’ll wait for your next request.",
            streamed_live=True,
        )

    monkeypatch.setattr(conversation_service, "run_unified_read_agent", unified_reader)
    monkeypatch.setattr(
        conversation_service,
        "stream_conversation_reply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a direct unified reply must not enter the second writer")
        ),
    )
    deltas = []

    response = handle_chat(
        db,
        user,
        conversation,
        "OK",
        text_delta_callback=lambda message_id, delta: deltas.append((message_id, delta)),
    )

    assert captured["text"] == "OK"
    assert captured["context"][-1]["content"].startswith("Hi!")
    assert response.message == "Got it — I’ll wait for your next request."
    assert "What would you like to look at next?" not in response.message
    assert "".join(delta for _message_id, delta in deltas) == response.message
    get_settings.cache_clear()


def test_repeated_assistant_text_is_recognized_without_router_or_echo(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    previous = handle_chat(db, user, conversation, "Hi").message
    repeated_input = f"Earlier you said: {previous}"
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("exact repeated context should not need the semantic router")),
    )

    def unified_reader(text, _taxonomy, _today, _timezone, recent_context, **kwargs):
        assert text == repeated_input
        assert recent_context[-1]["content"] == previous
        return UnifiedReadResult(
            reply="It looks like you pasted my last reply back to me. Were you testing context, or did you want to continue from there?"
        )

    monkeypatch.setattr(conversation_service, "run_unified_read_agent", unified_reader)
    monkeypatch.setattr(
        conversation_service,
        "stream_conversation_reply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a direct unified reply must not enter the second writer")
        ),
    )

    response = handle_chat(db, user, conversation, repeated_input)

    assert response.message != repeated_input
    assert "pasted my last reply" in response.message
    get_settings.cache_clear()


def test_unified_grounded_read_persists_and_emits_the_same_verified_reply(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    monkeypatch.setenv("UNIFIED_READ_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    grounding = ToolGrounding(
        name="spending_summary",
        arguments={"start": "2026-08-01", "end": "2026-08-14", "category_slug": None},
        result={
            "tool": "spending_summary",
            "schema_name": "SpendingSummaryResult",
            "data": {
                "total_minor": 125_000,
                "count": 3,
                "currency": "INR",
                "start": "2026-08-01",
                "end": "2026-08-14",
            },
        },
    )
    monkeypatch.setattr(
        conversation_service,
        "run_unified_read_agent",
        lambda *args, **kwargs: UnifiedReadResult(
            reply="You spent ₹1,250 across 3 transactions from August 1 through August 14.",
            tool_grounding=[grounding],
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "stream_conversation_reply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a verified unified read must not enter the second writer")
        ),
    )
    deltas = []

    response = handle_chat(
        db,
        user,
        conversation,
        "How much did I spend this month?",
        text_delta_callback=lambda message_id, delta: deltas.append((message_id, delta)),
    )

    assert response.message == "You spent ₹1,250 across 3 transactions from August 1 through August 14."
    assert "".join(delta for _message_id, delta in deltas) == response.message
    assert response.citations[0].query["tool"] == "spending_summary"
    persisted = db.get(Message, response.message_id)
    assert persisted.content == response.message
    get_settings.cache_clear()


def test_correction_handoff_preserves_prior_query_lineage_and_skips_duplicate_models(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    first = handle_chat(db, user, conversation, "Spent ₹700 on rent today")
    second = handle_chat(db, user, conversation, "Spent ₹900 on rent today")
    monkeypatch.setattr(conversation_service, "_fast_path_decision", lambda *args, **kwargs: None)

    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="search_transactions",
            query=QueryInterpretation(
                metric="spending_summary",
                result_mode="summary",
                operation="total",
                transaction_type="expense",
                category_slug="housing",
                limit=100,
            ),
            confidence=0.99,
            reason="Read all recorded Housing spending.",
        ),
    )
    summary = handle_chat(db, user, conversation, "How much have I spent on Housing across all time?")
    assert summary.citations[0].query["category_slug"] == "housing"
    assert "start_date" not in summary.citations[0].query

    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    monkeypatch.setenv("UNIFIED_READ_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    captured = {}

    def unified_reader(text, _taxonomy, _today, _timezone, recent_context, **kwargs):
        captured["context"] = recent_context
        captured["workflow"] = kwargs["workflow_context"]
        prior = recent_context[-1]["grounding"]["queries"][0]
        assert prior["category_slug"] == "housing"
        assert "start_date" not in prior
        return UnifiedReadResult(handoff=GovernedWorkflowHandoff(
            workflow="advanced_analysis",
            reason="The correction asks for the individual records behind the all-time Housing total.",
            resolved_request="List every Housing expense across all recorded time.",
            query=QueryInterpretation(
                metric="transaction_summary",
                result_mode="transaction_list",
                operation="list",
                transaction_type="expense",
                category_slug="housing",
                # Deliberately repeat the bad month-to-date default. The domain
                # correction postcondition must restore the matching all-time
                # grounded scope before execution.
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 14),
                limit=100,
            ),
        ))

    monkeypatch.setattr(conversation_service, "run_unified_read_agent", unified_reader)
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a complete typed handoff must not enter a second router")
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "stream_conversation_reply",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a governed read result must not enter a second prose writer")
        ),
    )

    response = handle_chat(
        db,
        user,
        conversation,
        "But I meant the Housing expenses behind that total—show the list.",
    )

    assert captured["workflow"]["correctionRequested"] is True
    assert "matching prior all-time filters" in response.message
    assert response.widgets[0].type == "data_table"
    assert {row["id"] for row in response.widgets[0].data["rows"]} == {
        first.widgets[0].data["transactionId"],
        second.widgets[0].data["transactionId"],
    }
    assert response.citations[0].query["category_slug"] == "housing"
    assert "start_date" not in response.citations[0].query
    action = db.scalar(select(AIAction).where(AIAction.action_type == "unified_typed_handoff"))
    assert action.payload_redacted["queryShape"]["resultMode"] == "transaction_list"
    get_settings.cache_clear()


def test_independent_top_n_handoff_releases_prior_direction_and_preserves_limit(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Got ₹2,000 salary today")
    handle_chat(db, user, conversation, "Received a ₹1,500 refund today")
    handle_chat(db, user, conversation, "Spent ₹1,000 at Toit today")
    handle_chat(db, user, conversation, "Spent ₹500 at Starbucks today")

    month_start = date.today().replace(day=1)
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="search_transactions",
            query=QueryInterpretation(
                result_mode="transaction_list",
                operation="list",
                transaction_type="expense",
                start_date=month_start,
                end_date=date.today(),
                limit=100,
            ),
            confidence=0.99,
            reason="List this month's expenses.",
        ),
    )
    prior = handle_chat(db, user, conversation, "Show this month's expenses")
    assert {row["transactionType"] for row in prior.widgets[0].data["rows"]} == {"Expense"}

    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    monkeypatch.setenv("UNIFIED_READ_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    def independent_handoff(*args, **kwargs):
        # Prior state remains available for calculators and real follow-ups;
        # the domain postcondition below is what prevents implicit inheritance.
        assert kwargs["workflow_context"]["activeAnalysisState"] is not None
        return UnifiedReadResult(handoff=GovernedWorkflowHandoff(
            workflow="advanced_analysis",
            reason="Rank the requested individual transaction records.",
            resolved_request="Find the top 3 transactions in August.",
            query=QueryInterpretation(
                metric="transaction_summary",
                result_mode="transaction_list",
                operation="rank",
                group_by="none",
                sort_direction="desc",
                # Simulate the production mistake: the agent copied expense
                # from the prior analysis even though this prompt did not.
                transaction_type="expense",
                start_date=month_start,
                end_date=date.today(),
                limit=3,
            ),
        ))

    monkeypatch.setattr(conversation_service, "run_unified_read_agent", independent_handoff)
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a complete typed handoff must not enter a second router")
        ),
    )

    response = handle_chat(db, user, conversation, "Can you find me top 3 transaction list of Agust?")

    assert response.message == "I found the top 3 matching transactions, ranked by amount."
    assert response.widgets[0].data["title"] == "Top 3 matching transactions"
    assert [row["amountMinor"] for row in response.widgets[0].data["rows"]] == [200_000, 150_000, 100_000]
    assert [row["transactionType"] for row in response.widgets[0].data["rows"]] == ["Income", "Refund", "Expense"]
    assert response.citations[0].query["limit"] == 3
    assert response.citations[0].query["start_date"] == month_start.isoformat()
    assert response.citations[0].query["end_date"] == date.today().isoformat()
    assert "transaction_type" not in response.citations[0].query
    get_settings.cache_clear()


def test_unified_clarification_handoff_compiles_without_a_second_router(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    clarification = ClarificationRequest(
        question="Which account should fund this transfer?",
        reason="Two source accounts were supplied and choosing one changes the transaction.",
        conflict_fields=["source account"],
        options=[
            ClarificationOption(id="salary", label="Salary account", resolution="Use the Salary account."),
            ClarificationOption(id="savings", label="Savings account", resolution="Use the Savings account."),
        ],
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    monkeypatch.setenv("UNIFIED_READ_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        conversation_service,
        "run_unified_read_agent",
        lambda *args, **kwargs: UnifiedReadResult(handoff=GovernedWorkflowHandoff(
            workflow="clarification",
            reason="The source account is ambiguous.",
            clarification=clarification,
        )),
    )
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a complete clarification handoff must not enter a second router")
        ),
    )

    response = handle_chat(db, user, conversation, "Transfer ₹5,000 from Salary or Savings")

    assert response.widgets[0].type == "clarification"
    assert response.pending_action.action == "resolve_clarification"
    assert response.widgets[0].data["question"] == clarification.question
    get_settings.cache_clear()


def test_unified_mutation_handoff_keeps_confirmation_pipeline_in_control(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    monkeypatch.setenv("UNIFIED_READ_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        conversation_service,
        "run_unified_read_agent",
        lambda *args, **kwargs: UnifiedReadResult(
            handoff=GovernedWorkflowHandoff(
                workflow="transaction",
                reason="Recording an expense requires the confirmed transaction workflow.",
            )
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="create_transaction_draft",
            transaction=TransactionInterpretation(
                transaction_type="expense",
                amount_minor=50_000,
                currency="INR",
                explicit_fields=["transaction_type", "amount"],
                confidence=0.99,
            ),
            confidence=0.99,
            reason="Create a governed expense draft.",
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "validate_copilot_decision",
        lambda *args, **kwargs: CopilotDecisionValidation(
            outcome="approve",
            confidence=1,
            summary="The transaction draft matches the request.",
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "stream_conversation_reply",
        lambda _text, draft_reply, _context, **_kwargs: draft_reply,
    )

    response = handle_chat(db, user, conversation, "Record an expense for ₹500")

    assert response.widgets[0].type in {"category_selector", "transaction_preview"}
    assert db.scalar(select(Transaction)) is None
    assert db.scalar(select(TransactionDraft)) is not None
    handoff_action = db.scalar(
        select(AIAction).where(AIAction.action_type == "unified_read_handoff")
    )
    assert handoff_action.payload_redacted["workflow"] == "transaction"
    get_settings.cache_clear()


def test_unified_agent_cannot_stream_a_mutation_claim_instead_of_handoff(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    monkeypatch.setenv("UNIFIED_READ_AGENT_ENABLED", "true")
    get_settings.cache_clear()

    def invalid_direct_reply(*_args, **kwargs):
        kwargs["on_delta"]("I added the expense.")
        return UnifiedReadResult(reply="I added the expense.", streamed_live=True)

    monkeypatch.setattr(conversation_service, "run_unified_read_agent", invalid_direct_reply)

    with pytest.raises(RuntimeError, match="authority postcondition"):
        handle_chat(
            db,
            user,
            conversation,
            "Record an expense for ₹500",
            text_delta_callback=lambda _message_id, _delta: None,
        )

    assert db.scalar(select(Transaction)) is None
    assert list(
        db.scalars(
            select(Message).where(
                Message.conversation_id == conversation.id,
                Message.role == "assistant",
                Message.content == "",
            )
        )
    ) == []
    get_settings.cache_clear()


def test_old_unresolved_draft_stops_contaminating_later_conversation(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    first = handle_chat(db, user, conversation, "Spent ₹500")
    assert first.widgets[0].data["draftId"]

    # The first unrelated turn can explicitly move away from the visible
    # clarification. Once its answer is latest, that older audit row is no
    # longer the active workflow for every future prompt.
    handle_chat(db, user, conversation, "Hi")
    activity = []
    response = handle_chat(db, user, conversation, "How are you?", activity.append)

    assert response.message
    assert not any(event["label"] == "Resumed transaction workflow" for event in activity)


def test_analysis_language_about_savings_is_not_misrouted_to_goal_crud():
    assert not conversation_service._looks_like_planning_command(
        "Let's discuss my expense pattern in detail so I can improve savings"
    )
    assert conversation_service._looks_like_planning_command("Create a ₹2 lakh savings goal")


def test_correction_language_is_marked_for_scope_reconciliation():
    assert conversation_service._is_correction_followup("No, I meant the Housing expenses")
    assert conversation_service._is_correction_followup("But that does not match what you said")
    assert not conversation_service._is_correction_followup("Show Housing expenses this month")


def test_expense_pattern_savings_request_runs_contextual_governed_analysis(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹300 on ice cream today")
    handle_chat(db, user, conversation, "Spent ₹100 on a cab today")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    monkeypatch.setenv("UNIFIED_READ_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        conversation_service,
        "run_unified_read_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a known governed analysis should not pay for a handoff model")
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("known expense-pattern analysis should not wait for the semantic router")
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "stream_conversation_reply",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a verified analysis result should not enter a second prose writer")
        ),
    )

    response = handle_chat(
        db,
        user,
        conversation,
        "Let's discuss my expense pattern in details so convince on savings",
    )

    assert response.widgets[0].type == "analysis_table"
    assert response.widgets[0].data["rows"]
    assert "recorded expenses" in response.message
    assert "Food" in response.message
    assert all(widget.type != "goal_progress" for widget in response.widgets)
    get_settings.cache_clear()


def test_every_grounded_reply_uses_contextual_writer_but_rejects_new_numbers(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    captured = {}

    def unsupported_writer(text, draft_reply, recent_context, **kwargs):
        captured["grounded"] = kwargs["grounded_response"]
        captured["draft"] = draft_reply
        captured["context"] = kwargs["response_context"]
        return "I prepared a ₹999 transaction draft."

    monkeypatch.setattr(conversation_service, "stream_conversation_reply", unsupported_writer)

    response = handle_chat(db, user, conversation, "₹500")

    assert captured["grounded"] is True
    assert captured["context"]["widgets"]
    assert "₹999" not in response.message
    assert response.message == captured["draft"]
    get_settings.cache_clear()


def test_failed_analysis_reroute_cannot_fall_through_to_unrelated_planning(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    prompt = Message(
        conversation_id=conversation.id,
        role="user",
        content="Discuss my expense pattern so I can save more",
        widgets=[],
        citations=[],
    )
    db.add(prompt)
    db.flush()
    calls = 0

    def router(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return CopilotDecision(
                tool="run_analysis_harness",
                confidence=0.9,
                reason="Analyse recorded expense patterns.",
            )
        raise ValueError("invalid stronger contract")

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", router)
    monkeypatch.setattr(
        conversation_service,
        "validate_copilot_decision",
        lambda *args, **kwargs: CopilotDecisionValidation(
            outcome="reject",
            confidence=0.99,
            issues=["The governed analysis needs a valid presentation contract."],
            summary="The analysis contract is invalid.",
        ),
    )

    decision = conversation_service._interpret_prompt(
        db,
        user,
        conversation,
        prompt,
        prompt.content,
    )

    assert calls == 2
    assert decision.tool == "unknown"
    assert "couldn’t draw" in decision.reply
    assert "savings goal" not in decision.reply


def test_validator_human_input_outcome_becomes_typed_clarification(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    prompt = Message(
        conversation_id=conversation.id,
        role="user",
        content="Set a savings plan using either my target or monthly contribution",
        widgets=[],
        citations=[],
    )
    db.add(prompt)
    db.flush()
    clarification = ClarificationRequest(
        question="Which value should control the savings plan?",
        reason="The target and contribution imply different schedules.",
        conflict_fields=["target", "monthly contribution"],
        options=[
            ClarificationOption(id="use_target", label="Use target", resolution="Use the target as authoritative."),
            ClarificationOption(id="use_contribution", label="Use contribution", resolution="Use the contribution as authoritative."),
        ],
        allow_custom=True,
    )
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="planning",
            confidence=0.9,
            reason="A planning workflow is required.",
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "validate_copilot_decision",
        lambda *args, **kwargs: CopilotDecisionValidation(
            outcome="request_human_input",
            confidence=0.99,
            issues=["The controlling savings input is ambiguous."],
            summary="Ask the customer which input should control the plan.",
            clarification=clarification,
        ),
    )

    decision = conversation_service._interpret_prompt(
        db,
        user,
        conversation,
        prompt,
        prompt.content,
    )

    assert decision.tool == "request_clarification"
    assert decision.clarification == clarification
    assert decision.validated_by == get_settings().validator_model


def test_chat_reports_safe_timed_agent_activity(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    activity = []

    handle_chat(db, user, conversation, "How much did I spend this month?", activity.append)

    latest = {event["id"]: event for event in activity}
    assert latest["classification"]["status"] == "completed"
    assert latest["classification"]["tool"] == "get_spending_summary"
    assert latest["execution"]["tool"] == "get_spending_summary"
    assert latest["execution"]["durationMs"] >= 0
    assert latest["grounding"]["detail"] == "1 structured data source"
    assert latest["grounding"]["cumulativeMs"] >= latest["execution"]["cumulativeMs"]


def test_known_complex_comparison_uses_validated_offline_fallback_after_agent_failure(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹300 on ice cream today")
    handle_chat(db, user, conversation, "Spent ₹100 on a cab today")
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated model outage")),
    )
    activity = []
    response = handle_chat(db, user, conversation, "Compare food and transport this month. Which is larger and by how much?", activity.append)
    assert response.message == "Food is larger at ₹300, compared with ₹100 for Transport; the difference is ₹200."
    assert response.widgets[0].type == "analysis_table"
    assert all(widget.data.get("eyebrow") != "Validated analysis capability" for widget in response.widgets)
    assert any(event.get("badge") == "Saved" for event in activity)
    assert any(event.get("badge") == "Validated" for event in activity)
    assert any(event["id"] == "classification" and event["label"] == "Agno is reasoning and planning" for event in activity)
    assert any(event["id"] == "classification" and event["label"] == "Offline capability compiler selected a validated plan" for event in activity)


def test_llm_classifier_routes_to_grounded_tool_without_using_template_keywords(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹400 on a cab today")
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="get_spending_summary",
            query=QueryInterpretation(metric="spending_summary", category_slug="transport", start_date=date.today(), end_date=date.today()),
            confidence=0.98,
            reason="User asks for today's mobility costs",
        ),
    )

    response = handle_chat(db, user, conversation, "What went on moving around today?")

    assert response.widgets[0].type == "financial_summary"
    assert response.widgets[0].data["title"] == "Transport · Today"
    assert response.widgets[0].data["amountMinor"] == 40_000


def test_typed_query_route_cannot_be_overridden_by_prompt_keywords(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    decision = CopilotDecision(
        tool="get_spending_summary",
        query=QueryInterpretation(
            metric="spending_summary",
            start_date=date.today(),
            end_date=date.today(),
        ),
        confidence=0.99,
        reason="The user requested a spending total.",
    )

    response = conversation_service._query_response(
        db, user, conversation, "Total spend including subscription purchases", decision
    )

    assert response.widgets[0].type == "financial_summary"
    assert response.widgets[0].data["count"] == 0


def test_llm_classifier_can_supply_a_structured_transaction(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="create_transaction_draft",
            transaction=TransactionInterpretation(transaction_type="expense", amount_minor=20_000, transaction_date=date.today(), category_slug="food", subcategory_slug="ice_cream", confidence=0.97),
            confidence=0.97,
            reason="A completed purchase event",
        ),
    )

    response = handle_chat(db, user, conversation, "two hundred for a frozen dessert")

    assert response.widgets[0].type == "transaction_preview"
    assert response.widgets[0].data["amountMinor"] == 20_000
    assert response.widgets[0].data["category"] == "Food"


def test_fast_gate_handles_bare_amount_without_calling_llm(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM should not be called")),
    )

    activity = []
    response = handle_chat(db, user, conversation, "₹1,234", activity.append)

    assert response.widgets[0].type == "transaction_type_selector"
    assert next(event for event in reversed(activity) if event["id"] == "classification")["tool"] == "create_transaction_draft"
    assert "Detected a standalone amount" in next(event for event in reversed(activity) if event["id"] == "classification")["detail"]


def test_rich_entry_is_applied_without_clarification(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Spent ₹2,000 at Toit yesterday.")
    assert response.widgets[0].type == "transaction_preview"
    assert response.widgets[0].data["title"] == "Toit"
    assert response.widgets[0].data["category"] == "Food"
    assert response.widgets[0].data["subcategory"] == "Dining"


def test_income_is_understood(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Salary ₹3 lakh credited today.")
    assert response.widgets[0].type == "transaction_preview"
    assert response.widgets[0].data["transactionType"] == "income"
    assert response.widgets[0].data["amountMinor"] == 30_000_000


def test_user_can_edit_an_automatically_saved_transaction(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Spent ₹2,000 at Toit today")
    transaction_id = response.widgets[0].data["transactionId"]
    response = handle_action(db, user, conversation, "edit_saved_transaction", {"transactionId": transaction_id})
    assert response.widgets[0].type == "transaction_edit"
    response = handle_action(db, user, conversation, "update_saved_transaction", {"transactionId": transaction_id, "amountMinor": 225_000, "merchant": "Toit Indiranagar", "transactionAt": "2026-08-09T00:00:00Z", "categoryId": response.widgets[0].data["categoryId"], "subcategoryId": response.widgets[0].data["subcategoryId"]})
    assert response.widgets[0].type == "transaction_preview"
    assert response.widgets[0].data["amountMinor"] == 225_000
    assert response.widgets[0].data["title"] == "Toit Indiranagar"
    assert response.widgets[0].data["transactionAt"] == datetime(2026, 8, 9, tzinfo=timezone.utc)


def test_user_can_cancel_saved_transaction_edit_without_mutation(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    created = handle_chat(db, user, conversation, "Spent ₹2,000 at Toit today")
    transaction_id = created.widgets[0].data["transactionId"]

    edit = handle_action(db, user, conversation, "edit_saved_transaction", {"transactionId": transaction_id})
    assert [action.action for action in edit.widgets[0].actions] == [
        "update_saved_transaction",
        "cancel_saved_transaction_edit",
    ]

    cancelled = handle_action(db, user, conversation, "cancel_saved_transaction_edit", {"transactionId": transaction_id})

    transaction = db.get(Transaction, UUID(transaction_id))
    assert transaction.amount_minor == 200_000
    assert transaction.merchant_name == "Toit"
    assert cancelled.message == "No changes were made."
    assert cancelled.widgets[0].type == "transaction_preview"
    assert cancelled.widgets[0].data["status"] == "Saved"


def test_financial_query_uses_database(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹2,000 at Toit today")
    response = handle_chat(db, user, conversation, "How much did I spend on food this month?")
    assert "₹2,000" in response.message
    assert response.widgets[0].type == "financial_summary"
    assert response.widgets[0].data["amountMinor"] == 200_000
    assert response.citations


def test_how_many_rupees_last_two_days_is_a_grounded_query(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)

    for text in (
        "Spent ₹500 at Toit today",
        "Spent ₹700 at Toit yesterday",
        "Spent ₹900 at Toit day before yesterday",
    ):
        handle_chat(db, user, conversation, text)

    response = handle_chat(db, user, conversation, "How many rupees spend we last two days?")

    assert response.widgets[0].type == "financial_summary"
    assert response.widgets[0].data["amountMinor"] == 120_000
    assert response.widgets[0].data["count"] == 2
    assert response.widgets[0].data["title"] == "Spending · Last 2 days"
    assert "₹1,200" in response.message
    assert response.citations[0].query["start"] == (date.today() - timedelta(days=1)).isoformat()


def test_ice_cream_entry_is_ready_with_inferred_food_category(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)

    response = handle_chat(db, user, conversation, "200 rupess for ice cream")

    assert response.widgets[0].type == "transaction_preview"
    assert response.widgets[0].data["amountMinor"] == 20_000
    assert response.widgets[0].data["category"] == "Food"
    assert response.widgets[0].data["subcategory"] == "Ice cream"
    assert response.pending_action is None
    assert db.scalar(select(Transaction)).amount_minor == 20_000


def test_transaction_persists_tags_location_spend_nature_and_field_provenance(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)

    response = handle_chat(db, user, conversation, "Spent ₹200 on ice cream in Bengaluru today #vacation discretionary")

    transaction = db.scalar(select(Transaction))
    assert transaction.location_label == "Bengaluru"
    assert transaction.location_source == "user"
    assert transaction.spend_nature == "discretionary"
    assert response.widgets[0].data["location"] == "Bengaluru"
    assert response.widgets[0].data["tags"] == ["Vacation"]
    tag = db.scalar(select(Tag).join(TransactionTag, TransactionTag.tag_id == Tag.id))
    assert tag.normalized_name == "vacation"
    provenance = list(db.scalars(select(TransactionFieldValue).where(TransactionFieldValue.transaction_id == transaction.id)))
    assert next(item for item in provenance if item.field_name == "location").origin == "explicit"
    assert next(item for item in provenance if item.field_name == "category_id").origin == "inferred"


def test_structured_transaction_edit_records_user_corrections(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    transaction_id = handle_chat(db, user, conversation, "₹250 for coffee").widgets[0].data["transactionId"]
    edit = handle_action(db, user, conversation, "edit_saved_transaction", {"transactionId": transaction_id})
    response = handle_action(db, user, conversation, "update_saved_transaction", {
        "transactionId": transaction_id,
        "amountMinor": 30_000,
        "merchant": "Local Coffee",
        "transactionAt": datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc).isoformat(),
        "transactionType": "expense",
        "location": "Indiranagar",
        "spendNature": "discretionary",
        "tags": ["friends", "weekend"],
        "categoryId": edit.widgets[0].data["categoryId"],
        "subcategoryId": edit.widgets[0].data["subcategoryId"],
    })
    transaction = db.get(Transaction, UUID(transaction_id))
    assert transaction.location_label == "Indiranagar"
    assert transaction.spend_nature == "discretionary"
    assert response.widgets[0].data["tags"] == ["Friends", "Weekend"]
    corrections = list(db.scalars(select(TransactionFieldValue).where(
        TransactionFieldValue.transaction_id == transaction.id,
        TransactionFieldValue.origin == "user_correction",
    )))
    assert {item.field_name for item in corrections} >= {"amount_minor", "location", "spend_nature", "tags"}


def test_rich_transfer_resolves_accounts_when_automatically_applied(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Moved ₹30,000 from HDFC to SBI today")
    assert response.widgets[0].type == "transaction_preview"
    transaction = db.scalar(select(Transaction))
    accounts = list(db.scalars(select(Account).order_by(Account.name)))
    assert [account.name for account in accounts] == ["HDFC", "SBI"]
    assert transaction.account_id == next(item.id for item in accounts if item.name == "HDFC")
    assert transaction.destination_account_id == next(item.id for item in accounts if item.name == "SBI")


def test_transfer_asks_only_for_missing_accounts(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Transferred ₹5,000 today")
    assert response.widgets[0].type == "account_selector"
    assert response.widgets[0].data["role"] == "source_account"
    response = handle_chat(db, user, conversation, "HDFC")
    assert response.widgets[0].data["role"] == "destination_account"
    response = handle_chat(db, user, conversation, "SBI")
    assert response.widgets[0].type == "transaction_preview"


def test_account_selector_accepts_a_new_account_without_leaving_the_hitl_card(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Transferred ₹5,000 today")
    source = response.widgets[0]
    draft_id = source.data["draftId"]

    assert {action.label for action in source.actions} >= {"Change type", "Cancel transaction"}
    response = handle_action(db, user, conversation, "select_account", {
        "draftId": draft_id,
        "role": "source_account",
        "accountName": "HDFC",
    })
    assert response.widgets[0].data["role"] == "destination_account"
    assert {action.label for action in response.widgets[0].actions} >= {"Change source account", "Cancel transaction"}

    response = handle_action(db, user, conversation, "select_account", {
        "draftId": draft_id,
        "role": "destination_account",
        "accountName": "SBI",
    })
    assert response.widgets[0].type == "transaction_preview"
    assert [account.name for account in db.scalars(select(Account).order_by(Account.name))] == ["HDFC", "SBI"]


def test_transaction_draft_can_go_back_or_cancel_without_saving(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Transferred ₹5,000 today")
    draft_id = response.widgets[0].data["draftId"]

    response = handle_action(db, user, conversation, "revisit_transaction_step", {
        "draftId": draft_id,
        "step": "transaction_type",
    })
    assert response.widgets[0].type == "transaction_type_selector"
    draft = db.get(TransactionDraft, UUID(draft_id))
    assert draft.transaction_type == "unknown"

    response = handle_action(db, user, conversation, "cancel_transaction_draft", {"draftId": draft_id})
    assert response.message == "Cancelled. Nothing was saved."
    assert draft.state == DraftState.CANCELLED.value
    assert db.scalar(select(Transaction)) is None


def test_every_blocking_planning_card_declares_a_cancel_transition(db):
    user = default_user(db)
    for prompt in (
        "Set a food budget of ₹20,000",
        "Create a vacation goal of ₹2 lakh",
    ):
        conversation = get_or_create_conversation(db, user)
        response = handle_chat(db, user, conversation, prompt)
        assert any(action.action == "cancel_pending_action" for action in response.widgets[0].actions)
        cancel = next(action for action in response.widgets[0].actions if action.action == "cancel_pending_action")
        cancelled = handle_action(db, user, conversation, "cancel_pending_action", cancel.payload)
        assert cancelled.message == "Cancelled. No changes were made."


def test_blocking_widget_contract_rejects_a_forward_only_card():
    forward_only = Widget(
        id="unsafe-budget",
        type=WidgetType.BUDGET_PROGRESS,
        data={
            "budgetId": "draft",
            "title": "Budget",
            "amountMinor": 20_000,
            "spentMinor": 0,
            "remainingMinor": 20_000,
            "percentUsed": 0,
            "currency": "INR",
        },
        actions=[WidgetAction(id="save", label="Set budget", action="save_budget", payload={
            "name": "Budget",
            "amountMinor": 20_000,
        })],
    )

    with pytest.raises(ValueError, match="cancellation transition"):
        conversation_service._validate_blocking_widget_contract(
            [forward_only],
            PendingAction(action="save_budget", resource_id="draft"),
        )


def test_category_creation_is_a_declared_reversible_transition(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "₹500")
    draft_id = response.widgets[0].data["draftId"]
    response = handle_action(db, user, conversation, "select_transaction_type", {
        "draftId": draft_id,
        "optionId": "expense",
    })
    start = next(action for action in response.widgets[0].actions if action.action == "start_add_category")

    response = handle_action(db, user, conversation, start.action, start.payload)
    assert response.widgets[0].data["mode"] == "create"
    assert {action.action for action in response.widgets[0].actions} >= {
        "create_category",
        "cancel_add_category",
        "cancel_transaction_draft",
    }

    back = next(action for action in response.widgets[0].actions if action.action == "cancel_add_category")
    response = handle_action(db, user, conversation, back.action, back.payload)
    assert response.widgets[0].type == "category_selector"
    assert response.widgets[0].data.get("mode") != "create"


def test_budget_creation_requires_action_and_uses_recorded_spending(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Set a food budget of ₹20,000")
    assert response.widgets[0].type == "budget_progress"
    assert db.scalar(select(Budget)) is None
    action = response.widgets[0].actions[0]
    response = handle_action(db, user, conversation, action.action, action.payload)
    assert db.scalar(select(Budget)).amount_minor == 2_000_000
    assert response.widgets[0].data["amountMinor"] == 2_000_000


def test_goal_creation_and_contribution_require_confirmation(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Create a vacation goal of ₹2 lakh")
    assert response.widgets[0].type == "goal_progress"
    assert db.scalar(select(Goal)) is None
    action = response.widgets[0].actions[0]
    handle_action(db, user, conversation, action.action, action.payload)
    goal = db.scalar(select(Goal))
    assert goal.name == "Vacation"
    assert goal.target_minor == 20_000_000

    response = handle_chat(db, user, conversation, "Add ₹20,000 to my vacation savings")
    action = response.widgets[0].actions[0]
    assert goal.current_minor == 0
    response = handle_action(db, user, conversation, action.action, action.payload)
    assert goal.current_minor == 2_000_000
    assert response.widgets[0].data["percentComplete"] == 10.0
    contribution = db.scalar(select(GoalContribution))
    assert contribution.amount_minor == 2_000_000
    assert contribution.contribution_at.tzinfo is None  # SQLite strips offsets on round-trip.
    assert contribution.contribution_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)


def test_explicit_merchant_correction_is_learned_and_overrides_inference(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Spent ₹900 at Toit today")
    transaction_id = response.widgets[0].data["transactionId"]
    edit = handle_action(db, user, conversation, "edit_saved_transaction", {"transactionId": transaction_id})
    entertainment_id = next(item["id"] for item in edit.widgets[0].data["categories"] if item["label"] == "Entertainment")
    events_id = next(item["id"] for item in edit.widgets[0].data["subcategories"] if item["categoryId"] == entertainment_id and item["label"] == "Events")
    handle_action(db, user, conversation, "update_saved_transaction", {"transactionId": transaction_id, "amountMinor": 90_000, "merchant": "Toit", "transactionAt": datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc).isoformat(), "categoryId": entertainment_id, "subcategoryId": events_id})

    response = handle_chat(db, user, conversation, "Paid ₹1,100 at Toit today")
    assert response.widgets[0].type == "transaction_preview"
    assert response.widgets[0].data["category"] == "Entertainment"
    assert response.widgets[0].data["subcategory"] == "Events"


def test_travelling_query_filters_transport_spending(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹1,000 on a cab today")
    handle_chat(db, user, conversation, "Spent ₹500 on fuel today")
    handle_chat(db, user, conversation, "Spent ₹700 on lunch today")

    response = handle_chat(db, user, conversation, "How much did I spend on Travelling this month?")

    assert response.widgets[0].type == "financial_summary"
    assert response.widgets[0].data["title"] == "Transport · This month"
    assert response.widgets[0].data["amountMinor"] == 150_000
    assert response.widgets[0].data["count"] == 2
    assert {item["label"] for item in response.widgets[0].data["breakdown"]} == {"Cab", "Fuel"}


def test_category_breakdown_follow_up_uses_current_month_without_unnecessary_questions(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹200 on ice cream today")
    handle_chat(db, user, conversation, "Spent ₹300 on lunch today")
    handle_chat(db, user, conversation, "Can you show the spend summary")
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Known category breakdown should not call the LLM")),
    )

    response = handle_chat(db, user, conversation, "Show the food breakdown")

    assert response.widgets[0].type == "financial_summary"
    assert response.widgets[0].data["title"] == "Food · This month"
    assert response.widgets[0].data["amountMinor"] == 50_000
    assert {item["label"] for item in response.widgets[0].data["breakdown"]} == {"Dining", "Ice cream"}


def test_remove_merchant_expense_searches_candidates_before_confirming(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    first = handle_chat(db, user, conversation, "Spent ₹900 at Toit today")
    second = handle_chat(db, user, conversation, "Spent ₹1,100 at Toit today")
    first_id = first.widgets[0].data["transactionId"]
    second_id = second.widgets[0].data["transactionId"]
    handle_chat(db, user, conversation, "₹333")
    abandoned_draft = db.scalar(select(TransactionDraft).where(TransactionDraft.raw_text == "₹333"))
    assert abandoned_draft.state == DraftState.NEEDS_CLARIFICATION.value
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="find_transactions_for_removal",
            confidence=0.99,
            reason="The semantic router identified an existing-record removal request.",
            safe_reasoning_summary=["Find matching existing transactions", "Require user confirmation"],
        ),
    )

    response = handle_chat(db, user, conversation, "I want to remove the Toit expense from the list")

    assert response.widgets[0].type == "data_table"
    rows = response.widgets[0].data["rows"]
    assert {row["id"] for row in rows} == {first_id, second_id}
    assert response.widgets[0].data["rowActions"][0]["action"] == "request_remove_transaction"
    assert all("transaction.remove" in row["_capabilities"] for row in rows)
    assert db.scalar(select(TransactionDraft).where(TransactionDraft.raw_text.ilike("%remove%"))) is None
    assert abandoned_draft.state == DraftState.CANCELLED.value

    review = handle_action(db, user, conversation, response.widgets[0].data["rowActions"][0]["action"], {"transactionId": rows[0]["id"]})
    assert review.widgets[0].type == "confirmation_card"
    assert db.get(Transaction, UUID(rows[0]["id"])).deleted_at is None
    removed = handle_action(db, user, conversation, "confirm_remove_transaction", {"transactionId": rows[0]["id"]})
    assert removed.widgets[0].data["status"] == "Removed"
    assert db.get(Transaction, UUID(rows[0]["id"])).deleted_at is not None


def test_semantic_removal_route_handles_natural_wording_and_typo_without_creating_a_draft(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    saved = handle_chat(db, user, conversation, "Spent ₹777 at Toit today")
    transaction_id = saved.widgets[0].data["transactionId"]
    prompt = "I want to remove the Toit of 777 rupess"
    assert conversation_service._fast_path_decision(prompt, date.today()) is None
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="find_transactions_for_removal",
            confidence=0.96,
            reason="The user wants to remove an existing Toit transaction.",
            safe_reasoning_summary=["Resolve Toit and amount against active records", "Require confirmation"],
        ),
    )

    response = handle_chat(db, user, conversation, prompt)

    assert response.widgets[0].type == "confirmation_card"
    assert response.widgets[0].data["transactionId"] == transaction_id
    assert db.scalar(select(TransactionDraft).where(TransactionDraft.raw_text == prompt)) is None
    assert db.get(Transaction, UUID(transaction_id)).deleted_at is None


def test_removal_does_not_treat_digits_inside_a_merchant_name_as_an_amount(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    merchant = "RemovalCafe1786430623348"
    first = handle_chat(db, user, conversation, f"Spent ₹654 at {merchant} for dinner today")
    second = handle_chat(db, user, conversation, f"Spent ₹765 at {merchant} for dinner today")
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="find_transactions_for_removal",
            confidence=0.99,
            reason="The user wants to remove an existing merchant transaction.",
            safe_reasoning_summary=["Find matching active records", "Require confirmation"],
        ),
    )

    response = handle_chat(db, user, conversation, f"I want to remove the {merchant} expense from the list")

    assert response.widgets[0].type == "data_table"
    assert {row["id"] for row in response.widgets[0].data["rows"]} == {
        first.widgets[0].data["transactionId"],
        second.widgets[0].data["transactionId"],
    }


def test_semantic_transaction_search_preserves_merchant_and_list_intent(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    first = handle_chat(db, user, conversation, "Spent ₹777 at Toit today")
    second = handle_chat(db, user, conversation, "Spent ₹900 at Toit today")
    handle_chat(db, user, conversation, "Spent ₹500 on fuel today")
    prompt = "Show to all expenses on toit"
    assert conversation_service._fast_path_decision(prompt, date.today()) is None
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="search_transactions",
            query=QueryInterpretation(result_mode="transaction_list", transaction_type="expense", merchant="Toit", limit=50),
            confidence=0.94,
            reason="The user wants individual Toit expense records.",
            safe_reasoning_summary=["Resolve the merchant", "List matching canonical expenses"],
        ),
    )

    response = handle_chat(db, user, conversation, prompt)

    assert response.widgets[0].type == "data_table"
    assert {row["id"] for row in response.widgets[0].data["rows"]} == {
        first.widgets[0].data["transactionId"],
        second.widgets[0].data["transactionId"],
    }
    assert "Toit" in response.message
    assert response.citations[0].query["merchant"] == "Toit"


def test_contextual_refinement_is_bound_to_the_previous_transaction_result_set(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    first = handle_chat(db, user, conversation, "Spent ₹120 at Blue Tokai for coffee today")
    second = handle_chat(db, user, conversation, "Spent ₹250 on coffee today")
    handle_chat(db, user, conversation, "Spent ₹500 on fuel today")
    captured_scope = {}

    def semantic_router(*args, **kwargs):
        text = args[0]
        if text == "Show the coffee transactions":
            return CopilotDecision(
                tool="search_transactions",
                query=QueryInterpretation(
                    result_mode="transaction_list",
                    operation="list",
                    transaction_type="expense",
                    category_slug="food",
                    subcategory_slug="coffee",
                    start_date=date.today().replace(day=1),
                    end_date=date.today(),
                    limit=100,
                ),
                confidence=0.99,
                reason="List current-month coffee transactions.",
            )
        captured_scope.update((kwargs.get("workflow_context") or {}).get("activeDataScope") or {})
        return CopilotDecision(
            tool="search_transactions",
            query=QueryInterpretation(
                result_mode="transaction_list",
                operation="list",
                transaction_type="expense",
                category_slug="food",
                subcategory_slug="coffee",
                start_date=date.today().replace(day=1),
                end_date=date.today(),
                limit=100,
                use_active_scope=True,
            ),
            confidence=0.99,
            reason="Refine only the previously shown records.",
        )

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", semantic_router)
    initial = handle_chat(db, user, conversation, "Show the coffee transactions")
    initial_ids = {row["id"] for row in initial.widgets[0].data["rows"]}
    assert initial_ids == {first.widgets[0].data["transactionId"], second.widgets[0].data["transactionId"]}

    # Simulate a concurrent financial observation after the list was shown. A
    # contextual refinement must not silently expand to include it.
    other_conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, other_conversation, "Spent ₹300 on coffee today")
    refined = handle_chat(db, user, conversation, "I mean just those coffee transactions")

    assert captured_scope["entityCount"] == 2
    assert set(captured_scope["entityIds"]) == initial_ids
    assert {row["id"] for row in refined.widgets[0].data["rows"]} == initial_ids
    assert refined.citations[0].query["use_active_scope"] is True
    assert set(refined.citations[0].query["scope_transaction_ids"]) == initial_ids


def test_query_bundle_refreshes_prior_table_and_builds_summary_over_the_same_scope(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    first = handle_chat(db, user, conversation, "Spent ₹120 at Blue Tokai for coffee today")
    second = handle_chat(db, user, conversation, "Spent ₹250 at Starbucks for coffee today")
    handle_chat(db, user, conversation, "Spent ₹500 on fuel today")

    def semantic_router(*args, **kwargs):
        if args[0] == "Show all coffee transactions":
            return CopilotDecision(
                tool="search_transactions",
                query=QueryInterpretation(
                    result_mode="transaction_list",
                    operation="list",
                    transaction_type="expense",
                    category_slug="food",
                    subcategory_slug="coffee",
                    start_date=date.today().replace(day=1),
                    end_date=date.today(),
                    limit=100,
                ),
                confidence=0.99,
                reason="List coffee expenses.",
            )
        return CopilotDecision(
            tool="run_query_bundle",
            query_bundle=QueryBundleInterpretation(
                # Deliberately empty: refresh semantics are resolved from the
                # authoritative prior query definition, not stale row IDs or
                # a model's lossy reconstruction.
                base_query=QueryInterpretation(),
                refresh_from_active_analysis=True,
                views=[
                    QueryView(id="rows", result_mode="transaction_list", operation="list", limit=100),
                    QueryView(id="summary", result_mode="summary", operation="total", limit=8),
                ],
            ),
            confidence=0.99,
            reason="Refresh the prior records and summarize the same scope.",
        )

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", semantic_router)
    monkeypatch.setattr(
        conversation_service,
        "validate_copilot_decision",
        lambda *_args, **_kwargs: CopilotDecisionValidation(outcome="approve", confidence=0.99, summary="The bundle preserves both views."),
    )

    initial = handle_chat(db, user, conversation, "Show all coffee transactions")
    initial_ids = {row["id"] for row in initial.widgets[0].data["rows"]}
    assert initial_ids == {
        first.widgets[0].data["transactionId"],
        second.widgets[0].data["transactionId"],
    }

    removed_id = first.widgets[0].data["transactionId"]
    handle_action(db, user, conversation, "confirm_remove_transaction", {"transactionId": removed_id})
    response = handle_chat(db, user, conversation, "Show again the same table with summary of expenses")

    assert [widget.type for widget in response.widgets] == ["data_table", "financial_summary"]
    table, summary = response.widgets
    assert summary.data["amountMinor"] == 25_000
    assert summary.data["count"] == 1
    assert summary.data["title"] == "Coffee spending · This month"
    assert summary.data["scopePath"] == ["Food", "Coffee"]
    assert summary.data["breakdown"] == [{"label": "Food → Coffee", "amount_minor": 25_000}]
    assert response.message == "I refreshed the same records using your previous filters. Your Food → Coffee spending this month totals ₹250 across 1 transaction."
    assert [row["id"] for row in table.data["rows"]] == [second.widgets[0].data["transactionId"]]
    assert all(citation.query["subcategory_slug"] == "coffee" for citation in response.citations)
    assert len({citation.query["bundle_id"] for citation in response.citations}) == 1
    assert {citation.query["result_mode"] for citation in response.citations} == {"summary", "transaction_list"}
    assert conversation.active_analysis_state["resultShapes"] == ["transaction_list", "summary"]
    assert conversation.active_data_scope["entityIds"] == [second.widgets[0].data["transactionId"]]


def test_grounded_summary_is_persisted_as_analysis_state_not_row_scope(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    captured_workflow = {}

    def semantic_router(*args, **kwargs):
        if args[0] == "Which category has the highest spend?":
            return CopilotDecision(
                tool="search_transactions",
                query=QueryInterpretation(
                    metric="spending_summary",
                    result_mode="summary",
                    operation="rank",
                    group_by="category",
                    transaction_type="expense",
                    start_date=date.today().replace(day=1),
                    end_date=date.today(),
                    limit=1,
                ),
                confidence=0.99,
                reason="Rank category spend.",
            )
        captured_workflow.update(kwargs.get("workflow_context") or {})
        return CopilotDecision(tool="conversation", reply="Captured.", confidence=0.99, reason="Test context.")

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", semantic_router)
    monkeypatch.setattr(
        conversation_service,
        "validate_copilot_decision",
        lambda *_args, **_kwargs: CopilotDecisionValidation(outcome="approve", confidence=0.99, summary="Valid."),
    )

    initial = handle_chat(db, user, conversation, "Which category has the highest spend?")
    assert initial.citations[0].entity_ids == []
    handle_chat(db, user, conversation, "retry one")
    handle_chat(db, user, conversation, "retry two")
    handle_chat(db, user, conversation, "retry three")
    handle_chat(db, user, conversation, "And other than Food?")

    assert captured_workflow["activeDataScope"] is None
    state = captured_workflow["activeAnalysisState"]
    assert state["answerSummary"] == initial.message
    assert state["query"]["operation"] == "rank"
    assert state["query"]["group_by"] == "category"
    assert state["query"]["limit"] == 1
    assert conversation.active_analysis_state == state


def test_independent_list_request_drops_accidental_active_scope(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    first = handle_chat(db, user, conversation, "Spent ₹120 at Blue Tokai for coffee today")
    second = handle_chat(db, user, conversation, "Spent ₹250 on coffee today")
    third = handle_chat(db, user, conversation, "Spent ₹500 on fuel today")

    def semantic_router(*args, **kwargs):
        if args[0] == "Show the coffee transactions":
            query = QueryInterpretation(
                result_mode="transaction_list",
                operation="list",
                transaction_type="expense",
                subcategory_slug="coffee",
                limit=100,
            )
        else:
            # Simulate the exact bad router contract observed in production.
            query = QueryInterpretation(
                result_mode="transaction_list",
                operation="list",
                limit=5,
                use_active_scope=True,
            )
        return CopilotDecision(tool="search_transactions", query=query, confidence=0.99, reason="Typed list query.")

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", semantic_router)
    monkeypatch.setattr(
        conversation_service,
        "validate_copilot_decision",
        lambda *_args, **_kwargs: CopilotDecisionValidation(outcome="approve", confidence=0.99, summary="Valid after scope policy."),
    )
    scoped = handle_chat(db, user, conversation, "Show the coffee transactions")
    assert len(scoped.widgets[0].data["rows"]) == 2

    response = handle_chat(db, user, conversation, "Show last 5 transactions")

    expected = {
        first.widgets[0].data["transactionId"],
        second.widgets[0].data["transactionId"],
        third.widgets[0].data["transactionId"],
    }
    assert {row["id"] for row in response.widgets[0].data["rows"]} == expected
    assert response.citations[0].query["use_active_scope"] is False
    assert response.citations[0].query["scope_transaction_ids"] == []


def test_independent_list_request_rejects_invalid_validator_scope_repair(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹120 at Blue Tokai for coffee today")
    handle_chat(db, user, conversation, "Spent ₹250 on coffee today")
    handle_chat(db, user, conversation, "Spent ₹500 on fuel today")

    def semantic_router(*args, **kwargs):
        if args[0] == "Show the coffee transactions":
            query = QueryInterpretation(
                result_mode="transaction_list",
                operation="list",
                transaction_type="expense",
                subcategory_slug="coffee",
                limit=100,
            )
        else:
            query = QueryInterpretation(
                result_mode="transaction_list",
                operation="list",
                limit=5,
                use_active_scope=True,
            )
        return CopilotDecision(tool="search_transactions", query=query, confidence=0.99, reason="Typed list query.")

    def validator(_text, decision, *_args, **_kwargs):
        if _text == "Show last 5 transactions":
            assert decision.query is not None
            assert decision.query.use_active_scope is False
            return CopilotDecisionValidation(
                outcome="reject",
                confidence=0.98,
                issues=["An active result scope is available."],
                repairs=["bind_active_scope"],
                summary="Incorrectly asks to bind the prior result set.",
            )
        return CopilotDecisionValidation(outcome="approve", confidence=0.99, summary="Valid query.")

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", semantic_router)
    monkeypatch.setattr(conversation_service, "validate_copilot_decision", validator)
    handle_chat(db, user, conversation, "Show the coffee transactions")

    response = handle_chat(db, user, conversation, "Show last 5 transactions")

    assert response.widgets[0].type == "data_table"
    assert len(response.widgets[0].data["rows"]) == 3
    assert response.citations[0].query["use_active_scope"] is False
    assert response.citations[0].query["scope_transaction_ids"] == []


def test_validator_can_repair_missing_scope_and_rank_an_individual_transaction(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹100 on coffee today")
    highest = handle_chat(db, user, conversation, "Spent ₹500 at Blue Tokai for coffee today")

    def semantic_router(*args, **kwargs):
        if args[0] == "Show the coffee transactions":
            query = QueryInterpretation(result_mode="transaction_list", operation="list", transaction_type="expense", subcategory_slug="coffee", limit=100)
        else:
            query = QueryInterpretation(result_mode="summary", operation="rank", group_by="none", sort_direction="desc", transaction_type="expense", subcategory_slug="coffee", limit=1)
        return CopilotDecision(tool="search_transactions", query=query, confidence=0.99, reason="Typed contextual query.")

    def validator(_text, decision, *_args, **_kwargs):
        if decision.query and decision.query.operation == "rank" and not decision.query.use_active_scope:
            return CopilotDecisionValidation(
                outcome="reject",
                confidence=0.99,
                issues=["The active result scope is missing."],
                repairs=["bind_active_scope"],
                summary="Bind the prior grounded transaction IDs.",
            )
        return CopilotDecisionValidation(outcome="approve", confidence=0.99, summary="The scoped query is valid.")

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", semantic_router)
    monkeypatch.setattr(conversation_service, "validate_copilot_decision", validator)
    handle_chat(db, user, conversation, "Show the coffee transactions")
    response = handle_chat(db, user, conversation, "Which of those is the highest?")

    assert response.message.startswith("The highest matching transaction is ₹500 at Blue Tokai")
    assert response.widgets[0].data["title"] == "Highest matching transaction"
    assert [row["id"] for row in response.widgets[0].data["rows"]] == [highest.widgets[0].data["transactionId"]]
    assert response.citations[0].query["use_active_scope"] is True
    assert len(response.citations[0].query["scope_transaction_ids"]) == 2


def test_deterministic_policy_preserves_explicit_filtered_rank_without_a_critic(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    low = handle_chat(db, user, conversation, "Spent ₹100 on a movie ticket today")
    high = handle_chat(db, user, conversation, "Spent ₹500 on a concert ticket today")
    final_attempts = 0

    def semantic_router(*args, **kwargs):
        nonlocal final_attempts
        if args[0] == "Show transactions in Entertainment":
            return CopilotDecision(
                tool="search_transactions",
                query=QueryInterpretation(
                    result_mode="transaction_list",
                    operation="list",
                    transaction_type="expense",
                    category_slug="entertainment",
                    start_date=date.today().replace(day=1),
                    end_date=date.today(),
                    limit=100,
                ),
                confidence=0.99,
                reason="List Entertainment transactions.",
            )
        final_attempts += 1
        if final_attempts == 1:
            return CopilotDecision(
                tool="search_transactions",
                query=QueryInterpretation(
                    result_mode="transaction_list",
                    operation="list",
                    transaction_type="expense",
                    category_slug="entertainment",
                    start_date=date.today().replace(day=1),
                    end_date=date.today(),
                ),
                confidence=0.8,
                reason="Incorrectly retained a list shape.",
            )
        raise AssertionError("Deterministic rank normalization should avoid a reroute")

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", semantic_router)
    monkeypatch.setattr(
        conversation_service,
        "validate_copilot_decision",
        lambda *args, **kwargs: pytest.fail("A typed read should not call the model validator"),
    )
    listed = handle_chat(db, user, conversation, "Show transactions in Entertainment")
    assert {row["id"] for row in listed.widgets[0].data["rows"]} == {
        low.widgets[0].data["transactionId"],
        high.widgets[0].data["transactionId"],
    }

    response = handle_chat(db, user, conversation, "highest spend in the entertainment category/")

    assert final_attempts == 1
    assert response.message.startswith("The highest matching transaction is ₹500")
    assert response.citations[0].query["operation"] == "rank"
    assert response.citations[0].query["group_by"] == "none"
    assert response.citations[0].query["category_slug"] == "entertainment"
    assert response.citations[0].query["limit"] == 1


def test_grounded_list_to_rank_transition_overrides_inconsistent_scope_critic(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹100 on a movie ticket today")
    highest = handle_chat(db, user, conversation, "Spent ₹500 on a concert ticket today")
    router_calls = 0

    def semantic_router(*args, **kwargs):
        nonlocal router_calls
        router_calls += 1
        if args[0] == "Show transactions in Entertainment":
            query = QueryInterpretation(
                result_mode="transaction_list",
                operation="list",
                transaction_type="expense",
                category_slug="entertainment",
                start_date=date.today().replace(day=1),
                end_date=date.today(),
                limit=100,
            )
        else:
            query = QueryInterpretation(
                metric="spending_summary",
                result_mode="summary",
                operation="rank",
                group_by="none",
                sort_direction="desc",
                transaction_type="expense",
                category_slug="entertainment",
                start_date=date.today().replace(day=1),
                end_date=date.today(),
                limit=10,
            )
        return CopilotDecision(tool="search_transactions", query=query, confidence=0.99, reason="Typed Entertainment query.")

    def inconsistent_validator(text, decision, *_args, **_kwargs):
        if text == "Show transactions in Entertainment":
            return CopilotDecisionValidation(outcome="approve", confidence=0.99, summary="Valid list.")
        return CopilotDecisionValidation(
            outcome="reject",
            confidence=0.99,
            issues=["Bind the exact prior row IDs even though the explicit filter and period are unchanged."],
            summary="Incorrect active-scope requirement.",
        )

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", semantic_router)
    monkeypatch.setattr(conversation_service, "validate_copilot_decision", inconsistent_validator)
    handle_chat(db, user, conversation, "Show transactions in Entertainment")
    response = handle_chat(db, user, conversation, "highest spend in the entertainment category/")

    assert router_calls == 2  # no redundant stronger-model reroute
    assert response.message.startswith("The highest matching transaction is ₹500")
    assert response.widgets[0].data["rows"][0]["id"] == highest.widgets[0].data["transactionId"]
    assert response.citations[0].query["result_mode"] == "transaction_list"
    assert response.citations[0].query["use_active_scope"] is False


def test_semantic_category_ranking_answers_the_requested_question(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹200 on ice cream today")
    handle_chat(db, user, conversation, "Spent ₹300 on lunch today")
    handle_chat(db, user, conversation, "Spent ₹100 on fuel today")
    prompt = "Which category had highest spend?"
    assert conversation_service._fast_path_decision(prompt, date.today()) is None
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="search_transactions",
            query=QueryInterpretation(
                result_mode="summary",
                operation="rank",
                group_by="category",
                transaction_type="expense",
                start_date=date.today().replace(day=1),
                end_date=date.today(),
                limit=1,
            ),
            confidence=0.97,
            reason="The user requested the highest-spend category.",
            safe_reasoning_summary=["Group current-month expenses by category", "Rank by total spend"],
        ),
    )

    response = handle_chat(db, user, conversation, prompt)

    assert response.widgets[0].type == "financial_summary"
    assert response.message == "Food had the highest category spend at ₹500."
    assert response.widgets[0].data["breakdown"] == [{"label": "Food", "amount_minor": 50_000}]
    assert response.citations[0].query["operation"] == "rank"
    assert response.citations[0].query["group_by"] == "category"


def test_semantic_merchant_summary_keeps_the_merchant_filter(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹777 at Toit today")
    handle_chat(db, user, conversation, "Spent ₹900 at Toit today")
    handle_chat(db, user, conversation, "Spent ₹500 on fuel today")
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="search_transactions",
            query=QueryInterpretation(result_mode="summary", operation="total", transaction_type="expense", merchant="Toit"),
            confidence=0.96,
            reason="The user requested a Toit-only total.",
            safe_reasoning_summary=["Filter canonical expenses by merchant", "Aggregate exact minor units"],
        ),
    )

    response = handle_chat(db, user, conversation, "How much have I spent at Toit?")

    assert response.widgets[0].data["amountMinor"] == 167_700
    assert response.widgets[0].data["count"] == 2
    assert response.citations[0].query["merchant"] == "Toit"


def test_semantic_income_summary_never_collapses_into_expense_spending(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Salary ₹3 lakh credited today")
    prompt = "Do I have any earnings from current month?"
    assert conversation_service._fast_path_decision(prompt, date.today()) is None
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="search_transactions",
            query=QueryInterpretation(
                metric="income_summary",
                result_mode="summary",
                operation="total",
                transaction_type="income",
                start_date=date.today().replace(day=1),
                end_date=date.today(),
            ),
            confidence=0.99,
            reason="The user asked for current-month earnings.",
            safe_reasoning_summary=["Filter current-month income", "Aggregate exact minor units"],
        ),
    )

    response = handle_chat(db, user, conversation, prompt)

    assert response.message == "You earned ₹3,00,000 this month, across 1 transaction."
    assert response.widgets[0].data["title"] == "Income · This month"
    assert response.widgets[0].data["amountMinor"] == 30_000_000
    assert response.citations[0].query["transaction_type"] == "income"


def test_category_selector_has_ranked_guesses_and_can_create_private_category(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "₹300")
    draft_id = response.widgets[0].data["draftId"]
    response = handle_action(db, user, conversation, "select_transaction_type", {"draftId": draft_id, "optionId": "expense"})
    widget = response.widgets[0]
    assert widget.type == "category_selector"
    assert len(widget.data["suggestions"]) == 3
    assert widget.data["allowCreate"] is True

    response = handle_action(db, user, conversation, "start_add_category", {"draftId": widget.data["draftId"]})
    assert response.widgets[0].data["mode"] == "create"
    response = handle_action(db, user, conversation, "create_category", {"draftId": widget.data["draftId"], "name": "Pets"})
    assert response.widgets[0].type == "transaction_preview"
    assert response.widgets[0].data["category"] == "Pets"
    category = db.scalar(select(Category).where(Category.owner_user_id == user.id, Category.name == "Pets"))
    assert category is not None
    assert category.scope == "user"


def test_taxonomy_actions_reject_another_users_owned_rows(db):
    user = default_user(db)
    other = User(email="taxonomy-owner@example.com", display_name="Taxonomy owner")
    db.add(other)
    db.flush()
    category = Category(
        slug=f"custom-{uuid4().hex}", name="Private foreign category", icon="circle",
        scope=TaxonomyScope.USER.value, owner_user_id=other.id,
    )
    db.add(category)
    db.flush()
    subcategory = Subcategory(
        category_id=category.id, slug=f"custom-{uuid4().hex}", name="Private foreign subcategory",
        scope=TaxonomyScope.USER.value, owner_user_id=other.id,
    )
    db.add(subcategory)
    db.commit()

    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "₹500")
    draft_id = response.widgets[0].data["draftId"]

    with pytest.raises(ValueError, match="Unknown category"):
        handle_action(db, user, conversation, "select_category", {
            "draftId": draft_id,
            "categoryId": str(category.id),
        })
    with pytest.raises(ValueError, match="Unknown category"):
        handle_action(db, user, conversation, "save_budget", {
            "categoryId": str(category.id),
            "amountMinor": 100_000,
        })

    draft = db.get(TransactionDraft, UUID(draft_id))
    system_category = db.scalar(select(Category).where(Category.slug == "food"))
    draft.category_id = system_category.id
    with pytest.raises(ValueError, match="Unknown subcategory"):
        handle_action(db, user, conversation, "select_subcategory", {
            "draftId": draft_id,
            "subcategoryId": str(subcategory.id),
        })


def test_explicit_user_subcategory_name_resolves_to_canonical_hierarchy(db, monkeypatch):
    user = default_user(db)
    category = Category(
        slug=f"custom-{uuid4().hex}", name="Construction", icon="hammer",
        scope=TaxonomyScope.USER.value, owner_user_id=user.id,
    )
    db.add(category)
    db.flush()
    subcategory = Subcategory(
        category_id=category.id, slug=f"custom-{uuid4().hex}", name="Labour Wages",
        scope=TaxonomyScope.USER.value, owner_user_id=user.id,
    )
    db.add(subcategory)
    db.flush()
    db.commit()
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="create_transaction_draft",
            transaction=TransactionInterpretation(
                transaction_type="expense",
                amount_minor=30_000,
                merchant="Labour wages",
                transaction_date=date.today(),
                category_slug="other",
                subcategory_slug="other",
                explicit_fields=["transaction_type", "amount"],
                confidence=0.93,
            ),
            confidence=0.99,
            reason="The user asked to record an expense.",
        ),
    )

    response = handle_chat(db, user, conversation, "Okay, can you add 300 expense to Labour wages")

    assert response.widgets, response.model_dump(mode="json")
    assert response.widgets[0].type == "transaction_preview"
    transaction = db.get(Transaction, UUID(response.widgets[0].data["transactionId"]))
    assert transaction.category_id == category.id
    assert transaction.subcategory_id == subcategory.id
    assert transaction.merchant_name is None
    assert response.widgets[0].data["category"] == "Construction"
    assert response.widgets[0].data["subcategory"] == "Labour Wages"


def test_model_cannot_pair_income_with_an_expense_taxonomy(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(conversation_service, "_fast_path_decision", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        conversation_service,
        "interpret_with_financial_copilot",
        lambda *args, **kwargs: CopilotDecision(
            tool="create_transaction_draft",
            transaction=TransactionInterpretation(
                transaction_type="income",
                amount_minor=50_000,
                merchant="earning",
                transaction_date=date.today(),
                category_slug="other",
                subcategory_slug="other",
                explicit_fields=["amount"],
                confidence=0.98,
            ),
            confidence=0.99,
            reason="The user asked to record income.",
        ),
    )

    response = handle_chat(db, user, conversation, "add 500 to earning in salary")

    transaction = db.get(Transaction, UUID(response.widgets[0].data["transactionId"]))
    assert transaction.transaction_type == "income"
    assert response.widgets[0].data["category"] == "Income"
    assert response.widgets[0].data["subcategory"] == "Salary"
    assert transaction.spend_nature == "unknown"


def test_saved_type_change_reclassifies_hidden_stale_taxonomy(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    created = handle_chat(db, user, conversation, "₹250 for coffee")
    transaction_id = created.widgets[0].data["transactionId"]
    old_category_id = created.widgets[0].data.get("categoryId")
    transaction = db.get(Transaction, UUID(transaction_id))
    old_category_id = old_category_id or str(transaction.category_id)
    old_subcategory_id = str(transaction.subcategory_id)

    changed = handle_action(db, user, conversation, "update_saved_transaction", {
        "transactionId": transaction_id,
        "amountMinor": 25_000,
        "transactionType": "income",
        # Simulate an older client submitting fields hidden after Type changed.
        "categoryId": old_category_id,
        "subcategoryId": old_subcategory_id,
        "spendNature": "discretionary",
    })

    db.refresh(transaction)
    assert transaction.transaction_type == "income"
    assert transaction.spend_nature == "unknown"
    assert changed.widgets[0].data["category"] == "Income"
    assert changed.widgets[0].data["subcategory"] == "Other"

    edit = handle_action(db, user, conversation, "edit_saved_transaction", {"transactionId": transaction_id})
    assert edit.widgets[0].data["categories"], "expense choices must be available before changing Type back"


def test_saved_income_edit_treats_legacy_null_spend_nature_as_unknown(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    created = handle_chat(db, user, conversation, "Salary ₹500 credited today")
    transaction_id = created.widgets[0].data["transactionId"]

    updated = handle_action(db, user, conversation, "update_saved_transaction", {
        "transactionId": transaction_id,
        "amountMinor": 50_000,
        "transactionType": "income",
        # Persisted widgets from before the field was hidden submit JSON null.
        "spendNature": None,
    })

    transaction = db.get(Transaction, UUID(transaction_id))
    assert transaction.spend_nature == "unknown"
    assert updated.widgets[0].data["spendNature"] == "unknown"


def test_automatic_transaction_can_be_removed_after_confirmation(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "₹250 for coffee")
    transaction_id = response.widgets[0].data["transactionId"]

    response = handle_action(db, user, conversation, "request_remove_transaction", {"transactionId": transaction_id})
    assert response.widgets[0].type == "confirmation_card"
    assert db.get(Transaction, UUID(transaction_id)).deleted_at is None
    response = handle_action(db, user, conversation, "confirm_remove_transaction", {"transactionId": transaction_id})
    assert response.widgets[0].data["status"] == "Removed"
    assert db.get(Transaction, UUID(transaction_id)).deleted_at is not None


def test_loan_and_investment_scenarios_use_deterministic_actions(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "What if I prepay ₹5 lakh on my home loan?")
    assert response.widgets[0].type == "loan_calculator"
    response = handle_action(db, user, conversation, "calculate_loan_scenario", {"principalMinor": 8_000_000_00, "annualRatePercent": 8.5, "tenureMonths": 180, "prepaymentMinor": 50_000_000})
    assert response.widgets[0].data["result"]["interest_saved_minor"] > 0
    assert response.citations[0].entity_type == "calculator"

    response = handle_chat(db, user, conversation, "What if I increase my SIP by ₹20,000?")
    assert response.widgets[0].type == "investment_projection"
    response = handle_action(db, user, conversation, "calculate_investment_scenario", {"monthlyContributionMinor": 2_000_000, "currentValueMinor": 0, "annualReturnPercent": 10, "years": 10})
    assert response.widgets[0].data["result"]["projected_value_minor"] > response.widgets[0].data["result"]["invested_minor"]
    assert "not guaranteed" in response.message


def test_deleting_a_conversation_leaves_no_trace_of_it_anywhere(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    kept = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹2,000 at Toit today")
    handle_chat(db, user, conversation, "₹1,234")
    handle_chat(db, user, kept, "₹500")
    tool = AnalysisTool(user_id=user.id, name="Monthly spend", description="Spend by category", intent_signature="monthly spend", specification={}, specification_hash="hash-1", validation_report={})
    db.add(tool)
    db.flush()
    db.add(AnalysisToolRun(user_id=user.id, tool_id=tool.id, conversation_id=conversation.id, status="completed", trace=[{"stage": "tool_execution"}]))
    db.add(AIAction(user_id=user.id, conversation_id=conversation.id, action_type="primary_router", payload_redacted={"tool": "search_transactions"}, status="completed"))
    db.commit()
    deleted_id = conversation.id

    delete_conversation(deleted_id, db, user)

    assert db.get(Conversation, deleted_id) is None
    # Not one row in the schema may still carry the deleted thread's id.
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, Uuid):
                assert db.scalar(select(func.count()).select_from(table).where(column == deleted_id)) == 0, f"{table.name}.{column.name} still references the deleted conversation"

    # The other thread, the money it recorded, and the reusable tool registry
    # are untouched — deleting a thread is not deleting your finances.
    assert db.get(Conversation, kept.id) is not None
    assert db.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == kept.id)) > 0
    assert db.scalar(select(func.count()).select_from(TransactionDraft).where(TransactionDraft.conversation_id == kept.id)) == 1
    assert db.scalar(select(func.count()).select_from(Transaction)) == 1
    assert db.get(AnalysisTool, tool.id) is not None


def test_deleting_an_unknown_conversation_fails_closed(db):
    user = default_user(db)
    with pytest.raises(HTTPException) as error:
        delete_conversation(uuid4(), db, user)
    assert error.value.status_code == 404


def test_conversation_history_pages_by_keyset_without_repeating_or_skipping(db):
    user = default_user(db)
    threads = [get_or_create_conversation(db, user) for _ in range(5)]
    for offset, thread in enumerate(threads):
        thread.title = f"Thread {offset}"
        thread.updated_at = datetime(2026, 8, 1, 12, 0) + timedelta(minutes=offset)
    db.commit()

    first = list_conversations(None, 2, db, user)
    second = list_conversations(first.next_cursor, 2, db, user)
    third = list_conversations(second.next_cursor, 2, db, user)

    assert [item.title for item in first.items] == ["Thread 4", "Thread 3"]
    assert [item.title for item in second.items] == ["Thread 2", "Thread 1"]
    assert [item.title for item in third.items] == ["Thread 0"]
    assert third.next_cursor is None

    # A thread deleted mid-scroll drops out of later pages rather than shifting
    # the window, which is the failure an OFFSET page would have.
    delete_conversation(threads[0].id, db, user)
    assert list_conversations(second.next_cursor, 2, db, user).items == []


def test_conversation_history_rejects_a_malformed_cursor(db):
    with pytest.raises(HTTPException) as error:
        list_conversations("not-a-cursor", 2, db, default_user(db))
    assert error.value.status_code == 422


def _after_admission(action):
    """Runs `action` once, on the first activity that follows admission.

    The opening "request" event is emitted before the turn writes anything, so
    keying off it would stand in for a message that arrived *before* this turn
    rather than during it."""
    fired: list[bool] = []

    def callback(event):
        if event["id"] == "request" or fired:
            return
        fired.append(True)
        action()

    return callback, fired


def test_a_reply_keeps_its_place_when_another_turn_is_admitted_mid_run(db):
    """A turn's answer belongs under its own question.

    The reply row is created while the turn is being admitted rather than when
    the model finishes, so a message sent while a slow turn is still running
    cannot be written between that turn's question and its answer.
    """
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)

    def admit_a_second_turn():
        db.add(Message(conversation_id=conversation.id, role="user", content="hello", widgets=[], citations=[]))
        db.flush()

    callback, fired = _after_admission(admit_a_second_turn)
    handle_chat(db, user, conversation, "How much did I spend this month?", callback)
    db.commit()

    assert fired, "the run produced no activity to interleave with"
    transcript = [
        (message.role, message.content)
        for message in db.scalars(
            select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at, Message.id)
        )
    ]
    # This turn's question, its answer, and only then the message that arrived
    # while it was still running.
    assert transcript[-3][0] == "user" and transcript[-3][1].startswith("How much")
    assert transcript[-2][0] == "assistant" and transcript[-2][1]
    assert transcript[-1] == ("user", "hello")


def test_a_failed_turn_leaves_no_empty_reply_behind(db):
    """The reserved row is not a message until the turn answers into it."""
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)

    def collapse():
        raise RuntimeError("router unavailable")

    callback, _ = _after_admission(collapse)
    with pytest.raises(RuntimeError):
        handle_chat(db, user, conversation, "How much did I spend this month?", callback)

    stranded = list(db.scalars(select(Message).where(Message.conversation_id == conversation.id, Message.content == "")))
    assert stranded == []


def test_subcategory_selector_learns_from_past_choices(db):
    """The subcategory step used to offer no guesses at all, only a flat list."""
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)

    def categorize(text, *, category_slug, subcategory_slug):
        """Walk one entry through whatever steps the flow still asks for."""
        response = handle_chat(db, user, conversation, text)
        widget = response.widgets[0]
        draft_id = widget.data["draftId"]
        suggestions = None
        if widget.type == "category_selector":
            chosen = next(item["id"] for item in widget.data["options"] if item["slug"] == category_slug)
            response = handle_action(db, user, conversation, "select_category", {"draftId": draft_id, "categoryId": chosen})
            widget = response.widgets[0]
        if widget.type == "subcategory_selector":
            suggestions = widget.data["suggestions"]
            chosen = next(item["id"] for item in widget.data["options"] if item["slug"] == subcategory_slug)
            handle_action(db, user, conversation, "select_subcategory", {"draftId": draft_id, "subcategoryId": chosen})
        handle_action(db, user, conversation, "commit_transaction", {"draftId": draft_id})
        return suggestions

    # The very first time there is nothing to learn from.
    first = categorize("Spent ₹250 at Third Wave", category_slug="food", subcategory_slug="coffee")
    assert first is not None
    # Nothing may claim the user has done this before, because they have not.
    assert all("times" not in reason for item in first for reason in item["reasons"])

    for _ in range(3):
        categorize("Spent ₹250 at Third Wave", category_slug="food", subcategory_slug="coffee")

    learned = categorize("Spent ₹250 at Third Wave", category_slug="food", subcategory_slug="coffee")
    if learned is None:
        # The habit is settled enough that the flow stopped asking at all.
        transaction = db.scalars(select(Transaction).where(Transaction.user_id == user.id)).all()[-1]
        assert transaction.subcategory_id is not None
        return
    assert learned, "the subcategory step must now carry ranked guesses"
    assert learned[0]["slug"] == "coffee"
    assert learned[0]["reasons"]


def test_non_converging_repair_stops_before_spending_a_second_validation(db, monkeypatch):
    """A repair that reproduces the rejected contract is not progress.

    For a chart the plan is compiled from the typed intent, so the repair model
    cannot edit what the compiler owns and returns the same contract. Detecting
    that is what keeps the turn from paying for a second verdict it already has.
    """
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    validations = []
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    monkeypatch.setenv("UNIFIED_READ_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        conversation_service,
        "run_unified_read_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("an explicit rich analysis should enter its semantic planner directly")
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "stream_conversation_reply",
        lambda _text, draft_reply, _context, **_kwargs: draft_reply,
    )

    def router(*args, **kwargs):
        return CopilotDecision(
            tool="run_analysis_harness",
            presentation={"mode": "chart", "requested_mark": "arc", "unit_of_analysis": "category"},
            confidence=0.99,
            reason="The user requested a read-only donut.",
        )

    def reject(*args, **kwargs):
        validations.append(args)
        return CopilotDecisionValidation(
            outcome="reject",
            confidence=0.99,
            issues=["The composition has no coherent total."],
            summary="Reject the incoherent composition.",
        )

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", router)
    monkeypatch.setattr(conversation_service, "validate_copilot_decision", reject)

    response = handle_chat(db, user, conversation, "Draw a breakdown of all transactions in donut form")

    assert len(validations) == 1
    assert "composition has no coherent total" in response.message.casefold()
    get_settings.cache_clear()


def test_universal_request_releases_the_previous_result_scope(db, monkeypatch):
    """"All transactions" cannot be a refinement of the records already shown."""
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    conversation.active_data_scope = {"entityIds": [], "entityCount": 20, "query": {"category_slug": "food"}}
    conversation.active_analysis_state = {
        "query": {"category_slug": "food", "subcategory_slug": "delivery", "result_mode": "transaction_list"},
        "queries": [{"category_slug": "food", "result_mode": "transaction_list"}],
        "entityType": "transaction",
        "resultShapes": ["transaction_list"],
        "answerSummary": "I found 20 transactions.",
        "sourceMessageId": "irrelevant",
    }
    db.commit()
    seen = {}

    def router(text, taxonomy, today, timezone, recent, **kwargs):
        seen["workflow_context"] = kwargs.get("workflow_context")
        return CopilotDecision(tool="conversation", reply="Sure.", confidence=0.99, reason="Chat.")

    monkeypatch.setattr(conversation_service, "interpret_with_financial_copilot", router)
    monkeypatch.setattr(
        conversation_service,
        "validate_copilot_decision",
        lambda *args, **kwargs: CopilotDecisionValidation(outcome="approve", confidence=0.99, summary="Fine."),
    )

    handle_chat(db, user, conversation, "Draw a breakdown of all transactions in donut form")

    assert seen["workflow_context"]["activeDataScope"] is None
    assert seen["workflow_context"]["activeAnalysisState"] is None
