from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from typing import cast, get_args, Literal

from pydantic import BaseModel, Field
from sqlalchemy import Table

from .. import models as _models  # noqa: F401 - registers every SQLAlchemy mapper
from ..database import Base
from ..domain import ACTIVE_STATUS, TransactionType
from ..validation import SemanticIdentifier


TimeGrain = Literal["minute", "hour", "day", "week", "month", "quarter", "year"]
CalendarTimeGrain = Literal["day", "week", "month", "quarter", "year"]
TimeComponent = Literal["hour_of_day", "day_of_week", "day_of_month", "month_of_year"]
FilterOperator = Literal["eq", "neq", "in", "not_in", "contains", "gt", "gte", "lt", "lte", "between"]
SortDirection = Literal["asc", "desc"]
TIME_GRAINS = get_args(TimeGrain)
CALENDAR_TIME_GRAINS = get_args(CalendarTimeGrain)
TIME_COMPONENTS = get_args(TimeComponent)
SUBDAY_TIME_GRAINS = frozenset({"minute", "hour"})


@dataclass(frozen=True)
class TimeGrainSpec:
    """Cross-layer metadata for one governed temporal grain."""

    sqlite_format: str | None
    postgres_format: str
    cadence: str
    aliases: frozenset[str]


TIME_GRAIN_SPECS: dict[TimeGrain, TimeGrainSpec] = {
    "minute": TimeGrainSpec("%Y-%m-%d %H:%M", "YYYY-MM-DD HH24:MI", "minutely", frozenset({"minute", "minutes", "minutely"})),
    "hour": TimeGrainSpec("%Y-%m-%d %H:00", "YYYY-MM-DD HH24:00", "hourly", frozenset({"hour", "hours", "hourly"})),
    "day": TimeGrainSpec("%Y-%m-%d", "YYYY-MM-DD", "daily", frozenset({"day", "days", "daily"})),
    "week": TimeGrainSpec("%Y-W%W", 'IYYY-"W"IW', "weekly", frozenset({"week", "weeks", "weekly"})),
    "month": TimeGrainSpec("%Y-%m", "YYYY-MM", "monthly", frozenset({"month", "months", "monthly"})),
    "quarter": TimeGrainSpec(None, 'YYYY-"Q"Q', "quarterly", frozenset({"quarter", "quarters", "quarterly"})),
    "year": TimeGrainSpec("%Y", "YYYY", "yearly", frozenset({"year", "years", "yearly", "annual", "annually"})),
}
if set(TIME_GRAIN_SPECS) != set(TIME_GRAINS):
    raise RuntimeError("Every governed time grain needs one metadata specification")


class SemanticField(BaseModel):
    name: SemanticIdentifier
    column: SemanticIdentifier
    data_type: Literal["uuid", "string", "integer", "decimal", "date", "datetime", "boolean", "json"]
    semantic_type: Literal["identifier", "money_minor", "currency", "date", "time", "category", "merchant", "account", "status", "percentage", "duration_months", "text", "enum", "count"]
    description: str
    filter_operators: list[FilterOperator] = Field(default_factory=list)
    selectable: bool = True
    sensitive: bool = False


class SemanticEntity(BaseModel):
    name: SemanticIdentifier
    table: SemanticIdentifier
    description: str
    grain: str
    tenant_key: SemanticIdentifier | None = None
    soft_delete_key: SemanticIdentifier | None = None
    event_date_key: SemanticIdentifier | None = None
    event_time_key: SemanticIdentifier | None = None
    supported_time_grains: list[TimeGrain] = Field(default_factory=list)
    time_semantics: Literal["event", "snapshot", "reference"] = "reference"
    fields: list[SemanticField]


class SemanticRelationship(BaseModel):
    name: SemanticIdentifier
    source_entity: SemanticIdentifier
    target_entity: SemanticIdentifier
    source_field: SemanticIdentifier
    target_field: SemanticIdentifier
    cardinality: Literal["many_to_one", "one_to_many", "one_to_one", "many_to_many"]
    default_join: Literal["inner", "left"] = "left"
    queryable: bool = True
    fanout_risk: bool = False
    description: str


class SemanticMetric(BaseModel):
    name: SemanticIdentifier
    base_entity: SemanticIdentifier
    aggregation: Literal["sum", "count", "average", "minimum", "maximum", "conditional_sum", "net_sum", "cash_flow_sum"]
    field: SemanticIdentifier | None = None
    result_type: Literal["money_minor", "count", "decimal"]
    time_semantics: Literal["event_window", "current_snapshot"]
    description: str
    fixed_filters: dict[str, str | list[str]] = Field(default_factory=dict)


class SemanticDimension(BaseModel):
    name: SemanticIdentifier
    base_entity: SemanticIdentifier
    field: SemanticIdentifier
    projection_field: SemanticIdentifier | None = None
    relationship_path: list[SemanticIdentifier] = Field(default_factory=list)
    transform: Literal["identity", "month"] = "identity"
    null_label: str | None = None
    description: str


class SemanticPolicy(BaseModel):
    max_window_days: int = 1830
    max_result_rows: int = 250
    max_queries_per_plan: int = 12
    max_relationship_hops: int = 3
    max_relationships_per_query: int = 6
    max_estimated_cells: int = 10_000
    require_tenant_scope: bool = True
    # The default analysis lane is arbitrary read-only SQL behind tenant RLS.
    # These flags describe analytical freedom, not database isolation.
    disallow_raw_sql: bool = False
    disallow_sensitive_projection: bool = False


class SemanticSchemaRegistry(BaseModel):
    version: str
    schema_hash: str
    entities: list[SemanticEntity]
    relationships: list[SemanticRelationship]
    metrics: list[SemanticMetric]
    dimensions: list[SemanticDimension]
    policy: SemanticPolicy
    financial_rules: list[str]

    @property
    def entities_by_name(self) -> dict[str, SemanticEntity]:
        return {item.name: item for item in self.entities}

    @property
    def relationships_by_name(self) -> dict[str, SemanticRelationship]:
        return {item.name: item for item in self.relationships}

    @property
    def metrics_by_name(self) -> dict[str, SemanticMetric]:
        return {item.name: item for item in self.metrics}

    def dimensions_for(self, entity: str) -> dict[str, SemanticDimension]:
        return {
            item.name: item
            for item in self.dimensions
            if item.base_entity == entity
        }

    def prompt_contract(self) -> dict:
        """Return the complete governed contract without runtime Python bindings."""
        return self.model_dump(mode="json")

    def routing_contract(self) -> dict:
        """Small capability map for intent routing; planning still uses the full contract."""
        return {
            "version": self.version,
            "metrics": [
                {"name": metric.name, "entity": metric.base_entity, "time_semantics": metric.time_semantics}
                for metric in self.metrics
            ],
            "dimensions_by_entity": {
                entity.name: sorted({item.name for item in self.dimensions if item.base_entity == entity.name})
                for entity in self.entities
                if any(item.base_entity == entity.name for item in self.dimensions)
            },
            "temporal_capabilities": {
                entity.name: {
                    "event_date_key": entity.event_date_key,
                    "event_time_key": entity.event_time_key,
                    "supported_grains": entity.supported_time_grains,
                    "output_field": "time_bucket",
                }
                for entity in self.entities
                if entity.event_date_key
            },
        }


def _field(name: str, column: str, data_type: str, semantic_type: str, description: str, *operators: str, sensitive: bool = False) -> SemanticField:
    expanded_operators = list(operators)
    # Categorical dimensions must support the full positive/negative set.
    # This is semantic capability metadata, not arbitrary SQL exposure.
    if semantic_type in {"category", "merchant", "account", "status", "enum", "currency"}:
        if "eq" in expanded_operators and "neq" not in expanded_operators:
            expanded_operators.append("neq")
        if "in" in expanded_operators and "not_in" not in expanded_operators:
            expanded_operators.append("not_in")
    return SemanticField(
        name=name,
        column=column,
        data_type=data_type,
        semantic_type=semantic_type,
        description=description,
        filter_operators=expanded_operators,
        sensitive=sensitive,
    )


MODEL_BINDINGS = {
    cast(Table, mapper.local_table).name: mapper.class_
    for mapper in Base.registry.mappers
}


@lru_cache(maxsize=1)
def semantic_schema_registry() -> SemanticSchemaRegistry:
    entities = [
        SemanticEntity(name="transactions", table="transactions", description="Canonical financial events after reconciliation.", grain="one canonical real-world financial event", tenant_key="user_id", soft_delete_key="deleted_at", event_date_key="transaction_at", event_time_key="transaction_at", supported_time_grains=list(TIME_GRAINS), time_semantics="event", fields=[
            _field("id", "id", "uuid", "identifier", "Canonical transaction identifier.", "eq", "in"),
            _field("transaction_type", "transaction_type", "string", "enum", "Expense, income, transfer, investment, loan payment, refund, reimbursement, cash withdrawal or deposit.", "eq", "neq", "in"),
            _field("amount", "amount_minor", "integer", "money_minor", "Exact amount in currency minor units; never floating point.", "eq", "gt", "gte", "lt", "lte", "between"),
            _field("currency", "currency", "string", "currency", "ISO-4217 transaction currency.", "eq", "in"),
            _field("merchant", "merchant_name", "string", "merchant", "Canonical display merchant while preserving source descriptions separately.", "eq", "neq", "in", "contains"),
            _field("transaction_date", "transaction_at", "datetime", "date", "UTC instant of the financial event, projected to the requested timezone for calendar operations.", "eq", "between", "gte", "lte"),
            _field("transaction_time", "transaction_at", "datetime", "time", "UTC instant of the financial event, projected to the requested timezone for sub-day operations.", "eq", "gte", "lte"),
            _field("posted_date", "posted_at", "datetime", "date", "UTC instant when the source posted the transaction.", "eq", "between", "gte", "lte"),
            _field("spend_nature", "spend_nature", "string", "enum", "User/inference label: essential, discretionary, potentially avoidable or unknown.", "eq", "in"),
            _field("status", "status", "string", "status", "Provisional or source-confirmed canonical status.", "eq", "in"),
            _field("location", "location_label", "string", "text", "Coarse transaction location label when permitted.", "eq", "contains", sensitive=True),
            _field("description", "description", "string", "text", "User/source transaction description.", "contains", sensitive=True),
        ]),
        SemanticEntity(name="accounts", table="accounts", description="User-owned financial accounts and current recorded balances.", grain="one user account", tenant_key="user_id", time_semantics="snapshot", fields=[
            _field("id", "id", "uuid", "identifier", "Account identifier.", "eq", "in"),
            _field("account", "name", "string", "account", "User-facing account name.", "eq", "in", "contains"),
            _field("account_type", "account_type", "string", "enum", "Bank, card, cash or investment account type.", "eq", "in"),
            _field("institution", "institution", "string", "text", "Financial institution name.", "eq", "contains"),
            _field("currency", "currency", "string", "currency", "Account currency.", "eq", "in"),
            _field("balance", "balance_minor", "integer", "money_minor", "Current recorded account balance in minor units.", "gt", "gte", "lt", "lte"),
        ]),
        SemanticEntity(name="account_balance_snapshots", table="account_balance_snapshots", description="Historical point-in-time account balances for net-worth and liquidity trends.", grain="one account balance observation", tenant_key="user_id", event_date_key="observed_at", supported_time_grains=list(CALENDAR_TIME_GRAINS), time_semantics="event", fields=[
            _field("id", "id", "uuid", "identifier", "Balance snapshot identifier.", "eq", "in"),
            _field("balance", "balance_minor", "integer", "money_minor", "Observed account balance in minor units.", "gt", "gte", "lt", "lte"),
            _field("currency", "currency", "string", "currency", "Snapshot currency.", "eq", "in"),
            _field("observed_at", "observed_at", "datetime", "date", "When the balance was observed.", "between", "gte", "lte"),
            _field("source_type", "source_type", "string", "enum", "Manual, bank or imported balance source.", "eq", "in"),
        ]),
        SemanticEntity(name="investment_holdings", table="investment_holdings", description="Current investment positions with exact quantity, cost basis and latest valuation.", grain="one investment holding", tenant_key="user_id", time_semantics="snapshot", fields=[
            _field("id", "id", "uuid", "identifier", "Holding identifier.", "eq", "in"),
            _field("holding", "name", "string", "text", "Investment holding name.", "eq", "contains"),
            _field("symbol", "symbol", "string", "text", "Ticker or fund symbol when available.", "eq", "in", "contains"),
            _field("asset_type", "asset_type", "string", "enum", "Equity, mutual fund, bond, deposit, crypto or other asset type.", "eq", "in"),
            _field("quantity", "quantity", "decimal", "count", "Exact holding quantity, never used as a money value.", "gt", "gte", "lt", "lte"),
            _field("cost_basis", "cost_basis_minor", "integer", "money_minor", "Total recorded cost basis in minor units.", "gt", "gte", "lt", "lte"),
            _field("current_value", "current_value_minor", "integer", "money_minor", "Latest recorded market value in minor units.", "gt", "gte", "lt", "lte"),
            _field("currency", "currency", "string", "currency", "Holding valuation currency.", "eq", "in"),
            _field("status", "status", "string", "status", "Active or closed position status.", "eq", "in"),
        ]),
        SemanticEntity(name="investment_valuation_snapshots", table="investment_valuation_snapshots", description="Historical investment valuations for portfolio performance trends.", grain="one holding valuation observation", tenant_key="user_id", event_date_key="observed_at", supported_time_grains=list(CALENDAR_TIME_GRAINS), time_semantics="event", fields=[
            _field("id", "id", "uuid", "identifier", "Valuation snapshot identifier.", "eq", "in"),
            _field("market_value", "market_value_minor", "integer", "money_minor", "Observed market value in minor units.", "gt", "gte", "lt", "lte"),
            _field("cost_basis", "cost_basis_minor", "integer", "money_minor", "Cost basis at observation time in minor units.", "gt", "gte", "lt", "lte"),
            _field("currency", "currency", "string", "currency", "Valuation currency.", "eq", "in"),
            _field("observed_at", "observed_at", "datetime", "date", "When the holding was valued.", "between", "gte", "lte"),
            _field("source_type", "source_type", "string", "enum", "Manual, broker or imported valuation source.", "eq", "in"),
        ]),
        SemanticEntity(name="categories", table="categories", description="Expense taxonomy categories.", grain="one category", time_semantics="reference", fields=[
            _field("id", "id", "uuid", "identifier", "Category identifier.", "eq", "in"),
            _field("category", "slug", "string", "category", "Stable category slug.", "eq", "in"),
            _field("category_name", "name", "string", "category", "Display category name.", "eq", "in", "contains"),
        ]),
        SemanticEntity(name="subcategories", table="subcategories", description="Expense taxonomy subcategories belonging to a category.", grain="one subcategory", time_semantics="reference", fields=[
            _field("id", "id", "uuid", "identifier", "Subcategory identifier.", "eq", "in"),
            _field("subcategory", "slug", "string", "category", "Stable subcategory slug.", "eq", "in"),
            _field("subcategory_name", "name", "string", "category", "Display subcategory name.", "eq", "in", "contains"),
        ]),
        SemanticEntity(name="merchants", table="merchants", description="Canonical merchants and normalization targets.", grain="one canonical merchant", time_semantics="reference", fields=[
            _field("id", "id", "uuid", "identifier", "Merchant identifier.", "eq", "in"),
            _field("merchant", "canonical_name", "string", "merchant", "Canonical merchant name.", "eq", "in", "contains"),
        ]),
        SemanticEntity(name="tags", table="tags", description="User-owned transaction tags.", grain="one user tag", tenant_key="user_id", time_semantics="reference", fields=[
            _field("id", "id", "uuid", "identifier", "Tag identifier.", "eq", "in"),
            _field("tag", "normalized_name", "string", "category", "Normalized user tag.", "eq", "in"),
            _field("tag_name", "name", "string", "category", "User-facing tag name.", "eq", "in", "contains"),
        ]),
        SemanticEntity(name="transaction_sources", table="transaction_sources", description="Provenance observations attached to canonical transactions.", grain="one source observation attached to a transaction", time_semantics="event", event_date_key="observed_at", supported_time_grains=list(CALENDAR_TIME_GRAINS), fields=[
            _field("id", "id", "uuid", "identifier", "Source record identifier.", "eq", "in"),
            _field("source_type", "source_type", "string", "enum", "Manual, SMS, email, bank, CSV, PDF, receipt or API.", "eq", "in"),
            _field("observed_at", "observed_at", "datetime", "date", "When this source was observed.", "between", "gte", "lte"),
            _field("confidence", "confidence", "decimal", "percentage", "Source confidence.", "gt", "gte", "lt", "lte"),
        ]),
        SemanticEntity(name="financial_observations", table="financial_observations", description="Idempotently ingested source events before or after canonical reconciliation. Observation totals are data-quality measures and must never be labelled canonical spending.", grain="one ingested financial observation", tenant_key="user_id", event_date_key="transaction_at", supported_time_grains=list(CALENDAR_TIME_GRAINS), time_semantics="event", fields=[
            _field("id", "id", "uuid", "identifier", "Financial observation identifier.", "eq", "in"),
            _field("source_type", "source_type", "string", "enum", "Manual, SMS, email, bank, CSV, PDF, receipt or API source.", "eq", "in"),
            _field("processing_state", "processing_state", "string", "status", "Received, attached, review or other ingestion state.", "eq", "in"),
            _field("transaction_type", "transaction_type", "string", "enum", "Observed financial direction before canonical resolution.", "eq", "in"),
            _field("amount", "amount_minor", "integer", "money_minor", "Observed amount in minor units; may duplicate another source observation.", "eq", "gt", "gte", "lt", "lte", "between"),
            _field("currency", "currency", "string", "currency", "Observed ISO-4217 currency.", "eq", "in"),
            _field("merchant", "merchant_normalized", "string", "merchant", "Normalized observed merchant, when present.", "eq", "in", "contains"),
            _field("transaction_date", "transaction_at", "datetime", "date", "UTC instant of the observed event.", "eq", "between", "gte", "lte"),
            _field("posted_date", "posted_at", "datetime", "date", "UTC instant of source posting.", "eq", "between", "gte", "lte"),
            _field("confidence", "confidence", "decimal", "percentage", "Parsing confidence for the observation.", "gt", "gte", "lt", "lte"),
        ]),
        SemanticEntity(name="budgets", table="budgets", description="User budget limits, optionally by category.", grain="one budget", tenant_key="user_id", time_semantics="snapshot", fields=[
            _field("id", "id", "uuid", "identifier", "Budget identifier.", "eq", "in"),
            _field("budget", "name", "string", "text", "Budget name.", "eq", "contains"),
            _field("amount", "amount_minor", "integer", "money_minor", "Budget limit in minor units.", "gt", "gte", "lt", "lte"),
            _field("period", "period", "string", "enum", "Budget cadence such as monthly.", "eq", "in"),
            _field("currency", "currency", "string", "currency", "Budget currency.", "eq", "in"),
        ]),
        SemanticEntity(name="goals", table="goals", description="User savings goals and current progress.", grain="one savings goal", tenant_key="user_id", time_semantics="snapshot", fields=[
            _field("id", "id", "uuid", "identifier", "Goal identifier.", "eq", "in"),
            _field("goal", "name", "string", "text", "Goal name.", "eq", "contains"),
            _field("target", "target_minor", "integer", "money_minor", "Goal target in minor units.", "gt", "gte", "lt", "lte"),
            _field("current", "current_minor", "integer", "money_minor", "Current saved amount in minor units.", "gt", "gte", "lt", "lte"),
            _field("target_date", "target_date", "date", "date", "Desired completion date.", "between", "gte", "lte"),
            _field("currency", "currency", "string", "currency", "Goal currency.", "eq", "in"),
        ]),
        SemanticEntity(name="goal_contributions", table="goal_contributions", description="Historical contributions allocated to savings goals.", grain="one goal contribution", tenant_key="user_id", event_date_key="contribution_at", supported_time_grains=list(CALENDAR_TIME_GRAINS), time_semantics="event", fields=[
            _field("id", "id", "uuid", "identifier", "Goal contribution identifier.", "eq", "in"),
            _field("amount", "amount_minor", "integer", "money_minor", "Contribution amount in minor units.", "gt", "gte", "lt", "lte"),
            _field("currency", "currency", "string", "currency", "Contribution currency.", "eq", "in"),
            _field("contribution_date", "contribution_at", "datetime", "date", "UTC instant when the amount was allocated, projected to the requested timezone for calendar operations.", "between", "gte", "lte"),
        ]),
        SemanticEntity(name="loans", table="loans", description="Saved active and inactive loan profiles.", grain="one loan", tenant_key="user_id", time_semantics="snapshot", fields=[
            _field("id", "id", "uuid", "identifier", "Loan identifier.", "eq", "in"),
            _field("loan", "name", "string", "text", "Loan name.", "eq", "contains"),
            _field("loan_type", "loan_type", "string", "enum", "Home, vehicle, personal or other loan type.", "eq", "in"),
            _field("lender", "lender", "string", "text", "Lender name.", "eq", "contains"),
            _field("direction", "direction", "string", "enum", "User-relative position: borrowed is a payable; lent is a receivable.", "eq", "in"),
            _field("counterparty", "counterparty_name", "string", "text", "The other person in a shared personal-loan record.", "eq", "contains"),
            _field("outstanding_principal", "outstanding_principal_minor", "integer", "money_minor", "Current outstanding principal in minor units.", "gt", "gte", "lt", "lte"),
            _field("accrued_interest", "accrued_interest_minor", "integer", "money_minor", "Expected interest still open in minor units.", "gt", "gte", "lt", "lte"),
            _field("annual_rate", "annual_rate_percent", "decimal", "percentage", "Annual interest percentage.", "gt", "gte", "lt", "lte"),
            _field("remaining_tenure", "remaining_tenure_months", "integer", "duration_months", "Remaining tenure in months.", "gt", "gte", "lt", "lte"),
            _field("current_emi", "current_emi_minor", "integer", "money_minor", "Current EMI in minor units.", "gt", "gte", "lt", "lte"),
            _field("next_due_date", "next_due_date", "date", "date", "Next expected return or payment date.", "between", "gte", "lte"),
            _field("next_due_amount", "next_due_minor", "integer", "money_minor", "Next expected return or payment amount.", "gt", "gte", "lt", "lte"),
            _field("response_needed", "response_needed", "boolean", "enum", "Whether this user needs to acknowledge or confirm shared state.", "eq"),
            _field("currency", "currency", "string", "currency", "Loan currency.", "eq", "in"),
            _field("status", "status", "string", "status", "Active or inactive loan status.", "eq", "in"),
        ]),
        SemanticEntity(name="recurring_transactions", table="recurring_transactions", description="Detected recurring financial patterns.", grain="one recurring pattern", tenant_key="user_id", time_semantics="snapshot", fields=[
            _field("id", "id", "uuid", "identifier", "Recurring pattern identifier.", "eq", "in"),
            _field("expected_amount", "expected_amount_minor", "integer", "money_minor", "Expected recurring amount in minor units.", "gt", "gte", "lt", "lte"),
            _field("currency", "currency", "string", "currency", "Recurring amount currency.", "eq", "in"),
            _field("cadence", "cadence", "string", "enum", "Weekly, monthly or other cadence.", "eq", "in"),
            _field("next_expected_date", "next_expected_date", "date", "date", "Next predicted occurrence.", "between", "gte", "lte"),
            _field("confidence", "confidence", "decimal", "percentage", "Pattern confidence.", "gt", "gte", "lt", "lte"),
        ]),
        SemanticEntity(name="subscriptions", table="subscriptions", description="Subscription records backed by recurring patterns.", grain="one subscription", tenant_key="user_id", time_semantics="snapshot", fields=[
            _field("id", "id", "uuid", "identifier", "Subscription identifier.", "eq", "in"),
            _field("subscription", "name", "string", "text", "Subscription name.", "eq", "contains"),
            _field("status", "status", "string", "status", "Active or inactive subscription status.", "eq", "in"),
        ]),
    ]
    relationships = [
        SemanticRelationship(name="transaction_category", source_entity="transactions", target_entity="categories", source_field="category_id", target_field="id", cardinality="many_to_one", description="Each categorized transaction references one category."),
        SemanticRelationship(name="transaction_subcategory", source_entity="transactions", target_entity="subcategories", source_field="subcategory_id", target_field="id", cardinality="many_to_one", description="Each categorized transaction may reference one subcategory."),
        SemanticRelationship(name="transaction_account", source_entity="transactions", target_entity="accounts", source_field="account_id", target_field="id", cardinality="many_to_one", description="A transaction may be assigned to one source account."),
        SemanticRelationship(name="transaction_merchant", source_entity="transactions", target_entity="merchants", source_field="merchant_id", target_field="id", cardinality="many_to_one", queryable=False, description="Canonical merchant lineage; analytical merchant queries use the preserved transaction merchant field."),
        SemanticRelationship(name="transaction_tags", source_entity="transactions", target_entity="tags", source_field="id", target_field="id", cardinality="many_to_many", fanout_risk=True, description="Transactions may have multiple user tags through transaction_tags."),
        SemanticRelationship(name="transaction_sources", source_entity="transactions", target_entity="transaction_sources", source_field="id", target_field="transaction_id", cardinality="one_to_many", queryable=False, fanout_risk=True, description="Provenance context only; source aggregation is not exposed to generated queries."),
        SemanticRelationship(name="balance_snapshot_account", source_entity="account_balance_snapshots", target_entity="accounts", source_field="account_id", target_field="id", cardinality="many_to_one", description="Each balance observation belongs to one user account."),
        SemanticRelationship(name="holding_account", source_entity="investment_holdings", target_entity="accounts", source_field="account_id", target_field="id", cardinality="many_to_one", description="An investment holding may belong to one investment account."),
        SemanticRelationship(name="valuation_holding", source_entity="investment_valuation_snapshots", target_entity="investment_holdings", source_field="holding_id", target_field="id", cardinality="many_to_one", description="Each valuation observation belongs to one holding."),
        SemanticRelationship(name="budget_category", source_entity="budgets", target_entity="categories", source_field="category_id", target_field="id", cardinality="many_to_one", description="A budget may constrain one category."),
        SemanticRelationship(name="contribution_goal", source_entity="goal_contributions", target_entity="goals", source_field="goal_id", target_field="id", cardinality="many_to_one", description="Each contribution is allocated to one savings goal."),
        SemanticRelationship(name="loan_account", source_entity="loans", target_entity="accounts", source_field="account_id", target_field="id", cardinality="many_to_one", queryable=False, description="Loan-account context only until an account dimension is defined for loans."),
        SemanticRelationship(name="recurring_merchant", source_entity="recurring_transactions", target_entity="merchants", source_field="merchant_id", target_field="id", cardinality="many_to_one", description="A recurring pattern may reference a canonical merchant."),
        SemanticRelationship(name="subscription_recurring", source_entity="subscriptions", target_entity="recurring_transactions", source_field="recurring_transaction_id", target_field="id", cardinality="one_to_one", queryable=False, description="Subscription recurrence context only until recurring dimensions are exposed for subscriptions."),
    ]
    metrics = [
        SemanticMetric(name="gross_spend", base_entity="transactions", aggregation="conditional_sum", field="amount", result_type="money_minor", time_semantics="event_window", fixed_filters={"transaction_type": TransactionType.EXPENSE}, description="Sum of expense transactions; transfers, refunds, income and loan payments are excluded."),
        SemanticMetric(name="net_spend", base_entity="transactions", aggregation="net_sum", field="amount", result_type="money_minor", time_semantics="event_window", description="Expenses less refunds and reimbursements."),
        SemanticMetric(name="income", base_entity="transactions", aggregation="conditional_sum", field="amount", result_type="money_minor", time_semantics="event_window", fixed_filters={"transaction_type": TransactionType.INCOME}, description="Sum of income transactions."),
        SemanticMetric(name="debt_service", base_entity="transactions", aggregation="conditional_sum", field="amount", result_type="money_minor", time_semantics="event_window", fixed_filters={"transaction_type": TransactionType.LOAN_PAYMENT}, description="Sum of loan-payment transactions."),
        SemanticMetric(name="refunds_received", base_entity="transactions", aggregation="conditional_sum", field="amount", result_type="money_minor", time_semantics="event_window", fixed_filters={"transaction_type": TransactionType.REFUND}, description="Canonical refunds received in the selected event window."),
        SemanticMetric(name="reimbursements_received", base_entity="transactions", aggregation="conditional_sum", field="amount", result_type="money_minor", time_semantics="event_window", fixed_filters={"transaction_type": TransactionType.REIMBURSEMENT}, description="Canonical reimbursements received in the selected event window."),
        SemanticMetric(name="investment_contributions", base_entity="transactions", aggregation="conditional_sum", field="amount", result_type="money_minor", time_semantics="event_window", fixed_filters={"transaction_type": TransactionType.INVESTMENT}, description="Recorded investment contributions; this is cash contributed, not current portfolio value or return."),
        SemanticMetric(name="cash_withdrawals", base_entity="transactions", aggregation="conditional_sum", field="amount", result_type="money_minor", time_semantics="event_window", fixed_filters={"transaction_type": TransactionType.CASH_WITHDRAWAL}, description="Recorded cash withdrawn from financial accounts."),
        SemanticMetric(name="cash_deposits", base_entity="transactions", aggregation="conditional_sum", field="amount", result_type="money_minor", time_semantics="event_window", fixed_filters={"transaction_type": TransactionType.CASH_DEPOSIT}, description="Recorded cash deposited into financial accounts."),
        SemanticMetric(name="transfer_volume", base_entity="transactions", aggregation="conditional_sum", field="amount", result_type="money_minor", time_semantics="event_window", fixed_filters={"transaction_type": TransactionType.TRANSFER}, description="Recorded transfer volume. Transfers are excluded from spending, income and net cash flow."),
        SemanticMetric(name="net_cash_flow", base_entity="transactions", aggregation="cash_flow_sum", field="amount", result_type="money_minor", time_semantics="event_window", description="Cash inflows minus outflows, excluding transfers. Income, refunds, reimbursements and cash deposits are positive; expenses, loan payments, investments and cash withdrawals are negative."),
        SemanticMetric(name="transaction_amount", base_entity="transactions", aggregation="sum", field="amount", result_type="money_minor", time_semantics="event_window", description="Sum of recorded absolute transaction amounts. Mixed financial directions must be separated by transaction_type; this is not net cash flow, spending, or income."),
        SemanticMetric(name="transaction_count", base_entity="transactions", aggregation="count", result_type="count", time_semantics="event_window", description="Count of matching canonical transactions."),
        SemanticMetric(name="average_expense", base_entity="transactions", aggregation="average", field="amount", result_type="money_minor", time_semantics="event_window", fixed_filters={"transaction_type": TransactionType.EXPENSE}, description="Average expense amount."),
        SemanticMetric(name="largest_expense", base_entity="transactions", aggregation="maximum", field="amount", result_type="money_minor", time_semantics="event_window", fixed_filters={"transaction_type": TransactionType.EXPENSE}, description="Largest individual canonical expense amount in the selected window."),
        SemanticMetric(name="smallest_expense", base_entity="transactions", aggregation="minimum", field="amount", result_type="money_minor", time_semantics="event_window", fixed_filters={"transaction_type": TransactionType.EXPENSE}, description="Smallest individual canonical expense amount in the selected window."),
        SemanticMetric(name="account_balance", base_entity="accounts", aggregation="sum", field="balance", result_type="money_minor", time_semantics="current_snapshot", description="Sum of current recorded account balances."),
        SemanticMetric(name="historical_account_balance", base_entity="account_balance_snapshots", aggregation="sum", field="balance", result_type="money_minor", time_semantics="event_window", description="Sum of recorded account balance observations. For net-worth trends, group by time and account to avoid summing multiple observations from the same account in one bucket without an explicit snapshot policy."),
        SemanticMetric(name="portfolio_value", base_entity="investment_holdings", aggregation="sum", field="current_value", result_type="money_minor", time_semantics="current_snapshot", fixed_filters={"status": ACTIVE_STATUS}, description="Latest recorded value of active investment holdings."),
        SemanticMetric(name="portfolio_cost_basis", base_entity="investment_holdings", aggregation="sum", field="cost_basis", result_type="money_minor", time_semantics="current_snapshot", fixed_filters={"status": ACTIVE_STATUS}, description="Recorded cost basis of active investment holdings."),
        SemanticMetric(name="holding_count", base_entity="investment_holdings", aggregation="count", result_type="count", time_semantics="current_snapshot", fixed_filters={"status": ACTIVE_STATUS}, description="Count of active investment holdings."),
        SemanticMetric(name="historical_portfolio_value", base_entity="investment_valuation_snapshots", aggregation="sum", field="market_value", result_type="money_minor", time_semantics="event_window", description="Historical observed market value. Group by time and holding when several observations can occur in one bucket."),
        SemanticMetric(name="historical_portfolio_cost", base_entity="investment_valuation_snapshots", aggregation="sum", field="cost_basis", result_type="money_minor", time_semantics="event_window", description="Historical observed cost basis for portfolio performance comparisons."),
        SemanticMetric(name="budget_limit", base_entity="budgets", aggregation="sum", field="amount", result_type="money_minor", time_semantics="current_snapshot", description="Sum of configured budget limits."),
        SemanticMetric(name="goal_target", base_entity="goals", aggregation="sum", field="target", result_type="money_minor", time_semantics="current_snapshot", description="Sum of savings goal targets."),
        SemanticMetric(name="goal_saved", base_entity="goals", aggregation="sum", field="current", result_type="money_minor", time_semantics="current_snapshot", description="Sum currently saved toward goals."),
        SemanticMetric(name="goal_contribution_amount", base_entity="goal_contributions", aggregation="sum", field="amount", result_type="money_minor", time_semantics="event_window", description="Sum of historical contributions allocated to savings goals."),
        SemanticMetric(name="loan_outstanding", base_entity="loans", aggregation="sum", field="outstanding_principal", result_type="money_minor", time_semantics="current_snapshot", fixed_filters={"status": ACTIVE_STATUS}, description="Outstanding principal across active loans."),
        SemanticMetric(name="loan_emi", base_entity="loans", aggregation="sum", field="current_emi", result_type="money_minor", time_semantics="current_snapshot", fixed_filters={"status": ACTIVE_STATUS}, description="Current EMI total across active loans."),
        SemanticMetric(name="peer_lending_receivable", base_entity="loans", aggregation="sum", field="outstanding_principal", result_type="money_minor", time_semantics="current_snapshot", fixed_filters={"status": ACTIVE_STATUS, "direction": "lent"}, description="Principal still owed to the user across active shared personal-loan plans; this is a receivable asset, not debt."),
        SemanticMetric(name="peer_lending_payable", base_entity="loans", aggregation="sum", field="outstanding_principal", result_type="money_minor", time_semantics="current_snapshot", fixed_filters={"status": ACTIVE_STATUS, "direction": "borrowed"}, description="Principal the user still owes across active shared personal-loan plans; this is a payable obligation."),
        SemanticMetric(name="peer_lending_expected_interest", base_entity="loans", aggregation="sum", field="accrued_interest", result_type="money_minor", time_semantics="current_snapshot", fixed_filters={"status": ACTIVE_STATUS, "direction": "lent"}, description="Expected interest still receivable across active shared personal-loan plans; it is not realized income until a confirmed payment exists."),
        SemanticMetric(name="recurring_expected", base_entity="recurring_transactions", aggregation="sum", field="expected_amount", result_type="money_minor", time_semantics="current_snapshot", description="Expected amount across recurring patterns."),
        SemanticMetric(name="subscription_count", base_entity="subscriptions", aggregation="count", result_type="count", time_semantics="current_snapshot", fixed_filters={"status": ACTIVE_STATUS}, description="Count of active subscriptions."),
        SemanticMetric(name="observation_count", base_entity="financial_observations", aggregation="count", result_type="count", time_semantics="event_window", description="Count of ingested source observations. This is not a count of canonical transactions."),
        SemanticMetric(name="observed_amount", base_entity="financial_observations", aggregation="sum", field="amount", result_type="money_minor", time_semantics="event_window", description="Sum of source-observation amounts for ingestion and reconciliation diagnostics; duplicate observations may represent the same canonical event."),
    ]
    dimensions = [
        SemanticDimension(name="transaction", base_entity="transactions", field="id", description="Unique canonical transaction, used for record-level analysis."),
        SemanticDimension(name="transaction_date", base_entity="transactions", field="transaction_date", description="Calendar date of the transaction event."),
        SemanticDimension(name="month", base_entity="transactions", field="transaction_date", transform="month", description="Calendar month of the transaction event."),
        SemanticDimension(name="category", base_entity="transactions", field="category", projection_field="category_name", relationship_path=["transaction_category"], null_label="Uncategorized", description="Expense category."),
        SemanticDimension(name="subcategory", base_entity="transactions", field="subcategory", projection_field="subcategory_name", relationship_path=["transaction_subcategory"], null_label="Uncategorized", description="Expense subcategory."),
        SemanticDimension(name="merchant", base_entity="transactions", field="merchant", null_label="Unknown merchant", description="Transaction merchant."),
        SemanticDimension(name="transaction_type", base_entity="transactions", field="transaction_type", description="Financial event direction/type."),
        SemanticDimension(name="currency", base_entity="transactions", field="currency", description="Transaction currency."),
        SemanticDimension(name="status", base_entity="transactions", field="status", description="Canonical confirmation status."),
        SemanticDimension(name="location", base_entity="transactions", field="location", null_label="Unknown location", description="Permitted coarse transaction location label."),
        SemanticDimension(name="posted_date", base_entity="transactions", field="posted_date", description="Source posting date, distinct from transaction date."),
        SemanticDimension(name="spend_nature", base_entity="transactions", field="spend_nature", description="Essential/discretionary/avoidable classification."),
        SemanticDimension(name="tag", base_entity="transactions", field="tag", projection_field="tag_name", relationship_path=["transaction_tags"], null_label="Untagged", description="User transaction tag."),
        SemanticDimension(name="account", base_entity="transactions", field="account", relationship_path=["transaction_account"], null_label="Unknown account", description="Transaction source account."),
        SemanticDimension(name="account", base_entity="accounts", field="account", description="Account name."),
        SemanticDimension(name="account_type", base_entity="accounts", field="account_type", description="Account type."),
        SemanticDimension(name="institution", base_entity="accounts", field="institution", null_label="Unknown institution", description="Financial institution."),
        SemanticDimension(name="currency", base_entity="accounts", field="currency", description="Account currency."),
        SemanticDimension(name="account", base_entity="account_balance_snapshots", field="account", relationship_path=["balance_snapshot_account"], description="Account associated with a balance observation."),
        SemanticDimension(name="currency", base_entity="account_balance_snapshots", field="currency", description="Balance snapshot currency."),
        SemanticDimension(name="source_type", base_entity="account_balance_snapshots", field="source_type", description="Balance observation source."),
        SemanticDimension(name="holding", base_entity="investment_holdings", field="holding", description="Investment holding name."),
        SemanticDimension(name="symbol", base_entity="investment_holdings", field="symbol", null_label="No symbol", description="Investment ticker or fund symbol."),
        SemanticDimension(name="asset_type", base_entity="investment_holdings", field="asset_type", description="Investment asset class."),
        SemanticDimension(name="account", base_entity="investment_holdings", field="account", relationship_path=["holding_account"], null_label="Unassigned account", description="Account containing the holding."),
        SemanticDimension(name="currency", base_entity="investment_holdings", field="currency", description="Holding currency."),
        SemanticDimension(name="holding", base_entity="investment_valuation_snapshots", field="holding", relationship_path=["valuation_holding"], description="Holding associated with a valuation observation."),
        SemanticDimension(name="currency", base_entity="investment_valuation_snapshots", field="currency", description="Valuation currency."),
        SemanticDimension(name="source_type", base_entity="investment_valuation_snapshots", field="source_type", description="Valuation source."),
        SemanticDimension(name="budget", base_entity="budgets", field="budget", description="Budget name."),
        SemanticDimension(name="category", base_entity="budgets", field="category", projection_field="category_name", relationship_path=["budget_category"], null_label="All categories", description="Budget category."),
        SemanticDimension(name="period", base_entity="budgets", field="period", description="Budget cadence."),
        SemanticDimension(name="currency", base_entity="budgets", field="currency", description="Budget currency."),
        SemanticDimension(name="goal", base_entity="goals", field="goal", description="Savings goal name."),
        SemanticDimension(name="target_date", base_entity="goals", field="target_date", description="Goal target date."),
        SemanticDimension(name="currency", base_entity="goals", field="currency", description="Goal currency."),
        SemanticDimension(name="goal", base_entity="goal_contributions", field="goal", relationship_path=["contribution_goal"], description="Goal receiving the contribution."),
        SemanticDimension(name="currency", base_entity="goal_contributions", field="currency", description="Contribution currency."),
        SemanticDimension(name="loan", base_entity="loans", field="loan", description="Loan name."),
        SemanticDimension(name="loan_type", base_entity="loans", field="loan_type", description="Loan type."),
        SemanticDimension(name="lender", base_entity="loans", field="lender", null_label="Unknown lender", description="Loan lender."),
        SemanticDimension(name="direction", base_entity="loans", field="direction", null_label="Borrowed", description="Whether the user lent the money (receivable) or borrowed it (payable)."),
        SemanticDimension(name="counterparty", base_entity="loans", field="counterparty", null_label="Unknown counterparty", description="The other person in a shared personal-loan record."),
        SemanticDimension(name="currency", base_entity="loans", field="currency", description="Loan currency."),
        SemanticDimension(name="status", base_entity="loans", field="status", description="Loan status."),
        SemanticDimension(name="cadence", base_entity="recurring_transactions", field="cadence", description="Recurring cadence."),
        SemanticDimension(name="currency", base_entity="recurring_transactions", field="currency", description="Recurring amount currency."),
        SemanticDimension(name="merchant", base_entity="recurring_transactions", field="merchant", relationship_path=["recurring_merchant"], null_label="Unknown merchant", description="Recurring merchant."),
        SemanticDimension(name="subscription", base_entity="subscriptions", field="subscription", description="Subscription name."),
        SemanticDimension(name="status", base_entity="subscriptions", field="status", description="Subscription status."),
        SemanticDimension(name="source_type", base_entity="financial_observations", field="source_type", description="Ingestion source type."),
        SemanticDimension(name="processing_state", base_entity="financial_observations", field="processing_state", description="Observation processing state."),
        SemanticDimension(name="transaction_type", base_entity="financial_observations", field="transaction_type", description="Observed financial direction."),
        SemanticDimension(name="merchant", base_entity="financial_observations", field="merchant", null_label="Unknown merchant", description="Normalized observed merchant."),
        SemanticDimension(name="currency", base_entity="financial_observations", field="currency", description="Observed currency."),
    ]

    # Fail at startup/test time if the governed contract is ambiguous or drifts from SQLAlchemy.
    def require_unique(values: list[str], kind: str) -> None:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for value in values:
            (duplicates if value in seen else seen).add(value)
        if duplicates:
            raise RuntimeError(f"Duplicate semantic {kind}: {sorted(duplicates)}")

    require_unique([entity.name for entity in entities], "entities")
    require_unique([relationship.name for relationship in relationships], "relationships")
    require_unique([metric.name for metric in metrics], "metrics")
    for entity in entities:
        require_unique([field.name for field in entity.fields], f"fields on {entity.name}")
    for entity_name in {dimension.base_entity for dimension in dimensions}:
        require_unique(
            [dimension.name for dimension in dimensions if dimension.base_entity == entity_name],
            f"dimensions on {entity_name}",
        )
    for entity in entities:
        model = MODEL_BINDINGS[entity.name]
        physical_columns = {column.name for column in model.__table__.columns}
        declared_columns = {field.column for field in entity.fields}
        missing = declared_columns - physical_columns
        if missing:
            raise RuntimeError(f"Semantic registry drift for {entity.name}: missing columns {sorted(missing)}")
    entity_map = {entity.name: entity for entity in entities}
    for relationship in relationships:
        source_columns = {column.name for column in MODEL_BINDINGS[relationship.source_entity].__table__.columns}
        target_columns = {column.name for column in MODEL_BINDINGS[relationship.target_entity].__table__.columns}
        if relationship.source_field not in source_columns or relationship.target_field not in target_columns:
            raise RuntimeError(f"Semantic relationship drift for {relationship.name}")
    for metric in metrics:
        entity = entity_map[metric.base_entity]
        if metric.field and metric.field not in {field.name for field in entity.fields}:
            raise RuntimeError(f"Semantic metric {metric.name} references unknown field {metric.field}")
    relationship_map = {relationship.name: relationship for relationship in relationships}
    for dimension in dimensions:
        entity_name = dimension.base_entity
        for relationship_name in dimension.relationship_path:
            relationship = relationship_map[relationship_name]
            if not relationship.queryable:
                raise RuntimeError(f"Semantic dimension {dimension.name} uses context-only relationship {relationship_name}")
            if relationship.source_entity != entity_name:
                raise RuntimeError(f"Semantic dimension {dimension.name} has a disconnected relationship path")
            entity_name = relationship.target_entity
        target_fields = {field.name for field in entity_map[entity_name].fields}
        if dimension.field not in target_fields:
            raise RuntimeError(f"Semantic dimension {dimension.name} references unknown filter field {dimension.field}")
        if dimension.projection_field and dimension.projection_field not in target_fields:
            raise RuntimeError(
                f"Semantic dimension {dimension.name} references unknown projection field {dimension.projection_field}"
            )

    payload = {
        "version": "2026-08-12.2",
        "entities": [item.model_dump(mode="json") for item in entities],
        "relationships": [item.model_dump(mode="json") for item in relationships],
        "metrics": [item.model_dump(mode="json") for item in metrics],
        "dimensions": [item.model_dump(mode="json") for item in dimensions],
        "policy": SemanticPolicy().model_dump(mode="json"),
    }
    schema_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return SemanticSchemaRegistry(
        version=payload["version"],
        schema_hash=schema_hash,
        entities=entities,
        relationships=relationships,
        metrics=metrics,
        dimensions=dimensions,
        policy=SemanticPolicy(),
        financial_rules=[
            "Every user-owned entity is scoped to the authenticated user.",
            "Deleted transactions are excluded.",
            "Money is stored and aggregated as integer minor units.",
            "Every money metric is scoped to the authenticated user's persisted currency; currencies are never summed together.",
            "Transfers do not count as spending or income.",
            "Refunds and reimbursements reduce net spend but not gross spend.",
            "Absolute transaction amount across mixed directions must be grouped by transaction type and must never be labelled as spending, income, or net cash flow.",
            "Observation metrics describe ingestion and reconciliation quality; they are never canonical transaction totals.",
            "Net cash flow excludes transfers and uses explicit signed direction rules; investment contributions are cash outflow but not spending.",
            "Snapshot metrics ignore event windows unless a historical snapshot table exists.",
            "Fan-out joins require distinct-grain protection and may not inflate money metrics.",
        ],
    )
