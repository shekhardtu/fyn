from __future__ import annotations

import ast
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from time import perf_counter
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import String, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..domain import (
    EDITABLE_TRANSACTION_TYPES,
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
    Import,
    ImportRecord,
    Message,
    ReconciliationCandidate,
    SavedAnalysis,
    Subcategory,
    Tag,
    Transaction,
    TransactionDraft,
    TransactionFieldValue,
    TransactionSource,
    TransactionTag,
    User,
)
from ..schemas import AgentActivityEvent, AgentResponse, DataReference, ObservationIn, PendingAction, Widget, WidgetAction, WidgetLifecycle, WidgetType, WidgetUpdate, validate_action_payload
from ..taxonomy_catalog import DefaultCategorySlug
from ..visualization_contracts import ORDERED_VISUAL_FIELD_TYPES
from .analytics import cash_position, category_breakdown, change_drivers, month_bounds, monthly_comparison, recurring_expenses, shift_month, spending_summary, subcategory_breakdown
from .accounts import AccountRepository
from .adapters import import_summary
from .agents import ACCEPTED_COPILOT_VALIDATION_OUTCOMES, GROUPED_QUERY_OPERATIONS, RECENT_CONTEXT_MESSAGE_LIMIT, CopilotDecision, CopilotDecisionValidation, QueryInterpretation, interpret_with_financial_copilot, validate_copilot_decision
from .analysis_harness import HarnessValidationError, discover_analysis_tools, execute_generated_tool
from .calculators import affordability, investment_projection, loan_with_prepayment
from .capabilities import CapabilityId, ExecutorKind, SAFE_READ_CAPABILITIES, capability_spec
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
from .extraction import ExtractedTransaction, extract_transaction, infer_expense_category, looks_like_financial_query, normalize_merchant, parse_amount_minor, parse_spending_period
from .merchants import MerchantRepository
from .reconciliation import attach_observation, ingest_observation, resolve_reconciliation
from .repositories import UserScopedRepository
from .runtime_tools import build_runtime_tools
from .semantic import AnalysisPlan, AnalysisToolProposal, AnalysisTransform, FinanceFilter, FinanceQueryPlan, VisualEncoding, VisualEncodingSet, VisualizationSpec
from .tags import TagRepository
from .taxonomy import TaxonomyRepository, agent_taxonomy as _agent_taxonomy
from .transactions import active_transaction, canonical_transactions, create_transaction, expense_transactions, owned_transaction_source
from .user_memory import remember_taxonomy_mapping
from .widget_library import FieldPresentation, RowCapability, TableBlueprint, WidgetLibrary


ActivityCallback = Callable[[dict], None]
DRAFT_RESOURCE_ID = "draft"
TAXONOMY_FIELDS = frozenset(TAXONOMY_FIELD_NAMES)
TAXONOMY_INFERENCE_FIELDS = TAXONOMY_FIELDS | {"merchant preference"}
SUBCATEGORY_INFERENCE_FIELDS = TAXONOMY_INFERENCE_FIELDS - {"category"}
MONTH_CATEGORY_DIMENSIONS = ("month", "category")


def _without_inferred_fields(values: list[str], excluded: Collection[str]) -> list[str]:
    return [field for field in values if field not in excluded]


def _analysis_lifecycle_badge(stage: str, label: str, status: ExecutionStatus | str) -> str | None:
    status = ExecutionStatus(status)
    if status is ExecutionStatus.FAILED and stage in {"tool_discovery", "tool_validation", "tool_repair", "result_verification"}:
        return "Rejected"
    if status is not ExecutionStatus.COMPLETED:
        return None
    if stage == "tool_synthesis":
        return "Saved"
    if stage == "tool_discovery" and label.startswith("Reusing"):
        return "Reused"
    if stage == "tool_repair":
        return "Updated"
    if stage == "tool_validation":
        return "Validated"
    return None


def _local_today(user: User) -> date:
    """Return the user's calendar date, never the API host's UTC date."""
    try:
        return datetime.now(ZoneInfo(user.timezone)).date()
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now(timezone.utc).date()


def _serialize_widget(widget: Widget) -> dict:
    return widget.model_dump(mode="json")


def _find_persisted_widget(
    db: Session,
    conversation: Conversation,
    widget_id: str,
) -> tuple[Message, int, Widget] | None:
    """Find one widget by its protocol identity inside this conversation."""
    messages = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at, Message.id)
    )
    for message in messages:
        for index, raw_widget in enumerate(message.widgets or []):
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
    safe_payload = {
        key: value
        for key, value in payload.items()
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
        if WidgetActionId(widget.data.get("operation")) is WidgetActionId.CREATE_SUBCATEGORY:
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
    return WidgetUpdate(widgetId=widget.id, widget=resolved)


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


def _reserve_reply(db: Session, conversation: Conversation) -> None:
    """Writes the empty row this turn's reply will be filled into."""
    reply = Message(conversation_id=conversation.id, role="assistant", content="", widgets=[], citations=[])
    db.add(reply)
    db.flush()
    _reserved_reply.set(reply)


def _history_only() -> tuple:
    """Keeps this turn's reserved reply out of anything that reads the
    conversation back. The row exists from the moment the turn is admitted so
    that the answer holds its place, but it is empty until the turn answers:
    it is not history, and it must never reach the model's context window."""
    reserved = _reserved_reply.get()
    return (Message.id != reserved.id,) if reserved is not None else ()


def _clarification_draft(
    db: Session,
    conversation: Conversation,
) -> TransactionDraft | None:
    """Return the one most recent draft awaiting user input in this chat."""
    return db.scalar(
        select(TransactionDraft)
        .where(
            TransactionDraft.conversation_id == conversation.id,
            TransactionDraft.state == DraftState.NEEDS_CLARIFICATION.value,
        )
        .order_by(TransactionDraft.updated_at.desc())
    )


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
    else:
        message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=content,
            widgets=serialized_widgets,
            citations=serialized_citations,
        )
        db.add(message)
    db.flush()
    if message.citations:
        conversation.active_analysis_state, conversation.active_data_scope = _grounded_states(message)
    return message


def persist_agent_response(
    db: Session,
    conversation: Conversation,
    content: str,
    *,
    widgets: list[Widget] | None = None,
    citations: list[DataReference] | None = None,
    pending_action: PendingAction | None = None,
    widget_updates: list[WidgetUpdate] | None = None,
) -> AgentResponse:
    """Persist and return one response through the canonical reply boundary."""
    response_widgets = widgets or []
    response_citations = citations or []
    message = record_assistant_message(
        db,
        conversation,
        content,
        response_widgets,
        response_citations,
    )
    db.commit()
    return AgentResponse(
        message=content,
        widgets=response_widgets,
        widgetUpdates=widget_updates or [],
        pendingAction=pending_action,
        citations=response_citations,
        conversation_id=conversation.id,
        message_id=message.id,
    )


def get_or_create_conversation(db: Session, user: User, conversation_id: UUID | None = None) -> Conversation:
    conversation = user_conversation(db, user.id, conversation_id) if conversation_id else None
    if conversation_id and not conversation:
        raise ValueError("Conversation not found")
    if conversation is None:
        conversation = Conversation(user_id=user.id, title="Financial check-in")
        db.add(conversation)
        db.flush()
        record_assistant_message(
            db,
            conversation,
            "Hi, I’m fyn. Tell me what happened, or ask anything about your money.",
            [Widget(
                id=f"welcome-{conversation.id}",
                type=WidgetType.INSIGHT_CARD,
                data={
                    "eyebrow": "Start naturally",
                    "title": "Your finances, in one conversation",
                    "body": "Try “Spent ₹500 on lunch”, “Got ₹2 lakh salary”, or “How much did I spend this month?”",
                    "tone": "welcome",
                },
            )],
        )
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
    _, category, subcategory, term = max(matches, key=lambda item: item[0])
    return category, subcategory, term


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


def _category_selector(db: Session, draft: TransactionDraft) -> Widget:
    categories = _expense_categories_for_user(db, draft.user_id)
    user = db.get(User, draft.user_id)
    recommendation = recommend_categories(db, user, draft, categories)
    return Widget(
        id=f"category-{draft.id}-{uuid4()}",
        type=WidgetType.CATEGORY_SELECTOR,
        data={"title": "Where should I categorize this?", "body": _suggestion_body(recommendation, "categories"), "draftId": str(draft.id), "suggestions": recommendation.as_dicts(), "options": [{"id": str(c.id), "slug": c.slug, "label": c.name, "icon": c.icon} for c in categories], "allowCreate": True},
        actions=[WidgetAction(id="select", label="Select category", action=WidgetActionId.SELECT_CATEGORY, payload={"draftId": str(draft.id)})],
    )


def _new_category_widget(draft: TransactionDraft) -> Widget:
    return Widget(
        id=f"category-create-{draft.id}-{uuid4()}",
        type=WidgetType.CATEGORY_SELECTOR,
        data={"title": "Add a new category", "mode": "create", "draftId": str(draft.id), "options": []},
        actions=[WidgetAction(id="create", label="Add category", action=WidgetActionId.CREATE_CATEGORY, style="primary", payload={"draftId": str(draft.id)})],
    )


def _taxonomy_editor_widget(
    operation: WidgetActionId | str,
    name: str | None,
    parent: Category | None,
    draft: TransactionDraft | None,
) -> Widget:
    action_id = WidgetActionId(operation)
    if action_id not in {WidgetActionId.CREATE_CATEGORY, WidgetActionId.CREATE_SUBCATEGORY}:
        raise ValueError("Unsupported taxonomy operation")
    return Widget(
        id=f"taxonomy-{uuid4()}",
        type=WidgetType.TAXONOMY_EDITOR,
        data={
            "operation": action_id,
            "name": name,
            "parentCategory": parent.name if parent else None,
            "appliesToDraft": bool(draft),
            "draftId": str(draft.id) if draft else None,
            "categoryId": str(parent.id) if parent else None,
            "lifecycle": "pending",
        },
        actions=[
            WidgetAction(
                id="confirm-taxonomy",
                label="Add subcategory" if action_id is WidgetActionId.CREATE_SUBCATEGORY else "Add category",
                action=action_id,
                style="primary",
                payload={
                    "draftId": str(draft.id) if draft else None,
                    "categoryId": str(parent.id) if parent else None,
                },
            ),
            WidgetAction(
                id="cancel-taxonomy",
                label="Cancel",
                action=WidgetActionId.CANCEL_TAXONOMY_CHANGE,
                payload={
                    "draftId": str(draft.id) if draft else None,
                    "categoryId": str(parent.id) if parent else None,
                },
            ),
        ],
    )


def _subcategory_selector(db: Session, draft: TransactionDraft) -> Widget:
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
        actions=[WidgetAction(id="select", label="Select subcategory", action=WidgetActionId.SELECT_SUBCATEGORY, payload={"draftId": str(draft.id)})],
    )


def _account_selector(db: Session, draft: TransactionDraft, role: str) -> Widget:
    accounts = list(db.scalars(select(Account).where(Account.user_id == draft.user_id).order_by(Account.name)))
    title = "Which account did the money leave?" if role == "source_account" else "Which account received the money?"
    return Widget(
        id=f"account-{role}-{draft.id}-{uuid4()}",
        type=WidgetType.ACCOUNT_SELECTOR,
        data={"title": title, "body": "Choose an account or type its name in the conversation.", "draftId": str(draft.id), "role": role, "options": [{"id": str(account.id), "slug": account.name.lower(), "label": account.name} for account in accounts]},
        actions=[WidgetAction(id="select", label="Select account", action=WidgetActionId.SELECT_ACCOUNT, payload={"draftId": str(draft.id), "role": role})],
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
        actions=[WidgetAction(id="select", label="Select transaction type", action=WidgetActionId.SELECT_TRANSACTION_TYPE, payload={"draftId": str(draft.id)})],
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
            "date": draft.transaction_date.isoformat() if draft.transaction_date else None,
            "time": draft.transaction_time,
            "timezone": draft.timezone,
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


def _create_draft(db: Session, user: User, conversation: Conversation, text: str, result: ExtractedTransaction | None = None) -> TransactionDraft:
    result = result or extract_transaction(text, today=_local_today(user), default_currency=user.currency)
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
        "transaction_date": result.transaction_date.isoformat() if result.transaction_date else None,
        "transaction_time": result.transaction_time,
        "timezone": result.timezone or user.timezone,
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
        transaction_date=result.transaction_date,
        transaction_time=result.transaction_time,
        timezone=result.timezone or user.timezone,
        location_label=result.location_label,
        location_source="user" if "location" in explicit else "inference" if result.location_label else None,
        description=text,
        tags=result.tags,
        spend_nature=result.spend_nature,
        field_provenance=provenance,
        confidence=result.confidence,
        inferred_fields=result.inferred_fields,
        missing_fields=result.missing_fields,
        state=DraftState.ENRICHED.value,
    )
    db.add(draft)
    db.flush()
    # What the user learned to do outranks a static catalog guess, but never
    # what they just said in this message.
    if draft.transaction_type == TransactionType.EXPENSE.value and provenance.get("category", {}).get("origin") != "explicit":
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
        widgets = [Widget(id=f"amount-{draft.id}-{uuid4()}", type=WidgetType.TRANSACTION_EDIT, data={"draftId": str(draft.id), "title": "Add the missing amount", "fields": ["amount"]})]
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
            "draftId": str(draft_id) if draft_id else None,
            "title": label,
            "amountMinor": transaction.amount_minor,
            "currency": transaction.currency,
            "date": transaction.transaction_date.isoformat(),
            "status": status,
            "sourceCount": len(transaction.sources) or 1,
            "transactionType": transaction.transaction_type,
            "category": category.name if category else None,
            "subcategory": subcategory.name if subcategory else None,
            "time": transaction.transaction_time,
            "timezone": transaction.timezone,
            "location": transaction.location_label,
            "spendNature": transaction.spend_nature,
            "tags": tag_names,
        },
        actions=[] if status == "Removed" else [
            WidgetAction(id="edit", label="Edit", action=WidgetActionId.EDIT_SAVED_TRANSACTION, style="secondary", payload={"transactionId": str(transaction.id)}),
            WidgetAction(id="remove", label="Remove", action=WidgetActionId.REQUEST_REMOVE_TRANSACTION, style="ghost", payload={"transactionId": str(transaction.id)}),
        ],
    )


def _committed_response(db: Session, user: User, conversation: Conversation, draft: TransactionDraft) -> AgentResponse:
    transaction = _commit_draft(db, user, draft)
    widget = _transaction_preview(db, transaction, draft.id)
    label = str(widget.data["title"])
    type_label = transaction.transaction_type.replace("_", " ")
    content = f"Added {format_money_minor(transaction.amount_minor, transaction.currency)} {label}{'' if label.lower() == type_label else f' {type_label}'}. You can edit or remove it below."
    return persist_agent_response(db, conversation, content, widgets=[widget])


def _draft_or_commit(db: Session, user: User, conversation: Conversation, draft: TransactionDraft) -> AgentResponse:
    _set_ready_if_complete(draft)
    if draft.state == DraftState.READY_FOR_CONFIRMATION.value:
        return _committed_response(db, user, conversation, draft)
    return _draft_response(db, conversation, draft)


def _looks_like_planning_command(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("budget", "goal", "savings", "save for", "saving for"))


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
        id=f"budget-{budget_id}",
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
        id=f"goal-{goal_id}",
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


def _planning_response(db: Session, user: User, conversation: Conversation, text: str) -> AgentResponse:
    lowered = text.lower()
    today = _local_today(user)
    parsed_amount = extract_transaction(text, today=today, default_currency=user.currency).amount_minor
    widgets: list[Widget] = []
    pending: PendingAction | None = None

    if "budget" in lowered:
        categories = _expense_categories_for_user(db, user.id)
        category = next((item for item in categories if item.slug in lowered or item.name.lower() in lowered), None)
        if any(token in lowered for token in ("set", "create", "make", "limit")):
            if not parsed_amount:
                content = "What monthly amount should I use for this budget?"
            else:
                name = f"{category.name} budget" if category else "Monthly spending budget"
                payload = {"name": name, "amountMinor": parsed_amount, "categoryId": str(category.id) if category else None}
                widgets = [_budget_widget(DRAFT_RESOURCE_ID, name, parsed_amount, 0, category.slug if category else None, user.currency, [WidgetAction(id="save", label="Set budget", action=WidgetActionId.SAVE_BUDGET, style="primary", payload=payload)])]
                content = f"Ready to set a {format_money_minor(parsed_amount, user.currency)} monthly {category.name.lower() + ' ' if category else ''}budget."
                pending = PendingAction(action=WidgetActionId.SAVE_BUDGET, resource_id=DRAFT_RESOURCE_ID)
        else:
            budgets = list(db.scalars(select(Budget).where(Budget.user_id == user.id).order_by(Budget.updated_at.desc())))
            start, end = month_bounds(today)
            taxonomy = TaxonomyRepository(db, user.id)
            for budget in budgets:
                category = taxonomy.category(budget.category_id)
                spent = spending_summary(db, user.id, start, min(today, end), category.slug if category else None)["total_minor"]
                widgets.append(_budget_widget(str(budget.id), budget.name, budget.amount_minor, spent, category.slug if category else None, budget.currency))
            content = f"You have {len(budgets)} active monthly budget{'s' if len(budgets) != 1 else ''}." if budgets else "You don’t have a budget yet. You can say “Set a ₹20,000 food budget.”"
    elif any(token in lowered for token in ("add", "contribute", "put")) and any(token in lowered for token in ("savings", "goal", "vacation")):
        name = _goal_name(text)
        goal = db.scalar(select(Goal).where(Goal.user_id == user.id, func.lower(Goal.name) == name.lower()))
        if not goal:
            content = f"I don’t have a {name} goal yet. Tell me its target first, for example “Create a {format_money_minor(parsed_amount or 20_000_000, user.currency)} {name.lower()} goal.”"
        elif not parsed_amount:
            content = f"How much should I add to your {goal.name} goal?"
        else:
            widgets = [_goal_widget(str(goal.id), goal.name, goal.target_minor, goal.current_minor, goal.currency, [WidgetAction(id="contribute", label=f"Add {format_money_minor(parsed_amount, goal.currency)}", action=WidgetActionId.CONTRIBUTE_GOAL, style="primary", payload={"goalId": str(goal.id), "amountMinor": parsed_amount})])]
            content = f"Ready to add {format_money_minor(parsed_amount, goal.currency)} to your {goal.name} goal."
            pending = PendingAction(action=WidgetActionId.CONTRIBUTE_GOAL, resource_id=str(goal.id))
    elif any(token in lowered for token in ("create", "set", "start", "save for", "saving for")):
        name = _goal_name(text)
        if not parsed_amount:
            content = f"What target amount should I use for your {name} goal?"
        else:
            payload = {"name": name, "targetMinor": parsed_amount}
            widgets = [_goal_widget(DRAFT_RESOURCE_ID, name, parsed_amount, 0, user.currency, [WidgetAction(id="save", label="Create goal", action=WidgetActionId.SAVE_GOAL, style="primary", payload=payload)])]
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


def _interpret_prompt(
    db: Session,
    user: User,
    conversation: Conversation,
    user_message: Message,
    text: str,
    enable_reasoning: bool = True,
    activity_callback: Callable[[str, str, str, str | None, str | None], None] | None = None,
) -> CopilotDecision | None:
    recent = list(db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id, Message.id != user_message.id, *_history_only())
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(RECENT_CONTEXT_MESSAGE_LIMIT)
    ))
    recent_context = [{"role": item.role, "content": item.content} for item in reversed(recent)]
    active_data_scope = conversation.active_data_scope
    active_analysis_state = conversation.active_analysis_state
    if not _complete_analysis_state(active_analysis_state):
        # Backfill pre-migration conversations once. Failed/clarification turns
        # and legacy partial citations do not erase the last complete state.
        active_analysis_state = None
        active_data_scope = None
        grounded_messages = list(db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id, Message.role == "assistant", *_history_only())
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(50)
        ))
        for item in grounded_messages:
            analysis_state, data_scope = _grounded_states(item)
            if _complete_analysis_state(analysis_state):
                active_analysis_state = analysis_state
                active_data_scope = data_scope
                conversation.active_analysis_state = analysis_state
                conversation.active_data_scope = data_scope
                break
    # Always expose the last complete structured state to the semantic router.
    # The router decides whether the new message is a continuation, while the
    # domain scope policy below prevents stale record ids from becoming an
    # implicit filter. This removes phrase/regex gating from multi-turn context.
    prompt_analysis_state = active_analysis_state
    prompt_data_scope = active_data_scope
    active_draft = _clarification_draft(db, conversation)
    workflow_context: dict = {
        "kind": "none",
        "allowedActions": ["route_new_request"],
        "activeDataScope": prompt_data_scope,
        "activeAnalysisState": prompt_analysis_state,
    }
    if active_draft:
        category = TaxonomyRepository(db, user.id).category(active_draft.category_id)
        workflow_context = {
            "kind": "transaction_draft",
            "draftId": str(active_draft.id),
            "state": active_draft.state,
            "missingFields": active_draft.missing_fields,
            "selectedCategory": category.name if category else None,
            "allowedActions": [
                WidgetActionId.SELECT_CATEGORY.value,
                WidgetActionId.SELECT_SUBCATEGORY.value,
                WidgetActionId.CREATE_CATEGORY.value,
                WidgetActionId.CREATE_SUBCATEGORY.value,
                "provide_account",
                "provide_amount",
                "cancel_draft",
                "route_new_request",
            ],
            "activeDataScope": prompt_data_scope,
            "activeAnalysisState": prompt_analysis_state,
        }

    def reject_ungrounded_financial_reply(
        value: CopilotDecision,
        validation,
    ):
        if (
            value.tool is CapabilityId.CONVERSATION
            and not value.tool_grounding
            and value.reply
            and re.search(r"(?:₹|\b(?:inr|rs\.?|rupees?)\b)\s*[\d,]+|\b\d+(?:\.\d+)?\s*%", value.reply, re.I)
        ):
            return CopilotDecisionValidation(
                outcome="reject",
                confidence=1.0,
                issues=["Financial figures require authenticated runtime-tool evidence."],
                summary="Rejected an ungrounded model-authored financial figure.",
            )
        return validation

    def bind_active_scope(value: CopilotDecision) -> CopilotDecision:
        target_query = value.query or (value.query_bundle.base_query if value.query_bundle else None)
        if not target_query or not target_query.use_active_scope or not active_data_scope:
            return value
        ids = []
        for raw_id in active_data_scope.get("entityIds", []):
            try:
                ids.append(UUID(str(raw_id)))
            except ValueError:
                continue
        bound_query = target_query.model_copy(update={"scope_transaction_ids": ids})
        if value.query_bundle:
            return value.model_copy(update={
                "query_bundle": value.query_bundle.model_copy(update={"base_query": bound_query}),
            })
        return value.model_copy(update={"query": bound_query})

    def remove_unrequested_scope(value: CopilotDecision) -> CopilotDecision:
        target_query = value.query or (value.query_bundle.base_query if value.query_bundle else None)
        if not target_query or not target_query.use_active_scope or _references_active_data_scope(text):
            return value
        if activity_callback:
            activity_callback(
                "scope_policy",
                "Removed an unrelated prior result-set scope",
                "completed",
                "domain_policy",
                "This prompt is an independent query, not a refinement of the displayed records",
            )
        unscoped_query = target_query.model_copy(update={"use_active_scope": False, "scope_transaction_ids": []})
        if value.query_bundle:
            return value.model_copy(update={
                "query_bundle": value.query_bundle.model_copy(update={"base_query": unscoped_query}),
            })
        return value.model_copy(update={"query": unscoped_query})

    def normalize_query_contract(value: CopilotDecision) -> CopilotDecision:
        """Repair contradictions visible from the typed schema, never prose."""
        if not value.query or value.query.operation != "rank":
            return value
        if value.query.group_by == "none":
            return value.model_copy(update={
                "query": value.query.model_copy(update={
                    "result_mode": "transaction_list",
                    "limit": 1,
                }),
            })
        redundant_group = any((
            value.query.group_by == "category" and bool(value.query.category_slug),
            value.query.group_by == "subcategory" and bool(value.query.subcategory_slug),
            value.query.group_by == "merchant" and bool(value.query.merchant),
            value.query.group_by == "account" and bool(value.query.account),
        ))
        if not redundant_group:
            return value
        if activity_callback:
            activity_callback(
                "contract_normalization",
                "Resolved a filtered rank to individual records",
                "completed",
                "domain_policy",
                f"A fixed {value.query.group_by} cannot also be the ranking dimension",
            )
        return value.model_copy(update={
            "query": value.query.model_copy(update={
                "result_mode": "transaction_list",
                "group_by": "none",
                "limit": 1,
            }),
        })

    def is_grounded_list_rank_refinement(value: CopilotDecision) -> bool:
        """Validate a list-to-rank transition from persisted typed state."""
        if not value.query or value.query.operation != "rank" or value.query.group_by != "none" or value.query.limit != 1:
            return False
        state = active_analysis_state or {}
        prior_queries = state.get("queries") or [state.get("query") or {}]
        prior = next((item for item in prior_queries if item.get("result_mode") == "transaction_list"), None)
        if not prior:
            return False
        current = value.query.model_dump(mode="json", exclude_none=True)
        scope_fields = (
            "transaction_type", "merchant", "category_slug", "subcategory_slug", "account", "tag",
            "min_amount_minor", "max_amount_minor", "start_date", "end_date",
        )
        return all(current.get(field) == prior.get(field) for field in scope_fields)
    try:
        taxonomy = _agent_taxonomy(db, user)
        runtime_tools = _user_runtime_tools(db, user, _local_today(user))
        if activity_callback:
            activity_callback("retrieval", "Retrieving relevant validated finance capabilities", "running", "semantic_rag", None)
        reusable_tools = discover_analysis_tools(db, user.id, text)
        if activity_callback:
            activity_callback("retrieval", "Retrieved semantic context", "completed", "semantic_rag", f"{len(reusable_tools)} relevant validated plan{'s' if len(reusable_tools) != 1 else ''}")
        settings = get_settings()
        if activity_callback:
            activity_callback("router", f"Routing with {settings.router_model}", "running", "agno_router", None)
        decision = interpret_with_financial_copilot(
            text,
            taxonomy,
            _local_today(user),
            user.timezone,
            recent_context,
            reusable_tools=reusable_tools,
            workflow_context=workflow_context,
            enable_reasoning=enable_reasoning,
            router_model_id=settings.router_model,
            user_id=user.id,
            runtime_tools=runtime_tools,
            user_currency=user.currency,
        )
        if activity_callback:
            activity_callback("router", f"{settings.router_model} produced a typed decision", "completed", "agno_router", decision.tool if decision else "No valid decision")
        if not decision:
            return None
        decision = normalize_query_contract(remove_unrequested_scope(decision))
        if decision.tool in {CapabilityId.CONVERSATION, CapabilityId.VISUALIZE_COMPUTATION} and decision.tool_grounding:
            # CopilotRouteDecision cannot manufacture this field. It is added
            # only after Agno reports a successful call to one of this run's
            # authenticated read-only tools, so no second model should reroute
            # the already-grounded answer back into an analysis workflow.
            if activity_callback:
                activity_callback(
                    "validator",
                    "Verified authenticated runtime tool execution",
                    "completed",
                    "runtime_tool_policy",
                    ", ".join(item.name for item in decision.tool_grounding),
                )
            return decision.model_copy(update={
                "validated_by": "runtime_tool_policy",
                "validation_confidence": 1.0,
            })
        if activity_callback:
            activity_callback("validator", f"Validating with {settings.validator_model}", "running", "agno_validator", None)
        validation = validate_copilot_decision(
            text,
            decision,
            _local_today(user),
            user.timezone,
            workflow_context,
            recent_context,
        )
        validation = reject_ungrounded_financial_reply(decision, validation)
        validation_label = validation.outcome.replace("_", " ") if validation else "unavailable"
        if activity_callback:
            activity_callback("validator", f"{settings.validator_model}: {validation_label}", "completed", "agno_validator", validation.summary if validation else "Validator unavailable")
        if validation and validation.outcome in ACCEPTED_COPILOT_VALIDATION_OUTCOMES:
            decision = bind_active_scope(decision)
            return decision.model_copy(update={"validated_by": settings.validator_model, "validation_confidence": validation.confidence})
        if validation and is_grounded_list_rank_refinement(decision) and not _references_active_data_scope(text):
            if activity_callback:
                activity_callback(
                    "state_transition_policy",
                    "Validated a grounded list-to-rank refinement",
                    "completed",
                    "domain_policy",
                    "The prior filters and period are unchanged; ranking runs against current canonical records",
                )
            return decision.model_copy(update={
                "validated_by": "domain_state_transition_policy",
                "validation_confidence": 1.0,
            })
        if (
            validation
            and validation.repairs == ["bind_active_scope"]
            and decision.query
            and not decision.query.use_active_scope
            and not _references_active_data_scope(text)
        ):
            # The domain layer owns result-set scope. A model validator cannot
            # turn an independent query into a refinement of stale records.
            if activity_callback:
                activity_callback(
                    "scope_policy",
                    "Rejected an invalid prior-result scope repair",
                    "completed",
                    "domain_policy",
                    "The prompt is an independent query and requires no prior entity IDs",
                )
            return decision.model_copy(update={
                "validated_by": "domain_scope_policy",
                "validation_confidence": validation.confidence,
            })
        if (
            validation
            and validation.repairs == ["bind_active_scope"]
            and active_data_scope
            and decision.query
            and _references_active_data_scope(text)
        ):
            if activity_callback:
                activity_callback("contract_repair", "Binding the grounded result-set scope", "running", "domain_repair", None)
            decision = decision.model_copy(update={
                "query": decision.query.model_copy(update={"use_active_scope": True}),
            })
            if activity_callback:
                activity_callback("contract_repair", "Bound the grounded result-set scope", "completed", "domain_repair", f"{active_data_scope.get('entityCount', 0)} transaction IDs")
                activity_callback("repair_validation", f"Revalidating repaired contract with {settings.validator_model}", "running", "agno_validator", None)
            repaired_validation = validate_copilot_decision(
                text,
                decision,
                _local_today(user),
                user.timezone,
                workflow_context,
                recent_context,
            )
            if activity_callback:
                repair_label = repaired_validation.outcome.replace("_", " ") if repaired_validation else "unavailable"
                activity_callback("repair_validation", f"{settings.validator_model}: {repair_label}", "completed", "agno_validator", repaired_validation.summary if repaired_validation else "Validator unavailable")
            if repaired_validation and repaired_validation.outcome in ACCEPTED_COPILOT_VALIDATION_OUTCOMES:
                decision = bind_active_scope(decision)
                return decision.model_copy(update={"validated_by": settings.validator_model, "validation_confidence": repaired_validation.confidence})
        if validation:
            if activity_callback:
                activity_callback("reroute", f"Rerouting with {settings.analysis_model}", "running", "agno_reroute", "The fast validator rejected the first semantic contract")
            repair_context = {
                **workflow_context,
                "decisionRepair": {
                    "rejectedDecision": decision.model_dump(mode="json", exclude_none=True),
                    "validatorOutcome": validation.model_dump(mode="json", exclude_none=True),
                    "instruction": "Produce a corrected typed decision that resolves every validator issue without dropping valid filters, dates, direction, or result scope.",
                },
            }
            stronger = interpret_with_financial_copilot(
                text,
                taxonomy,
                _local_today(user),
                user.timezone,
                recent_context,
                reusable_tools=reusable_tools,
                workflow_context=repair_context,
                enable_reasoning=enable_reasoning,
                router_model_id=settings.analysis_model,
                user_id=user.id,
                runtime_tools=runtime_tools,
                user_currency=user.currency,
            )
            if activity_callback:
                activity_callback("reroute", f"{settings.analysis_model} produced a revised decision", "completed", "agno_reroute", stronger.tool if stronger else "No valid decision")
            if not stronger:
                if decision.tool in SAFE_READ_CAPABILITIES:
                    return CopilotDecision(
                        tool=CapabilityId.UNKNOWN,
                        reply="I couldn’t validate that read-only analysis yet. No financial record was created or changed.",
                        confidence=1.0,
                        reason="The analysis contract was rejected and cannot fall through to a write workflow.",
                    )
                return None
            stronger = normalize_query_contract(remove_unrequested_scope(stronger))
            if activity_callback:
                activity_callback("revalidation", f"Revalidating with {settings.validator_model}", "running", "agno_validator", None)
            second = validate_copilot_decision(
                text,
                stronger,
                _local_today(user),
                user.timezone,
                workflow_context,
                recent_context,
            )
            second = reject_ungrounded_financial_reply(stronger, second)
            second_label = second.outcome.replace("_", " ") if second else "unavailable"
            if activity_callback:
                activity_callback("revalidation", f"{settings.validator_model}: {second_label}", "completed", "agno_validator", second.summary if second else "Validator unavailable")
            if not second or second.outcome == "reject":
                if decision.tool in SAFE_READ_CAPABILITIES:
                    return CopilotDecision(
                        tool=CapabilityId.UNKNOWN,
                        reply="I couldn’t validate that read-only analysis yet. No financial record was created or changed.",
                        confidence=1.0,
                        reason="The analysis contract was rejected and cannot fall through to a write workflow.",
                    )
                return None
            stronger = bind_active_scope(stronger)
            return stronger.model_copy(update={"validated_by": settings.validator_model, "validation_confidence": second.confidence})
    except Exception as error:
        db.add(AIAction(user_id=user.id, conversation_id=conversation.id, action_type="primary_router", payload_redacted={"errorType": type(error).__name__}, status=ExecutionStatus.FAILED))
        return None
    return bind_active_scope(decision) if decision else None


def _extracted_from_decision(text: str, decision: CopilotDecision, today: date, default_currency: str) -> ExtractedTransaction:
    baseline = extract_transaction(text, today=today, default_currency=default_currency)
    interpreted = decision.transaction
    if not interpreted:
        return baseline
    explicit = set(interpreted.explicit_fields)
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
    return ExtractedTransaction(
        transaction_type=transaction_type,
        amount_minor=amount_minor,
        currency=interpreted.currency.upper() if interpreted.currency else baseline.currency,
        merchant=merchant,
        source_account=interpreted.source_account or baseline.source_account,
        destination_account=interpreted.destination_account or baseline.destination_account,
        transaction_date=transaction_date,
        category_slug=interpreted.category_slug or baseline.category_slug,
        subcategory_slug=interpreted.subcategory_slug or baseline.subcategory_slug,
        transaction_time=interpreted.transaction_time or baseline.transaction_time,
        timezone=interpreted.timezone or baseline.timezone,
        location_label=location_label,
        tags=tags,
        spend_nature=spend_nature,
        explicit_fields=list(explicit),
        confidence=Decimal(str(interpreted.confidence)),
        inferred_fields=inferred_fields,
    )


def _conversation_response(db: Session, conversation: Conversation, content: str) -> AgentResponse:
    return persist_agent_response(db, conversation, content)


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


def _tool_grounded_response(db: Session, user: User, conversation: Conversation, decision: CopilotDecision) -> AgentResponse:
    """Persist a grounded answer together with non-sensitive provenance."""
    content = decision.reply
    if decision.tool_grounding:
        item = decision.tool_grounding[0]
        parsed = _parsed_tool_result(item) or {}
        raw_result = _tool_result_data(item)
        if item.name == "read_user_expense_taxonomy" and isinstance(raw_result, list):
            names = [str(category.get("name")) for category in raw_result if isinstance(category, dict) and category.get("name")]
            content = (
                f"You have {len(names)} expense categor{'y' if len(names) == 1 else 'ies'}: "
                f"{', '.join(names)}."
            )
        elif item.name == "loan_payment" and parsed.get("emi_minor") is not None:
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
        elif not content and parsed.get("summary") and isinstance(parsed["summary"], dict):
            content = f"I completed the {item.name.replace('_', ' ')} calculation using the supplied inputs."
        elif not content:
            content = "I found the requested information from the authenticated financial tool."
    citations = [_tool_reference(item) for item in decision.tool_grounding]
    return persist_agent_response(db, conversation, content, citations=citations)


def _computed_visualization_response(
    db: Session,
    user: User,
    conversation: Conversation,
    decision: CopilotDecision,
) -> AgentResponse:
    """Render any authenticated computed dataset through one visual grammar."""
    grounded = next((
        (item, parsed)
        for item in decision.tool_grounding
        if (parsed := _parsed_tool_result(item)) and parsed.get("kind") == "computed_dataset"
    ), None)
    if not grounded:
        return _conversation_response(
            db,
            conversation,
            "I couldn’t obtain a validated calculation dataset for that visual. No financial record was changed.",
        )
    item, dataset = grounded
    dataset["currency"] = dataset.get("currency") or user.currency
    rows = dataset.get("rows") or []
    fields = dataset.get("fields") or []
    if not isinstance(rows, list) or not rows or not isinstance(fields, list):
        return _conversation_response(db, conversation, "The calculation returned no rows to visualize.")
    field_catalog = {
        field.get("name"): field
        for field in fields
        if isinstance(field, dict) and isinstance(field.get("name"), str)
    }
    dimension = decision.presentation.x_field or dataset.get("default_dimension")
    measures = decision.presentation.y_fields or list(dataset.get("default_measures") or [])
    if dimension not in field_catalog or not measures or any(name not in field_catalog for name in measures):
        return _conversation_response(
            db,
            conversation,
            "I couldn’t bind the requested axes to validated calculation fields. No financial record was changed.",
        )
    if field_catalog[dimension].get("role") != "dimension" or any(
        field_catalog[name].get("role") != "measure" for name in measures
    ):
        return _conversation_response(db, conversation, "The requested calculation fields are not valid chart axes.")

    x_field = field_catalog[dimension]
    mark = decision.presentation.requested_mark
    if mark == "auto":
        mark = "line" if x_field.get("type") in ORDERED_VISUAL_FIELD_TYPES else "bar"
    if mark == "arc":
        mark = "bar"
    x_encoding = VisualEncoding(
        field=dimension,
        type=x_field.get("type", "ordinal"),
        title=x_field.get("label") or dimension.replace("_", " ").title(),
        value_type=x_field.get("value_type", "number"),
        sort="ascending",
    )
    if len(measures) == 1:
        measure = field_catalog[measures[0]]
        render_rows = rows
        y_encoding = VisualEncoding(
            field=measures[0],
            type="quantitative",
            title=measure.get("label") or measures[0].replace("_", " ").title(),
            value_type=measure.get("value_type", "number"),
        )
        color_encoding = None
        tooltip = [x_encoding, y_encoding]
    else:
        # A generic wide-to-long projection supports any set of returned
        # measures without frontend code generation or a calculator-specific
        # chart renderer.
        render_rows = [
            {
                dimension: row.get(dimension),
                "measure": field_catalog[measure].get("label") or measure.replace("_", " ").title(),
                "value": row.get(measure),
            }
            for row in rows
            for measure in measures
        ]
        y_encoding = VisualEncoding(
            field="value",
            type="quantitative",
            title="Amount",
            value_type=field_catalog[measures[0]].get("value_type", "number"),
        )
        color_encoding = VisualEncoding(
            field="measure", type="nominal", title="Measure", value_type="category"
        )
        tooltip = [x_encoding, color_encoding, y_encoding]

    dataset_name = str(dataset.get("name") or item.name)
    visualization = VisualizationSpec(
        name=f"{dataset_name} visual",
        query_name=dataset_name,
        mark=mark,
        encoding=VisualEncodingSet(
            x=x_encoding,
            y=y_encoding,
            color=color_encoding,
            tooltip=tooltip,
        ),
        title=str(dataset.get("title") or item.name.replace("_", " ").title()),
        rationale="The chart uses only fields returned by the authenticated deterministic calculator.",
    )
    widget = WidgetLibrary.data_visualization(
        widget_id=f"computed-visualization-{uuid4()}",
        title=str(dataset.get("title") or "Calculated analysis"),
        body=str(dataset.get("description") or "Deterministic calculation results."),
        datasets={dataset_name: render_rows},
        visualizations=[visualization],
        query_results={dataset_name: {
            "name": dataset_name,
            "rows": rows,
            "summary": dataset.get("summary") or {},
        }},
    )
    labels = [field_catalog[name].get("label") or name.replace("_", " ") for name in measures]
    dimension_label = str(x_field.get("label") or dimension.replace("_", " ")).lower()
    unit_label = dimension_label if len(rows) == 1 else (
        dimension_label if dimension_label.endswith("s") else f"{dimension_label}s"
    )
    content = (
        f"Here is {', '.join(labels).lower()} across all {len(rows)} {unit_label}."
    )
    citations = [_tool_reference(item)]
    return persist_agent_response(
        db,
        conversation,
        content,
        widgets=[widget],
        citations=citations,
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
    widget = _taxonomy_editor_widget(taxonomy.operation, taxonomy.name, parent, draft)
    if taxonomy.operation is WidgetActionId.CREATE_SUBCATEGORY:
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
) -> AgentResponse:
    proposal = decision.analysis_tool
    if proposal and proposal.plan.missing_information:
        missing = proposal.plan.missing_information
        content = "I need " + ", ".join(missing[:-1]) + ((" and " + missing[-1]) if len(missing) > 1 else missing[0]) + " before I can answer that without guessing."
        widget = Widget(
            id=f"analysis-inputs-{uuid4()}",
            type=WidgetType.INSIGHT_CARD,
            data={
                "eyebrow": "More information needed",
                "title": "I won’t invent missing financial inputs",
                "body": content,
            },
        )
        return persist_agent_response(db, conversation, content, widgets=[widget])
    try:
        generated = execute_generated_tool(
            db,
            user.id,
            conversation.id,
            _local_today(user),
            proposal,
            decision.reuse_tool_id,
            harness_callback,
        )
    except HarnessValidationError:
        content = "I couldn’t validate a safe analysis plan for that request. Please add the missing period or financial input and I’ll try again."
        return persist_agent_response(db, conversation, content)
    result = generated.result
    widgets = result.widgets
    return persist_agent_response(
        db,
        conversation,
        result.message,
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


def _is_bare_amount(text: str) -> bool:
    return bool(re.fullmatch(
        r"\s*(?:(?:₹|rs\.?|inr)\s*)?[\d,]+(?:\.\d+)?(?:\s*(?:k|thousand|lakh|lac|crore))?\s*",
        text,
        re.I,
    )) and parse_amount_minor(text) is not None


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
        "projection", "optimize", "three months", "last 3 months", "last three months",
    ))


def _references_active_data_scope(text: str) -> bool:
    """Whether the prompt explicitly refines the previously displayed records.

    This is a scope safety invariant, not an intent router: the agent still
    decides the query. The domain layer only prevents an unrelated new request
    from accidentally inheriting a prior result-set boundary.
    """
    return bool(re.search(
        r"\b(?:those|these|them|shown|above|previous|same|only|just)\b"
        r"|\bthe\s+(?:transactions|records|expenses|results|list)\b"
        r"|\bwhich\s+(?:of|one)\b",
        text,
        re.I,
    ))


def _references_prior_analysis(text: str) -> bool:
    """Whether the current turn explicitly depends on the prior analysis.

    Definite nouns such as "the transactions" are not sufficient: users often
    use them in a complete, independent request. Inheritance is reserved for
    actual anaphora or continuation language so a fresh chart cannot silently
    acquire stale dates, filters, metrics or direction semantics.
    """
    return bool(re.search(
        r"\b(?:those|these|them|that|same|shown|above|previous|earlier|former|latter)\b"
        r"|^\s*(?:and|also|now|then|instead)\b"
        r"|\b(?:what|how)\s+about\b",
        text,
        re.I,
    ))


def _fast_path_decision(text: str, today: date, default_currency: str | None = None) -> tuple[CopilotDecision, ExtractedTransaction | None] | None:
    """Resolve only unambiguous intents; everything else remains agent-routed."""
    if re.fullmatch(r"\s*(?:hi|hello|hey|good\s+(?:morning|afternoon|evening))[!. ]*", text, re.I):
        return CopilotDecision(
            tool=CapabilityId.CONVERSATION,
            reply="Hi! Tell me what happened financially, or ask me anything about your money.",
            confidence=1,
            reason="Unambiguous greeting handled locally.",
            safe_reasoning_summary=["Recognized a greeting", "No financial tool or model call is needed"],
        ), None
    if re.fullmatch(r"\s*(?:thanks|thank you|okay|ok|got it|great)[!. ]*", text, re.I):
        return CopilotDecision(
            tool=CapabilityId.CONVERSATION,
            reply="You’re welcome. What would you like to look at next?",
            confidence=1,
            reason="Unambiguous acknowledgement handled locally.",
            safe_reasoning_summary=["Recognized an acknowledgement", "No financial state needs to change"],
        ), None
    extracted = extract_transaction(
        text,
        today=today,
        default_currency=default_currency or get_settings().default_currency,
    )
    if _is_bare_amount(text):
        return CopilotDecision(
            tool=CapabilityId.CREATE_TRANSACTION_DRAFT,
            confidence=0.75,
            reason="Bare amount uses the minimal clarification workflow.",
            safe_reasoning_summary=["Detected a standalone amount", "Start a transaction draft and ask only for missing classification"],
        ), extracted
    if (
        extracted.amount_minor is not None
        and extracted.transaction_type != TransactionType.UNKNOWN
        and not looks_like_financial_query(text)
        and ("transaction_type" in extracted.explicit_fields or (_is_amount_led_shorthand(text) and extracted.category_slug is not None))
    ):
        return CopilotDecision(
            tool=CapabilityId.CREATE_TRANSACTION_DRAFT,
            confidence=float(extracted.confidence),
            reason="A complete explicit financial event can use deterministic extraction.",
            safe_reasoning_summary=["Detected a financial event", "Validate the structured draft", "Apply the existing auto-save policy"],
        ), extracted
    if looks_like_financial_query(text) and not _needs_deep_reasoning(text):
        lowered = text.casefold()
        if "recurring" in lowered or "subscription" in lowered:
            tool = CapabilityId.GET_RECURRING_EXPENSES
        elif "biggest" in lowered or "largest expense" in lowered:
            tool = CapabilityId.GET_BIGGEST_EXPENSES
        elif "saved analys" in lowered:
            tool = CapabilityId.SHOW_SAVED_ANALYSES
        elif "duplicate" in lowered or "reconciliation" in lowered or "need review" in lowered:
            tool = CapabilityId.SHOW_RECONCILIATION_REVIEW
        elif any(token in lowered for token in ("how much", "how many rupees", "summary", "breakdown", "total", "spent this", "spending this")):
            # A composite read asks for more than a scalar summary. It must go
            # through the typed query-bundle planner so the row and aggregate
            # views cannot lose each other's scope.
            if re.search(r"\b(?:table|rows?|records?|transactions?|list)\b", lowered):
                return None
            if re.search(r"\b(?:earn|earned|earning|earnings|income|salary|credit|credited)\b", lowered):
                return None
            # Merchant/account qualifiers require semantic extraction so their
            # filters are never collapsed into a generic monthly total.
            if re.search(r"\b(?:at|from|using|via)\b", lowered):
                return None
            tool = CapabilityId.GET_SPENDING_SUMMARY
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
    """Compile established analysis grammars; novel requests continue to Agno."""
    lowered = text.casefold()
    today = _local_today(user)
    if "avoidable" in lowered or "unnecessary expense" in lowered:
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
            "travelling": DefaultCategorySlug.TRANSPORT,
            "traveling": DefaultCategorySlug.TRANSPORT,
            "travel": DefaultCategorySlug.TRANSPORT,
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
            return None
        if any(token in lowered for token in ("last three months", "last 3 months", "three months", "3 months")):
            start = shift_month(today.replace(day=1), -2)
        else:
            parsed = parse_spending_period(text, today)
            start = parsed[0] if parsed else today.replace(day=1)
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
    else:
        return None
    proposal = AnalysisToolProposal(
        name=name,
        description=f"Governed capability compiled for: {intent}.",
        intent_signature=intent,
        plan=plan,
    )
    return CopilotDecision(
        tool=CapabilityId.RUN_ANALYSIS_HARNESS,
        analysis_tool=proposal,
        safe_reasoning_summary=plan.safe_reasoning_summary,
        confidence=1,
        reason="A known analysis grammar compiled directly to the governed tool protocol.",
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
            "date": transaction.transaction_date.isoformat(),
            "status": "Confirm removal",
            "inferredFields": [],
        },
        actions=[
            WidgetAction(id="remove", label="Remove transaction", action=WidgetActionId.CONFIRM_REMOVE_TRANSACTION, style="primary", payload={"transactionId": str(transaction.id)}),
            WidgetAction(id="cancel", label="Cancel", action=WidgetActionId.CANCEL_REMOVE_TRANSACTION, style="secondary", payload={"transactionId": str(transaction.id)}),
        ],
    )


def _transaction_table_widget(
    db: Session,
    user_id: UUID,
    transactions: list[Transaction],
    *,
    title: str,
    body: str | None = None,
    action_mode: str = "manage",
    widget_id: str | None = None,
) -> Widget:
    """Present transactions from their real field shape and authorized actions."""
    category_ids = {item.category_id for item in transactions if item.category_id}
    subcategory_ids = {item.subcategory_id for item in transactions if item.subcategory_id}
    account_ids = {item.account_id for item in transactions if item.account_id}
    taxonomy = TaxonomyRepository(db, user_id)
    categories = {
        item_id: item.name
        for item_id, item in taxonomy.categories_by_id(category_ids).items()
    }
    subcategories = {
        item_id: item.name
        for item_id, item in taxonomy.subcategories_by_id(subcategory_ids).items()
    }
    accounts = {
        item_id: item.name
        for item_id, item in UserScopedRepository(db, user_id).by_ids(Account, account_ids).items()
    }
    transaction_ids = [item.id for item in transactions]
    tags_by_transaction: dict[UUID, list[str]] = {}
    if transaction_ids:
        tag_rows = db.execute(
            select(TransactionTag.transaction_id, Tag.name)
            .join(Tag, Tag.id == TransactionTag.tag_id)
            .where(TransactionTag.transaction_id.in_(transaction_ids))
            .order_by(Tag.name)
        )
        for transaction_id, tag_name in tag_rows:
            tags_by_transaction.setdefault(transaction_id, []).append(tag_name)

    if action_mode == "review_remove":
        capabilities = ["transaction.remove"]
        actions = (
            RowCapability(
                id="review-remove",
                label="Review removal",
                action=WidgetActionId.REQUEST_REMOVE_TRANSACTION,
                payload_key="transactionId",
                style="danger",
                icon="review",
                capability="transaction.remove",
            ),
        )
    elif action_mode == "manage":
        capabilities = ["transaction.edit", "transaction.remove"]
        actions = (
            RowCapability(
                id="edit",
                label="Edit",
                action=WidgetActionId.EDIT_SAVED_TRANSACTION,
                payload_key="transactionId",
                icon="edit",
                capability="transaction.edit",
            ),
            RowCapability(
                id="remove",
                label="Remove",
                action=WidgetActionId.REQUEST_REMOVE_TRANSACTION,
                payload_key="transactionId",
                style="danger",
                icon="remove",
                capability="transaction.remove",
            ),
        )
    else:
        capabilities = []
        actions = ()

    rows = [{
        "id": str(item.id),
        "merchant": item.merchant_name or item.transaction_type.replace("_", " ").title(),
        "transactionType": item.transaction_type.replace("_", " ").title(),
        "category": categories.get(item.category_id),
        "subcategory": subcategories.get(item.subcategory_id),
        "account": accounts.get(item.account_id),
        "location": item.location_label,
        "tags": tags_by_transaction.get(item.id, []),
        "date": item.transaction_date.isoformat(),
        "status": item.status,
        "amountMinor": item.amount_minor,
        "currency": item.currency,
        "_capabilities": capabilities,
    } for item in transactions]
    blueprint = TableBlueprint(
        fields=(
            FieldPresentation("merchant", "Transaction", "entity", "primary", secondary_keys=("transactionType",)),
            FieldPresentation("category", "Category", "text", "secondary", secondary_keys=("subcategory",)),
            FieldPresentation("account", "Account", "text", "detail"),
            FieldPresentation("location", "Location", "text", "detail"),
            FieldPresentation("tags", "Tags", "tags", "detail"),
            FieldPresentation("date", "Date", "date", "secondary"),
            FieldPresentation("status", "Status", "status", "detail"),
            FieldPresentation("amountMinor", "Amount", "money", "primary", "right", "currency"),
        ),
        row_actions=actions,
        empty_message="No matching transactions.",
    )
    return WidgetLibrary.data_table(
        widget_id=widget_id or WidgetLibrary.generated_id("transaction-table"),
        title=title,
        body=body,
        rows=rows,
        blueprint=blueprint,
    )


def _comparison_table_widget(result: dict, *, title: str) -> Widget:
    """Present legacy deterministic comparisons without owning a chart choice.

    Visual selection belongs to the analysis plan. This fallback stays useful
    and exact while avoiding a bespoke two-period chart contract.
    """
    current = result["current"]
    previous = result["previous"]

    def period_row(label: str, period: dict) -> dict:
        return {
            "period": label,
            "startDate": period["start"],
            "endDate": period["end"],
            "amountMinor": period["total_minor"],
            "transactions": period["count"],
            "currency": period["currency"],
        }

    rows = [
        period_row("Previous period", previous),
        period_row("Current period", current),
    ]
    return WidgetLibrary.data_table(
        widget_id=WidgetLibrary.generated_id("comparison-table"),
        title=title,
        body="The periods use the same elapsed-day window for a fair comparison.",
        rows=rows,
        blueprint=TableBlueprint(fields=(
            FieldPresentation("period", "Period", "entity", "primary", secondary_keys=("startDate", "endDate")),
            FieldPresentation("amountMinor", "Spent", "money", "primary", "right", "currency"),
            FieldPresentation("transactions", "Transactions", "number", "secondary", "right"),
        )),
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
        .order_by(Transaction.transaction_date.desc(), Transaction.created_at.desc())
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
        matches = [item for item in matches if period[0] <= item.transaction_date <= period[1]]
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
        content = f"I found {len(matches)} matching {label.title() if label else ''} transaction{'s' if len(matches) != 1 else ''}. Choose the one you want to review for removal."
        widgets = [_transaction_table_widget(
            db,
            user.id,
            shown,
            title="Choose a transaction to remove",
            body="Selecting Review removal will still require a final confirmation.",
            action_mode="review_remove",
            widget_id=f"remove-list-{uuid4()}",
        )]
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
        stmt = stmt.where(Transaction.transaction_date >= query.start_date)
    if query.end_date:
        stmt = stmt.where(Transaction.transaction_date <= min(query.end_date, _local_today(user)))
    filtered_ids = stmt.with_only_columns(Transaction.id).order_by(None).subquery()
    individual_rank = query.operation == "rank" and query.group_by == "none"
    if query.result_mode == "summary" and not individual_rank:
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
        group_label = func.coalesce(Category.name, "Other")
        group_join = (Category, Category.id == Transaction.category_id)
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
            group_label = func.substr(func.cast(Transaction.transaction_date, String), 1, 7)
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
        period = f"{resolved_start.strftime('%b %d') if resolved_start else 'Beginning'} – {resolved_end.strftime('%b %d')}"
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
        widgets = [Widget(id=f"transaction-summary-{uuid4()}", type=WidgetType.FINANCIAL_SUMMARY, data={
            "title": f"{'Highest' if query.sort_direction == 'desc' else 'Lowest'} {query.group_by.replace('_', ' ')} spend" if query.operation == "rank" else summary_title,
            "amountMinor": int(grouped[0].amount) if query.operation == "rank" and grouped else int(total_minor),
            "currency": user.currency,
            "count": int(grouped[0].count) if query.operation == "rank" and grouped else count,
            "period": period,
            "periodTitle": period_title,
            "scopePath": scope_path,
            "scopeLabel": scope_label or query.merchant,
            "description": content,
            "breakdown": breakdown_rows,
        })]
        citations = [DataReference(
            label="Filtered canonical transaction summary",
            entity_type="transaction",
            entity_ids=[],
            query=query.model_dump(mode="json", exclude_none=True),
        )]
        return content, widgets, citations
    if individual_rank:
        amount_order = Transaction.amount_minor.asc() if query.sort_direction == "asc" else Transaction.amount_minor.desc()
        stmt = stmt.order_by(amount_order, Transaction.transaction_date.desc(), Transaction.created_at.desc())
    else:
        stmt = stmt.order_by(Transaction.transaction_date.desc(), Transaction.created_at.desc())
    effective_limit = 1 if individual_rank else query.limit
    fetched = list(db.scalars(stmt.limit(effective_limit + 1)))
    has_more = len(fetched) > effective_limit
    transactions = fetched[:effective_limit]
    record_kind = f"{query.transaction_type.replace('_', ' ')} " if query.transaction_type else ""
    qualifiers = [
        f"at {query.merchant}" if query.merchant else None,
        f"in {query.category_slug}" if query.category_slug else None,
        f"under {query.subcategory_slug}" if query.subcategory_slug else None,
        f"tagged {query.tag}" if query.tag else None,
    ]
    qualifier_label = " ".join(item for item in qualifiers if item)
    filter_label = f"{record_kind}transactions{(' ' + qualifier_label) if qualifier_label else ''}"
    if individual_rank and transactions:
        rank_kind = "lowest" if query.sort_direction == "asc" else "highest"
        item = transactions[0]
        content = f"The {rank_kind} matching transaction is {format_money_minor(item.amount_minor, item.currency)} at {item.merchant_name or item.transaction_type.replace('_', ' ')} on {item.transaction_date.strftime('%b %d')}."
    elif transactions:
        count_label = f"at least {len(transactions)}" if has_more else str(len(transactions))
        content = f"I found {count_label} active {filter_label}."
    else:
        content = f"I found no active {filter_label}."
    list_title = (("Lowest" if query.sort_direction == "asc" else "Highest") + " matching transaction") if individual_rank else "Matching transactions"
    widgets = [_transaction_table_widget(
        db,
        user.id,
        transactions,
        title=list_title,
        body="Results come from canonical, non-deleted financial records.",
        widget_id=f"transaction-search-{uuid4()}",
    )]
    citations = [DataReference(
        label="Matching canonical transactions",
        entity_type="transaction",
        entity_ids=[str(item.id) for item in transactions],
        query=query.model_dump(mode="json", exclude_none=True),
    )]
    return content, widgets, citations


def _transaction_search_response(db: Session, user: User, conversation: Conversation, decision: CopilotDecision) -> AgentResponse:
    """Execute and persist one tenant-scoped transaction query."""
    if not decision.query:
        return _conversation_response(db, conversation, "I couldn’t resolve the requested transaction filters safely. Please clarify what records you want to see.")
    content, widgets, citations = _transaction_search_parts(db, user, decision.query)
    return persist_agent_response(
        db,
        conversation,
        content,
        widgets=widgets,
        citations=citations,
    )


def _query_bundle_response(db: Session, user: User, conversation: Conversation, decision: CopilotDecision) -> AgentResponse:
    """Compile several views over one authoritative transaction filter scope."""
    bundle = decision.query_bundle
    if not bundle:
        return _conversation_response(db, conversation, "I couldn’t resolve the requested data views safely. Please clarify what you want to compare or display.")

    base_query = bundle.base_query
    if bundle.refresh_from_active_analysis:
        prior_state = conversation.active_analysis_state or {}
        prior_queries = prior_state.get("queries") or [prior_state.get("query") or {}]
        prior_list = next((item for item in prior_queries if item.get("result_mode") == "transaction_list"), None)
        if prior_list:
            allowed = set(QueryInterpretation.model_fields)
            normalized = {key: value for key, value in prior_list.items() if key in allowed}
            try:
                base_query = QueryInterpretation.model_validate(normalized).model_copy(update={
                    "use_active_scope": False,
                    "scope_transaction_ids": [],
                })
            except ValueError:
                pass

    view_ids = [view.id for view in bundle.views]
    if len(view_ids) != len(set(view_ids)):
        return _conversation_response(db, conversation, "I couldn’t safely render duplicate data views. Please retry the analysis.")

    bundle_id = str(uuid4())
    rendered: list[tuple[str, list[Widget], list[DataReference]]] = []
    for view in bundle.views:
        query = base_query.model_copy(update={
            "result_mode": view.result_mode,
            "operation": view.operation,
            "group_by": view.group_by,
            "sort_direction": view.sort_direction,
            "limit": view.limit,
            "use_active_scope": False if bundle.refresh_from_active_analysis else base_query.use_active_scope,
            "scope_transaction_ids": [] if bundle.refresh_from_active_analysis else base_query.scope_transaction_ids,
        })
        content, widgets, citations = _transaction_search_parts(db, user, query)
        for citation in citations:
            citation.query = {
                **citation.query,
                "bundle_id": bundle_id,
                "view_id": view.id,
                "refresh_from_active_analysis": bundle.refresh_from_active_analysis,
            }
        rendered.append((content, widgets, citations))

    # Interactive record views perform the HITL responsibility first. The
    # grounded read-only conclusion closes the business response.
    rendered.sort(key=lambda item: 1 if item[1] and item[1][0].type is WidgetType.FINANCIAL_SUMMARY else 0)
    widgets = [widget for _, view_widgets, _ in rendered for widget in view_widgets]
    citations = [citation for _, _, view_citations in rendered for citation in view_citations]
    summary_content = next((content for content, view_widgets, _ in rendered if view_widgets and view_widgets[0].type is WidgetType.FINANCIAL_SUMMARY), None)
    list_content = next((content for content, view_widgets, _ in rendered if view_widgets and view_widgets[0].type is WidgetType.DATA_TABLE), None)
    if summary_content and list_content:
        content = f"I refreshed the same records using your previous filters. {summary_content}" if bundle.refresh_from_active_analysis else summary_content
    else:
        content = summary_content or list_content or "I found no matching financial records."
    return persist_agent_response(
        db,
        conversation,
        content,
        widgets=widgets,
        citations=citations,
    )


def _query_response(db: Session, user: User, conversation: Conversation, text: str, decision: CopilotDecision | None = None) -> AgentResponse:
    lowered = text.lower()
    today = _local_today(user)
    start, end = month_bounds(today)
    selected_tool = decision.tool if decision else None
    citations: list[DataReference] = []
    widgets: list[Widget] = []

    def selected(capability: CapabilityId, fallback_match: bool) -> bool:
        """A typed route is authoritative; text matching is outage fallback only."""
        return selected_tool == capability if selected_tool is not None else fallback_match

    if "save this" in lowered:
        previous = db.scalar(select(Message).where(Message.conversation_id == conversation.id, Message.role == "assistant", *_history_only()).order_by(Message.created_at.desc()))
        if previous and previous.widgets:
            widget = previous.widgets[-1]
            analysis = SavedAnalysis(user_id=user.id, title=str(widget.get("data", {}).get("title", "Saved financial analysis")), analysis_type=str(widget.get("type", "analysis")), parameters={}, result=widget)
            db.add(analysis)
            db.flush()
            content = f"Saved “{analysis.title}” to your analyses."
            widgets = [Widget(id=f"saved-analysis-{analysis.id}", type=WidgetType.INSIGHT_CARD, data={"eyebrow": "Saved analysis", "title": analysis.title, "body": "You can ask me to show this analysis again at any time."})]
        else:
            content = "There isn’t an analysis to save yet."
            widgets = []
    elif selected(CapabilityId.SHOW_SAVED_ANALYSES, "saved analys" in lowered):
        analyses = list(db.scalars(select(SavedAnalysis).where(SavedAnalysis.user_id == user.id).order_by(SavedAnalysis.updated_at.desc()).limit(10)))
        content = (
            f"You have {len(analyses)} saved {'analysis' if len(analyses) == 1 else 'analyses'}."
            if analyses
            else "You don’t have any saved analyses yet. Ask me to create a chart, comparison, or scenario, then say “Save this analysis.”"
        )
        widgets = [WidgetLibrary.data_table(
            widget_id=f"saved-list-{datetime.now().timestamp()}",
            title="Saved analyses",
            rows=[{"id": str(item.id), "title": item.title, "analysisType": item.analysis_type, "updatedAt": item.updated_at.isoformat()} for item in analyses],
            body="Saved charts, comparisons, and scenarios appear here and can be reopened in conversation.",
            blueprint=TableBlueprint(
                fields=(
                    FieldPresentation("title", "Analysis", "entity", "primary", secondary_keys=("analysisType",)),
                    FieldPresentation("updatedAt", "Updated", "datetime", "secondary"),
                ),
                empty_message="No saved analyses yet. Generate an analysis in the conversation, then say “Save this analysis.”",
            ),
        )]
    elif selected(CapabilityId.SHOW_RECONCILIATION_REVIEW, any(token in lowered for token in ("duplicate", "reconciliation", "need review"))):
        candidate = db.scalar(select(ReconciliationCandidate).where(ReconciliationCandidate.user_id == user.id, ReconciliationCandidate.decision == ReconciliationOutcome.NEEDS_REVIEW).order_by(ReconciliationCandidate.score.desc()))
        if not candidate:
            content = "There are no ambiguous transactions waiting for review."
            widgets = [Widget(id=f"review-clear-{datetime.now().timestamp()}", type=WidgetType.INSIGHT_CARD, data={"eyebrow": "Reconciliation", "title": "All clear", "body": "Every imported observation is either matched or recorded separately."})]
        else:
            owned = UserScopedRepository(db, user.id)
            observation = owned.get(FinancialObservation, candidate.observation_id)
            transaction = owned.get(Transaction, candidate.transaction_id)
            if observation is None or transaction is None:
                raise ValueError("Reconciliation candidate references unavailable records")
            content = "I found a possible duplicate. I won’t merge it without your decision."
            widgets = [Widget(
                id=f"reconcile-{candidate.id}",
                type=WidgetType.RECONCILIATION_REVIEW,
                data={"candidateId": str(candidate.id), "title": "Possible duplicate", "score": float(candidate.score), "incoming": {"amountMinor": observation.amount_minor, "currency": observation.currency, "merchant": observation.merchant_raw, "date": observation.transaction_date.isoformat(), "source": observation.source_type}, "existing": {"transactionId": str(transaction.id), "amountMinor": transaction.amount_minor, "currency": transaction.currency, "merchant": transaction.merchant_name, "date": transaction.transaction_date.isoformat(), "sourceCount": len(transaction.sources)}, "signals": candidate.matching_signals},
                actions=[WidgetAction(id="merge", label="Same transaction", action=WidgetActionId.MERGE_RECONCILIATION, style="primary", payload={"candidateId": str(candidate.id)}), WidgetAction(id="separate", label="Keep separate", action=WidgetActionId.SEPARATE_RECONCILIATION, payload={"candidateId": str(candidate.id)})],
            )]
    elif selected(CapabilityId.GET_RECURRING_EXPENSES, "recurring" in lowered or "subscription" in lowered):
        recurring = recurring_expenses(db, user.id)
        content = f"I found {len(recurring)} recurring expense pattern{'s' if len(recurring) != 1 else ''}." if recurring else "I don’t have enough repeated transactions to identify a recurring expense yet."
        widgets = [WidgetLibrary.data_table(
            widget_id=f"recurring-{datetime.now().timestamp()}",
            title="Recurring expenses",
            rows=[{
                "id": item["id"],
                "merchant": item["merchant"],
                "cadence": item["cadence"],
                "occurrences": item["occurrences"],
                "lastDate": item["last_date"],
                "amountMinor": item["amount_minor"],
                "currency": item["currency"],
            } for item in recurring],
            blueprint=TableBlueprint(
                fields=(
                    FieldPresentation("merchant", "Merchant", "entity", "primary", secondary_keys=("cadence",)),
                    FieldPresentation("occurrences", "Occurrences", "number", "secondary", "right"),
                    FieldPresentation("lastDate", "Last seen", "date", "secondary"),
                    FieldPresentation("amountMinor", "Typical amount", "money", "primary", "right", "currency"),
                ),
                empty_message="No recurring expense patterns yet.",
            ),
        )]
        citations = [DataReference(label="Repeated merchant transactions", entity_type="transaction", query={"patterns": len(recurring)})]
    elif selected(CapabilityId.CALCULATE_LOAN, any(token in lowered for token in ("prepay", "interest save", "emi"))):
        content = "I can calculate this exactly, but I still need the outstanding principal, annual interest rate, and remaining tenure."
        widgets = [Widget(id=f"loan-{datetime.now().timestamp()}", type=WidgetType.LOAN_CALCULATOR, data={"title": "Home-loan prepayment", "body": "Add the loan principal, rate, and remaining months to compare the baseline with a prepayment.", "prepaymentMinor": extract_transaction(text, default_currency=user.currency).amount_minor, "currency": user.currency}, actions=[WidgetAction(id="calculate", label="Calculate", action=WidgetActionId.CALCULATE_LOAN_SCENARIO, style="primary")])]
    elif selected(CapabilityId.CALCULATE_INVESTMENT_PROJECTION, "sip" in lowered or "investment projection" in lowered):
        content = "I can project the change deterministically once you choose a time horizon and expected annual return."
        widgets = [Widget(id=f"investment-{datetime.now().timestamp()}", type=WidgetType.INVESTMENT_PROJECTION, data={"title": "Investment projection", "body": "The result will separate your contributions from estimated returns and state the return assumption.", "monthlyContributionMinor": extract_transaction(text, default_currency=user.currency).amount_minor, "currency": user.currency}, actions=[WidgetAction(id="calculate", label="Project", action=WidgetActionId.CALCULATE_INVESTMENT_SCENARIO, style="primary")])]
    elif selected(CapabilityId.GET_CHANGE_DRIVERS, "why" in lowered and ("spend" in lowered or "expensive" in lowered)):
        result = change_drivers(db, user.id, today)
        difference = result["difference_minor"]
        if difference > 0:
            lead = next((item for item in result["drivers"] if item["change_minor"] > 0), None)
            content = f"You’ve spent {format_money_minor(difference, result['current']['currency'])} more than the same point last month."
            if lead:
                content += f" {lead['label']} is the largest increase at {format_money_minor(lead['change_minor'], result['current']['currency'])}."
        elif difference < 0:
            content = f"You’ve spent {format_money_minor(abs(difference), result['current']['currency'])} less than the same point last month."
        else:
            content = "Your spending is unchanged from the same point last month."
        widgets = [_comparison_table_widget(result, title="This month vs last month")]
        citations = [DataReference(label="Expense transactions used in the comparison", entity_type="transaction", query={"current": result["current"], "previous": result["previous"]})]
    elif selected(CapabilityId.GET_MONTHLY_COMPARISON, "compare" in lowered and ("month" in lowered or "july" in lowered or "august" in lowered)):
        result = monthly_comparison(db, user.id, today)
        diff = result["difference_minor"]
        content = f"This month is {format_money_minor(abs(diff), result['current']['currency'])} {'higher' if diff > 0 else 'lower' if diff < 0 else 'different'} than the same point last month."
        widgets = [_comparison_table_widget(result, title="Monthly spending")]
        citations = [DataReference(label="Transactions included", entity_type="transaction", query={"current": result["current"], "previous": result["previous"]})]
    elif selected(CapabilityId.CALCULATE_AFFORDABILITY, "afford" in lowered):
        parsed = extract_transaction(text, default_currency=user.currency)
        purchase_minor = parsed.amount_minor or 20_000_000
        position = cash_position(db, user.id)
        current_month = spending_summary(db, user.id, start, min(today, end))
        result = affordability(purchase_minor, max(position["net_minor"], 0), position["income_minor"], current_month["total_minor"], 6)
        if result["affordable_now"]:
            content = f"Based on the money recorded here, {format_money_minor(purchase_minor, user.currency)} is affordable while preserving a six-month expense reserve."
        else:
            months = result["months_to_goal"]
            content = f"Not safely yet based on the records I have. You’re {format_money_minor(result['gap_minor'], user.currency)} short after keeping a six-month expense reserve."
            if months:
                content += f" At your recorded surplus, that’s about {months} month{'s' if months != 1 else ''}."
        widgets = [Widget(id=f"scenario-{datetime.now().timestamp()}", type=WidgetType.SCENARIO_ANALYSIS, data={"title": f"Can I afford {format_money_minor(purchase_minor, user.currency)}?", "currency": user.currency, **result, "dataQuality": "Based only on recorded transactions"})]
        citations = [DataReference(label="Recorded income and expenses", entity_type="transaction", query={"position": position, "month": current_month})]
    elif selected(CapabilityId.GET_BIGGEST_EXPENSES, "biggest" in lowered):
        transactions = list(db.scalars(
            expense_transactions(user.id, currency=user.currency)
            .order_by(Transaction.amount_minor.desc())
            .limit(10)
        ))
        content = "Here are your biggest recorded expenses." if transactions else "You don’t have any recorded expenses yet."
        widgets = [_transaction_table_widget(db, user.id, transactions, title="Biggest expenses", widget_id=f"list-{datetime.now().timestamp()}")]
        citations = [DataReference(label="Largest expense transactions", entity_type="transaction", entity_ids=[str(t.id) for t in transactions])]
    else:
        category_slug = decision.query.category_slug if decision and decision.query else None
        if not category_slug:
            category_slug, _ = infer_expense_category(text)
        category = TaxonomyRepository(db, user.id).category_by_slug(category_slug, expense_only=True) if category_slug else None
        if not category:
            category = next((item for item in _expense_categories_for_user(db, user.id) if item.name.casefold() in lowered), None)
            category_slug = category.slug if category else None
        category_label = category.name if category else None
        explicit_period = parse_spending_period(text, today)
        if decision and decision.query and decision.query.start_date and decision.query.end_date:
            query_start = decision.query.start_date
            query_end = min(decision.query.end_date, today)
            if query_start > query_end:
                query_start, query_end = start, min(today, end)
            period_title = _period_title(query_start, query_end, today)
        else:
            query_start, query_end, period_title = explicit_period or (start, min(today, end), "This month")
        result = spending_summary(db, user.id, query_start, query_end, category_slug)
        breakdown = subcategory_breakdown(db, user.id, query_start, query_end, category_slug) if category_slug else category_breakdown(db, user.id, query_start, query_end)
        label = f" on {category_label.lower()}" if category_label else ""
        period_phrase = period_title.lower()
        content = f"You’ve spent {format_money_minor(result['total_minor'], result['currency'])}{label} {period_phrase} across {result['count']} transaction{'s' if result['count'] != 1 else ''}."
        widgets = [Widget(id=f"summary-{datetime.now().timestamp()}", type=WidgetType.FINANCIAL_SUMMARY, data={"title": f"{(category_label or 'Spending')} · {period_title}", "amountMinor": result["total_minor"], "currency": result["currency"], "count": result["count"], "period": f"{query_start.strftime('%b %d')} – {query_end.strftime('%b %d')}", "breakdown": breakdown})]
        citations = [DataReference(label="Expense transactions included", entity_type="transaction", query=result)]

    return persist_agent_response(
        db,
        conversation,
        content,
        widgets=widgets,
        citations=citations,
    )


def _dispatch_decision(
    db: Session,
    user: User,
    conversation: Conversation,
    text: str,
    decision: CopilotDecision,
    execute: Callable[[str, str, Callable[[], AgentResponse]], AgentResponse],
    emit: Callable[[str, str, str, str | None, str | None, str | None], None],
    *,
    extracted: ExtractedTransaction | None = None,
) -> AgentResponse:
    """Execute every routed capability through its one registry-owned executor."""
    spec = capability_spec(decision.tool)
    capability = spec.id

    if spec.executor is ExecutorKind.CONVERSATION:
        if decision.tool_grounding:
            operation = lambda: _tool_grounded_response(db, user, conversation, decision)
        else:
            operation = lambda: _conversation_response(
                db,
                conversation,
                decision.reply or "Hi! Tell me what happened financially, or ask me anything about your money.",
            )
    elif spec.executor is ExecutorKind.UNKNOWN:
        operation = lambda: _conversation_response(
            db,
            conversation,
            decision.reply or "I’m not sure what you want me to do yet. You can record a financial event or ask me a question about your recorded finances.",
        )
    elif spec.executor is ExecutorKind.DRAFT:
        resolved = extracted or _extracted_from_decision(
            text, decision, _local_today(user), user.currency
        )

        def operation() -> AgentResponse:
            draft = _create_draft(db, user, conversation, text, resolved)
            return _draft_or_commit(db, user, conversation, draft)
    elif spec.executor is ExecutorKind.REMOVAL:
        operation = lambda: _transaction_removal_response(db, user, conversation, text)
    elif spec.executor is ExecutorKind.TAXONOMY:
        operation = lambda: _taxonomy_response(db, user, conversation, decision)
    elif spec.executor is ExecutorKind.PLANNING:
        operation = lambda: _planning_response(db, user, conversation, text)
    elif spec.executor is ExecutorKind.COMPUTED_VISUAL:
        operation = lambda: _computed_visualization_response(db, user, conversation, decision)
    elif spec.executor is ExecutorKind.BUNDLE:
        operation = lambda: _query_bundle_response(db, user, conversation, decision)
    elif spec.executor is ExecutorKind.QUERY:
        operation = (
            (lambda: _transaction_search_response(db, user, conversation, decision))
            if capability is CapabilityId.SEARCH_TRANSACTIONS
            else (lambda: _query_response(db, user, conversation, text, decision))
        )
    elif spec.executor is ExecutorKind.HARNESS:
        operation = lambda: _analysis_harness_response(
            db,
            user,
            conversation,
            decision,
            lambda stage, label, status, detail: emit(
                stage,
                label,
                status,
                capability.value,
                detail,
                _analysis_lifecycle_badge(stage, label, status),
            ),
        )
    else:  # Exhaustiveness is enforced by CapabilitySpec.ExecutorKind.
        raise RuntimeError(f"No executor is registered for {capability.value}")

    return execute(capability.value, spec.execution_label, operation)


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
) -> AgentResponse:
    """Runs one conversational turn, question and answer as a single unit."""
    with _reply_reservation(db):
        return _run_turn(db, user, conversation, text, activity_callback)


def _run_turn(
    db: Session,
    user: User,
    conversation: Conversation,
    text: str,
    activity_callback: ActivityCallback | None = None,
) -> AgentResponse:
    run_started = perf_counter()
    stage_started: dict[str, float] = {}

    def emit(
        stage: str,
        label: str,
        status: ExecutionStatus | str,
        tool: str | None = None,
        detail: str | None = None,
        badge: str | None = None,
    ) -> None:
        if not activity_callback:
            return
        status = ExecutionStatus(status)
        now = perf_counter()
        if status is ExecutionStatus.RUNNING:
            stage_started[stage] = now
            duration_ms = 0.0
        else:
            duration_ms = round((now - stage_started.get(stage, now)) * 1000, 1)
        activity_callback(AgentActivityEvent(
            id=stage,
            label=label,
            status=status,
            tool=tool,
            detail=detail,
            badge=badge,
            duration_ms=duration_ms,
            cumulative_ms=round((now - run_started) * 1000, 1),
        ).model_dump(mode="json", by_alias=True))

    def execute(tool: str, label: str, operation: Callable[[], AgentResponse]) -> AgentResponse:
        emit("execution", label, "running", tool)
        response = operation()
        emit("execution", label, "completed", tool)
        emit("grounding", "Grounding response in structured state", "running", tool)
        source_count = len(response.citations)
        emit(
            "grounding",
            "Grounded response",
            "completed",
            tool,
            f"{source_count} structured data source{'s' if source_count != 1 else ''}" if source_count else "No financial figures generated",
        )
        return response

    user_message = Message(conversation_id=conversation.id, role="user", content=text, widgets=[], citations=[])
    db.add(user_message)
    if conversation.title == "Financial check-in":
        conversation.title = text[:54] + ("…" if len(text) > 54 else "")
    db.flush()
    # The answer's place in the transcript is decided here, with the question,
    # rather than whenever the model happens to finish. Two turns in flight at
    # once can then only finish out of order, not read out of order.
    _reserve_reply(db, conversation)
    emit("request", "Request received", "completed")

    # Typed text can also answer an outstanding category question.
    active_draft = _clarification_draft(db, conversation)
    if active_draft and active_draft.missing_fields:
        emit("classification", "Resumed transaction workflow", ExecutionStatus.COMPLETED, WidgetActionId.UPDATE_TRANSACTION_DRAFT.value)
        answer = text.strip().lower()
        if active_draft.missing_fields[0] == "category":
            category = next((item for item in _expense_categories_for_user(db, user.id) if item.name.casefold() == answer.casefold()), None)
            if category:
                active_draft.category_id = category.id
                _set_ready_if_complete(active_draft)
                return execute(WidgetActionId.UPDATE_TRANSACTION_DRAFT.value, "Updating transaction draft", lambda: _draft_or_commit(db, user, conversation, active_draft))
        elif active_draft.missing_fields[0] == "subcategory":
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

    fast_path = _fast_path_decision(text, _local_today(user), user.currency)
    settings = get_settings()
    if (
        fast_path
        and settings.primary_agent_enabled
        and settings.openai_api_key
        and fast_path[0].tool is not CapabilityId.CONVERSATION
        and not _is_bare_amount(text)
    ):
        # In LLM mode only greetings/acknowledgements and the deliberately
        # ambiguous bare-amount workflow bypass semantic validation. Financial
        # questions and complete events are always agent-routed first.
        fast_path = None
    if fast_path:
        decision, extracted = fast_path
        emit(
            "classification",
            "Fast intent gate selected a validated path",
            "completed",
            decision.tool,
            " → ".join(decision.safe_reasoning_summary),
        )
        db.add(AIAction(
            user_id=user.id,
            conversation_id=conversation.id,
            action_type="fast_router",
            payload_redacted={"tool": decision.tool, "confidence": decision.confidence},
            status=ExecutionStatus.COMPLETED,
        ))
        return _dispatch_decision(
            db, user, conversation, text, decision, execute, emit, extracted=extracted
        )

    deep_reasoning = _needs_deep_reasoning(text)
    emit(
        "classification",
        "Agno is reasoning and planning" if deep_reasoning else "Agno is routing the request",
        "running",
        "agno_reasoning" if deep_reasoning else "agno_router",
    )
    decision = _interpret_prompt(db, user, conversation, user_message, text, deep_reasoning, emit)
    model_tool = decision.tool if decision else None
    override_detail = None
    if _is_bare_amount(text) and (not decision or decision.tool in {CapabilityId.CONVERSATION, CapabilityId.UNKNOWN}):
        decision = CopilotDecision(
            tool=CapabilityId.CREATE_TRANSACTION_DRAFT,
            confidence=max(decision.confidence if decision else 0.0, 0.7),
            reason="Bare currency amounts enter the minimal transaction clarification workflow.",
        )
        override_detail = f"Domain guardrail corrected {model_tool or 'no route'} → create_transaction_draft"
    if decision:
        payload = {"tool": decision.tool, "confidence": decision.confidence}
        if decision.query:
            payload["queryShape"] = {
                "metric": decision.query.metric,
                "resultMode": decision.query.result_mode,
                "operation": decision.query.operation,
                "groupBy": decision.query.group_by,
                "sortDirection": decision.query.sort_direction,
                "limit": decision.query.limit,
                "usesActiveScope": decision.query.use_active_scope,
                "filterFields": [key for key, value in {
                    "transactionType": decision.query.transaction_type,
                    "merchant": decision.query.merchant,
                    "category": decision.query.category_slug,
                    "subcategory": decision.query.subcategory_slug,
                    "account": decision.query.account,
                    "tag": decision.query.tag,
                    "minimumAmount": decision.query.min_amount_minor,
                    "maximumAmount": decision.query.max_amount_minor,
                    "startDate": decision.query.start_date,
                    "endDate": decision.query.end_date,
                }.items() if value is not None],
            }
        if decision.validated_by:
            payload.update({"validatedBy": decision.validated_by, "validationConfidence": decision.validation_confidence})
        if decision.tool_grounding:
            payload["groundedTools"] = [item.name for item in decision.tool_grounding]
        if model_tool and model_tool != decision.tool:
            payload["modelTool"] = model_tool
        db.add(AIAction(user_id=user.id, conversation_id=conversation.id, action_type="primary_router", payload_redacted=payload, status=ExecutionStatus.COMPLETED))
    reasoning_detail = None
    if decision and decision.safe_reasoning_summary:
        reasoning_detail = " → ".join(decision.safe_reasoning_summary)
        if decision.validated_by:
            reasoning_detail += f" → Validated by {decision.validated_by} ({round((decision.validation_confidence or 0) * 100)}%)"
    emit(
        "classification",
        ("Agno completed its reasoning plan" if deep_reasoning else "Agno selected a route") if decision else "Deterministic fallback selected",
        "completed",
        decision.tool if decision else "deterministic_fallback",
        override_detail or (
            f"{reasoning_detail} · Confidence {round(decision.confidence * 100)}%"
            if reasoning_detail
            else (f"Confidence {round(decision.confidence * 100)}%" if decision else "The model was unavailable or returned no valid route")
        ),
    )
    if decision:
        return _dispatch_decision(db, user, conversation, text, decision, execute, emit)

    # This compiler is a model-outage fallback. It never runs before Agno in
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
            db, user, conversation, text, compiled_analysis, execute, emit
        )

    # Safe deterministic fallback for local/offline use and transient model failures.
    if re.fullmatch(r"\s*(?:hi|hello|hey|good\s+(?:morning|afternoon|evening))[!. ]*", text, re.I):
        fallback_decision = CopilotDecision(
            tool=CapabilityId.CONVERSATION,
            reply="Hi! Tell me what happened financially, or ask me anything about your recorded finances.",
            confidence=1,
            reason="Offline greeting fallback.",
        )
        return _dispatch_decision(db, user, conversation, text, fallback_decision, execute, emit)
    lowered = text.casefold()
    if "sip" in lowered or "investment projection" in lowered:
        fallback_decision = CopilotDecision(tool=CapabilityId.CALCULATE_INVESTMENT_PROJECTION, confidence=0.7, reason="Offline deterministic calculator fallback.")
        return _dispatch_decision(db, user, conversation, text, fallback_decision, execute, emit)
    if _looks_like_planning_command(text):
        fallback_decision = CopilotDecision(tool=CapabilityId.PLANNING, confidence=0.7, reason="Offline planning fallback.")
        return _dispatch_decision(db, user, conversation, text, fallback_decision, execute, emit)
    if looks_like_financial_query(text):
        fallback_decision = CopilotDecision(
            tool=CapabilityId.UNKNOWN,
            reply="I understood this as a financial query, but I couldn’t safely resolve all requested filters. I won’t return an unrelated total; please restate the records or summary you want.",
            confidence=0.5,
            reason="Offline query guardrail.",
        )
        return _dispatch_decision(db, user, conversation, text, fallback_decision, execute, emit)

    extracted = extract_transaction(text, today=_local_today(user), default_currency=user.currency)
    if extracted.transaction_type == TransactionType.UNKNOWN and extracted.amount_minor is None:
        fallback_decision = CopilotDecision(tool=CapabilityId.UNKNOWN, reply="I can help record expenses or income, analyze your spending, or plan a financial goal. What would you like to do?", confidence=0.5, reason="Offline clarification fallback.")
        return _dispatch_decision(db, user, conversation, text, fallback_decision, execute, emit)
    if "transaction_type" not in extracted.explicit_fields and not _is_bare_amount(text):
        fallback_decision = CopilotDecision(tool=CapabilityId.UNKNOWN, reply="I couldn’t safely determine whether you want to create or change a financial record. Please tell me the action you want, and I won’t modify anything until it is clear.", confidence=0.5, reason="Offline mutation guardrail.")
        return _dispatch_decision(db, user, conversation, text, fallback_decision, execute, emit)
    fallback_decision = CopilotDecision(tool=CapabilityId.CREATE_TRANSACTION_DRAFT, confidence=0.7, reason="Offline transaction fallback.")
    return _dispatch_decision(
        db, user, conversation, text, fallback_decision, execute, emit, extracted=extracted
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
    normalized = normalize_merchant(draft.merchant_name)
    if normalized:
        merchant = MerchantRepository(db, user.id).get_or_create(draft.merchant_name, normalized)
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
        transaction_date=draft.transaction_date,
        transaction_time=draft.transaction_time,
        timezone=draft.timezone,
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
        transaction_date=draft.transaction_date,
        description=draft.description,
        observed_at=datetime.now(timezone.utc),
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
        "transaction_date": transaction.transaction_date.isoformat(),
        "transaction_time": transaction.transaction_time,
        "timezone": transaction.timezone,
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
        # Agno memory is an interpretation aid only. The category IDs above
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
    owned = UserScopedRepository(db, user.id)
    draft_id = payload.get("draftId")
    draft = owned.get(TransactionDraft, UUID(draft_id)) if draft_id else None
    if draft and draft.conversation_id != conversation.id:
        draft = None
    taxonomy = TaxonomyRepository(db, user.id)
    if action is WidgetActionId.SET_SPEND_NATURE:
        transaction_id = payload.get("transactionId")
        transaction = active_transaction(db, user.id, UUID(str(transaction_id))) if transaction_id else None
        spend_nature = str(payload.get("spendNature") or "")
        if not transaction:
            raise ValueError("Unknown transaction")
        transaction.spend_nature = spend_nature
        label = "potentially avoidable" if spend_nature == SpendNature.POTENTIALLY_AVOIDABLE else spend_nature
        content = f"Marked the {format_money_minor(transaction.amount_minor, transaction.currency)} {transaction.merchant_name or 'expense'} transaction as {label}."
        widget = _transaction_preview(db, transaction, status="Updated")
        return persist_agent_response(db, conversation, content, widgets=[widget])
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
        category = taxonomy.create_category(name, "circle-ellipsis", f"custom-{uuid4().hex}")
        subcategory = taxonomy.create_subcategory(category, "Other", "other")
        if draft:
            draft.category_id = category.id
            draft.subcategory_id = subcategory.id
            draft.inferred_fields = _without_inferred_fields(draft.inferred_fields, TAXONOMY_INFERENCE_FIELDS)
            _set_ready_if_complete(draft)
            return _draft_or_commit(db, user, conversation, draft)
        return _conversation_response(db, conversation, f"Added {category.name} to your categories.")
    if action is WidgetActionId.CREATE_SUBCATEGORY:
        name = str(payload.get("name") or "").strip()
        category_id = payload.get("categoryId")
        category = taxonomy.category(UUID(str(category_id)), expense_only=True) if category_id else None
        if not name or len(name) > 80:
            raise ValueError("Subcategory name must be between 1 and 80 characters")
        if not category:
            raise ValueError("Unknown parent category")
        existing = next((item for item in _subcategories_for_user(db, user.id, category.id) if item.name.casefold() == name.casefold()), None)
        if not existing:
            existing = taxonomy.create_subcategory(category, name, f"custom-{uuid4().hex}")
        if draft:
            if draft.category_id != category.id:
                raise ValueError("Subcategory does not belong to the draft category")
            draft.subcategory_id = existing.id
            draft.inferred_fields = _without_inferred_fields(draft.inferred_fields, SUBCATEGORY_INFERENCE_FIELDS)
            _set_ready_if_complete(draft)
            return _draft_or_commit(db, user, conversation, draft)
        return _conversation_response(db, conversation, f"Added {existing.name} under {category.name}.")
    if action is WidgetActionId.SELECT_CATEGORY and draft:
        category = taxonomy.category(UUID(payload["categoryId"]))
        if not category:
            raise ValueError("Unknown category")
        draft.category_id = category.id
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
        subcategory = taxonomy.subcategory(UUID(payload["subcategoryId"]), category_id=draft.category_id)
        if not subcategory:
            raise ValueError("Unknown subcategory")
        draft.subcategory_id = subcategory.id
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
        amount_minor = payload.get("amountMinor")
        category_id = UUID(str(payload["categoryId"])) if payload.get("categoryId") else None
        category = taxonomy.category(category_id, expense_only=True)
        if category_id and not category:
            raise ValueError("Unknown category")
        budget = db.scalar(select(Budget).where(Budget.user_id == user.id, Budget.category_id == category_id))
        if budget:
            budget.amount_minor = amount_minor
            budget.name = str(payload.get("name") or budget.name)
        else:
            budget = Budget(user_id=user.id, category_id=category_id, name=str(payload.get("name") or "Monthly spending budget"), amount_minor=amount_minor, currency=user.currency)
            db.add(budget)
            db.flush()
        today = _local_today(user)
        start, end = month_bounds(today)
        spent = spending_summary(db, user.id, start, min(today, end), category.slug if category else None)["total_minor"]
        content = f"Set your {budget.name.lower()} to {format_money_minor(budget.amount_minor, budget.currency)} per month."
        widget = _budget_widget(str(budget.id), budget.name, budget.amount_minor, spent, category.slug if category else None, budget.currency)
        return persist_agent_response(db, conversation, content, widgets=[widget])
    if action is WidgetActionId.SAVE_GOAL:
        target_minor = payload.get("targetMinor")
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
        amount_minor = payload.get("amountMinor")
        if not goal:
            raise ValueError("Unknown goal")
        goal.current_minor += amount_minor
        content = f"Added {format_money_minor(amount_minor, goal.currency)} to your {goal.name} goal."
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
        widget = Widget(id=f"loan-result-{datetime.now().timestamp()}", type=WidgetType.LOAN_CALCULATOR, data={"title": "Home-loan prepayment result", "principalMinor": principal_minor, "annualRatePercent": rate, "tenureMonths": months, "prepaymentMinor": prepayment_minor, "currency": user.currency, "result": result})
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
        widget = Widget(id=f"investment-result-{datetime.now().timestamp()}", type=WidgetType.INVESTMENT_PROJECTION, data={"title": "Investment projection result", "monthlyContributionMinor": monthly_minor, "currentValueMinor": current_minor, "annualReturnPercent": rate, "years": years, "currency": user.currency, "result": result})
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
            data={"draftId": str(draft.id), "title": "Edit transaction", "amountMinor": draft.amount_minor, "currency": draft.currency, "merchant": draft.merchant_name, "date": draft.transaction_date.isoformat() if draft.transaction_date else None, "fields": ["amount", "merchant", "date"]},
            actions=[WidgetAction(id="update", label="Apply changes", action=WidgetActionId.UPDATE_TRANSACTION_DRAFT, style="primary", payload={"draftId": str(draft.id)})],
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
        if payload.get("date"):
            try:
                draft.transaction_date = date.fromisoformat(str(payload["date"]))
            except ValueError as error:
                raise ValueError("Date must be valid") from error
        _set_ready_if_complete(draft)
        return _draft_response(db, conversation, draft)
    if action is WidgetActionId.EDIT_SAVED_TRANSACTION:
        transaction_id = payload.get("transactionId")
        transaction = active_transaction(db, user.id, UUID(str(transaction_id))) if transaction_id else None
        if not transaction:
            raise ValueError("Unknown transaction")
        categories = _expense_categories_for_user(db, user.id) if transaction.transaction_type == TransactionType.EXPENSE else []
        subcategories = [
            item
            for category in categories
            for item in taxonomy.subcategories(category.id)
        ]
        tags = list(db.scalars(select(Tag.name).join(TransactionTag, TransactionTag.tag_id == Tag.id).where(TransactionTag.transaction_id == transaction.id).order_by(Tag.name)))
        widget = Widget(
            id=f"edit-saved-{transaction.id}-{uuid4()}",
            type=WidgetType.TRANSACTION_EDIT,
            data={"transactionId": str(transaction.id), "title": "Edit saved transaction", "amountMinor": transaction.amount_minor, "currency": transaction.currency, "merchant": transaction.merchant_name, "date": transaction.transaction_date.isoformat(), "transactionType": transaction.transaction_type, "location": transaction.location_label, "spendNature": transaction.spend_nature, "tags": tags, "categoryId": str(transaction.category_id) if transaction.category_id else None, "subcategoryId": str(transaction.subcategory_id) if transaction.subcategory_id else None, "categories": [{"id": str(category.id), "label": category.name} for category in categories], "subcategories": [{"id": str(item.id), "categoryId": str(item.category_id), "label": item.name} for item in subcategories], "fields": ["amount", "merchant", "date", "transaction_type", "location", "spend_nature", "tags", "category", "subcategory"]},
            actions=[
                WidgetAction(id="update", label="Apply changes", action=WidgetActionId.UPDATE_SAVED_TRANSACTION, style="primary", payload={"transactionId": str(transaction.id)}),
                WidgetAction(id="cancel", label="Cancel", action=WidgetActionId.CANCEL_SAVED_TRANSACTION_EDIT, style="secondary", payload={"transactionId": str(transaction.id)}),
            ],
        )
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
        transaction = active_transaction(db, user.id, UUID(str(transaction_id))) if transaction_id else None
        if not transaction:
            raise ValueError("Unknown transaction")
        amount_minor = payload.get("amountMinor")
        transaction.amount_minor = amount_minor
        changed_fields: dict[str, object] = {"amount_minor": amount_minor}
        if "merchant" in payload:
            transaction.merchant_name = str(payload.get("merchant") or "").strip() or None
            changed_fields["merchant"] = transaction.merchant_name
        if payload.get("date"):
            try:
                transaction.transaction_date = date.fromisoformat(str(payload["date"]))
                changed_fields["transaction_date"] = transaction.transaction_date.isoformat()
            except ValueError as error:
                raise ValueError("Date must be valid") from error
        if "transactionType" in payload:
            transaction_type = str(payload.get("transactionType") or "")
            transaction.transaction_type = transaction_type
            changed_fields["transaction_type"] = transaction_type
        if "location" in payload:
            location = str(payload.get("location") or "").strip()[:160] or None
            transaction.location_label = location
            transaction.location_source = "user" if location else None
            changed_fields["location"] = location
        if "spendNature" in payload:
            spend_nature = str(payload.get("spendNature") or SpendNature.UNKNOWN)
            transaction.spend_nature = spend_nature
            changed_fields["spend_nature"] = spend_nature
        if payload.get("categoryId"):
            category = taxonomy.category(UUID(str(payload["categoryId"])), expense_only=True)
            if not category:
                raise ValueError("Unknown category")
            transaction.category_id = category.id
            transaction.subcategory_id = None
            changed_fields["category_id"] = str(category.id)
            if payload.get("subcategoryId"):
                subcategory = taxonomy.subcategory(UUID(str(payload["subcategoryId"])), category_id=category.id)
                if not subcategory:
                    raise ValueError("Unknown subcategory")
                transaction.subcategory_id = subcategory.id
                changed_fields["subcategory_id"] = str(subcategory.id)
        if "tags" in payload:
            raw_tags = payload.get("tags")
            tag_names = raw_tags if isinstance(raw_tags, list) else str(raw_tags or "").split(",")
            changed_fields["tags"] = TagRepository(db, user.id).replace_transaction_tags(
                transaction.id,
                tag_names,
                source="user",
                confidence=Decimal("1"),
            )
        normalized = normalize_merchant(transaction.merchant_name)
        if normalized:
            merchant = MerchantRepository(db, user.id).get_or_create(transaction.merchant_name, normalized)
            transaction.merchant_id = merchant.id
            transaction.merchant_name = merchant.canonical_name
        for field_name, value in changed_fields.items():
            db.add(TransactionFieldValue(
                transaction_id=transaction.id,
                field_name=field_name,
                value={"value": value},
                origin="user_correction",
                confidence=Decimal("1"),
                user_confirmed=True,
            ))
        content = f"Updated the {format_money_minor(transaction.amount_minor, transaction.currency)} transaction."
        widget = _transaction_preview(db, transaction, status="Updated")
        return persist_agent_response(db, conversation, content, widgets=[widget])
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
            transaction.deleted_at = datetime.now(timezone.utc)
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
        widget = Widget(id=f"resolved-{candidate_id}", type=WidgetType.TRANSACTION_PREVIEW, data={"transactionId": str(transaction.id), "title": transaction.merchant_name or "Transaction", "amountMinor": transaction.amount_minor, "currency": transaction.currency, "date": transaction.transaction_date.isoformat(), "status": "Reconciliation complete", "sourceCount": len(transaction.sources)})
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
