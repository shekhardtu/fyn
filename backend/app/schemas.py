from __future__ import annotations

from datetime import date as DateValue, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import DEFAULT_CURRENCY
from .domain import CONVERSATION_TITLE_MAX, MAX_TRANSACTION_AMOUNT_MINOR, ExecutionStatus, FinancialSourceType, IdentityProvider, IdentitySource, ImportStatus, MESSAGE_SOURCE_TYPES, OtpChannel, ReconciliationOutcome, SpendNature, TaxonomyOperation, TransactionStatus, TransactionType, ValueEnum, WidgetActionId
from .event_time import as_utc, from_local_parts, now_utc
from .services.tool_models import AffordabilityInput, InvestmentProjectionInput, LoanWithPrepaymentInput
from .visualization_contracts import VisualizationView


class WidgetType(ValueEnum):
    AGENT_ACTIVITY = "agent_activity"
    CLARIFICATION = "clarification"
    CATEGORY_SELECTOR = "category_selector"
    TRANSACTION_TYPE_SELECTOR = "transaction_type_selector"
    SUBCATEGORY_SELECTOR = "subcategory_selector"
    TAXONOMY_EDITOR = "taxonomy_editor"
    ACCOUNT_SELECTOR = "account_selector"
    CONFIRMATION_CARD = "confirmation_card"
    TRANSACTION_PREVIEW = "transaction_preview"
    TRANSACTION_EDIT = "transaction_edit"
    DATA_CHART = "data_chart"
    AVOIDABLE_EXPENSES = "avoidable_expenses"
    INSIGHT_CARD = "insight_card"
    BUDGET_PROGRESS = "budget_progress"
    GOAL_PROGRESS = "goal_progress"
    LOAN_CALCULATOR = "loan_calculator"
    INVESTMENT_PROJECTION = "investment_projection"
    RECONCILIATION_REVIEW = "reconciliation_review"
    IMPORT_REVIEW = "import_review"
    RELATED_QUESTIONS = "related_questions"
    OPERATION_FORM = "operation_form"
    OPERATION_APPROVAL = "operation_approval"


class WidgetLifecycle(ValueEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class WidgetActionStyle(ValueEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    DANGER = "danger"
    GHOST = "ghost"


class WidgetActionIcon(ValueEnum):
    EDIT = "edit"
    REMOVE = "remove"
    VIEW = "view"
    REVIEW = "review"
    DOWNLOAD = "download"
    RETRY = "retry"
    OPEN = "open"


def _camel_case(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.title() for part in tail)


_ACTION_MODEL_CONFIG = ConfigDict(
    populate_by_name=True,
    extra="forbid",
    alias_generator=_camel_case,
)


class ActionPayloadBase(BaseModel):
    model_config = _ACTION_MODEL_CONFIG


class DraftActionPayload(ActionPayloadBase):
    draft_id: UUID = Field(alias="draftId")


class TaxonomyCancelPayload(ActionPayloadBase):
    draft_id: UUID | None = Field(default=None, alias="draftId")
    category_id: UUID | None = Field(default=None, alias="categoryId")


class CreateCategoryPayload(TaxonomyCancelPayload):
    name: str = Field(min_length=1, max_length=80)


class CreateSubcategoryPayload(CreateCategoryPayload):
    category_id: UUID = Field(alias="categoryId")


class CreateTaxonomyPathPayload(ActionPayloadBase):
    name: str = Field(min_length=1, max_length=80)
    subcategories: list[str] = Field(min_length=1, max_length=10)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_category_name(cls, value):
        return " ".join(str(value).split())

    @field_validator("subcategories", mode="before")
    @classmethod
    def normalize_subcategory_names(cls, value):
        if not isinstance(value, list):
            raise ValueError("Subcategories must be a list")
        return [" ".join(str(item).split()) for item in value]

    @model_validator(mode="after")
    def require_unique_subcategories(self):
        if any(not item or len(item) > 80 for item in self.subcategories):
            raise ValueError("Subcategory names must be between 1 and 80 characters")
        normalized = [item.casefold() for item in self.subcategories]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Subcategory names must be unique")
        return self


class SelectCategoryPayload(DraftActionPayload):
    category_id: UUID = Field(alias="categoryId")


class SelectTransactionTypePayload(DraftActionPayload):
    option_id: TransactionType | None = Field(default=None, alias="optionId")
    transaction_type: TransactionType | None = Field(default=None, alias="transactionType")

    @model_validator(mode="after")
    def require_type(self):
        selected = self.option_id or self.transaction_type
        if selected is None or selected is TransactionType.UNKNOWN:
            raise ValueError("Choose a supported transaction type")
        return self


class SelectSubcategoryPayload(DraftActionPayload):
    subcategory_id: UUID = Field(alias="subcategoryId")


class SelectAccountPayload(DraftActionPayload):
    role: Literal["source_account", "destination_account"]
    option_id: UUID | None = Field(default=None, alias="optionId")
    account_id: UUID | None = Field(default=None, alias="accountId")
    account_name: str | None = Field(default=None, alias="accountName", min_length=1, max_length=120)

    @field_validator("account_name", mode="before")
    @classmethod
    def normalize_account_name(cls, value):
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @model_validator(mode="after")
    def require_account(self):
        if self.option_id is None and self.account_id is None and not self.account_name:
            raise ValueError("Choose an account")
        return self


class RevisitTransactionStepPayload(DraftActionPayload):
    step: Literal["transaction_type", "category", "source_account"]


class CancelPendingActionPayload(ActionPayloadBase):
    resource_id: str = Field(alias="resourceId", min_length=1, max_length=64)


class BudgetActionPayload(ActionPayloadBase):
    budget_id: UUID = Field(alias="budgetId")


class TransactionSpendNaturePayload(ActionPayloadBase):
    transaction_id: UUID = Field(alias="transactionId")
    spend_nature: SpendNature = Field(alias="spendNature")


class SaveBudgetPayload(ActionPayloadBase):
    budget_id: UUID | None = Field(default=None, alias="budgetId")
    amount_minor: int = Field(alias="amountMinor", gt=0)
    category_id: UUID | None = Field(default=None, alias="categoryId")
    name: str | None = Field(default=None, max_length=120)


class SaveGoalPayload(ActionPayloadBase):
    target_minor: int = Field(alias="targetMinor", gt=0)
    name: str | None = Field(default=None, max_length=120)


class ContributeGoalPayload(ActionPayloadBase):
    goal_id: UUID = Field(alias="goalId")
    amount_minor: int = Field(alias="amountMinor", gt=0)


class ImportActionPayload(ActionPayloadBase):
    import_id: UUID = Field(alias="importId")


class LoanScenarioActionPayload(LoanWithPrepaymentInput):
    """The calculator input is the sole validation contract for this action."""

    model_config = _ACTION_MODEL_CONFIG


class InvestmentScenarioActionPayload(InvestmentProjectionInput):
    """The calculator input is the sole validation contract for this action."""

    model_config = _ACTION_MODEL_CONFIG


class UpdateDraftPayload(DraftActionPayload):
    amount_minor: int | None = Field(default=None, alias="amountMinor", gt=0)
    merchant: str | None = Field(default=None, max_length=160)
    transaction_at: datetime | None = Field(default=None, alias="transactionAt")

    @field_validator("transaction_at")
    @classmethod
    def utc_transaction_at(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value is not None else None


class TransactionActionPayload(ActionPayloadBase):
    transaction_id: UUID = Field(alias="transactionId")


class EditSavedTransactionPayload(TransactionActionPayload):
    """Optional server-prefilled patch carried by a disambiguation choice."""

    amount_minor: int | None = Field(default=None, alias="amountMinor", gt=0, le=MAX_TRANSACTION_AMOUNT_MINOR)
    merchant: str | None = Field(default=None, max_length=160)
    transaction_date: DateValue | None = Field(default=None, alias="transactionDate")
    transaction_type: TransactionType | None = Field(default=None, alias="transactionType")
    category_slug: str | None = Field(default=None, alias="categorySlug", max_length=120)
    subcategory_slug: str | None = Field(default=None, alias="subcategorySlug", max_length=120)
    location: str | None = Field(default=None, max_length=160)
    spend_nature: SpendNature | None = Field(default=None, alias="spendNature")
    tags: list[str] | None = Field(default=None, max_length=8)


class UpdateSavedTransactionPayload(TransactionActionPayload):
    amount_minor: int = Field(alias="amountMinor", gt=0, le=MAX_TRANSACTION_AMOUNT_MINOR)
    expected_version: int | None = Field(default=None, alias="expectedVersion", ge=1)
    merchant: str | None = Field(default=None, max_length=160)
    transaction_at: datetime | None = Field(default=None, alias="transactionAt")
    transaction_type: TransactionType | None = Field(default=None, alias="transactionType")
    location: str | None = Field(default=None, max_length=160)
    spend_nature: SpendNature | None = Field(default=None, alias="spendNature")
    category_id: UUID | None = Field(default=None, alias="categoryId")
    subcategory_id: UUID | None = Field(default=None, alias="subcategoryId")
    tags: list[str] | str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_accuracy: int | None = Field(default=None, alias="locationAccuracy", ge=0)

    @field_validator("transaction_at")
    @classmethod
    def utc_transaction_at(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value is not None else None


class ReconciliationActionPayload(ActionPayloadBase):
    candidate_id: UUID = Field(alias="candidateId")


class ResolveClarificationPayload(ActionPayloadBase):
    clarification_id: UUID = Field(alias="clarificationId")
    option_id: str = Field(alias="optionId", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    custom_text: str | None = Field(default=None, alias="customText", min_length=1, max_length=1000)


class OperationActionPayload(ActionPayloadBase):
    operation_id: str = Field(alias="operationId", pattern=r"^[a-z][a-z0-9_.-]{1,99}$")
    operation_version: int = Field(alias="operationVersion", ge=1)
    operation_checksum: str = Field(alias="operationChecksum", pattern=r"^[a-f0-9]{64}$")
    inputs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("inputs")
    @classmethod
    def bounded_inputs(cls, value: dict[str, Any]) -> dict[str, Any]:
        import json
        if len(json.dumps(value, default=str)) > 50_000:
            raise ValueError("Operation inputs exceed the supported size")
        return value


class ConversationRenameIn(BaseModel):
    """User-chosen thread title; the bound is the column's own limit."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=CONVERSATION_TITLE_MAX)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Title must contain visible characters")
        return normalized


ACTION_PAYLOAD_MODELS: dict[WidgetActionId, type[BaseModel]] = {
    WidgetActionId.SET_SPEND_NATURE: TransactionSpendNaturePayload,
    WidgetActionId.START_ADD_CATEGORY: DraftActionPayload,
    WidgetActionId.START_ADD_SUBCATEGORY: DraftActionPayload,
    WidgetActionId.CANCEL_ADD_CATEGORY: DraftActionPayload,
    WidgetActionId.CANCEL_TAXONOMY_CHANGE: TaxonomyCancelPayload,
    WidgetActionId.CREATE_CATEGORY: CreateCategoryPayload,
    WidgetActionId.CREATE_SUBCATEGORY: CreateSubcategoryPayload,
    WidgetActionId.CREATE_TAXONOMY_PATH: CreateTaxonomyPathPayload,
    WidgetActionId.SELECT_CATEGORY: SelectCategoryPayload,
    WidgetActionId.SELECT_TRANSACTION_TYPE: SelectTransactionTypePayload,
    WidgetActionId.SELECT_SUBCATEGORY: SelectSubcategoryPayload,
    WidgetActionId.CHANGE_CATEGORY: DraftActionPayload,
    WidgetActionId.SELECT_ACCOUNT: SelectAccountPayload,
    WidgetActionId.REVISIT_TRANSACTION_STEP: RevisitTransactionStepPayload,
    WidgetActionId.CANCEL_TRANSACTION_DRAFT: DraftActionPayload,
    WidgetActionId.CANCEL_PENDING_ACTION: CancelPendingActionPayload,
    WidgetActionId.EDIT_BUDGET: BudgetActionPayload,
    WidgetActionId.REQUEST_DELETE_BUDGET: BudgetActionPayload,
    WidgetActionId.DELETE_BUDGET: BudgetActionPayload,
    WidgetActionId.SAVE_BUDGET: SaveBudgetPayload,
    WidgetActionId.SAVE_GOAL: SaveGoalPayload,
    WidgetActionId.CONTRIBUTE_GOAL: ContributeGoalPayload,
    WidgetActionId.COMMIT_IMPORT: ImportActionPayload,
    WidgetActionId.CALCULATE_LOAN_SCENARIO: LoanScenarioActionPayload,
    WidgetActionId.CALCULATE_INVESTMENT_SCENARIO: InvestmentScenarioActionPayload,
    WidgetActionId.COMMIT_TRANSACTION: DraftActionPayload,
    WidgetActionId.EDIT_TRANSACTION: DraftActionPayload,
    WidgetActionId.CANCEL_TRANSACTION_EDIT: DraftActionPayload,
    WidgetActionId.UPDATE_TRANSACTION_DRAFT: UpdateDraftPayload,
    WidgetActionId.EDIT_SAVED_TRANSACTION: EditSavedTransactionPayload,
    WidgetActionId.CANCEL_SAVED_TRANSACTION_EDIT: TransactionActionPayload,
    WidgetActionId.UPDATE_SAVED_TRANSACTION: UpdateSavedTransactionPayload,
    WidgetActionId.REQUEST_REMOVE_TRANSACTION: TransactionActionPayload,
    WidgetActionId.CONFIRM_REMOVE_TRANSACTION: TransactionActionPayload,
    WidgetActionId.CANCEL_REMOVE_TRANSACTION: TransactionActionPayload,
    WidgetActionId.MERGE_RECONCILIATION: ReconciliationActionPayload,
    WidgetActionId.SEPARATE_RECONCILIATION: ReconciliationActionPayload,
    WidgetActionId.RESOLVE_CLARIFICATION: ResolveClarificationPayload,
    # The REST rename contract doubles as the HITL action payload so the title
    # rule exists exactly once.
    WidgetActionId.RENAME_CONVERSATION: ConversationRenameIn,
    WidgetActionId.SUBMIT_OPERATION: OperationActionPayload,
    WidgetActionId.APPROVE_OPERATION: OperationActionPayload,
    WidgetActionId.CANCEL_OPERATION: OperationActionPayload,
}

if set(ACTION_PAYLOAD_MODELS) != set(WidgetActionId):
    raise RuntimeError("Every widget action must have exactly one payload model")


def validate_action_payload(action: WidgetActionId | str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one submitted action at the shared boundary."""
    action_id = WidgetActionId(action)
    validated = ACTION_PAYLOAD_MODELS[action_id].model_validate(payload)
    return validated.model_dump(mode="json", by_alias=True, exclude_unset=True)


class WidgetAction(BaseModel):
    id: str
    label: str
    action: WidgetActionId
    style: WidgetActionStyle = WidgetActionStyle.SECONDARY
    payload: dict[str, Any] = Field(default_factory=dict)


class WidgetDataBase(BaseModel):
    """Fields shared by all durable widget payloads after an action resolves."""

    lifecycle: WidgetLifecycle | None = None
    completion: dict[str, Any] | None = None
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class ClarificationOptionData(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=240)


class ClarificationData(WidgetDataBase):
    clarification_id: UUID = Field(alias="clarificationId")
    title: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=3, max_length=500)
    reason: str = Field(min_length=3, max_length=500)
    conflict_fields: list[str] = Field(default_factory=list, alias="conflictFields", max_length=8)
    options: list[ClarificationOptionData] = Field(default_factory=list, max_length=6)
    allow_custom: bool = Field(default=False, alias="allowCustom")
    custom_label: str | None = Field(default=None, alias="customLabel", max_length=100)

    @model_validator(mode="after")
    def validate_response_choices(self) -> ClarificationData:
        if len(self.options) >= 2:
            return self
        if not self.options and self.allow_custom:
            return self
        raise ValueError("clarification requires at least two options or custom input")


class ChartLineage(BaseModel):
    """Provenance every rendered chart must carry: which governed origin drew
    it, under which source manifest, and when it executed."""

    origin: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    manifest_hash: str = Field(alias="manifestHash", pattern=r"^[a-f0-9]{64}$")
    # ISO 8601 timestamp; kept as a plain string so replayed widgets never
    # fail on serializer-specific offset forms.
    executed_at: str = Field(alias="executedAt", min_length=1, max_length=64)
    model_config = ConfigDict(populate_by_name=True)


class DataChartData(WidgetDataBase):
    """A governed visualization view plus the exact executed rows it binds.

    Money rows stay in minor units (`money_minor` encodings); renderers divide
    by 100 for display — the backend never scales chart values.
    """

    view: VisualizationView
    rows: list[dict[str, Any]] = Field(min_length=1, max_length=250)
    currency: str | None = None
    lineage: ChartLineage


class AgentToolCallMetrics(BaseModel):
    """Content-free timing for one model-selected tool execution."""

    name: str = Field(min_length=1, max_length=160)
    duration_ms: float | None = Field(default=None, alias="durationMs", ge=0)
    failed: bool = False
    model_config = ConfigDict(populate_by_name=True)


class AgentModelPassMetrics(BaseModel):
    stage: str
    model: str
    provider: str | None = None
    reasoning_profile: str | None = Field(default=None, alias="reasoningProfile", max_length=32)
    prompt_characters: int | None = Field(default=None, alias="promptCharacters", ge=0)
    prompt_components: dict[str, int] = Field(default_factory=dict, alias="promptComponents")
    mounted_tool_count: int = Field(default=0, alias="mountedToolCount", ge=0)
    mounted_tools: list[str] = Field(default_factory=list, alias="mountedTools", max_length=64)
    tool_calls: list[AgentToolCallMetrics] = Field(default_factory=list, alias="toolCalls", max_length=64)
    input_tokens: int = Field(alias="inputTokens", ge=0)
    output_tokens: int = Field(alias="outputTokens", ge=0)
    total_tokens: int = Field(alias="totalTokens", ge=0)
    cache_read_tokens: int = Field(alias="cacheReadTokens", ge=0)
    cache_write_tokens: int = Field(alias="cacheWriteTokens", ge=0)
    reasoning_tokens: int = Field(alias="reasoningTokens", ge=0)
    duration_ms: float | None = Field(default=None, alias="durationMs", ge=0)
    time_to_first_token_ms: float | None = Field(default=None, alias="timeToFirstTokenMs", ge=0)
    cost_usd: float | None = Field(default=None, alias="costUsd", ge=0)
    model_config = ConfigDict(populate_by_name=True)


class AgentServerTimingMetrics(BaseModel):
    """Elapsed server timings calculated without storing request content."""

    queue_wait_ms: float | None = Field(default=None, alias="queueWaitMs", ge=0)
    started_to_first_activity_ms: float | None = Field(default=None, alias="startedToFirstActivityMs", ge=0)
    started_to_first_reasoning_ms: float | None = Field(default=None, alias="startedToFirstReasoningMs", ge=0)
    started_to_first_tool_call_ms: float | None = Field(default=None, alias="startedToFirstToolCallMs", ge=0)
    started_to_first_text_ms: float | None = Field(default=None, alias="startedToFirstTextMs", ge=0)
    accepted_to_first_text_ms: float | None = Field(default=None, alias="acceptedToFirstTextMs", ge=0)
    first_text_to_finished_ms: float | None = Field(default=None, alias="firstTextToFinishedMs", ge=0)
    accepted_to_finished_ms: float | None = Field(default=None, alias="acceptedToFinishedMs", ge=0)
    event_counts: dict[str, int] = Field(default_factory=dict, alias="eventCounts")
    model_config = ConfigDict(populate_by_name=True)


class AgentClientTimingMetrics(BaseModel):
    """Browser-observed elapsed timings; reporting is always best-effort."""

    submit_to_run_created_ms: float | None = Field(default=None, alias="submitToRunCreatedMs", ge=0)
    submit_to_first_activity_received_ms: float | None = Field(default=None, alias="submitToFirstActivityReceivedMs", ge=0)
    submit_to_first_reasoning_received_ms: float | None = Field(default=None, alias="submitToFirstReasoningReceivedMs", ge=0)
    submit_to_first_text_received_ms: float | None = Field(default=None, alias="submitToFirstTextReceivedMs", ge=0)
    submit_to_first_answer_visible_ms: float | None = Field(default=None, alias="submitToFirstAnswerVisibleMs", ge=0)
    submit_to_response_resolved_ms: float | None = Field(default=None, alias="submitToResponseResolvedMs", ge=0)
    submit_to_composer_unlocked_ms: float | None = Field(default=None, alias="submitToComposerUnlockedMs", ge=0)
    page_visible_at_submit: bool | None = Field(default=None, alias="pageVisibleAtSubmit")
    replayed: bool = False
    model_config = ConfigDict(populate_by_name=True)


class AgentClientTelemetryIn(AgentClientTimingMetrics):
    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")


class AgentRunMetrics(BaseModel):
    source: Literal["agno_run_output"] = "agno_run_output"
    model_passes: int = Field(default=0, alias="modelPasses", ge=0)
    input_tokens: int = Field(default=0, alias="inputTokens", ge=0)
    output_tokens: int = Field(default=0, alias="outputTokens", ge=0)
    total_tokens: int = Field(default=0, alias="totalTokens", ge=0)
    cache_read_tokens: int = Field(default=0, alias="cacheReadTokens", ge=0)
    cache_write_tokens: int = Field(default=0, alias="cacheWriteTokens", ge=0)
    reasoning_tokens: int = Field(default=0, alias="reasoningTokens", ge=0)
    model_duration_ms: float | None = Field(default=None, alias="modelDurationMs", ge=0)
    first_model_time_to_first_token_ms: float | None = Field(
        default=None,
        alias="firstModelTimeToFirstTokenMs",
        ge=0,
    )
    cost_usd: float | None = Field(default=None, alias="costUsd", ge=0)
    cost_coverage: float = Field(default=0, alias="costCoverage", ge=0, le=1)
    passes: list[AgentModelPassMetrics] = Field(default_factory=list)
    server: AgentServerTimingMetrics | None = None
    client: AgentClientTimingMetrics | None = None
    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def validate_pass_count(self):
        if self.model_passes != len(self.passes):
            raise ValueError("modelPasses must equal the number of per-pass metrics")
        if self.cost_usd is not None and self.cost_coverage != 1:
            raise ValueError("costUsd is exact only when costCoverage is 1")
        return self


class AgentActivityData(WidgetDataBase):
    title: str
    engine: str
    model: str
    summary: str = ""
    reasoning_trace: str | None = Field(default=None, alias="reasoningTrace")
    debug_trace: bool = Field(default=False, alias="debugTrace")
    steps: list[dict[str, Any]] = Field(default_factory=list)
    # Server-authored everywhere: stored widgets carry the terminal value and
    # live streams carry the running value on each AgentActivityEvent.
    model_pass_count: int | None = Field(default=None, alias="modelPassCount", ge=0)
    metrics: AgentRunMetrics | None = None
    total_ms: float = Field(default=0, alias="totalMs", ge=0)
    live: bool = False


class CategorySelectorData(WidgetDataBase):
    title: str
    body: str | None = None
    draft_id: str | None = Field(default=None, alias="draftId")
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    options: list[dict[str, Any]] = Field(default_factory=list)
    allow_create: bool = Field(default=False, alias="allowCreate")
    mode: str | None = None


class TransactionTypeSelectorData(WidgetDataBase):
    title: str
    body: str | None = None
    draft_id: str | None = Field(default=None, alias="draftId")
    options: list[dict[str, Any]] = Field(default_factory=list)


class SubcategorySelectorData(WidgetDataBase):
    title: str
    body: str | None = None
    category: str
    draft_id: str | None = Field(default=None, alias="draftId")
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    options: list[dict[str, Any]] = Field(default_factory=list)
    allow_create: bool = Field(default=False, alias="allowCreate")


class TaxonomyEditorData(WidgetDataBase):
    operation: TaxonomyOperation
    name: str | None = None
    subcategories: list[str] = Field(default_factory=list, max_length=10)
    parent_category: str | None = Field(default=None, alias="parentCategory")
    applies_to_draft: bool = Field(default=False, alias="appliesToDraft")
    draft_id: str | None = Field(default=None, alias="draftId")
    category_id: str | None = Field(default=None, alias="categoryId")
    result_id: str | None = Field(default=None, alias="resultId")
    result_ids: list[str] = Field(default_factory=list, alias="resultIds", max_length=11)


class AccountSelectorData(WidgetDataBase):
    title: str
    body: str | None = None
    draft_id: str | None = Field(default=None, alias="draftId")
    role: str
    options: list[dict[str, Any]] = Field(default_factory=list)


class ConfirmationCardData(WidgetDataBase):
    title: str
    transaction_id: str | None = Field(default=None, alias="transactionId")
    draft_id: str | None = Field(default=None, alias="draftId")
    amount_minor: int = Field(alias="amountMinor")
    currency: str
    merchant: str | None = None
    source_account: str | None = Field(default=None, alias="sourceAccount")
    destination_account: str | None = Field(default=None, alias="destinationAccount")
    transaction_type: TransactionType = Field(alias="transactionType")
    transaction_at: datetime = Field(alias="transactionAt")
    category: str | None = None
    subcategory: str | None = None
    location: str | None = None
    spend_nature: SpendNature | None = Field(default=None, alias="spendNature")
    tags: list[str] = Field(default_factory=list)
    status: str
    inferred_fields: list[str] = Field(default_factory=list, alias="inferredFields")


class TransactionPreviewData(WidgetDataBase):
    title: str
    transaction_id: str | None = Field(default=None, alias="transactionId")
    row_version: int | None = Field(default=None, alias="rowVersion", ge=1)
    draft_id: str | None = Field(default=None, alias="draftId")
    amount_minor: int = Field(alias="amountMinor")
    currency: str
    transaction_at: datetime = Field(alias="transactionAt")
    status: str
    source_count: int | None = Field(default=None, alias="sourceCount")
    transaction_type: TransactionType | None = Field(default=None, alias="transactionType")
    category: str | None = None
    subcategory: str | None = None
    location: str | None = None
    spend_nature: SpendNature | None = Field(default=None, alias="spendNature")
    tags: list[str] = Field(default_factory=list)
    # When a later card represents the same ledger row, the earlier card is an
    # audit receipt rather than a second current record. These fields preserve
    # the durable link in both the stored transcript and the live widget patch.
    superseded_by_version: int | None = Field(default=None, alias="supersededByVersion", ge=1)
    superseded_by_widget_id: str | None = Field(default=None, alias="supersededByWidgetId")


class TransactionEditData(WidgetDataBase):
    title: str
    transaction_id: str | None = Field(default=None, alias="transactionId")
    row_version: int | None = Field(default=None, alias="rowVersion", ge=1)
    draft_id: str | None = Field(default=None, alias="draftId")
    amount_minor: int | None = Field(default=None, alias="amountMinor")
    currency: str | None = None
    merchant: str | None = None
    transaction_at: datetime | None = Field(default=None, alias="transactionAt")
    transaction_type: TransactionType | None = Field(default=None, alias="transactionType")
    location: str | None = None
    spend_nature: SpendNature | None = Field(default=None, alias="spendNature")
    tags: list[str] = Field(default_factory=list)
    category_id: str | None = Field(default=None, alias="categoryId")
    subcategory_id: str | None = Field(default=None, alias="subcategoryId")
    categories: list[dict[str, Any]] = Field(default_factory=list)
    subcategories: list[dict[str, Any]] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)


class AvoidableExpensesData(WidgetDataBase):
    title: str
    body: str | None = None
    transactions: list[dict[str, Any]] = Field(default_factory=list)
    potential_minor: int = Field(alias="potentialMinor")
    currency: str


class InsightCardData(WidgetDataBase):
    eyebrow: str | None = None
    title: str
    body: str
    tone: str | None = None


class BudgetProgressData(WidgetDataBase):
    budget_id: str = Field(alias="budgetId")
    title: str
    body: str | None = None
    amount_minor: int = Field(alias="amountMinor")
    spent_minor: int = Field(alias="spentMinor")
    remaining_minor: int = Field(alias="remainingMinor")
    percent_used: float = Field(alias="percentUsed")
    currency: str
    category_slug: str | None = Field(default=None, alias="categorySlug")


class GoalProgressData(WidgetDataBase):
    goal_id: str = Field(alias="goalId")
    title: str
    body: str | None = None
    target_minor: int = Field(alias="targetMinor")
    current_minor: int = Field(alias="currentMinor")
    remaining_minor: int = Field(alias="remainingMinor")
    percent_complete: float = Field(alias="percentComplete")
    currency: str


class LoanCalculatorData(WidgetDataBase):
    title: str
    body: str | None = None
    principal_minor: int | None = Field(default=None, alias="principalMinor")
    annual_rate_percent: float | None = Field(default=None, alias="annualRatePercent")
    tenure_months: int | None = Field(default=None, alias="tenureMonths")
    prepayment_minor: int | None = Field(default=None, alias="prepaymentMinor")
    currency: str | None = None
    result: dict[str, Any] | None = None


class InvestmentProjectionData(WidgetDataBase):
    title: str
    body: str | None = None
    monthly_contribution_minor: int = Field(alias="monthlyContributionMinor")
    current_value_minor: int | None = Field(default=None, alias="currentValueMinor")
    annual_return_percent: float | None = Field(default=None, alias="annualReturnPercent")
    years: int | None = None
    currency: str
    result: dict[str, Any] | None = None


class ReconciliationReviewData(WidgetDataBase):
    candidate_id: str = Field(alias="candidateId")
    title: str
    score: float
    incoming: dict[str, Any]
    existing: dict[str, Any]
    signals: dict[str, Any] = Field(default_factory=dict)


class ImportSummaryData(BaseModel):
    """Canonical import counters shared by API responses and review widgets."""

    import_id: UUID = Field(alias="importId")
    status: ImportStatus
    total: int = Field(ge=0)
    high_confidence: int = Field(alias="highConfidence", ge=0)
    needs_review: int = Field(alias="needsReview", ge=0)
    duplicates: int = Field(ge=0)
    idempotent_replay: bool = Field(alias="idempotentReplay")
    model_config = ConfigDict(populate_by_name=True)


class ImportReviewData(WidgetDataBase, ImportSummaryData):
    title: str


class RelatedQuestionsData(WidgetDataBase):
    """Follow-up questions the user can post with one tap.

    Suggestion text only — clicking posts it as an ordinary user message, so
    the questions carry no payloads, actions, or claims of their own.
    """

    questions: list[str] = Field(min_length=1, max_length=4)

    @field_validator("questions")
    @classmethod
    def bounded_questions(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = " ".join(str(item).split())
            if not 1 <= len(text) <= 160:
                raise ValueError("Each related question must be 1-160 characters")
            cleaned.append(text)
        return cleaned


class OperationReferenceData(WidgetDataBase):
    title: str = Field(min_length=1, max_length=160)
    body: str | None = Field(default=None, max_length=500)
    operation_id: str = Field(alias="operationId", pattern=r"^[a-z][a-z0-9_.-]{1,99}$")
    operation_version: int = Field(alias="operationVersion", ge=1)
    operation_checksum: str = Field(alias="operationChecksum", pattern=r"^[a-f0-9]{64}$")
    inputs: dict[str, Any] = Field(default_factory=dict)


class OperationFormData(OperationReferenceData):
    input_schema: dict[str, Any] = Field(alias="inputSchema")
    missing_fields: list[str] = Field(default_factory=list, alias="missingFields", max_length=30)
    submit_label: str = Field(default="Continue", alias="submitLabel", min_length=1, max_length=80)


class OperationApprovalData(OperationReferenceData):
    effect: Literal["draft", "mutation"]
    summary: str = Field(min_length=1, max_length=500)


WIDGET_DATA_MODELS: dict[WidgetType, type[BaseModel]] = {
    WidgetType.AGENT_ACTIVITY: AgentActivityData,
    WidgetType.CLARIFICATION: ClarificationData,
    WidgetType.CATEGORY_SELECTOR: CategorySelectorData,
    WidgetType.TRANSACTION_TYPE_SELECTOR: TransactionTypeSelectorData,
    WidgetType.SUBCATEGORY_SELECTOR: SubcategorySelectorData,
    WidgetType.TAXONOMY_EDITOR: TaxonomyEditorData,
    WidgetType.ACCOUNT_SELECTOR: AccountSelectorData,
    WidgetType.CONFIRMATION_CARD: ConfirmationCardData,
    WidgetType.TRANSACTION_PREVIEW: TransactionPreviewData,
    WidgetType.TRANSACTION_EDIT: TransactionEditData,
    WidgetType.DATA_CHART: DataChartData,
    WidgetType.AVOIDABLE_EXPENSES: AvoidableExpensesData,
    WidgetType.INSIGHT_CARD: InsightCardData,
    WidgetType.BUDGET_PROGRESS: BudgetProgressData,
    WidgetType.GOAL_PROGRESS: GoalProgressData,
    WidgetType.LOAN_CALCULATOR: LoanCalculatorData,
    WidgetType.INVESTMENT_PROJECTION: InvestmentProjectionData,
    WidgetType.RECONCILIATION_REVIEW: ReconciliationReviewData,
    WidgetType.IMPORT_REVIEW: ImportReviewData,
    WidgetType.RELATED_QUESTIONS: RelatedQuestionsData,
    WidgetType.OPERATION_FORM: OperationFormData,
    WidgetType.OPERATION_APPROVAL: OperationApprovalData,
}

if set(WIDGET_DATA_MODELS) != set(WidgetType):
    raise RuntimeError("Every widget type must have exactly one registered data model")


class Widget(BaseModel):
    id: str
    type: WidgetType
    version: Literal[1] = 1
    data: dict[str, Any]
    actions: list[WidgetAction] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_registered_data(self):
        WIDGET_DATA_MODELS[self.type].model_validate(self.data)
        return self


class PendingAction(BaseModel):
    action: WidgetActionId
    resource_id: str
    requires_confirmation: bool = True
    # Server-authored continuation state is copied into the durable interrupt,
    # never sent as part of the public AgentResponse contract.
    continuation: dict[str, Any] = Field(default_factory=dict, exclude=True)


class DataReference(BaseModel):
    label: str
    entity_type: str
    entity_ids: list[str] = Field(default_factory=list)
    query: dict[str, Any] = Field(default_factory=dict)


class WidgetUpdate(BaseModel):
    """A durable replacement for a widget already present in the transcript."""

    widget_id: str = Field(alias="widgetId")
    widget: Widget
    model_config = ConfigDict(populate_by_name=True)


class AgentResponse(BaseModel):
    message: str = ""
    widgets: list[Widget] = Field(default_factory=list)
    widget_updates: list[WidgetUpdate] = Field(default_factory=list, alias="widgetUpdates")
    pending_action: PendingAction | None = Field(default=None, alias="pendingAction")
    citations: list[DataReference] = Field(default_factory=list)
    conversation_id: UUID
    message_id: UUID
    # The persisted identity of the user turn this reply answers, when the turn
    # recorded one. The client renders a sent message optimistically before the
    # server has durably stored it; this is how it learns the stored ID.
    user_message_id: UUID | None = None
    delivered_at: datetime
    # Runtime-only outcome used to finish the durable AgentRun truthfully. The
    # public response stays backward compatible; AgentRunOut exposes it.
    task_status: Literal["needs_input", "succeeded", "degraded", "failed", "cancelled"] = Field(
        default="succeeded",
        exclude=True,
    )
    failure_stage: str | None = Field(default=None, exclude=True)
    error_code: str | None = Field(default=None, exclude=True)
    model_config = ConfigDict(populate_by_name=True)


class AgentInterruptOut(BaseModel):
    id: UUID
    run_id: UUID = Field(serialization_alias="runId")
    tool_call_id: str = Field(serialization_alias="toolCallId")
    widget_id: str = Field(serialization_alias="widgetId")
    reason: str
    message: str | None = None
    response_schema: dict[str, Any] = Field(serialization_alias="responseSchema")
    metadata: dict[str, Any] = Field(validation_alias="metadata_payload", serialization_alias="metadata")
    status: str
    expires_at: datetime | None = Field(default=None, serialization_alias="expiresAt")
    model_config = ConfigDict(from_attributes=True)


class AgentRunOut(BaseModel):
    id: UUID
    status: str
    task_status: str = Field(serialization_alias="taskStatus")
    failure_stage: str | None = Field(default=None, serialization_alias="failureStage")
    error_code: str | None = Field(default=None, serialization_alias="errorCode")
    last_sequence: int = Field(serialization_alias="lastSequence")
    cancel_requested: bool = Field(serialization_alias="cancelRequested")
    final_message_id: UUID | None = Field(default=None, serialization_alias="finalMessageId")
    delivery_mode: str = Field(default="verified_final", serialization_alias="deliveryMode")
    created_at: datetime = Field(serialization_alias="createdAt")
    started_at: datetime | None = Field(default=None, serialization_alias="startedAt")
    first_response_at: datetime | None = Field(default=None, serialization_alias="firstResponseAt")
    finished_at: datetime | None = Field(default=None, serialization_alias="finishedAt")
    duration_ms: float | None = Field(default=None, serialization_alias="durationMs")
    time_to_first_response_ms: float | None = Field(default=None, serialization_alias="timeToFirstResponseMs")
    metrics: AgentRunMetrics = Field(default_factory=AgentRunMetrics)
    model_config = ConfigDict(from_attributes=True)


class AgentThreadStateOut(BaseModel):
    thread_id: UUID = Field(serialization_alias="threadId")
    active_run: AgentRunOut | None = Field(default=None, serialization_alias="activeRun")
    latest_run: AgentRunOut | None = Field(default=None, serialization_alias="latestRun")
    interrupts: list[AgentInterruptOut] = Field(default_factory=list)


class ImportResultOut(ImportSummaryData):
    agent_response: AgentResponse = Field(serialization_alias="agentResponse")


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None

    @field_validator("text")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be blank")
        return value


class ActionRequest(BaseModel):
    conversation_id: UUID
    widget_id: str
    action: WidgetActionId
    payload: dict[str, Any] = Field(default_factory=dict)
    complete_widget: bool = Field(default=True, alias="completeWidget")
    model_config = ConfigDict(populate_by_name=True)


class MessageOut(BaseModel):
    id: UUID
    role: str
    content: str
    widgets: list[Widget]
    citations: list[DataReference]
    created_at: datetime
    delivered_at: datetime
    model_config = ConfigDict(from_attributes=True)


class ConversationOut(BaseModel):
    id: UUID
    title: str
    messages: list[MessageOut]
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

    @field_validator("messages")
    @classmethod
    def chronological_messages(cls, value: list[MessageOut]) -> list[MessageOut]:
        """A refreshed thread must never depend on database relationship order."""
        return sorted(value, key=lambda message: (message.created_at, message.id.hex))


class ConversationSummaryOut(BaseModel):
    id: UUID
    title: str
    updated_at: datetime = Field(serialization_alias="updatedAt")
    model_config = ConfigDict(from_attributes=True)


class ConversationPage(BaseModel):
    """One screenful of history. `next_cursor` is absent on the last page."""

    items: list[ConversationSummaryOut]
    next_cursor: str | None = Field(default=None, serialization_alias="nextCursor")


class BootstrapUser(BaseModel):
    id: UUID
    name: str
    currency: str = Field(min_length=3, max_length=3)
    timezone: str


class FeatureAvailabilityOut(BaseModel):
    personal_lending: bool = Field(serialization_alias="personalLending")


class BootstrapResponse(BaseModel):
    user: BootstrapUser
    active_conversation: ConversationOut
    features: FeatureAvailabilityOut


class OverviewPeriodOut(BaseModel):
    start: DateValue
    end: DateValue
    previous_start: DateValue = Field(serialization_alias="previousStart")
    previous_end: DateValue = Field(serialization_alias="previousEnd")
    label: str
    is_current: bool = Field(serialization_alias="isCurrent")


class OverviewSummaryOut(BaseModel):
    currency: str = Field(min_length=3, max_length=3)
    income_minor: int = Field(serialization_alias="incomeMinor")
    spent_minor: int = Field(serialization_alias="spentMinor")
    net_minor: int = Field(serialization_alias="netMinor")
    expense_count: int = Field(serialization_alias="expenseCount")
    previous_spent_minor: int = Field(serialization_alias="previousSpentMinor")
    change_minor: int = Field(serialization_alias="changeMinor")
    change_percent: float | None = Field(serialization_alias="changePercent")


class OverviewSubcategoryOut(BaseModel):
    id: str
    label: str
    amount_minor: int = Field(serialization_alias="amountMinor")
    count: int
    share_percent: float = Field(serialization_alias="sharePercent")


class OverviewCategoryOut(OverviewSubcategoryOut):
    subcategories: list[OverviewSubcategoryOut]


class OverviewTrendPointOut(BaseModel):
    day: int = Field(ge=1, le=31)
    date: DateValue
    income_minor: int = Field(serialization_alias="incomeMinor")
    spent_minor: int = Field(serialization_alias="spentMinor")
    previous_income_minor: int = Field(serialization_alias="previousIncomeMinor")
    previous_spent_minor: int = Field(serialization_alias="previousSpentMinor")


class OverviewTransactionOut(BaseModel):
    id: UUID
    transaction_type: TransactionType = Field(serialization_alias="transactionType")
    amount_minor: int = Field(serialization_alias="amountMinor")
    currency: str = Field(min_length=3, max_length=3)
    merchant: str | None = None
    transaction_at: datetime = Field(serialization_alias="transactionAt")
    category: str | None = None
    account: str | None = None


class OverviewAccountOut(BaseModel):
    id: UUID
    name: str
    account_type: str = Field(serialization_alias="accountType")
    institution: str | None = None
    mask: str | None = None
    balance_minor: int = Field(serialization_alias="balanceMinor")
    currency: str = Field(min_length=3, max_length=3)


class OverviewBudgetOut(BaseModel):
    id: UUID
    name: str
    category_id: UUID | None = Field(default=None, serialization_alias="categoryId")
    category_slug: str | None = Field(default=None, serialization_alias="categorySlug")
    category: str | None = None
    amount_minor: int = Field(ge=0, serialization_alias="amountMinor")
    spent_minor: int = Field(ge=0, serialization_alias="spentMinor")
    remaining_minor: int = Field(ge=0, serialization_alias="remainingMinor")
    over_minor: int = Field(ge=0, serialization_alias="overMinor")
    percent_used: float = Field(ge=0, serialization_alias="percentUsed")
    currency: str = Field(min_length=3, max_length=3)
    period: str


class OverviewOut(BaseModel):
    period: OverviewPeriodOut
    summary: OverviewSummaryOut
    categories: list[OverviewCategoryOut]
    budgets: list[OverviewBudgetOut]
    trend: list[OverviewTrendPointOut]
    recent_transactions: list[OverviewTransactionOut] = Field(serialization_alias="recentTransactions")
    accounts: list[OverviewAccountOut]


class CategoryDirectorySubcategoryOut(BaseModel):
    id: UUID
    slug: str
    label: str
    editable: bool = False


class TransactionCategoryHintOut(BaseModel):
    id: UUID
    merchant: str
    category_id: UUID = Field(serialization_alias="categoryId")
    subcategory_id: UUID | None = Field(default=None, serialization_alias="subcategoryId")
    subcategory: str | None = None


class CategoryDirectoryOut(BaseModel):
    id: UUID
    slug: str
    label: str
    icon: str | None = None
    subcategories: list[CategoryDirectorySubcategoryOut]
    editable: bool = False
    hints: list[TransactionCategoryHintOut] = Field(default_factory=list)


class TaxonomyCreateIn(BaseModel):
    """One field for both taxonomy levels: the caller names the thing, the
    server owns slugs, icons and ownership scope."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=80)


class TransactionCategoryHintIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    merchant: str = Field(min_length=1, max_length=160)
    subcategory_id: UUID | None = Field(default=None, alias="subcategoryId")


class TransactionListItemOut(BaseModel):
    id: UUID
    row_version: int = Field(serialization_alias="rowVersion", ge=1)
    transaction_type: TransactionType = Field(serialization_alias="transactionType")
    amount_minor: int = Field(serialization_alias="amountMinor")
    currency: str = Field(min_length=3, max_length=3)
    merchant: str | None = None
    transaction_at: datetime = Field(serialization_alias="transactionAt")
    status: TransactionStatus
    category_id: UUID | None = Field(default=None, serialization_alias="categoryId")
    category: str | None = None
    subcategory_id: UUID | None = Field(default=None, serialization_alias="subcategoryId")
    subcategory: str | None = None
    spend_nature: SpendNature = Field(serialization_alias="spendNature")
    location: str | None = None
    source_count: int = Field(serialization_alias="sourceCount")
    deleted_at: datetime | None = Field(default=None, serialization_alias="deletedAt")


class TransactionUpdateIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    amount_minor: int = Field(alias="amountMinor", gt=0, le=MAX_TRANSACTION_AMOUNT_MINOR)
    # Optional because the same shape is used for creation. PATCH requires it
    # at the endpoint; POST rejects no stale row because none exists yet.
    expected_version: int | None = Field(default=None, alias="expectedVersion", ge=1)
    merchant: str | None = Field(default=None, max_length=160)
    transaction_at: datetime = Field(alias="transactionAt")
    transaction_type: TransactionType = Field(alias="transactionType")
    category_id: UUID | None = Field(default=None, alias="categoryId")
    subcategory_id: UUID | None = Field(default=None, alias="subcategoryId")
    spend_nature: SpendNature = Field(alias="spendNature")
    location: str | None = Field(default=None, max_length=160)
    # A device fix, sent only when the person has turned location on. The
    # service checks that preference again before storing any of it — a client
    # that sends coordinates regardless must not be able to record them.
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_accuracy: int | None = Field(default=None, alias="locationAccuracy", ge=0)


class TransactionRevisionChangeOut(BaseModel):
    before: Any = None
    after: Any = None


class TransactionRevisionOut(BaseModel):
    revision_number: int = Field(serialization_alias="revisionNumber", ge=1)
    source: str
    reason: str | None = None
    changes: dict[str, TransactionRevisionChangeOut]
    created_at: datetime = Field(serialization_alias="createdAt")


class LocationResolveIn(BaseModel):
    """A browser fix to name before a new transaction is submitted."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class LocationResolveOut(BaseModel):
    location: str | None = None


class ObservationIn(BaseModel):
    source_type: FinancialSourceType
    source_message_id: str | None = None
    external_transaction_id: str | None = None
    source_account: str | None = None
    transaction_type: TransactionType
    amount_minor: int = Field(gt=0)
    currency: str = Field(default=DEFAULT_CURRENCY, min_length=3, max_length=3)
    merchant: str | None = None
    transaction_at: datetime | None = None
    posted_at: datetime | None = None
    reference_number: str | None = None
    description: str | None = None
    raw_reference: str | None = None
    observed_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_dates(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for legacy, canonical in (("transaction_date", "transaction_at"), ("posted_date", "posted_at")):
            if normalized.get(canonical) is not None or normalized.get(legacy) is None:
                continue
            day = normalized[legacy]
            if not isinstance(day, DateValue):
                day = DateValue.fromisoformat(str(day))
            normalized[canonical] = from_local_parts(day, None, "UTC")
        normalized.setdefault("transaction_at", now_utc())
        return normalized

    @model_validator(mode="after")
    def utc_instants(self) -> "ObservationIn":
        self.transaction_at = as_utc(self.transaction_at or now_utc())
        if self.posted_at is not None:
            self.posted_at = as_utc(self.posted_at)
        if self.observed_at is not None:
            self.observed_at = as_utc(self.observed_at)
        return self


class ReconciliationResultOut(BaseModel):
    observation_id: UUID
    transaction_id: UUID | None
    decision: ReconciliationOutcome
    score: float | None = None
    signals: dict[str, Any] = Field(default_factory=dict)
    idempotent_replay: bool = False


class AffordabilityIn(AffordabilityInput):
    pass


class LoanCalculationIn(LoanWithPrepaymentInput):
    pass


class InvestmentProjectionIn(InvestmentProjectionInput):
    pass


class FinancialMessageIn(BaseModel):
    source_type: FinancialSourceType
    message_id: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=10000)
    observed_at: datetime | None = None

    @field_validator("source_type")
    @classmethod
    def message_source_only(cls, value: FinancialSourceType) -> FinancialSourceType:
        if value not in MESSAGE_SOURCE_TYPES:
            raise ValueError("Financial messages support SMS or email")
        return value


class LocationPreferenceIn(BaseModel):
    enabled: bool


AnswerValidationModeValue = Literal["full", "evidence_only", "off"]
AnswerStyleValue = Literal["explained", "concise"]


class AgentSettingsIn(BaseModel):
    answer_validation_mode: AnswerValidationModeValue | None = Field(
        default=None,
        alias="answerValidationMode",
    )
    answer_style: AnswerStyleValue | None = Field(default=None, alias="answerStyle")
    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def contains_a_setting(self) -> "AgentSettingsIn":
        if self.answer_validation_mode is None and self.answer_style is None:
            raise ValueError("At least one agent setting is required")
        return self


class DataDeletionIn(BaseModel):
    confirmation: Literal["DELETE MY DATA"]


class AgentModelSet(BaseModel):
    operator: str
    planner: str
    validator: str
    reconciler: str


AgentMode = Literal["llm", "deterministic_fallback"]


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    time: datetime
    database: Literal["postgresql", "sqlite"]
    agent_mode: AgentMode
    models: AgentModelSet | None = None
    operation_catalog: dict[str, Any] = Field(alias="operationCatalog")
    model_config = ConfigDict(populate_by_name=True)


class AgentDecisionDiagnostic(BaseModel):
    tool: str | None = None
    confidence: float | None = None
    status: str
    created_at: datetime


class AgentDiagnosticsOut(BaseModel):
    mode: AgentMode
    models: AgentModelSet | None = None
    recent_decisions: list[AgentDecisionDiagnostic]


class ConversationCreatedOut(BaseModel):
    id: UUID


class FinancialMessageOut(BaseModel):
    classification: str
    relevant: bool
    reason: str
    reconciliation: ReconciliationResultOut | None = None


class ReconciliationReviewOut(BaseModel):
    id: UUID
    observation_id: UUID = Field(serialization_alias="observationId")
    transaction_id: UUID = Field(serialization_alias="transactionId")
    score: float
    signals: dict[str, Any] = Field(default_factory=dict)


class OtpStartIn(BaseModel):
    channel: OtpChannel
    # Accepted as typed and normalized server-side; the client is not trusted to
    # produce E.164 or to canonicalise an address.
    value: str = Field(min_length=3, max_length=320)


class OtpVerifyIn(BaseModel):
    challenge_id: UUID = Field(alias="challengeId")
    code: str = Field(min_length=4, max_length=10)
    model_config = ConfigDict(populate_by_name=True)


class OtpSentOut(BaseModel):
    challenge_id: UUID = Field(serialization_alias="challengeId")
    channel: OtpChannel
    # Enough to confirm where the code went without restating the identifier.
    destination_masked: str = Field(serialization_alias="destinationMasked")
    expires_in_seconds: int = Field(serialization_alias="expiresInSeconds")
    resend_after_seconds: int = Field(serialization_alias="resendAfterSeconds")
    debug_code: str | None = Field(default=None, serialization_alias="debugCode")


class GoogleSignInIn(BaseModel):
    credential: str = Field(min_length=1)


class IdentityOut(BaseModel):
    id: UUID
    provider: IdentityProvider
    # The address or number as the account holder entered it; a Google row
    # reports the address rather than the opaque subject.
    value: str
    source: IdentitySource
    verified_at: datetime = Field(serialization_alias="verifiedAt")
    last_login_at: datetime | None = Field(default=None, serialization_alias="lastLoginAt")


class ProfileOut(BaseModel):
    id: UUID
    display_name: str = Field(serialization_alias="displayName")
    currency: str = Field(min_length=3, max_length=3)
    timezone: str
    email: str | None = None
    phone: str | None = None
    identities: list[IdentityOut]
    # False once a second method is linked, which is what the profile page uses
    # to explain why a sign-in method cannot be removed.
    google_sign_in_available: bool = Field(serialization_alias="googleSignInAvailable")


class ProfileUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=2, max_length=120, validation_alias="displayName")

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_display_name(cls, value: Any) -> str:
        normalized = " ".join(str(value).split())
        if normalized.casefold() == "you":
            raise ValueError("Enter the name the other person will recognize")
        return normalized


class AuthStatusOut(BaseModel):
    authenticated: bool
    profile: ProfileOut | None = None
    google_sign_in_available: bool = Field(serialization_alias="googleSignInAvailable")
    features: FeatureAvailabilityOut


class SignOutOut(BaseModel):
    signed_out: Literal[True] = Field(serialization_alias="signedOut")


class PrivacyStatusOut(BaseModel):
    location_enabled: bool = Field(serialization_alias="locationEnabled")
    sources: dict[str, bool]
    retention: Literal["until_deleted"]


class LocationPreferenceOut(BaseModel):
    location_enabled: bool = Field(serialization_alias="locationEnabled")


class AgentSettingsOut(BaseModel):
    answer_validation_mode: AnswerValidationModeValue = Field(serialization_alias="answerValidationMode")
    answer_style: AnswerStyleValue = Field(serialization_alias="answerStyle")


class SourceRevocationOut(BaseModel):
    source_type: str = Field(serialization_alias="sourceType")
    active: Literal[False]


class DataDeletionOut(BaseModel):
    deleted: Literal[True]


class AgentActivityEvent(BaseModel):
    id: str
    stage_id: str | None = Field(default=None, alias="stageId")
    label: str
    status: ExecutionStatus
    tool: str | None = None
    result_tool: str | None = Field(default=None, alias="resultTool")
    detail: str | None = None
    badge: str | None = None
    input_payload: Any | None = Field(default=None, alias="input")
    output_payload: Any | None = Field(default=None, alias="output")
    duration_ms: float = Field(alias="durationMs", ge=0)
    cumulative_ms: float = Field(alias="cumulativeMs", ge=0)
    # Run-level aggregates, stamped on every streamed snapshot the same way
    # cumulativeMs already is. The server is the only author of these rules;
    # a live card renders them verbatim instead of re-deriving them.
    failure_summary: str | None = Field(default=None, alias="failureSummary")
    model_pass_count: int | None = Field(default=None, alias="modelPassCount", ge=0)
    model_config = ConfigDict(populate_by_name=True)


class SpreadsheetColumnDraftOut(BaseModel):
    """One column of an uploaded source: the profile plus the drafted meaning.

    This IS the confirmation surface: the client shows these drafts and posts
    the user's corrections to the annotations endpoint, which records them
    with user_stated provenance.
    """

    name: str
    inferred_type: str = Field(serialization_alias="inferredType")
    role: str
    confidence: float
    user_stated: str | None = Field(default=None, serialization_alias="userStated")


class SpreadsheetSourceOut(BaseModel):
    source_id: UUID = Field(serialization_alias="sourceId")
    name: str
    manifest_version: int = Field(serialization_alias="manifestVersion")
    row_count: int = Field(serialization_alias="rowCount")
    columns: list[SpreadsheetColumnDraftOut]
    needs_confirmation: bool = Field(default=True, serialization_alias="needsConfirmation")


class SourceAnnotationIn(BaseModel):
    field: str = Field(min_length=1, max_length=120)
    statement: str = Field(min_length=1, max_length=2000)
    # The structured half: when set, this role overrides inference in every
    # deterministic consumer (query semantics, tool catalogs), per the
    # user_stated-wins provenance law.
    role: str | None = Field(default=None, max_length=40)


class SourceAnnotationsIn(BaseModel):
    annotations: list[SourceAnnotationIn] = Field(min_length=1, max_length=60)


class SourceAnnotationsOut(BaseModel):
    source_id: UUID = Field(serialization_alias="sourceId")
    manifest_version: int = Field(serialization_alias="manifestVersion")
    annotated_fields: list[str] = Field(serialization_alias="annotatedFields")


class DashboardCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class DashboardCreatedOut(BaseModel):
    id: UUID
    name: str


class DashboardSummaryOut(BaseModel):
    id: UUID
    name: str
    tile_count: int = Field(serialization_alias="tileCount")


class DashboardListOut(BaseModel):
    dashboards: list[DashboardSummaryOut]


class DashboardTileCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    # Validated against AnalysisToolProposal at the route boundary; keeping the
    # wire type structural here avoids a schemas -> services import.
    proposal: dict[str, Any]
    position: int | None = Field(default=None, ge=0)


class DashboardTileCreatedOut(BaseModel):
    id: UUID
    dashboard_id: UUID = Field(serialization_alias="dashboardId")
    title: str
    position: int


class DashboardTileErrorOut(BaseModel):
    code: str
    detail: str


class DashboardTileOut(BaseModel):
    id: UUID
    title: str
    position: int
    executed_at: str = Field(serialization_alias="executedAt")
    # DataChartData JSON, exactly as build_chart_widget serialized it.
    chart: dict[str, Any] | None = None
    error: DashboardTileErrorOut | None = None


class DashboardOut(BaseModel):
    id: UUID
    name: str
    tiles: list[DashboardTileOut]


class InsightEvidenceRowOut(BaseModel):
    """One exact number behind a claim, with the unit that makes it readable."""

    label: str
    value: int
    unit: str
    currency: str | None = None
    # The date the number is attached to, when it has one (a due date, the day
    # a payment landed). Absent for quantities that belong to no single day.
    on: str | None = None


class InsightEvidenceOut(BaseModel):
    rows: list[InsightEvidenceRowOut]
    # The display strings the headline quotes. Verified alongside the numbers:
    # a headline naming last month's merchant spelling is as wrong as one
    # naming last month's amount.
    labels: dict[str, str]


class InsightLineageOut(BaseModel):
    manifest_hash: str = Field(serialization_alias="manifestHash")
    # Null only when the user has no traits at all — an honest "not computed",
    # never a borrowed timestamp.
    traits_computed_at: str | None = Field(default=None, serialization_alias="traitsComputedAt")
    computed_at: str = Field(serialization_alias="computedAt")


class InsightOut(BaseModel):
    id: UUID
    kind: str
    subject: str
    headline: str
    evidence: InsightEvidenceOut
    lineage: InsightLineageOut
    # The deterministic parameters that reproduce this claim, so a reader can
    # ask for it to be rechecked rather than taking it on trust.
    recompute_key: dict[str, Any] = Field(serialization_alias="recomputeKey")
    verified_at: str = Field(serialization_alias="verifiedAt")


class InsightsOut(BaseModel):
    insights: list[InsightOut]
    # When this response's verification pass ran. Every insight below reproduced
    # at this moment; a claim that did not is not in the list.
    verified_at: str = Field(serialization_alias="verifiedAt")
