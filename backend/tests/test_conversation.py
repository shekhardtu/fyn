from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import Uuid, func, select

from app.api import delete_conversation, list_conversations
from app.config import get_settings
from app.database import Base
from app.models import AIAction, Account, AnalysisToolRun, AnalysisToolTemplate, Budget, Category, Conversation, DraftState, Goal, GoalContribution, Message, Subcategory, Tag, TaxonomyScope, Transaction, TransactionDraft, TransactionFieldValue, TransactionTag, User, UserAnalysisTool
from app.operations import operation_catalog
from app.operation_types import ContextRelationship
from app.operations.tools import OperationProposal
from app.seed import default_user
from app.services.agents import ClarificationOption, ClarificationRequest, CopilotDecision, QueryInterpretation, TaxonomyInterpretation, ToolGrounding, OperatorResult
from app.services import conversation as conversation_service
from app.services.agui import execute_widget_action
from app.schemas import ActionRequest, PendingAction, Widget, WidgetAction, WidgetType
from app.services.conversation import get_or_create_conversation, handle_action, handle_chat
from app.services.preferences import AnswerStyle, AnswerValidationMode, set_answer_style, set_answer_validation_mode, set_user_preference


def _operator_proposal(operation_id: str, inputs: dict) -> OperatorResult:
    """One strictly typed filesystem operation proposal, as the Operator emits it."""
    operation = operation_catalog().snapshot().operation(operation_id)
    return OperatorResult(operation=OperationProposal(
        operation_id=operation.id,
        version=operation.version,
        checksum=operation.checksum,
        inputs=inputs,
    ))


def _search_inputs(query: QueryInterpretation) -> dict:
    """Flatten a typed query into search_transactions operation inputs."""
    inputs = query.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
    # Authoritative scope IDs are server state; the operation contract has no
    # field for them, so a proposal can never author them.
    inputs.pop("scope_transaction_ids", None)
    return inputs


@pytest.fixture()
def agent_enabled(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()



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


def test_chat_reply_names_the_persisted_user_message(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)

    response = handle_chat(db, user, conversation, "₹2,000")

    stored = db.scalar(
        select(Message)
        .where(Message.conversation_id == conversation.id, Message.role == "user")
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    assert stored is not None and stored.content == "₹2,000"
    # The client renders the sent bubble under a provisional id; the reply is
    # how it learns the stored identity, so the two must always agree.
    assert response.user_message_id == stored.id


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


def test_active_draft_semantically_routes_subcategory_creation_with_state_context(db, monkeypatch, agent_enabled):
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

    def operator_runner(*args, **kwargs):
        captured.update(kwargs.get("workflow_context") or {})
        return _operator_proposal("manage_taxonomy", {
            "operation": "create_subcategory",
            "parent_category": "Construction",
        })

    monkeypatch.setattr(conversation_service, "run_operator", operator_runner)
    response = handle_chat(db, user, conversation, "make a new type under this one")

    assert captured["kind"] == "transaction_draft"
    assert captured["draftId"] == str(draft.id)
    assert captured["missingFields"] == ["subcategory"]
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


def test_compound_taxonomy_request_uses_one_plan_and_one_idempotent_mutation(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    original = "Create a category called Pet Care with a Vet Sub Category"
    routed = CopilotDecision(
        tool="manage_taxonomy",
        taxonomy=TaxonomyInterpretation(operation="create_category", name="Pet Care"),
        confidence=0.99,
        reason="The request creates a category.",
    )

    decision = conversation_service._normalize_compound_taxonomy_decision(original, routed)

    assert decision.taxonomy.operation == "create_taxonomy_path"
    assert decision.taxonomy.name == "Pet Care"
    assert decision.taxonomy.subcategories == ["Vet"]
    response = conversation_service._taxonomy_response(db, user, conversation, decision)
    assert response.pending_action.action == "create_taxonomy_path"
    assert response.widgets[0].type == "taxonomy_editor"
    assert response.widgets[0].data["name"] == "Pet Care"
    assert response.widgets[0].data["subcategories"] == ["Vet"]
    assert [action.action for action in response.widgets[0].actions] == [
        "create_taxonomy_path",
        "cancel_taxonomy_change",
    ]

    approved = execute_widget_action(ActionRequest(
        conversation_id=conversation.id,
        widget_id=response.widgets[0].id,
        action="create_taxonomy_path",
        payload={"name": "Pet Care", "subcategories": ["Vet"]},
    ), db, user)

    category = db.scalar(select(Category).where(
        Category.owner_user_id == user.id,
        Category.name == "Pet Care",
    ))
    assert category is not None
    assert category.scope == "user"
    children = list(db.scalars(
        select(Subcategory)
        .where(Subcategory.category_id == category.id)
        .order_by(Subcategory.name)
    ))
    assert [item.name for item in children] == ["Other", "Vet"]
    assert all(item.scope == "user" and item.owner_user_id == user.id for item in children)
    other = User(email="taxonomy-observer@example.com", display_name="Taxonomy observer")
    db.add(other)
    db.flush()
    assert all(
        item.name != "Pet Care"
        for item in conversation_service.TaxonomyRepository(db, other.id).expense_categories()
    )
    assert approved.widget_updates[0].widget.data["lifecycle"] == "completed"
    assert approved.widget_updates[0].widget.data["name"] == "Pet Care"
    assert approved.widget_updates[0].widget.data["subcategories"] == ["Vet"]
    assert len(approved.widget_updates[0].widget.data["resultIds"]) == 2

    replay = handle_action(
        db,
        user,
        conversation,
        "create_taxonomy_path",
        {"name": "Pet Care", "subcategories": ["Vet"]},
    )
    assert "no duplicate was created" in replay.message
    assert db.scalar(select(func.count()).select_from(Category).where(
        Category.owner_user_id == user.id,
        Category.name == "Pet Care",
    )) == 1
    assert db.scalar(select(func.count()).select_from(Subcategory).where(
        Subcategory.category_id == category.id,
        Subcategory.name == "Vet",
    )) == 1


def test_explicit_compound_taxonomy_contract_survives_a_partial_operator_route(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    original = "Create a category called Pet Care with a Vet Sub Category"
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        # The Operator's route preserved only the parent; the deterministic
        # compound-taxonomy policy must restore the explicit child.
        lambda *args, **kwargs: _operator_proposal("manage_taxonomy", {
            "operation": "create_category",
            "name": "Pet Care",
        }),
    )

    response = handle_chat(db, user, conversation, original)

    assert response.pending_action.action == "create_taxonomy_path"
    assert response.widgets[0].data["name"] == "Pet Care"
    assert response.widgets[0].data["subcategories"] == ["Vet"]


def test_compound_taxonomy_mutation_rolls_back_parent_when_a_child_fails(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    create_subcategory = conversation_service.TaxonomyRepository.create_subcategory

    def fail_on_requested_child(repository, category, name, slug):
        if name == "Vet":
            raise RuntimeError("simulated child failure")
        return create_subcategory(repository, category, name, slug)

    monkeypatch.setattr(
        conversation_service.TaxonomyRepository,
        "create_subcategory",
        fail_on_requested_child,
    )

    with pytest.raises(RuntimeError, match="simulated child failure"):
        handle_action(
            db,
            user,
            conversation,
            "create_taxonomy_path",
            {"name": "Pet Care", "subcategories": ["Vet"]},
        )

    assert db.scalar(select(func.count()).select_from(Category).where(
        Category.owner_user_id == user.id,
        Category.name == "Pet Care",
    )) == 0


def test_context_window_is_small_for_standalone_and_full_for_follow_up(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    routed_contexts = []
    long_reply = "Complete answer: " + ("context " * 120)

    def operator_runner(*args, **kwargs):
        routed_contexts.append(list(args[4]))
        reply = long_reply if args[0] == "sixth" else f"Answer to {args[0]}."
        return OperatorResult(reply=reply)

    monkeypatch.setattr(conversation_service, "run_operator", operator_runner)

    for prompt in ("first", "second", "third", "fourth", "fifth", "sixth", "seventh"):
        handle_chat(db, user, conversation, prompt)

    assert routed_contexts[-1] == [
        {"role": "user", "content": "fifth"},
        {"role": "assistant", "content": "Answer to fifth."},
        {"role": "user", "content": "sixth"},
        # Direct replies are stripped before they are persisted.
        {"role": "assistant", "content": long_reply.strip()},
    ]
    assert len(routed_contexts[-1][-1]["content"]) > 500

    handle_chat(db, user, conversation, "What about that?")

    assert routed_contexts[-1] == [
        {"role": "user", "content": "third"},
        {"role": "assistant", "content": "Answer to third."},
        {"role": "user", "content": "fourth"},
        {"role": "assistant", "content": "Answer to fourth."},
        {"role": "user", "content": "fifth"},
        {"role": "assistant", "content": "Answer to fifth."},
        {"role": "user", "content": "sixth"},
        {"role": "assistant", "content": long_reply.strip()},
        {"role": "user", "content": "seventh"},
        {"role": "assistant", "content": "Answer to seventh."},
    ]


def test_category_count_uses_authenticated_runtime_taxonomy_tool(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    captured = {}

    def operator_runner(*args, **kwargs):
        taxonomy_tool = next(
            tool
            for tool in kwargs["runtime_tools"]
            if tool.name == "read_user_expense_taxonomy"
        )
        taxonomy = taxonomy_tool.entrypoint()
        captured["tool_schema"] = taxonomy_tool.parameters
        return OperatorResult(
            # Deliberately wrong: the persisted answer must be derived from the
            # authenticated result envelope, not trusted model prose.
            reply="You have 999 expense categories.",
            tool_grounding=[ToolGrounding(
                name=taxonomy_tool.name,
                arguments={},
                result=str(taxonomy),
            )],
        )

    monkeypatch.setattr(conversation_service, "run_operator", operator_runner)

    response = handle_chat(db, user, conversation, "How many categories are there?")

    assert response.message == (
        "You have 10 expense categories: Bills, Education, Entertainment, Food, Health, Housing, Other, "
        "Personal care, Shopping, Travel."
    )
    assert response.citations[0].entity_type == "runtime_tool"
    assert response.citations[0].label == "Read User Expense Taxonomy result"
    assert captured["tool_schema"]["properties"] == {}


def test_a_retried_tool_call_answers_the_turn_instead_of_its_first_failure(db, monkeypatch, agent_enabled):
    """Production failure 2026-08-18 (run beb26337): the Operator's first
    analysis plan was rejected by the typed contract, it corrected the plan and
    the second call succeeded — and the turn still reported "nothing was
    computed" because only the first grounding item decided the verdict. The
    answer the tool actually computed must be the one that reaches the reader,
    and the failed attempt must not be cited as a data source.
    """
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(conversation_service, "_fast_path_decision", lambda *args, **kwargs: None)

    def operator_runner(*args, **kwargs):
        return OperatorResult(
            reply="Housing is running highest this month.",
            tool_grounding=[
                ToolGrounding(
                    name="run_financial_analysis",
                    arguments={"name": "first attempt"},
                    result={"tool": "run_financial_analysis", "data": {"error": {
                        "code": "invalid_analysis_plan",
                        "detail": "plan.transforms.0.operation: Field required",
                    }}},
                ),
                ToolGrounding(
                    name="run_financial_analysis",
                    arguments={"name": "corrected attempt"},
                    result={"tool": "run_financial_analysis", "data": {
                        "kind": "governed_analysis",
                        "message": "Housing is running highest this month.",
                    }},
                ),
            ],
        )

    monkeypatch.setattr(conversation_service, "run_operator", operator_runner)

    response = handle_chat(db, user, conversation, "Which categories are unusually high this month?")

    assert response.task_status == "succeeded"
    assert response.error_code is None
    assert response.message == "Housing is running highest this month."
    # The rejected attempt computed nothing, so it is not one of the sources
    # the answer rests on — and it must not become the inherited lineage.
    assert len(response.citations) == 1
    assert response.citations[0].query["arguments"] == {"name": "corrected attempt"}


def test_a_failed_calls_error_payload_is_not_evidence_for_a_figure(db, monkeypatch, agent_enabled):
    """Production shape 2026-08-18 (run 43ab5c2e): the first governed-SQL call
    failed and its error text carried incidental numbers — line numbers, offsets,
    dates lifted from the rejected query. Admitting those as evidence would let a
    figure the data never produced certify itself against an error message.
    """
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(conversation_service, "_fast_path_decision", lambda *args, **kwargs: None)

    def operator_runner(*args, **kwargs):
        return OperatorResult(
            reply="You spent ₹250 on food last month.",
            tool_grounding=[
                ToolGrounding(
                    name="run_governed_sql",
                    arguments={"purpose": "first attempt"},
                    # 250 appears only inside the failure detail.
                    result={"tool": "run_governed_sql", "data": {"error": {
                        "code": "execution_error",
                        "detail": "SyntaxError at character 250 of the submitted statement",
                    }}},
                ),
                ToolGrounding(
                    name="run_governed_sql",
                    arguments={"purpose": "corrected attempt"},
                    result={"tool": "run_governed_sql", "data": {
                        "kind": "governed_sql", "columns": ["spending_minor"],
                        "rows": [{"spending_minor": 990}],
                    }},
                ),
            ],
        )

    monkeypatch.setattr(conversation_service, "run_operator", operator_runner)

    response = handle_chat(db, user, conversation, "How much did I spend on food last month?")

    assert response.task_status == "degraded"
    assert response.error_code == "unsupported_money_claim"
    assert "₹250" not in response.message


def test_a_turn_whose_every_tool_call_failed_still_reports_the_failure(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    activity = []
    monkeypatch.setattr(conversation_service, "_fast_path_decision", lambda *args, **kwargs: None)

    def operator_runner(*args, **kwargs):
        return OperatorResult(
            reply="Housing is running highest this month.",
            tool_grounding=[
                ToolGrounding(
                    name="run_financial_analysis",
                    arguments={"name": "first attempt"},
                    result={"tool": "run_financial_analysis", "data": {"error": {
                        "code": "invalid_analysis_plan", "detail": "first failure",
                    }}},
                ),
                ToolGrounding(
                    name="run_financial_analysis",
                    arguments={"name": "second attempt"},
                    result={"tool": "run_financial_analysis", "data": {"error": {
                        "code": "template_binding_rejected", "detail": "second failure",
                    }}},
                ),
            ],
        )

    monkeypatch.setattr(conversation_service, "run_operator", operator_runner)

    response = handle_chat(
        db,
        user,
        conversation,
        "Which categories are unusually high this month?",
        activity.append,
    )

    assert response.task_status == "failed"
    # The attempt the model stopped on is the one the run reports.
    assert response.error_code == "template_binding_rejected"
    assert response.citations == []
    latest = {item["id"]: item for item in activity}
    assert latest["tool_result"]["status"] == "failed"
    assert latest["tool_result"]["tool"] == "run_financial_analysis"
    assert "template_binding_rejected" in latest["tool_result"]["detail"]
    assert "answer_validation" not in latest


def test_off_validation_is_skipped_when_every_tool_call_failed(
    db, monkeypatch, agent_enabled
):
    user = default_user(db)
    set_answer_validation_mode(db, user.id, AnswerValidationMode.OFF)
    conversation = get_or_create_conversation(db, user)
    activity = []
    monkeypatch.setattr(conversation_service, "_fast_path_decision", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: OperatorResult(
            reply="August was higher than July.",
            tool_grounding=[ToolGrounding(
                name="run_governed_sql",
                arguments={"purpose": "Compare August with July"},
                result={"tool": "run_governed_sql", "data": {"error": {
                    "code": "execution_error",
                    "detail": "The governed query did not execute.",
                }}},
            )],
        ),
    )

    response = handle_chat(
        db,
        user,
        conversation,
        "Compare August 1–19 spending with July 1–19",
        activity.append,
    )

    assert response.task_status == "failed"
    assert response.error_code == "execution_error"
    latest = {item["id"]: item for item in activity}
    assert latest["tool_result"]["status"] == "failed"
    assert latest["tool_result"]["label"] == "No successful tool result was available"
    assert latest["answer_validation"]["status"] == "completed"
    assert latest["answer_validation"]["label"] == "Answer validation: off"
    assert "Skipped evidence" in latest["answer_validation"]["detail"]
    assert not any(
        item["id"] == "answer_validation" and item["status"] == "failed"
        for item in activity
    )


def test_a_reply_with_a_number_no_analysis_supports_falls_back_to_the_analysis(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(conversation_service, "_fast_path_decision", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: OperatorResult(
            reply="You spent ₹999 across 2 transactions.",
            tool_grounding=[ToolGrounding(
                name="run_financial_analysis",
                arguments={},
                result={
                    "kind": "governed_analysis",
                    "message": "You spent ₹120 across 2 transactions from 2026-08-01 through 2026-08-14.",
                    "query_results": [{"name": "Total spend", "rows": [{"value": 12_000, "count": 2}]}],
                },
            )],
        ),
    )

    response = handle_chat(db, user, conversation, "How much did I spend?")

    assert "₹999" not in response.message
    # The harness that computed the figures also worded them; nothing re-renders.
    assert response.message == (
        "You spent ₹120 across 2 transactions from 2026-08-01 through 2026-08-14."
    )
    assert response.citations[0].query["tool"] == "run_financial_analysis"
    # The reader got a true answer, but not the one the pipeline composed. The
    # run says so, or an override no one can see reads as a clean success.
    assert response.task_status == "degraded"
    assert response.failure_stage == "grounding"
    assert response.error_code == "unsupported_money_claim"


def test_a_failed_tool_result_is_reported_as_a_failed_task(db, monkeypatch, agent_enabled):
    """A tool that reported a failure cannot be narrated as a completed one."""
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(conversation_service, "_fast_path_decision", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: OperatorResult(
            reply="I completed the analysis using your data.",
            tool_grounding=[ToolGrounding(
                name="run_financial_analysis",
                arguments={},
                result={"error": {
                    "stage": "plan_validation",
                    "code": "invalid_analysis_plan",
                    "detail": "plan.objective: Input should be 'descriptive'",
                }},
            )],
        ),
    )

    response = handle_chat(db, user, conversation, "How much do those three add up to?")

    assert "I completed" not in response.message
    assert response.task_status == "failed"
    assert response.failure_stage == "plan_validation"
    assert response.error_code == "invalid_analysis_plan"



def test_calculator_state_is_available_to_the_next_semantic_turn_without_phrase_gating(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    captured_contexts = []
    calls = 0

    def operator_runner(*args, **kwargs):
        nonlocal calls
        calls += 1
        captured_contexts.append(kwargs.get("workflow_context"))
        if calls == 1:
            return OperatorResult(
                reply="The EMI is ₹1,000.",
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
            )
        return OperatorResult(reply="Ready.")

    monkeypatch.setattr(conversation_service, "run_operator", operator_runner)

    handle_chat(db, user, conversation, "Calculate this loan")
    handle_chat(db, user, conversation, "Create a useful visual")

    state = captured_contexts[-1]["activeAnalysisState"]
    assert state["query"]["source_kind"] == "calculator"
    assert state["query"]["arguments"]["tenure_months"] == 12


def test_ungrounded_model_financial_figure_is_never_approved(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: OperatorResult(
            reply="Your EMI is ₹99,999.",
        ),
    )

    response = handle_chat(db, user, conversation, "Calculate my EMI")

    assert "₹99,999" not in response.message
    assert "couldn’t safely" in response.message or "couldn’t validate" in response.message
    assert response.citations == []


def test_sql_mode_never_replaces_rejected_evidence_with_legacy_analysis(
    db, monkeypatch, agent_enabled
):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    activity = []
    question = (
        "Compare my Food and Travel spending from May through August 19, "
        "group it by month, identify the largest merchants, and compare it "
        "with the previous three-month average."
    )
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: OperatorResult(
            reply="Food was ₹12,440 and Travel was ₹17,540."
        ),
    )

    response = handle_chat(db, user, conversation, question, activity.append)

    assert response.task_status == "failed"
    assert response.failure_stage == "grounding"
    assert response.error_code == "ungrounded_financial_claim"
    assert "₹12,440" not in response.message
    assert "didn’t substitute a simpler analysis" in response.message
    assert db.scalar(select(func.count()).select_from(AnalysisToolRun)) == 0
    latest = {event["id"]: event for event in activity}
    assert latest["operator"]["detail"] == "ungrounded_financial_claim"
    assert latest["classification"]["tool"] == "sql_analysis_policy"
    assert not any(
        event["label"] == "Offline capability compiler selected a validated plan"
        for event in activity
    )


def test_authoritative_empty_sql_result_is_a_successful_comparison_answer(
    db, monkeypatch, agent_enabled
):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    grounding = ToolGrounding(
        name="run_governed_sql",
        arguments={"purpose": "Compare the last three completed months."},
        result={
            "tool": "run_governed_sql",
            "data": {
                "kind": "governed_sql",
                "columns": ["category", "difference_minor", "volatility_rank"],
                "rows": [],
                "row_count": 0,
                "empty_result": True,
            },
        },
    )
    answer = (
        "No recorded expenses were available across the last three full months, "
        "so the months cannot be compared and no highest category can be identified."
    )
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: OperatorResult(
            reply=answer,
            tool_grounding=[grounding],
        ),
    )

    response = handle_chat(
        db,
        user,
        conversation,
        "Compare the last three full months and identify the highest category.",
    )

    assert response.task_status == "succeeded"
    assert response.error_code is None
    assert response.message == answer


def test_grounded_answer_repairs_missing_coverage_without_rerunning_sql(
    db, monkeypatch, agent_enabled
):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    grounding = ToolGrounding(
        name="run_governed_sql",
        arguments={"purpose": "Find the largest Food merchant."},
        result={
            "tool": "run_governed_sql",
            "data": {
                "kind": "governed_sql",
                "columns": ["category", "largest_merchant", "merchant_amount_minor"],
                "rows": [{
                    "category": "Food",
                    "largest_merchant": "Fresh Foods",
                    "merchant_amount_minor": 12_000,
                }],
            },
        },
    )
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: OperatorResult(
            reply="Food merchant spending was ₹120.",
            tool_grounding=[grounding],
        ),
    )
    repairs = []

    def repair(*args, **kwargs):
        repairs.append((args, kwargs))
        return "The top Food merchant was Fresh Foods at ₹120."

    monkeypatch.setattr(conversation_service, "repair_grounded_answer", repair)

    response = handle_chat(
        db,
        user,
        conversation,
        "Identify the largest merchant for Food.",
    )

    assert response.task_status == "succeeded"
    assert response.message == "The top Food merchant was Fresh Foods at ₹120."
    assert len(repairs) == 1


def test_grounded_answer_repairs_unsupported_claim_without_weakening_validation(
    db, monkeypatch, agent_enabled
):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    grounding = ToolGrounding(
        name="run_governed_sql",
        arguments={"purpose": "Show daily Food shares."},
        result={
            "tool": "run_governed_sql",
            "data": {
                "kind": "governed_sql",
                "columns": ["day", "share_percent"],
                "rows": [
                    {"day": "2026-08-24", "share_percent": 24.19},
                    {"day": "2026-08-26", "share_percent": 23.34},
                ],
            },
        },
    )
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: OperatorResult(
            reply="August 24 and 26 together accounted for 47.53%.",
            tool_grounding=[grounding],
        ),
    )
    repairs = []

    def repair(*args, **kwargs):
        repairs.append((args, kwargs))
        return "August 24 accounted for 24.19%, and August 26 accounted for 23.34%."

    monkeypatch.setattr(conversation_service, "repair_grounded_answer", repair)

    response = handle_chat(db, user, conversation, "Show daily Food spending shares.")

    assert response.task_status == "succeeded"
    assert response.error_code is None
    assert response.message == (
        "August 24 accounted for 24.19%, and August 26 accounted for 23.34%."
    )
    assert len(repairs) == 1
    action = db.scalar(
        select(AIAction)
        .where(
            AIAction.conversation_id == conversation.id,
            AIAction.action_type == "answer_evidence_repair",
        )
        .order_by(AIAction.created_at.desc())
    )
    assert action is not None
    assert action.status == "completed"
    assert action.payload_redacted["passed"] is True


def test_evidence_only_mode_skips_coverage_repair(db, monkeypatch, agent_enabled):
    user = default_user(db)
    set_answer_validation_mode(db, user.id, AnswerValidationMode.EVIDENCE_ONLY)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(
        conversation_service,
        "repair_grounded_answer",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("evidence-only mode must not run coverage repair")
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: OperatorResult(
            reply="Food merchant spending was ₹120.",
            tool_grounding=[ToolGrounding(
                name="run_governed_sql",
                arguments={},
                result={
                    "kind": "governed_sql",
                    "columns": ["category", "largest_merchant", "merchant_amount_minor"],
                    "rows": [{
                        "category": "Food",
                        "largest_merchant": "Fresh Foods",
                        "merchant_amount_minor": 12_000,
                    }],
                },
            )],
        ),
    )

    response = handle_chat(db, user, conversation, "Identify the largest merchant for Food.")

    assert response.task_status == "succeeded"
    assert response.message == "Food merchant spending was ₹120."


def test_off_mode_publishes_financial_read_draft_without_answer_checks(
    db, monkeypatch, agent_enabled
):
    user = default_user(db)
    set_answer_validation_mode(db, user.id, AnswerValidationMode.OFF)
    conversation = get_or_create_conversation(db, user)
    activity = []
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: OperatorResult(
            reply="You spent ₹999 on Food.",
        ),
    )

    response = handle_chat(
        db, user, conversation, "How much did I spend on Food?", activity.append
    )

    assert response.task_status == "succeeded"
    assert response.message == "You spent ₹999 on Food."
    assert response.citations == []
    validation_event = {item["id"]: item for item in activity}["answer_validation"]
    assert validation_event["label"] == "Answer validation: off"
    assert "Tenant policy" in validation_event["detail"]


def test_unresolved_transaction_count_never_becomes_a_money_draft(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    before = db.scalar(
        select(func.count())
        .select_from(TransactionDraft)
        .where(TransactionDraft.conversation_id == conversation.id)
    )
    monkeypatch.setattr(conversation_service, "run_operator", lambda *args, **kwargs: None)

    response = handle_chat(db, user, conversation, "Share 3 recent transactions")

    after = db.scalar(
        select(func.count())
        .select_from(TransactionDraft)
        .where(TransactionDraft.conversation_id == conversation.id)
    )
    assert after == before
    assert response.task_status == "failed"
    assert response.error_code == "unresolved_financial_query"
    assert "didn’t read or change any financial records" in response.message


def test_ambiguous_addition_prefers_hitl_transaction_type_selector(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    # The deterministic transaction contract is authoritative in Operator mode:
    # an ambiguous addition must reach HITL without consulting the model.
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("an ambiguous addition must not enter the Operator")
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


def test_minimal_amount_commands_share_the_zero_model_hitl_path(db, monkeypatch):
    user = default_user(db)

    for prompt in ("500", "Add 500"):
        conversation = get_or_create_conversation(db, user)
        response = handle_chat(db, user, conversation, prompt)

        assert response.widgets[0].type == "transaction_type_selector"
        assert response.pending_action.action == "select_transaction_type"
        draft = db.scalar(
            select(TransactionDraft).where(
                TransactionDraft.conversation_id == conversation.id
            )
        )
        assert draft.raw_text == prompt
        assert draft.amount_minor == 50_000
        assert draft.transaction_type == "unknown"


def test_transaction_shaped_generic_clarification_is_normalized_to_draft(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(conversation_service, "_fast_path_decision", lambda *args, **kwargs: None)
    clarification = ClarificationRequest(
        question="Is this an expense or income?",
        reason="The direction changes how the amount is recorded.",
        conflict_fields=["transaction_type"],
        options=[
            ClarificationOption(
                id="expense",
                label="Expense",
                resolution="Record the amount as an expense.",
            ),
            ClarificationOption(
                id="income",
                label="Income",
                resolution="Record the amount as income.",
            ),
        ],
    )
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal("request_clarification", {
            "clarification": clarification.model_dump(mode="json", exclude_none=True),
        }),
    )

    response = handle_chat(db, user, conversation, "Please add ₹500")

    assert response.widgets[0].type == "transaction_type_selector"
    assert response.pending_action.action == "select_transaction_type"
    assert all(widget.type != "clarification" for widget in response.widgets)
    draft = db.scalar(
        select(TransactionDraft).where(
            TransactionDraft.conversation_id == conversation.id
        )
    )
    assert draft.raw_text == "Please add ₹500"
    assert draft.amount_minor == 50_000
    assert draft.transaction_type == "unknown"
    action = db.scalar(
        select(AIAction).where(
            AIAction.action_type == "transaction_clarification_normalized"
        )
    )
    assert action.payload_redacted["conflictFields"] == ["transaction_type"]


def test_non_transaction_clarification_cannot_be_normalized_by_shared_amount_field(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(conversation_service, "_fast_path_decision", lambda *args, **kwargs: None)
    clarification = ClarificationRequest(
        question="Should ₹500 be the total budget or the monthly contribution?",
        reason="Those amounts create different savings plans.",
        conflict_fields=["amount"],
        options=[
            ClarificationOption(
                id="budget_total",
                label="Total budget",
                resolution="Use ₹500 as the total budget.",
            ),
            ClarificationOption(
                id="monthly_contribution",
                label="Monthly contribution",
                resolution="Use ₹500 as the monthly contribution.",
            ),
        ],
    )
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal("request_clarification", {
            "clarification": clarification.model_dump(mode="json", exclude_none=True),
        }),
    )

    response = handle_chat(db, user, conversation, "Create a ₹500 budget")

    assert response.widgets[0].type == "clarification"
    assert response.pending_action.action == "resolve_clarification"
    assert db.scalar(
        select(TransactionDraft).where(
            TransactionDraft.conversation_id == conversation.id
        )
    ) is None


def test_greeting_without_a_model_fails_closed_instead_of_replying_canned(db, monkeypatch):
    # Small talk belongs to the contextual agent. With no model configured
    # there is no canned greeting fallback: the turn fails closed honestly,
    # and it still never creates a transaction draft.
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    activity = []

    response = handle_chat(db, user, conversation, "Hi", activity.append)

    assert response.task_status == "failed"
    assert response.error_code == "unresolved_financial_query"
    assert response.widgets == []
    assert db.scalar(select(TransactionDraft)) is None
    assert next(event for event in activity if event["id"] == "classification")["durationMs"] == 0


def test_model_enabled_acknowledgement_uses_recent_context_instead_of_template(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    seed = handle_chat(db, user, conversation, "Hi")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    captured = {}

    def operator_runner(text, _taxonomy, _today, _timezone, recent_context, **kwargs):
        captured["text"] = text
        captured["context"] = recent_context
        kwargs["on_delta"]("Got it — I’ll wait for your next request.")
        return OperatorResult(
            reply="Got it — I’ll wait for your next request.",
            streamed_live=True,
        )

    monkeypatch.setattr(conversation_service, "run_operator", operator_runner)
    deltas = []

    response = handle_chat(
        db,
        user,
        conversation,
        "OK",
        text_delta_callback=lambda message_id, delta: deltas.append((message_id, delta)),
    )

    assert captured["text"] == "OK"
    assert captured["context"][-1]["content"] == seed.message
    assert response.message == "Got it — I’ll wait for your next request."
    assert "What would you like to look at next?" not in response.message
    assert "".join(delta for _message_id, delta in deltas) == response.message
    get_settings.cache_clear()


def test_repeated_assistant_text_is_recognized_without_operator_or_echo(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    previous = handle_chat(db, user, conversation, "Hi").message
    repeated_input = f"Earlier you said: {previous}"
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()

    def operator_runner(text, _taxonomy, _today, _timezone, recent_context, **kwargs):
        assert text == repeated_input
        assert recent_context[-1]["content"] == previous
        return OperatorResult(
            reply="It looks like you pasted my last reply back to me. Were you testing context, or did you want to continue from there?"
        )

    monkeypatch.setattr(conversation_service, "run_operator", operator_runner)

    response = handle_chat(db, user, conversation, repeated_input)

    assert response.message != repeated_input
    assert "pasted my last reply" in response.message
    get_settings.cache_clear()


def test_operator_grounded_read_persists_and_emits_the_same_verified_reply(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
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
        "run_operator",
        lambda *args, **kwargs: OperatorResult(
            reply="You spent ₹1,250 across 3 transactions from August 1 through August 14.",
            tool_grounding=[grounding],
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


def test_explicit_transfer_enters_draft_without_any_model_operator(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("an explicit transfer must not enter the Operator")
        ),
    )

    response = handle_chat(db, user, conversation, "Transfer ₹5,000 from Salary or Savings")

    assert response.widgets[0].type == "account_selector"
    assert response.pending_action.action == "select_account"
    assert response.widgets[0].data["role"] == "source_account"
    draft = db.scalar(
        select(TransactionDraft).where(
            TransactionDraft.conversation_id == conversation.id
        )
    )
    assert draft.transaction_type == "transfer"
    assert draft.missing_fields == ["source_account", "destination_account"]
    get_settings.cache_clear()


def test_complete_explicit_transaction_uses_zero_model_passes_in_operator_mode(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a complete explicit transaction must not enter the Operator")
        ),
    )
    activities = []

    response = handle_chat(
        db,
        user,
        conversation,
        "Add 100 rupee to expense, as Food Category, and subcategory Coffee",
        activity_callback=activities.append,
    )

    assert response.message == "Added ₹100 expense under Food → Coffee. You can edit or remove it below."
    assert response.widgets[0].type == "transaction_preview"
    transaction = db.scalar(select(Transaction))
    assert transaction.amount_minor == 10_000
    assert transaction.transaction_type == "expense"
    assert db.get(Category, transaction.category_id).slug == "food"
    assert db.get(Subcategory, transaction.subcategory_id).slug == "coffee"
    assert db.scalar(select(AIAction).where(AIAction.action_type == "operator_handoff")) is None
    request_event = next(
        event
        for event in activities
        if event["id"] == "request" and event["status"] == "completed"
    )
    assert request_event["input"]["userMessageId"] != request_event["output"]["assistantMessageId"]
    assert request_event["output"]["assistantMessageId"] == str(response.message_id)
    assert not any(
        event["id"].startswith("model_pass_")
        or event["id"] in {"operator", "planner", "validator"}
        for event in activities
    )
    get_settings.cache_clear()


def test_completed_multi_pass_trace_events_carry_both_input_and_output(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: OperatorResult(operation=(lambda operation: OperationProposal(
            operation_id=operation.id,
            version=operation.version,
            checksum=operation.checksum,
            inputs={
                "transaction_type": "income",
                "amount_minor": 50_000,
                "currency": "INR",
                "transaction_date": "2026-08-14",
                "explicit_fields": ["transaction_type", "amount_minor", "transaction_date"],
            },
        ))(conversation_service.operation_catalog().snapshot().operation("create_transaction_draft"))),
    )
    activities = []

    response = handle_chat(
        db,
        user,
        conversation,
        "Please record yesterday's five hundred rupee income",
        activity_callback=activities.append,
    )

    assert response.widgets[0].type == "transaction_preview"
    completed = [event for event in activities if event["status"] == "completed"]
    traced_stages = {
        "request",
        "operator",
        "operation_compilation",
        "classification",
        "validator",
        "execution",
        "grounding",
    }
    relevant = [event for event in completed if event["id"] in traced_stages]
    assert traced_stages <= {event["id"] for event in relevant}
    assert all(event["input"] is not None for event in relevant)
    assert all(event["output"] is not None for event in relevant)
    get_settings.cache_clear()


def test_operator_cannot_stream_a_mutation_claim_instead_of_handoff(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()

    def invalid_direct_reply(*_args, **kwargs):
        kwargs["on_delta"]("I added the expense.")
        return OperatorResult(reply="I added the expense.", streamed_live=True)

    monkeypatch.setattr(conversation_service, "run_operator", invalid_direct_reply)

    with pytest.raises(RuntimeError, match="authority postcondition"):
        handle_chat(
            db,
            user,
            conversation,
            "Log ₹500 as a debit",
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


def test_context_relationship_uses_one_typed_contract_and_distinguishes_amount_bounds():
    active_state = {"query": {"transaction_type": "expense"}}

    assert conversation_service._context_relationship(
        "keep the same period",
        active_state,
    ) is ContextRelationship.FOLLOW_UP
    assert conversation_service._context_relationship(
        "No, I meant Housing expenses",
        active_state,
    ) is ContextRelationship.CORRECTION
    assert conversation_service._context_relationship(
        "show expenses above 5000",
        active_state,
    ) is ContextRelationship.STANDALONE
    assert conversation_service._context_relationship(
        "Compare my spending this month with the same elapsed days last month",
        active_state,
    ) is ContextRelationship.STANDALONE


def test_expense_pattern_savings_request_runs_contextual_governed_analysis(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹300 on ice cream today")
    handle_chat(db, user, conversation, "Spent ₹100 on a cab today")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a known governed analysis should not pay for a handoff model")
        ),
    )

    response = handle_chat(
        db,
        user,
        conversation,
        "Let's discuss my expense pattern in details so convince on savings",
    )

    assert response.widgets == []
    assert "recorded expenses" in response.message
    assert "**Three-month spending allocation**" in response.message
    assert "Food" in response.message
    get_settings.cache_clear()


def test_transaction_clarification_uses_governed_final_copy(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()

    response = handle_chat(db, user, conversation, "₹500")

    assert response.widgets[0].type == "transaction_type_selector"
    assert response.message == "Is this an expense, income, transfer, or something else?"
    get_settings.cache_clear()


def test_repeat_question_skips_planner_validator_and_retrieval_passes(db, monkeypatch):
    from app.services.analysis_harness import execute_analysis_template

    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add(Transaction(
        user_id=user.id, transaction_type="expense", amount_minor=20_000, currency="INR",
        merchant_name="Repeat seed", category_id=food.id,
        transaction_at=datetime.now(timezone.utc),
    ))
    db.flush()
    question = "How much food spending so far this period?"
    today = conversation_service._local_today(user)
    from app.services.semantic import AnalysisPlan, AnalysisToolProposal, FinanceFilter, FinanceQueryPlan
    proposal = AnalysisToolProposal(
        name="Food spending by category",
        description="Summarize recorded food expenses for the current period.",
        intent_signature="current food spending",
        plan=AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Filter recorded expenses to food", "Aggregate the validated period"],
            queries=[FinanceQueryPlan(
                name="Food spending this period",
                metric="gross_spend",
                filters=[FinanceFilter(field="category", value="food")],
                start_date=today.replace(day=1),
                end_date=today,
            )],
        ),
    )
    first = execute_analysis_template(db, user.id, conversation.id, today, proposal, question=question)
    assert first.run.status == "completed"

    def must_not_run(name):
        def _fail(*_args, **_kwargs):
            raise AssertionError(f"{name} must not run for an exact repeat question")
        return _fail

    from app.services import analysis_tools as analysis_tools_module

    monkeypatch.setattr(conversation_service, "run_operator", must_not_run("the Operator model pass"))
    monkeypatch.setattr(analysis_tools_module, "retrieve_templates", must_not_run("the template retrieval rung"))

    activity = []
    response = handle_chat(db, user, conversation, question, activity.append)
    repeated_run = db.scalar(
        select(AnalysisToolRun).order_by(AnalysisToolRun.created_at.desc()).limit(1)
    )
    latest_activity = {item["id"]: item for item in activity}

    assert response.task_status == "succeeded"
    assert repeated_run.template_id == first.template.id
    assert next(
        stage for stage in repeated_run.trace if stage["stage"] == "template_match"
    )["values"] == {"templateId": str(first.template.id), "matched": True}
    assert "operator" not in latest_activity
    assert "planner" not in latest_activity
    assert latest_activity["retrieval"]["tool"] == "template_replay"
    assert latest_activity["validator"]["tool"] == "template_replay_policy"


def test_repeat_guarantee_replays_evidence_but_operator_composes_the_answer(db, monkeypatch):
    from app.services.analysis_harness import execute_analysis_template
    from app.services.semantic import AnalysisPlan, AnalysisToolProposal, AnalysisTransform, FinanceQueryPlan

    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    today = conversation_service._local_today(user)
    question = (
        "Based only on these three months, guarantee me a recurring ₹10,000 monthly "
        "cost reduction without changing housing, food, health, transport, education, or bills."
    )
    start = conversation_service.shift_month(today.replace(day=1), -2)
    proposal = AnalysisToolProposal(
        name="Three-month cost reduction evidence",
        description="Review recorded category and merchant spending over three months.",
        intent_signature="three month recurring cost reduction guarantee",
        plan=AnalysisPlan(
            objective="recommendation",
            analysis_type="semantic_query",
            context_sources=["budgets"],
            safe_reasoning_summary=[
                "Review observed category and merchant spending",
                "Do not turn historical candidates into a guarantee",
            ],
            queries=[
                FinanceQueryPlan(
                    name="Spending by category",
                    metric="gross_spend",
                    dimensions=["category"],
                    start_date=start,
                    end_date=today,
                    order="desc",
                    limit=20,
                ),
                FinanceQueryPlan(
                    name="Spending by merchant",
                    metric="gross_spend",
                    dimensions=["merchant"],
                    start_date=start,
                    end_date=today,
                    order="desc",
                    limit=20,
                ),
            ],
            transforms=[AnalysisTransform(
                name="Category spending comparison",
                operation="compare_totals",
                query_name="Spending by category",
                dimension="category",
            )],
        ),
    )
    first = execute_analysis_template(
        db,
        user.id,
        conversation.id,
        today,
        proposal,
        question=question,
    )

    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    # This case exercises the legacy replay contract explicitly. Default SQL
    # mode intentionally bypasses AnalysisPlan replay.
    monkeypatch.setenv("ANALYSIS_QUERY_MODE", "hybrid")
    get_settings.cache_clear()
    expected = (
        "I can’t guarantee ₹10,000 in recurring monthly savings from three months of "
        "history. The replayed figures show past spending candidates, but they do not "
        "prove those costs will recur while preserving every protected category and constraint."
    )

    def composing_operator(*_args, **kwargs):
        replay_tool = next(
            tool
            for tool in kwargs["analysis_tools"]
            if tool.name == analysis_tools_module.REPLAY_ANALYSIS_TOOL_NAME
        )
        payload = replay_tool.entrypoint()
        assert payload["kind"] == "governed_analysis"
        assert payload["reused_template"] is True
        return OperatorResult(
            reply=expected,
            tool_grounding=[ToolGrounding(
                name=replay_tool.name,
                arguments={},
                result={"tool": replay_tool.name, "data": payload},
            )],
        )

    from app.services import analysis_tools as analysis_tools_module

    monkeypatch.setattr(conversation_service, "run_operator", composing_operator)
    activity = []
    response = handle_chat(db, user, conversation, question, activity.append)
    latest_activity = {item["id"]: item for item in activity}
    repeated_run = db.scalar(
        select(AnalysisToolRun).order_by(AnalysisToolRun.created_at.desc()).limit(1)
    )

    assert response.message == expected
    assert response.task_status == "succeeded"
    assert "I ran 2 validated analyses" not in response.message
    assert repeated_run.template_id == first.template.id
    assert latest_activity["retrieval"]["tool"] == "template_replay"
    assert latest_activity["validator"]["tool"] == "template_replay_composition_policy"
    assert latest_activity["operator"]["status"] == "completed"
    get_settings.cache_clear()


def test_derived_financial_analysis_reaches_the_agent_loop_with_analysis_tools(db, monkeypatch):
    user = default_user(db)
    set_answer_style(db, user.id, AnswerStyle.CONCISE)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    captured: dict = {}

    def capture_operator(*_args, **kwargs):
        captured["analysis_tools"] = kwargs.get("analysis_tools") or []
        captured["answer_style"] = kwargs.get("answer_style")
        captured["presentation"] = kwargs.get("presentation")
        return OperatorResult(reply="The agent loop received the request.", tool_grounding=[])

    monkeypatch.setattr(conversation_service, "run_operator", capture_operator)

    response = handle_chat(
        db,
        user,
        conversation,
        "Forecast my cash flow and rank the expense drivers.",
    )

    assert response.message == "The agent loop received the request."
    tool_names = [tool.name for tool in captured["analysis_tools"]]
    # Default SQL mode exposes the unrestricted tenant-governed query author,
    # not the finite AnalysisPlan transform grammar.
    assert "run_governed_sql" in tool_names
    assert "run_financial_analysis" not in tool_names
    assert captured["answer_style"] is AnswerStyle.CONCISE
    assert captured["presentation"].style is AnswerStyle.CONCISE
    assert captured["presentation"].provider_verbosity == "low"
    get_settings.cache_clear()


def test_self_contained_calculator_keeps_agent_but_prunes_ledger_capabilities(
    db,
    monkeypatch,
    agent_enabled,
):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    captured: dict = {}

    def capture_operator(*_args, **kwargs):
        captured.update(kwargs)
        return OperatorResult(reply="I could not complete the calculation.")

    monkeypatch.setattr(conversation_service, "run_operator", capture_operator)
    monkeypatch.setattr(
        conversation_service,
        "get_traits",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stored traits must not load for a hypothetical calculator")
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "current_insights",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stored insights must not load for a hypothetical calculator")
        ),
    )

    handle_chat(
        db,
        user,
        conversation,
        "What is the monthly EMI on a ₹12 lakh loan at 8% for 5 years?",
    )

    assert [tool.name for tool in captured["runtime_tools"]] == [
        conversation_service.FINANCIAL_CALCULATOR_TOOL_NAME,
    ]
    assert captured["analysis_tools"] == []
    assert "userTraits" not in captured["workflow_context"]
    assert "verifiedInsights" not in captured["workflow_context"]
    assert conversation_service._is_self_contained_calculator_request(
        "What is the EMI on ₹12 lakh at 8% for 5 years?",
        ContextRelationship.STANDALONE,
    )
    assert not conversation_service._is_self_contained_calculator_request(
        "Compare my spending with the EMI on ₹12 lakh at 8% for 5 years.",
        ContextRelationship.STANDALONE,
    )
    assert not conversation_service._is_self_contained_calculator_request(
        "What about a 7-year tenure?",
        ContextRelationship.FOLLOW_UP,
    )


def test_social_greeting_keeps_contextual_agent_without_finance_capabilities(
    db,
    monkeypatch,
    agent_enabled,
):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    captured: dict = {}

    def capture_operator(*_args, **kwargs):
        captured.update(kwargs)
        return OperatorResult(reply="Doing well—glad you're here.")

    monkeypatch.setattr(conversation_service, "run_operator", capture_operator)
    monkeypatch.setattr(
        conversation_service,
        "get_traits",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stored traits must not load for a greeting")
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "current_insights",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stored insights must not load for a greeting")
        ),
    )

    response = handle_chat(db, user, conversation, "How are you doing?")

    assert response.message == "Doing well—glad you're here."
    assert captured["runtime_tools"] == []
    assert captured["analysis_tools"] == []
    assert captured["workflow_context"]["kind"] == "conversation_only"
    assert conversation_service._is_social_conversation_only("Hi")
    assert conversation_service._is_social_conversation_only("Hello, how are you doing?")
    assert not conversation_service._is_social_conversation_only("Hi, log ₹500 for lunch")


def test_explicit_no_record_explanation_keeps_model_but_loads_no_user_data(
    db,
    monkeypatch,
    agent_enabled,
):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    captured: dict = {}

    def capture_operator(*_args, **kwargs):
        captured.update(kwargs)
        return OperatorResult(reply="Principal is the amount borrowed; interest is its borrowing cost.")

    monkeypatch.setattr(conversation_service, "run_operator", capture_operator)
    monkeypatch.setattr(
        conversation_service,
        "get_traits",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stored traits must not load when the user excludes records")
        ),
    )
    monkeypatch.setattr(
        conversation_service,
        "current_insights",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stored insights must not load when the user excludes records")
        ),
    )

    response = handle_chat(
        db,
        user,
        conversation,
        "Explain principal versus interest without using my records.",
    )

    assert response.task_status == "succeeded"
    assert captured["runtime_tools"] == []
    assert captured["analysis_tools"] == []
    assert captured["workflow_context"]["kind"] == "knowledge_only"

def test_chat_reports_safe_timed_agent_activity(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    activity = []

    handle_chat(db, user, conversation, "How much did I spend this month?", activity.append)

    latest = {event["id"]: event for event in activity}
    assert latest["classification"]["status"] == "completed"
    assert latest["execution"]["tool"] == "run_analysis_harness"
    assert latest["execution"]["durationMs"] >= 0
    assert "structured data source" in latest["grounding"]["detail"]
    assert latest["grounding"]["cumulativeMs"] >= latest["execution"]["cumulativeMs"]


def test_known_complex_comparison_uses_validated_offline_fallback_after_agent_failure(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹300 on ice cream today")
    handle_chat(db, user, conversation, "Spent ₹100 on a cab today")
    activity = []
    response = handle_chat(db, user, conversation, "Compare food and transport this month. Which is larger and by how much?", activity.append)
    assert response.message.startswith("Food is larger at ₹300, compared with ₹100 for Travel; the difference is ₹200.")
    assert response.widgets == []
    assert "| Food | ₹300 |" in response.message
    assert any(event.get("badge") == "Saved" for event in activity)
    assert any(event.get("badge") == "Validated" for event in activity)
    assert any(event["id"] == "classification" and event["label"] == "Offline capability compiler selected a validated plan" for event in activity)


def test_llm_classifier_routes_to_grounded_tool_without_using_template_keywords(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹400 on a cab today")
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal("search_transactions", _search_inputs(
            QueryInterpretation(metric="spending_summary", category_slug="travel", start_date=date.today(), end_date=date.today()),
        )),
    )

    response = handle_chat(db, user, conversation, "What went on moving around today?")

    assert response.widgets == []
    assert "**Travel spending · Today**" in response.message
    assert "₹400" in response.message


def test_typed_query_route_cannot_be_overridden_by_prompt_keywords(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    decision = CopilotDecision(
        tool="get_biggest_expenses",
        confidence=0.99,
        reason="The user requested their largest expenses.",
    )

    response = conversation_service._query_response(
        db, user, conversation, "Biggest expenses, or is this a duplicate needing review?", decision
    )

    # The typed route is authoritative: the reconciliation keywords in the text
    # must not steer the response away from the routed capability. Ranked
    # records are read through the transaction_list tool now, so the routed
    # capability refuses rather than answering with someone else's shape.
    assert [widget.type for widget in response.widgets] == []
    assert response.task_status == "failed"
    assert response.error_code == "unresolved_financial_query"


def test_llm_classifier_can_supply_a_structured_transaction(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal("create_transaction_draft", {
            "transaction_type": "expense",
            "amount_minor": 20_000,
            "transaction_date": date.today().isoformat(),
            "category_slug": "food",
            "subcategory_slug": "ice_cream",
        }),
    )

    response = handle_chat(db, user, conversation, "two hundred for a frozen dessert")

    assert response.widgets[0].type == "transaction_preview"
    assert response.widgets[0].data["amountMinor"] == 20_000
    assert response.widgets[0].data["category"] == "Food"


def test_fast_gate_handles_bare_amount_without_calling_llm(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)

    activity = []
    response = handle_chat(db, user, conversation, "₹1,234", activity.append)

    assert response.widgets[0].type == "transaction_type_selector"
    assert next(event for event in reversed(activity) if event["id"] == "classification")["tool"] == "create_transaction_draft"
    assert "Detected a standalone amount" in next(event for event in reversed(activity) if event["id"] == "classification")["detail"]


def test_contextual_transaction_shape_bypasses_the_stateless_fast_write_gate():
    prompt = "same as before: expense 5000"

    assert conversation_service._fast_path_decision(
        prompt,
        date(2026, 8, 20),
        context_relationship=ContextRelationship.FOLLOW_UP,
    ) is None


def test_compound_follow_up_read_reaches_contextual_operator_without_creating_a_transaction(
    db,
    monkeypatch,
    agent_enabled,
):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    prior_query = {
        "metric": "amount",
        "result_mode": "summary",
        "operation": "rank",
        "group_by": "merchant",
        "transaction_type": "expense",
        "start_date": "2026-08-01",
        "end_date": "2026-08-19",
        "limit": 5,
        "use_active_scope": False,
        "scope_transaction_ids": [],
    }
    conversation.active_analysis_state = {
        "sourceMessageId": str(uuid4()),
        "answerSummary": "Prior merchant ranking.",
        "entityType": "transaction",
        "query": prior_query,
        "queries": [prior_query],
        "resultShapes": ["summary"],
    }
    db.commit()
    captured_workflow = {}

    def operator_runner(*args, **kwargs):
        captured_workflow.update(kwargs.get("workflow_context") or {})
        return OperatorResult(reply="No matching expenses were found.")

    monkeypatch.setattr(conversation_service, "run_operator", operator_runner)
    before_transactions = db.scalar(select(func.count()).select_from(Transaction))
    before_drafts = db.scalar(select(func.count()).select_from(TransactionDraft))

    response = handle_chat(
        db,
        user,
        conversation,
        "drop Swiggy, keep the same period, and show expenses above 8000",
    )

    assert response.message == "No matching expenses were found."
    assert captured_workflow["contextRelationship"] == "follow_up"
    assert captured_workflow["activeAnalysisState"]["query"] == prior_query
    assert db.scalar(select(func.count()).select_from(Transaction)) == before_transactions
    assert db.scalar(select(func.count()).select_from(TransactionDraft)) == before_drafts


def test_effect_gate_blocks_a_model_read_to_transaction_escalation(
    db,
    monkeypatch,
    agent_enabled,
):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal("create_transaction_draft", {
            "transaction_type": "expense",
            "amount_minor": 800_000,
            "merchant": "Delivery",
            "transaction_date": date.today().isoformat(),
            "category_slug": "food",
            "subcategory_slug": "delivery",
        }),
    )

    response = handle_chat(
        db,
        user,
        conversation,
        "drop Swiggy, keep the same period, and show expenses above 8000",
    )

    assert response.task_status == "failed"
    assert response.failure_stage == "effect_authorization"
    assert response.error_code == "read_to_mutation_escalation"
    assert db.scalar(select(Transaction)) is None
    assert db.scalar(select(TransactionDraft)) is None
    authorization = db.scalar(
        select(AIAction)
        .where(AIAction.action_type == "effect_authorization")
        .order_by(AIAction.created_at.desc(), AIAction.id.desc())
    )
    assert authorization.payload_redacted["outcome"] == "deny"


def test_plain_numeric_follow_up_cannot_enter_the_standalone_transaction_shortcut(
    db,
    monkeypatch,
    agent_enabled,
):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    db.add(Message(
        conversation_id=conversation.id,
        role="user",
        content="Help me plan how long to save.",
        widgets=[],
        citations=[],
    ))
    db.flush()
    db.add(Message(
        conversation_id=conversation.id,
        role="assistant",
        content="How many months are you saving for?",
        widgets=[],
        citations=[],
    ))
    db.commit()
    captured_workflow = {}

    def operator_runner(*args, **kwargs):
        captured_workflow.update(kwargs.get("workflow_context") or {})
        return _operator_proposal("create_transaction_draft", {
            "transaction_type": "expense",
            "amount_minor": 2_400,
            "transaction_date": date.today().isoformat(),
            "category_slug": "other",
            "subcategory_slug": "other",
        })

    monkeypatch.setattr(conversation_service, "run_operator", operator_runner)

    response = handle_chat(db, user, conversation, "24")

    assert captured_workflow["contextRelationship"] == "follow_up"
    assert captured_workflow["intentContract"]["requested_effect"] == "unknown"
    assert response.widgets[0].type == "clarification"
    assert response.pending_action is not None
    assert db.scalar(select(Transaction)) is None
    assert db.scalar(select(TransactionDraft)) is None


def test_model_value_question_is_persisted_as_a_durable_custom_continuation(
    db,
    monkeypatch,
    agent_enabled,
):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(
        conversation_service,
        "_fast_path_decision",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: OperatorResult(
            reply="How many months are you saving for?",
        ),
    )

    response = handle_chat(
        db,
        user,
        conversation,
        "Calculate how much I should save each month",
    )

    assert response.message == "How many months are you saving for?"
    assert response.widgets[0].type == "clarification"
    assert response.widgets[0].data["options"] == []
    assert response.widgets[0].data["allowCustom"] is True
    assert response.pending_action.continuation["schemaVersion"] == 4
    assert response.pending_action.continuation["customStrategy"] == "route_once"


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


def test_saved_transaction_edit_ignores_null_transport_placeholders(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    created = handle_chat(db, user, conversation, "Spent ₹2,000 at Toit today")

    response = execute_widget_action(ActionRequest(
        conversation_id=conversation.id,
        widget_id=created.widgets[0].id,
        action="edit_saved_transaction",
        payload={
            "transactionId": created.widgets[0].data["transactionId"],
            "amountMinor": None,
            "merchant": None,
            "transactionDate": None,
            "transactionType": None,
            "categorySlug": None,
            "subcategorySlug": None,
            "location": None,
            "spendNature": None,
            "tags": None,
        },
    ), db, user)

    assert response.widgets[0].type == "transaction_edit"
    assert response.widgets[0].data["amountMinor"] == 200_000
    assert response.widgets[0].data["merchant"] == "Toit"


def test_noop_conversation_edit_is_truthful_and_does_not_invent_a_version(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    created = handle_chat(db, user, conversation, "Spent ₹2,000 at Toit today")

    unchanged = handle_action(db, user, conversation, "update_saved_transaction", {
        "transactionId": created.widgets[0].data["transactionId"],
        "amountMinor": 200_000,
    })

    assert unchanged.message == "No changes were needed; this transaction is already up to date."
    assert unchanged.widgets[0].data["status"] == "Unchanged"
    assert unchanged.widgets[0].data["rowVersion"] == 1
    replaced = next(update.widget for update in unchanged.widget_updates if update.widget_id == created.widgets[0].id)
    assert replaced.data["rowVersion"] == replaced.data["supersededByVersion"] == 1


def test_conversational_amount_correction_prefills_the_preceding_saved_transaction(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    created = handle_chat(
        db,
        user,
        conversation,
        "Spent ₹6,00,000 on clothing for the car cost today",
    )
    transaction_id = created.widgets[0].data["transactionId"]
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal(
            "edit_transaction",
            {"target_mode": "preceding_card", "amount_minor": 64_000_000},
        ),
    )

    prepared = handle_chat(
        db,
        user,
        conversation,
        "Can I correct the car cost to ₹6,40,000?",
    )

    # Conversation can now enter the canonical saved-edit workflow. Merely
    # preparing the correction does not mutate the ledger before HITL Apply.
    transaction = db.get(Transaction, UUID(transaction_id))
    assert transaction.amount_minor == 60_000_000
    assert prepared.widgets[0].type == "transaction_edit"
    assert prepared.widgets[0].data["transactionId"] == transaction_id
    assert prepared.widgets[0].data["amountMinor"] == 64_000_000
    assert prepared.pending_action.action == "update_saved_transaction"
    assert "Apply changes" in prepared.message

    updated = handle_action(
        db,
        user,
        conversation,
        "update_saved_transaction",
        {"transactionId": transaction_id, "amountMinor": 64_000_000},
    )

    assert db.get(Transaction, UUID(transaction_id)).amount_minor == 64_000_000
    assert updated.widgets[0].data["status"] == "Updated"
    prior_card = next(
        update.widget
        for update in updated.widget_updates
        if update.widget_id == created.widgets[0].id
    )
    assert prior_card.actions == []
    assert prior_card.data["lifecycle"] == "completed"
    assert prior_card.data["rowVersion"] == 1
    assert prior_card.data["supersededByVersion"] == 2
    assert prior_card.data["supersededByWidgetId"] == updated.widgets[0].id
    stored_created = db.get(Message, created.message_id)
    assert stored_created.widgets[0]["data"]["supersededByVersion"] == 2

    amended_again = handle_action(
        db,
        user,
        conversation,
        "update_saved_transaction",
        {"transactionId": transaction_id, "amountMinor": 65_000_000},
    )
    version_two_card = next(
        update.widget
        for update in amended_again.widget_updates
        if update.widget_id == updated.widgets[0].id
    )
    assert version_two_card.data["rowVersion"] == 2
    assert version_two_card.data["supersededByVersion"] == 3
    assert all(update.widget_id != created.widgets[0].id for update in amended_again.widget_updates)
    db.refresh(stored_created)
    assert stored_created.widgets[0]["data"]["supersededByVersion"] == 2


def test_latest_expense_qualifier_is_applied_before_latest_selection(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    expense = handle_chat(db, user, conversation, "Spent ₹250 on coffee today")
    handle_chat(db, user, conversation, "Salary ₹500 credited today")
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal(
            "edit_transaction",
            {
                "target_mode": "latest_entered",
                "target_transaction_type": "expense",
                "amount_minor": 30_000,
            },
        ),
    )

    prepared = handle_chat(db, user, conversation, "Correct my last expense to ₹300")

    assert prepared.widgets[0].type == "transaction_edit"
    assert prepared.widgets[0].data["transactionId"] == expense.widgets[0].data["transactionId"]
    assert prepared.widgets[0].data["transactionType"] == "expense"
    assert prepared.widgets[0].data["amountMinor"] == 30_000


def test_relative_amount_correction_uses_the_existing_amount_as_its_base(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    created = handle_chat(db, user, conversation, "Spent ₹250 on coffee today")
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal(
            "edit_transaction",
            {
                "target_mode": "preceding_card",
                # Mirrors the dangerous model interpretation the old path
                # made; deterministic arithmetic must still recover ₹350.
                "amount_minor": 10_000,
            },
        ),
    )

    prepared = handle_chat(db, user, conversation, "Increase that transaction by ₹100")

    assert prepared.widgets[0].data["transactionId"] == created.widgets[0].data["transactionId"]
    assert prepared.widgets[0].data["amountMinor"] == 35_000


def test_stale_conversation_editor_does_not_overwrite_a_newer_version(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    created = handle_chat(db, user, conversation, "₹250 for coffee")
    transaction_id = created.widgets[0].data["transactionId"]
    edit = handle_action(db, user, conversation, "edit_saved_transaction", {"transactionId": transaction_id})

    handle_action(db, user, conversation, "update_saved_transaction", {
        "transactionId": transaction_id,
        "expectedVersion": edit.widgets[0].data["rowVersion"],
        "amountMinor": 30_000,
    })
    stale = execute_widget_action(ActionRequest(
        conversation_id=conversation.id,
        widget_id=edit.widgets[0].id,
        action="update_saved_transaction",
        payload={"transactionId": transaction_id, "amountMinor": 40_000},
    ), db, user)

    transaction = db.get(Transaction, UUID(transaction_id))
    assert transaction.amount_minor == 30_000
    assert transaction.row_version == 2
    assert "did not overwrite" in stale.message
    assert stale.widgets[0].data["rowVersion"] == 2


def test_saved_edit_cannot_create_a_transfer_without_accounts(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    created = handle_chat(db, user, conversation, "₹250 for coffee")
    transaction_id = created.widgets[0].data["transactionId"]
    transaction = db.get(Transaction, UUID(transaction_id))

    with pytest.raises(ValueError, match="requires both a source and destination account"):
        handle_action(db, user, conversation, "update_saved_transaction", {
            "transactionId": transaction_id,
            "expectedVersion": transaction.row_version,
            "amountMinor": transaction.amount_minor,
            "transactionType": "transfer",
        })

    db.refresh(transaction)
    assert transaction.transaction_type == "expense"
    assert transaction.row_version == 1


def test_operator_can_prepare_non_amount_edits_through_the_same_saved_editor(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    created = handle_chat(db, user, conversation, "Spent ₹900 at Toit today")
    transaction_id = created.widgets[0].data["transactionId"]
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal(
            "edit_transaction",
            {"target_mode": "preceding_card", "merchant": "Local Coffee"},
        ),
    )

    prepared = handle_chat(
        db,
        user,
        conversation,
        "Change the merchant on that transaction to Local Coffee",
    )

    assert db.get(Transaction, UUID(transaction_id)).merchant_name == "Toit"
    assert prepared.widgets[0].type == "transaction_edit"
    assert prepared.widgets[0].data["merchant"] == "Local Coffee"
    assert prepared.widgets[0].data["transactionId"] == transaction_id


def test_ambiguous_structured_edit_preserves_the_patch_until_the_user_selects_a_row(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹900 at Toit today")
    handle_chat(db, user, conversation, "Spent ₹1,100 at Toit today")
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal(
            "edit_transaction",
            {
                "target_mode": "matching",
                "target_merchant": "Toit",
                "amount_minor": 120_000,
            },
        ),
    )

    candidates = handle_chat(
        db,
        user,
        conversation,
        "Change a Toit transaction to ₹1,200",
    )

    assert len(candidates.widgets) == 2
    first_edit = next(
        action for action in candidates.widgets[0].actions
        if action.action == "edit_saved_transaction"
    )
    assert first_edit.payload["amountMinor"] == 120_000

    prepared = handle_action(
        db,
        user,
        conversation,
        first_edit.action,
        first_edit.payload,
    )

    assert prepared.widgets[0].data["transactionId"] == candidates.widgets[0].data["transactionId"]
    assert prepared.widgets[0].data["amountMinor"] == 120_000


def test_structured_old_values_select_one_row_without_using_the_replacement_as_a_filter(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    first = handle_chat(db, user, conversation, "Spent ₹900 at Toit today")
    handle_chat(db, user, conversation, "Spent ₹1,100 at Toit today")
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal(
            "edit_transaction",
            {
                "target_mode": "matching",
                "target_merchant": "Toit",
                "target_amount_minor": 90_000,
                "amount_minor": 120_000,
            },
        ),
    )

    prepared = handle_chat(
        db,
        user,
        conversation,
        "Change the ₹900 Toit transaction to ₹1,200",
    )

    assert prepared.widgets[0].type == "transaction_edit"
    assert prepared.widgets[0].data["transactionId"] == first.widgets[0].data["transactionId"]
    assert prepared.widgets[0].data["amountMinor"] == 120_000


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
    assert response.widgets == []
    assert response.citations[0].entity_type == "semantic_query"


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

    assert response.widgets == []
    assert "₹1,200" in response.message
    assert response.citations[0].entity_type == "semantic_query"
    assert response.citations[0].query["start_date"] == (date.today() - timedelta(days=1)).isoformat()


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


def test_saved_transaction_hitl_edit_records_device_fix_without_copying_it_to_the_widget_receipt(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    transaction_id = handle_chat(db, user, conversation, "₹250 for coffee").widgets[0].data["transactionId"]
    set_user_preference(db, user.id, "location:enabled", {"enabled": True})
    edit = handle_action(db, user, conversation, "edit_saved_transaction", {"transactionId": transaction_id})

    response = execute_widget_action(ActionRequest(
        conversation_id=conversation.id,
        widget_id=edit.widgets[0].id,
        action="update_saved_transaction",
        payload={
            "transactionId": transaction_id,
            "amountMinor": 25_000,
            "location": "Bengaluru, Karnataka",
            "latitude": 12.971599,
            "longitude": 77.594566,
            "locationAccuracy": 18,
        },
    ), db, user)

    transaction = db.get(Transaction, UUID(transaction_id))
    assert float(transaction.latitude) == 12.971599
    assert float(transaction.longitude) == 77.594566
    assert transaction.location_accuracy == 18
    assert transaction.location_label == "Bengaluru, Karnataka"
    receipt = response.widget_updates[0].widget.data["completion"]["values"]
    assert receipt["location"] == "Bengaluru, Karnataka"
    assert {"latitude", "longitude", "locationAccuracy"}.isdisjoint(receipt)


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


def test_every_blocking_goal_card_declares_a_cancel_transition(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Create a vacation goal of ₹2 lakh")
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


def test_complete_budget_creation_saves_once_and_uses_recorded_spending(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Set a food budget of ₹20,000")
    assert response.widgets[0].type == "budget_progress"
    assert response.pending_action is None
    assert db.scalar(select(Budget)).amount_minor == 2_000_000
    assert response.widgets[0].data["amountMinor"] == 2_000_000
    assert {action.action for action in response.widgets[0].actions} == {
        "edit_budget",
        "request_delete_budget",
    }


def test_budget_period_year_is_not_treated_as_the_limit(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(conversation_service, "_local_today", lambda _user: date(2026, 8, 19))

    response = handle_chat(db, user, conversation, "Set a lower August 2026 travel budget")

    assert response.message == "What monthly amount should I use for the Travel budget?"
    assert response.widgets[0].type == "clarification"
    assert response.widgets[0].data["options"] == []
    assert response.widgets[0].data["allowCustom"] is True
    assert response.pending_action is not None
    assert response.pending_action.continuation["customStrategy"] == "budget_amount"
    assert db.scalar(select(Budget)) is None


def test_budget_update_and_delete_phrases_enter_the_planning_workflow():
    assert conversation_service._looks_like_planning_command("Update my travel budget")
    assert conversation_service._looks_like_planning_command("Delete my travel budget")


def test_existing_budget_setup_without_amount_opens_prefilled_editor(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    travel = db.scalar(select(Category).where(Category.slug == "travel"))
    budget = Budget(
        user_id=user.id,
        category_id=travel.id,
        name="Travel budget",
        amount_minor=3_000_000,
        currency="INR",
    )
    db.add(budget)
    db.flush()

    response = handle_chat(db, user, conversation, "Set up my travel budget")

    assert response.message == "Choose the new monthly amount for your travel budget."
    assert response.pending_action.action == "save_budget"
    assert response.widgets[0].data["amountMinor"] == 3_000_000
    save = next(action for action in response.widgets[0].actions if action.action == "save_budget")
    assert save.label == "Update budget"
    assert save.payload["amountMinor"] == 3_000_000


def test_budget_management_uses_the_deterministic_hitl_route():
    decision, extracted = conversation_service._fast_path_decision(
        "Show my travel budget",
        date(2026, 8, 19),
    )

    assert decision.tool == conversation_service.capability_for_primitive("planning.run@1")
    assert extracted is None

    decision, extracted = conversation_service._fast_path_decision(
        "Set up my travel budget",
        date(2026, 8, 19),
    )

    assert decision.tool == conversation_service.capability_for_primitive("budget.manage@1")
    assert extracted is None


def test_show_budget_does_not_fall_through_to_sql_operator(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    travel = db.scalar(select(Category).where(Category.slug == "travel"))
    db.add(Budget(
        user_id=user.id,
        category_id=travel.id,
        name="Travel budget",
        amount_minor=3_000_000,
        currency="INR",
    ))
    db.flush()
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *_args, **_kwargs: pytest.fail("budget management must not invoke SQL routing"),
    )

    response = handle_chat(db, user, conversation, "Show my travel budget")

    assert response.message == "You have 1 active monthly budget."
    assert response.widgets[0].data["amountMinor"] == 3_000_000
    assert {action.action for action in response.widgets[0].actions} == {"edit_budget", "request_delete_budget"}


def test_budget_mutation_does_not_invoke_sql_operator(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *_args, **_kwargs: pytest.fail("budget mutation must use the deterministic gate"),
    )

    response = handle_chat(db, user, conversation, "Set a ₹20,000 food budget")

    assert response.pending_action is None
    assert db.scalar(select(Budget)).amount_minor == 2_000_000


def test_category_budget_create_update_and_delete_are_governed_and_show_spend(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    travel = db.scalar(select(Category).where(Category.slug == "travel"))
    monkeypatch.setattr(conversation_service, "_local_today", lambda _user: date(2026, 8, 19))
    db.add(Transaction(
        user_id=user.id,
        transaction_type="expense",
        amount_minor=500_000,
        currency="INR",
        merchant_name="Travel seed",
        category_id=travel.id,
        transaction_at=datetime(2026, 8, 10, 9, tzinfo=timezone.utc),
        status="confirmed",
    ))
    db.flush()

    created = handle_chat(db, user, conversation, "Set the August 2026 travel budget to ₹20,000")
    assert created.pending_action is None
    budget = db.scalar(select(Budget))
    assert budget.amount_minor == 2_000_000
    assert created.widgets[0].data["spentMinor"] == 500_000
    assert {action.action for action in created.widgets[0].actions} == {"edit_budget", "request_delete_budget"}
    widget_ids = [created.widgets[0].id]

    edit = next(action for action in created.widgets[0].actions if action.action == "edit_budget")
    editor = handle_action(db, user, conversation, edit.action, edit.payload)
    assert editor.pending_action.action == "save_budget"
    widget_ids.append(editor.widgets[0].id)
    update = next(action for action in editor.widgets[0].actions if action.action == "save_budget")
    updated = handle_action(db, user, conversation, update.action, {**update.payload, "amountMinor": 1_500_000})
    assert budget.amount_minor == 1_500_000
    widget_ids.append(updated.widgets[0].id)

    request_delete = next(action for action in updated.widgets[0].actions if action.action == "request_delete_budget")
    confirmation = handle_action(db, user, conversation, request_delete.action, request_delete.payload)
    assert confirmation.pending_action.action == "delete_budget"
    widget_ids.append(confirmation.widgets[0].id)
    assert len(widget_ids) == len(set(widget_ids))
    delete = next(action for action in confirmation.widgets[0].actions if action.action == "delete_budget")
    deleted = handle_action(db, user, conversation, delete.action, delete.payload)
    assert "transactions were not changed" in deleted.message
    assert db.scalar(select(Budget)) is None


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


def test_goal_contribution_amount_keeps_the_exact_goal_in_its_continuation(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    goal = Goal(
        user_id=user.id,
        name="Vacation",
        target_minor=20_000_000,
        current_minor=0,
        currency="INR",
    )
    db.add(goal)
    db.flush()

    response = handle_chat(db, user, conversation, "Add to my vacation goal")

    assert response.message == "How much should I add to your Vacation goal?"
    assert response.pending_action.action == "resolve_clarification"
    continuation = response.pending_action.continuation
    assert continuation["customStrategy"] == "goal_amount"
    assert continuation["customGoal"]["operation"] == "contribute_goal"
    assert continuation["customGoal"]["goalId"] == str(goal.id)


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


def test_travelling_query_filters_travel_spending(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹1,000 on a cab today")
    handle_chat(db, user, conversation, "Spent ₹500 on fuel today")
    handle_chat(db, user, conversation, "Spent ₹700 on lunch today")

    response = handle_chat(db, user, conversation, "How much did I spend on Travelling this month?")

    assert response.widgets == []
    assert "₹1,500" in response.message
    assert "| Local transport |" in response.message and "| Other |" in response.message


def test_category_breakdown_follow_up_uses_current_month_without_unnecessary_questions(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹200 on ice cream today")
    handle_chat(db, user, conversation, "Spent ₹300 on lunch today")
    handle_chat(db, user, conversation, "Can you show the spend summary")

    response = handle_chat(db, user, conversation, "Show the food breakdown")

    assert response.widgets == []
    assert "₹500" in response.message
    assert "| Dining |" in response.message and "| Ice cream |" in response.message


def test_remove_merchant_expense_searches_candidates_before_confirming(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    first = handle_chat(db, user, conversation, "Spent ₹900 at Toit today")
    handle_chat(db, user, conversation, "Spent ₹1,100 at Toit today")
    first_id = first.widgets[0].data["transactionId"]
    handle_chat(db, user, conversation, "₹333")
    abandoned_draft = db.scalar(select(TransactionDraft).where(TransactionDraft.raw_text == "₹333"))
    assert abandoned_draft.state == DraftState.NEEDS_CLARIFICATION.value
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal("find_transactions_for_removal", {}),
    )

    response = handle_chat(db, user, conversation, "I want to remove the Toit expense from the list")

    # Disambiguation is a question, so it arrives as markdown with no widget.
    # Only the single-candidate confirmation is a HITL surface.
    assert [widget.type for widget in response.widgets] == []
    assert "₹900" in response.message and "₹1,100" in response.message
    assert db.scalar(select(TransactionDraft).where(TransactionDraft.raw_text.ilike("%remove%"))) is None
    assert abandoned_draft.state == DraftState.CANCELLED.value

    review = handle_action(db, user, conversation, "request_remove_transaction", {"transactionId": first_id})
    assert review.widgets[0].type == "confirmation_card"
    assert db.get(Transaction, UUID(first_id)).deleted_at is None
    removed = handle_action(db, user, conversation, "confirm_remove_transaction", {"transactionId": first_id})
    assert removed.widgets[0].data["status"] == "Removed"
    assert db.get(Transaction, UUID(first_id)).deleted_at is not None


def test_semantic_removal_route_handles_natural_wording_and_typo_without_creating_a_draft(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    saved = handle_chat(db, user, conversation, "Spent ₹777 at Toit today")
    transaction_id = saved.widgets[0].data["transactionId"]
    prompt = "I want to remove the Toit of 777 rupess"
    assert conversation_service._fast_path_decision(prompt, date.today()) is None
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal("find_transactions_for_removal", {}),
    )

    response = handle_chat(db, user, conversation, prompt)

    assert response.widgets[0].type == "confirmation_card"
    assert response.widgets[0].data["transactionId"] == transaction_id
    assert db.scalar(select(TransactionDraft).where(TransactionDraft.raw_text == prompt)) is None
    assert db.get(Transaction, UUID(transaction_id)).deleted_at is None


def test_explicit_expense_amount_does_not_turn_a_removal_into_a_new_draft():
    assert conversation_service._fast_path_decision(
        "Remove the ₹500 expense",
        date(2026, 8, 15),
    ) is None


def test_selector_amount_does_not_become_a_replacement_amount():
    assert conversation_service._fast_path_decision(
        "Correct the ₹500 transaction with merchant Toit",
        date(2026, 8, 15),
    ) is None


def test_removal_does_not_treat_digits_inside_a_merchant_name_as_an_amount(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    merchant = "RemovalCafe1786430623348"
    first = handle_chat(db, user, conversation, f"Spent ₹654 at {merchant} for dinner today")
    second = handle_chat(db, user, conversation, f"Spent ₹765 at {merchant} for dinner today")
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal("find_transactions_for_removal", {}),
    )

    response = handle_chat(db, user, conversation, f"I want to remove the {merchant} expense from the list")

    # Both candidates survive the merchant's embedded digits, and the list of
    # them is markdown rather than a typed table.
    assert [widget.type for widget in response.widgets] == []
    assert "₹654" in response.message and "₹765" in response.message
    assert set(response.citations[0].entity_ids) == {
        first.widgets[0].data["transactionId"],
        second.widgets[0].data["transactionId"],
    }


def test_grounded_summary_is_persisted_as_analysis_state_not_row_scope(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    captured_workflow = {}

    def operator_runner(*args, **kwargs):
        if args[0] == "Which category has the highest spend?":
            return _operator_proposal("search_transactions", _search_inputs(QueryInterpretation(
                metric="spending_summary",
                result_mode="summary",
                operation="rank",
                group_by="category",
                transaction_type="expense",
                start_date=date.today().replace(day=1),
                end_date=date.today(),
                limit=1,
            )))
        captured_workflow.update(kwargs.get("workflow_context") or {})
        return OperatorResult(reply="Captured.")

    monkeypatch.setattr(conversation_service, "run_operator", operator_runner)

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


def test_semantic_category_ranking_answers_the_requested_question(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹200 on ice cream today")
    handle_chat(db, user, conversation, "Spent ₹300 on lunch today")
    handle_chat(db, user, conversation, "Spent ₹100 on fuel today")
    prompt = "Which category had highest spend?"
    assert conversation_service._fast_path_decision(prompt, date.today()) is None
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal("search_transactions", _search_inputs(QueryInterpretation(
            result_mode="summary",
            operation="rank",
            group_by="category",
            transaction_type="expense",
            start_date=date.today().replace(day=1),
            end_date=date.today(),
            limit=1,
        ))),
    )

    response = handle_chat(db, user, conversation, prompt)

    assert response.widgets == []
    assert response.message.startswith("Food had the highest category spend at ₹500.")
    assert "| Food | ₹500 |" in response.message
    assert response.citations[0].query["operation"] == "rank"
    assert response.citations[0].query["group_by"] == "category"


def test_semantic_merchant_summary_keeps_the_merchant_filter(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Spent ₹777 at Toit today")
    handle_chat(db, user, conversation, "Spent ₹900 at Toit today")
    handle_chat(db, user, conversation, "Spent ₹500 on fuel today")
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal("search_transactions", _search_inputs(
            QueryInterpretation(result_mode="summary", operation="total", transaction_type="expense", merchant="Toit"),
        )),
    )

    response = handle_chat(db, user, conversation, "How much have I spent at Toit?")

    assert response.widgets == []
    assert "₹1,677" in response.message
    assert response.citations[0].query["merchant"] == "Toit"


def test_semantic_income_summary_never_collapses_into_expense_spending(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    handle_chat(db, user, conversation, "Salary ₹3 lakh credited today")
    prompt = "Do I have any earnings from current month?"
    assert conversation_service._fast_path_decision(prompt, date.today()) is None
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal("search_transactions", _search_inputs(QueryInterpretation(
            metric="income_summary",
            result_mode="summary",
            operation="total",
            transaction_type="income",
            start_date=date.today().replace(day=1),
            end_date=date.today(),
        ))),
    )

    response = handle_chat(db, user, conversation, prompt)

    assert response.message.startswith("You earned ₹3,00,000 this month, across 1 transaction.")
    assert response.widgets == []
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


def test_explicit_user_subcategory_name_resolves_to_canonical_hierarchy(db, monkeypatch, agent_enabled):
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
        "run_operator",
        lambda *args, **kwargs: _operator_proposal("create_transaction_draft", {
            "transaction_type": "expense",
            "amount_minor": 30_000,
            "merchant": "Labour wages",
            "transaction_date": date.today().isoformat(),
            "category_slug": "other",
            "subcategory_slug": "other",
            "explicit_fields": ["transaction_type", "amount"],
        }),
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


def test_model_cannot_pair_income_with_an_expense_taxonomy(db, monkeypatch, agent_enabled):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setattr(conversation_service, "_fast_path_decision", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        conversation_service,
        "run_operator",
        lambda *args, **kwargs: _operator_proposal("create_transaction_draft", {
            "transaction_type": "income",
            "amount_minor": 50_000,
            "merchant": "earning",
            "transaction_date": date.today().isoformat(),
            "category_slug": "other",
            "subcategory_slug": "other",
            "explicit_fields": ["amount"],
        }),
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

    # Offline, an investment question fails closed instead of being guessed
    # into a calculator by keyword matching; the routed Operator supplies the
    # widget in normal operation, and the deterministic action still computes.
    response = handle_chat(db, user, conversation, "What if I increase my SIP by ₹20,000?")
    assert response.task_status == "failed"
    assert response.error_code == "unresolved_financial_query"
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
    template = AnalysisToolTemplate(
        capability_name="Descriptive spend analysis",
        capability_description="Reusable read-only spending analysis.",
        capability_signature="objective descriptive | metric gross_spend",
        template_version="governed-analysis-template.v2",
        status="active",
        semantic_registry_version="test",
        source_manifest_hash="registry-hash",
        parameter_schema=[],
        plan_template={},
        template_hash="template-hash",
        validation_report={"passed": True},
        created_by_user_id=user.id,
    )
    db.add(template)
    db.flush()
    user_tool = UserAnalysisTool(
        user_id=user.id,
        template_id=template.id,
        name="Monthly spend",
        description="Spend by category",
        intent_signature="monthly spend",
        status="active",
    )
    db.add(user_tool)
    db.flush()
    db.add(AnalysisToolRun(
        user_id=user.id,
        template_id=template.id,
        user_tool_id=user_tool.id,
        conversation_id=conversation.id,
        status="completed",
        parameters={},
        trace=[{"stage": "tool_execution"}],
    ))
    db.add(AIAction(user_id=user.id, conversation_id=conversation.id, action_type="operator_decision", payload_redacted={"tool": "search_transactions"}, status="completed"))
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
    assert db.get(AnalysisToolTemplate, template.id) is not None
    assert db.get(UserAnalysisTool, user_tool.id) is not None


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


def test_reserved_reply_records_completion_as_its_delivery_time(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    reserved = conversation_service._reserve_reply(db, conversation)
    completed_at = reserved.created_at + timedelta(seconds=20)
    monkeypatch.setattr(conversation_service, "now_utc", lambda: completed_at)

    response = conversation_service.persist_agent_response(
        db,
        conversation,
        "Completed response",
    )

    assert reserved.created_at < completed_at
    assert reserved.delivered_at == completed_at
    assert response.delivered_at == completed_at


def test_persistence_rejects_reused_actionable_widget_event_id(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Create a vacation goal of ₹2 lakh")
    repeated = response.widgets[0].model_copy(deep=True)
    with pytest.raises(ValueError, match="one HITL event"):
        conversation_service.persist_agent_response(db, conversation, "Second", widgets=[repeated])


def test_a_failed_turn_leaves_no_empty_reply_behind(db):
    """The reserved row is not a message until the turn answers into it."""
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)

    def collapse():
        raise RuntimeError("operator_decision unavailable")

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


def test_universal_request_releases_the_previous_result_scope(db, monkeypatch, agent_enabled):
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

    def operator_runner(text, taxonomy, today, timezone, recent, **kwargs):
        seen["workflow_context"] = kwargs.get("workflow_context")
        return OperatorResult(reply="Sure.")

    monkeypatch.setattr(conversation_service, "run_operator", operator_runner)

    handle_chat(db, user, conversation, "Draw a breakdown of all transactions in donut form")

    assert seen["workflow_context"]["activeDataScope"] is None
    assert seen["workflow_context"]["activeAnalysisState"] is None


def test_a_new_thread_starts_empty_and_opens_with_the_first_question(db):
    """No seeded greeting: the transcript's first row is the person's own words.

    The client's opening screen carries the invitation, so a server-side
    greeting only ever surfaced as a reply to a question nobody had asked yet.
    """
    user = default_user(db)
    conversation = conversation_service.get_or_create_conversation(db, user)

    assert db.scalars(select(Message).where(Message.conversation_id == conversation.id)).all() == []

    handle_chat(db, user, conversation, "Hello there")
    transcript = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at, Message.id)
    ).all()

    assert [message.role for message in transcript] == ["user", "assistant"]
    assert transcript[0].content == "Hello there"


def test_rename_conversation_sets_the_users_title_and_auto_title_never_overwrites_it(db):
    from app.api import rename_conversation
    from app.schemas import ConversationRenameIn

    user = default_user(db)
    conversation = conversation_service.get_or_create_conversation(db, user)
    recency_before_rename = conversation.updated_at

    renamed = rename_conversation(
        conversation.id,
        ConversationRenameIn(title="  August   grocery audit  "),
        db,
        user,
    )

    assert renamed.title == "August grocery audit"
    assert db.get(Conversation, conversation.id).title == "August grocery audit"
    # Renaming is housekeeping, not activity: the thread keeps its place in the
    # recency-ordered rail rather than jumping to the top.
    assert db.get(Conversation, conversation.id).updated_at == recency_before_rename

    # The first-message auto-title only ever replaces the placeholder, so an
    # explicit rename survives subsequent messages.
    handle_chat(db, user, conversation, "Hello there")
    assert db.get(Conversation, conversation.id).title == "August grocery audit"


def test_rename_conversation_rejects_titles_without_visible_characters(db):
    from app.schemas import ConversationRenameIn

    with pytest.raises(ValueError):
        ConversationRenameIn(title="   ")


def test_thread_rename_recognizer_extracts_only_explicit_titles():
    assert conversation_service._conversation_rename_request(
        'Can you update the page title to "prepare the list in table form with user and system?"'
    ) == "prepare the list in table form with user and system?"
    assert (
        conversation_service._conversation_rename_request("Rename this chat to Grocery planning")
        == "Grocery planning"
    )
    assert conversation_service._conversation_rename_request("Can you rename this thread?") is None
    assert conversation_service._conversation_rename_request("List all categories in table form") is None
    assert conversation_service._conversation_rename_request("Set a ₹20,000 food budget") is None


_TAXONOMY_RESULT = [
    {"name": "Bills", "subcategories": [{"name": "Internet"}, {"name": "Phone"}]},
    {"name": "Other", "subcategories": [{"name": "Other"}]},
    {"name": "Pet Care", "subcategories": [{"name": "Grooming"}, {"name": "Other"}, {"name": "Vet"}]},
    {"name": "Travel", "subcategories": [{"name": "Flights"}, {"name": "Visa and documents"}]},
]


def test_cash_position_computes_a_governed_ratio_and_honours_the_period(db):
    user = default_user(db)
    db.add_all([
        Transaction(
            user_id=user.id, transaction_type="income", amount_minor=1_000_000,
            currency="INR", merchant_name="Employer",
            transaction_at=datetime(2026, 8, 5, 9, tzinfo=timezone.utc), status="confirmed",
        ),
        Transaction(
            user_id=user.id, transaction_type="expense", amount_minor=440_000,
            currency="INR", merchant_name="Rent",
            transaction_at=datetime(2026, 8, 6, 9, tzinfo=timezone.utc), status="confirmed",
        ),
        Transaction(
            user_id=user.id, transaction_type="expense", amount_minor=100_000,
            currency="INR", merchant_name="Old expense",
            transaction_at=datetime(2026, 7, 6, 9, tzinfo=timezone.utc), status="confirmed",
        ),
    ])
    db.flush()

    from app.services.intelligence import cash_totals as cash_position

    all_time = cash_position(db, user.id)
    assert all_time["income_to_expense_ratio"] == round(1_000_000 / 540_000, 2)
    assert all_time["start"] is None and all_time["end"] is None

    period = cash_position(db, user.id, date(2026, 8, 1), date(2026, 8, 16))
    assert period["income_minor"] == 1_000_000
    assert period["expenses_minor"] == 440_000
    assert period["income_to_expense_ratio"] == round(1_000_000 / 440_000, 2)
    assert period["start"] == "2026-08-01" and period["end"] == "2026-08-16"


def test_rename_ask_then_bare_title_reaches_the_confirmation_flow(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)

    first = handle_chat(db, user, conversation, "Can you rename this thread?")
    assert first.message == conversation_service.RENAME_TITLE_QUESTION

    second = handle_chat(db, user, conversation, "Love aaj kal")
    assert second.message == "Rename this thread to “Love aaj kal”?"
    assert second.task_status == "needs_input"
    card = next(widget for widget in second.widgets if widget.type == "insight_card")
    rename = next(action for action in card.actions if action.action == "rename_conversation")
    assert rename.payload == {"title": "Love aaj kal"}
    # The thread is untouched until the user confirms through the widget.
    assert db.get(Conversation, conversation.id).title != "Love aaj kal"


def test_operator_cannot_fabricate_a_thread_rename(db, monkeypatch):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    get_settings.cache_clear()

    def fabricated_rename(*_args, **kwargs):
        kwargs["on_delta"]("Renamed this thread to “Love aaj kal.”")
        return OperatorResult(reply="Renamed this thread to “Love aaj kal.”", streamed_live=True)

    monkeypatch.setattr(conversation_service, "run_operator", fabricated_rename)

    with pytest.raises(RuntimeError, match="authority postcondition"):
        handle_chat(
            db,
            user,
            conversation,
            "did you change it?",
            text_delta_callback=lambda _message_id, _delta: None,
        )

    assert db.get(Conversation, conversation.id).title != "Love aaj kal"
    get_settings.cache_clear()


def test_a_governed_analysis_is_rendered_by_the_harness_that_computed_it():
    """The analysis words its own answer; nothing here re-renders its figures."""
    from types import SimpleNamespace

    item = SimpleNamespace(
        name="run_financial_analysis",
        arguments={},
        result=SimpleNamespace(data={
            "kind": "governed_analysis",
            "message": "Food spending from 2026-07-01 through 2026-08-16 was ₹6,000 across 2 subcategories.",
            "query_results": [],
        }),
    )

    assert conversation_service._grounded_tool_rendering(item, SimpleNamespace(currency="INR")) == (
        "Food spending from 2026-07-01 through 2026-08-16 was ₹6,000 across 2 subcategories."
    )


def test_an_analytical_tool_result_has_no_hand_written_rendering():
    """Analytical reads execute through the harness, so none is reachable here.

    A breakdown payload arriving without a governed-analysis envelope means the
    read bypassed the query builder; the answer is that there is no answer, not
    a second rendering of the same numbers maintained in this module.
    """
    from types import SimpleNamespace

    item = SimpleNamespace(
        name="subcategory_breakdown",
        arguments={"start": "2026-07-01", "end": "2026-08-16", "category_slug": "food"},
        result=SimpleNamespace(data=[
            {"id": "groceries", "label": "Groceries", "amount_minor": 440_000, "count": 12, "currency": "INR"},
        ]),
    )

    assert conversation_service._grounded_tool_rendering(item, SimpleNamespace(currency="INR")) is None


def test_the_taxonomy_rendering_answers_at_the_scope_the_question_asked():
    """The fallback answers the question asked, or it is not an answer at all."""
    render = conversation_service._taxonomy_rendering

    assert render("Which Pet Care subcategories exist?", _TAXONOMY_RESULT) == (
        "Pet Care has 3 subcategories: Grooming, Other, Vet."
    )
    assert render("What are Bills’ subcategories?", _TAXONOMY_RESULT) == (
        "Bills has 2 subcategories: Internet, Phone."
    )
    # No category named: the whole hierarchy, not a bare list of category names.
    assert render("Show all categories and their sub categories", _TAXONOMY_RESULT) == (
        "- **Bills:** Internet, Phone\n"
        "- **Other:** Other\n"
        "- **Pet Care:** Grooming, Other, Vet\n"
        "- **Travel:** Flights, Visa and documents"
    )
    # No enumeration asked for: the category list is the answer.
    assert render("How many categories are there?", _TAXONOMY_RESULT) == (
        "You have 4 expense categories: Bills, Other, Pet Care, Travel."
    )


def test_a_result_with_no_faithful_rendering_produces_no_sentence():
    """No rendering means no answer — never a sentence that narrates completion."""
    from types import SimpleNamespace

    item = SimpleNamespace(
        name="loan_amortization_schedule",
        arguments={"principal_minor": 100_000},
        result=SimpleNamespace(data={"summary": {"payment_minor": 9_321}, "rows": []}),
    )

    assert conversation_service._grounded_tool_rendering(item, SimpleNamespace(currency="INR")) is None
