from __future__ import annotations

from datetime import date as DateValue, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import DEFAULT_CURRENCY
from .domain import ExecutionStatus, FinancialSourceType, IdentityProvider, IdentitySource, ImportStatus, MESSAGE_SOURCE_TYPES, OtpChannel, ReconciliationOutcome, SpendNature, TaxonomyOperation, TransactionStatus, TransactionType, ValueEnum, WidgetActionId
from .event_time import as_utc, from_local_parts, now_utc
from .services.tool_models import AffordabilityInput, InvestmentProjectionInput, LoanWithPrepaymentInput
from .validation import DataFieldKey
# VisualEncodingContract and VisualFieldEncoding are unused here but re-exported:
# contracts.py imports them from this module to build the frontend bundle.
from .visualization_contracts import VisualEncodingContract, VisualFieldEncoding, VisualizationLayout, VisualizationView  # noqa: F401


class WidgetType(ValueEnum):
    AGENT_ACTIVITY = "agent_activity"
    CATEGORY_SELECTOR = "category_selector"
    TRANSACTION_TYPE_SELECTOR = "transaction_type_selector"
    SUBCATEGORY_SELECTOR = "subcategory_selector"
    TAXONOMY_EDITOR = "taxonomy_editor"
    ACCOUNT_SELECTOR = "account_selector"
    CONFIRMATION_CARD = "confirmation_card"
    TRANSACTION_PREVIEW = "transaction_preview"
    TRANSACTION_EDIT = "transaction_edit"
    TRANSACTION_LIST = "transaction_list"
    DATA_TABLE = "data_table"
    DATA_CHART = "data_chart"
    DATA_VISUALIZATION = "data_visualization"
    FINANCIAL_SUMMARY = "financial_summary"
    ANALYSIS_TABLE = "analysis_table"
    AVOIDABLE_EXPENSES = "avoidable_expenses"
    INSIGHT_CARD = "insight_card"
    BUDGET_PROGRESS = "budget_progress"
    GOAL_PROGRESS = "goal_progress"
    SCENARIO_ANALYSIS = "scenario_analysis"
    LOAN_CALCULATOR = "loan_calculator"
    LOAN_STRATEGY = "loan_strategy"
    INVESTMENT_PROJECTION = "investment_projection"
    RECONCILIATION_REVIEW = "reconciliation_review"
    IMPORT_REVIEW = "import_review"


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


class TableColumnType(ValueEnum):
    ENTITY = "entity"
    TEXT = "text"
    MONEY = "money"
    DATE = "date"
    DATETIME = "datetime"
    NUMBER = "number"
    PERCENTAGE = "percentage"
    BOOLEAN = "boolean"
    STATUS = "status"
    TAGS = "tags"


class TableColumnAlign(ValueEnum):
    LEFT = "left"
    RIGHT = "right"


class TableColumnPriority(ValueEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    DETAIL = "detail"


class DataChartType(ValueEnum):
    BAR = "bar"
    LINE = "line"
    AREA = "area"
    PIE = "pie"
    HEATMAP = "heatmap"


class DataChartAxisType(ValueEnum):
    CATEGORY = "category"
    DATE = "date"
    DATETIME = "datetime"
    NUMBER = "number"


class DataChartValueType(ValueEnum):
    MONEY = "money"
    NUMBER = "number"
    PERCENTAGE = "percentage"


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

    @model_validator(mode="after")
    def require_account(self):
        if self.option_id is None and self.account_id is None:
            raise ValueError("Choose an account")
        return self


class TransactionSpendNaturePayload(ActionPayloadBase):
    transaction_id: UUID = Field(alias="transactionId")
    spend_nature: SpendNature = Field(alias="spendNature")


class SaveBudgetPayload(ActionPayloadBase):
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


class UpdateSavedTransactionPayload(TransactionActionPayload):
    amount_minor: int = Field(alias="amountMinor", gt=0)
    merchant: str | None = Field(default=None, max_length=160)
    transaction_at: datetime | None = Field(default=None, alias="transactionAt")
    transaction_type: TransactionType | None = Field(default=None, alias="transactionType")
    location: str | None = Field(default=None, max_length=160)
    spend_nature: SpendNature | None = Field(default=None, alias="spendNature")
    category_id: UUID | None = Field(default=None, alias="categoryId")
    subcategory_id: UUID | None = Field(default=None, alias="subcategoryId")
    tags: list[str] | str | None = None

    @field_validator("transaction_at")
    @classmethod
    def utc_transaction_at(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value is not None else None


class ReconciliationActionPayload(ActionPayloadBase):
    candidate_id: UUID = Field(alias="candidateId")


ACTION_PAYLOAD_MODELS: dict[WidgetActionId, type[BaseModel]] = {
    WidgetActionId.SET_SPEND_NATURE: TransactionSpendNaturePayload,
    WidgetActionId.START_ADD_CATEGORY: DraftActionPayload,
    WidgetActionId.START_ADD_SUBCATEGORY: DraftActionPayload,
    WidgetActionId.CANCEL_ADD_CATEGORY: DraftActionPayload,
    WidgetActionId.CANCEL_TAXONOMY_CHANGE: TaxonomyCancelPayload,
    WidgetActionId.CREATE_CATEGORY: CreateCategoryPayload,
    WidgetActionId.CREATE_SUBCATEGORY: CreateSubcategoryPayload,
    WidgetActionId.SELECT_CATEGORY: SelectCategoryPayload,
    WidgetActionId.SELECT_TRANSACTION_TYPE: SelectTransactionTypePayload,
    WidgetActionId.SELECT_SUBCATEGORY: SelectSubcategoryPayload,
    WidgetActionId.CHANGE_CATEGORY: DraftActionPayload,
    WidgetActionId.SELECT_ACCOUNT: SelectAccountPayload,
    WidgetActionId.SAVE_BUDGET: SaveBudgetPayload,
    WidgetActionId.SAVE_GOAL: SaveGoalPayload,
    WidgetActionId.CONTRIBUTE_GOAL: ContributeGoalPayload,
    WidgetActionId.COMMIT_IMPORT: ImportActionPayload,
    WidgetActionId.CALCULATE_LOAN_SCENARIO: LoanScenarioActionPayload,
    WidgetActionId.CALCULATE_INVESTMENT_SCENARIO: InvestmentScenarioActionPayload,
    WidgetActionId.COMMIT_TRANSACTION: DraftActionPayload,
    WidgetActionId.EDIT_TRANSACTION: DraftActionPayload,
    WidgetActionId.UPDATE_TRANSACTION_DRAFT: UpdateDraftPayload,
    WidgetActionId.EDIT_SAVED_TRANSACTION: TransactionActionPayload,
    WidgetActionId.CANCEL_SAVED_TRANSACTION_EDIT: TransactionActionPayload,
    WidgetActionId.UPDATE_SAVED_TRANSACTION: UpdateSavedTransactionPayload,
    WidgetActionId.REQUEST_REMOVE_TRANSACTION: TransactionActionPayload,
    WidgetActionId.CONFIRM_REMOVE_TRANSACTION: TransactionActionPayload,
    WidgetActionId.CANCEL_REMOVE_TRANSACTION: TransactionActionPayload,
    WidgetActionId.MERGE_RECONCILIATION: ReconciliationActionPayload,
    WidgetActionId.SEPARATE_RECONCILIATION: ReconciliationActionPayload,
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


class DataTableColumn(BaseModel):
    key: DataFieldKey
    label: str = Field(min_length=1, max_length=80)
    type: TableColumnType = TableColumnType.TEXT
    align: TableColumnAlign = TableColumnAlign.LEFT
    priority: TableColumnPriority = TableColumnPriority.SECONDARY
    currency_key: DataFieldKey | None = Field(default=None, alias="currencyKey")
    secondary_keys: list[str] = Field(default_factory=list, alias="secondaryKeys", max_length=4)
    model_config = ConfigDict(populate_by_name=True)


class DataTableRowAction(BaseModel):
    id: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=80)
    action: WidgetActionId
    style: WidgetActionStyle = WidgetActionStyle.SECONDARY
    resource_key: DataFieldKey = Field(alias="resourceKey")
    payload_key: DataFieldKey = Field(alias="payloadKey")
    icon: WidgetActionIcon | None = None
    capability: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,99}$")
    model_config = ConfigDict(populate_by_name=True)


class DataTableData(WidgetDataBase):
    title: str = Field(min_length=1, max_length=160)
    body: str | None = Field(default=None, max_length=500)
    columns: list[DataTableColumn] = Field(min_length=1, max_length=12)
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    row_id_key: DataFieldKey = Field(default="id", alias="rowIdKey")
    row_actions: list[DataTableRowAction] = Field(default_factory=list, alias="rowActions", max_length=6)
    capabilities_key: str = Field(default="_capabilities", alias="capabilitiesKey", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    empty_message: str = Field(default="No matching records.", alias="emptyMessage", max_length=240)
    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def validate_contract(self):
        keys = [column.key for column in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("data-table column keys must be unique")
        for action in self.row_actions:
            if any(action.resource_key not in row for row in self.rows):
                raise ValueError(f"row action {action.id} references a missing resource key")
            if action.capability and any(
                self.capabilities_key not in row or not isinstance(row[self.capabilities_key], list)
                for row in self.rows
            ):
                raise ValueError(f"row action {action.id} requires row capabilities")
        return self


class DataChartAxis(BaseModel):
    key: DataFieldKey
    label: str = Field(min_length=1, max_length=80)
    type: DataChartAxisType = DataChartAxisType.CATEGORY


class DataChartSeries(BaseModel):
    key: DataFieldKey
    label: str = Field(min_length=1, max_length=80)
    value_type: DataChartValueType = Field(default=DataChartValueType.NUMBER, alias="valueType")
    currency: str | None = Field(default=None, max_length=3)
    group_key: DataFieldKey | None = Field(default=None, alias="groupKey")
    model_config = ConfigDict(populate_by_name=True)


class DataChartData(WidgetDataBase):
    title: str = Field(min_length=1, max_length=160)
    body: str | None = Field(default=None, max_length=500)
    chart_type: DataChartType = Field(alias="chartType")
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    x_axis: DataChartAxis = Field(alias="xAxis")
    y_axis: DataChartAxis | None = Field(default=None, alias="yAxis")
    series: list[DataChartSeries] = Field(min_length=1, max_length=8)
    label_keys: list[str] = Field(default_factory=list, alias="labelKeys", max_length=3)
    empty_message: str = Field(default="No data is available for this chart.", alias="emptyMessage", max_length=240)
    query_result: dict[str, Any] | None = Field(default=None, alias="queryResult")
    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def validate_contract(self):
        required = {self.x_axis.key, *self.label_keys}
        if self.y_axis:
            required.add(self.y_axis.key)
        required.update(series.key for series in self.series)
        required.update(series.group_key for series in self.series if series.group_key)
        for row in self.rows:
            missing = required - set(row)
            if missing:
                raise ValueError(f"chart row is missing referenced fields: {sorted(missing)}")
        if self.chart_type == "pie" and (len(self.series) != 1 or len(self.rows) > 12):
            raise ValueError("pie charts require one series and at most twelve slices")
        if self.chart_type == "heatmap" and not self.y_axis:
            raise ValueError("heatmaps require a y-axis dimension")
        return self


class DataVisualizationData(WidgetDataBase):
    """Governed multi-view BI payload with inline, already-authorized data."""

    title: str = Field(min_length=1, max_length=160)
    body: str | None = Field(default=None, max_length=500)
    datasets: dict[str, list[dict[str, Any]]]
    views: list[VisualizationView] = Field(min_length=1, max_length=8)
    layout: VisualizationLayout = Field(default_factory=VisualizationLayout)
    query_results: dict[str, dict[str, Any]] | None = Field(default=None, alias="queryResults")
    empty_message: str = Field(default="No data is available for this visualization.", alias="emptyMessage", max_length=240)
    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def validate_contract(self):
        if set(self.datasets) - {view.dataset for view in self.views}:
            raise ValueError("every dataset must be used by at least one view")
        total_rows = sum(len(rows) for rows in self.datasets.values())
        if total_rows > 800:
            raise ValueError("visualization payload exceeds the governed row budget")
        for view in self.views:
            rows = self.datasets.get(view.dataset)
            if rows is None:
                raise ValueError(f"view references unknown dataset: {view.dataset}")
            encodings = view.encoding
            fields = {
                item.field for item in (
                    encodings.x, encodings.y, encodings.color, encodings.size,
                    encodings.theta, encodings.row, encodings.column, *encodings.tooltip,
                ) if item is not None
            }
            for row in rows:
                missing = fields - set(row)
                if missing:
                    raise ValueError(f"visualization row is missing referenced fields: {sorted(missing)}")
            if view.mark == "rect" and not (encodings.x and encodings.y and encodings.color):
                raise ValueError("rect marks require x, y and color")
            if view.mark == "arc" and not (encodings.theta and encodings.color):
                raise ValueError("arc marks require theta and color")
        return self


class AgentActivityData(WidgetDataBase):
    title: str
    engine: str
    model: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
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
    parent_category: str | None = Field(default=None, alias="parentCategory")
    applies_to_draft: bool = Field(default=False, alias="appliesToDraft")
    draft_id: str | None = Field(default=None, alias="draftId")
    category_id: str | None = Field(default=None, alias="categoryId")
    result_id: str | None = Field(default=None, alias="resultId")


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


class TransactionEditData(WidgetDataBase):
    title: str
    transaction_id: str | None = Field(default=None, alias="transactionId")
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


class TransactionListData(WidgetDataBase):
    title: str
    body: str | None = None
    transactions: list[dict[str, Any]] = Field(default_factory=list)


class FinancialSummaryData(WidgetDataBase):
    title: str
    amount_minor: int = Field(alias="amountMinor")
    currency: str
    count: int = 0
    period: str | None = None
    period_title: str | None = Field(default=None, alias="periodTitle")
    scope_path: list[str] = Field(default_factory=list, alias="scopePath")
    scope_label: str | None = Field(default=None, alias="scopeLabel")
    description: str | None = None
    breakdown: list[dict[str, Any]] = Field(default_factory=list)


class AnalysisTableData(WidgetDataBase):
    title: str
    body: str | None = None
    columns: list[dict[str, Any]] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    budget_room: list[dict[str, Any]] = Field(default_factory=list, alias="budgetRoom")
    query_results: list[dict[str, Any]] = Field(default_factory=list, alias="queryResults")
    transforms: list[dict[str, Any]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    currency: str | None = None


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


class ScenarioAnalysisData(WidgetDataBase):
    title: str
    currency: str
    purchase_minor: int | None = None
    reserve_required_minor: int | None = None
    available_after_reserve_minor: int | None = None
    gap_minor: int | None = None
    monthly_surplus_minor: int | None = None
    months_to_goal: int | None = None
    affordable_now: bool | None = None
    rule: str | None = None
    data_quality: str | dict[str, Any] | None = Field(default=None, alias="dataQuality")


class LoanCalculatorData(WidgetDataBase):
    title: str
    body: str | None = None
    principal_minor: int | None = Field(default=None, alias="principalMinor")
    annual_rate_percent: float | None = Field(default=None, alias="annualRatePercent")
    tenure_months: int | None = Field(default=None, alias="tenureMonths")
    prepayment_minor: int | None = Field(default=None, alias="prepaymentMinor")
    currency: str | None = None
    result: dict[str, Any] | None = None


class LoanStrategyData(WidgetDataBase):
    title: str
    body: str | None = None
    loans: list[dict[str, Any]] = Field(default_factory=list)


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


WIDGET_DATA_MODELS: dict[WidgetType, type[BaseModel]] = {
    WidgetType.AGENT_ACTIVITY: AgentActivityData,
    WidgetType.CATEGORY_SELECTOR: CategorySelectorData,
    WidgetType.TRANSACTION_TYPE_SELECTOR: TransactionTypeSelectorData,
    WidgetType.SUBCATEGORY_SELECTOR: SubcategorySelectorData,
    WidgetType.TAXONOMY_EDITOR: TaxonomyEditorData,
    WidgetType.ACCOUNT_SELECTOR: AccountSelectorData,
    WidgetType.CONFIRMATION_CARD: ConfirmationCardData,
    WidgetType.TRANSACTION_PREVIEW: TransactionPreviewData,
    WidgetType.TRANSACTION_EDIT: TransactionEditData,
    WidgetType.TRANSACTION_LIST: TransactionListData,
    WidgetType.DATA_TABLE: DataTableData,
    WidgetType.DATA_CHART: DataChartData,
    WidgetType.DATA_VISUALIZATION: DataVisualizationData,
    WidgetType.FINANCIAL_SUMMARY: FinancialSummaryData,
    WidgetType.ANALYSIS_TABLE: AnalysisTableData,
    WidgetType.AVOIDABLE_EXPENSES: AvoidableExpensesData,
    WidgetType.INSIGHT_CARD: InsightCardData,
    WidgetType.BUDGET_PROGRESS: BudgetProgressData,
    WidgetType.GOAL_PROGRESS: GoalProgressData,
    WidgetType.SCENARIO_ANALYSIS: ScenarioAnalysisData,
    WidgetType.LOAN_CALCULATOR: LoanCalculatorData,
    WidgetType.LOAN_STRATEGY: LoanStrategyData,
    WidgetType.INVESTMENT_PROJECTION: InvestmentProjectionData,
    WidgetType.RECONCILIATION_REVIEW: ReconciliationReviewData,
    WidgetType.IMPORT_REVIEW: ImportReviewData,
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
    model_config = ConfigDict(populate_by_name=True)


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


class BootstrapResponse(BaseModel):
    user: BootstrapUser
    active_conversation: ConversationOut


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


class OverviewOut(BaseModel):
    period: OverviewPeriodOut
    summary: OverviewSummaryOut
    categories: list[OverviewCategoryOut]


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


class TransactionUpdateIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    amount_minor: int = Field(alias="amountMinor", gt=0)
    merchant: str | None = Field(default=None, max_length=160)
    transaction_at: datetime = Field(alias="transactionAt")
    transaction_type: TransactionType = Field(alias="transactionType")
    category_id: UUID | None = Field(default=None, alias="categoryId")
    subcategory_id: UUID | None = Field(default=None, alias="subcategoryId")
    spend_nature: SpendNature = Field(alias="spendNature")
    location: str | None = Field(default=None, max_length=160)


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


class DataDeletionIn(BaseModel):
    confirmation: Literal["DELETE MY DATA"]


class AgentModelSet(BaseModel):
    router: str
    transaction: str
    analysis: str
    validator: str
    reconciliation: str


AgentMode = Literal["llm", "deterministic_fallback"]


class HealthOut(BaseModel):
    status: Literal["ok"]
    time: datetime
    database: Literal["postgresql", "sqlite"]
    agent_mode: AgentMode
    models: AgentModelSet | None = None


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


class AuthStatusOut(BaseModel):
    authenticated: bool
    profile: ProfileOut | None = None
    google_sign_in_available: bool = Field(serialization_alias="googleSignInAvailable")
    # Only ever populated for a client that declared it cannot hold a cookie.
    # A browser must not receive this: the session is deliberately `httponly`
    # there, and handing the same value back in a readable body would put it
    # right back within reach of script on the page.
    session_token: str | None = Field(default=None, serialization_alias="sessionToken")


class SignOutOut(BaseModel):
    signed_out: Literal[True] = Field(serialization_alias="signedOut")


class PrivacyStatusOut(BaseModel):
    location_enabled: bool = Field(serialization_alias="locationEnabled")
    sources: dict[str, bool]
    retention: Literal["until_deleted"]


class LocationPreferenceOut(BaseModel):
    location_enabled: bool = Field(serialization_alias="locationEnabled")


class SourceRevocationOut(BaseModel):
    source_type: str = Field(serialization_alias="sourceType")
    active: Literal[False]


class DataDeletionOut(BaseModel):
    deleted: Literal[True]


class AgentActivityEvent(BaseModel):
    id: str
    label: str
    status: ExecutionStatus
    tool: str | None = None
    detail: str | None = None
    badge: str | None = None
    duration_ms: float = Field(alias="durationMs", ge=0)
    cumulative_ms: float = Field(alias="cumulativeMs", ge=0)
    model_config = ConfigDict(populate_by_name=True)


class StreamErrorEvent(BaseModel):
    message: str
    error_type: str = Field(alias="errorType")
    model_config = ConfigDict(populate_by_name=True)
