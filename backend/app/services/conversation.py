from __future__ import annotations

import ast
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from time import perf_counter
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql.elements import ColumnElement

from ..config import get_settings
from ..event_time import as_utc, from_local_parts, local_date, local_now, local_time, now_utc, resolve_event_time, utc_range_for_local_dates
from ..domain import (
    CONVERSATION_TITLE_MAX,
    EDITABLE_TRANSACTION_TYPES,
    MAX_TRANSACTION_AMOUNT_MINOR,
    DraftState,
    ExecutionStatus,
    FinancialSourceType,
    ImportRecordStatus,
    ImportStatus,
    ReconciliationOutcome,
    ReconciliationResolution,
    SpendNature,
    TAXONOMY_FIELD_NAMES,
    TaxonomyScope,
    TransactionStatus,
    TransactionType,
    WidgetActionId,
)
from ..models import (
    AIAction,
    Account,
    Budget,
    Category,
    Conversation,
    FinancialObservation,
    Goal,
    GoalContribution,
    Import,
    ImportRecord,
    Message,
    ReconciliationCandidate,
    Subcategory,
    Tag,
    Transaction,
    TransactionDraft,
    TransactionFieldValue,
    TransactionSource,
    TransactionTag,
    User,
)
from ..operation_types import AuthorizationOutcome, ContextRelationship, DataEffect, IntentAuthority
from ..schemas import AgentActivityEvent, AgentResponse, DataReference, ObservationIn, PendingAction, Widget, WidgetAction, WidgetLifecycle, WidgetType, WidgetUpdate, validate_action_payload
from ..taxonomy_catalog import DefaultCategorySlug, TRANSACTION_CATEGORY_ROOTS, category_slug_matches_transaction_type
from ..operations import operation_catalog
from ..operations.execution import (
    OperationChangedError,
    OperationInputError,
    execute_operation,
    execute_operation_steps,
    missing_required_inputs,
    operation_inputs_from_route,
    render_operation_text,
    requires_confirmation,
    resolve_current_operation,
    validate_operation_inputs,
)
from .finance_time import ambiguous_numeric_date_options, month_bounds, shift_month
from .analysis_tools import AnalysisToolContext, build_analysis_tools
from .intelligence import expense_summary
from .markdown_views import join_blocks, markdown_section, markdown_table, money
from .accounts import AccountRepository
from .adapters import import_summary
from .agents import GROUPED_QUERY_OPERATIONS, RECENT_CONTEXT_TURN_LIMIT, STANDALONE_RECENT_CONTEXT_TURN_LIMIT, ClarificationOption, ClarificationRequest, CompilationAssumption, CopilotDecision, QueryInterpretation, ResolvedIntentContract, TaxonomyInterpretation, build_analysis_delegate_tool, contains_internal_analysis_diagnostic, filesystem_operation_decision, releases_prior_scope, repair_grounded_answer, run_operator, suggest_related_questions
from .analysis_sandbox import PYTHON_TOOL_NAME
from .analysis_harness import AnalysisTraceStage, HarnessValidationError, ReplayDisposition, bind_repeat_analysis, execute_analysis_template
from .answer_validation import compile_answer_contract, contains_financial_claim, validate_coverage, validate_evidence
from .answer_presentation import answer_presentation as build_answer_presentation
from .calculators import investment_projection, loan_with_prepayment
from .cdp import get_traits, traits_context_line
from .capabilities import (
    CapabilityId,
    capability_for_metric,
    capability_for_primitive,
    capability_invokes,
    capability_spec,
    safe_read_capabilities,
)
from .continuations import (
    CancelContinuation,
    ClarificationContinuationEnvelope,
    ClarificationTransition,
    GovernedBudgetContinuation,
    GovernedGoalContinuation,
    GovernedQueryContinuation,
    GovernedTaxonomyContinuation,
    LegacyPromptContinuation,
    parse_clarification_transition,
)
from .proactive import current_insights, insights_context_line
from .preferences import AnswerValidationMode, answer_style, answer_validation_mode
from .recommendation import (
    AMOUNT,
    AREA,
    MERCHANT,
    PLACE,
    TIME,
    TOKEN,
    Recommendation,
    load_ledger,
    recommend_categories,
    recommend_subcategories,
)
from .currency import format_money_minor
from .extraction import ExtractedTransaction, explicit_currency_codes, extract_transaction, infer_expense_category, normalize_merchant, parse_amount_minor, parse_spending_period
from .merchants import MerchantRepository
from .planning_contracts import (
    BudgetSetupContract,
    BudgetSetupSeed,
    GoalAmountContract,
    GoalAmountSeed,
)
from .reconciliation import attach_observation, ingest_observation, resolve_reconciliation
from .repositories import UserScopedRepository
from .turn_policy import EffectAuthorization, TurnIntentContract, authorize_capability, resolve_turn_intent
from .turn_signals import expects_value_answer, has_amount_comparison, has_explicit_transaction_mutation_cue, looks_like_financial_query
from .runtime_tools import FINANCIAL_CALCULATOR_TOOL_NAME, build_runtime_tools, capability_notes
from .semantic import AnalysisPlan, AnalysisToolProposal, AnalysisTransform, FinanceFilter, FinanceQueryPlan
from .tags import TagRepository
from .taxonomy import TaxonomyRepository
from .transactions import UNSET, TransactionVersionConflict, active_transaction, canonical_transactions, create_transaction, owned_transaction_source, update_saved_transaction
from .user_memory import remember_taxonomy_mapping


ActivityCallback = Callable[[dict], None]
TextDeltaCallback = Callable[[UUID, str], None]
ReasoningDeltaCallback = Callable[[str], None]
DRAFT_RESOURCE_ID = "draft"
TAXONOMY_FIELDS = frozenset(TAXONOMY_FIELD_NAMES)
TAXONOMY_INFERENCE_FIELDS = TAXONOMY_FIELDS | {"merchant preference"}
SUBCATEGORY_INFERENCE_FIELDS = TAXONOMY_INFERENCE_FIELDS - {"category"}
MONTH_CATEGORY_DIMENSIONS = ("month", "category")
_COMPUTED_TABLE_ROW_CAP = 30


def _without_inferred_fields(values: list[str], excluded: Collection[str]) -> list[str]:
    return [field for field in values if field not in excluded]


def _analysis_lifecycle_badge(stage: str, label: str, status: ExecutionStatus | str) -> str | None:
    status = ExecutionStatus(status)
    if status is ExecutionStatus.FAILED and stage in {
        AnalysisTraceStage.TEMPLATE_CANDIDATES,
        AnalysisTraceStage.TEMPLATE_VALIDATION,
        AnalysisTraceStage.TEMPLATE_REPAIR,
        AnalysisTraceStage.RESULT_VERIFICATION,
    }:
        return "Rejected"
    if status is not ExecutionStatus.COMPLETED:
        return None
    if stage == AnalysisTraceStage.TEMPLATE_MATCH and label.startswith("Created"):
        return "Saved"
    if stage == AnalysisTraceStage.TEMPLATE_MATCH and (
        "matches" in label.casefold() or "identical" in label.casefold()
    ):
        return "Reused"
    if stage == AnalysisTraceStage.TEMPLATE_REPAIR:
        return "Updated"
    if stage == AnalysisTraceStage.TEMPLATE_VALIDATION:
        return "Validated"
    return None


def _local_today(user: User) -> date:
    """Return the user's calendar date, never the API host's UTC date."""
    return local_now(user.timezone).date()


def _serialize_widget(widget: Widget) -> dict:
    return widget.model_dump(mode="json")


def _find_persisted_widget(
    db: Session,
    conversation: Conversation,
    widget_id: str,
) -> tuple[Message, int, Widget] | None:
    """Find one widget by its protocol identity inside this conversation."""
    messages = list(db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at, Message.id)
    ))
    # Widget ids are protocol event identities. Older budget cards used the
    # resource id directly, though, so a thread can contain a completed card
    # and a newer pending editor with the same id. Prefer the newest instance
    # to keep those existing threads recoverable.
    for message in reversed(messages):
        for index in range(len(message.widgets or []) - 1, -1, -1):
            raw_widget = message.widgets[index]
            if isinstance(raw_widget, dict) and raw_widget.get("id") == widget_id:
                return message, index, Widget.model_validate(raw_widget)
    return None


def prepare_widget_action(
    db: Session,
    conversation: Conversation,
    widget_id: str,
    action: str,
) -> tuple[Message, int, Widget] | None:
    """Validate that an action originated from this conversation's widget."""
    found = _find_persisted_widget(db, conversation, widget_id)
    if not found:
        raise ValueError("This widget action is no longer available")
    _message, _index, widget = found
    lifecycle = WidgetLifecycle(widget.data.get("lifecycle") or WidgetLifecycle.PENDING)
    if lifecycle is not WidgetLifecycle.PENDING:
        raise ValueError("This widget action has already been completed")
    if not any(item.action == WidgetActionId(action) for item in widget.actions):
        raise ValueError("This action is not available on the widget")
    return found


def prepare_widget_cancellation(
    db: Session,
    conversation: Conversation,
    widget_id: str,
) -> tuple[Message, int, Widget] | None:
    """Validate a protocol-level cancellation of a pending widget.

    Cancelling an interrupt is deliberately not one of the widget's domain
    actions. It closes the HITL boundary without authorizing domain work.
    """
    found = _find_persisted_widget(db, conversation, widget_id)
    if not found:
        raise ValueError("This widget action is no longer available")
    _message, _index, widget = found
    lifecycle = WidgetLifecycle(widget.data.get("lifecycle") or WidgetLifecycle.PENDING)
    if lifecycle is not WidgetLifecycle.PENDING:
        raise ValueError("This widget action has already been completed")
    return found


def resolve_widget_action(
    db: Session,
    origin: tuple[Message, int, Widget] | None,
    *,
    lifecycle: WidgetLifecycle,
    action: str,
    payload: dict,
) -> WidgetUpdate | None:
    """Persist a read-only action receipt with the values the user submitted."""
    if origin is None:
        return None
    message, index, widget = origin
    # Exact coordinates belong only on the transaction record. Keeping them a
    # second time in the durable widget receipt would widen their exposure and
    # is unnecessary for the compact "Updated" history state.
    private_receipt_fields = {"latitude", "longitude", "locationAccuracy"}
    safe_payload = {
        key: value
        for key, value in payload.items()
        if key not in private_receipt_fields
        if isinstance(value, (str, int, float, bool, list, dict)) or value is None
    }
    data = {
        **widget.data,
        "lifecycle": lifecycle,
        "completion": {"action": action, "values": safe_payload},
    }
    # Taxonomy names are first-class display data. Keeping the canonical field
    # populated lets old and new renderers show the receipt without knowing the
    # completion envelope.
    if widget.type is WidgetType.TAXONOMY_EDITOR and lifecycle is WidgetLifecycle.COMPLETED:
        canonical: Category | Subcategory | None = None
        draft_id = safe_payload.get("draftId") or widget.data.get("draftId")
        taxonomy_user_id = db.scalar(
            select(Conversation.user_id).where(Conversation.id == message.conversation_id)
        )
        draft = (
            UserScopedRepository(db, taxonomy_user_id).get(
                TransactionDraft,
                UUID(str(draft_id)),
            )
            if draft_id and taxonomy_user_id
            else None
        )
        taxonomy = TaxonomyRepository(db, taxonomy_user_id) if taxonomy_user_id else None
        operation = WidgetActionId(widget.data.get("operation"))
        if operation is WidgetActionId.CREATE_TAXONOMY_PATH and taxonomy:
            requested_name = str(safe_payload.get("name") or widget.data.get("name") or "")
            canonical = next((
                item for item in taxonomy.expense_categories()
                if item.name.casefold() == requested_name.casefold()
            ), None)
            raw_children = safe_payload.get("subcategories") or widget.data.get("subcategories") or []
            requested_children = (
                [str(item) for item in raw_children]
                if isinstance(raw_children, list)
                else []
            )
            canonical_children = (
                [
                    item for item in taxonomy.subcategories(canonical.id)
                    if item.name.casefold() in {name.casefold() for name in requested_children}
                ]
                if canonical
                else []
            )
            if canonical:
                data["name"] = canonical.name
                data["subcategories"] = [item.name for item in canonical_children]
                data["resultId"] = str(canonical.id)
                data["resultIds"] = [
                    str(canonical.id),
                    *(str(item.id) for item in canonical_children),
                ]
                safe_payload["name"] = canonical.name
                safe_payload["subcategories"] = [item.name for item in canonical_children]
        elif operation is WidgetActionId.CREATE_SUBCATEGORY:
            if draft and draft.subcategory_id:
                canonical = taxonomy.subcategory(draft.subcategory_id) if taxonomy else None
            elif taxonomy and safe_payload.get("categoryId") and safe_payload.get("name"):
                canonical = next((
                    item for item in taxonomy.subcategories(UUID(str(safe_payload["categoryId"])))
                    if item.name.casefold() == str(safe_payload["name"]).casefold()
                ), None)
        elif draft and draft.category_id:
            canonical = taxonomy.category(draft.category_id) if taxonomy else None
        elif taxonomy and safe_payload.get("name"):
            canonical = next((
                item for item in taxonomy.expense_categories()
                if item.name.casefold() == str(safe_payload["name"]).casefold()
            ), None)
        if canonical:
            data["name"] = canonical.name
            data["resultId"] = str(canonical.id)
            safe_payload["name"] = canonical.name
        elif isinstance(safe_payload.get("name"), str):
            data["name"] = safe_payload["name"]
    resolved = Widget(id=widget.id, type=widget.type, version=widget.version, data=data, actions=[])
    widgets = list(message.widgets or [])
    widgets[index] = _serialize_widget(resolved)
    # JSON columns do not detect nested mutations; assigning a fresh list is
    # what makes the receipt durable.
    message.widgets = widgets
    return WidgetUpdate(widget_id=widget.id, widget=resolved)


def _grounded_states(message: Message) -> tuple[dict | None, dict | None]:
    query_references = [citation for citation in (message.citations or []) if citation.get("query")]
    if not query_references:
        return None, None
    row_reference = next((
        citation for citation in query_references
        if citation.get("entity_type") == "transaction" and citation.get("entity_ids")
    ), None)
    query_reference = row_reference or query_references[0]
    analysis_state = {
        "sourceMessageId": str(message.id),
        "answerSummary": message.content[:500],
        "citationLabel": query_reference.get("label"),
        "entityType": query_reference.get("entity_type"),
        "query": query_reference.get("query") or {},
        "queries": [citation.get("query") or {} for citation in query_references],
        "resultShapes": [
            (citation.get("query") or {}).get("result_mode")
            for citation in query_references
            if (citation.get("query") or {}).get("result_mode")
        ],
    }
    entity_ids = (row_reference or {}).get("entity_ids") or []
    data_scope = None
    if row_reference and entity_ids:
        data_scope = {
            "sourceMessageId": str(message.id),
            "query": row_reference.get("query") or {},
            "entityIds": entity_ids[:100],
            "entityCount": len(entity_ids),
        }
    return analysis_state, data_scope


_LINEAGE_NUMBER = re.compile(r"(?<![\w.,])[+-]?\d[\d,]*(?:\.\d+)?(?![\w,])")


def _lineage_numbers(content: str) -> set[Decimal]:
    """Normalize factual numbers before reusing a prior answer's citations."""
    values: set[Decimal] = set()
    for match in _LINEAGE_NUMBER.finditer(content):
        try:
            values.add(Decimal(match.group().replace(",", "")))
        except ArithmeticError:
            continue
    return values


def _prior_grounding_citations(
    db: Session,
    conversation: Conversation,
    active_analysis_state: dict | None,
    reply: str,
) -> list[DataReference]:
    """Reuse provenance for a bounded follow-up over the prior grounded answer.

    The model may explain or prioritize facts already visible in the preceding
    answer without paying for the same read tool again.  Provenance is inherited
    only from an assistant message in this conversation, and only when every
    numeric value in the new reply already appeared in that source message.
    Novel numbers therefore still require fresh tool evidence.
    """
    raw_source_id = (active_analysis_state or {}).get("sourceMessageId")
    if not raw_source_id:
        return []
    try:
        source_id = UUID(str(raw_source_id))
    except ValueError:
        return []
    source = db.scalar(select(Message).where(
        Message.id == source_id,
        Message.conversation_id == conversation.id,
        Message.role == "assistant",
    ))
    if source is None or not source.citations:
        return []
    if not _lineage_numbers(reply).issubset(_lineage_numbers(source.content)):
        return []
    try:
        return [DataReference.model_validate(item) for item in source.citations]
    except (TypeError, ValueError):
        return []


def _message_context_entry(message: Message) -> dict[str, Any]:
    """Expose prior wording plus bounded structured lineage to later turns.

    Prose alone cannot tell a follow-up that “15 expenses” was all-time while
    “8 expenses” was month-to-date. Query lineage carries those dates, filters,
    and result shapes without replaying rows, entity IDs, or financial payloads.
    """
    entry: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.role != "assistant" or not message.citations:
        return entry
    analysis_state, _data_scope = _grounded_states(message)
    if analysis_state:
        entry["grounding"] = {
            "entityType": analysis_state.get("entityType"),
            "queries": (analysis_state.get("queries") or [])[:4],
            "resultShapes": analysis_state.get("resultShapes") or [],
        }
    response_surfaces = []
    for widget in (message.widgets or [])[:4]:
        if not isinstance(widget, dict) or widget.get("type") == WidgetType.AGENT_ACTIVITY:
            continue
        data: dict[str, Any] = (
            widget["data"] if isinstance(widget.get("data"), dict) else {}
        )
        response_surfaces.append({
            "type": widget.get("type"),
            "title": data.get("title"),
            "rowCount": len(data.get("rows") or []) if isinstance(data.get("rows"), list) else None,
        })
    if response_surfaces:
        entry["responseSurfaces"] = response_surfaces
    return entry


def _complete_analysis_state(state: dict | None) -> bool:
    if not state or not (state.get("query") or state.get("queries")):
        return False
    query = state.get("query") or state["queries"][0]
    if query.get("source_kind") == "calculator":
        return bool(query.get("tool") and isinstance(query.get("arguments"), dict))
    if state.get("entityType") == "semantic_query":
        return {
            "metric", "dimensions", "filters", "start_date", "end_date", "order", "limit",
        } <= set(query)
    return "operation" in query or "result_mode" in query


def _active_loan_chart_clarification(
    text: str,
    active_analysis_state: dict | None,
    currency: str,
) -> CopilotDecision | None:
    """Stop a chart from silently choosing between conflicting loan inputs."""
    if (
        not active_analysis_state
        or not re.search(r"\b(?:chart|draw|graph|plot|visuali[sz]e)\b", text, re.I)
    ):
        return None
    queries = [
        item
        for item in (active_analysis_state.get("queries") or [])
        if isinstance(item, dict) and item.get("source_kind") == "calculator"
    ]
    tenure_query = next((item for item in queries if item.get("tool") == "loan_payment"), None)
    fixed_query = next((item for item in queries if item.get("tool") == "amortize_with_fixed_payment"), None)
    if not tenure_query or not fixed_query:
        return None
    tenure_args = tenure_query.get("arguments") or {}
    fixed_args = fixed_query.get("arguments") or {}
    tenure_result = tenure_query.get("result_summary") or {}
    supplied_payment = fixed_args.get("payment_minor")
    calculated_payment = tenure_result.get("emi_minor")
    if (
        not isinstance(supplied_payment, int)
        or not isinstance(calculated_payment, int)
        or supplied_payment == calculated_payment
        or tenure_args.get("principal_minor") != fixed_args.get("principal_minor")
        or tenure_args.get("annual_rate_percent") != fixed_args.get("annual_rate_percent")
    ):
        return None
    tenure_months = tenure_args.get("tenure_months")
    if not isinstance(tenure_months, int) or tenure_months <= 0:
        return None
    clarification = ClarificationRequest(
        question="Which installment assumption should control the loan chart?",
        reason=(
            f"The supplied payment is {format_money_minor(supplied_payment, currency)}, while the "
            f"{tenure_months}-month schedule requires about {format_money_minor(calculated_payment, currency)}. "
            "Those choices produce different repayment schedules."
        ),
        conflict_fields=["tenure", "monthly installment"],
        options=[
            {
                "id": "use_tenure",
                "label": f"Keep the {tenure_months}-month tenure",
                "description": f"Chart the required installment of about {format_money_minor(calculated_payment, currency)}.",
                "resolution": (
                    f"Use the supplied tenure of {tenure_months} months as authoritative, ignore the conflicting "
                    "fixed-payment scenario, and chart the calculated amortization schedule."
                ),
            },
            {
                "id": "use_installment",
                "label": f"Keep the {format_money_minor(supplied_payment, currency)} installment",
                "description": "Calculate the resulting tenure and chart that fixed-payment schedule.",
                "resolution": (
                    f"Use the supplied fixed monthly payment of {format_money_minor(supplied_payment, currency)} as "
                    "authoritative, calculate the resulting tenure, and chart the fixed-payment amortization schedule."
                ),
            },
            {
                "id": "compare_scenarios",
                "label": "Compare both schedules",
                "description": "Show how tenure, interest, and principal reduction differ.",
                "resolution": "Calculate both the tenure-controlled and fixed-payment schedules and compare them explicitly.",
            },
        ],
        allow_custom=True,
        custom_label="Use another assumption",
    )
    return CopilotDecision(
        tool=capability_for_primitive("agent.clarify@1"),
        clarification=clarification,
        confidence=1.0,
        reason="The active calculator lineage contains conflicting tenure and payment assumptions.",
        safe_reasoning_summary=[
            "Compared the supplied loan assumptions",
            "Paused before selecting one silently",
        ],
        validated_by="calculator_conflict_policy",
        validation_confidence=1.0,
    )


"""One turn's reply row, reserved while the turn is being admitted.

A transcript is ordered by `(created_at, id)`, so if a reply row is only
created once the model finishes, a fast turn started second can be written
ahead of a slow turn started first — the two questions end up adjacent and
each answer sits under the wrong one. Creating the row up front makes
`created_at` record the turn's position rather than its duration.

Held in a ContextVar rather than a module global because every request runs
`handle_chat` in its own worker thread, and `asyncio.to_thread` gives each of
those an isolated copy of the context.
"""
_reserved_reply: ContextVar[Message | None] = ContextVar("reserved_reply", default=None)
_clarification_resume_guard: ContextVar[dict[str, Any] | None] = ContextVar(
    "clarification_resume_guard",
    default=None,
)

def _reserve_reply(db: Session, conversation: Conversation) -> Message:
    """Writes the empty row this turn's reply will be filled into."""
    reply = Message(conversation_id=conversation.id, role="assistant", content="", widgets=[], citations=[])
    db.add(reply)
    db.flush()
    _reserved_reply.set(reply)
    return reply


def _history_only() -> tuple:
    """Keeps this turn's reserved reply out of anything that reads the
    conversation back. The row exists from the moment the turn is admitted so
    that the answer holds its place, but it is empty until the turn answers:
    it is not history, and it must never reach the model's context window."""
    reserved = _reserved_reply.get()
    return (Message.id != reserved.id,) if reserved is not None else ()


def _recent_complete_turn_context(
    db: Session,
    conversation: Conversation,
    current_user_message: Message,
    *,
    limit: int = RECENT_CONTEXT_TURN_LIMIT,
) -> list[dict[str, Any]]:
    """Return recent complete user/assistant turns with full message text.

    A message-count limit can bisect a turn and the former 500-character slice
    could remove the conclusion from a persisted answer. We first identify a
    bounded set of recent user turns, then keep the newest complete ones and
    preserve every selected message verbatim. The current prompt and its
    reserved blank reply are deliberately excluded.
    """
    if limit <= 0:
        return []
    candidate_users = list(
        db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.role == "user",
                Message.id != current_user_message.id,
                *_history_only(),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit * 3)
        )
    )
    if not candidate_users:
        return []
    oldest = candidate_users[-1]
    rows = list(
        db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.id != current_user_message.id,
                tuple_(Message.created_at, Message.id) >= (oldest.created_at, oldest.id),
                *_history_only(),
            )
            .order_by(Message.created_at, Message.id)
        )
    )
    turns: list[list[Message]] = []
    current: list[Message] | None = None
    for message in rows:
        if message.role == "user":
            if current and any(item.role == "assistant" and item.content for item in current):
                turns.append(current)
            current = [message]
        elif message.role == "assistant" and current is not None and message.content:
            current.append(message)
    if current and any(item.role == "assistant" and item.content for item in current):
        turns.append(current)
    return [
        _message_context_entry(message)
        for turn in turns[-limit:]
        for message in turn
    ]


def _recent_complete_turn_snapshot(
    db: Session,
    conversation: Conversation,
    *,
    limit: int = RECENT_CONTEXT_TURN_LIMIT,
) -> list[dict[str, Any]]:
    """Capture complete history immediately before admitting a new turn."""
    if limit <= 0:
        return []
    candidate_users = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id, Message.role == "user")
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit * 3)
        )
    )
    if not candidate_users:
        return []
    oldest = candidate_users[-1]
    rows = list(
        db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                tuple_(Message.created_at, Message.id) >= (oldest.created_at, oldest.id),
            )
            .order_by(Message.created_at, Message.id)
        )
    )
    turns: list[list[Message]] = []
    current: list[Message] | None = None
    for message in rows:
        if message.role == "user":
            if current and any(item.role == "assistant" and item.content for item in current):
                turns.append(current)
            current = [message]
        elif message.role == "assistant" and current is not None and message.content:
            current.append(message)
    if current and any(item.role == "assistant" and item.content for item in current):
        turns.append(current)
    return [
        _message_context_entry(message)
        for turn in turns[-limit:]
        for message in turn
    ]


def _clarification_draft(
    db: Session,
    conversation: Conversation,
) -> TransactionDraft | None:
    """Return the draft the latest assistant turn is actively presenting.

    An unresolved row is not automatically the current conversation state.
    Older clarification drafts can remain in the audit history after the user
    moves on; treating the newest such row as active made every later prompt
    look like a transaction-field answer. The visible latest assistant turn is
    the authority for whether a draft is still being handed to the user.
    """
    latest_assistant = db.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.role == "assistant",
            Message.content != "",
            *_history_only(),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    if latest_assistant is None:
        return None
    for widget in reversed(latest_assistant.widgets or []):
        data = widget.get("data") if isinstance(widget, dict) else None
        raw_draft_id = data.get("draftId") if isinstance(data, dict) else None
        if not raw_draft_id:
            continue
        try:
            draft_id = UUID(str(raw_draft_id))
        except ValueError:
            continue
        return db.scalar(
            select(TransactionDraft).where(
                TransactionDraft.id == draft_id,
                TransactionDraft.conversation_id == conversation.id,
                TransactionDraft.state == DraftState.NEEDS_CLARIFICATION.value,
            )
        )
    return None


def _claim_reserved_reply(conversation: Conversation) -> Message | None:
    """Hands over the reservation, once. A turn that answers more than once —
    a draft that clarifies and then commits — appends the rest normally."""
    reserved = _reserved_reply.get()
    if reserved is None or reserved.conversation_id != conversation.id:
        return None
    _reserved_reply.set(None)
    return reserved


def record_assistant_message(db: Session, conversation: Conversation, content: str, widgets: list[Widget], citations: list[DataReference] | None = None) -> Message:
    serialized_widgets = [_serialize_widget(widget) for widget in widgets]
    serialized_citations = [item.model_dump(mode="json") for item in (citations or [])]
    message = _claim_reserved_reply(conversation)
    if message is not None:
        message.content = content
        message.widgets = serialized_widgets
        message.citations = serialized_citations
        message.delivered_at = now_utc()
    else:
        message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=content,
            widgets=serialized_widgets,
            citations=serialized_citations,
            delivered_at=now_utc(),
        )
        db.add(message)
    db.flush()
    if message.citations:
        conversation.active_analysis_state, conversation.active_data_scope = _grounded_states(message)
    return message


_HITL_ESCAPE_ACTIONS = {
    WidgetActionId.CANCEL_ADD_CATEGORY,
    WidgetActionId.CANCEL_TAXONOMY_CHANGE,
    WidgetActionId.CANCEL_TRANSACTION_DRAFT,
    WidgetActionId.CANCEL_PENDING_ACTION,
    WidgetActionId.CANCEL_TRANSACTION_EDIT,
    WidgetActionId.CANCEL_SAVED_TRANSACTION_EDIT,
    WidgetActionId.CANCEL_REMOVE_TRANSACTION,
    WidgetActionId.CANCEL_OPERATION,
}


def _validate_blocking_widget_contract(widgets: list[Widget], pending_action: PendingAction | None) -> None:
    """Fail closed when a new blocking card has no way forward or out.

    This is the scalable HITL boundary: renderers may change spacing and
    layout, but a persisted interrupt cannot be created unless its widget owns
    both the declared continuation and an explicit escape transition.
    """
    if pending_action is None:
        return
    actions = [action for widget in widgets for action in widget.actions]
    if not any(action.action is pending_action.action for action in actions):
        raise ValueError(f"Blocking widget is missing its {pending_action.action.value} continuation")
    has_escape = any(action.action in _HITL_ESCAPE_ACTIONS for action in actions)
    has_clarification_cancel = any(
        action.action is WidgetActionId.RESOLVE_CLARIFICATION
        and action.payload.get("optionId") == "cancel"
        for action in actions
    )
    if not has_escape and not has_clarification_cancel:
        raise ValueError("Blocking widget must declare a cancellation transition")


def _validate_actionable_widget_event_ids(
    db: Session,
    conversation: Conversation,
    widgets: list[Widget],
) -> None:
    """Require every interactive card occurrence to have its own identity.

    The frontend retires a completed HITL event by widget id. Reusing a
    resource id (budget, goal, reconciliation candidate, and so on) for a new
    card therefore makes the new controls inherit the old event's completed
    state. Enforce event identity at the persistence boundary so every current
    and future HITL producer gets the same protection.
    """
    emitted_ids = [
        widget.id
        for widget in widgets
        if widget.actions or widget.data.get("rowActions")
    ]
    if len(emitted_ids) != len(set(emitted_ids)):
        raise ValueError("Actionable widget ids must be unique within a response")
    if not emitted_ids:
        return
    emitted = set(emitted_ids)
    stored_widget_groups = db.scalars(
        select(Message.widgets).where(Message.conversation_id == conversation.id)
    )
    for stored_widgets in stored_widget_groups:
        for raw_widget in stored_widgets or []:
            if (
                isinstance(raw_widget, dict)
                and raw_widget.get("id") in emitted
                and (
                    raw_widget.get("actions")
                    or (raw_widget.get("data") or {}).get("rowActions")
                )
            ):
                raise ValueError("Actionable widget ids must identify one HITL event")


def persist_agent_response(
    db: Session,
    conversation: Conversation,
    content: str,
    *,
    widgets: list[Widget] | None = None,
    citations: list[DataReference] | None = None,
    pending_action: PendingAction | None = None,
    widget_updates: list[WidgetUpdate] | None = None,
    task_status: str = "succeeded",
    failure_stage: str | None = None,
    error_code: str | None = None,
    commit: bool = True,
) -> AgentResponse:
    """Persist and return one response through the canonical reply boundary."""
    response_widgets = widgets or []
    response_citations = citations or []
    _validate_blocking_widget_contract(response_widgets, pending_action)
    _validate_actionable_widget_event_ids(db, conversation, response_widgets)
    message = record_assistant_message(
        db,
        conversation,
        content,
        response_widgets,
        response_citations,
    )
    if commit:
        db.commit()
    if pending_action is not None and task_status == "succeeded":
        task_status = "needs_input"
    return AgentResponse(
        message=content,
        widgets=response_widgets,
        widgetUpdates=widget_updates or [],
        pendingAction=pending_action,
        citations=response_citations,
        conversation_id=conversation.id,
        message_id=message.id,
        delivered_at=message.delivered_at,
        task_status=task_status,
        failure_stage=failure_stage,
        error_code=error_code,
    )


_RESOLVED_DATE_FIELDS = re.compile(
    r"\bstart_date\s*=\s*(\d{4}-\d{2}-\d{2})\b.*?"
    r"\bend_date\s*=\s*(\d{4}-\d{2}-\d{2})\b",
    re.I | re.S,
)

_QUOTED_TAXONOMY_PATH = re.compile(
    r"[\"“](?P<category>[^\"”]{1,80})[\"”]\s+category\b"
    r".*?[\"“](?P<subcategory>[^\"”]{1,80})[\"”]"
    r"(?:\s+as\s+its)?\s+sub[\s-]*category\b",
    re.I | re.S,
)
_NAMED_TAXONOMY_PATH = re.compile(
    r"\b(?:create|add|make)(?:ing)?\s+(?:a\s+|an\s+|the\s+)?category\s+"
    r"(?:(?:called|named)\s+)?(?P<category>.+?)\s+"
    r"(?:with|and(?:\s+its)?)\s+(?:a\s+|an\s+|the\s+|its\s+)?"
    r"(?P<subcategory>.+?)\s+sub[\s-]*category\b",
    re.I | re.S,
)


def _clean_taxonomy_label(value: str) -> str:
    return " ".join(value.strip(" \t\r\n.,:;!?\"'“”").split())


def _explicit_compound_taxonomy_path(text: str) -> TaxonomyInterpretation | None:
    """Compile only an explicitly named parent-and-child creation request.

    The model-owned typed contract remains the general language interface.
    This narrow compiler is a deterministic repair for a known lossy route and
    for clarification records persisted before compound taxonomy plans existed.
    """
    matched = _QUOTED_TAXONOMY_PATH.search(text) or _NAMED_TAXONOMY_PATH.search(text)
    if not matched:
        return None
    category = _clean_taxonomy_label(matched.group("category"))
    subcategory = _clean_taxonomy_label(matched.group("subcategory"))
    if not category or not subcategory or len(category) > 80 or len(subcategory) > 80:
        return None
    return TaxonomyInterpretation(
        operation=WidgetActionId.CREATE_TAXONOMY_PATH,
        name=category,
        subcategories=[subcategory],
    )


def _normalize_compound_taxonomy_decision(
    text: str,
    decision: CopilotDecision,
) -> CopilotDecision:
    taxonomy = decision.taxonomy
    if not capability_invokes(decision.tool, "taxonomy.change@1") or taxonomy is None:
        return decision
    if taxonomy.operation is WidgetActionId.CREATE_TAXONOMY_PATH:
        return decision
    compiled = _explicit_compound_taxonomy_path(text)
    if compiled is None:
        return decision
    known_names = {
        value.casefold()
        for value in (taxonomy.name, taxonomy.parent_category)
        if value
    }
    compiled_names = {
        item.casefold()
        for item in (compiled.name, *compiled.subcategories)
        if item
    }
    if known_names and not known_names <= compiled_names:
        return decision
    return decision.model_copy(update={
        "taxonomy": compiled,
        "reason": "Deterministic taxonomy policy preserved the explicitly requested parent and child.",
        "safe_reasoning_summary": [
            "Preserved the requested category and subcategory as one plan",
            "Prepare one governed approval for the compound change",
        ],
    })


def _clarification_fingerprint(clarification: ClarificationRequest) -> str:
    normalized = {
        "fields": sorted(field.casefold() for field in clarification.conflict_fields),
        "options": [
            {
                "id": option.id.casefold(),
                "label": " ".join(option.label.casefold().split()),
                "disposition": option.disposition,
            }
            for option in clarification.options
        ],
        "question": " ".join(clarification.question.casefold().split()),
    }
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _clarification_stall_reason(
    previous: dict[str, Any],
    current: ClarificationRequest,
    depth: int,
) -> str | None:
    if depth >= 1:
        return "The clarification chain reached its maximum depth."
    previous_fields = {
        str(field).casefold()
        for field in previous.get("conflictFields", [])
        if str(field).strip()
    }
    current_fields = {field.casefold() for field in current.conflict_fields if field.strip()}
    previous_fingerprint = str(previous.get("fingerprint") or "")
    current_fingerprint = _clarification_fingerprint(current)
    if previous_fingerprint and previous_fingerprint == current_fingerprint:
        return "The resumed route reproduced the same clarification."
    if previous_fields and current_fields and (
        previous_fields <= current_fields or current_fields <= previous_fields
    ):
        return "The resumed route is still blocked on the same fields."
    return None


def _legacy_taxonomy_continuation(
    original_request: str,
    selected_label: str,
    resolution: str,
    previous_clarification: dict[str, Any] | None,
) -> GovernedTaxonomyContinuation | None:
    previous = previous_clarification or {}
    conflict_fields = [str(field).casefold() for field in previous.get("conflictFields", [])]
    if not conflict_fields or not all(field.startswith("taxonomy.") for field in conflict_fields):
        return None
    selected = next((
        option for option in previous.get("options", [])
        if isinstance(option, dict) and option.get("label") == selected_label
    ), {})
    selected_evidence = "\n".join(str(value) for value in (
        selected.get("description") if isinstance(selected, dict) else None,
        selected_label,
        resolution,
    ) if value)
    if re.search(r"\b(?:only|omit|without|skip)\b", selected_evidence, re.I):
        return None
    if not (
        re.search(r"\bcategor(?:y|ies)\b", selected_evidence, re.I)
        and re.search(r"\bsub[\s-]*categor(?:y|ies)\b", selected_evidence, re.I)
    ):
        return None
    evidence_candidates = [str(value) for value in (
        previous.get("question"),
        selected.get("description") if isinstance(selected, dict) else None,
        original_request,
    ) if value]
    compiled = next((
        plan for plan in (
            _explicit_compound_taxonomy_path(value)
            for value in evidence_candidates
        )
        if plan is not None
    ), None)
    if compiled is None:
        return None
    request_terms = _taxonomy_language(original_request)
    names = [compiled.name, *compiled.subcategories]
    if any(_taxonomy_language(name) not in request_terms for name in names if name):
        return None
    return GovernedTaxonomyContinuation(label=selected_label, taxonomy=compiled)


def _resolved_intent_for_clarification_option(
    original_request: str,
    clarification: ClarificationRequest,
    resolution: str,
) -> ResolvedIntentContract | None:
    """Compile supported clarification choices into executable typed intent.

    This deliberately recognizes only a narrow contract we can prove from the
    server-authored choice. Unsupported clarification kinds receive an explicit
    legacy transition; they never get guessed into this typed fast path.
    """
    if not any("date" in field.casefold() or "period" in field.casefold() for field in clarification.conflict_fields):
        return None
    matched = _RESOLVED_DATE_FIELDS.search(resolution)
    if not matched:
        return None
    lowered = original_request.casefold()
    is_spending = bool(re.search(r"\b(?:expense|expenses|spend|spending|spent)\b", lowered))
    if not is_spending:
        return None
    try:
        start_date, end_date = (date.fromisoformat(value) for value in matched.groups())
    except ValueError:
        return None
    if start_date > end_date:
        return None
    if re.search(r"\b(?:list|show|find|transactions?|records?|entries)\b", lowered):
        # A request for the records themselves is the Operator's to answer from
        # the transaction_list tool. Compiling it into the summary lane here
        # would answer a listing question with a total.
        return None
    query = QueryInterpretation(
        metric="spending_summary",
        result_mode="summary",
        operation="total",
        transaction_type=TransactionType.EXPENSE,
        start_date=start_date,
        end_date=end_date,
        limit=50,
        use_active_scope=False,
    )
    return ResolvedIntentContract(
        context_mode="standalone",
        # Summary aggregation is the search lane's result_mode="summary"; the
        # fixed spending_summary capability no longer exists.
        capability=capability_for_primitive("transaction.search@1"),
        query=query,
    )


_TRANSACTION_CLARIFICATION_FIELDS = {
    "amount": "amount",
    "category": "category",
    "category_slug": "category",
    "currency": "currency",
    "destination": "destination_account",
    "destination_account": "destination_account",
    "financial_direction": "transaction_type",
    "source": "source_account",
    "source_account": "source_account",
    "subcategory": "subcategory",
    "subcategory_slug": "subcategory",
    "transaction_type": "transaction_type",
    "type": "transaction_type",
}


def _transaction_clarification_seed(
    text: str,
    clarification: ClarificationRequest,
    today: date,
    currency: str,
) -> ExtractedTransaction | None:
    """Normalize transaction-shaped clarification into the draft state machine.

    This is a workflow boundary, not another intent classifier. It only accepts
    fields already declared by the structured clarification contract and a
    prompt that independently looks like a requested financial mutation.
    """
    conflict_fields = {
        _TRANSACTION_CLARIFICATION_FIELDS.get(
            re.sub(r"[^a-z0-9]+", "_", field.casefold()).strip("_")
        )
        for field in clarification.conflict_fields
    }
    conflict_fields.discard(None)
    if not conflict_fields or looks_like_financial_query(text):
        return None

    extracted = extract_transaction(text, today=today, default_currency=currency)
    if (
        "currency" in conflict_fields
        and len(explicit_currency_codes(text)) > 1
    ):
        # Two explicitly supplied currencies carry a real conflict. An omitted
        # currency uses the profile default, while one explicit currency is an
        # authoritative override; neither case needs another question.
        return None
    if (
        re.search(r"\b(?:budget|goal|savings?\s+plan)\b", text, re.I)
        or re.search(r"\b(?:save|saving)\s+.+\s+for\b", text, re.I)
        or re.search(r"\b(?:remove|delete|undo)\b", text, re.I)
        or re.search(
            r"\b(?:create|add|rename|delete)\b.{0,30}\b(?:category|subcategory)\b",
            text,
            re.I,
        )
    ):
        return None
    mutation_cue = bool(re.search(
        r"\b(?:add|create|enter|log|record|save|spent|paid|received|earned|transfer|transferred)\b",
        text,
        re.I,
    ))
    explicit_direction = (
        extracted.transaction_type != TransactionType.UNKNOWN
        and "transaction_type" in extracted.explicit_fields
    )
    amount_mutation = extracted.amount_minor is not None and mutation_cue
    if not explicit_direction and not amount_mutation:
        return None

    # A clarification means this field was not established. Clear it before
    # persistence so recommendation history or parser defaults cannot skip the
    # exact HITL boundary the user still needs to resolve.
    if "amount" in conflict_fields:
        extracted.amount_minor = None
    if "transaction_type" in conflict_fields:
        extracted.transaction_type = TransactionType.UNKNOWN
        extracted.category_slug = None
        extracted.subcategory_slug = None
    if "category" in conflict_fields:
        extracted.category_slug = None
        extracted.subcategory_slug = None
    if "subcategory" in conflict_fields:
        extracted.subcategory_slug = None
    if "source_account" in conflict_fields:
        extracted.source_account = None
    if "destination_account" in conflict_fields:
        extracted.destination_account = None

    extracted.explicit_fields = [
        field for field in extracted.explicit_fields
        if field not in conflict_fields
    ]
    extracted.inferred_fields = [
        field for field in extracted.inferred_fields
        if field not in {*conflict_fields, *TAXONOMY_FIELDS}
    ]
    missing_fields = []
    if extracted.amount_minor is None:
        missing_fields.append("amount")
    if extracted.transaction_type == TransactionType.UNKNOWN:
        missing_fields.append("transaction_type")
    if extracted.transaction_type == TransactionType.EXPENSE:
        if not extracted.category_slug:
            missing_fields.append("category")
        elif not extracted.subcategory_slug:
            missing_fields.append("subcategory")
    if extracted.transaction_type == TransactionType.TRANSFER:
        if not extracted.source_account:
            missing_fields.append("source_account")
        if not extracted.destination_account:
            missing_fields.append("destination_account")
    extracted.missing_fields = missing_fields
    return extracted


def _clarification_response(
    db: Session,
    conversation: Conversation,
    original_request: str,
    clarification: ClarificationRequest,
    *,
    custom_budget: BudgetSetupSeed | None = None,
    custom_goal: GoalAmountSeed | None = None,
) -> AgentResponse:
    """Persist a generic, resumable ambiguity instead of guessing."""
    clarification_id = uuid4()
    source_message = db.scalar(
        select(Message)
        .where(Message.conversation_id == conversation.id, Message.role == "user")
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    if source_message is None:
        raise ValueError("A clarification requires its source user message")
    actions = [
        WidgetAction(
            id=option.id,
            label=option.label,
            action=WidgetActionId.RESOLVE_CLARIFICATION,
            style="primary" if index == 0 else "secondary",
            payload={
                "clarificationId": str(clarification_id),
                "optionId": option.id,
            },
        )
        for index, option in enumerate(clarification.options)
    ]
    if clarification.allow_custom:
        actions.append(WidgetAction(
            id="custom",
            label=clarification.custom_label or "Something else",
            action=WidgetActionId.RESOLVE_CLARIFICATION,
            payload={
                "clarificationId": str(clarification_id),
                "optionId": "custom",
            },
        ))
    # Cancellation is a first-class transition, not a special option tile. It
    # remains in the same durable continuation contract as the authored
    # choices, so resuming the interrupt cannot accidentally re-route the
    # original request.
    actions.append(WidgetAction(
        id="cancel",
        label="Cancel",
        action=WidgetActionId.RESOLVE_CLARIFICATION,
        style="ghost",
        payload={
            "clarificationId": str(clarification_id),
            "optionId": "cancel",
        },
    ))
    widget = Widget(
        id=f"clarification-{clarification_id}",
        type=WidgetType.CLARIFICATION,
        data={
            "clarificationId": str(clarification_id),
            "title": "One detail needs your confirmation",
            "question": clarification.question,
            "reason": clarification.reason,
            "conflictFields": clarification.conflict_fields,
            "options": [
                {
                    "id": option.id,
                    "label": option.label,
                    "description": option.description,
                }
                for option in clarification.options
            ],
            "allowCustom": clarification.allow_custom,
            "customLabel": clarification.custom_label,
        },
        actions=actions,
    )
    transitions = {}
    for option in clarification.options:
        resolved = _resolved_intent_for_clarification_option(
            original_request,
            clarification,
            option.resolution,
        )
        transition: ClarificationTransition
        if option.disposition == "cancel":
            transition = CancelContinuation(label=option.label)
        elif option.taxonomy is not None:
            transition = GovernedTaxonomyContinuation(
                label=option.label,
                taxonomy=option.taxonomy,
            )
        elif resolved is not None:
            transition = GovernedQueryContinuation(label=option.label, intent=resolved)
        else:
            transition = LegacyPromptContinuation(
                label=option.label,
                resolution=option.resolution,
            )
        transitions[option.id] = transition
    transitions["cancel"] = CancelContinuation(label="Cancel")
    resume_guard = _clarification_resume_guard.get() or {}
    continuation = ClarificationContinuationEnvelope(
        clarification_id=clarification_id,
        source_message_id=source_message.id,
        original_request=original_request,
        options=transitions,
        allow_custom=clarification.allow_custom,
        custom_strategy=(
            "budget_amount"
            if custom_budget is not None
            else "goal_amount"
            if custom_goal is not None
            else "route_once"
        ),
        custom_budget=custom_budget,
        custom_goal=custom_goal,
        clarification_depth=int(resume_guard.get("depth", -1)) + 1,
        clarification_fingerprint=_clarification_fingerprint(clarification),
    ).model_dump(mode="json", by_alias=True)
    return persist_agent_response(
        db,
        conversation,
        clarification.question,
        widgets=[widget],
        pending_action=PendingAction(
            action=WidgetActionId.RESOLVE_CLARIFICATION,
            resource_id=str(clarification_id),
            continuation=continuation,
        ),
    )


def get_or_create_conversation(db: Session, user: User, conversation_id: UUID | None = None) -> Conversation:
    conversation = user_conversation(db, user.id, conversation_id) if conversation_id else None
    if conversation_id and not conversation:
        raise ValueError("Conversation not found")
    if conversation is None:
        # A new thread starts empty on purpose. The client's own opening screen
        # carries the invitation and the examples, so seeding a greeting would
        # only push the person's first question down under a turn nobody asked
        # for — and leave the transcript reading as a reply to nothing.
        conversation = Conversation(user_id=user.id, title="Financial check-in")
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
    return conversation


def _expense_categories_for_user(db: Session, user_id: UUID) -> list[Category]:
    return TaxonomyRepository(db, user_id).expense_categories()


def _subcategories_for_user(db: Session, user_id: UUID, category_id: UUID) -> list[Subcategory]:
    return TaxonomyRepository(db, user_id).subcategories(category_id)


def _taxonomy_language(value: str) -> str:
    """Normalize a human taxonomy label without reducing it to a DB slug."""
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold(), flags=re.UNICODE).split())


def _explicit_taxonomy_match(
    db: Session,
    user_id: UUID,
    text: str,
) -> tuple[Category, Subcategory | None, str] | None:
    """Resolve an explicit user-visible category label to its stable IDs.

    This is entity linking, not keyword classification: only labels in this
    user's visible taxonomy are eligible, and the longest label wins.
    """
    normalized_text = f" {_taxonomy_language(text)} "
    matches: list[tuple[int, Category, Subcategory | None, str]] = []
    for category in _expense_categories_for_user(db, user_id):
        category_terms = {category.name, category.slug.replace("_", "-")}
        for term in category_terms:
            normalized_term = _taxonomy_language(term)
            if len(normalized_term) >= 3 and f" {normalized_term} " in normalized_text:
                matches.append((len(normalized_term), category, None, term))
        for subcategory in _subcategories_for_user(db, user_id, category.id):
            subcategory_terms = {subcategory.name, subcategory.slug.replace("_", "-")}
            for term in subcategory_terms:
                normalized_term = _taxonomy_language(term)
                if len(normalized_term) >= 3 and f" {normalized_term} " in normalized_text:
                    # Prefer a subcategory when equally long labels exist.
                    matches.append((len(normalized_term) + 1, category, subcategory, term))
    if not matches:
        return None
    _, selected_category, selected_subcategory, selected_term = max(
        matches, key=lambda item: item[0]
    )
    return selected_category, selected_subcategory, selected_term


def _apply_explicit_taxonomy(
    db: Session,
    user: User,
    text: str,
    result: ExtractedTransaction,
) -> None:
    """Apply authoritative user taxonomy before draft persistence."""
    if result.transaction_type != TransactionType.EXPENSE:
        return
    match = _explicit_taxonomy_match(db, user.id, text)
    if not match:
        return
    category, subcategory, matched_term = match
    result.category_slug = category.slug
    result.subcategory_slug = subcategory.slug if subcategory else None
    is_user_taxonomy = category.scope == TaxonomyScope.USER.value or bool(
        subcategory and subcategory.scope == TaxonomyScope.USER.value
    )
    has_taxonomy_cue = bool(re.search(r"\b(?:category|subcategory|categor(?:y|ize|ise)|under|to)\b", text, re.I))
    if is_user_taxonomy or has_taxonomy_cue:
        result.explicit_fields = list(dict.fromkeys([
            *result.explicit_fields,
            "category",
            *(("subcategory",) if subcategory else ()),
        ]))
        result.inferred_fields = _without_inferred_fields(
            result.inferred_fields,
            TAXONOMY_INFERENCE_FIELDS,
        )
        result.confidence = max(result.confidence, Decimal("0.99"))
    else:
        result.inferred_fields = list(dict.fromkeys([
            *result.inferred_fields,
            "category",
            *(("subcategory",) if subcategory else ()),
        ]))

    # Phrases such as "add ₹300 expense to Labour Wages" name the user's
    # subcategory, not a merchant. An explicit merchant cue retains it.
    if result.merchant and _taxonomy_language(result.merchant) == _taxonomy_language(matched_term):
        has_explicit_merchant_cue = bool(re.search(r"\b(?:at|merchant|store|vendor|business)\b", text, re.I))
        if not has_explicit_merchant_cue:
            result.merchant = None


def _suggestion_body(recommendation: Recommendation, noun: str) -> str:
    """Describe only the evidence that actually produced these guesses."""
    if not any(item.evidence_backed for item in recommendation.suggestions):
        return "No history to learn from yet, so these are common starting points. They adapt as you categorize."
    channels = {item.dominant_channel for item in recommendation.suggestions}
    named = [
        label for channel, label in (
            (MERCHANT, "merchant"),
            (PLACE, "location"),
            (AREA, "area"),
            (TOKEN, "wording"),
            (TIME, "time of day"),
            (AMOUNT, "amount"),
        ) if channel in channels
    ]
    if not named:
        return f"Ranked from the {noun} you use most, weighted toward recent choices."
    listed = named[0] if len(named) == 1 else ", ".join(named[:-1]) + f" and {named[-1]}"
    return f"Ranked from your own history by {listed}, weighted toward recent choices."


def _draft_navigation_actions(
    draft: TransactionDraft,
    *,
    revisit_step: str | None = None,
    revisit_label: str | None = None,
) -> list[WidgetAction]:
    """Every blocking draft step has a way back and a way out.

    These are server-declared transitions, not decorative client controls: the
    revisit action restores the previous valid state and cancellation retires
    the draft so the next message cannot accidentally resume it.
    """
    actions: list[WidgetAction] = []
    if revisit_step and revisit_label:
        actions.append(WidgetAction(
            id=f"revisit-{revisit_step}",
            label=revisit_label,
            action=WidgetActionId.REVISIT_TRANSACTION_STEP,
            style="secondary",
            payload={"draftId": str(draft.id), "step": revisit_step},
        ))
    actions.append(WidgetAction(
        id="cancel-draft",
        label="Cancel transaction",
        action=WidgetActionId.CANCEL_TRANSACTION_DRAFT,
        style="ghost",
        payload={"draftId": str(draft.id)},
    ))
    return actions


def _cancel_pending_action(resource_id: str, label: str = "Cancel") -> WidgetAction:
    """Standard escape for a blocking action that has no mutable draft."""
    return WidgetAction(
        id="cancel",
        label=label,
        action=WidgetActionId.CANCEL_PENDING_ACTION,
        style="ghost",
        payload={"resourceId": resource_id},
    )


def _budget_management_actions(budget: Budget) -> list[WidgetAction]:
    """Server-authored entry points for the two governed budget mutations."""
    payload = {"budgetId": str(budget.id)}
    return [
        WidgetAction(
            id="edit",
            label="Update budget",
            action=WidgetActionId.EDIT_BUDGET,
            style="secondary",
            payload=payload,
        ),
        WidgetAction(
            id="delete",
            label="Delete budget",
            action=WidgetActionId.REQUEST_DELETE_BUDGET,
            style="danger",
            payload=payload,
        ),
    ]


def _category_selector(db: Session, draft: TransactionDraft) -> Widget:
    categories = _expense_categories_for_user(db, draft.user_id)
    user = db.get(User, draft.user_id)
    recommendation = recommend_categories(db, user, draft, categories)
    return Widget(
        id=f"category-{draft.id}-{uuid4()}",
        type=WidgetType.CATEGORY_SELECTOR,
        data={"title": "Where should I categorize this?", "body": _suggestion_body(recommendation, "categories"), "draftId": str(draft.id), "suggestions": recommendation.as_dicts(), "options": [{"id": str(c.id), "slug": c.slug, "label": c.name, "icon": c.icon} for c in categories], "allowCreate": True},
        actions=[
            WidgetAction(id="select", label="Select category", action=WidgetActionId.SELECT_CATEGORY, payload={"draftId": str(draft.id)}),
            WidgetAction(id="add", label="Add new category", action=WidgetActionId.START_ADD_CATEGORY, style="ghost", payload={"draftId": str(draft.id)}),
            *_draft_navigation_actions(draft, revisit_step="transaction_type", revisit_label="Change type"),
        ],
    )


def _new_category_widget(draft: TransactionDraft) -> Widget:
    return Widget(
        id=f"category-create-{draft.id}-{uuid4()}",
        type=WidgetType.CATEGORY_SELECTOR,
        data={"title": "Add a new category", "mode": "create", "draftId": str(draft.id), "options": []},
        actions=[
            WidgetAction(id="create", label="Add category", action=WidgetActionId.CREATE_CATEGORY, style="primary", payload={"draftId": str(draft.id)}),
            WidgetAction(id="back", label="Back", action=WidgetActionId.CANCEL_ADD_CATEGORY, style="secondary", payload={"draftId": str(draft.id)}),
            *_draft_navigation_actions(draft),
        ],
    )


def _taxonomy_editor_widget(
    operation: WidgetActionId | str,
    name: str | None,
    parent: Category | None,
    draft: TransactionDraft | None,
    subcategories: list[str] | None = None,
) -> Widget:
    action_id = WidgetActionId(operation)
    if action_id not in {
        WidgetActionId.CREATE_CATEGORY,
        WidgetActionId.CREATE_SUBCATEGORY,
        WidgetActionId.CREATE_TAXONOMY_PATH,
    }:
        raise ValueError("Unsupported taxonomy operation")
    child_names = list(subcategories or [])
    if action_id is WidgetActionId.CREATE_TAXONOMY_PATH and (not name or not child_names):
        raise ValueError("A taxonomy path requires a category and at least one subcategory")
    action_label = (
        "Add category and subcategories"
        if action_id is WidgetActionId.CREATE_TAXONOMY_PATH
        else "Add subcategory"
        if action_id is WidgetActionId.CREATE_SUBCATEGORY
        else "Add category"
    )
    action_payload: dict[str, Any] = {
        "draftId": str(draft.id) if draft else None,
        "categoryId": str(parent.id) if parent else None,
    }
    if action_id is WidgetActionId.CREATE_TAXONOMY_PATH:
        action_payload = {"name": name, "subcategories": child_names}
    return Widget(
        id=f"taxonomy-{uuid4()}",
        type=WidgetType.TAXONOMY_EDITOR,
        data={
            "operation": action_id,
            "name": name,
            "subcategories": child_names,
            "parentCategory": parent.name if parent else None,
            "appliesToDraft": bool(draft),
            "draftId": str(draft.id) if draft else None,
            "categoryId": str(parent.id) if parent else None,
            "lifecycle": "pending",
        },
        actions=[
            WidgetAction(
                id="confirm-taxonomy",
                label=action_label,
                action=action_id,
                style="primary",
                payload=action_payload,
            ),
            WidgetAction(
                id="cancel-taxonomy",
                label="Back" if draft else "Cancel",
                action=WidgetActionId.CANCEL_TAXONOMY_CHANGE,
                payload={
                    "draftId": str(draft.id) if draft else None,
                    "categoryId": str(parent.id) if parent else None,
                },
            ),
            *(_draft_navigation_actions(draft) if draft else []),
        ],
    )


def _subcategory_selector(db: Session, draft: TransactionDraft) -> Widget:
    if draft.category_id is None:
        raise ValueError("Unknown category")
    category = TaxonomyRepository(db, draft.user_id).category(draft.category_id)
    if not category:
        raise ValueError("Unknown category")
    subcategories = _subcategories_for_user(db, draft.user_id, draft.category_id)
    user = db.get(User, draft.user_id)
    recommendation = recommend_subcategories(db, user, draft, category, subcategories)
    return Widget(
        id=f"subcategory-{draft.id}-{uuid4()}",
        type=WidgetType.SUBCATEGORY_SELECTOR,
        data={"title": f"What type of {category.name.lower()} expense?", "body": _suggestion_body(recommendation, "subcategories"), "category": category.name, "draftId": str(draft.id), "suggestions": recommendation.as_dicts(), "options": [{"id": str(s.id), "slug": s.slug, "label": s.name} for s in subcategories], "allowCreate": True},
        actions=[
            WidgetAction(id="select", label="Select subcategory", action=WidgetActionId.SELECT_SUBCATEGORY, payload={"draftId": str(draft.id)}),
            WidgetAction(id="add", label="Add new subcategory", action=WidgetActionId.START_ADD_SUBCATEGORY, style="ghost", payload={"draftId": str(draft.id)}),
            *_draft_navigation_actions(draft, revisit_step="category", revisit_label="Change category"),
        ],
    )


def _account_selector(db: Session, draft: TransactionDraft, role: str) -> Widget:
    accounts = list(db.scalars(select(Account).where(Account.user_id == draft.user_id).order_by(Account.name)))
    title = "Which account did the money leave?" if role == "source_account" else "Which account received the money?"
    return Widget(
        id=f"account-{role}-{draft.id}-{uuid4()}",
        type=WidgetType.ACCOUNT_SELECTOR,
        data={"title": title, "body": "Choose a saved account or enter a name.", "draftId": str(draft.id), "role": role, "options": [{"id": str(account.id), "slug": account.name.lower(), "label": account.name} for account in accounts]},
        actions=[
            WidgetAction(id="select", label="Select account", action=WidgetActionId.SELECT_ACCOUNT, payload={"draftId": str(draft.id), "role": role}),
            *_draft_navigation_actions(
                draft,
                revisit_step="transaction_type" if role == "source_account" else "source_account",
                revisit_label="Change type" if role == "source_account" else "Change source account",
            ),
        ],
    )


def _transaction_type_selector(draft: TransactionDraft) -> Widget:
    options = [(item.value, item.value.replace("_", " ").capitalize()) for item in EDITABLE_TRANSACTION_TYPES]
    return Widget(
        id=f"transaction-type-{draft.id}-{uuid4()}",
        type=WidgetType.TRANSACTION_TYPE_SELECTOR,
        data={
            "title": "What kind of financial event is this?",
            "body": f"The amount is {format_money_minor(draft.amount_minor or 0, draft.currency)}. Choose the type and I’ll ask only for anything else that is required.",
            "draftId": str(draft.id),
            "options": [{"id": value, "slug": value, "label": label} for value, label in options],
        },
        actions=[
            WidgetAction(id="select", label="Select transaction type", action=WidgetActionId.SELECT_TRANSACTION_TYPE, payload={"draftId": str(draft.id)}),
            *_draft_navigation_actions(draft),
        ],
    )


def _confirmation(db: Session, draft: TransactionDraft) -> Widget:
    category, subcategory = TaxonomyRepository(db, draft.user_id).path(
        draft.category_id,
        draft.subcategory_id,
    )
    type_label = draft.transaction_type.replace("_", " ").title()
    title_bits = [format_money_minor(draft.amount_minor or 0, draft.currency)]
    if draft.merchant_name:
        title_bits.append(draft.merchant_name)
    elif subcategory:
        title_bits.append(subcategory.name)
    title_bits.append(type_label.lower())
    return Widget(
        id=f"confirm-{draft.id}-{uuid4()}",
        type=WidgetType.CONFIRMATION_CARD,
        data={
            "draftId": str(draft.id),
            "title": " ".join(title_bits),
            "amountMinor": draft.amount_minor,
            "currency": draft.currency,
            "merchant": draft.merchant_name,
            "sourceAccount": draft.source_account_name,
            "destinationAccount": draft.destination_account_name,
            "transactionType": draft.transaction_type,
            "transactionAt": as_utc(draft.transaction_at),
            "category": category.name if category else None,
            "subcategory": subcategory.name if subcategory else None,
            "location": draft.location_label,
            "spendNature": draft.spend_nature,
            "tags": draft.tags,
            "status": "Ready to save",
            "inferredFields": draft.inferred_fields,
        },
        actions=[
            WidgetAction(id="save", label="Save transaction", action=WidgetActionId.COMMIT_TRANSACTION, style="primary", payload={"draftId": str(draft.id)}),
            WidgetAction(id="edit", label="Edit", action=WidgetActionId.EDIT_TRANSACTION, style="secondary", payload={"draftId": str(draft.id)}),
            *([WidgetAction(id="category", label="Change category", action=WidgetActionId.CHANGE_CATEGORY, style="ghost", payload={"draftId": str(draft.id)})] if draft.transaction_type == TransactionType.EXPENSE and draft.merchant_name else []),
            *_draft_navigation_actions(draft),
        ],
    )


def _set_ready_if_complete(draft: TransactionDraft) -> None:
    missing = []
    if draft.amount_minor is None:
        missing.append("amount")
    if draft.transaction_type == TransactionType.UNKNOWN:
        missing.append("transaction_type")
    if draft.transaction_type == TransactionType.EXPENSE and not draft.category_id:
        missing.append("category")
    if draft.transaction_type == TransactionType.EXPENSE and draft.category_id and not draft.subcategory_id:
        missing.append("subcategory")
    if draft.transaction_type == TransactionType.TRANSFER and not draft.source_account_name:
        missing.append("source_account")
    if draft.transaction_type == TransactionType.TRANSFER and not draft.destination_account_name:
        missing.append("destination_account")
    draft.missing_fields = missing
    draft.state = DraftState.NEEDS_CLARIFICATION.value if missing else DraftState.READY_FOR_CONFIRMATION.value


def _create_draft(
    db: Session,
    user: User,
    conversation: Conversation,
    text: str,
    result: ExtractedTransaction | None = None,
    *,
    allow_learned_taxonomy: bool = True,
) -> TransactionDraft:
    current = now_utc()
    result = result or extract_transaction(text, today=local_now(user.timezone, current=current).date(), default_currency=user.currency)
    transaction_at = resolve_event_time(
        day=result.transaction_date,
        clock=result.transaction_time,
        timezone_name=result.timezone or user.timezone,
        current=current,
        use_current_time="transaction_date" in result.inferred_fields and not result.transaction_time,
    )
    _apply_explicit_taxonomy(db, user, text, result)
    taxonomy = TaxonomyRepository(db, user.id)
    category = taxonomy.category_by_slug(result.category_slug) if result.category_slug else None
    subcategory = None
    if category and result.subcategory_slug:
        subcategory = taxonomy.subcategory_by_slug(category.id, result.subcategory_slug)
    field_values = {
        "transaction_type": result.transaction_type,
        "amount": result.amount_minor,
        "merchant": result.merchant,
        "source_account": result.source_account,
        "destination_account": result.destination_account,
        "transaction_at": transaction_at.isoformat(),
        "location": result.location_label,
        "category": result.category_slug,
        "subcategory": result.subcategory_slug,
        "tags": result.tags,
        "spend_nature": result.spend_nature,
    }
    explicit = set(result.explicit_fields)
    inferred = set(result.inferred_fields)
    provenance = {
        key: {
            "origin": "explicit" if key in explicit else "inferred" if key in inferred or value is not None else "missing",
            "confidence": float(result.confidence),
        }
        for key, value in field_values.items()
        if value not in (None, [], "unknown")
    }
    provenance["transaction_at"] = {
        "origin": "explicit" if {"transaction_date", "transaction_time"} & explicit else "inferred",
        "confidence": float(result.confidence),
    }
    inferred_fields = [
        field for field in result.inferred_fields
        if field not in {"transaction_date", "transaction_time", "timezone"}
    ]
    if not ({"transaction_date", "transaction_time"} & explicit):
        inferred_fields.append("transaction_at")
    draft = TransactionDraft(
        user_id=user.id,
        conversation_id=conversation.id,
        raw_text=text,
        transaction_type=result.transaction_type,
        amount_minor=result.amount_minor,
        currency=result.currency,
        merchant_name=result.merchant,
        source_account_name=result.source_account,
        destination_account_name=result.destination_account,
        category_id=category.id if category else None,
        subcategory_id=subcategory.id if subcategory else None,
        transaction_at=transaction_at,
        location_label=result.location_label,
        location_source="user" if "location" in explicit else "inference" if result.location_label else None,
        description=text,
        tags=result.tags,
        spend_nature=result.spend_nature,
        field_provenance=provenance,
        confidence=result.confidence,
        inferred_fields=list(dict.fromkeys(inferred_fields)),
        missing_fields=result.missing_fields,
        state=DraftState.ENRICHED.value,
    )
    db.add(draft)
    db.flush()
    # What the user learned to do outranks a static catalog guess, but never
    # what they just said in this message.
    if (
        allow_learned_taxonomy
        and draft.transaction_type == TransactionType.EXPENSE.value
        and provenance.get("category", {}).get("origin") != "explicit"
    ):
        _apply_confident_taxonomy(db, user, draft)
    _set_ready_if_complete(draft)
    return draft


def _apply_confident_taxonomy(db: Session, user: User, draft: TransactionDraft) -> None:
    """Fill the taxonomy when the user's own history is decisive on its own.

    This replaces a last-write-wins merchant preference. The decision is now a
    vote over decayed observations, so a single miscategorization no longer
    erases a settled habit, and the same evidence is reused for the
    subcategory instead of leaving it to be asked for separately.
    """
    categories = _expense_categories_for_user(db, draft.user_id)
    if not categories:
        return
    ledger = load_ledger(db, draft.user_id, reference=_local_today(user))
    recommendation = recommend_categories(db, user, draft, categories, ledger=ledger)
    if not recommendation.is_confident or recommendation.top is None:
        return
    category = next((item for item in categories if str(item.id) == recommendation.top.id), None)
    if category is None:
        return

    if draft.category_id != category.id:
        # A subcategory belongs to exactly one category, so it cannot survive
        # the parent changing underneath it.
        draft.subcategory_id = None
    draft.category_id = category.id
    draft.inferred_fields = _without_inferred_fields(draft.inferred_fields, TAXONOMY_FIELDS)
    draft.inferred_fields.append("learned from your history")
    provenance = dict(draft.field_provenance)
    provenance["category"] = {"origin": "inferred", "confidence": round(recommendation.confidence, 3)}
    provenance.pop("subcategory", None)

    subcategories = _subcategories_for_user(db, draft.user_id, category.id)
    nested = recommend_subcategories(db, user, draft, category, subcategories, ledger=ledger)
    if nested.is_confident and nested.top is not None:
        match = next((item for item in subcategories if str(item.id) == nested.top.id), None)
        if match is not None:
            draft.subcategory_id = match.id
            provenance["subcategory"] = {"origin": "inferred", "confidence": round(nested.confidence, 3)}
    draft.field_provenance = provenance


def _draft_response(db: Session, conversation: Conversation, draft: TransactionDraft) -> AgentResponse:
    if "amount" in draft.missing_fields:
        content = "What amount should I use?"
        widgets = [Widget(
            id=f"amount-{draft.id}-{uuid4()}",
            type=WidgetType.TRANSACTION_EDIT,
            data={"draftId": str(draft.id), "title": "Add the missing amount", "fields": ["amount"]},
            actions=[
                WidgetAction(id="update", label="Save entry", action=WidgetActionId.UPDATE_TRANSACTION_DRAFT, style="primary", payload={"draftId": str(draft.id)}),
                *_draft_navigation_actions(draft),
            ],
        )]
        pending = PendingAction(action=WidgetActionId.UPDATE_TRANSACTION_DRAFT, resource_id=str(draft.id))
    elif "transaction_type" in draft.missing_fields:
        content = "Is this an expense, income, transfer, or something else?"
        widgets = [_transaction_type_selector(draft)]
        pending = PendingAction(action=WidgetActionId.SELECT_TRANSACTION_TYPE, resource_id=str(draft.id))
    elif "category" in draft.missing_fields:
        content = "I’ve treated this as an expense. Where should I categorize it?"
        widgets = [_category_selector(db, draft)]
        pending = PendingAction(action=WidgetActionId.SELECT_CATEGORY, resource_id=str(draft.id))
    elif "subcategory" in draft.missing_fields:
        content = "Got it. What type?"
        widgets = [_subcategory_selector(db, draft)]
        pending = PendingAction(action=WidgetActionId.SELECT_SUBCATEGORY, resource_id=str(draft.id))
    elif "source_account" in draft.missing_fields:
        content = "Which account did the money leave? You can choose one or type its name."
        widgets = [_account_selector(db, draft, "source_account")]
        pending = PendingAction(action=WidgetActionId.SELECT_ACCOUNT, resource_id=str(draft.id))
    elif "destination_account" in draft.missing_fields:
        content = "Which account received the money? You can choose one or type its name."
        widgets = [_account_selector(db, draft, "destination_account")]
        pending = PendingAction(action=WidgetActionId.SELECT_ACCOUNT, resource_id=str(draft.id))
    else:
        content = f"I found a {format_money_minor(draft.amount_minor or 0, draft.currency)} {draft.transaction_type.replace('_', ' ')}. Check it before I save it."
        widgets = [_confirmation(db, draft)]
        pending = PendingAction(action=WidgetActionId.COMMIT_TRANSACTION, resource_id=str(draft.id))
    return persist_agent_response(
        db,
        conversation,
        content,
        widgets=widgets,
        pending_action=pending,
    )


def _transaction_label(transaction: Transaction, subcategory: Subcategory | None) -> str:
    return transaction.merchant_name or (subcategory.name if subcategory else transaction.transaction_type.replace("_", " "))


def _transaction_preview(db: Session, transaction: Transaction, draft_id: UUID | None = None, status: str = "Saved") -> Widget:
    category, subcategory = TaxonomyRepository(db, transaction.user_id).path(
        transaction.category_id,
        transaction.subcategory_id,
    )
    label = _transaction_label(transaction, subcategory)
    tag_names = list(db.scalars(select(Tag.name).join(TransactionTag, TransactionTag.tag_id == Tag.id).where(TransactionTag.transaction_id == transaction.id).order_by(Tag.name)))
    return Widget(
        id=f"transaction-{status.lower().replace(' ', '-')}-{transaction.id}-{uuid4()}",
        type=WidgetType.TRANSACTION_PREVIEW,
        data={
            "transactionId": str(transaction.id),
            "rowVersion": transaction.row_version,
            "draftId": str(draft_id) if draft_id else None,
            "title": label,
            "amountMinor": transaction.amount_minor,
            "currency": transaction.currency,
            "transactionAt": as_utc(transaction.transaction_at),
            "status": status,
            "sourceCount": len(transaction.sources) or 1,
            "transactionType": transaction.transaction_type,
            "category": category.name if category else None,
            "subcategory": subcategory.name if subcategory else None,
            "location": transaction.location_label,
            "spendNature": transaction.spend_nature,
            "tags": tag_names,
        },
        actions=[] if status == "Removed" else [
            WidgetAction(id="edit", label="Edit", action=WidgetActionId.EDIT_SAVED_TRANSACTION, style="secondary", payload={"transactionId": str(transaction.id)}),
            WidgetAction(id="remove", label="Remove", action=WidgetActionId.REQUEST_REMOVE_TRANSACTION, style="ghost", payload={"transactionId": str(transaction.id)}),
        ],
    )


def _supersede_transaction_previews(
    db: Session,
    conversation: Conversation,
    transaction: Transaction,
    replacement: Widget,
) -> list[WidgetUpdate]:
    """Turn earlier cards for this ledger row into durable audit receipts.

    A transaction UUID names the record; ``rowVersion`` names the snapshot a
    card displayed. Without replacing the old persisted widgets, a correction
    leaves several full-size cards that all appear current after a reload.
    """
    transaction_id = str(transaction.id)
    updates: list[WidgetUpdate] = []
    messages = list(db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at, Message.id)
    ))
    for message in messages:
        stored = list(message.widgets or [])
        changed = False
        for index, raw_widget in enumerate(stored):
            if not isinstance(raw_widget, dict):
                continue
            raw_data = raw_widget.get("data")
            if (
                raw_widget.get("type") != WidgetType.TRANSACTION_PREVIEW.value
                or not isinstance(raw_data, dict)
                or str(raw_data.get("transactionId")) != transaction_id
                or raw_widget.get("id") == replacement.id
                # Preserve the immediate version-to-version chain. Version 1
                # should keep pointing to Version 2 after Version 3 appears.
                or raw_data.get("supersededByWidgetId") is not None
            ):
                continue
            data = {
                **raw_data,
                "lifecycle": WidgetLifecycle.COMPLETED,
                "supersededByVersion": transaction.row_version,
                "supersededByWidgetId": replacement.id,
                "completion": {
                    "action": "supersede_transaction_preview",
                    "values": {
                        "transactionId": transaction_id,
                        "replacementWidgetId": replacement.id,
                        "replacementVersion": transaction.row_version,
                    },
                },
            }
            resolved = Widget(
                id=str(raw_widget["id"]),
                type=WidgetType.TRANSACTION_PREVIEW,
                version=raw_widget.get("version", 1),
                data=data,
                actions=[],
            )
            stored[index] = _serialize_widget(resolved)
            updates.append(WidgetUpdate(widget_id=resolved.id, widget=resolved))
            changed = True
        if changed:
            # JSON columns do not observe mutations within their nested list.
            message.widgets = stored
    return updates


def _transaction_edit_action_patch(proposed: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "amount_minor": "amountMinor",
        "transaction_date": "transactionDate",
        "transaction_type": "transactionType",
        "category_slug": "categorySlug",
        "subcategory_slug": "subcategorySlug",
        "spend_nature": "spendNature",
    }
    return {
        aliases.get(key, key): value
        for key, value in proposed.items()
        if key in {
            "amount_minor", "merchant", "transaction_date", "transaction_type",
            "category_slug", "subcategory_slug", "location", "spend_nature", "tags",
        }
    }


def _transaction_edit_candidate_preview(
    db: Session,
    transaction: Transaction,
    proposed: dict[str, Any],
) -> Widget:
    """Keep a proposed patch attached while the user chooses an exact row."""
    widget = _transaction_preview(db, transaction)
    patch = _transaction_edit_action_patch(proposed)
    widget.actions = [
        action.model_copy(update={
            "payload": {**action.payload, **patch},
        })
        if action.action is WidgetActionId.EDIT_SAVED_TRANSACTION
        else action
        for action in widget.actions
    ]
    return widget


def _transaction_ids_on_message(message: Message) -> list[UUID]:
    """Read server-issued transaction identities from one prior response."""
    ids: list[UUID] = []
    for widget in message.widgets or []:
        if not isinstance(widget, dict) or widget.get("type") != WidgetType.TRANSACTION_PREVIEW:
            continue
        raw_data = widget.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        if data.get("status") == "Removed" or not data.get("transactionId"):
            continue
        try:
            ids.append(UUID(str(data["transactionId"])))
        except ValueError:
            continue
    return list(dict.fromkeys(ids))


def _transaction_edit_targets(
    db: Session,
    user: User,
    conversation: Conversation,
    text: str,
    inputs: dict[str, Any],
) -> list[Transaction]:
    """Resolve an edit target without exposing ledger IDs to the model.

    The target mode and old-record selectors are a separate namespace from
    replacement fields. Only server-issued card IDs or exact canonical fields
    may select a row; prose keyword overlap is never mutation authority.
    """
    target_mode = str(inputs.get("target_mode") or "")
    latest_assistant = db.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.role == "assistant",
            Message.content != "",
            *_history_only(),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    if latest_assistant is not None:
        card_targets = [
            transaction
            for transaction_id in _transaction_ids_on_message(latest_assistant)
            if (transaction := active_transaction(db, user.id, transaction_id)) is not None
        ]
        if target_mode == "preceding_card":
            return card_targets
        if (
            not target_mode
            and card_targets
            and (
                _is_correction_followup(text)
                or re.search(r"\b(?:it|this|that|previous|above|same)\b", text, re.I)
            )
        ):
            return card_targets

    latest_by_entry = target_mode in {"latest", "latest_entered"}
    order = (
        (Transaction.created_at.desc(), Transaction.id.desc())
        if latest_by_entry
        else (Transaction.transaction_at.desc(), Transaction.created_at.desc(), Transaction.id.desc())
    )
    selector_used = False
    statement = canonical_transactions(user.id).order_by(*order)
    target_merchant = normalize_merchant(str(inputs.get("target_merchant") or ""))
    if target_merchant:
        selector_used = True
    if inputs.get("target_amount_minor") is not None:
        selector_used = True
        statement = statement.where(Transaction.amount_minor == int(inputs["target_amount_minor"]))
    if inputs.get("target_transaction_date"):
        selector_used = True
        try:
            target_day = date.fromisoformat(str(inputs["target_transaction_date"]))
        except ValueError:
            return []
        start_at, end_at = utc_range_for_local_dates(target_day, target_day, user.timezone)
        statement = statement.where(
            Transaction.transaction_at >= start_at,
            Transaction.transaction_at < end_at,
        )
    if inputs.get("target_transaction_type"):
        selector_used = True
        statement = statement.where(Transaction.transaction_type == str(inputs["target_transaction_type"]))
    if inputs.get("target_category_slug"):
        selector_used = True
        category = TaxonomyRepository(db, user.id).category_by_slug(
            str(inputs["target_category_slug"]),
            expense_only=True,
        )
        if category is None:
            return []
        statement = statement.where(Transaction.category_id == category.id)

    if not selector_used and target_mode not in {"latest", "latest_entered", "latest_by_transaction_date"}:
        return []
    # An unqualified latest request needs one row. Exact selectors intentionally
    # have no arbitrary recency ceiling: an older transaction remains editable
    # no matter how large the user's ledger becomes.
    if not selector_used:
        statement = statement.limit(1)
    candidates = list(db.scalars(statement))
    if target_merchant:
        candidates = [
            item for item in candidates
            if normalize_merchant(item.merchant_name) == target_merchant
        ]
    if target_mode in {"latest", "latest_entered", "latest_by_transaction_date"}:
        # Qualifiers such as "last expense" are applied before latest wins.
        # The model may propose a mode, but it never gets to erase an exact
        # server-side selector and thereby select a different financial row.
        return candidates[:1]
    return candidates


def _saved_transaction_edit_response(
    db: Session,
    user: User,
    conversation: Conversation,
    transaction: Transaction,
    *,
    proposed: dict[str, Any] | None = None,
) -> AgentResponse:
    """Render the one canonical saved-transaction editor, optionally prefilled."""
    proposed = dict(proposed or {})
    taxonomy = TaxonomyRepository(db, user.id)
    categories = _expense_categories_for_user(db, user.id)
    subcategories = [
        item
        for category in categories
        for item in taxonomy.subcategories(category.id)
    ]

    transaction_type = str(proposed.get("transaction_type") or transaction.transaction_type)
    category_id = transaction.category_id
    subcategory_id = transaction.subcategory_id
    if transaction_type == TransactionType.EXPENSE.value:
        if "category_slug" in proposed:
            category = taxonomy.category_by_slug(
                str(proposed["category_slug"]),
                expense_only=True,
            )
            if category is not None:
                category_id = category.id
                subcategory_id = None
        if "subcategory_slug" in proposed and category_id is not None:
            subcategory = taxonomy.subcategory_by_slug(
                category_id,
                str(proposed["subcategory_slug"]),
            )
            if subcategory is not None:
                subcategory_id = subcategory.id
    else:
        category_id = None
        subcategory_id = None

    transaction_at = transaction.transaction_at
    if proposed.get("transaction_date"):
        try:
            proposed_day = date.fromisoformat(str(proposed["transaction_date"]))
        except ValueError:
            proposed_day = None
        if proposed_day is not None:
            transaction_at = from_local_parts(
                proposed_day,
                local_time(transaction.transaction_at, user.timezone),
                user.timezone,
            )

    current_tags = list(db.scalars(
        select(Tag.name)
        .join(TransactionTag, TransactionTag.tag_id == Tag.id)
        .where(TransactionTag.transaction_id == transaction.id)
        .order_by(Tag.name)
    ))
    amount_minor = int(proposed.get("amount_minor") or transaction.amount_minor)
    data = {
        "transactionId": str(transaction.id),
        "rowVersion": transaction.row_version,
        "title": "Edit saved transaction",
        "amountMinor": amount_minor,
        "currency": transaction.currency,
        "merchant": proposed.get("merchant", transaction.merchant_name),
        "transactionAt": as_utc(transaction_at),
        "transactionType": transaction_type,
        "location": proposed.get("location", transaction.location_label),
        "spendNature": proposed.get("spend_nature", transaction.spend_nature),
        "tags": proposed.get("tags", current_tags),
        "categoryId": str(category_id) if category_id else None,
        "subcategoryId": str(subcategory_id) if subcategory_id else None,
        "categories": [{"id": str(category.id), "label": category.name} for category in categories],
        "subcategories": [
            {"id": str(item.id), "categoryId": str(item.category_id), "label": item.name}
            for item in subcategories
        ],
        "fields": ["amount", "merchant", "transaction_at", "transaction_type", "location", "spend_nature", "tags", "category", "subcategory"],
    }
    widget = Widget(
        id=f"edit-saved-{transaction.id}-{uuid4()}",
        type=WidgetType.TRANSACTION_EDIT,
        data=data,
        actions=[
            WidgetAction(id="update", label="Apply changes", action=WidgetActionId.UPDATE_SAVED_TRANSACTION, style="primary", payload={"transactionId": str(transaction.id), "expectedVersion": transaction.row_version}),
            WidgetAction(id="cancel", label="Cancel", action=WidgetActionId.CANCEL_SAVED_TRANSACTION_EDIT, style="secondary", payload={"transactionId": str(transaction.id)}),
        ],
    )
    if proposed:
        content = (
            f"I prepared the correction to {format_money_minor(amount_minor, transaction.currency)}. "
            "Review the fields below, then press Apply changes."
            if "amount_minor" in proposed
            else "I prefilled the requested changes. Review them, then press Apply changes."
        )
    else:
        content = "Update the fields below. Changes apply when you press Apply changes."
    return persist_agent_response(
        db,
        conversation,
        content,
        widgets=[widget],
        pending_action=PendingAction(
            action=WidgetActionId.UPDATE_SAVED_TRANSACTION,
            resource_id=str(transaction.id),
        ),
    )


def _transaction_edit_response(
    db: Session,
    user: User,
    conversation: Conversation,
    text: str,
    decision: CopilotDecision,
) -> AgentResponse:
    inputs = dict(decision.operation_inputs)
    targets = _transaction_edit_targets(db, user, conversation, text, inputs)
    if not targets:
        return persist_agent_response(
            db,
            conversation,
            "I couldn’t identify one active transaction to edit, so nothing was changed. Open the transaction’s Edit action or describe the old merchant, amount, or date.",
            task_status="failed",
            failure_stage="transaction_target_resolution",
            error_code="transaction_edit_target_not_found",
        )
    if len(targets) > 1:
        shown = targets[:5]
        try:
            proposals = {
                item.id: _transaction_edit_proposal(inputs, text, item)
                for item in shown
            }
        except ValueError as error:
            return persist_agent_response(
                db,
                conversation,
                f"I couldn’t prepare that correction: {error}. Nothing was changed.",
                task_status="failed",
                failure_stage="transaction_edit_validation",
                error_code="invalid_transaction_edit",
            )
        return persist_agent_response(
            db,
            conversation,
            f"I found {len(targets)} possible transactions. Choose Edit on the one you meant; nothing has changed yet.",
            widgets=[
                _transaction_edit_candidate_preview(db, item, proposals[item.id])
                for item in shown
            ],
            citations=[DataReference(
                label="Possible transactions to edit",
                entity_type="transaction",
                entity_ids=[str(item.id) for item in shown],
            )],
        )
    try:
        proposed = _transaction_edit_proposal(inputs, text, targets[0])
    except ValueError as error:
        return persist_agent_response(
            db,
            conversation,
            f"I couldn’t prepare that correction: {error}. Nothing was changed.",
            task_status="failed",
            failure_stage="transaction_edit_validation",
            error_code="invalid_transaction_edit",
        )
    return _saved_transaction_edit_response(db, user, conversation, targets[0], proposed=proposed)


def _committed_response(db: Session, user: User, conversation: Conversation, draft: TransactionDraft) -> AgentResponse:
    transaction = _commit_draft(db, user, draft)
    widget = _transaction_preview(db, transaction, draft.id)
    type_label = transaction.transaction_type.replace("_", " ")
    category_label = str(widget.data.get("category") or "").strip()
    subcategory_label = str(widget.data.get("subcategory") or "").strip()
    merchant_label = str(transaction.merchant_name or "").strip()
    if transaction.transaction_type == TransactionType.EXPENSE.value and category_label:
        path = f"{category_label} → {subcategory_label}" if subcategory_label else category_label
        descriptor = f"expense under {path}"
    elif merchant_label:
        descriptor = f"{type_label} at {merchant_label}"
    else:
        descriptor = type_label
    content = (
        f"Added {format_money_minor(transaction.amount_minor, transaction.currency)} {descriptor}. "
        "You can edit or remove it below."
    )
    return persist_agent_response(db, conversation, content, widgets=[widget])


def _draft_or_commit(db: Session, user: User, conversation: Conversation, draft: TransactionDraft) -> AgentResponse:
    _set_ready_if_complete(draft)
    if draft.state == DraftState.READY_FOR_CONFIRMATION.value:
        return _committed_response(db, user, conversation, draft)
    return _draft_response(db, conversation, draft)


def _looks_like_planning_command(text: str) -> bool:
    lowered = text.lower()
    # A planning noun is not a planning action. "Analyse my expense pattern so
    # I can save" is a read-only diagnostic; routing it to goal CRUD produced a
    # completely unrelated "you have no goal" reply after an analysis failure.
    return bool(
        re.search(
            r"\b(?:create|set(?:\s+up)?|setup|start|make|add|contribute|put|update|change|delete|remove|show|list|view|track)\b"
            r".{0,40}\b(?:budget|goal|savings)\b",
            lowered,
        )
        or re.search(r"\b(?:save|saving)\s+for\b", lowered)
    )


def _looks_like_budget_mutation_command(text: str) -> bool:
    """Separate budget writes from read-only planning views at intake."""
    return bool(
        re.search(r"\bbudget\b", text, re.I)
        and re.search(
            r"\b(?:create|set(?:\s+up)?|setup|make|update|change|lower|raise|delete|remove)\b",
            text,
            re.I,
        )
    )


def _goal_name(text: str) -> str:
    lowered = text.lower()
    known = ("vacation", "emergency fund", "home", "car", "education", "wedding", "retirement")
    for name in known:
        if name in lowered:
            return name.title()
    match = re.search(r"(?:goal|save for|saving for)\s+(?:of\s+)?(?:₹?[\d,.]+\s*(?:lakh|lac|k|crore|cr)?\s+)?([a-z][a-z -]{1,35})", lowered)
    if match:
        return match.group(1).strip(" .").title()
    return "Savings goal"


def _budget_widget(budget_id: str, name: str, amount_minor: int, spent_minor: int, category_slug: str | None, currency: str, actions: list[WidgetAction] | None = None) -> Widget:
    return Widget(
        # A budget can move through saved -> edit -> saved -> delete-confirm
        # in one thread. Its widget id identifies one HITL event, not the
        # underlying budget resource; reusing the resource id causes a newly
        # emitted editor to inherit the completed state of the previous card.
        id=f"budget-{budget_id}-{uuid4()}",
        type=WidgetType.BUDGET_PROGRESS,
        data={
            "budgetId": budget_id,
            "title": name,
            "body": "Monthly budget",
            "amountMinor": amount_minor,
            "spentMinor": spent_minor,
            "remainingMinor": max(amount_minor - spent_minor, 0),
            "percentUsed": round((spent_minor / amount_minor) * 100, 1) if amount_minor else 0,
            "currency": currency,
            "categorySlug": category_slug,
        },
        actions=actions or [],
    )


def _goal_widget(goal_id: str, name: str, target_minor: int, current_minor: int, currency: str, actions: list[WidgetAction] | None = None) -> Widget:
    return Widget(
        id=f"goal-{goal_id}-{uuid4()}",
        type=WidgetType.GOAL_PROGRESS,
        data={
            "goalId": goal_id,
            "title": name,
            "body": "Savings goal",
            "targetMinor": target_minor,
            "currentMinor": current_minor,
            "remainingMinor": max(target_minor - current_minor, 0),
            "percentComplete": round((current_minor / target_minor) * 100, 1) if target_minor else 0,
            "currency": currency,
        },
        actions=actions or [],
    )


_BUDGET_PERIOD_YEAR = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+(?:19|20)\d{2}\b",
    re.I,
)


def _budget_amount_minor(text: str) -> int | None:
    """Read an amount without mistaking a named budget period for money."""
    return parse_amount_minor(_BUDGET_PERIOD_YEAR.sub("", text))


def _budget_spent_minor(db: Session, user: User, category: Category | None) -> int:
    today = _local_today(user)
    start, end = month_bounds(today)
    return expense_summary(
        db,
        user.id,
        start,
        min(today, end),
        category.slug if category else None,
    )["total_minor"]


def _custom_value_clarification(
    db: Session,
    conversation: Conversation,
    original_request: str,
    *,
    question: str,
    reason: str,
    conflict_field: str,
    custom_label: str,
    custom_budget: BudgetSetupSeed | None = None,
    custom_goal: GoalAmountSeed | None = None,
) -> AgentResponse:
    return _clarification_response(
        db,
        conversation,
        original_request,
        ClarificationRequest(
            question=question,
            reason=reason,
            conflict_fields=[conflict_field],
            options=[],
            allow_custom=True,
            custom_label=custom_label,
        ),
        custom_budget=custom_budget,
        custom_goal=custom_goal,
    )


def _budget_setup_response(
    db: Session,
    user: User,
    conversation: Conversation,
    setup: BudgetSetupContract,
) -> AgentResponse:
    taxonomy = TaxonomyRepository(db, user.id)
    category = taxonomy.category(setup.category_id, expense_only=True)
    if setup.category_id is not None and category is None:
        return persist_agent_response(
            db,
            conversation,
            "That budget category is no longer available, so no budget was prepared.",
            task_status="failed",
            failure_stage="planning",
            error_code="budget_category_unavailable",
        )
    owned = UserScopedRepository(db, user.id)
    existing_budget = owned.get(Budget, setup.budget_id) if setup.budget_id else None
    if setup.budget_id is not None and existing_budget is None:
        return persist_agent_response(
            db,
            conversation,
            "That budget is no longer available, so no change was prepared.",
            task_status="failed",
            failure_stage="planning",
            error_code="budget_unavailable",
        )
    if existing_budget and existing_budget.category_id != setup.category_id:
        raise ValueError("Budget category cannot be changed")
    if existing_budget is None:
        existing_budget = db.scalar(select(Budget).where(
            Budget.user_id == user.id,
            Budget.category_id == setup.category_id,
        ))
    updating = existing_budget is not None
    if existing_budget:
        existing_budget.amount_minor = setup.amount_minor
        existing_budget.name = setup.name or existing_budget.name
        budget = existing_budget
    else:
        budget = Budget(
            user_id=user.id,
            category_id=setup.category_id,
            name=setup.name,
            amount_minor=setup.amount_minor,
            currency=setup.currency,
        )
        db.add(budget)
        db.flush()
    spent = _budget_spent_minor(db, user, category)
    widget = _budget_widget(
        str(budget.id),
        budget.name,
        budget.amount_minor,
        spent,
        category.slug if category else None,
        budget.currency,
        _budget_management_actions(budget),
    )
    return persist_agent_response(
        db,
        conversation,
        f"{'Updated' if updating else 'Set'} your {budget.name.lower()} to {format_money_minor(budget.amount_minor, budget.currency)} per month.",
        widgets=[widget],
    )


def _budget_edit_response(
    db: Session,
    user: User,
    conversation: Conversation,
    budget: Budget,
) -> AgentResponse:
    category = TaxonomyRepository(db, user.id).category(budget.category_id, expense_only=True)
    widget = _budget_widget(
        str(budget.id),
        budget.name,
        budget.amount_minor,
        _budget_spent_minor(db, user, category),
        category.slug if category else None,
        budget.currency,
        [
            WidgetAction(
                id="save",
                label="Update budget",
                action=WidgetActionId.SAVE_BUDGET,
                style="primary",
                payload={
                    "budgetId": str(budget.id),
                    "name": budget.name,
                    "amountMinor": budget.amount_minor,
                    "categoryId": str(budget.category_id) if budget.category_id else None,
                },
            ),
            _cancel_pending_action(str(budget.id)),
        ],
    )
    return persist_agent_response(
        db,
        conversation,
        f"Choose the new monthly amount for your {budget.name.lower()}.",
        widgets=[widget],
        pending_action=PendingAction(
            action=WidgetActionId.SAVE_BUDGET,
            resource_id=str(budget.id),
        ),
    )


def _goal_amount_response(
    db: Session,
    user: User,
    conversation: Conversation,
    setup: GoalAmountContract,
) -> AgentResponse:
    """Resume one goal amount slot without reinterpreting the user's number."""
    if setup.operation == WidgetActionId.SAVE_GOAL.value:
        payload = {"name": setup.name, "targetMinor": setup.amount_minor}
        widget = _goal_widget(
            DRAFT_RESOURCE_ID,
            setup.name,
            setup.amount_minor,
            0,
            setup.currency,
            [
                WidgetAction(
                    id="save",
                    label="Create goal",
                    action=WidgetActionId.SAVE_GOAL,
                    style="primary",
                    payload=payload,
                ),
                _cancel_pending_action(DRAFT_RESOURCE_ID),
            ],
        )
        content = (
            f"Ready to create a {format_money_minor(setup.amount_minor, setup.currency)} "
            f"{setup.name} goal."
        )
        pending = PendingAction(
            action=WidgetActionId.SAVE_GOAL,
            resource_id=DRAFT_RESOURCE_ID,
        )
    else:
        goal_id = setup.goal_id
        if goal_id is None:
            raise ValueError("A goal contribution is missing its goal id")
        goal = UserScopedRepository(db, user.id).get(Goal, goal_id)
        if goal is None:
            return _conversation_response(
                db,
                conversation,
                "That savings goal is no longer available. Nothing was changed.",
                task_status="failed",
                failure_stage="goal_resolution",
                error_code="goal_unavailable",
            )
        widget = _goal_widget(
            str(goal.id),
            goal.name,
            goal.target_minor,
            goal.current_minor,
            goal.currency,
            [
                WidgetAction(
                    id="contribute",
                    label=f"Add {format_money_minor(setup.amount_minor, goal.currency)}",
                    action=WidgetActionId.CONTRIBUTE_GOAL,
                    style="primary",
                    payload={"goalId": str(goal.id), "amountMinor": setup.amount_minor},
                ),
                _cancel_pending_action(str(goal.id)),
            ],
        )
        content = (
            f"Ready to add {format_money_minor(setup.amount_minor, goal.currency)} "
            f"to your {goal.name} goal."
        )
        pending = PendingAction(
            action=WidgetActionId.CONTRIBUTE_GOAL,
            resource_id=str(goal.id),
        )
    return persist_agent_response(
        db,
        conversation,
        content,
        widgets=[widget],
        pending_action=pending,
    )


def _planning_response(
    db: Session,
    user: User,
    conversation: Conversation,
    text: str,
    *,
    budget_setup: BudgetSetupContract | None = None,
    goal_amount: GoalAmountContract | None = None,
    allow_budget_mutation: bool = False,
) -> AgentResponse:
    if budget_setup is not None:
        if not allow_budget_mutation:
            raise RuntimeError("A complete budget contract requires the budget mutation capability")
        return _budget_setup_response(db, user, conversation, budget_setup)
    if goal_amount is not None:
        return _goal_amount_response(db, user, conversation, goal_amount)
    lowered = text.lower()
    today = _local_today(user)
    parsed_amount = _budget_amount_minor(text) if "budget" in lowered else extract_transaction(text, today=today, default_currency=user.currency).amount_minor
    widgets: list[Widget] = []
    pending: PendingAction | None = None

    if "budget" in lowered:
        explicit_path = _explicit_taxonomy_match(db, user.id, text)
        category = explicit_path[0] if explicit_path else None
        existing_budget = db.scalar(select(Budget).where(
            Budget.user_id == user.id,
            Budget.category_id == (category.id if category else None),
        ))
        if any(token in lowered for token in ("delete", "remove")):
            if not existing_budget:
                content = f"You don’t have a {category.name.lower() + ' ' if category else ''}budget to delete."
            else:
                spent = _budget_spent_minor(db, user, category)
                widgets = [_budget_widget(
                    str(existing_budget.id),
                    existing_budget.name,
                    existing_budget.amount_minor,
                    spent,
                    category.slug if category else None,
                    existing_budget.currency,
                    [
                        _cancel_pending_action(str(existing_budget.id)),
                        WidgetAction(
                            id="delete",
                            label="Delete budget",
                            action=WidgetActionId.DELETE_BUDGET,
                            style="danger",
                            payload={"budgetId": str(existing_budget.id)},
                        ),
                    ],
                )]
                content = f"Ready to delete your {existing_budget.name.lower()}."
                pending = PendingAction(action=WidgetActionId.DELETE_BUDGET, resource_id=str(existing_budget.id))
        elif any(token in lowered for token in ("set", "create", "make", "limit", "update", "change", "lower", "raise")):
            if not parsed_amount and existing_budget:
                return _budget_edit_response(db, user, conversation, existing_budget)
            if not parsed_amount:
                name = existing_budget.name if existing_budget else f"{category.name} budget" if category else "Monthly spending budget"
                return _custom_value_clarification(
                    db,
                    conversation,
                    text,
                    question=(
                        f"What monthly amount should I use for the {category.name} budget?"
                        if category
                        else "What monthly amount should I use for this budget?"
                    ),
                    reason="The amount is required before the budget can be saved.",
                    conflict_field="amount_minor",
                    custom_label="Enter monthly amount",
                    custom_budget=BudgetSetupSeed(
                        category_id=category.id if category else None,
                        category_name=category.name if category else None,
                        budget_id=existing_budget.id if existing_budget else None,
                        name=name,
                        currency=user.currency,
                    ),
                )
            else:
                name = existing_budget.name if existing_budget else f"{category.name} budget" if category else "Monthly spending budget"
                if not allow_budget_mutation:
                    raise RuntimeError("A complete budget request requires the budget mutation capability")
                return _budget_setup_response(
                    db,
                    user,
                    conversation,
                    BudgetSetupContract(
                        category_id=category.id if category else None,
                        category_name=category.name if category else None,
                        budget_id=existing_budget.id if existing_budget else None,
                        name=name,
                        currency=user.currency,
                        amount_minor=parsed_amount,
                    ),
                )
        else:
            budgets = list(db.scalars(select(Budget).where(Budget.user_id == user.id).order_by(Budget.updated_at.desc())))
            if category:
                budgets = [budget for budget in budgets if budget.category_id == category.id]
            start, end = month_bounds(today)
            taxonomy = TaxonomyRepository(db, user.id)
            for budget in budgets:
                category = taxonomy.category(budget.category_id)
                spent = expense_summary(db, user.id, start, min(today, end), category.slug if category else None)["total_minor"]
                actions = _budget_management_actions(budget) if len(budgets) == 1 else []
                widgets.append(_budget_widget(str(budget.id), budget.name, budget.amount_minor, spent, category.slug if category else None, budget.currency, actions))
            content = f"You have {len(budgets)} active monthly budget{'s' if len(budgets) != 1 else ''}." if budgets else "You don’t have a budget yet. You can say “Set a ₹20,000 food budget.”"
    elif any(token in lowered for token in ("add", "contribute", "put")) and any(token in lowered for token in ("savings", "goal", "vacation")):
        name = _goal_name(text)
        goal = db.scalar(select(Goal).where(Goal.user_id == user.id, func.lower(Goal.name) == name.lower()))
        if not goal:
            content = f"I don’t have a {name} goal yet. Tell me its target first, for example “Create a {format_money_minor(parsed_amount or 20_000_000, user.currency)} {name.lower()} goal.”"
        elif not parsed_amount:
            return _custom_value_clarification(
                db,
                conversation,
                text,
                question=f"How much should I add to your {goal.name} goal?",
                reason="The contribution amount is required before an approval can be prepared.",
                conflict_field="amount_minor",
                custom_label="Enter contribution amount",
                custom_goal=GoalAmountSeed(
                    operation=WidgetActionId.CONTRIBUTE_GOAL.value,
                    goal_id=goal.id,
                    name=goal.name,
                    currency=goal.currency,
                ),
            )
        else:
            widgets = [_goal_widget(str(goal.id), goal.name, goal.target_minor, goal.current_minor, goal.currency, [WidgetAction(id="contribute", label=f"Add {format_money_minor(parsed_amount, goal.currency)}", action=WidgetActionId.CONTRIBUTE_GOAL, style="primary", payload={"goalId": str(goal.id), "amountMinor": parsed_amount}), _cancel_pending_action(str(goal.id))])]
            content = f"Ready to add {format_money_minor(parsed_amount, goal.currency)} to your {goal.name} goal."
            pending = PendingAction(action=WidgetActionId.CONTRIBUTE_GOAL, resource_id=str(goal.id))
    elif any(token in lowered for token in ("create", "set", "start", "save for", "saving for")):
        name = _goal_name(text)
        if not parsed_amount:
            return _custom_value_clarification(
                db,
                conversation,
                text,
                question=f"What target amount should I use for your {name} goal?",
                reason="The target amount is required before a goal approval can be prepared.",
                conflict_field="target_minor",
                custom_label="Enter target amount",
                custom_goal=GoalAmountSeed(
                    operation=WidgetActionId.SAVE_GOAL.value,
                    name=name,
                    currency=user.currency,
                ),
            )
        else:
            payload = {"name": name, "targetMinor": parsed_amount}
            widgets = [_goal_widget(DRAFT_RESOURCE_ID, name, parsed_amount, 0, user.currency, [WidgetAction(id="save", label="Create goal", action=WidgetActionId.SAVE_GOAL, style="primary", payload=payload), _cancel_pending_action(DRAFT_RESOURCE_ID)])]
            content = f"Ready to create a {format_money_minor(parsed_amount, user.currency)} {name} goal."
            pending = PendingAction(action=WidgetActionId.SAVE_GOAL, resource_id=DRAFT_RESOURCE_ID)
    else:
        goals = list(db.scalars(select(Goal).where(Goal.user_id == user.id).order_by(Goal.updated_at.desc())))
        widgets = [_goal_widget(str(goal.id), goal.name, goal.target_minor, goal.current_minor, goal.currency) for goal in goals]
        content = f"You have {len(goals)} savings goal{'s' if len(goals) != 1 else ''}." if goals else "You don’t have a savings goal yet. You can say “Create a ₹2 lakh vacation goal.”"

    return persist_agent_response(
        db,
        conversation,
        content,
        widgets=widgets,
        pending_action=pending,
    )


def _user_runtime_tools(db: Session, user: User, today: date) -> list:
    return build_runtime_tools(db, user, today)


def _extracted_from_decision(text: str, decision: CopilotDecision, today: date, default_currency: str) -> ExtractedTransaction:
    baseline = extract_transaction(text, today=today, default_currency=default_currency)
    interpreted = decision.transaction
    if not interpreted:
        return baseline
    interpreted_explicit = set(interpreted.explicit_fields)
    # A model may add understanding, but it cannot demote a value the
    # deterministic parser read directly from the user's text to "inferred".
    explicit = interpreted_explicit | set(baseline.explicit_fields)
    transaction_type = interpreted.transaction_type
    if transaction_type == TransactionType.UNKNOWN and "transaction_type" in baseline.explicit_fields:
        transaction_type = baseline.transaction_type
    deterministic_amount = parse_amount_minor(text)
    amount_minor = deterministic_amount if deterministic_amount is not None else interpreted.amount_minor
    transaction_date = interpreted.transaction_date or baseline.transaction_date or today
    merchant = interpreted.merchant or baseline.merchant
    normalized_text = normalize_merchant(text) or ""
    if merchant and (normalize_merchant(merchant) or "") not in normalized_text:
        merchant = baseline.merchant
    inferred_fields = []
    if "transaction_type" not in explicit:
        inferred_fields.append("transaction_type")
    if "transaction_date" not in explicit:
        inferred_fields.append("transaction_date")
    if interpreted.category_slug and "category" not in explicit:
        inferred_fields.append("category")
    if interpreted.subcategory_slug and "subcategory" not in explicit:
        inferred_fields.append("subcategory")
    if interpreted.transaction_time and "transaction_time" not in explicit:
        inferred_fields.append("transaction_time")
    if interpreted.timezone and "timezone" not in explicit:
        inferred_fields.append("timezone")
    location_label = interpreted.location_label or baseline.location_label
    if location_label and location_label.casefold() not in text.casefold():
        location_label = baseline.location_label
    if location_label and "location" not in explicit:
        inferred_fields.append("location")
    tags = list(dict.fromkeys([*baseline.tags, *[tag.casefold().strip() for tag in interpreted.tags if tag.strip()]]))[:8]
    if tags and "tags" not in explicit:
        inferred_fields.append("tags")
    spend_nature = interpreted.spend_nature if interpreted.spend_nature != SpendNature.UNKNOWN else baseline.spend_nature
    if spend_nature != SpendNature.UNKNOWN and "spend_nature" not in explicit:
        inferred_fields.append("spend_nature")
    category_slug = interpreted.category_slug if "category" in interpreted_explicit else baseline.category_slug or interpreted.category_slug
    subcategory_slug = interpreted.subcategory_slug if "subcategory" in interpreted_explicit else baseline.subcategory_slug or interpreted.subcategory_slug
    if not category_slug_matches_transaction_type(transaction_type, category_slug):
        # The deterministic path is the fallback authority when the model mixes
        # direction and taxonomy (for example income with Other/Other). If it
        # has no compatible path, use the direction's canonical root or leave
        # an expense unresolved so the persisted draft asks the user.
        if category_slug_matches_transaction_type(transaction_type, baseline.category_slug):
            category_slug, subcategory_slug = baseline.category_slug, baseline.subcategory_slug
        elif transaction_type in TRANSACTION_CATEGORY_ROOTS:
            category_slug = TRANSACTION_CATEGORY_ROOTS[transaction_type].value
            subcategory_slug = "other"
        else:
            category_slug = subcategory_slug = None
    return ExtractedTransaction(
        transaction_type=transaction_type,
        amount_minor=amount_minor,
        # Currency is account-level state unless the current prompt explicitly
        # overrides it. The deterministic parser recognizes those explicit
        # symbols/codes/names; a model-authored field can never silently change
        # the user's default currency.
        currency=baseline.currency,
        merchant=merchant,
        source_account=interpreted.source_account or baseline.source_account,
        destination_account=interpreted.destination_account or baseline.destination_account,
        transaction_date=transaction_date,
        category_slug=category_slug,
        subcategory_slug=subcategory_slug,
        transaction_time=interpreted.transaction_time or baseline.transaction_time,
        timezone=interpreted.timezone or baseline.timezone,
        location_label=location_label,
        tags=tags,
        spend_nature=spend_nature,
        explicit_fields=list(explicit),
        confidence=Decimal(str(interpreted.confidence)),
        inferred_fields=inferred_fields,
    )


def _conversation_response(
    db: Session,
    conversation: Conversation,
    content: str,
    *,
    task_status: str = "succeeded",
    failure_stage: str | None = None,
    error_code: str | None = None,
    citations: list[DataReference] | None = None,
    preserve_active_grounding: bool = False,
) -> AgentResponse:
    previous_analysis_state = conversation.active_analysis_state
    previous_data_scope = conversation.active_data_scope
    response = persist_agent_response(
        db,
        conversation,
        content,
        task_status=task_status,
        failure_stage=failure_stage,
        error_code=error_code,
        citations=citations,
        commit=not preserve_active_grounding,
    )
    if preserve_active_grounding:
        # A cited interpretation of the preceding result is not a new query.
        # Keep future follow-ups anchored to the original tool-backed message,
        # while the new message still exposes the inherited citation to users.
        conversation.active_analysis_state = previous_analysis_state
        conversation.active_data_scope = previous_data_scope
        db.commit()
    return response


def _tool_result_data(item):
    value = item.result.data
    if isinstance(value, str):
        try:
            value = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return value
    return value


def _parsed_tool_result(item) -> dict | None:
    value = _tool_result_data(item)
    return value if isinstance(value, dict) else None


# The live-streaming gate reads this: an enumeration answer the fallback
# renderer may have to replace must never have been streamed to the reader
# first.
_SUBCATEGORY_ENUMERATION_REQUEST = re.compile(r"\bsub[\s-]*categor(?:y|ies)\b", re.I)


def _normalized_name(value: str) -> str:
    return " ".join(str(value).split()).casefold().strip(".;:")


def _tool_failure(item) -> dict | None:
    """The failure a tool reported instead of an answer, if it reported one."""
    parsed = _parsed_tool_result(item)
    error = parsed.get("error") if parsed else None
    return error if isinstance(error, dict) else None


def _taxonomy_rendering(request_text: str, result: list) -> str | None:
    """Answer a taxonomy question at the scope it was asked, from the result.

    The renderer reads the request the same way the postcondition does, so a
    question about one category's subcategories is answered with that category's
    subcategories. Answering a narrow question with the whole category list is
    true and useless, which is the failure mode this exists to avoid.
    """
    hierarchy = {
        str(category["name"]): [
            str(child["name"])
            for child in category.get("subcategories") or []
            if isinstance(child, dict) and child.get("name")
        ]
        for category in result
        if isinstance(category, dict) and category.get("name")
    }
    if not hierarchy:
        return None
    if not _SUBCATEGORY_ENUMERATION_REQUEST.search(request_text):
        names = list(hierarchy)
        return (
            f"You have {len(names)} expense categor{'y' if len(names) == 1 else 'ies'}: "
            f"{', '.join(names)}."
        )
    asked = _normalized_name(request_text)
    scoped = [name for name in hierarchy if re.search(rf"\b{re.escape(_normalized_name(name))}\b", asked)]
    if len(scoped) == 1:
        name = scoped[0]
        children = hierarchy[name]
        if not children:
            return f"{name} has no subcategories."
        return (
            f"{name} has {len(children)} subcategor{'y' if len(children) == 1 else 'ies'}: "
            f"{', '.join(children)}."
        )
    listed = scoped or list(hierarchy)
    return "\n".join(
        f"- **{name}:** {', '.join(hierarchy[name]) or 'no subcategories'}" for name in listed
    )


def _grounded_tool_rendering(item, user: User, request_text: str = "") -> str | None:
    """Render a tool result directly, for when model prose cannot be shipped.

    There are two renderings and no templates. A governed analysis already
    carries its own verified markdown, so the harness that computed the answer
    also words it; the taxonomy is metadata rather than a query, so it is
    rendered here at the scope the question asked. Anything else returns
    ``None`` and the caller says it could not verify an answer — a sentence that
    narrates completion without carrying a figure from the result is a
    fabricated success, not a fallback.
    """
    result = _tool_result_data(item)
    if item.name == "read_user_expense_taxonomy" and isinstance(result, list):
        return _taxonomy_rendering(request_text, result)
    if isinstance(result, dict) and result.get("kind") == "governed_analysis" and str(result.get("message") or "").strip():
        # A governed analysis carries its own deterministic grounded rendering;
        # replacing a failed model composition with it loses nothing factual.
        return str(result["message"])
    return None


def _tool_reference(item, *, include_result_summary: bool = True) -> DataReference:
    parsed = _parsed_tool_result(item)
    result_summary = parsed.get("summary") if parsed and isinstance(parsed.get("summary"), dict) else None
    query = {
        "source_kind": "calculator" if item.name in {
            "loan_payment", "loan_amortization_schedule", "loan_with_prepayment",
            "amortize_with_fixed_payment", "loan_strategy_options", "investment_projection", "affordability",
        } else "runtime_tool",
        "tool": item.name,
        "arguments": item.arguments,
    }
    if include_result_summary and result_summary:
        query["result_summary"] = result_summary
    elif include_result_summary and parsed and item.name == "loan_payment":
        query["result_summary"] = parsed
    return DataReference(
        label=f"{item.name.replace('_', ' ').title()} result",
        entity_type="calculator" if query["source_kind"] == "calculator" else "runtime_tool",
        query=query,
    )


def _grounded_chart_widgets(grounding: list) -> list[Widget]:
    """Re-validate and lift data_chart widgets a governed analysis attached.

    Only the chart lane travels through tool payloads; every widget is
    revalidated at this boundary so a malformed payload fails loudly instead
    of reaching the transcript.
    """
    widgets: list[Widget] = []
    for item in grounding:
        parsed = _parsed_tool_result(item)
        if not parsed or parsed.get("kind") != "governed_analysis":
            continue
        for raw_widget in parsed.get("widgets") or []:
            widget = Widget.model_validate(raw_widget)
            if widget.type is WidgetType.DATA_CHART:
                widgets.append(widget)
    return widgets


def _tool_grounded_response(
    db: Session,
    user: User,
    conversation: Conversation,
    request_text: str,
    decision: CopilotDecision,
    validation_callback: Callable[[AnswerValidationMode, str, str], None] | None = None,
    tool_result_callback: Callable[[str, str, str], None] | None = None,
) -> AgentResponse:
    """Persist a grounded answer together with non-sensitive provenance."""
    content = decision.reply
    validation_mode = answer_validation_mode(db, user.id)
    selected_answer_style = answer_style(db, user.id)
    selected_presentation = build_answer_presentation(selected_answer_style)
    task_status, failure_stage, error_code = "succeeded", None, None
    # A call that returned an error produced no data, so it is not a data
    # source: citing it both overstates what the answer rests on and poisons
    # the follow-up lineage, which reads the first citation back as the scope
    # to inherit. The attempt still survives in the run's activity trace.
    grounded_sources = [
        item for item in decision.tool_grounding if _tool_failure(item) is None
    ]
    citations = [_tool_reference(item) for item in grounded_sources]
    if decision.tool_grounding:
        # A turn may hold several calls. A typed tool error is handed back to
        # the model, which routinely corrects the arguments and calls again, so
        # an early failure followed by a success is an ordinary healthy turn,
        # not a failed one. The authoritative result is therefore the last call
        # that succeeded; the turn only fails when none of them did. Reading a
        # fixed position instead would report whichever call happened to run
        # first, discarding an answer the tool actually computed.
        if not grounded_sources:
            # Every attempt failed. Narrating completion over that is the same
            # fabricated success as an unsupported figure, so the turn reports
            # the failure it actually had — the last one, which is the attempt
            # the model stopped on.
            failure = _tool_failure(decision.tool_grounding[-1]) or {}
            final_tool = decision.tool_grounding[-1].name
            failure_code = str(failure.get("code") or "tool_failed")
            db.add(AIAction(
                user_id=user.id,
                conversation_id=conversation.id,
                action_type="tool_result_availability",
                payload_redacted={
                    "successfulResult": False,
                    "attemptedTools": [item.name for item in decision.tool_grounding],
                    "finalTool": final_tool,
                    "failureCode": failure_code,
                },
                status=ExecutionStatus.FAILED,
            ))
            if tool_result_callback:
                tool_result_callback(
                    final_tool,
                    ExecutionStatus.FAILED,
                    f"Every attempted tool call failed; the final attempt reported {failure_code}.",
                )
            # Validation cannot fail when it is disabled. Preserve the visible
            # skipped stage for an explicit Off selection, while the separate
            # tool-result stage owns the actual run failure.
            if validation_mode is AnswerValidationMode.OFF:
                db.add(AIAction(
                    user_id=user.id,
                    conversation_id=conversation.id,
                    action_type="answer_validation",
                    payload_redacted={
                        "mode": validation_mode.value,
                        "skipped": True,
                        "reason": "no_successful_tool_result",
                    },
                    status=ExecutionStatus.COMPLETED,
                ))
            if (
                validation_mode is AnswerValidationMode.OFF
                and validation_callback
            ):
                validation_callback(
                    validation_mode,
                    ExecutionStatus.COMPLETED,
                    "Skipped evidence and requested-answer checks. The run failed separately because no tool produced a successful result.",
                )
            return persist_agent_response(
                db,
                conversation,
                "I couldn’t complete that request because the analysis it needed failed its checks, "
                "so nothing was computed. Open the execution trace to see the exact failure.",
                citations=citations,
                task_status="failed",
                failure_stage=str(failure.get("stage") or "execution"),
                error_code=str(failure.get("code") or "tool_failed"),
            )
        item = grounded_sources[-1]
        parsed = _parsed_tool_result(item) or {}
        if item.name == "loan_payment" and parsed.get("emi_minor") is not None:
            principal = item.arguments.get("principal_minor")
            rate = item.arguments.get("annual_rate_percent")
            months = item.arguments.get("tenure_months")
            terms = []
            if principal is not None:
                terms.append(f"a {format_money_minor(int(principal), user.currency)} loan")
            if months is not None:
                terms.append(f"over {int(months)} months")
            if rate is not None:
                terms.append(f"at {float(rate):g}% annual interest")
            prefix = " ".join(terms)
            content = f"For {prefix}, the estimated monthly EMI is {format_money_minor(int(parsed['emi_minor']), user.currency)}."
            if validation_callback:
                validation_callback(
                    validation_mode,
                    ExecutionStatus.COMPLETED,
                    "Published the deterministic calculator rendering; tenant and tool policies remained active.",
                )
        if not content:
            task_status, failure_stage, error_code = "degraded", "grounding", "empty_model_reply"
            content = _grounded_tool_rendering(item, user, request_text) or (
                "I couldn’t compose a written answer from the verified result. Please try again."
            )
        elif item.name != "loan_payment":
            # The two reply validators have disjoint jobs. Evidence validation
            # proves typed financial claims against successful result values;
            # coverage validation proves that the explicit parts of the user's
            # request are represented. Neither reads SQL arguments or treats
            # presentation syntax as correctness.
            validation_trace_status = None
            validation_trace_detail = None
            if validation_mode is AnswerValidationMode.OFF:
                db.add(AIAction(
                    user_id=user.id,
                    conversation_id=conversation.id,
                    action_type="answer_validation",
                    payload_redacted={
                        "mode": validation_mode.value,
                        "skipped": True,
                    },
                    status=ExecutionStatus.COMPLETED,
                ))
                if validation_callback:
                    validation_callback(
                        validation_mode,
                        ExecutionStatus.COMPLETED,
                        "Skipped evidence and requested-answer checks. Tenant policy, query admission, and SQL safety remained active.",
                    )
                evidence_validation = None
                coverage_validation = None
            else:
                evidence_validation = validate_evidence(
                    content, grounded_sources, request_text
                )
                answer_contract = compile_answer_contract(request_text)
                coverage_validation = (
                    validate_coverage(
                        content,
                        answer_contract,
                        evidence_validation.facts,
                    )
                    if validation_mode is AnswerValidationMode.FULL
                    else None
                )
                missing_evidence = coverage_validation.missing_evidence if coverage_validation else []
                missing_answer = coverage_validation.missing_answer if coverage_validation else []
                validation_passed = evidence_validation.passed and (
                    coverage_validation is None or coverage_validation.passed
                )
                db.add(AIAction(
                    user_id=user.id,
                    conversation_id=conversation.id,
                    action_type="answer_validation",
                    payload_redacted={
                        "mode": validation_mode.value,
                        "skipped": False,
                        "evidencePassed": evidence_validation.passed,
                        "unsupportedClaimKinds": sorted({
                            claim.kind.value for claim in evidence_validation.unsupported
                        }),
                        "missingEvidence": [
                            obligation.code.value for obligation in missing_evidence
                        ],
                        "missingAnswer": [
                            obligation.code.value for obligation in missing_answer
                        ],
                    },
                    status=(
                        ExecutionStatus.COMPLETED
                        if validation_passed else ExecutionStatus.FAILED
                    ),
                ))
                validation_trace_status = (
                    ExecutionStatus.COMPLETED
                    if validation_passed else ExecutionStatus.FAILED
                )
                validation_trace_detail = (
                    "Verified typed financial claims against successful tool results."
                    if validation_mode is AnswerValidationMode.EVIDENCE_ONLY
                    else "Verified typed evidence and coverage of the requested answer."
                )
            if evidence_validation is not None and not evidence_validation.passed:
                repaired = None
                repair_error = None
                repair_obligations = [
                    "Remove or replace every unsupported financial claim. Every financial number in the answer must be copied exactly from the supplied typed evidence; do not calculate a new percentage, ratio, average, share, multiple, or combined value.",
                    *[item.description for item in answer_contract.obligations],
                ]
                try:
                    repaired = repair_grounded_answer(
                        request_text,
                        content,
                        repair_obligations,
                        [fact.as_dict() for fact in evidence_validation.facts],
                        _local_today(user),
                        user.timezone,
                        answer_style=selected_answer_style,
                        presentation=selected_presentation,
                    )
                except Exception as error:
                    repair_error = type(error).__name__
                if repaired:
                    repaired_evidence = validate_evidence(
                        repaired, grounded_sources, request_text
                    )
                    repaired_coverage = (
                        validate_coverage(
                            repaired,
                            answer_contract,
                            repaired_evidence.facts,
                        )
                        if validation_mode is AnswerValidationMode.FULL
                        else None
                    )
                else:
                    repaired_evidence = None
                    repaired_coverage = None
                repair_passed = bool(
                    repaired
                    and repaired_evidence
                    and repaired_evidence.passed
                    and (
                        repaired_coverage is None
                        or repaired_coverage.passed
                    )
                )
                db.add(AIAction(
                    user_id=user.id,
                    conversation_id=conversation.id,
                    action_type="answer_evidence_repair",
                    payload_redacted={
                        "attempted": True,
                        "passed": repair_passed,
                        "errorType": repair_error,
                    },
                    status=(
                        ExecutionStatus.COMPLETED
                        if repair_passed else ExecutionStatus.FAILED
                    ),
                ))
                if repair_passed and repaired is not None:
                    content = repaired
                    evidence_validation = repaired_evidence
                    coverage_validation = repaired_coverage
                    validation_trace_status = ExecutionStatus.COMPLETED
                    validation_trace_detail = (
                        "Verified typed evidence after repairing unsupported financial claims."
                    )
            if evidence_validation is not None and not evidence_validation.passed:
                task_status, failure_stage = "degraded", "grounding"
                error_code = evidence_validation.error_code
                content = _grounded_tool_rendering(item, user, request_text) or (
                    "I couldn’t verify every financial claim against this run’s typed result, "
                    "so I’m not going to publish those figures. Please try again."
                )
            elif coverage_validation is not None and coverage_validation.missing_evidence:
                task_status, failure_stage, error_code = (
                    "failed", "analysis", "answer_evidence_incomplete"
                )
                content = (
                    "The analysis result did not contain every value needed to answer the full "
                    "question, so I stopped instead of presenting a partial answer. Please try again."
                )
            elif coverage_validation is not None and coverage_validation.missing_answer:
                if evidence_validation is None:
                    raise RuntimeError(
                        "Coverage validation requires typed evidence validation"
                    )
                repaired = None
                repair_error = None
                try:
                    repaired = repair_grounded_answer(
                        request_text,
                        content,
                        [item.description for item in coverage_validation.missing_answer],
                        [fact.as_dict() for fact in evidence_validation.facts],
                        _local_today(user),
                        user.timezone,
                        answer_style=selected_answer_style,
                        presentation=selected_presentation,
                    )
                except Exception as error:
                    repair_error = type(error).__name__
                if repaired:
                    repaired_evidence = validate_evidence(
                        repaired, grounded_sources, request_text
                    )
                    repaired_coverage = validate_coverage(
                        repaired,
                        answer_contract,
                        repaired_evidence.facts,
                    )
                else:
                    repaired_evidence = None
                    repaired_coverage = None
                repair_passed = bool(
                    repaired
                    and repaired_evidence
                    and repaired_evidence.passed
                    and repaired_coverage
                    and repaired_coverage.passed
                )
                db.add(AIAction(
                    user_id=user.id,
                    conversation_id=conversation.id,
                    action_type="answer_coverage_repair",
                    payload_redacted={
                        "attempted": True,
                        "passed": repair_passed,
                        "errorType": repair_error,
                    },
                    status=(
                        ExecutionStatus.COMPLETED
                        if repair_passed else ExecutionStatus.FAILED
                    ),
                ))
                if repair_passed:
                    content = repaired
                    validation_trace_status = ExecutionStatus.COMPLETED
                    validation_trace_detail = (
                        "Verified typed evidence and repaired the answer to cover every requested comparison."
                    )
                else:
                    task_status, failure_stage, error_code = (
                        "degraded", "grounding", "answer_coverage_incomplete"
                    )
                    content = (
                        "The data was verified, but I couldn’t compose every requested comparison "
                        "without adding unsupported claims. Please try again."
                    )
            if validation_callback and validation_trace_status and validation_trace_detail:
                validation_callback(
                    validation_mode,
                    validation_trace_status,
                    validation_trace_detail,
                )
    if content is None:
        raise RuntimeError("A grounded response requires reply content")
    return persist_agent_response(
        db,
        conversation,
        content,
        widgets=_grounded_chart_widgets(decision.tool_grounding),
        citations=citations,
        task_status=task_status,
        failure_stage=failure_stage,
        error_code=error_code,
    )


def _taxonomy_response(db: Session, user: User, conversation: Conversation, decision: CopilotDecision) -> AgentResponse:
    taxonomy = decision.taxonomy
    if not taxonomy:
        return _conversation_response(db, conversation, "Tell me whether you want to add a category or a subcategory, and what it should be called.")
    draft = _clarification_draft(db, conversation)
    parent = None
    if taxonomy.parent_category:
        target = taxonomy.parent_category.casefold()
        parent = next((item for item in _expense_categories_for_user(db, user.id) if item.name.casefold() == target or item.slug.casefold() == target), None)
    if taxonomy.operation is WidgetActionId.CREATE_SUBCATEGORY and not parent and draft and draft.category_id:
        parent = TaxonomyRepository(db, user.id).category(draft.category_id)
    if taxonomy.operation is WidgetActionId.CREATE_SUBCATEGORY and not parent:
        return _conversation_response(db, conversation, "Which category should the new subcategory belong to?")
    widget = _taxonomy_editor_widget(
        taxonomy.operation,
        taxonomy.name,
        parent,
        draft,
        taxonomy.subcategories,
    )
    if taxonomy.operation is WidgetActionId.CREATE_TAXONOMY_PATH:
        child_label = ", ".join(taxonomy.subcategories)
        content = (
            f"Review creating the {taxonomy.name} category with "
            f"{child_label} as its subcategor{'y' if len(taxonomy.subcategories) == 1 else 'ies'}."
        )
    elif taxonomy.operation is WidgetActionId.CREATE_SUBCATEGORY:
        assert parent is not None
        content = f"What should the new subcategory under {parent.name} be called?" if not taxonomy.name else f"Review the new {parent.name} subcategory before adding it."
    else:
        content = "What should the new category be called?" if not taxonomy.name else "Review the new category before adding it."
    return persist_agent_response(
        db,
        conversation,
        content,
        widgets=[widget],
        pending_action=PendingAction(
            action=taxonomy.operation,
            resource_id=str(draft.id if draft else conversation.id),
        ),
    )


def _analysis_harness_response(
    db: Session,
    user: User,
    conversation: Conversation,
    decision: CopilotDecision,
    harness_callback: Callable[[str, str, str, str | None], None] | None = None,
    question: str | None = None,
) -> AgentResponse:
    proposal = decision.analysis_tool
    if proposal and proposal.plan.missing_information:
        missing = proposal.plan.missing_information
        if contains_internal_analysis_diagnostic(missing):
            return persist_agent_response(
                db,
                conversation,
                "I couldn’t complete that calculation with the available analysis method. "
                "I haven’t guessed or changed any financial data.",
                task_status="failed",
                failure_stage="planning",
                error_code="analysis_capability_unavailable",
            )
        readable = [item.strip().rstrip(".") for item in missing if item.strip()]
        if len(readable) == 1:
            content = f"Before I calculate this, please provide {readable[0]}."
        else:
            content = "Before I calculate this, please provide: " + "; ".join(readable) + "."
        if question:
            return _custom_value_clarification(
                db,
                conversation,
                question,
                question=content,
                reason="The requested analysis cannot run until this input is supplied.",
                conflict_field=readable[0] if readable else "requested_value",
                custom_label="Enter the missing information",
            )
        return persist_agent_response(
            db,
            conversation,
            content,
            task_status="needs_input",
        )
    try:
        generated = execute_analysis_template(
            db,
            user.id,
            conversation.id,
            _local_today(user),
            proposal,
            decision.candidate_template_id,
            harness_callback,
            question=question,
        )
    except HarnessValidationError as exc:
        # The refusing check already wrote its human-readable reason into the
        # durable stage trace; the reply points there instead of restating it.
        content = "I couldn’t run that request because a governed analysis check stopped it. Open the execution trace to see the exact failed check."
        return persist_agent_response(
            db,
            conversation,
            content,
            task_status="failed",
            failure_stage=exc.failure_stage,
            error_code=exc.error_code,
        )
    result = generated.result
    widgets = result.widgets
    # An assumption the user cannot see is indistinguishable from a wrong
    # answer, so whatever the compiler had to decide on their behalf is stated
    # alongside the numbers it produced.
    message = " ".join([result.message, *(item.detail for item in decision.assumptions)])
    return persist_agent_response(
        db,
        conversation,
        message,
        widgets=widgets,
        citations=result.citations,
    )


def _period_title(start: date, end: date, today: date) -> str:
    current_start, _ = month_bounds(today)
    if start == current_start and end == today:
        return "This month"
    previous_end = current_start.fromordinal(current_start.toordinal() - 1)
    if start == previous_end.replace(day=1) and end == previous_end:
        return "Last month"
    if start == end == today:
        return "Today"
    if start == end == today.fromordinal(today.toordinal() - 1):
        return "Yesterday"
    if end == today:
        return f"Last {(end - start).days + 1} days"
    return f"{start.strftime('%b %d')} – {end.strftime('%b %d')}"


_QUESTION_IDEAS_REQUEST = re.compile(
    r"\b(?:suggest|recommend)\w*\b[^.?!]{0,40}\bquestio\w+\b"
    r"|\bquestio\w+\b[^.?!]{0,40}\b(?:i\s+(?:can|could|should)\s+ask|to\s+ask)\b"
    r"|\bwhat\s+(?:should|can|could)\s+i\s+ask\b",
    re.I,
)

_RENAME_TITLE_REQUEST = re.compile(
    r"\b(?:rename|retitle|(?:change|update|set|edit)\s+(?:the\s+|this\s+)?(?:page\s+|chat\s+|thread\s+|conversation\s+)?title)\b",
    re.I,
)


def _conversation_rename_request(text: str) -> str | None:
    """Extract the requested thread title from an explicit rename ask.

    Only unambiguous requests resolve here — a rename verb plus a concrete new
    title, quoted or introduced by "to". Anything vaguer stays agent-routed so
    the Operator can ask what the title should be.
    """
    if not _RENAME_TITLE_REQUEST.search(text):
        return None
    quoted = re.search(r"[\"“”'‘’]([^\"“”'‘’]{1,500})[\"“”'‘’]", text)
    candidate = quoted.group(1) if quoted else None
    if candidate is None:
        after_to = re.search(r"\bto\b\s+(.+)$", text, re.I | re.S)
        candidate = after_to.group(1) if after_to else None
    if candidate is None:
        return None
    title = " ".join(candidate.split()).strip(" .")
    if not title or len(title) > CONVERSATION_TITLE_MAX:
        return None
    return title


RENAME_TITLE_QUESTION = "What should this thread be called?"


def _awaiting_rename_title(db: Session, conversation: Conversation) -> bool:
    """True when the previous assistant turn is our own deterministic ask.

    Anchoring on the exact template keeps the follow-up deterministic: the
    Operator never has to interpret the bare title reply, so it can never
    improvise (or fabricate) the rename.
    """
    last_assistant = db.scalar(
        select(Message.content)
        .where(
            Message.conversation_id == conversation.id,
            Message.role == "assistant",
            *_history_only(),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    return (last_assistant or "").strip() == RENAME_TITLE_QUESTION


def _conversation_rename_confirmation(db: Session, conversation: Conversation, title: str) -> AgentResponse:
    """Offer the rename as a HITL confirmation instead of doing it silently."""
    widget = Widget(
        id=f"rename-conversation-{uuid4()}",
        type=WidgetType.INSIGHT_CARD,
        data={
            "eyebrow": "Thread settings",
            "title": "Rename this thread",
            "body": f"New title: “{title}”",
        },
        actions=[
            WidgetAction(
                id="confirm-rename",
                label="Rename",
                action=WidgetActionId.RENAME_CONVERSATION,
                style="primary",
                payload={"title": title},
            ),
            WidgetAction(
                id="keep-title",
                label="Keep current title",
                action=WidgetActionId.CANCEL_PENDING_ACTION,
                payload={"resourceId": str(conversation.id)},
            ),
        ],
    )
    return persist_agent_response(
        db,
        conversation,
        f"Rename this thread to “{title}”?",
        widgets=[widget],
        pending_action=PendingAction(
            action=WidgetActionId.RENAME_CONVERSATION,
            resource_id=str(conversation.id),
        ),
    )


def _is_bare_amount(text: str) -> bool:
    """Recognize the same underspecified amount whether or not it has a ledger verb."""
    return bool(re.fullmatch(
        r"\s*(?:(?:add|enter|log|record|save)\s+)?"
        r"(?:(?:₹|rs\.?|inr)\s*)?[\d,]+(?:\.\d+)?"
        r"(?:\s*(?:k|thousand|lakh|lac|crore))?"
        r"(?:\s+(?:entry|transaction))?\s*[.!]?\s*",
        text,
        re.I,
    )) and parse_amount_minor(text) is not None


def _replacement_amount_minor(text: str) -> int | None:
    """Read the replacement side of an amount correction.

    ``parse_amount_minor`` intentionally returns the first financial amount in
    ordinary prose. An edit such as ``change ₹500 to ₹640`` has a different
    grammar: the value after ``to`` is the replacement. Composite Indian
    amounts remain delegated to the canonical parser.
    """
    if _relative_amount_change(text) is not None:
        return None
    replacement_cues = list(re.finditer(
        r"\b(?:to|with)\b|\bshould\s+(?:be|read)\b|\bmake(?:\s+it|\s+the\s+amount)?\b|(?:->|→)",
        text,
        re.I,
    ))
    for cue in reversed(replacement_cues):
        tail = text[cue.end():]
        # ``with`` may introduce another field ("with merchant Toit"). It is
        # an amount replacement only when the value starts immediately after
        # the cue.
        if not re.match(r"\s*(?:(?:₹|rs\.?|inr)\s*)?[0-9]", tail, re.I):
            continue
        replacement = parse_amount_minor(tail)
        if replacement is not None:
            return replacement
    return parse_amount_minor(text)


def _relative_amount_change(text: str) -> tuple[str, int] | None:
    """Return a deterministic relative amount instruction from user text.

    Keywords here describe arithmetic only; they never identify the row. The
    target remains a server-issued card ID or exact canonical selectors.
    """
    lowered = text.casefold()
    direction: str | None = None
    if re.search(r"\b(?:increase|raise|add|increment|more)\b", lowered):
        direction = "add"
    elif re.search(r"\b(?:decrease|reduce|lower|subtract|deduct|less)\b", lowered):
        direction = "subtract"
    if direction is None:
        return None
    # "Increase to ₹500" is a replacement, not current + ₹500.
    if re.search(r"\b(?:increase|raise|decrease|reduce|lower)\b[^.!?]*\bto\b", lowered):
        return None

    percent = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b)", lowered)
    if percent:
        bps = int((Decimal(percent.group(1)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return (f"percentage_{'increase' if direction == 'add' else 'decrease'}", bps)

    cue = re.search(r"\bby\b", text, re.I)
    if cue:
        delta = parse_amount_minor(text[cue.end():])
    else:
        verb = re.search(r"\b(?:increase|raise|add|increment|decrease|reduce|lower|subtract|deduct)\b", text, re.I)
        delta = parse_amount_minor(text[verb.end():]) if verb else None
    if delta is None:
        direction_word = re.search(r"\b(?:more|less)\b", text, re.I)
        delta = parse_amount_minor(text[:direction_word.start()]) if direction_word else None
    return (direction, delta) if delta is not None else None


def _transaction_edit_proposal(
    inputs: dict[str, Any],
    text: str,
    transaction: Transaction,
) -> dict[str, Any]:
    metadata = {"target_mode", "amount_change_kind", "amount_delta_minor", "amount_percent_bps"}
    proposed = {
        key: value
        for key, value in inputs.items()
        if not key.startswith("target_") and key not in metadata
    }

    relative = _relative_amount_change(text)
    if relative is None and inputs.get("amount_change_kind") in {
        "add", "subtract", "percentage_increase", "percentage_decrease",
    }:
        kind = str(inputs["amount_change_kind"])
        operand = inputs.get("amount_percent_bps") if kind.startswith("percentage_") else inputs.get("amount_delta_minor")
        if operand is not None:
            relative = kind, int(operand)

    if relative is not None:
        kind, operand = relative
        if operand <= 0:
            raise ValueError("the increase or decrease must be greater than zero")
        if kind in {"add", "subtract"}:
            amount_minor = transaction.amount_minor + operand if kind == "add" else transaction.amount_minor - operand
        else:
            delta = int(
                (Decimal(transaction.amount_minor) * Decimal(operand) / Decimal(10_000))
                .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            amount_minor = transaction.amount_minor + delta if kind == "percentage_increase" else transaction.amount_minor - delta
        proposed["amount_minor"] = amount_minor
    else:
        explicit_replacement = re.search(
            r"\b(?:to|with)\b|\bshould\s+(?:be|read)\b|\bmake(?:\s+it|\s+the\s+amount)?\b|(?:->|→)",
            text,
            re.I,
        )
        replacement = _replacement_amount_minor(text) if explicit_replacement else None
        if replacement is not None and "amount_minor" in inputs:
            # The user's digits outrank the model-authored copy of those digits.
            proposed["amount_minor"] = replacement

    amount = proposed.get("amount_minor")
    if amount is not None:
        amount = int(amount)
        if amount <= 0:
            raise ValueError("the corrected amount must be greater than zero")
        if amount > MAX_TRANSACTION_AMOUNT_MINOR:
            raise ValueError("the corrected amount exceeds the supported maximum")
        proposed["amount_minor"] = amount
    return proposed


def _is_amount_led_shorthand(text: str) -> bool:
    """Narrow ledger shorthand such as '₹250 for coffee'; not a general intent classifier."""
    return bool(
        re.match(r"\s*(?:₹|rs\.?|inr|\d)", text, re.I)
        and re.search(r"\bfor\b", text, re.I)
        and parse_amount_minor(text) is not None
    )


def _needs_deep_reasoning(text: str) -> bool:
    lowered = text.casefold()
    return any(token in lowered for token in (
        "why", "compare", "versus", " vs ", "which is", "recommend", "should i", "where should",
        "avoidable", "reduce my loan", "reduce loan", "afford", "scenario", "what if", "forecast",
        "project", "prorate", "projection", "rank", "trend", "driver", "optimize",
        "three months", "last 3 months", "last three months",
    ))


def _is_self_contained_calculator_request(
    text: str,
    context_relationship: ContextRelationship,
) -> bool:
    """Scope a complete hypothetical to calculators without answering it.

    The universal Operator still reasons, chooses the calculator, and writes
    the answer. This only withholds ledger/SQL capabilities when the current
    standalone text supplies a numeric scenario and does not ask about the
    user's stored financial records.
    """

    if context_relationship is not ContextRelationship.STANDALONE:
        return False
    calculator_subject = re.search(
        r"\b(?:emi|loan|mortgage|prepay(?:ment)?|amorti[sz]ation|sip|"
        r"compound(?:ed|ing)?|investment\s+projection)\b",
        text,
        re.I,
    )
    stored_data_subject = re.search(
        r"\b(?:transactions?|records?|spend|spent|spending|expenses?|income|salary|"
        r"cash\s*flow|budgets?|categories|subcategories|merchants?|accounts?|"
        r"recurring|subscriptions?)\b",
        text,
        re.I,
    )
    numeric_inputs = re.findall(r"\d+(?:[.,]\d+)?", text)
    return bool(calculator_subject and not stored_data_subject and len(numeric_inputs) >= 2)


def _is_social_conversation_only(text: str) -> bool:
    """Recognize only self-contained greetings; the model still writes them."""

    return bool(re.fullmatch(
        r"\s*(?:(?:hi|hello|hey|hiya)(?:\s+there)?(?:\s*[,—-]\s*"
        r"(?:how\s+are\s+you(?:\s+doing)?|how(?:'s|\s+is)\s+it\s+going))?"
        r"|how\s+are\s+you(?:\s+doing)?|how(?:'s|\s+is)\s+it\s+going)\s*[!?.]*\s*",
        text,
        re.I,
    ))


def _requests_no_record_explanation(
    text: str,
    context_relationship: ContextRelationship,
    turn_intent: TurnIntentContract,
) -> bool:
    """Honor an explicit request for general knowledge without data access."""

    return bool(
        context_relationship is ContextRelationship.STANDALONE
        and not turn_intent.write_evidence
        and not turn_intent.ambiguous
        and re.search(
            r"\bwithout\s+(?:using|looking\s+at|accessing|reading|checking)\s+"
            r"(?:my|our)\s+(?:records?|data|transactions?)\b",
            text,
            re.I,
        )
    )


def _is_correction_followup(text: str) -> bool:
    """Mark turns that must reconcile prior answers instead of merely continue.

    This flag does not choose an intent or a query. It tells the contextual
    model that disagreement is part of the request, so it must compare the
    bounded grounding lineage supplied with recent turns before responding.
    """
    return bool(re.search(
        r"^\s*(?:no\b|but\b|i\s+mean\b|(?:can|could|would)\s+i\s+(?:correct|fix)\b)"
        r"|\b(?:not\s+what\s+i\s+(?:asked|meant)|you\s+(?:said|showed)|earlier\s+you|"
        r"seem(?:s|ed)?\s+to\s+be|that(?:'s|\s+is)\s+(?:wrong|incorrect)|"
        r"why\s+(?:is|are|did)|does(?:n['’]t|\s+not)\s+match|"
        r"(?:correct|fix)\s+(?:it|this|that|the\s+(?:amount|cost|transaction|entry)))\b",
        text,
        re.I,
    ))


def _references_active_data_scope(text: str) -> bool:
    """Whether the prompt explicitly refines the previously displayed records.

    This is a scope safety invariant and does not select intent: Operator still
    decides the query. The domain layer only prevents an unrelated new request
    from accidentally inheriting a prior result-set boundary.
    """
    reference_text = text
    if (
        re.search(r"\bcompare\b", text, re.I)
        and re.search(r"\b(?:this|current)\s+month\b", text, re.I)
        and re.search(
            r"\b(?:same\s+(?:elapsed\s+)?days?|last\s+month|previous\s+month)\b",
            text,
            re.I,
        )
    ):
        # "same elapsed days last month" defines a complete temporal scope;
        # "same" is not an anaphor to a prior chat turn in this construction.
        reference_text = re.sub(
            r"\bsame\s+(?:elapsed\s+)?days?\b",
            "elapsed days",
            text,
            flags=re.I,
        )
    references_scope = bool(re.search(
        r"\b(?:those|these|them|shown|previous|same|only|just)\b"
        r"|\bthe\s+(?:transactions|records|expenses|results|list)\b"
        r"|\bwhich\s+(?:of|one)\b",
        reference_text,
        re.I,
    ))
    references_prior_answer = bool(re.search(r"\babove\b", text, re.I)) and not has_amount_comparison(text)
    return references_scope or references_prior_answer


def _references_prior_analysis(text: str) -> bool:
    """Whether the current turn explicitly depends on the prior analysis.

    Definite nouns such as "the transactions" are not sufficient: users often
    use them in a complete, independent request. Inheritance is reserved for
    actual anaphora or continuation language so a fresh chart cannot silently
    acquire stale dates, filters, metrics or direction semantics.
    """
    reference_text = text
    if (
        re.search(r"\bcompare\b", text, re.I)
        and re.search(r"\b(?:this|current)\s+month\b", text, re.I)
        and re.search(
            r"\b(?:same\s+(?:elapsed\s+)?days?|last\s+month|previous\s+month)\b",
            text,
            re.I,
        )
    ):
        reference_text = re.sub(
            r"\bsame\s+(?:elapsed\s+)?days?\b",
            "elapsed days",
            text,
            flags=re.I,
        )
    references_analysis = bool(re.search(
        r"\b(?:those|these|them|that|same|shown|previous|earlier|former|latter)\b"
        r"|^\s*(?:and|also|now|then|instead)\b"
        r"|\b(?:what|how)\s+about\b",
        reference_text,
        re.I,
    ))
    references_prior_answer = bool(re.search(r"\babove\b", text, re.I)) and not has_amount_comparison(text)
    return references_analysis or references_prior_answer


def _recent_assistant_expects_value(recent_context: list[dict[str, Any]]) -> bool:
    """Compatibility safety net for older/plain value requests.

    New missing-input flows persist an interrupt. Existing transcripts and a
    provider that still emits a plain scalar question must nevertheless keep a
    reply such as ``24`` out of the standalone transaction shortcut.
    """
    latest_assistant = next(
        (
            str(item.get("content") or "")
            for item in reversed(recent_context)
            if item.get("role") == "assistant"
        ),
        "",
    )
    return expects_value_answer(latest_assistant)


def _context_relationship(text: str, active_analysis_state: dict | None) -> ContextRelationship:
    """Classify state inheritance before either model sees structured state."""
    if _is_correction_followup(text):
        return ContextRelationship.CORRECTION
    if _references_prior_analysis(text) or _references_active_data_scope(text):
        return ContextRelationship.FOLLOW_UP
    if active_analysis_state:
        # An underspecified request to render a prior calculation is a genuine
        # continuation even without an explicit pronoun ("create a visual").
        asks_for_visual = bool(re.search(
            r"\b(?:chart|graph|plot|visual|visuali[sz]e|dashboard)\b",
            text,
            re.I,
        ))
        names_new_financial_scope = bool(re.search(
            r"\b(?:expense|expenses|spend|spending|income|salary|transactions?|records?|"
            r"budget|goal|loan|account|merchant|category)\b",
            text,
            re.I,
        ))
        if asks_for_visual and not names_new_financial_scope:
            return ContextRelationship.FOLLOW_UP
    return ContextRelationship.STANDALONE


def _release_unreferenced_prior_filters(
    text: str,
    query: QueryInterpretation,
    active_analysis_state: dict | None,
) -> QueryInterpretation:
    """Remove filters copied into a self-contained, independent request.

    The semantic agents may read recent grounding so they can resolve genuine
    follow-ups. That same context must not silently narrow a fresh request.
    Only values that exactly match the prior query are candidates here; new
    filters parsed from the current message remain untouched.
    """
    if _references_prior_analysis(text) or not active_analysis_state:
        return query
    prior_queries = active_analysis_state.get("queries") or [active_analysis_state.get("query") or {}]
    prior = next((item for item in reversed(prior_queries) if isinstance(item, dict) and item), None)
    if not prior:
        return query

    normalized_text = " ".join(text.casefold().replace("_", " ").replace("-", " ").split())
    direction_words = {
        "income": ("income", "earning", "earnings", "earned", "salary", "credit", "credited", "inflow"),
        "expense": ("expense", "expenses", "spending", "spend", "spent", "debit", "outflow"),
        "transfer": ("transfer", "transfers", "transferred"),
        "refund": ("refund", "refunds", "refunded"),
        "cash_withdrawal": ("withdrawal", "withdrawals", "withdrawn", "atm"),
    }
    updates: dict[str, Any] = {"use_active_scope": False, "scope_transaction_ids": []}
    current_type = query.transaction_type.value if isinstance(query.transaction_type, TransactionType) else query.transaction_type
    if current_type and current_type == prior.get("transaction_type"):
        aliases = direction_words.get(str(current_type), (str(current_type).replace("_", " "),))
        if not any(re.search(rf"\b{re.escape(alias)}\b", normalized_text) for alias in aliases):
            updates["transaction_type"] = None
            if query.metric in {"spending_summary", "income_summary", "net_spend"}:
                updates["metric"] = "transaction_summary"

    for field in ("merchant", "category_slug", "subcategory_slug", "account", "tag"):
        value = getattr(query, field)
        if value is None or value != prior.get(field):
            continue
        normalized_value = " ".join(str(value).casefold().replace("_", " ").replace("-", " ").split())
        if normalized_value not in normalized_text:
            updates[field] = None

    has_amount_constraint = bool(re.search(
        r"\b(?:above|below|over|under|more than|less than|at least|at most|minimum|maximum)\b",
        normalized_text,
    ))
    if not has_amount_constraint:
        for field in ("min_amount_minor", "max_amount_minor"):
            value = getattr(query, field)
            if value is not None and value == prior.get(field):
                updates[field] = None

    explicit_period = bool(
        parse_spending_period(text)
        or re.search(
            r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
            r"jul(?:y)?|aug(?:ust)?|agust|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
            r"dec(?:ember)?|today|yesterday|ytd|mtd|\d{4})\b",
            text,
            re.I,
        )
    )
    if not explicit_period:
        for field in ("start_date", "end_date"):
            value = getattr(query, field)
            prior_value = prior.get(field)
            serialized = value.isoformat() if isinstance(value, date) else value
            if value is not None and serialized == prior_value:
                updates[field] = None
    return query.model_copy(update=updates)


def _ambiguous_numeric_date_decision(text: str) -> CopilotDecision | None:
    """Create the two valid date interpretations without a model call."""
    if not re.search(r"\b(?:expense|expenses|spend|spending|spent|transactions?|records?)\b", text, re.I):
        return None
    interpretations = ambiguous_numeric_date_options(text)
    if not interpretations:
        return None
    options = []
    for interpretation in interpretations:
        start_date = interpretation.start_date
        end_date = interpretation.end_date
        readable = f"{start_date.strftime('%d %b %Y')} to {end_date.strftime('%d %b %Y')}"
        options.append({
            "id": interpretation.id,
            "label": interpretation.label,
            "description": readable,
            "resolution": (
                f"Use start_date={start_date.isoformat()} and end_date={end_date.isoformat()}. "
                "Treat both dates as inclusive."
            ),
        })
    return CopilotDecision(
        tool=capability_for_primitive("agent.clarify@1"),
        clarification=ClarificationRequest(
            question="Which date format did you mean?",
            reason="Both dates form valid but different periods in day/month and month/day formats.",
            conflict_fields=["date_format", "start_date", "end_date"],
            options=options,
            allow_custom=True,
            custom_label="Enter different dates",
        ),
        confidence=1.0,
        reason="The numeric date range has two valid interpretations.",
        safe_reasoning_summary=[
            "Detected two valid date-range interpretations",
            "Wait for the exact period before reading financial records",
        ],
        validated_by="deterministic_date_policy",
        validation_confidence=1.0,
    )


def _fast_path_decision(
    text: str,
    today: date,
    default_currency: str | None = None,
    *,
    context_relationship: ContextRelationship = ContextRelationship.STANDALONE,
) -> tuple[CopilotDecision, ExtractedTransaction | None] | None:
    """Resolve only unambiguous intents; everything else remains agent-routed."""
    ambiguous_dates = _ambiguous_numeric_date_decision(text)
    if ambiguous_dates:
        return ambiguous_dates, None
    if _looks_like_planning_command(text):
        # Budget and goal management is already a deterministic, typed HITL
        # workflow. Keeping an explicit "show/update/delete my … budget"
        # request on that path prevents the general financial-query agent from
        # treating budget state as an ad-hoc SQL analysis.
        return CopilotDecision(
            tool=capability_for_primitive(
                "budget.manage@1"
                if _looks_like_budget_mutation_command(text)
                else "planning.run@1"
            ),
            confidence=1.0,
            reason="Explicit budget or goal management request.",
            safe_reasoning_summary=[
                "Recognized a budget or goal workflow",
                (
                    "Use the typed budget mutation contract"
                    if _looks_like_budget_mutation_command(text)
                    else "Use the governed HITL planning surface"
                ),
            ],
        ), None
    # Small talk (greetings, acknowledgements, identity questions) is never
    # resolved here: the contextual agent owns conversation so replies stay
    # humanized. There is deliberately no canned offline reply either — when
    # no model is available such turns fail closed instead of guessing.
    extracted = extract_transaction(
        text,
        today=today,
        default_currency=default_currency or get_settings().default_currency,
    )
    if (
        _is_bare_amount(text)
        and context_relationship is ContextRelationship.STANDALONE
    ):
        # A number carries an amount, not a direction. Expense used to be the
        # extractor's convenience default here, which let a salary or transfer
        # enter category selection without ever asking what the money was.
        extracted.transaction_type = TransactionType.UNKNOWN
        extracted.category_slug = None
        extracted.subcategory_slug = None
        extracted.inferred_fields = [
            field for field in extracted.inferred_fields
            if field not in {"transaction_type", *TAXONOMY_FIELD_NAMES}
        ]
        extracted.missing_fields = ["transaction_type"]
        return CopilotDecision(
            tool=capability_for_primitive("transaction.record@1"),
            confidence=0.75,
            reason="Bare amount requires transaction-type clarification.",
            safe_reasoning_summary=["Detected a standalone amount", "Ask whether it is an expense, income, transfer, or another type"],
        ), extracted
    if (
        extracted.amount_minor is not None
        and extracted.transaction_type != TransactionType.UNKNOWN
        # A contextual write may inherit taxonomy, dates, or even the requested
        # effect. The stateless extractor cannot safely bind those dependencies,
        # but safe deterministic read routes below remain available.
        and context_relationship is ContextRelationship.STANDALONE
        and not looks_like_financial_query(text)
        and not re.search(
            r"\b(?:remove|delete|undo|edit|change|update|correct|replace)\b",
            text,
            re.I,
        )
        and ("transaction_type" in extracted.explicit_fields or (_is_amount_led_shorthand(text) and extracted.category_slug is not None))
    ):
        return CopilotDecision(
            tool=capability_for_primitive("transaction.record@1"),
            confidence=float(extracted.confidence),
            reason="A complete explicit financial event can use deterministic extraction.",
            safe_reasoning_summary=["Detected a financial event", "Validate the structured draft", "Apply the existing auto-save policy"],
        ), extracted
    if looks_like_financial_query(text) and not _needs_deep_reasoning(text):
        # Analysis reads (summaries, comparisons, recurring detection) belong
        # to the template pool and, offline, to the known-analysis grammars.
        # Only the surviving interactive surfaces keep a deterministic route.
        lowered = text.casefold()
        if "biggest" in lowered or "largest expense" in lowered:
            tool = capability_for_metric("biggest_expenses")
        elif "duplicate" in lowered or "reconciliation" in lowered or "need review" in lowered:
            tool = capability_for_metric("reconciliation_review")
        else:
            return None
        return CopilotDecision(
            tool=tool,
            confidence=0.99,
            reason="A known read-only query shape can use a validated deterministic capability.",
            safe_reasoning_summary=["Recognized a known financial query", "Use structured data with the requested filters and period"],
        ), None
    return None


def _compile_known_analysis(db: Session, user: User, text: str) -> CopilotDecision | None:
    """Compile established analysis grammars; novel requests continue to Planner."""
    lowered = text.casefold()
    today = _local_today(user)
    if _is_known_expense_pattern_analysis(text):
        plan = AnalysisPlan(
            objective="recommendation",
            analysis_type="three_month_allocation",
            context_sources=["budgets", "goals"],
            safe_reasoning_summary=[
                "Review three months of category spending patterns",
                "Check recorded budgets and goals",
                "Separate evidence from savings suggestions",
            ],
        )
        name = "Expense-pattern savings review"
        intent = "three month expense pattern savings recommendation"
    elif "avoidable" in lowered or "unnecessary expense" in lowered:
        plan = AnalysisPlan(
            objective="recommendation",
            analysis_type="avoidable_expenses",
            safe_reasoning_summary=["Review recorded discretionary and fee signals", "Surface candidates for user judgement", "Do not label anything automatically"],
        )
        name = "Review potentially avoidable expenses"
        intent = "review avoidable expenses"
    elif ("where should" in lowered or "spend more" in lowered) and any(token in lowered for token in ("three months", "3 months", "last three")):
        plan = AnalysisPlan(
            objective="recommendation",
            analysis_type="three_month_allocation",
            context_sources=["budgets", "goals"],
            safe_reasoning_summary=["Compare category spending across three months", "Check explicit budgets and goals", "Separate spending room from a recommendation"],
        )
        name = "Three-month allocation review"
        intent = "three month spending allocation recommendation"
    elif (
        any(token in lowered for token in ("reduce", "pay off", "prepay", "prepayment", "strategy"))
        and "loan" in lowered
    ):
        plan = AnalysisPlan(
            objective="scenario",
            analysis_type="loan_strategy",
            context_sources=["loans", "accounts"],
            safe_reasoning_summary=["Load saved loan terms", "Calculate prepayment scenarios deterministically", "Preserve missing-input and cash-reserve caveats"],
        )
        name = "Loan reduction strategy"
        intent = "loan reduction prepayment strategy"
    elif "recurring" in lowered or "subscription" in lowered:
        plan = AnalysisPlan(
            objective="descriptive",
            analysis_type="recurring_expenses",
            safe_reasoning_summary=["Group repeated merchant charges deterministically", "Detect weekly and monthly cadences", "Report only observed patterns"],
        )
        name = "Recurring expense patterns"
        intent = "recurring expenses detection"
    elif "afford" in lowered:
        parsed = extract_transaction(text, default_currency=user.currency)
        plan = AnalysisPlan(
            objective="scenario",
            analysis_type="affordability",
            service_inputs={"purchase_minor": parsed.amount_minor or 20_000_000},
            safe_reasoning_summary=["Load recorded income, expenses, and cash position", "Apply the six-month reserve rule deterministically", "Report the affordability verdict"],
        )
        name = "Affordability check"
        intent = "affordability check for purchase"
    elif "why" in lowered and any(token in lowered for token in ("spend", "expensive", "cost")):
        start = shift_month(today.replace(day=1), -1)
        query = FinanceQueryPlan(
            name="Monthly category spending drivers",
            metric="gross_spend",
            dimensions=list(MONTH_CATEGORY_DIMENSIONS),
            start_date=start,
            end_date=today,
            limit=100,
        )
        plan = AnalysisPlan(
            objective="diagnostic",
            analysis_type="semantic_query",
            queries=[query],
            transforms=[AnalysisTransform(name="Category change drivers", operation="change_drivers", query_name=query.name, dimension="category", period_dimension="month")],
            safe_reasoning_summary=["Compare this month with the prior month", "Calculate category-level changes", "Report only recorded drivers"],
        )
        name = "Monthly spending change drivers"
        intent = "monthly spending change drivers"
    elif "compare" in lowered or "which is larger" in lowered or "which was larger" in lowered:
        aliases = {
            "travelling": DefaultCategorySlug.TRAVEL,
            "traveling": DefaultCategorySlug.TRAVEL,
            "travel": DefaultCategorySlug.TRAVEL,
            # Transport was a category until it was replaced by Travel, and
            # people go on asking for it by the word they have always used.
            # The label changed; what someone means by it did not.
            "transport": DefaultCategorySlug.TRAVEL,
            "commute": DefaultCategorySlug.TRAVEL,
            "restaurant": DefaultCategorySlug.FOOD,
            "restaurants": DefaultCategorySlug.FOOD,
        }
        mentioned = []
        for category in _expense_categories_for_user(db, user.id):
            if re.search(rf"\b{re.escape(category.name.casefold())}\b", lowered) or re.search(rf"\b{re.escape(category.slug.casefold())}\b", lowered):
                mentioned.append(category.slug)
        for alias, slug in aliases.items():
            if re.search(rf"\b{alias}\b", lowered) and slug not in mentioned:
                mentioned.append(slug)
        if len(mentioned) < 2:
            if "last month" not in lowered and "previous month" not in lowered:
                return None
            # A comparison naming fewer than two categories against last month
            # is the whole-month comparison, owned by the dedicated service.
            plan = AnalysisPlan(
                objective="descriptive",
                analysis_type="monthly_comparison",
                safe_reasoning_summary=["Compare month-to-date spending with the same elapsed days last month", "Use the equal-elapsed-days product policy"],
            )
            name = "Month-to-date versus last month"
            intent = "monthly spending versus last month"
        else:
            if any(token in lowered for token in ("last three months", "last 3 months", "three months", "3 months")):
                start = shift_month(today.replace(day=1), -2)
            else:
                parsed_period = parse_spending_period(text, today)
                start = parsed_period[0] if parsed_period else today.replace(day=1)
            dimensions = list(MONTH_CATEGORY_DIMENSIONS) if "month" in lowered else ["category"]
            query = FinanceQueryPlan(
                name="Category spending comparison",
                metric="gross_spend",
                dimensions=dimensions,
                filters=[FinanceFilter(field="category", operator="in", value=mentioned[:6])],
                start_date=start,
                end_date=today,
                limit=100,
            )
            plan = AnalysisPlan(
                objective="diagnostic",
                analysis_type="semantic_query",
                queries=[query],
                transforms=[AnalysisTransform(name="Category total comparison", operation="compare_totals", query_name=query.name, dimension="category")],
                safe_reasoning_summary=["Resolve the requested categories and period", "Aggregate canonical expenses", "Calculate the difference deterministically"],
            )
            name = "Category spending comparison"
            intent = "compare category spending"
    elif (
        ("last month" in lowered or "previous month" in lowered)
        and any(token in lowered for token in ("more than", "less than", "vs ", "versus", "compared"))
        and any(token in lowered for token in ("spend", "spending", "spent", "expense", "expenses"))
    ):
        plan = AnalysisPlan(
            objective="descriptive",
            analysis_type="monthly_comparison",
            safe_reasoning_summary=["Compare month-to-date spending with the same elapsed days last month", "Use the equal-elapsed-days product policy"],
        )
        name = "Month-to-date versus last month"
        intent = "monthly spending versus last month"
    elif (
        any(token in lowered for token in ("how much", "how many rupees", "summary", "breakdown", "total", "spent this", "spending this", "spend this"))
        and not re.search(r"\b(?:table|rows?|records?|transactions?|list)\b", lowered)
        and not re.search(r"\b(?:earn|earned|earning|earnings|income|salary|credit|credited)\b", lowered)
        and not re.search(r"\b(?:at|from|using|via)\b", lowered)
    ):
        period = parse_spending_period(text, today)
        start, end = (period[0], min(period[1], today)) if period else (today.replace(day=1), today)
        category_slug, _ = infer_expense_category(text)
        if not category_slug:
            named = next(
                (item for item in _expense_categories_for_user(db, user.id) if item.name.casefold() in lowered),
                None,
            )
            category_slug = named.slug if named else None
        filters = [FinanceFilter(field="category", value=category_slug)] if category_slug else []
        total_query = FinanceQueryPlan(
            name="Total spend",
            metric="gross_spend",
            filters=list(filters),
            start_date=start,
            end_date=end,
            limit=1,
        )
        breakdown_query = FinanceQueryPlan(
            name="Spend by subcategory" if category_slug else "Spend by category",
            metric="gross_spend",
            dimensions=["subcategory"] if category_slug else ["category"],
            filters=list(filters),
            start_date=start,
            end_date=end,
            limit=50,
        )
        plan = AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            queries=[total_query, breakdown_query],
            safe_reasoning_summary=["Aggregate canonical expenses for the requested period", "Group the same period for context", "Report only recorded totals"],
        )
        name = "Spending summary"
        intent = "spending summary total for period"
    else:
        return None
    proposal = AnalysisToolProposal(
        name=name,
        description=f"Governed capability compiled for: {intent}.",
        intent_signature=intent,
        plan=plan,
    )
    return CopilotDecision(
        tool=capability_for_primitive("analysis.run@1"),
        analysis_tool=proposal,
        safe_reasoning_summary=plan.safe_reasoning_summary,
        confidence=1,
        reason="A known analysis grammar compiled directly to the governed tool protocol.",
    )


def _is_known_expense_pattern_analysis(text: str) -> bool:
    lowered = text.casefold()
    return (
        any(token in lowered for token in ("expense pattern", "spending pattern", "expense behaviour", "spending behaviour"))
        and any(token in lowered for token in ("save", "saving", "savings", "reduce"))
    )


def _removal_confirmation_widget(transaction: Transaction) -> Widget:
    return Widget(
        id=f"remove-{transaction.id}-{uuid4()}",
        type=WidgetType.CONFIRMATION_CARD,
        data={
            "transactionId": str(transaction.id),
            "title": "Remove transaction",
            "amountMinor": transaction.amount_minor,
            "currency": transaction.currency,
            "merchant": transaction.merchant_name,
            "transactionType": transaction.transaction_type,
            "transactionAt": as_utc(transaction.transaction_at),
            "status": "Confirm removal",
            "inferredFields": [],
        },
        actions=[
            WidgetAction(id="remove", label="Remove transaction", action=WidgetActionId.CONFIRM_REMOVE_TRANSACTION, style="primary", payload={"transactionId": str(transaction.id)}),
            WidgetAction(id="cancel", label="Cancel", action=WidgetActionId.CANCEL_REMOVE_TRANSACTION, style="secondary", payload={"transactionId": str(transaction.id)}),
        ],
    )


def _transaction_removal_response(db: Session, user: User, conversation: Conversation, text: str) -> AgentResponse:
    """Generate bounded candidates; only a later confirmed action may delete one."""
    unfinished_states = {
        DraftState.RECEIVED.value,
        DraftState.CLASSIFIED.value,
        DraftState.EXTRACTED.value,
        DraftState.ENRICHED.value,
        DraftState.NEEDS_CLARIFICATION.value,
        DraftState.READY_FOR_CONFIRMATION.value,
    }
    for draft in db.scalars(select(TransactionDraft).where(
        TransactionDraft.conversation_id == conversation.id,
        TransactionDraft.state.in_(unfinished_states),
    )):
        draft.state = DraftState.CANCELLED.value
    candidates = list(db.scalars(
        canonical_transactions(user.id)
        .order_by(Transaction.transaction_at.desc(), Transaction.created_at.desc())
        .limit(250)
    ))
    normalized_text = normalize_merchant(text) or text.casefold()
    mentioned_merchants = {
        normalize_merchant(item.merchant_name)
        for item in candidates
        if item.merchant_name
        and normalize_merchant(item.merchant_name)
        and re.search(rf"\b{re.escape(normalize_merchant(item.merchant_name) or '')}\b", normalized_text)
    }
    amount_text = text
    if mentioned_merchants:
        for merchant_name in {item.merchant_name for item in candidates if item.merchant_name and normalize_merchant(item.merchant_name) in mentioned_merchants}:
            amount_text = re.sub(re.escape(merchant_name), " ", amount_text, flags=re.I)
    amount_minor = parse_amount_minor(amount_text)
    period = parse_spending_period(text, _local_today(user))
    category_slug, _ = infer_expense_category(text)
    matches = candidates
    if mentioned_merchants:
        matches = [item for item in matches if normalize_merchant(item.merchant_name) in mentioned_merchants]
    if amount_minor is not None:
        matches = [item for item in matches if item.amount_minor == amount_minor]
    if period:
        matches = [item for item in matches if period[0] <= local_date(item.transaction_at, user.timezone) <= period[1]]
    if category_slug and not mentioned_merchants:
        category = TaxonomyRepository(db, user.id).category_by_slug(
            category_slug,
            expense_only=True,
        )
        category_id = category.id if category else None
        matches = [item for item in matches if item.category_id == category_id]

    if not matches:
        content = "I couldn’t find an active transaction matching those details. Nothing was removed."
        widgets: list[Widget] = []
        pending = None
        citations: list[DataReference] = []
    elif len(matches) == 1:
        transaction = matches[0]
        content = "I found one matching transaction. Review it before removal."
        widgets = [_removal_confirmation_widget(transaction)]
        pending = PendingAction(action=WidgetActionId.CONFIRM_REMOVE_TRANSACTION, resource_id=str(transaction.id))
        citations = [DataReference(label="Matching active transaction", entity_type="transaction", entity_ids=[str(transaction.id)])]
    else:
        shown = matches[:20]
        label = next(iter(mentioned_merchants), None)
        # Disambiguation is a question, not a data view: the candidates are
        # listed as markdown and the user names one in their own words. The
        # single-match branch above still owns the confirmation HITL, so no
        # record is ever removed straight from this list.
        candidate_lines = "\n".join(
            f"{index}. {format_money_minor(item.amount_minor, item.currency)} at "
            f"{item.merchant_name or item.transaction_type.replace('_', ' ')} on "
            f"{local_date(item.transaction_at, user.timezone).strftime('%b %d, %Y')}"
            for index, item in enumerate(shown, start=1)
        )
        content = join_blocks(
            f"I found {len(matches)} matching {label.title() if label else ''}".replace("  ", " ").strip()
            + f" transaction{'s' if len(matches) != 1 else ''}. Tell me which one to remove.",
            candidate_lines,
        )
        widgets = []
        pending = None
        citations = [DataReference(label="Matching active transactions", entity_type="transaction", entity_ids=[str(item.id) for item in shown])]
    return persist_agent_response(
        db,
        conversation,
        content,
        widgets=widgets,
        citations=citations,
        pending_action=pending,
    )


class UnsupportedResultModeError(RuntimeError):
    """A governed query asked for a result shape this path no longer renders."""

    def __init__(self, result_mode: str | None):
        super().__init__(f"Unsupported governed result mode: {result_mode}")
        self.result_mode = result_mode


def _transaction_search_parts(db: Session, user: User, query: QueryInterpretation) -> tuple[str, list[Widget], list[DataReference]]:
    """Compile one governed transaction query without persisting a chat message."""
    stmt = canonical_transactions(user.id, currency=user.currency)
    if query.use_active_scope and query.scope_transaction_ids:
        stmt = stmt.where(Transaction.id.in_(query.scope_transaction_ids))
    if query.transaction_type:
        stmt = stmt.where(Transaction.transaction_type == query.transaction_type)
    if query.merchant:
        stmt = stmt.where(Transaction.merchant_name.ilike(f"%{query.merchant.strip()}%"))
    if query.category_slug:
        stmt = stmt.join(Category, Category.id == Transaction.category_id).where(Category.slug == query.category_slug)
    if query.subcategory_slug:
        stmt = stmt.join(Subcategory, Subcategory.id == Transaction.subcategory_id).where(Subcategory.slug == query.subcategory_slug)
    if query.account:
        stmt = stmt.join(Account, Account.id == Transaction.account_id).where(Account.name.ilike(f"%{query.account.strip()}%"))
    if query.tag:
        stmt = stmt.join(TransactionTag, TransactionTag.transaction_id == Transaction.id).join(Tag, Tag.id == TransactionTag.tag_id).where(Tag.normalized_name == query.tag.casefold().strip())
    if query.min_amount_minor is not None:
        stmt = stmt.where(Transaction.amount_minor >= query.min_amount_minor)
    if query.max_amount_minor is not None:
        stmt = stmt.where(Transaction.amount_minor <= query.max_amount_minor)
    if query.start_date:
        stmt = stmt.where(Transaction.transaction_at >= from_local_parts(query.start_date, None, user.timezone))
    if query.end_date:
        resolved_end = min(query.end_date, _local_today(user))
        _, end_at = utc_range_for_local_dates(resolved_end, resolved_end, user.timezone)
        stmt = stmt.where(Transaction.transaction_at < end_at)
    filtered_ids = stmt.with_only_columns(Transaction.id).order_by(None).subquery()
    if query.result_mode == "summary":
        category = next((item for item in _expense_categories_for_user(db, user.id) if item.slug == query.category_slug), None)
        subcategory = None
        if query.subcategory_slug:
            candidate_categories = [category] if category else _expense_categories_for_user(db, user.id)
            for candidate_category in candidate_categories:
                if not candidate_category:
                    continue
                subcategory = next((
                    item for item in _subcategories_for_user(db, user.id, candidate_category.id)
                    if item.slug == query.subcategory_slug
                ), None)
                if subcategory:
                    category = candidate_category
                    break
        scope_path = [item for item in (
            category.name if category else None,
            subcategory.name if subcategory else None,
        ) if item]
        scope_label = " → ".join(scope_path)
        total_minor, count = db.execute(
            select(func.coalesce(func.sum(Transaction.amount_minor), 0), func.count(Transaction.id))
            .join(filtered_ids, filtered_ids.c.id == Transaction.id)
        ).one()
        group_label: ColumnElement[str] = func.coalesce(Category.name, "Other")
        group_join: tuple[
            type[Category] | type[Subcategory] | type[Account],
            ColumnElement[bool],
        ] | None = (Category, Category.id == Transaction.category_id)
        if query.group_by == "subcategory":
            group_label = func.coalesce(Subcategory.name, "Other")
            group_join = (Subcategory, Subcategory.id == Transaction.subcategory_id)
        elif query.group_by == "merchant":
            group_label = func.coalesce(Transaction.merchant_name, "Unknown merchant")
            group_join = None
        elif query.group_by == "account":
            group_label = func.coalesce(Account.name, "Unassigned account")
            group_join = (Account, Account.id == Transaction.account_id)
        elif query.group_by == "month":
            dialect = db.get_bind().dialect.name
            group_label = (
                func.strftime("%Y-%m", Transaction.transaction_at)
                if dialect == "sqlite"
                else func.to_char(func.timezone(user.timezone, Transaction.transaction_at), "YYYY-MM")
            )
            group_join = None
        grouped_stmt = (
            select(
                group_label.label("label"),
                func.sum(Transaction.amount_minor).label("amount"),
                func.count(Transaction.id).label("count"),
            )
            .join(filtered_ids, filtered_ids.c.id == Transaction.id)
        )
        if group_join:
            grouped_stmt = grouped_stmt.outerjoin(*group_join)
        group_limit = query.limit if query.operation == "rank" else min(query.limit, 8)
        order_expression = func.sum(Transaction.amount_minor).asc() if query.sort_direction == "asc" else func.sum(Transaction.amount_minor).desc()
        include_breakdown = query.operation in GROUPED_QUERY_OPERATIONS or bool(query.merchant or query.category_slug or query.subcategory_slug)
        grouped = db.execute(grouped_stmt.group_by(group_label).order_by(order_expression).limit(group_limit)).all() if include_breakdown else []
        filters = [
            f"at {query.merchant}" if query.merchant else None,
            f"for {scope_label}" if scope_label else None,
            f"in {query.account}" if query.account else None,
            f"tagged {query.tag}" if query.tag else None,
        ]
        filter_label = " ".join(item for item in filters if item) or "matching records"
        if query.operation == "rank" and grouped:
            rank_kind = "lowest" if query.sort_direction == "asc" else "highest"
            dimension = query.group_by.replace("_", " ")
            content = f"{grouped[0].label} had the {rank_kind} {dimension} spend at {format_money_minor(int(grouped[0].amount), user.currency)}."
        elif query.operation == "rank":
            content = "I found no matching expenses to rank."
        else:
            direction = query.transaction_type or "expense"
            resolved_end_for_copy = min(query.end_date or _local_today(user), _local_today(user))
            period_for_copy = _period_title(query.start_date, resolved_end_for_copy, _local_today(user)).lower() if query.start_date else "across all recorded time"
            if direction == "expense":
                subject = f"Your {scope_label} spending" if scope_label else (f"Your spending at {query.merchant}" if query.merchant else "Your spending")
                content = f"{subject} {period_for_copy} totals {format_money_minor(int(total_minor), user.currency)} across {count} transaction{'s' if count != 1 else ''}."
            else:
                verb = {
                    "income": "earned",
                    "refund": "received in refunds",
                    "reimbursement": "received in reimbursements",
                    "transfer": "transferred",
                    "investment": "invested",
                    "loan_payment": "paid toward loans",
                }.get(direction, "recorded")
                qualifier = "" if filter_label == "matching records" else f" {filter_label}"
                content = f"You {verb} {format_money_minor(int(total_minor), user.currency)}{qualifier} {period_for_copy}, across {count} transaction{'s' if count != 1 else ''}."
        resolved_end = min(query.end_date or _local_today(user), _local_today(user))
        resolved_start = query.start_date
        period_title = _period_title(resolved_start, resolved_end, _local_today(user)) if resolved_start else "All time"
        direction_title = "Spending" if (query.transaction_type or "expense") == "expense" else (query.transaction_type or "transactions").replace("_", " ").title()
        scope_title = (subcategory.name if subcategory else None) or (category.name if category else None) or query.merchant
        summary_title = f"{scope_title + ' spending' if scope_title and direction_title == 'Spending' else (scope_title or direction_title)} · {period_title}"
        breakdown_rows = [{"label": row.label, "amount_minor": int(row.amount)} for row in grouped]
        if query.operation == "total" and scope_label:
            breakdown_rows = [{"label": scope_label, "amount_minor": int(total_minor)}]
        elif query.operation == "total" and query.merchant:
            breakdown_rows = [{"label": query.merchant, "amount_minor": int(total_minor)}]
        omitted_minor = int(total_minor) - sum(item["amount_minor"] for item in breakdown_rows)
        if query.operation == "breakdown" and omitted_minor > 0:
            breakdown_rows.append({"label": "Other categories", "amount_minor": omitted_minor})
        markdown_title = (
            f"{'Highest' if query.sort_direction == 'desc' else 'Lowest'} {query.group_by.replace('_', ' ')} spend"
            if query.operation == "rank"
            else summary_title
        )
        breakdown_table = markdown_table(
            ["", "Amount"],
            [[row["label"], money(row["amount_minor"], user.currency)] for row in breakdown_rows],
        )
        if breakdown_table:
            content = join_blocks(content, markdown_section(markdown_title, breakdown_table))
        citations = [DataReference(
            label="Filtered canonical transaction summary",
            entity_type="transaction",
            entity_ids=[],
            query=query.model_dump(mode="json", exclude_none=True),
        )]
        return content, [], citations
    # Individual records are no longer rendered here. Reading rows is the
    # `transaction_list` grounding tool's job, and the Operator writes the
    # markdown over what that tool returned. A non-summary query arriving here
    # is a routing bug, so it fails loudly instead of answering with a total
    # the user never asked for.
    raise UnsupportedResultModeError(query.result_mode)


def _transaction_search_response(db: Session, user: User, conversation: Conversation, decision: CopilotDecision) -> AgentResponse:
    """Execute and persist one tenant-scoped transaction query."""
    if not decision.query:
        return _conversation_response(db, conversation, "I couldn’t resolve the requested transaction filters safely. Please clarify what records you want to see.")
    try:
        content, widgets, citations = _transaction_search_parts(db, user, decision.query)
    except UnsupportedResultModeError:
        return persist_agent_response(
            db,
            conversation,
            "I couldn’t resolve that request to a governed summary, so nothing was computed.",
            task_status="failed",
            failure_stage="intent_resolution",
            error_code="unsupported_result_mode",
        )
    if decision.assumptions:
        content = " ".join([*(item.detail for item in decision.assumptions), content])
    return persist_agent_response(
        db,
        conversation,
        content,
        widgets=widgets,
        citations=citations,
    )


def _reconcile_correction_query(
    text: str,
    query: QueryInterpretation,
    recent_context: list[dict[str, Any]],
) -> tuple[QueryInterpretation, bool]:
    """Repair a correction against the most relevant prior grounded scope.

    The model still resolves intent. This domain postcondition only prevents an
    omitted period in a correction from silently becoming a new period. It
    chooses lineage by matching specific filters, not merely the latest turn,
    which is important when the latest turn is the answer being challenged.
    """
    if not _is_correction_followup(text):
        return query, False
    lowered = text.casefold().replace("_", " ").replace("-", " ")
    filter_fields = ("merchant", "category_slug", "subcategory_slug", "account", "tag")
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    order = 0
    for entry in recent_context:
        grounding = entry.get("grounding") if isinstance(entry, dict) else None
        for prior in (grounding or {}).get("queries", []):
            if not isinstance(prior, dict):
                continue
            score = 0
            for field in filter_fields:
                prior_value = prior.get(field)
                current_value = getattr(query, field)
                if not prior_value:
                    continue
                prior_words = str(prior_value).casefold().replace("_", " ").replace("-", " ")
                if current_value and str(current_value).casefold() == str(prior_value).casefold():
                    score += 4
                elif prior_words in lowered:
                    score += 3
            if prior.get("transaction_type") and prior.get("transaction_type") == query.transaction_type:
                score += 1
            candidates.append((score, order, prior))
            order += 1
    if not candidates:
        return query, False
    score, _order, prior = max(candidates, key=lambda item: (item[0], item[1]))
    if score < 3:
        return query, False

    updates: dict[str, Any] = {}
    for field in filter_fields:
        prior_value = prior.get(field)
        if getattr(query, field) is None and prior_value:
            prior_words = str(prior_value).casefold().replace("_", " ").replace("-", " ")
            if prior_words in lowered:
                updates[field] = prior_value
    if query.transaction_type is None and prior.get("transaction_type"):
        updates["transaction_type"] = prior["transaction_type"]

    explicit_period = bool(
        parse_spending_period(text)
        or releases_prior_scope(text)
        or re.search(
            r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
            r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
            r"dec(?:ember)?|\d{4})\b|\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b",
            text,
            re.I,
        )
    )
    if not explicit_period:
        prior_start = prior.get("start_date")
        prior_end = prior.get("end_date")
        if (prior_start is None) == (prior_end is None):
            try:
                updates["start_date"] = date.fromisoformat(prior_start) if isinstance(prior_start, str) else prior_start
                updates["end_date"] = date.fromisoformat(prior_end) if isinstance(prior_end, str) else prior_end
            except ValueError:
                pass
    if not updates:
        return query, False
    reconciled = query.model_copy(update=updates)
    return reconciled, reconciled != query



def _normalize_operation_decision(
    text: str,
    decision: CopilotDecision,
    conversation: Conversation,
    emit,
) -> CopilotDecision:
    """Deterministic contract normalizations for a typed operation decision.

    These repair only what the typed schema makes visible — stale result-set
    scope, unreferenced prior filters, an explicit those/these refinement, and
    rank-shape contradictions — never prose, and never a financial value.
    """
    active_data_scope = conversation.active_data_scope
    active_analysis_state = conversation.active_analysis_state

    def target_of(value: CopilotDecision):
        return value.query

    def rebind(value: CopilotDecision, query) -> CopilotDecision:
        return value.model_copy(update={"query": query})

    # 1. An independent query never inherits the displayed record scope.
    target_query = target_of(decision)
    if target_query and target_query.use_active_scope and not _references_active_data_scope(text):
        emit(
            "scope_policy",
            "Removed an unrelated prior result-set scope",
            "completed",
            "domain_policy",
            "This prompt is an independent query, not a refinement of the displayed records",
        )
        decision = rebind(decision, target_query.model_copy(update={"use_active_scope": False, "scope_transaction_ids": []}))

    # 2. Filters from an unrelated prior analysis are released.
    target_query = target_of(decision)
    if target_query:
        released = _release_unreferenced_prior_filters(text, target_query, active_analysis_state)
        if released != target_query:
            emit(
                "scope_policy",
                "Released filters from an unrelated prior analysis",
                "completed",
                "domain_policy",
                "Only filters stated by this independent request were retained",
            )
            decision = rebind(decision, released)

    # 3. An explicit those/these refinement binds the prior result set.
    target_query = target_of(decision)
    if (
        target_query
        and not target_query.use_active_scope
        and active_data_scope
        and _references_active_data_scope(text)
    ):
        emit(
            "state_transition_policy",
            "Bound the explicitly referenced prior result set",
            "completed",
            "domain_policy",
            f"{active_data_scope.get('entityCount', 0)} canonical transaction IDs",
        )
        decision = rebind(decision, target_query.model_copy(update={"use_active_scope": True}))

    # 3b. A scoped query executes against the recorded canonical IDs, never a
    # model-supplied list.
    target_query = target_of(decision)
    if target_query and target_query.use_active_scope and active_data_scope:
        scope_ids = []
        for raw_id in active_data_scope.get("entityIds", []):
            try:
                scope_ids.append(UUID(str(raw_id)))
            except ValueError:
                continue
        decision = rebind(decision, target_query.model_copy(update={"scope_transaction_ids": scope_ids}))

    # 4. Rank-shape contradictions visible from the typed schema alone.
    if decision.query:
        query = decision.query
        lowered = text.casefold()
        descending_rank = bool(re.search(r"\b(?:highest|largest|biggest|most expensive)\b", lowered))
        ascending_rank = bool(re.search(r"\b(?:lowest|smallest|least expensive)\b", lowered))
        if query.operation != "rank" and (descending_rank or ascending_rank) and query.group_by != "none":
            query = query.model_copy(update={
                "operation": "rank",
                "sort_direction": "asc" if ascending_rank else "desc",
                "result_mode": "summary",
                "limit": 1,
            })
            decision = decision.model_copy(update={"query": query})
            emit(
                "contract_normalization",
                "Preserved the explicit ranking request",
                "completed",
                "domain_policy",
                "The typed route had retained a list shape for an explicit highest/lowest request",
            )
        if query.operation == "rank":
            if query.group_by == "none":
                # Ranking individual records is a transaction_list read, not a
                # governed summary. Leaving the decision untouched lets the
                # search lane refuse it rather than answer with a total.
                return decision
            redundant_group = any((
                query.group_by == "category" and bool(query.category_slug),
                query.group_by == "subcategory" and bool(query.subcategory_slug),
                query.group_by == "merchant" and bool(query.merchant),
                query.group_by == "account" and bool(query.account),
            ))
            if redundant_group:
                emit(
                    "contract_normalization",
                    "Resolved a filtered rank to individual records",
                    "completed",
                    "domain_policy",
                    f"A fixed {query.group_by} cannot also be the ranking dimension",
                )
                return decision
    return decision


def _query_response(db: Session, user: User, conversation: Conversation, text: str, decision: CopilotDecision) -> AgentResponse:
    today = _local_today(user)
    start, end = month_bounds(today)
    selected_metric = capability_spec(decision.tool).metric
    citations: list[DataReference] = []
    widgets: list[Widget] = []

    def selected(metric: str) -> bool:
        """Only the typed route is authoritative; prompt keywords never select."""
        return selected_metric == metric

    if selected("reconciliation_review"):
        candidate = db.scalar(select(ReconciliationCandidate).where(ReconciliationCandidate.user_id == user.id, ReconciliationCandidate.decision == ReconciliationOutcome.NEEDS_REVIEW).order_by(ReconciliationCandidate.score.desc()))
        if not candidate:
            content = (
                "There are no ambiguous transactions waiting for review. "
                "Every imported observation is either matched or recorded separately."
            )
            widgets = []
        else:
            owned = UserScopedRepository(db, user.id)
            observation = owned.get(FinancialObservation, candidate.observation_id)
            transaction = owned.get(Transaction, candidate.transaction_id)
            if observation is None or transaction is None:
                raise ValueError("Reconciliation candidate references unavailable records")
            content = "I found a possible duplicate. I won’t merge it without your decision."
            widgets = [Widget(
                id=f"reconcile-{candidate.id}-{uuid4()}",
                type=WidgetType.RECONCILIATION_REVIEW,
                data={"candidateId": str(candidate.id), "title": "Possible duplicate", "score": float(candidate.score), "incoming": {"amountMinor": observation.amount_minor, "currency": observation.currency, "merchant": observation.merchant_raw, "transactionAt": as_utc(observation.transaction_at), "source": observation.source_type}, "existing": {"transactionId": str(transaction.id), "amountMinor": transaction.amount_minor, "currency": transaction.currency, "merchant": transaction.merchant_name, "transactionAt": as_utc(transaction.transaction_at), "sourceCount": len(transaction.sources)}, "signals": candidate.matching_signals},
                actions=[WidgetAction(id="merge", label="Same transaction", action=WidgetActionId.MERGE_RECONCILIATION, style="primary", payload={"candidateId": str(candidate.id)}), WidgetAction(id="separate", label="Keep separate", action=WidgetActionId.SEPARATE_RECONCILIATION, payload={"candidateId": str(candidate.id)})],
            )]
    elif selected("loan"):
        content = "I can calculate this exactly, but I still need the outstanding principal, annual interest rate, and remaining tenure."
        widgets = [Widget(id=f"loan-{uuid4()}", type=WidgetType.LOAN_CALCULATOR, data={"title": "Home-loan prepayment", "body": "Add the loan principal, rate, and remaining months to compare the baseline with a prepayment.", "prepaymentMinor": extract_transaction(text, default_currency=user.currency).amount_minor, "currency": user.currency}, actions=[WidgetAction(id="calculate", label="Calculate", action=WidgetActionId.CALCULATE_LOAN_SCENARIO, style="primary")])]
    elif selected("investment_projection"):
        content = "I can project the change deterministically once you choose a time horizon and expected annual return."
        widgets = [Widget(id=f"investment-{uuid4()}", type=WidgetType.INVESTMENT_PROJECTION, data={"title": "Investment projection", "body": "The result will separate your contributions from estimated returns and state the return assumption.", "monthlyContributionMinor": extract_transaction(text, default_currency=user.currency).amount_minor or 0, "currency": user.currency}, actions=[WidgetAction(id="calculate", label="Project", action=WidgetActionId.CALCULATE_INVESTMENT_SCENARIO, style="primary")])]
    elif selected("biggest_expenses"):
        # Ranked records are read through the `transaction_list` grounding tool
        # so the Operator writes the list itself. This deterministic metric
        # kept only a table renderer, which no longer exists.
        return persist_agent_response(
            db,
            conversation,
            "I couldn’t resolve that ranking to a governed capability, so nothing was computed.",
            task_status="failed",
            failure_stage="intent_resolution",
            error_code="unresolved_financial_query",
        )
    else:
        # Every analysis metric now executes through the template pool and the
        # governed harness; a metric reaching this branch is a catalog bug, and
        # answering it heuristically would hide that bug behind a wrong answer.
        return persist_agent_response(
            db,
            conversation,
            "I couldn’t resolve that query to a governed capability, so nothing was computed.",
            task_status="failed",
            failure_stage="intent_resolution",
            error_code="unresolved_financial_query",
        )

    return persist_agent_response(
        db,
        conversation,
        content,
        widgets=widgets,
        citations=citations,
    )


def _operation_action_payload(operation, inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "operationId": operation.id,
        "operationVersion": operation.version,
        "operationChecksum": operation.checksum,
        "inputs": inputs,
    }


def _operation_form_response(
    db: Session,
    conversation: Conversation,
    operation,
    inputs: dict[str, Any],
) -> AgentResponse:
    definition = operation.definition
    missing = missing_required_inputs(operation, inputs)
    payload = _operation_action_payload(operation, inputs)
    widget = Widget(
        id=f"operation-form-{uuid4()}",
        type=WidgetType.OPERATION_FORM,
        data={
            "title": definition.metadata.title,
            "body": (
                definition.clarification.prompt
                if definition.clarification and definition.clarification.prompt
                else "Provide the required information to continue."
            ),
            **payload,
            "inputSchema": definition.input.schema_,
            "missingFields": missing,
            "submitLabel": "Review",
        },
        actions=[
            WidgetAction(
                id="submit",
                label="Review",
                action=WidgetActionId.SUBMIT_OPERATION,
                style="primary",
                payload=payload,
            ),
            WidgetAction(
                id="cancel",
                label="Cancel",
                action=WidgetActionId.CANCEL_OPERATION,
                style="ghost",
                payload=payload,
            ),
        ],
    )
    return persist_agent_response(
        db,
        conversation,
        widget.data["body"],
        widgets=[widget],
        pending_action=PendingAction(
            action=WidgetActionId.SUBMIT_OPERATION,
            resource_id=f"{operation.id}:{operation.checksum[:12]}",
        ),
        task_status="needs_input",
    )


def _operation_approval_response(
    db: Session,
    conversation: Conversation,
    operation,
    inputs: dict[str, Any],
    *,
    changed: bool = False,
) -> AgentResponse:
    definition = operation.definition
    payload = _operation_action_payload(operation, inputs)
    summary = render_operation_text(definition.approval.summary, inputs) or (
        f"Run {definition.metadata.title} with the values shown below."
    )
    body = (
        "This operation changed after the previous review. Please review and approve the current version."
        if changed
        else summary
    )
    widget = Widget(
        id=f"operation-approval-{uuid4()}",
        type=WidgetType.OPERATION_APPROVAL,
        data={
            "title": definition.approval.title or f"Confirm {definition.metadata.title}",
            "body": body,
            **payload,
            "effect": operation.derived_effect,
            "summary": summary,
        },
        actions=[
            WidgetAction(
                id="approve",
                label="Approve",
                action=WidgetActionId.APPROVE_OPERATION,
                style="primary",
                payload=payload,
            ),
            WidgetAction(
                id="cancel",
                label="Cancel",
                action=WidgetActionId.CANCEL_OPERATION,
                style="ghost",
                payload=payload,
            ),
        ],
    )
    return persist_agent_response(
        db,
        conversation,
        body,
        widgets=[widget],
        pending_action=PendingAction(
            action=WidgetActionId.APPROVE_OPERATION,
            resource_id=f"{operation.id}:{operation.checksum[:12]}",
        ),
        task_status="needs_input",
    )


def _execute_managed_operation_response(
    db: Session,
    user: User,
    conversation: Conversation,
    operation,
    inputs: dict[str, Any],
) -> AgentResponse:
    result = execute_operation(db, user, operation, inputs)
    content = join_blocks(
        f"**{operation.definition.presentation.success.title}**",
        result.message,
    )
    return persist_agent_response(db, conversation, content)


def _managed_operation_response(
    db: Session,
    user: User,
    conversation: Conversation,
    decision: CopilotDecision,
) -> AgentResponse:
    operation = resolve_current_operation(
        operation_catalog(),
        str(decision.operation_id),
        int(decision.operation_version or 0),
        str(decision.operation_checksum),
    )
    inputs = validate_operation_inputs(operation, decision.operation_inputs, require_complete=False)
    if missing_required_inputs(operation, inputs):
        return _operation_form_response(db, conversation, operation, inputs)
    validate_operation_inputs(operation, inputs)
    if requires_confirmation(operation):
        return _operation_approval_response(db, conversation, operation, inputs)
    return _execute_managed_operation_response(db, user, conversation, operation, inputs)


class _ConversationPrimitiveRuntime:
    """Trusted implementations of the protected primitive protocol.

    The primitive registry owns the method name.  The operation file owns the
    primitive sequence.  This runtime only supplies authenticated request
    context and domain services, so adding another operation that composes an
    existing primitive never adds a dispatch branch here.
    """

    def __init__(
        self,
        db: Session,
        user: User,
        conversation: Conversation,
        text: str,
        decision: CopilotDecision,
        emit: Callable[..., None],
        capability: CapabilityId,
        intent_contract: TurnIntentContract,
        authorization: EffectAuthorization,
        *,
        transaction_clarification: ExtractedTransaction | None = None,
        extracted: ExtractedTransaction | None = None,
        budget_setup: BudgetSetupContract | None = None,
        goal_amount: GoalAmountContract | None = None,
        inherited_citations: list[DataReference] | None = None,
    ):
        self.db = db
        self.user = user
        self.conversation = conversation
        self.text = text
        self.decision = decision
        self.emit = emit
        self.capability = capability
        self.intent_contract = intent_contract
        self.authorization = authorization
        self.transaction_clarification = transaction_clarification
        self.extracted = extracted
        self.budget_setup = budget_setup
        self.goal_amount = goal_amount
        self.inherited_citations = inherited_citations or []

    def invoke(self, target, arguments: dict[str, Any]) -> AgentResponse:
        if target.effect is DataEffect.MUTATION and not self.authorization.allowed:
            raise RuntimeError("A mutation primitive reached execution without effect authorization")
        if (
            target.effect is DataEffect.MUTATION
            and capability_spec(self.capability).maximum_effect is not DataEffect.MUTATION
        ):
            raise RuntimeError("A capability invoked a primitive above its declared effect ceiling")
        method_name = target.runtime_method
        method = getattr(self, str(method_name), None)
        if method is None:
            raise RuntimeError(
                f"Protected primitive {target.reference} has no runtime implementation"
            )
        return method(arguments)

    def respond(self, _arguments: dict[str, Any]) -> AgentResponse:
        if self.decision.tool_grounding:
            return _tool_grounded_response(
                self.db,
                self.user,
                self.conversation,
                self.text,
                self.decision,
                lambda mode, status, detail: self.emit(
                    "answer_validation",
                    f"Answer validation: {mode.value.replace('_', ' ')}",
                    status,
                    "answer_validation",
                    detail,
                ),
                lambda tool, status, detail: self.emit(
                    "tool_result",
                    "No successful tool result was available",
                    status,
                    tool,
                    detail,
                ),
            )
        return _conversation_response(
            self.db,
            self.conversation,
            self.decision.reply
            or "Hi! Tell me what happened financially, or ask me anything about your money.",
            citations=self.inherited_citations,
            preserve_active_grounding=bool(self.inherited_citations),
        )

    def clarify(self, _arguments: dict[str, Any]) -> AgentResponse:
        clarification = self.decision.clarification
        if clarification is None:
            raise RuntimeError("The clarification capability has no clarification contract")
        return _clarification_response(
            self.db, self.conversation, self.text, clarification
        )

    def unknown(self, _arguments: dict[str, Any]) -> AgentResponse:
        return _conversation_response(
            self.db,
            self.conversation,
            self.decision.reply
            or "I’m not sure what you want me to do yet. You can record a financial event or ask me a question about your recorded finances.",
            task_status=self.decision.task_status,
            failure_stage=self.decision.failure_stage,
            error_code=self.decision.error_code,
        )

    def record_transaction(self, _arguments: dict[str, Any]) -> AgentResponse:
        resolved = (
            self.transaction_clarification
            or self.extracted
            or _extracted_from_decision(
                self.text,
                self.decision,
                _local_today(self.user),
                self.user.currency,
            )
        )
        draft = _create_draft(
            self.db,
            self.user,
            self.conversation,
            self.text,
            resolved,
            allow_learned_taxonomy=self.transaction_clarification is None,
        )
        return _draft_or_commit(self.db, self.user, self.conversation, draft)

    def edit_transaction(self, _arguments: dict[str, Any]) -> AgentResponse:
        return _transaction_edit_response(
            self.db,
            self.user,
            self.conversation,
            self.text,
            self.decision,
        )

    def remove_transaction(self, _arguments: dict[str, Any]) -> AgentResponse:
        return _transaction_removal_response(
            self.db, self.user, self.conversation, self.text
        )

    def change_taxonomy(self, _arguments: dict[str, Any]) -> AgentResponse:
        return _taxonomy_response(
            self.db, self.user, self.conversation, self.decision
        )

    def run_planning(self, _arguments: dict[str, Any]) -> AgentResponse:
        return _planning_response(
            self.db,
            self.user,
            self.conversation,
            self.text,
            budget_setup=self.budget_setup,
            goal_amount=self.goal_amount,
        )

    def manage_budget(self, _arguments: dict[str, Any]) -> AgentResponse:
        return _planning_response(
            self.db,
            self.user,
            self.conversation,
            self.text,
            budget_setup=self.budget_setup,
            allow_budget_mutation=True,
        )

    def run_query(self, _arguments: dict[str, Any]) -> AgentResponse:
        return _query_response(
            self.db, self.user, self.conversation, self.text, self.decision
        )

    def search_transactions(self, _arguments: dict[str, Any]) -> AgentResponse:
        return _transaction_search_response(
            self.db, self.user, self.conversation, self.decision
        )

    def run_analysis(self, _arguments: dict[str, Any]) -> AgentResponse:
        return _analysis_harness_response(
            self.db,
            self.user,
            self.conversation,
            self.decision,
            lambda stage, label, status, detail: self.emit(
                stage,
                label,
                status,
                self.capability.value,
                detail,
                _analysis_lifecycle_badge(stage, label, status),
            ),
            question=self.text,
        )

    def run_managed_operation(self, _arguments: dict[str, Any]) -> AgentResponse:
        return _managed_operation_response(
            self.db, self.user, self.conversation, self.decision
        )


class ActivityEmitter(Protocol):
    """The shape of the activity callback the dispatcher is handed.

    A `Callable[[...], None]` cannot express a default, so the annotation this
    replaces declared all eight parameters required — and every one of its
    callers passed four or five, contradicting it. It also typed `status` as
    `str` where the implementation accepts the enum. An annotation nobody can
    satisfy documents nothing; this one is the function's real signature.
    """

    def __call__(
        self,
        stage: str,
        label: str,
        status: ExecutionStatus | str,
        tool: str | None = None,
        detail: str | None = None,
        badge: str | None = None,
        input_payload: Any | None = None,
        output_payload: Any | None = None,
    ) -> None: ...


def _dispatch_decision(
    db: Session,
    user: User,
    conversation: Conversation,
    text: str,
    decision: CopilotDecision,
    execute: Callable[[str, str, Callable[[], AgentResponse], Any | None], AgentResponse],
    emit: ActivityEmitter,
    *,
    extracted: ExtractedTransaction | None = None,
    intent_contract: TurnIntentContract | None = None,
    budget_setup: BudgetSetupContract | None = None,
    goal_amount: GoalAmountContract | None = None,
    inherited_citations: list[DataReference] | None = None,
) -> AgentResponse:
    """Execute every routed capability through its one registry-owned executor."""
    spec = capability_spec(decision.tool)
    capability = spec.id
    workflow = operation_catalog().snapshot().operation(capability.value)
    if workflow is None or workflow.source != "core":
        raise RuntimeError(f"Protected operation is unavailable: {capability.value}")
    if spec.invokes("planning.run@1") and _looks_like_budget_mutation_command(text):
        spec = capability_spec(capability_for_primitive("budget.manage@1"))
        capability = spec.id
        workflow = operation_catalog().snapshot().operation(capability.value)
        if workflow is None or workflow.source != "core":
            raise RuntimeError(f"Protected operation is unavailable: {capability.value}")
        emit(
            "effect_normalization",
            "Normalized budget write to the mutation capability",
            ExecutionStatus.COMPLETED,
            capability.value,
            "Keep durable budget effects under their declared authorization ceiling",
        )
    resume_guard = _clarification_resume_guard.get()
    if (
        spec.invokes("agent.clarify@1")
        and decision.clarification is not None
        and resume_guard is not None
    ):
        stall_reason = _clarification_stall_reason(
            dict(resume_guard.get("previous") or {}),
            decision.clarification,
            int(resume_guard.get("depth", 0)),
        )
        if stall_reason:
            emit(
                "clarification_convergence",
                "Stopped a non-progressing clarification",
                ExecutionStatus.COMPLETED,
                "clarification_policy",
                stall_reason,
            )
            response = execute(
                capability_for_primitive("agent.clarify@1").value,
                "Stopping a non-progressing clarification",
                lambda: _conversation_response(
                    db,
                    conversation,
                    (
                        "I couldn’t turn that confirmed choice into a complete executable plan, "
                        "so I stopped instead of asking the same question again. No changes were made."
                    ),
                    task_status="failed",
                    failure_stage="clarification_resolution",
                    error_code="clarification_did_not_progress",
                ),
                {
                    "userMessage": text,
                    "previousConflictFields": list(
                        dict(resume_guard.get("previous") or {}).get("conflictFields", [])
                    ),
                    "nextConflictFields": decision.clarification.conflict_fields,
                    "clarificationDepth": int(resume_guard.get("depth", 0)),
                },
            )
            return response
    transaction_clarification = None
    if spec.invokes("agent.clarify@1") and decision.clarification is not None:
        transaction_clarification = _transaction_clarification_seed(
            text,
            decision.clarification,
            _local_today(user),
            user.currency,
        )
        if transaction_clarification is not None:
            spec = capability_spec(capability_for_primitive("transaction.record@1"))
            capability = spec.id
            workflow = operation_catalog().snapshot().operation(capability.value)
            if workflow is None or workflow.source != "core":
                raise RuntimeError(f"Protected operation is unavailable: {capability.value}")
            emit(
                "continuation_compilation",
                "Normalized transaction clarification to the draft workflow",
                ExecutionStatus.COMPLETED,
                capability.value,
                "Persist the known fields once and ask only for the next missing typed field",
            )
            db.add(AIAction(
                user_id=user.id,
                conversation_id=conversation.id,
                action_type="transaction_clarification_normalized",
                payload_redacted={
                    "tool": capability.value,
                    "conflictFields": decision.clarification.conflict_fields,
                    "missingFields": transaction_clarification.missing_fields,
                },
                status=ExecutionStatus.COMPLETED,
            ))

    intent_contract = intent_contract or resolve_turn_intent(
        text,
        _context_relationship(text, conversation.active_analysis_state),
        implicit_transaction_entry=(
            _is_bare_amount(text) or _is_amount_led_shorthand(text)
        ),
    )
    authorization = authorize_capability(intent_contract, spec)
    emit(
        "effect_authorization",
        (
            "Authorized the capability effect"
            if authorization.allowed
            else "Blocked a capability effect mismatch"
        ),
        ExecutionStatus.COMPLETED,
        capability.value,
        f"{authorization.code} · {authorization.reason}",
        input_payload={
            "intent": intent_contract.model_dump(mode="json"),
            "capability": {
                "id": capability.value,
                "access": spec.access.value,
                "maximumEffect": spec.maximum_effect.value,
            },
        },
        output_payload={
            "outcome": authorization.outcome.value,
            "code": authorization.code,
        },
    )
    db.add(AIAction(
        user_id=user.id,
        conversation_id=conversation.id,
        action_type="effect_authorization",
        payload_redacted={
            "intent": intent_contract.model_dump(mode="json"),
            "capability": capability.value,
            "access": spec.access.value,
            "maximumEffect": spec.maximum_effect.value,
            "outcome": authorization.outcome.value,
            "code": authorization.code,
        },
        status=ExecutionStatus.COMPLETED,
    ))
    if not authorization.allowed:
        if authorization.outcome is AuthorizationOutcome.CLARIFY:
            return _clarification_response(
                db,
                conversation,
                text,
                ClarificationRequest(
                    question="Do you want to view existing records, or create or change one?",
                    reason="The current message depends on prior context and does not safely establish a data-changing action.",
                    conflict_fields=["requested_effect"],
                    options=[
                        ClarificationOption(
                            id="view_records",
                            label="View records",
                            description="Keep this request read-only.",
                            resolution="Treat the original request as read-only and do not create or change financial data.",
                        ),
                        ClarificationOption(
                            id="change_records",
                            label="Create or change",
                            description="Continue through the governed write workflow.",
                            resolution="The customer explicitly confirms that the original request should create or change financial data.",
                        ),
                    ],
                ),
            )
        return persist_agent_response(
            db,
            conversation,
            (
                "I interpreted this as a request to view financial data, so I didn’t "
                "create or change anything. Please try the request again."
            ),
            task_status="failed",
            failure_stage="effect_authorization",
            error_code=authorization.code,
        )

    runtime = _ConversationPrimitiveRuntime(
        db,
        user,
        conversation,
        text,
        decision,
        emit,
        capability,
        intent_contract,
        authorization,
        transaction_clarification=transaction_clarification,
        extracted=extracted,
        budget_setup=budget_setup,
        goal_amount=goal_amount,
        inherited_citations=inherited_citations,
    )

    def run_declared_workflow() -> AgentResponse:
        workflow_inputs = (
            dict(decision.operation_inputs)
            if decision.operation_id == workflow.id
            else {}
        )
        if not workflow_inputs:
            workflow_inputs = operation_inputs_from_route(
                workflow,
                {
                    "transaction": decision.transaction,
                    "query": decision.query,
                    "taxonomy": decision.taxonomy,
                    "presentation": decision.presentation,
                    "clarification": decision.clarification,
                    "reply": decision.reply,
                },
                request=text,
            )
        outputs = execute_operation_steps(workflow, workflow_inputs, runtime.invoke)
        final = outputs[workflow.definition.execution.steps[-1].id]
        if not isinstance(final, AgentResponse):
            raise RuntimeError(f"Operation {workflow.id} did not produce an agent response")
        return final

    execution_input: dict[str, Any] = {
        "userMessage": text,
        "capability": capability.value,
        "decision": decision.model_dump(mode="json", by_alias=True, exclude_none=True),
    }
    resolved_input = transaction_clarification or extracted
    if resolved_input is not None:
        execution_input["extractedTransaction"] = vars(resolved_input)
    response = execute(
        capability.value,
        spec.execution_label,
        run_declared_workflow,
        execution_input,
    )
    return response


@contextmanager
def _reply_reservation(db: Session) -> Iterator[None]:
    """Scopes one turn's reservation so a blank reply can never outlive it.

    A turn that raises before answering rolls its whole session back, which
    takes the empty row with it; this covers the other case, where something
    committed on the way and the row would otherwise stay in the transcript
    as an empty message from the copilot."""
    token = _reserved_reply.set(None)
    try:
        yield
    finally:
        stranded = _reserved_reply.get()
        _reserved_reply.reset(token)
        if stranded is not None:
            try:
                db.delete(stranded)
                db.flush()
            except SQLAlchemyError:
                # The session is already broken, so closing it discards the
                # row anyway. Never mask the original failure with this one.
                db.rollback()


def handle_chat(
    db: Session,
    user: User,
    conversation: Conversation,
    text: str,
    activity_callback: ActivityCallback | None = None,
    text_delta_callback: TextDeltaCallback | None = None,
    reasoning_delta_callback: ReasoningDeltaCallback | None = None,
) -> AgentResponse:
    """Runs one conversational turn, question and answer as a single unit."""
    with _reply_reservation(db):
        response = _run_turn(
            db,
            user,
            conversation,
            text,
            activity_callback,
            text_delta_callback,
            reasoning_delta_callback,
        )
        # The turn persisted the question on its way to the answer. Handing the
        # stored ID back with the reply lets the client retire the provisional
        # identity it rendered the sent bubble with.
        response.user_message_id = db.scalar(
            select(Message.id)
            .where(
                Message.conversation_id == conversation.id,
                Message.role == "user",
                Message.content == text,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        return response


def handle_clarification_resolution(
    db: Session,
    user: User,
    conversation: Conversation,
    *,
    original_request: str,
    selected_label: str,
    resolution: str,
    transition: dict[str, Any] | None = None,
    resolved_intent: dict[str, Any] | None = None,
    previous_clarification: dict[str, Any] | None = None,
    clarification_depth: int = 0,
    source_message_id: UUID,
    activity_callback: ActivityCallback | None = None,
    text_delta_callback: TextDeltaCallback | None = None,
    reasoning_delta_callback: ReasoningDeltaCallback | None = None,
) -> AgentResponse:
    """Resume the original turn with one server-validated customer choice."""
    source_message = db.scalar(
        select(Message).where(
            Message.id == source_message_id,
            Message.conversation_id == conversation.id,
            Message.role == "user",
        )
    )
    if source_message is None:
        raise ValueError("The original clarification request is unavailable")
    parsed_transition = (
        parse_clarification_transition(transition)
        if transition is not None
        else None
    )
    if isinstance(parsed_transition, CancelContinuation):
        raise ValueError("A cancel transition cannot execute a clarification")
    if isinstance(parsed_transition, LegacyPromptContinuation):
        parsed_transition = _legacy_taxonomy_continuation(
            original_request,
            parsed_transition.label,
            parsed_transition.resolution,
            previous_clarification,
        ) or parsed_transition
    intent_contract: ResolvedIntentContract | None
    if isinstance(parsed_transition, GovernedQueryContinuation):
        selected_label = parsed_transition.label
        intent_contract = parsed_transition.intent
    else:
        intent_contract = (
            ResolvedIntentContract.model_validate(resolved_intent)
            if resolved_intent is not None
            else None
        )
    taxonomy_contract = (
        parsed_transition.taxonomy
        if isinstance(parsed_transition, GovernedTaxonomyContinuation)
        else None
    )
    budget_contract = (
        parsed_transition.budget
        if isinstance(parsed_transition, GovernedBudgetContinuation)
        else None
    )
    goal_contract = (
        parsed_transition.goal
        if isinstance(parsed_transition, GovernedGoalContinuation)
        else None
    )
    if isinstance(parsed_transition, GovernedTaxonomyContinuation):
        selected_label = parsed_transition.label
    if isinstance(parsed_transition, LegacyPromptContinuation):
        selected_label = parsed_transition.label
        resolution = parsed_transition.resolution
    legacy_prompt_resume = (
        intent_contract is None
        and taxonomy_contract is None
        and budget_contract is None
        and goal_contract is None
    )
    resolved_request = original_request.strip()
    if legacy_prompt_resume:
        resolved_request = (
            f"{resolved_request}\n\n"
            f"Customer clarification (authoritative): {selected_label}. {resolution.strip()}\n"
            "Continue the original request using this clarification. Do not ask the same question again."
        )
    recent_context = _recent_complete_turn_snapshot(db, conversation)
    guard_token = None
    if legacy_prompt_resume:
        guard_token = _clarification_resume_guard.set({
            "previous": previous_clarification or {},
            "depth": clarification_depth,
        })
    try:
        with _reply_reservation(db):
            return _run_turn(
                db,
                user,
                conversation,
                resolved_request,
                activity_callback,
                text_delta_callback,
                reasoning_delta_callback,
                source_user_message=source_message,
                recent_context_override=recent_context,
                resolved_intent=intent_contract,
                resolved_taxonomy=taxonomy_contract,
                resolved_budget=budget_contract,
                resolved_goal=goal_contract,
                clarification_resume=legacy_prompt_resume,
            )
    finally:
        if guard_token is not None:
            _clarification_resume_guard.reset(guard_token)


def _run_turn(
    db: Session,
    user: User,
    conversation: Conversation,
    text: str,
    activity_callback: ActivityCallback | None = None,
    text_delta_callback: TextDeltaCallback | None = None,
    reasoning_delta_callback: ReasoningDeltaCallback | None = None,
    *,
    source_user_message: Message | None = None,
    recent_context_override: list[dict[str, Any]] | None = None,
    resolved_intent: ResolvedIntentContract | None = None,
    resolved_taxonomy: TaxonomyInterpretation | None = None,
    resolved_budget: BudgetSetupContract | None = None,
    resolved_goal: GoalAmountContract | None = None,
    clarification_resume: bool = False,
) -> AgentResponse:
    run_started = perf_counter()
    today = _local_today(user)
    selected_answer_style = answer_style(db, user.id)
    selected_presentation = build_answer_presentation(selected_answer_style)
    stage_started: dict[str, float] = {}
    stage_inputs: dict[str, Any] = {}
    stage_outputs: dict[str, Any] = {}
    retain_debug_payloads = get_settings().environment != "production"

    def debug_payload(value: Any | None) -> Any | None:
        """Return a JSON-safe copy of exact stage I/O only outside production."""
        if not retain_debug_payloads or value is None:
            return None
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))

    def emit(
        stage: str,
        label: str,
        status: ExecutionStatus | str,
        tool: str | None = None,
        detail: str | None = None,
        badge: str | None = None,
        input_payload: Any | None = None,
        output_payload: Any | None = None,
    ) -> None:
        if not activity_callback and not reasoning_delta_callback:
            return
        status = ExecutionStatus(status)
        now = perf_counter()
        if status is ExecutionStatus.RUNNING:
            stage_started[stage] = now
            duration_ms = 0.0
        else:
            duration_ms = round((now - stage_started.get(stage, now)) * 1000, 1)
        if retain_debug_payloads and input_payload is None and stage not in stage_inputs:
            # Deterministic policy/lifecycle stages may have no richer typed
            # arguments than the current turn and their stage metadata. Keep
            # that complete boundary visible instead of silently omitting an
            # Input disclosure from only those rows.
            input_payload = {
                "currentUserMessage": text,
                "stage": stage,
                "tool": tool,
            }
        if (
            retain_debug_payloads
            and status is not ExecutionStatus.RUNNING
            and output_payload is None
        ):
            output_payload = {
                "status": status.value,
                "label": label,
                "detail": detail,
                "tool": tool,
            }
        serialized_input = debug_payload(input_payload)
        serialized_output = debug_payload(output_payload)
        if serialized_input is not None:
            stage_inputs[stage] = serialized_input
        if serialized_output is not None:
            stage_outputs[stage] = serialized_output
        if activity_callback:
            activity_callback(AgentActivityEvent(
                id=stage,
                label=label,
                status=status,
                tool=tool,
                detail=detail,
                badge=badge,
                # A terminal event is a complete stage snapshot by itself.
                # Live consumers therefore do not have to correctly merge the
                # earlier running row to retain the exact input and output.
                input_payload=stage_inputs.get(stage),
                output_payload=stage_outputs.get(stage),
                duration_ms=duration_ms,
                cumulative_ms=round((now - run_started) * 1000, 1),
            ).model_dump(mode="json", by_alias=True))
        if status is not ExecutionStatus.RUNNING:
            stage_inputs.pop(stage, None)
            stage_outputs.pop(stage, None)
        # Router decision details stay in the activity trace. The AG-UI
        # reasoning channel carries only genuine provider-emitted reasoning,
        # never a templated decision summary dressed up as model thought.

    def execute(
        tool: str,
        label: str,
        operation: Callable[[], AgentResponse],
        input_payload: Any | None = None,
    ) -> AgentResponse:
        emit("execution", label, "running", tool, input_payload=input_payload)
        response = operation()
        response_payload = response.model_dump(mode="json", by_alias=True, exclude_none=True)
        emit("execution", label, "completed", tool, output_payload=response_payload)
        emit(
            "grounding",
            "Grounding response in structured state",
            "running",
            tool,
            input_payload=response_payload,
        )
        source_count = len(response.citations)
        # A reply the postconditions replaced reads as an ordinary grounded
        # answer unless the trace says otherwise — and an override no one can
        # see is how a wrong answer survives as a clean success.
        overridden = response.failure_stage == "grounding"
        emit(
            "grounding",
            "Replaced unverified prose with the authenticated result" if overridden else "Grounded response",
            "completed",
            tool,
            f"Model prose failed its postcondition ({response.error_code})" if overridden
            else f"{source_count} structured data source{'s' if source_count != 1 else ''}" if source_count else "No financial figures generated",
            output_payload={
                "message": response.message,
                "widgets": [widget.model_dump(mode="json", by_alias=True) for widget in response.widgets],
                "citations": [citation.model_dump(mode="json", by_alias=True) for citation in response.citations],
                "taskStatus": response.task_status,
            },
        )
        return response

    if source_user_message is None:
        user_message = Message(conversation_id=conversation.id, role="user", content=text, widgets=[], citations=[])
        db.add(user_message)
        if conversation.title == "Financial check-in":
            conversation.title = text[:54] + ("…" if len(text) > 54 else "")
        db.flush()
    else:
        if source_user_message.conversation_id != conversation.id or source_user_message.role != "user":
            raise ValueError("Clarification source message belongs to a different workflow")
        user_message = source_user_message
    # The answer's place in the transcript is decided here, with the question,
    # rather than whenever the model happens to finish. Two turns in flight at
    # once can then only finish out of order, not read out of order.
    reserved_reply = _reserve_reply(db, conversation)
    emit(
        "request",
        "Request received",
        "completed",
        input_payload={
            "conversationId": str(conversation.id),
            "userMessageId": str(user_message.id),
            "text": text,
        },
        output_payload={
            "accepted": True,
            "replyReserved": True,
            "assistantMessageId": str(reserved_reply.id),
        },
    )

    context_relationship = (
        resolved_intent.context_mode
        if resolved_intent is not None
        else _context_relationship(text, conversation.active_analysis_state)
    )
    intent_authority = (
        IntentAuthority.SERVER_CONTINUATION
        if (
            resolved_intent is not None
            or resolved_taxonomy is not None
            or resolved_budget is not None
            or resolved_goal is not None
        )
        else IntentAuthority.USER_TURN
    )
    turn_intent = resolve_turn_intent(
        text,
        context_relationship,
        authority=intent_authority,
        implicit_transaction_entry=(
            _is_bare_amount(text) or _is_amount_led_shorthand(text)
        ),
    )

    if resolved_budget is not None:
        decision = CopilotDecision(
            tool=capability_for_primitive("budget.manage@1"),
            confidence=1.0,
            reason="A server-authored clarification contract resolved the budget amount.",
            safe_reasoning_summary=[
                "Retained the selected category by stable ID",
                "Persist the completed budget contract without routing again",
            ],
            validated_by="clarification_continuation_policy",
            validation_confidence=1.0,
        )
        emit(
            "operator",
            "Resumed the validated budget contract",
            "completed",
            decision.tool,
            f"{resolved_budget.name} · {resolved_budget.amount_minor} minor units",
        )
        return _dispatch_decision(
            db,
            user,
            conversation,
            text,
            decision,
            execute,
            emit,
            intent_contract=turn_intent,
            budget_setup=resolved_budget,
        )

    if resolved_goal is not None:
        decision = CopilotDecision(
            tool=capability_for_primitive("planning.run@1"),
            confidence=1.0,
            reason="A server-authored clarification contract resolved the goal amount.",
            safe_reasoning_summary=[
                "Retained the goal operation and stable record identity",
                "Prepare the governed goal approval without routing again",
            ],
            validated_by="clarification_continuation_policy",
            validation_confidence=1.0,
        )
        emit(
            "operator",
            "Resumed the validated goal contract",
            "completed",
            decision.tool,
            f"{resolved_goal.operation} · {resolved_goal.amount_minor} minor units",
        )
        return _dispatch_decision(
            db,
            user,
            conversation,
            text,
            decision,
            execute,
            emit,
            intent_contract=turn_intent,
            goal_amount=resolved_goal,
        )

    if resolved_taxonomy is not None:
        decision = CopilotDecision(
            tool=capability_for_primitive("taxonomy.change@1"),
            taxonomy=resolved_taxonomy,
            confidence=1.0,
            reason="A server-authored clarification contract resolved the complete taxonomy mutation.",
            safe_reasoning_summary=[
                "Applied the selected clarification to one typed taxonomy plan",
                "Prepare the governed mutation approval without routing again",
            ],
            validated_by="clarification_continuation_policy",
            validation_confidence=1.0,
        )
        emit(
            "operator",
            "Resumed the validated taxonomy contract",
            "completed",
            decision.tool,
            (
                f"{resolved_taxonomy.name} → "
                f"{', '.join(resolved_taxonomy.subcategories)}"
            ),
        )
        db.add(AIAction(
            user_id=user.id,
            conversation_id=conversation.id,
            action_type="typed_taxonomy_continuation",
            payload_redacted={
                "tool": decision.tool.value,
                "operation": resolved_taxonomy.operation.value,
                "category": resolved_taxonomy.name,
                "subcategories": resolved_taxonomy.subcategories,
            },
            status=ExecutionStatus.COMPLETED,
        ))
        response = _dispatch_decision(
            db,
            user,
            conversation,
            text,
            decision,
            execute,
            emit,
            intent_contract=turn_intent,
        )
        return response

    if resolved_intent is not None:
        decision = CopilotDecision(
            tool=resolved_intent.capability,
            query=resolved_intent.query,
            confidence=1.0,
            reason="A server-authored clarification contract resolved every required query field.",
            safe_reasoning_summary=[
                "Applied the selected clarification to the original typed intent",
                "Execute the governed query without routing the request again",
            ],
            validated_by="clarification_continuation_policy",
            validation_confidence=1.0,
        )
        emit(
            "classification",
            "Resumed the validated intent contract",
            "completed",
            decision.tool,
            (
                f"{resolved_intent.context_mode.replace('_', ' ').title()} · "
                f"{resolved_intent.query.start_date} to {resolved_intent.query.end_date}"
            ),
        )
        db.add(AIAction(
            user_id=user.id,
            conversation_id=conversation.id,
            action_type="typed_clarification_continuation",
            payload_redacted={
                "schemaVersion": resolved_intent.schema_version,
                "contextMode": resolved_intent.context_mode,
                "tool": resolved_intent.capability.value,
                "queryShape": {
                    "metric": resolved_intent.query.metric,
                    "resultMode": resolved_intent.query.result_mode,
                    "operation": resolved_intent.query.operation,
                    "transactionType": resolved_intent.query.transaction_type,
                    "startDate": resolved_intent.query.start_date.isoformat() if resolved_intent.query.start_date else None,
                    "endDate": resolved_intent.query.end_date.isoformat() if resolved_intent.query.end_date else None,
                },
            },
            status=ExecutionStatus.COMPLETED,
        ))
        return _dispatch_decision(
            db,
            user,
            conversation,
            text,
            decision,
            execute,
            emit,
            intent_contract=turn_intent,
        )

    # A legacy clarification has already been routed and constrained by the
    # server-owned continuation. Its composite prompt must go straight to the
    # Operator: ordinary intake shortcuts can otherwise consume the confirmed
    # answer as a draft field, reopen the same ambiguity, or start an unrelated
    # workflow. Typed query and taxonomy continuations returned above through
    # their governed contracts and never rely on this model-resume lane.

    # Typed text can also answer an outstanding category question.
    active_draft = _clarification_draft(db, conversation)
    if not clarification_resume and active_draft and active_draft.missing_fields:
        emit("classification", "Resumed transaction workflow", ExecutionStatus.COMPLETED, WidgetActionId.UPDATE_TRANSACTION_DRAFT.value)
        answer = text.strip().lower()
        if active_draft.missing_fields[0] == "category":
            category = next((item for item in _expense_categories_for_user(db, user.id) if item.name.casefold() == answer.casefold()), None)
            if category:
                active_draft.category_id = category.id
                _set_ready_if_complete(active_draft)
                return execute(WidgetActionId.UPDATE_TRANSACTION_DRAFT.value, "Updating transaction draft", lambda: _draft_or_commit(db, user, conversation, active_draft))
        elif (
            active_draft.missing_fields[0] == "subcategory"
            and active_draft.category_id is not None
        ):
            subcategory = next((
                item for item in _subcategories_for_user(db, user.id, active_draft.category_id)
                if item.name.casefold() == answer.casefold()
            ), None)
            if subcategory:
                active_draft.subcategory_id = subcategory.id
                _set_ready_if_complete(active_draft)
                return execute(WidgetActionId.UPDATE_TRANSACTION_DRAFT.value, "Updating transaction draft", lambda: _draft_or_commit(db, user, conversation, active_draft))
        elif active_draft.missing_fields[0] == "source_account":
            active_draft.source_account_name = text.strip()
            _set_ready_if_complete(active_draft)
            return execute(WidgetActionId.UPDATE_TRANSACTION_DRAFT.value, "Updating transaction draft", lambda: _draft_or_commit(db, user, conversation, active_draft))
        elif active_draft.missing_fields[0] == "destination_account":
            active_draft.destination_account_name = text.strip()
            _set_ready_if_complete(active_draft)
            return execute(WidgetActionId.UPDATE_TRANSACTION_DRAFT.value, "Updating transaction draft", lambda: _draft_or_commit(db, user, conversation, active_draft))
        elif active_draft.missing_fields[0] == "amount" and _is_bare_amount(text):
            amount_minor = parse_amount_minor(text)
            if amount_minor:
                active_draft.amount_minor = amount_minor
                _set_ready_if_complete(active_draft)
                return execute(WidgetActionId.UPDATE_TRANSACTION_DRAFT.value, "Updating transaction draft", lambda: _draft_or_commit(db, user, conversation, active_draft))

    requested_thread_title = (
        None if clarification_resume else _conversation_rename_request(text)
    )
    rename_intent_without_title = (
        not clarification_resume
        and requested_thread_title is None
        and bool(_RENAME_TITLE_REQUEST.search(text))
    )
    if (
        not clarification_resume
        and requested_thread_title is None
        and not rename_intent_without_title
        and _awaiting_rename_title(db, conversation)
        and not re.fullmatch(r"(?:cancel|no|nope|never\s?mind|stop|forget it)[.! ]*", text.strip(), re.I)
    ):
        candidate = " ".join(text.split()).strip(" .\"“”'‘’")
        if candidate and len(candidate) <= CONVERSATION_TITLE_MAX:
            requested_thread_title = candidate
    if not clarification_resume and requested_thread_title is not None:
        confirmed_title = requested_thread_title
        return execute(
            WidgetActionId.RENAME_CONVERSATION.value,
            "Preparing a thread rename confirmation",
            lambda: _conversation_rename_confirmation(db, conversation, confirmed_title),
            {"userMessage": text, "requestedTitle": confirmed_title},
        )
    if not clarification_resume and rename_intent_without_title:
        return execute(
            WidgetActionId.RENAME_CONVERSATION.value,
            "Asking for the new thread title",
            lambda: _conversation_response(db, conversation, RENAME_TITLE_QUESTION),
            {"userMessage": text},
        )

    recent_context = (
        recent_context_override
        if recent_context_override is not None
        else _recent_complete_turn_context(
            db,
            conversation,
            user_message,
            limit=(
                STANDALONE_RECENT_CONTEXT_TURN_LIMIT
                if context_relationship is ContextRelationship.STANDALONE
                else RECENT_CONTEXT_TURN_LIMIT
            ),
        )
    )
    replay_decision: CopilotDecision | None = None
    analysis_settings = get_settings()
    sql_only_analysis = (
        analysis_settings.primary_agent_enabled
        and analysis_settings.sql_lane_enabled
        and getattr(analysis_settings, "analysis_query_mode", "hybrid") == "sql"
    )
    # Grammar-template replay used to return a structurally valid but
    # semantically incomplete comparison before the Operator could author the
    # SQL the question required. SQL mode deliberately bypasses that legacy
    # short circuit; the SQL lane maintains its own value-free example memory.
    analysis_replay = (
        None
        if sql_only_analysis or clarification_resume
        else bind_repeat_analysis(db, user.id, text, today)
    )
    if analysis_replay is not None:
        emit(
            "retrieval",
            "Matched this user's validated template for the identical question",
            "completed",
            "template_replay",
            f"Source run {analysis_replay.source_run_id}; dates were rebound from the central finance-time policy.",
            "Reused",
            input_payload={"question": text, "userId": str(user.id)},
            output_payload={
                "templateId": str(analysis_replay.template_id),
                "sourceRunId": str(analysis_replay.source_run_id),
                "disposition": analysis_replay.disposition.value,
            },
        )
        replay_decision = CopilotDecision(
            tool="run_analysis_harness",
            analysis_tool=analysis_replay.proposal,
            candidate_template_id=analysis_replay.template_id,
            confidence=1.0,
            reason="Deterministic replay of this user's stored validated analysis template for an identical question.",
            validated_by="template_replay_policy",
            validation_confidence=1.0,
        )
        db.add(AIAction(
            user_id=user.id,
            conversation_id=conversation.id,
            action_type="analysis_template_replay",
            payload_redacted={
                "templateId": str(analysis_replay.template_id),
                "sourceRunId": str(analysis_replay.source_run_id),
                "disposition": analysis_replay.disposition.value,
            },
            status=ExecutionStatus.COMPLETED,
        ))
        if analysis_replay.disposition is ReplayDisposition.FINAL:
            emit(
                "validator",
                "Verified answer-complete template replay",
                "completed",
                "template_replay_policy",
                "The exact question is a self-contained descriptive read, and the rebound plan passed scope, structure, manifest, and date checks.",
                "Validated",
            )
            return _dispatch_decision(
                db,
                user,
                conversation,
                text,
                replay_decision,
                execute,
                emit,
                intent_contract=turn_intent,
            )
        emit(
            "validator",
            "Reserved replay for question-aware composition",
            "completed",
            "template_replay_composition_policy",
            "The cached computation is valid, but the request requires interpretation; the Operator must compose the final answer from replayed evidence.",
            "Compose",
        )
    normalized_operation_request = " ".join(text.casefold().split())
    exact_operation = (
        None
        if clarification_resume
        else next((
            operation
            for operation in operation_catalog().snapshot().operations.values()
            if operation.definition.routing.strategy in {"decision", "managed"}
            and normalized_operation_request in {
                " ".join(value.casefold().split())
                for value in [
                    *operation.definition.discovery.aliases,
                    *operation.definition.discovery.examples,
                ]
            }
        ), None)
    )
    if exact_operation is not None:
        matching_contract = next((
            case
            for case in exact_operation.definition.tests
            if " ".join(case.request.casefold().split()) == normalized_operation_request
        ), None)
        exact_decision = filesystem_operation_decision(
            exact_operation.id,
            matching_contract.expected_inputs if matching_contract and matching_contract.expected_inputs else {},
            confidence=1.0,
            reason="Exact filesystem operation example or alias match.",
        )
        if exact_decision is not None:
            return _dispatch_decision(
                db,
                user,
                conversation,
                text,
                exact_decision,
                execute,
                emit,
                intent_contract=turn_intent,
            )
    if not clarification_resume and _QUESTION_IDEAS_REQUEST.search(text):
        # An explicit ask for question ideas is answered with the tappable
        # suggestion chips themselves, never a prose list nobody can tap.
        # Deterministic routing, model-generated content; anything vaguer
        # (or a generation failure) falls through to the Operator.
        try:
            ideas = suggest_related_questions(
                text,
                "",
                recent_context,
                capability_notes(),
                today,
                user.timezone,
            )
        except Exception:
            ideas = []
        if ideas:
            ideas_widget = Widget(
                id=f"related-questions-{uuid4()}",
                type=WidgetType.RELATED_QUESTIONS,
                data={"questions": ideas},
            )
            return execute(
                "related_questions",
                "Preparing suggested questions",
                lambda: persist_agent_response(
                    db,
                    conversation,
                    "Here are a few things you could ask — tap one to run it.",
                    widgets=[ideas_widget],
                ),
                {"userMessage": text},
            )

    if (
        context_relationship is ContextRelationship.STANDALONE
        and _recent_assistant_expects_value(recent_context)
    ):
        context_relationship = ContextRelationship.FOLLOW_UP
        if recent_context_override is None:
            recent_context = _recent_complete_turn_context(
                db,
                conversation,
                user_message,
                limit=RECENT_CONTEXT_TURN_LIMIT,
            )
        turn_intent = resolve_turn_intent(
            text,
            context_relationship,
            authority=intent_authority,
            implicit_transaction_entry=False,
        )
    fast_path = None if clarification_resume else _fast_path_decision(
        text,
        today,
        user.currency,
        context_relationship=context_relationship,
    )
    normalized_current = " ".join(text.casefold().split())
    repeated_assistant_text = next(
        (
            item["content"]
            for item in reversed(recent_context)
            if item["role"] == "assistant"
            and (
                " ".join(item["content"].casefold().split()) == normalized_current
                or (
                    len(item["content"].strip()) >= 20
                    and " ".join(item["content"].casefold().split()) in normalized_current
                )
            )
        ),
        None,
    )
    if (
        not clarification_resume
        and fast_path is None
        and repeated_assistant_text
    ):
        fast_path = (
            CopilotDecision(
                tool=capability_for_primitive("agent.respond@1"),
                reply="It looks like the user repeated the assistant's previous message. Recognize that context and respond without echoing it again.",
                confidence=1,
                reason="The current message exactly repeats the prior assistant turn.",
                safe_reasoning_summary=["Recognized a repeated prior reply", "Avoid echoing the same answer"],
            ),
            None,
        )
    guarded_fast_path = fast_path
    guarded_extraction = extract_transaction(
        text,
        today=today,
        default_currency=user.currency,
    )
    guarded_mutation_intent = bool(
        guarded_extraction.amount_minor is not None
        and guarded_extraction.transaction_type != TransactionType.UNKNOWN
        and has_explicit_transaction_mutation_cue(text)
    )
    settings = get_settings()
    deep_reasoning = _needs_deep_reasoning(text)

    calculator_clarification = (
        None
        if clarification_resume
        else _active_loan_chart_clarification(
            text,
            conversation.active_analysis_state,
            user.currency,
        )
    )
    if calculator_clarification:
        emit(
            "classification",
            "Detected conflicting calculator assumptions",
            "completed",
            capability_for_primitive("agent.clarify@1").value,
            calculator_clarification.reason,
        )
        clarification = calculator_clarification.clarification
        if clarification is None:
            raise RuntimeError("Calculator conflict policy produced no clarification")
        db.add(AIAction(
            user_id=user.id,
            conversation_id=conversation.id,
            action_type="calculator_conflict_policy",
            payload_redacted={
                "tool": capability_for_primitive("agent.clarify@1").value,
                "conflictFields": clarification.conflict_fields,
            },
            status=ExecutionStatus.COMPLETED,
        ))
        return _dispatch_decision(
            db,
            user,
            conversation,
            text,
            calculator_clarification,
            execute,
            emit,
            intent_contract=turn_intent,
        )

    # The legacy grammar remains available only in explicitly selected hybrid
    # mode. SQL mode must never be short-circuited into a finite plan just
    # because the request resembles a historical pattern.
    if (
        not clarification_resume
        and not sql_only_analysis
        and analysis_replay is None
        and _is_known_expense_pattern_analysis(text)
    ):
        compiled_pattern = _compile_known_analysis(db, user, text)
        if compiled_pattern:
            emit(
                "classification",
                "Selected the governed expense-pattern analysis",
                "completed",
                compiled_pattern.tool,
                " → ".join(compiled_pattern.safe_reasoning_summary),
            )
            db.add(AIAction(
                user_id=user.id,
                conversation_id=conversation.id,
                action_type="known_analysis_policy",
                payload_redacted={"tool": compiled_pattern.tool, "intent": "expense_pattern_savings"},
                status=ExecutionStatus.COMPLETED,
            ))
            return _dispatch_decision(
                db,
                user,
                conversation,
                text,
                compiled_pattern,
                execute,
                emit,
                intent_contract=turn_intent,
            )

    if (
        fast_path
        and settings.primary_agent_enabled
        and settings.openai_api_key
        and not (
            capability_invokes(fast_path[0].tool, "transaction.record@1")
            or capability_invokes(fast_path[0].tool, "agent.clarify@1")
            or capability_invokes(fast_path[0].tool, "planning.run@1")
            or capability_invokes(fast_path[0].tool, "budget.manage@1")
        )
    ):
        # In Operator mode one contextual agent owns ordinary conversation and
        # safe reads (the gate no longer proposes small talk at all). A
        # deterministic transaction contract remains authoritative: routing it
        # again only repeats already validated extraction and taxonomy work.
        fast_path = None
    if fast_path:
        decision, extracted = fast_path
        emit(
            "classification",
            "Fast intent gate selected a validated path",
            "completed",
            decision.tool,
            " → ".join(decision.safe_reasoning_summary),
            input_payload={"text": text, "currency": user.currency},
            output_payload={
                "decision": decision.model_dump(mode="json", by_alias=True, exclude_none=True),
                "extractedTransaction": vars(extracted) if extracted is not None else None,
            },
        )
        db.add(AIAction(
            user_id=user.id,
            conversation_id=conversation.id,
            action_type="deterministic_gate",
            payload_redacted={"tool": decision.tool, "confidence": decision.confidence},
            status=ExecutionStatus.COMPLETED,
        ))
        return _dispatch_decision(
            db,
            user,
            conversation,
            text,
            decision,
            execute,
            emit,
            extracted=extracted,
            intent_contract=turn_intent,
        )

    # The single agent loop owns every financial ask from here: retrieved pool
    # templates and the open plan author are mounted as tools on the Operator
    # turn, so simple and complex analyses alike are one agentic run.
    operator_decision: CopilotDecision | None = None
    operator_rejection_code: str | None = None
    if (
        settings.primary_agent_enabled
        and settings.openai_api_key
        and (
            not _is_bare_amount(text)
            or context_relationship is not ContextRelationship.STANDALONE
        )
    ):
        conversation_only = _is_social_conversation_only(text)
        no_record_explanation = _requests_no_record_explanation(
            text,
            context_relationship,
            turn_intent,
        )
        tool_free_model_turn = conversation_only or no_record_explanation
        calculator_only = _is_self_contained_calculator_request(
            text,
            context_relationship,
        )
        runtime_tools = [] if tool_free_model_turn else _user_runtime_tools(db, user, today)
        if calculator_only:
            runtime_tools = [
                tool
                for tool in runtime_tools
                if getattr(tool, "name", None) == FINANCIAL_CALCULATOR_TOOL_NAME
            ]
        analysis_context = AnalysisToolContext(
            db=db,
            user_id=user.id,
            conversation_id=conversation.id,
            today=today,
            timezone_name=user.timezone,
            question=text,
            currency=user.currency,
        )
        analysis_tools = (
            build_analysis_tools(
                analysis_context,
                exact_replay=analysis_replay
                if analysis_replay is not None
                and analysis_replay.disposition is ReplayDisposition.COMPOSE
                else None,
            )
            if not tool_free_model_turn
            and not calculator_only
            and (looks_like_financial_query(text) or deep_reasoning)
            else []
        )
        prompt_analysis_state = (
            conversation.active_analysis_state
            if context_relationship in {ContextRelationship.FOLLOW_UP, ContextRelationship.CORRECTION} and not releases_prior_scope(text)
            else None
        )
        prompt_data_scope = (
            conversation.active_data_scope
            if _references_active_data_scope(text) and not releases_prior_scope(text)
            else None
        )
        workflow_context: dict = {
            "kind": (
                "conversation_only"
                if conversation_only
                else "knowledge_only"
                if no_record_explanation
                else "calculator_scenario"
                if calculator_only
                else "none"
            ),
            "activeDataScope": prompt_data_scope,
            "activeAnalysisState": prompt_analysis_state,
            "contextRelationship": context_relationship.value,
            "intentContract": turn_intent.model_dump(mode="json"),
            "correctionRequested": _is_correction_followup(text),
            "defaultCurrency": user.currency,
        }
        latest_contextual_assistant = next((
            item for item in reversed(recent_context)
            if item.get("role") == "assistant"
        ), None)
        transaction_surface_count = sum(
            1
            for surface in (
                latest_contextual_assistant.get("responseSurfaces", [])
                if latest_contextual_assistant
                else []
            )
            if surface.get("type") == WidgetType.TRANSACTION_PREVIEW
        )
        if transaction_surface_count:
            # This is a capability hint, not financial context: the model gets
            # no row ID or value. The runtime later binds the server-issued
            # card identity if and only if the edit proposal chooses it.
            workflow_context.update({
                "kind": "saved_transaction_card",
                "transactionCardCount": transaction_surface_count,
            })
        if active_draft:
            workflow_context.update({
                "kind": "transaction_draft",
                "draftId": str(active_draft.id),
                "state": active_draft.state,
                "missingFields": active_draft.missing_fields,
            })
        # Deterministic traits, refreshed on demand. The key is present only
        # when traits exist — an empty line would read as "no income, no
        # baselines" rather than "not computed" — and every value carries its
        # own computed_at, so the Operator can never quote a stale number as
        # current.
        if not tool_free_model_turn and not calculator_only:
            user_traits = get_traits(db, user, today=today)
            if user_traits:
                workflow_context["userTraits"] = traits_context_line(user_traits)
        # Insights beside the traits, under the same law: every claim here was
        # replayed from its own recompute key during this turn, and each one
        # prints the moment it was computed and the moment it last verified. A
        # claim that no longer reproduces is stale and never reaches the key.
        if not tool_free_model_turn and not calculator_only:
            verified_insights = current_insights(db, user, today)
            if verified_insights:
                workflow_context["verifiedInsights"] = insights_context_line(verified_insights)

        # Complex escalation is one optional tool inside the universal
        # Operator turn, never a model/router pass in front of it. The delegate
        # receives the complete read-only tool set (including bounded Python),
        # while the ordinary Operator swaps Python for the smaller delegation
        # capability so common reads stay at the eight-tool ceiling.
        if analysis_tools:
            delegate_tool = build_analysis_delegate_tool(
                text,
                today,
                user.timezone,
                recent_context,
                user_id=user.id,
                read_tools=[*runtime_tools, *analysis_tools],
                presentation=selected_presentation,
            )
            if delegate_tool is not None:
                analysis_tools = [
                    tool
                    for tool in analysis_tools
                    if getattr(tool, "name", None) != PYTHON_TOOL_NAME
                ]
                analysis_tools.append(delegate_tool)

        emitted_direct_deltas: list[str] = []

        def direct_delta(delta: str) -> None:
            reserved = _reserved_reply.get()
            if not delta or reserved is None:
                return
            emitted_direct_deltas.append(delta)
            if text_delta_callback:
                text_delta_callback(reserved.id, delta)

        # One "operator" stage tracks the Operator model pass across every exit
        # of this block: direct answer, operation proposal, guarded reroute, and
        # provider failure all close the same stage id they opened.
        emit(
            "operator",
            "The Operator is reading the conversation and available tools",
            "running",
            "operator",
            input_payload={
                "currentUserMessage": text,
                "recentContext": recent_context,
                "workflowContext": workflow_context,
                "taxonomyAvailableViaTool": True,
                "runtimeTools": [getattr(tool, "name", type(tool).__name__) for tool in runtime_tools],
                "model": settings.operator_model,
                "answerStyle": selected_answer_style.value,
                "answerPresentation": selected_presentation.trace_values(),
                "currentDate": today,
                "timezone": user.timezone,
            },
        )
        try:
            direct_result = run_operator(
                text,
                [],
                today,
                user.timezone,
                recent_context,
                workflow_context=workflow_context,
                model_id=settings.operator_model,
                user_id=user.id,
                runtime_tools=runtime_tools,
                analysis_tools=analysis_tools,
                answer_style=selected_answer_style,
                presentation=selected_presentation,
                on_delta=direct_delta if text_delta_callback else None,
                on_reasoning_delta=reasoning_delta_callback,
                # Financial/tool answers remain buffered until their numeric
                # evidence postcondition passes. Ordinary conversation can use
                # the provider's exact deltas immediately.
                # A reply the post-hoc verifiers might replace must be buffered,
                # never streamed: the gate reuses the exact trigger predicates
                # of those verifiers so the two can never disagree.
                allow_live_deltas=bool(
                    text_delta_callback
                    and not looks_like_financial_query(text)
                    and context_relationship is ContextRelationship.STANDALONE
                    and not _SUBCATEGORY_ENUMERATION_REQUEST.search(text)
                ),
            )
        except Exception as error:
            if emitted_direct_deltas:
                emit(
                    "operator",
                    "The Operator response stream failed",
                    "failed",
                    "operator",
                    type(error).__name__,
                    output_payload={"errorType": type(error).__name__, "message": str(error)},
                )
                raise
            db.add(AIAction(
                user_id=user.id,
                conversation_id=conversation.id,
                action_type="operator",
                payload_redacted={"errorType": type(error).__name__},
                status=ExecutionStatus.FAILED,
            ))
            emit(
                "operator",
                "The Operator was unavailable, continuing with the governed pipeline",
                "completed",
                "operator",
                type(error).__name__,
            )
            direct_result = None

        if direct_result and direct_result.operation:
            if emitted_direct_deltas:
                raise RuntimeError("Operator emitted text before an operation proposal")
            proposal = direct_result.operation
            proposal_reason = "Selected a strictly typed filesystem operation proposal."
            try:
                selected_operation = resolve_current_operation(
                    operation_catalog(),
                    proposal.operation_id,
                    proposal.version,
                    proposal.checksum,
                )
            except (OperationChangedError, OperationInputError):
                selected_operation = None
            operator_decision = None
            if selected_operation is not None:
                operator_decision = filesystem_operation_decision(
                    proposal.operation_id,
                    proposal.inputs,
                    confidence=1.0,
                    reason=proposal_reason,
                    expected_version=proposal.version,
                    expected_checksum=proposal.checksum,
                )
                if operator_decision is not None and operator_decision.query is not None:
                    query, reconciled = _reconcile_correction_query(
                        text,
                        operator_decision.query,
                        recent_context,
                    )
                    query = _release_unreferenced_prior_filters(
                        text,
                        query,
                        conversation.active_analysis_state,
                    )
                    assumptions = list(operator_decision.assumptions)
                    if reconciled:
                        assumptions.append(CompilationAssumption(
                            code="correction_scope_reconciled",
                            detail=(
                                "I reconciled this correction with the matching prior all-time filters."
                                if query.start_date is None or query.end_date is None
                                else (
                                    "I reconciled this correction with the matching prior period, "
                                    f"{query.start_date.isoformat()} through {query.end_date.isoformat()}."
                                )
                            ),
                        ))
                    operator_decision = operator_decision.model_copy(update={
                        "query": query,
                        "assumptions": assumptions,
                    })
            if operator_decision is None:
                # Reached for a stale revision OR a proposal whose inputs failed
                # the typed contract — say so honestly instead of blaming the
                # catalog for both.
                operator_decision = CopilotDecision(
                    tool=capability_for_primitive("agent.unknown@1"),
                    reply=(
                        "I couldn’t bind that request to a governed operation, so nothing "
                        "was changed. Please restate the records or summary you want."
                    ),
                    confidence=1.0,
                    reason="The proposed operation could not be bound to an active governed revision.",
                )
            emit(
                "operation_compilation",
                "Compiled the strict filesystem operation proposal",
                "completed",
                proposal.operation_id,
                (
                    selected_operation.definition.routing.strategy
                    if selected_operation is not None
                    else "unavailable"
                ),
                input_payload={
                    "operationId": proposal.operation_id,
                    "operationVersion": proposal.version,
                    "operationChecksum": proposal.checksum,
                    "inputs": proposal.inputs,
                },
                output_payload=(
                    operator_decision.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    )
                    if operator_decision is not None
                    else {"decision": None}
                ),
            )
            emit(
                "operator",
                "The Operator selected a typed filesystem operation",
                "completed",
                proposal.operation_id,
                proposal_reason,
                output_payload={
                    "operationId": proposal.operation_id,
                    "operationVersion": proposal.version,
                    "operationChecksum": proposal.checksum,
                    "inputs": proposal.inputs,
                },
            )
            proposal_audit: dict[str, Any] = {
                "operationId": proposal.operation_id,
                "operationVersion": proposal.version,
                "inputFields": sorted(proposal.inputs),
                "strategy": (
                    selected_operation.definition.routing.strategy
                    if selected_operation is not None
                    else "unavailable"
                ),
            }
            if operator_decision is not None and operator_decision.query is not None:
                proposal_audit["queryShape"] = {
                    "metric": operator_decision.query.metric,
                    "resultMode": operator_decision.query.result_mode,
                    "operation": operator_decision.query.operation,
                    "groupBy": operator_decision.query.group_by,
                    "usesActiveScope": operator_decision.query.use_active_scope,
                }
            db.add(AIAction(
                user_id=user.id,
                conversation_id=conversation.id,
                action_type="operator_operation_proposal",
                payload_redacted=proposal_audit,
                status=ExecutionStatus.COMPLETED,
            ))

        if direct_result and direct_result.reply:
            # Grounded prose is not checked here. It travels into the typed
            # decision below and faces the postconditions once, at the reply
            # boundary, which is also where a replacement is recorded.
            direct_reply = direct_result.reply.strip()
            expects_missing_value = bool(
                not direct_result.tool_grounding
                and expects_value_answer(direct_reply)
                and (
                    looks_like_financial_query(text)
                    or _looks_like_planning_command(text)
                    or deep_reasoning
                )
            )
            if expects_missing_value:
                emit(
                    "continuation_policy",
                    "Converted a plain value request into a durable continuation",
                    ExecutionStatus.COMPLETED,
                    capability_for_primitive("agent.clarify@1").value,
                    "A short numeric reply must retain the operation that requested it.",
                )
                clarification_decision = CopilotDecision(
                    tool=capability_for_primitive("agent.clarify@1"),
                    clarification=ClarificationRequest(
                        question=direct_reply,
                        reason="This value is required to continue the current financial request safely.",
                        conflict_fields=["requested_value"],
                        options=[],
                        allow_custom=True,
                        custom_label="Enter the requested value",
                    ),
                    confidence=1.0,
                    reason="A model-authored value request requires a durable continuation.",
                    validated_by="continuation_postcondition",
                    validation_confidence=1.0,
                )
                return _dispatch_decision(
                    db,
                    user,
                    conversation,
                    text,
                    clarification_decision,
                    execute,
                    emit,
                    intent_contract=turn_intent,
                )
            direct_validation_mode = answer_validation_mode(db, user.id)
            contextual_financial_read = bool(
                prompt_analysis_state
                and context_relationship
                in {ContextRelationship.FOLLOW_UP, ContextRelationship.CORRECTION}
            )
            inherited_citations = (
                _prior_grounding_citations(
                    db,
                    conversation,
                    prompt_analysis_state,
                    direct_reply,
                )
                if contextual_financial_read and not direct_result.tool_grounding
                else []
            )
            ungrounded_financial_claim = bool(
                direct_validation_mode is not AnswerValidationMode.OFF
                and not direct_result.tool_grounding
                and not inherited_citations
                and (looks_like_financial_query(text) or contextual_financial_read)
                and contains_financial_claim(direct_reply)
            )
            # A model claiming an app-settings mutation it has no tool for
            # ("Renamed this thread to X") is a fabricated success — the same
            # violation as a fabricated financial write, so it takes the same
            # guarded reroute regardless of what the fast path thought.
            claims_settings_mutation = bool(
                not direct_result.tool_grounding
                and re.search(
                    r"\brenamed\s+(?:this|the|your)\s+(?:thread|chat|conversation|title)\b"
                    r"|\b(?:renamed|updated|changed|set)\s+(?:the\s+|this\s+|your\s+)?(?:page\s+|thread\s+|chat\s+|conversation\s+)?title\b",
                    direct_reply,
                    re.I,
                )
            )
            direct_mutation_claim = claims_settings_mutation or bool(
                not direct_result.tool_grounding
                and re.search(
                    r"\b(?:added|created|deleted|removed|recorded|saved|updated|changed|set)\b",
                    direct_reply,
                    re.I,
                )
                and (
                    active_draft is not None
                    or _looks_like_planning_command(text)
                    or (
                        guarded_fast_path is not None
                        and guarded_fast_path[0].tool
                        not in safe_read_capabilities()
                        | {capability_for_primitive("agent.respond@1")}
                    )
                )
            )
            missed_required_handoff = bool(
                _looks_like_planning_command(text)
                or guarded_mutation_intent
                or (
                    guarded_fast_path is not None
                    and guarded_fast_path[0].tool
                    not in safe_read_capabilities()
                    | {capability_for_primitive("agent.respond@1")}
                )
            )
            if (
                not ungrounded_financial_claim
                and not direct_mutation_claim
                and not missed_required_handoff
            ):
                emit(
                    "operator",
                    "The Operator completed the answer",
                    "completed",
                    "operator",
                    (
                        f"{', '.join(item.name for item in direct_result.tool_grounding)} · Answer style: {selected_answer_style.value}"
                        if direct_result.tool_grounding
                        else f"Used recent complete turns · Answer style: {selected_answer_style.value}"
                    ),
                    output_payload=direct_result.model_dump(mode="json", by_alias=True, exclude_none=True),
                )
                validation_skipped = bool(
                    direct_validation_mode is AnswerValidationMode.OFF
                    and not direct_result.tool_grounding
                    and looks_like_financial_query(text)
                    and contains_financial_claim(direct_reply)
                )
                if validation_skipped:
                    emit(
                        "answer_validation",
                        "Answer validation: off",
                        ExecutionStatus.COMPLETED,
                        "answer_validation",
                        "Skipped grounding, evidence, and requested-answer checks for this read answer. Tenant policy, query admission, mutation safeguards, and required operation handoffs remained active.",
                    )
                    db.add(AIAction(
                        user_id=user.id,
                        conversation_id=conversation.id,
                        action_type="answer_validation",
                        payload_redacted={
                            "mode": direct_validation_mode.value,
                            "skipped": True,
                            "ungroundedReadAnswer": True,
                        },
                        status=ExecutionStatus.COMPLETED,
                    ))
                if inherited_citations:
                    emit(
                        "answer_validation",
                        "Validated facts against the prior grounded answer",
                        ExecutionStatus.COMPLETED,
                        "answer_lineage_validation",
                        "Reused source provenance after verifying that the follow-up introduced no new numeric values.",
                    )
                    db.add(AIAction(
                        user_id=user.id,
                        conversation_id=conversation.id,
                        action_type="answer_lineage_validation",
                        payload_redacted={
                            "sourceMessageId": (prompt_analysis_state or {}).get("sourceMessageId"),
                            "citationCount": len(inherited_citations),
                            "numericSubsetVerified": True,
                        },
                        status=ExecutionStatus.COMPLETED,
                    ))
                db.add(AIAction(
                    user_id=user.id,
                    conversation_id=conversation.id,
                    action_type="operator",
                    payload_redacted={
                        "groundedTools": [item.name for item in direct_result.tool_grounding],
                        "streamedLive": direct_result.streamed_live,
                        "answerStyle": selected_answer_style.value,
                        "answerPresentation": selected_presentation.trace_values(),
                    },
                    status=ExecutionStatus.COMPLETED,
                ))
                direct_decision = CopilotDecision(
                    tool=capability_for_primitive("agent.respond@1"),
                    reply=direct_reply,
                    confidence=1.0,
                    reason="One end-to-end conversational/read run completed safely.",
                    tool_grounding=direct_result.tool_grounding,
                    validated_by=(
                        "runtime_tool_policy"
                        if direct_result.tool_grounding
                        else (
                            "answer_validation_off"
                            if validation_skipped
                            else (
                                "prior_grounding_lineage"
                                if inherited_citations
                                else "operator_conversation_policy"
                            )
                        )
                    ),
                    validation_confidence=1.0,
                )
                response = _dispatch_decision(
                    db,
                    user,
                    conversation,
                    text,
                    direct_decision,
                    execute,
                    emit,
                    intent_contract=turn_intent,
                    inherited_citations=inherited_citations,
                )
                if text_delta_callback and not direct_result.streamed_live:
                    text_delta_callback(response.message_id, response.message)
                return response
            if emitted_direct_deltas:
                # Once provider text crossed the protocol boundary, silently
                # replacing it through the governed pipeline would create two
                # answers under one message id. Fail the run so replay and the
                # canonical transcript cannot disagree with the live reader.
                raise RuntimeError("A streamed Operator answer failed its authority postcondition")
            operator_rejection_code = next(
                reason
                for reason in (
                    "ungrounded_financial_claim" if ungrounded_financial_claim else None,
                    "unauthorized_mutation_claim" if direct_mutation_claim else None,
                    "required_operation_handoff_missing" if missed_required_handoff else None,
                )
                if reason is not None
            )
            emit(
                "operator",
                "Rejected a direct answer that failed its authority or answer contract",
                "completed",
                "deterministic_evidence_policy",
                operator_rejection_code,
                output_payload={
                    "status": "rejected",
                    "reason": operator_rejection_code,
                    "groundedTools": [item.name for item in direct_result.tool_grounding],
                },
            )

    superseded_scope = conversation.active_data_scope if releases_prior_scope(text) else None
    final_decision = operator_decision
    if final_decision is not None:
        # Strict proposal validation, tenancy scoping, and — for mutations —
        # the HITL approval widget govern typed operations; no model critic
        # reviews a decision the machine can check.
        final_decision = _normalize_compound_taxonomy_decision(text, final_decision)
        final_decision = _normalize_operation_decision(text, final_decision, conversation, emit)
        if not final_decision.validated_by:
            final_decision = final_decision.model_copy(update={
                "validated_by": "deterministic_contract_policy",
                "validation_confidence": 1.0,
            })
        emit(
            "validator",
            "Verified the typed operation contract",
            "completed",
            "deterministic_contract_policy",
            "Typed proposal validation, tenant scoping, and HITL approval govern this operation.",
        )
    if final_decision and superseded_scope:
        # Widening away from the records on screen is a change the user should
        # read in the answer, not infer from a different-looking chart.
        final_decision = final_decision.model_copy(update={"assumptions": [
            *final_decision.assumptions,
            CompilationAssumption(
                code="scope_released",
                detail=(
                    f"This reads your full history, not just the {superseded_scope.get('entityCount', 0)} "
                    "records shown earlier."
                ),
            ),
        ]})
    model_tool = final_decision.tool if final_decision else None
    override_detail = None
    if (
        _is_bare_amount(text)
        and context_relationship is ContextRelationship.STANDALONE
        and (
            not final_decision
            or capability_invokes(final_decision.tool, "agent.respond@1")
            or capability_invokes(final_decision.tool, "agent.unknown@1")
        )
    ):
        final_decision = CopilotDecision(
            tool=capability_for_primitive("transaction.record@1"),
            confidence=max(final_decision.confidence if final_decision else 0.0, 0.7),
            reason="Bare currency amounts enter the minimal transaction clarification workflow.",
        )
        override_detail = (
            f"Domain guardrail corrected {model_tool or 'no route'} → "
            f"{capability_for_primitive('transaction.record@1').value}"
        )
    if final_decision:
        payload = {"tool": final_decision.tool, "confidence": final_decision.confidence}
        if final_decision.query:
            payload["queryShape"] = {
                "metric": final_decision.query.metric,
                "resultMode": final_decision.query.result_mode,
                "operation": final_decision.query.operation,
                "groupBy": final_decision.query.group_by,
                "sortDirection": final_decision.query.sort_direction,
                "limit": final_decision.query.limit,
                "usesActiveScope": final_decision.query.use_active_scope,
                "filterFields": [key for key, value in {
                    "transactionType": final_decision.query.transaction_type,
                    "merchant": final_decision.query.merchant,
                    "category": final_decision.query.category_slug,
                    "subcategory": final_decision.query.subcategory_slug,
                    "account": final_decision.query.account,
                    "tag": final_decision.query.tag,
                    "minimumAmount": final_decision.query.min_amount_minor,
                    "maximumAmount": final_decision.query.max_amount_minor,
                    "startDate": final_decision.query.start_date,
                    "endDate": final_decision.query.end_date,
                }.items() if value is not None],
            }
        if final_decision.validated_by:
            payload.update({"validatedBy": final_decision.validated_by, "validationConfidence": final_decision.validation_confidence})
        if final_decision.tool_grounding:
            payload["groundedTools"] = [item.name for item in final_decision.tool_grounding]
        if model_tool and model_tool != final_decision.tool:
            payload["modelTool"] = model_tool
        db.add(AIAction(user_id=user.id, conversation_id=conversation.id, action_type="operator_decision", payload_redacted=payload, status=ExecutionStatus.COMPLETED))
    # Model self-reported confidence is not a measurement; the raw values stay
    # in the ai_actions decision log for diagnostics but are never rendered as
    # a user-facing percentage.
    reasoning_detail = None
    if final_decision and final_decision.safe_reasoning_summary:
        reasoning_detail = " → ".join(final_decision.safe_reasoning_summary)
        if final_decision.validated_by:
            reasoning_detail += f" → Validated by {final_decision.validated_by}"
    if (
        final_decision is None
        and sql_only_analysis
        and looks_like_financial_query(text)
    ):
        # A failed SQL-authored answer must fail closed. Falling through to the
        # old phrase compiler answers a smaller, different question and makes
        # an evidence rejection appear successful to the reader.
        failure_code = operator_rejection_code or "sql_operator_unavailable"
        emit(
            "classification",
            "SQL analysis ended without verified evidence",
            "completed",
            "sql_analysis_policy",
            failure_code,
            input_payload={"currentUserMessage": text},
            output_payload={"status": "failed", "reason": failure_code},
        )
        failed_sql_decision = CopilotDecision(
            tool=capability_for_primitive("agent.unknown@1"),
            reply=(
                "I couldn’t validate a complete SQL answer for this request, so I didn’t "
                "substitute a simpler analysis that would answer a different question. "
                "Please try again."
            ),
            confidence=1.0,
            reason="SQL-only analysis failed closed instead of invoking the legacy analysis harness.",
            task_status="failed",
            failure_stage="grounding" if operator_rejection_code else "analysis",
            error_code=failure_code,
        )
        return _dispatch_decision(
            db,
            user,
            conversation,
            text,
            failed_sql_decision,
            execute,
            emit,
            intent_contract=turn_intent,
        )
    emit(
        "classification",
        ("The governed pipeline completed its reasoning plan" if deep_reasoning else "The governed pipeline selected a capability") if final_decision else "Deterministic fallback selected",
        "completed",
        final_decision.tool if final_decision else "deterministic_fallback",
        override_detail or reasoning_detail or (None if final_decision else "The model was unavailable or returned no valid route"),
        input_payload={"currentUserMessage": text},
        output_payload=final_decision.model_dump(mode="json", by_alias=True, exclude_none=True) if final_decision else None,
    )
    if final_decision:
        return _dispatch_decision(
            db,
            user,
            conversation,
            text,
            final_decision,
            execute,
            emit,
            intent_contract=turn_intent,
        )

    # A provider outage must not turn an interpretive replay into a generic
    # analysis summary. Execute the already validated plan, then let the same
    # answer-contract gate used by every harness response return a calibrated
    # fallback rather than pretending the renderer fulfilled the request.
    if replay_decision is not None:
        return _dispatch_decision(
            db,
            user,
            conversation,
            text,
            replay_decision,
            execute,
            emit,
            intent_contract=turn_intent,
        )

    # This compiler is a model-outage fallback. It never runs before Operator in
    # normal operation, so phrase matching cannot override semantic intent.
    compiled_analysis = _compile_known_analysis(db, user, text)
    if compiled_analysis:
        emit(
            "classification",
            "Offline capability compiler selected a validated plan",
            "completed",
            "analysis_harness",
            " → ".join(compiled_analysis.safe_reasoning_summary),
        )
        return _dispatch_decision(
            db,
            user,
            conversation,
            text,
            compiled_analysis,
            execute,
            emit,
            intent_contract=turn_intent,
        )

    # Offline fallbacks are deliberately scarce. A heuristic that guesses
    # intent during a model outage answers the wrong question with confidence:
    # canned small-talk regexes already shipped one wrong-feeling miss, and a
    # substring such as "sip" matched inside unrelated words. Both were
    # removed; unresolved turns fail closed below instead of guessing. What
    # remains is guarded by explicit language and only ever proposes HITL
    # widgets — nothing mutates or reads without the user's next action.
    if _looks_like_planning_command(text):
        fallback_decision = CopilotDecision(tool=capability_for_primitive("planning.run@1"), confidence=0.7, reason="Offline planning fallback.")
        return _dispatch_decision(
            db,
            user,
            conversation,
            text,
            fallback_decision,
            execute,
            emit,
            intent_contract=turn_intent,
        )
    # A failed semantic route must not be reinterpreted by a different domain
    # heuristic. In particular, an ordinary count such as "3 transactions"
    # must never become a ₹3 draft merely because a model contract was
    # unavailable. The outage compiler accepts a transaction only when the
    # deterministic gate already found explicit mutation language; every other
    # unresolved request fails closed without claiming a read or a write.
    if guarded_mutation_intent:
        extracted = extract_transaction(
            text,
            today=today,
            default_currency=user.currency,
        )
        fallback_decision = CopilotDecision(
            tool=capability_for_primitive("transaction.record@1"),
            confidence=0.7,
            reason="Explicit transaction mutation entered the offline draft fallback.",
        )
        return _dispatch_decision(
            db,
            user,
            conversation,
            text,
            fallback_decision,
            execute,
            emit,
            extracted=extracted,
            intent_contract=turn_intent,
        )

    fallback_decision = CopilotDecision(
        tool=capability_for_primitive("agent.unknown@1"),
        reply=(
            "I couldn’t safely resolve this request through an available governed operation, "
            "so I didn’t read or change any financial records. Please try again."
        ),
        confidence=1.0,
        reason="The semantic route was unavailable and no explicit deterministic mutation matched.",
        task_status="failed",
        failure_stage="intent_resolution",
        error_code="unresolved_financial_query",
    )
    return _dispatch_decision(
        db,
        user,
        conversation,
        text,
        fallback_decision,
        execute,
        emit,
        intent_contract=turn_intent,
    )


def _commit_draft(db: Session, user: User, draft: TransactionDraft) -> Transaction:
    if draft.state == DraftState.COMMITTED.value:
        existing_source = owned_transaction_source(
            db,
            user.id,
            TransactionSource.source_type == FinancialSourceType.MANUAL,
            TransactionSource.source_message_id == str(draft.id),
        )
        if existing_source is None:
            raise ValueError("Committed draft has no canonical transaction source")
        transaction = UserScopedRepository(db, user.id).get(
            Transaction,
            existing_source.transaction_id,
        )
        if transaction is None:
            raise ValueError("Committed draft transaction is unavailable")
        return transaction
    _set_ready_if_complete(draft)
    if draft.state != DraftState.READY_FOR_CONFIRMATION.value:
        raise ValueError("Draft is not ready for confirmation")
    merchant = None
    merchant_name = draft.merchant_name
    normalized = normalize_merchant(merchant_name)
    if normalized:
        if merchant_name is None:
            raise RuntimeError("A normalized merchant must retain its display name")
        merchant = MerchantRepository(db, user.id).get_or_create(merchant_name, normalized)
    source_account = None
    destination_account = None
    accounts = AccountRepository(db, user.id)
    if draft.source_account_name:
        source_account = accounts.get_or_create(draft.source_account_name, draft.currency)
        draft.account_id = source_account.id
    if draft.destination_account_name:
        destination_account = accounts.get_or_create(draft.destination_account_name, draft.currency)
        draft.destination_account_id = destination_account.id
    draft.state = DraftState.USER_APPROVED.value
    transaction = create_transaction(
        db,
        user_id=user.id,
        account_id=draft.account_id,
        destination_account_id=draft.destination_account_id,
        transaction_type=draft.transaction_type,
        amount_minor=draft.amount_minor,
        currency=draft.currency,
        merchant_id=merchant.id if merchant else None,
        merchant_name=merchant.canonical_name if merchant else draft.merchant_name,
        category_id=draft.category_id,
        subcategory_id=draft.subcategory_id,
        transaction_at=draft.transaction_at,
        latitude=draft.latitude,
        longitude=draft.longitude,
        location_accuracy=draft.location_accuracy,
        location_source=draft.location_source,
        location_label=draft.location_label,
        description=draft.description,
        spend_nature=draft.spend_nature,
        status=TransactionStatus.PROVISIONAL,
        confidence=draft.confidence,
    )
    source_hash = hashlib.sha256(f"manual:{draft.id}".encode()).hexdigest()
    observation = FinancialObservation(
        user_id=user.id,
        source_type=FinancialSourceType.MANUAL,
        source_message_id=str(draft.id),
        source_hash=source_hash,
        source_account=source_account.name if source_account else None,
        transaction_type=draft.transaction_type,
        amount_minor=draft.amount_minor,
        currency=draft.currency,
        merchant_raw=draft.merchant_name,
        merchant_normalized=normalized,
        transaction_at=draft.transaction_at,
        description=draft.description,
        observed_at=now_utc(),
        confidence=draft.confidence,
    )
    db.add(observation)
    db.flush()
    attach_observation(
        db,
        observation,
        transaction,
        draft.confidence,
        field_values={"raw_text": draft.raw_text, "provenance": draft.field_provenance},
    )
    TagRepository(db, user.id).replace_transaction_tags(
        transaction.id,
        draft.tags,
        source="user" if draft.field_provenance.get("tags", {}).get("origin") == "explicit" else "ai",
        confidence=draft.confidence,
    )
    canonical_values = {
        "transaction_type": transaction.transaction_type,
        "amount_minor": transaction.amount_minor,
        "currency": transaction.currency,
        "merchant": transaction.merchant_name,
        "category_id": str(transaction.category_id) if transaction.category_id else None,
        "subcategory_id": str(transaction.subcategory_id) if transaction.subcategory_id else None,
        "transaction_at": as_utc(transaction.transaction_at).isoformat(),
        "location": transaction.location_label,
        "spend_nature": transaction.spend_nature,
    }
    provenance_aliases = {"amount_minor": "amount", "category_id": "category", "subcategory_id": "subcategory"}
    for field_name, value in canonical_values.items():
        if value is None:
            continue
        provenance = draft.field_provenance.get(provenance_aliases.get(field_name, field_name), {})
        db.add(TransactionFieldValue(
            transaction_id=transaction.id,
            field_name=field_name,
            value={"value": value},
            origin=str(provenance.get("origin", "system")),
            confidence=Decimal(str(provenance.get("confidence", draft.confidence))),
            source_observation_id=observation.id,
            user_confirmed=provenance.get("origin") == "explicit",
        ))
    category, subcategory = TaxonomyRepository(db, user.id).path(
        draft.category_id,
        draft.subcategory_id,
    )
    category_was_explicit = draft.field_provenance.get("category", {}).get("origin") == "explicit"
    subcategory_was_explicit = draft.field_provenance.get("subcategory", {}).get("origin") == "explicit"
    if category and (category_was_explicit or subcategory_was_explicit):
        # Model memory is an interpretation aid only. The category IDs above
        # remain the canonical truth, and no amount or raw prompt is copied.
        remember_taxonomy_mapping(
            user.id,
            category,
            subcategory,
            alias=draft.merchant_name or (subcategory.name if subcategory else category.name),
        )
    draft.state = DraftState.COMMITTED.value
    return transaction


def handle_action(db: Session, user: User, conversation: Conversation, action: str, payload: dict) -> AgentResponse:
    action = WidgetActionId(action)
    payload = validate_action_payload(action, payload)
    if action is WidgetActionId.RESOLVE_CLARIFICATION:
        raise ValueError("Clarifications must resume their active agent interrupt")
    if action is WidgetActionId.CANCEL_PENDING_ACTION:
        return _conversation_response(db, conversation, "Cancelled. No changes were made.")
    if action is WidgetActionId.RENAME_CONVERSATION:
        conversation.title = str(payload["title"])
        return _conversation_response(db, conversation, f"Renamed this thread to “{conversation.title}”.")
    if action is WidgetActionId.CANCEL_OPERATION:
        return _conversation_response(db, conversation, "Cancelled. The operation made no changes.")
    if action in {WidgetActionId.SUBMIT_OPERATION, WidgetActionId.APPROVE_OPERATION}:
        manager = operation_catalog()
        current = manager.snapshot().operation(str(payload["operationId"]))
        if current is None or current.source != "managed":
            return _conversation_response(
                db,
                conversation,
                "That operation is no longer available. Nothing was changed.",
                task_status="cancelled",
                failure_stage="operation_resolution",
                error_code="operation_unavailable",
            )
        inputs = dict(payload.get("inputs") or {})
        changed = (
            current.version != int(payload["operationVersion"])
            or current.checksum != str(payload["operationChecksum"])
        )
        if changed:
            # Carry forward only fields the current file still declares. Any
            # incompatible value returns to the form instead of being coerced.
            declared = set(current.definition.input.schema_.get("properties", {}))
            inputs = {key: value for key, value in inputs.items() if key in declared}
            try:
                validate_operation_inputs(current, inputs, require_complete=False)
            except OperationInputError:
                inputs = {}
            if missing_required_inputs(current, inputs):
                return _operation_form_response(db, conversation, current, inputs)
            validate_operation_inputs(current, inputs)
            return _operation_approval_response(
                db,
                conversation,
                current,
                inputs,
                changed=True,
            )
        validate_operation_inputs(current, inputs, require_complete=False)
        if missing_required_inputs(current, inputs):
            return _operation_form_response(db, conversation, current, inputs)
        validate_operation_inputs(current, inputs)
        if action is WidgetActionId.SUBMIT_OPERATION and requires_confirmation(current):
            return _operation_approval_response(db, conversation, current, inputs)
        return _execute_managed_operation_response(db, user, conversation, current, inputs)
    owned = UserScopedRepository(db, user.id)
    draft_id = payload.get("draftId")
    draft = owned.get(TransactionDraft, UUID(draft_id)) if draft_id else None
    if draft and draft.conversation_id != conversation.id:
        draft = None
    taxonomy = TaxonomyRepository(db, user.id)
    if action is WidgetActionId.EDIT_BUDGET:
        budget = owned.get(Budget, UUID(str(payload["budgetId"])))
        if not budget:
            raise ValueError("Unknown budget")
        return _budget_edit_response(db, user, conversation, budget)
    if action is WidgetActionId.REQUEST_DELETE_BUDGET:
        budget = owned.get(Budget, UUID(str(payload["budgetId"])))
        if not budget:
            raise ValueError("Unknown budget")
        category = taxonomy.category(budget.category_id, expense_only=True)
        widget = _budget_widget(
            str(budget.id),
            budget.name,
            budget.amount_minor,
            _budget_spent_minor(db, user, category),
            category.slug if category else None,
            budget.currency,
            [
                _cancel_pending_action(str(budget.id)),
                WidgetAction(
                    id="delete",
                    label="Delete budget",
                    action=WidgetActionId.DELETE_BUDGET,
                    style="danger",
                    payload={"budgetId": str(budget.id)},
                ),
            ],
        )
        return persist_agent_response(
            db,
            conversation,
            f"Delete your {budget.name.lower()}? This removes the limit, not any transactions.",
            widgets=[widget],
            pending_action=PendingAction(action=WidgetActionId.DELETE_BUDGET, resource_id=str(budget.id)),
        )
    if action is WidgetActionId.DELETE_BUDGET:
        budget = owned.get(Budget, UUID(str(payload["budgetId"])))
        if not budget:
            raise ValueError("Unknown budget")
        name = budget.name
        db.delete(budget)
        return _conversation_response(db, conversation, f"Deleted your {name.lower()}. Your transactions were not changed.")
    if action is WidgetActionId.SET_SPEND_NATURE:
        transaction_id = payload.get("transactionId")
        transaction = active_transaction(db, user.id, UUID(str(transaction_id))) if transaction_id else None
        spend_nature = str(payload.get("spendNature") or "")
        if not transaction:
            raise ValueError("Unknown transaction")
        if transaction.transaction_type != TransactionType.EXPENSE:
            raise ValueError("Spend nature applies only to expenses")
        transaction.spend_nature = spend_nature
        label = "potentially avoidable" if spend_nature == SpendNature.POTENTIALLY_AVOIDABLE else spend_nature
        content = f"Marked the {format_money_minor(transaction.amount_minor, transaction.currency)} {transaction.merchant_name or 'expense'} transaction as {label}."
        widget = _transaction_preview(db, transaction, status="Updated")
        return persist_agent_response(db, conversation, content, widgets=[widget])
    if action is WidgetActionId.CANCEL_TRANSACTION_DRAFT:
        if not draft:
            raise ValueError("Transaction draft is no longer available")
        if draft.state == DraftState.COMMITTED.value:
            raise ValueError("A saved transaction cannot be cancelled as a draft")
        draft.state = DraftState.CANCELLED.value
        return _conversation_response(db, conversation, "Cancelled. Nothing was saved.")
    if action is WidgetActionId.CANCEL_TRANSACTION_EDIT:
        if not draft:
            raise ValueError("Transaction draft is no longer available")
        _set_ready_if_complete(draft)
        return _draft_response(db, conversation, draft)
    if action is WidgetActionId.REVISIT_TRANSACTION_STEP:
        if not draft:
            raise ValueError("Transaction draft is no longer available")
        step = str(payload.get("step") or "")
        if step == "transaction_type":
            draft.transaction_type = TransactionType.UNKNOWN.value
            draft.category_id = None
            draft.subcategory_id = None
            draft.account_id = None
            draft.source_account_name = None
            draft.destination_account_id = None
            draft.destination_account_name = None
        elif step == "category":
            draft.category_id = None
            draft.subcategory_id = None
        elif step == "source_account":
            draft.account_id = None
            draft.source_account_name = None
        else:
            raise ValueError("Unknown transaction step")
        _set_ready_if_complete(draft)
        return _draft_response(db, conversation, draft)
    if action is WidgetActionId.START_ADD_CATEGORY and draft:
        widget = _new_category_widget(draft)
        content = "What should the new category be called?"
        return persist_agent_response(
            db,
            conversation,
            content,
            widgets=[widget],
            pending_action=PendingAction(action=WidgetActionId.CREATE_CATEGORY, resource_id=str(draft.id)),
        )
    if action is WidgetActionId.START_ADD_SUBCATEGORY and draft:
        category = taxonomy.category(draft.category_id)
        if not category:
            raise ValueError("Choose a category before adding a subcategory")
        widget = _taxonomy_editor_widget(WidgetActionId.CREATE_SUBCATEGORY, None, category, draft)
        content = f"What should the new subcategory under {category.name} be called?"
        return persist_agent_response(
            db,
            conversation,
            content,
            widgets=[widget],
            pending_action=PendingAction(action=WidgetActionId.CREATE_SUBCATEGORY, resource_id=str(draft.id)),
        )
    if action in {WidgetActionId.CANCEL_ADD_CATEGORY, WidgetActionId.CANCEL_TAXONOMY_CHANGE}:
        if draft:
            return _draft_response(db, conversation, draft)
        return _conversation_response(db, conversation, "No taxonomy changes were made.")
    if action is WidgetActionId.CREATE_TAXONOMY_PATH:
        name = str(payload.get("name") or "").strip()
        child_names = [str(item).strip() for item in payload.get("subcategories") or []]
        if not name or len(name) > 80:
            raise ValueError("Category name must be between 1 and 80 characters")
        if not child_names or any(not item or len(item) > 80 for item in child_names):
            raise ValueError("Subcategory names must be between 1 and 80 characters")
        created_category = False
        created_children: list[str] = []
        resolved_children: list[Subcategory] = []
        # The savepoint makes parent-plus-children one mutation even when this
        # action runs inside a larger request transaction. A failure creating
        # any child rolls the entire path back, including a newly made parent.
        with db.begin_nested():
            category = next((
                item for item in _expense_categories_for_user(db, user.id)
                if item.name.casefold() == name.casefold()
            ), None)
            if category is None:
                category = taxonomy.create_category(name, "circle-ellipsis", f"custom-{uuid4().hex}")
                created_category = True
                if not any(item.casefold() == "other" for item in child_names):
                    taxonomy.create_subcategory(category, "Other", "other")
            for child_name in child_names:
                child = next((
                    item for item in taxonomy.subcategories(category.id)
                    if item.name.casefold() == child_name.casefold()
                ), None)
                if child is None:
                    child = taxonomy.create_subcategory(
                        category,
                        child_name,
                        f"custom-{uuid4().hex}",
                    )
                    created_children.append(child.name)
                resolved_children.append(child)
            db.flush()
        child_label = ", ".join(item.name for item in resolved_children)
        if created_category or created_children:
            content = (
                f"Added {category.name} with {child_label} "
                f"as its subcategor{'y' if len(resolved_children) == 1 else 'ies'}."
            )
        else:
            content = (
                f"{category.name} already has {child_label} "
                f"as subcategor{'y' if len(resolved_children) == 1 else 'ies'}; no duplicate was created."
            )
        return _conversation_response(db, conversation, content)
    if action is WidgetActionId.CREATE_CATEGORY:
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 80:
            raise ValueError("Category name must be between 1 and 80 characters")
        existing = next((category for category in _expense_categories_for_user(db, user.id) if category.name.casefold() == name.casefold()), None)
        if existing and draft:
            draft.category_id = existing.id
            draft.subcategory_id = None
            _set_ready_if_complete(draft)
            return _draft_or_commit(db, user, conversation, draft)
        if existing:
            return _conversation_response(db, conversation, f"{existing.name} already exists in your categories.")
        custom_category = taxonomy.create_category(name, "circle-ellipsis", f"custom-{uuid4().hex}")
        custom_subcategory = taxonomy.create_subcategory(custom_category, "Other", "other")
        if draft:
            draft.category_id = custom_category.id
            draft.subcategory_id = custom_subcategory.id
            draft.inferred_fields = _without_inferred_fields(draft.inferred_fields, TAXONOMY_INFERENCE_FIELDS)
            _set_ready_if_complete(draft)
            return _draft_or_commit(db, user, conversation, draft)
        return _conversation_response(db, conversation, f"Added {custom_category.name} to your categories.")
    if action is WidgetActionId.CREATE_SUBCATEGORY:
        name = str(payload.get("name") or "").strip()
        category_id = payload.get("categoryId")
        parent_category = taxonomy.category(UUID(str(category_id)), expense_only=True) if category_id else None
        if not name or len(name) > 80:
            raise ValueError("Subcategory name must be between 1 and 80 characters")
        if not parent_category:
            raise ValueError("Unknown parent category")
        existing_subcategory = next((
            item
            for item in _subcategories_for_user(db, user.id, parent_category.id)
            if item.name.casefold() == name.casefold()
        ), None)
        if not existing_subcategory:
            existing_subcategory = taxonomy.create_subcategory(parent_category, name, f"custom-{uuid4().hex}")
        if draft:
            if draft.category_id != parent_category.id:
                raise ValueError("Subcategory does not belong to the draft category")
            draft.subcategory_id = existing_subcategory.id
            draft.inferred_fields = _without_inferred_fields(draft.inferred_fields, SUBCATEGORY_INFERENCE_FIELDS)
            _set_ready_if_complete(draft)
            return _draft_or_commit(db, user, conversation, draft)
        return _conversation_response(db, conversation, f"Added {existing_subcategory.name} under {parent_category.name}.")
    if action is WidgetActionId.SELECT_CATEGORY and draft:
        selected_category = taxonomy.category(UUID(payload["categoryId"]))
        if not selected_category:
            raise ValueError("Unknown category")
        draft.category_id = selected_category.id
        draft.subcategory_id = None
        draft.inferred_fields = _without_inferred_fields(draft.inferred_fields, TAXONOMY_INFERENCE_FIELDS)
        _set_ready_if_complete(draft)
        return _draft_or_commit(db, user, conversation, draft)
    if action is WidgetActionId.SELECT_TRANSACTION_TYPE and draft:
        transaction_type = str(payload.get("optionId") or payload.get("transactionType") or "")
        draft.transaction_type = transaction_type
        if transaction_type != TransactionType.EXPENSE:
            draft.category_id = None
            draft.subcategory_id = None
        draft.inferred_fields = [item for item in draft.inferred_fields if item != "transaction_type"]
        provenance = dict(draft.field_provenance or {})
        provenance["transaction_type"] = {"origin": "explicit", "confidence": 1.0}
        draft.field_provenance = provenance
        _set_ready_if_complete(draft)
        return _draft_or_commit(db, user, conversation, draft)
    if action is WidgetActionId.SELECT_SUBCATEGORY and draft:
        selected_subcategory = taxonomy.subcategory(UUID(payload["subcategoryId"]), category_id=draft.category_id)
        if not selected_subcategory:
            raise ValueError("Unknown subcategory")
        draft.subcategory_id = selected_subcategory.id
        draft.inferred_fields = _without_inferred_fields(draft.inferred_fields, SUBCATEGORY_INFERENCE_FIELDS)
        _set_ready_if_complete(draft)
        return _draft_or_commit(db, user, conversation, draft)
    if action is WidgetActionId.CHANGE_CATEGORY and draft:
        if draft.transaction_type != TransactionType.EXPENSE:
            raise ValueError("Only expenses have spending categories")
        draft.category_id = None
        draft.subcategory_id = None
        draft.inferred_fields = _without_inferred_fields(draft.inferred_fields, TAXONOMY_INFERENCE_FIELDS)
        _set_ready_if_complete(draft)
        return _draft_response(db, conversation, draft)
    if action is WidgetActionId.SELECT_ACCOUNT and draft:
        account_id = payload.get("optionId") or payload.get("accountId")
        account = owned.get(Account, UUID(str(account_id))) if account_id else None
        account_name = str(payload.get("accountName") or "").strip()
        if not account and account_name:
            account = AccountRepository(db, user.id).get_or_create(account_name, draft.currency)
        if not account:
            raise ValueError("Unknown account")
        role = payload.get("role")
        if role == "source_account":
            draft.account_id = account.id
            draft.source_account_name = account.name
        elif role == "destination_account":
            draft.destination_account_id = account.id
            draft.destination_account_name = account.name
        else:
            raise ValueError("Unknown account role")
        _set_ready_if_complete(draft)
        return _draft_or_commit(db, user, conversation, draft)
    if action is WidgetActionId.SAVE_BUDGET:
        budget_amount_minor = int(payload["amountMinor"])
        category_id = UUID(str(payload["categoryId"])) if payload.get("categoryId") else None
        category = taxonomy.category(category_id, expense_only=True)
        if category_id and not category:
            raise ValueError("Unknown category")
        budget_id = payload.get("budgetId")
        budget = owned.get(Budget, UUID(str(budget_id))) if budget_id else None
        if budget_id and not budget:
            raise ValueError("Unknown budget")
        if budget and budget.category_id != category_id:
            raise ValueError("Budget category cannot be changed")
        if budget is None:
            budget = db.scalar(select(Budget).where(Budget.user_id == user.id, Budget.category_id == category_id))
        if budget:
            budget.amount_minor = budget_amount_minor
            budget.name = str(payload.get("name") or budget.name)
        else:
            budget = Budget(user_id=user.id, category_id=category_id, name=str(payload.get("name") or "Monthly spending budget"), amount_minor=budget_amount_minor, currency=user.currency)
            db.add(budget)
            db.flush()
        spent = _budget_spent_minor(db, user, category)
        content = f"Set your {budget.name.lower()} to {format_money_minor(budget.amount_minor, budget.currency)} per month."
        widget = _budget_widget(str(budget.id), budget.name, budget.amount_minor, spent, category.slug if category else None, budget.currency, _budget_management_actions(budget))
        return persist_agent_response(db, conversation, content, widgets=[widget])
    if action is WidgetActionId.SAVE_GOAL:
        target_minor = int(payload["targetMinor"])
        name = str(payload.get("name") or "Savings goal").strip()[:120]
        goal = db.scalar(select(Goal).where(Goal.user_id == user.id, func.lower(Goal.name) == name.lower()))
        if goal:
            goal.target_minor = target_minor
        else:
            goal = Goal(user_id=user.id, name=name, target_minor=target_minor, current_minor=0, currency=user.currency)
            db.add(goal)
            db.flush()
        content = f"Created your {goal.name} goal with a {format_money_minor(goal.target_minor, goal.currency)} target."
        widget = _goal_widget(str(goal.id), goal.name, goal.target_minor, goal.current_minor, goal.currency)
        return persist_agent_response(db, conversation, content, widgets=[widget])
    if action is WidgetActionId.CONTRIBUTE_GOAL:
        goal = owned.get(Goal, UUID(str(payload.get("goalId")))) if payload.get("goalId") else None
        contribution_minor = int(payload["amountMinor"])
        if not goal:
            raise ValueError("Unknown goal")
        goal.current_minor += contribution_minor
        db.add(GoalContribution(
            user_id=user.id,
            goal_id=goal.id,
            amount_minor=contribution_minor,
            currency=goal.currency,
            contribution_at=now_utc(),
        ))
        content = f"Added {format_money_minor(contribution_minor, goal.currency)} to your {goal.name} goal."
        widget = _goal_widget(str(goal.id), goal.name, goal.target_minor, goal.current_minor, goal.currency)
        return persist_agent_response(db, conversation, content, widgets=[widget])
    if action is WidgetActionId.COMMIT_IMPORT:
        import_id = payload.get("importId")
        job = owned.get(Import, UUID(str(import_id))) if import_id else None
        if not job:
            raise ValueError("Unknown import")
        if job.status == ImportStatus.COMPLETED:
            content = f"{job.filename} was already imported. No duplicate records were created."
        else:
            records = list(db.scalars(select(ImportRecord).where(ImportRecord.import_id == job.id).order_by(ImportRecord.row_number)))
            high_confidence = review = duplicates = 0
            for record in records:
                if record.status != ImportRecordStatus.STAGED or not record.observation_payload:
                    review += int(record.status == ImportRecordStatus.INVALID)
                    continue
                result = ingest_observation(db, user.id, ObservationIn.model_validate(record.observation_payload))
                record.observation_id = result.observation_id
                record.status = ImportRecordStatus.DUPLICATE if result.idempotent_replay else ImportRecordStatus.NEEDS_REVIEW if result.decision is ReconciliationOutcome.NEEDS_REVIEW else ImportRecordStatus.IMPORTED
                duplicates += int(record.status == ImportRecordStatus.DUPLICATE)
                review += int(record.status == ImportRecordStatus.NEEDS_REVIEW)
                high_confidence += int(record.status == ImportRecordStatus.IMPORTED)
            job.high_confidence_records = high_confidence
            job.review_records = review
            job.duplicate_records = duplicates
            job.status = ImportStatus.READY_FOR_REVIEW if review else ImportStatus.COMPLETED
            content = f"Imported {high_confidence} transaction{'s' if high_confidence != 1 else ''} from {job.filename}."
            if review:
                content += f" {review} need your review."
            if duplicates:
                content += f" {duplicates} duplicate{'s were' if duplicates != 1 else ' was'} skipped."
        widget = Widget(
            id=f"imported-{job.id}",
            type=WidgetType.IMPORT_REVIEW,
            data={"title": job.filename, **import_summary(job, idempotent_replay=False)},
        )
        return persist_agent_response(db, conversation, content, widgets=[widget])
    if action is WidgetActionId.CALCULATE_LOAN_SCENARIO:
        principal_minor = payload["principalMinor"]
        rate = payload["annualRatePercent"]
        months = payload["tenureMonths"]
        prepayment_minor = payload.get("prepaymentMinor", 0)
        result = loan_with_prepayment(principal_minor, rate, months, prepayment_minor)
        content = f"A {format_money_minor(prepayment_minor, user.currency)} prepayment saves {format_money_minor(result['interest_saved_minor'], user.currency)} in interest and reduces the EMI by {format_money_minor(result['emi_reduction_minor'], user.currency)}, assuming the remaining tenure stays unchanged."
        widget = Widget(id=f"loan-result-{uuid4()}", type=WidgetType.LOAN_CALCULATOR, data={"title": "Home-loan prepayment result", "principalMinor": principal_minor, "annualRatePercent": rate, "tenureMonths": months, "prepaymentMinor": prepayment_minor, "currency": user.currency, "result": result}, actions=[WidgetAction(id="calculate", label="Calculate", action=WidgetActionId.CALCULATE_LOAN_SCENARIO, style="primary", payload={})])
        citations = [DataReference(label="Deterministic amortization calculation", entity_type="calculator", query={"principalMinor": principal_minor, "annualRatePercent": rate, "tenureMonths": months, "prepaymentMinor": prepayment_minor})]
        return persist_agent_response(
            db,
            conversation,
            content,
            widgets=[widget],
            citations=citations,
        )
    if action is WidgetActionId.CALCULATE_INVESTMENT_SCENARIO:
        monthly_minor = payload["monthlyContributionMinor"]
        current_minor = payload.get("currentValueMinor", 0)
        rate = payload["annualReturnPercent"]
        years = payload["years"]
        result = investment_projection(monthly_minor, current_minor, rate, years)
        content = f"At an assumed {rate:g}% annual return, the projected value after {years} years is {format_money_minor(result['projected_value_minor'], user.currency)}; {format_money_minor(result['estimated_returns_minor'], user.currency)} is estimated growth, not guaranteed return."
        widget = Widget(id=f"investment-result-{uuid4()}", type=WidgetType.INVESTMENT_PROJECTION, data={"title": "Investment projection result", "monthlyContributionMinor": monthly_minor, "currentValueMinor": current_minor, "annualReturnPercent": rate, "years": years, "currency": user.currency, "result": result}, actions=[WidgetAction(id="calculate", label="Project", action=WidgetActionId.CALCULATE_INVESTMENT_SCENARIO, style="primary", payload={})])
        citations = [DataReference(label="Deterministic compound-growth calculation", entity_type="calculator", query={"monthlyContributionMinor": monthly_minor, "currentValueMinor": current_minor, "annualReturnPercent": rate, "years": years})]
        return persist_agent_response(
            db,
            conversation,
            content,
            widgets=[widget],
            citations=citations,
        )
    if action is WidgetActionId.COMMIT_TRANSACTION and draft:
        return _committed_response(db, user, conversation, draft)
    if action is WidgetActionId.EDIT_TRANSACTION and draft:
        widget = Widget(
            id=f"edit-{draft.id}-{uuid4()}",
            type=WidgetType.TRANSACTION_EDIT,
            data={"draftId": str(draft.id), "title": "Edit transaction", "amountMinor": draft.amount_minor, "currency": draft.currency, "merchant": draft.merchant_name, "transactionAt": as_utc(draft.transaction_at), "fields": ["amount", "merchant", "transaction_at"]},
            actions=[
                WidgetAction(id="update", label="Apply changes", action=WidgetActionId.UPDATE_TRANSACTION_DRAFT, style="primary", payload={"draftId": str(draft.id)}),
                WidgetAction(id="back", label="Back", action=WidgetActionId.CANCEL_TRANSACTION_EDIT, style="secondary", payload={"draftId": str(draft.id)}),
                *_draft_navigation_actions(draft),
            ],
        )
        content = "What would you like to change?"
        return persist_agent_response(
            db,
            conversation,
            content,
            widgets=[widget],
            pending_action=PendingAction(
                action=WidgetActionId.UPDATE_TRANSACTION_DRAFT,
                resource_id=str(draft.id),
            ),
        )
    if action is WidgetActionId.UPDATE_TRANSACTION_DRAFT and draft:
        amount_minor = payload.get("amountMinor")
        if amount_minor is not None:
            draft.amount_minor = amount_minor
        if "merchant" in payload:
            draft.merchant_name = str(payload.get("merchant") or "").strip() or None
        if payload.get("transactionAt"):
            draft.transaction_at = as_utc(datetime.fromisoformat(str(payload["transactionAt"]).replace("Z", "+00:00")))
        _set_ready_if_complete(draft)
        return _draft_response(db, conversation, draft)
    if action is WidgetActionId.EDIT_SAVED_TRANSACTION:
        transaction_id = payload.get("transactionId")
        transaction = active_transaction(db, user.id, UUID(str(transaction_id))) if transaction_id else None
        if not transaction:
            raise ValueError("Unknown transaction")
        proposed_aliases = {
            "amountMinor": "amount_minor",
            "merchant": "merchant",
            "transactionDate": "transaction_date",
            "transactionType": "transaction_type",
            "categorySlug": "category_slug",
            "subcategorySlug": "subcategory_slug",
            "location": "location",
            "spendNature": "spend_nature",
            "tags": "tags",
        }
        proposed = {
            field: payload[source]
            for source, field in proposed_aliases.items()
            # The action transport materializes optional schema fields as null.
            # Opening an editor with those placeholders must not be mistaken
            # for a request to clear every nullable transaction field.
            if source in payload and payload[source] is not None
        }
        return _saved_transaction_edit_response(
            db,
            user,
            conversation,
            transaction,
            proposed=proposed,
        )
    if action is WidgetActionId.CANCEL_SAVED_TRANSACTION_EDIT:
        transaction_id = payload.get("transactionId")
        transaction = active_transaction(db, user.id, UUID(str(transaction_id))) if transaction_id else None
        if not transaction:
            raise ValueError("Unknown transaction")
        content = "No changes were made."
        widget = _transaction_preview(db, transaction)
        return persist_agent_response(db, conversation, content, widgets=[widget])
    if action is WidgetActionId.UPDATE_SAVED_TRANSACTION:
        transaction_id = payload.get("transactionId")
        if not transaction_id:
            raise ValueError("Unknown transaction")
        existing_transaction = active_transaction(db, user.id, UUID(str(transaction_id)))
        if existing_transaction is None:
            raise ValueError("Unknown transaction")
        opening_version = existing_transaction.row_version
        transaction_at = (
            datetime.fromisoformat(str(payload["transactionAt"]).replace("Z", "+00:00"))
            if payload.get("transactionAt")
            else UNSET
        )
        try:
            transaction = update_saved_transaction(
                db,
                user.id,
                UUID(str(transaction_id)),
                amount_minor=int(payload["amountMinor"]),
                merchant=payload["merchant"] if "merchant" in payload else UNSET,
                transaction_at=transaction_at,
                transaction_type=payload["transactionType"] if "transactionType" in payload else UNSET,
                location=payload["location"] if "location" in payload else UNSET,
                spend_nature=payload["spendNature"] if "spendNature" in payload else UNSET,
                category_id=payload["categoryId"] if "categoryId" in payload else UNSET,
                subcategory_id=payload["subcategoryId"] if "subcategoryId" in payload else UNSET,
                latitude=payload.get("latitude"),
                longitude=payload.get("longitude"),
                location_accuracy=payload.get("locationAccuracy"),
                tags=payload["tags"] if "tags" in payload else UNSET,
                expected_version=payload.get("expectedVersion"),
                source="conversation_edit",
                conversation_id=conversation.id,
            )
        except TransactionVersionConflict:
            current_transaction = active_transaction(db, user.id, UUID(str(transaction_id)))
            if current_transaction is None:
                raise ValueError("Unknown transaction")
            return persist_agent_response(
                db,
                conversation,
                "This transaction changed after the editor opened, so I did not overwrite it. Review the latest version and choose Edit again.",
                widgets=[_transaction_preview(db, current_transaction, status="Changed since opened")],
                task_status="needs_input",
                failure_stage="transaction_version_conflict",
                error_code="stale_transaction_edit",
            )
        amended = transaction.row_version > opening_version
        content = (
            f"Updated the {format_money_minor(transaction.amount_minor, transaction.currency)} transaction."
            if amended
            else "No changes were needed; this transaction is already up to date."
        )
        widget = _transaction_preview(db, transaction, status="Updated" if amended else "Unchanged")
        superseded = _supersede_transaction_previews(db, conversation, transaction, widget)
        return persist_agent_response(
            db,
            conversation,
            content,
            widgets=[widget],
            widget_updates=superseded,
        )
    if action is WidgetActionId.REQUEST_REMOVE_TRANSACTION:
        transaction_id = payload.get("transactionId")
        transaction = active_transaction(db, user.id, UUID(str(transaction_id))) if transaction_id else None
        if not transaction:
            raise ValueError("Unknown transaction")
        widget = _removal_confirmation_widget(transaction)
        content = "Remove this transaction? This will exclude it from your financial totals."
        return persist_agent_response(
            db,
            conversation,
            content,
            widgets=[widget],
            pending_action=PendingAction(
                action=WidgetActionId.CONFIRM_REMOVE_TRANSACTION,
                resource_id=str(transaction.id),
            ),
        )
    if action in {WidgetActionId.CONFIRM_REMOVE_TRANSACTION, WidgetActionId.CANCEL_REMOVE_TRANSACTION}:
        transaction_id = payload.get("transactionId")
        transaction = active_transaction(db, user.id, UUID(str(transaction_id))) if transaction_id else None
        if not transaction:
            raise ValueError("Unknown transaction")
        if action is WidgetActionId.CONFIRM_REMOVE_TRANSACTION:
            transaction.deleted_at = now_utc()
            content = f"Removed the {format_money_minor(transaction.amount_minor, transaction.currency)} transaction."
            widget = _transaction_preview(db, transaction, status="Removed")
        else:
            content = "Kept the transaction."
            widget = _transaction_preview(db, transaction)
        return persist_agent_response(db, conversation, content, widgets=[widget])
    if action in {WidgetActionId.MERGE_RECONCILIATION, WidgetActionId.SEPARATE_RECONCILIATION}:
        candidate_id = payload.get("candidateId")
        if not candidate_id:
            raise ValueError("Missing reconciliation candidate")
        decision = ReconciliationResolution.SAME_TRANSACTION if action is WidgetActionId.MERGE_RECONCILIATION else ReconciliationResolution.SEPARATE_TRANSACTION
        transaction = resolve_reconciliation(db, user.id, UUID(str(candidate_id)), decision)
        content = "Merged the observations into one transaction." if decision is ReconciliationResolution.SAME_TRANSACTION else "Kept this as a separate transaction."
        widget = Widget(id=f"resolved-{candidate_id}", type=WidgetType.TRANSACTION_PREVIEW, data={"transactionId": str(transaction.id), "title": transaction.merchant_name or "Transaction", "amountMinor": transaction.amount_minor, "currency": transaction.currency, "transactionAt": as_utc(transaction.transaction_at), "status": "Reconciliation complete", "sourceCount": len(transaction.sources)})
        return persist_agent_response(db, conversation, content, widgets=[widget])
    raise ValueError("This action is no longer available")


def user_conversation(
    db: Session,
    user_id: UUID,
    conversation_id: UUID,
    *,
    with_messages: bool = False,
) -> Conversation | None:
    """The canonical ownership filter for every conversation-scoped operation."""
    statement = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.user_id == user_id,
    )
    if with_messages:
        statement = statement.options(selectinload(Conversation.messages))
    return db.scalar(statement)
