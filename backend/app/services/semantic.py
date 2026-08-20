from __future__ import annotations

from datetime import date, datetime, time, timedelta
import re
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, Field, model_validator
from sqlalchemy import DateTime, Integer, and_, case, cast, func, or_, select
from sqlalchemy.orm import Session

from ..domain import TransactionType
from ..event_time import from_local_parts, resolve_event_time, utc_range_for_local_dates
from ..models import (
    Account,
    AccountBalanceSnapshot,
    Budget,
    Category,
    Goal,
    GoalContribution,
    InvestmentHolding,
    InvestmentValuationSnapshot,
    Merchant,
    RecurringTransaction,
    Subcategory,
    Tag,
    Transaction,
    TransactionTag,
)
from ..validation import SemanticIdentifier
from ..visualization_contracts import VisualizationView
from .semantic_registry import CalendarTimeGrain, FilterOperator, MODEL_BINDINGS, SemanticMetric, SortDirection, SUBDAY_TIME_GRAINS, TIME_GRAIN_SPECS, TimeComponent, TimeGrain, semantic_schema_registry
from .currency import user_currency, user_timezone
from .transactions import apply_canonical_transaction_scope


AnalysisContextSource = Literal["budgets", "goals", "loans", "accounts", "recurring_expenses"]
DEDICATED_ANALYSIS_TYPES = frozenset({
    "three_month_allocation", "avoidable_expenses", "loan_strategy",
    "recurring_expenses", "affordability", "monthly_comparison",
})
_SERVICE_INPUT_KEY = re.compile(r"[a-z][a-z0-9_]{0,40}")
AnalysisTransformOperation = Literal[
    "compare_totals", "period_change", "change_drivers", "share_of_total", "rank",
    "difference", "ratio", "prorate", "cumulative_sum", "moving_average",
]
BINARY_TRANSFORM_OPERATIONS = frozenset({"difference", "ratio"})
WINDOW_TRANSFORM_OPERATIONS = frozenset({"cumulative_sum", "moving_average"})
DIMENSION_TRANSFORM_OPERATIONS = frozenset({
    "compare_totals", "period_change", "change_drivers", "share_of_total", "rank",
    "cumulative_sum", "moving_average",
})
SCALAR_TRANSFORM_OPERATIONS = frozenset({"difference", "prorate"})
TIME_PIVOT_DIMENSIONS = ("time_bucket", "time_segment")

class SemanticValidationError(ValueError):
    pass


def _iana_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown IANA timezone: {value}") from exc
    return value


IanaTimezone = Annotated[str, AfterValidator(_iana_timezone)]


class FinanceFilter(BaseModel):
    field: SemanticIdentifier
    operator: FilterOperator = "eq"
    value: str | int | float | date | list[str | int | float | date]


class TimeGrouping(BaseModel):
    """A compositional temporal operator, independent of physical SQL fields."""

    field: Literal["event_time"] = "event_time"
    grain: TimeGrain
    timezone: IanaTimezone = "UTC"
    fill_gaps: bool = False


class TimePivot(BaseModel):
    """Two-dimensional temporal projection for heatmaps and pivot views."""

    row_grain: CalendarTimeGrain = "day"
    column_component: TimeComponent = "hour_of_day"
    timezone: IanaTimezone = "UTC"


class FinanceQueryPlan(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    entity: SemanticIdentifier | None = None
    metric: SemanticIdentifier
    dimensions: list[SemanticIdentifier] = Field(default_factory=list, max_length=8)
    relationships: list[SemanticIdentifier] = Field(default_factory=list, max_length=6)
    filters: list[FinanceFilter] = Field(default_factory=list, max_length=20)
    start_date: date
    end_date: date
    start_datetime: datetime | None = None
    end_datetime: datetime | None = None
    time_grouping: TimeGrouping | None = None
    time_pivot: TimePivot | None = None
    order: SortDirection = "desc"
    limit: int = Field(default=25, ge=1, le=250)

    @model_validator(mode="after")
    def validate_window(self):
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if self.end_date - self.start_date > timedelta(days=366 * 5):
            raise ValueError("semantic queries are limited to five years")
        if (self.start_datetime is None) != (self.end_datetime is None):
            raise ValueError("start_datetime and end_datetime must be supplied together")
        if self.start_datetime and self.end_datetime and self.start_datetime > self.end_datetime:
            raise ValueError("start_datetime must not be after end_datetime")
        if self.time_grouping and self.time_pivot:
            raise ValueError("time_grouping and time_pivot are mutually exclusive")
        return self


class AnalysisTransform(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    operation: AnalysisTransformOperation
    query_name: str = Field(min_length=1, max_length=100)
    secondary_query_name: str | None = Field(default=None, min_length=1, max_length=100)
    secondary_transform_name: str | None = Field(default=None, min_length=1, max_length=100)
    dimension: SemanticIdentifier | None = None
    period_dimension: SemanticIdentifier | None = None
    target_start_date: date | None = None
    target_end_date: date | None = None
    limit: int = Field(default=5, ge=2, le=20)
    window: int = Field(default=3, ge=2, le=24)


class AnalysisPlan(BaseModel):
    objective: Literal["descriptive", "diagnostic", "recommendation", "scenario"]
    analysis_type: Literal[
        "semantic_query", "three_month_allocation", "avoidable_expenses", "loan_strategy",
        "recurring_expenses", "affordability", "monthly_comparison",
    ]
    queries: list[FinanceQueryPlan] = Field(default_factory=list, max_length=12)
    transforms: list[AnalysisTransform] = Field(default_factory=list, max_length=12)
    # Renderer-neutral chart declarations. Each view's dataset names one of
    # this plan's queries; chart-specific coherence is enforced by the
    # deterministic chart builder, which degrades loudly without failing the
    # analysis itself.
    visualizations: list[VisualizationView] = Field(default_factory=list, max_length=4)
    context_sources: list[AnalysisContextSource] = Field(default_factory=list, max_length=5)
    service_inputs: dict[str, int] = Field(default_factory=dict)
    safe_reasoning_summary: list[str] = Field(default_factory=list, min_length=1, max_length=6)
    missing_information: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_transforms(self):
        if self.service_inputs:
            if self.analysis_type not in DEDICATED_ANALYSIS_TYPES:
                raise ValueError("service_inputs are only valid for dedicated analysis types")
            if len(self.service_inputs) > 4:
                raise ValueError("service_inputs accepts at most four entries")
            for key in self.service_inputs:
                if not _SERVICE_INPUT_KEY.fullmatch(key):
                    raise ValueError(f"service_inputs key is not a valid identifier: {key}")
        from .chart_widgets import dataset_id

        # Names that collapse to one dataset id would bind a chart view to
        # whichever query happened to come first — reject the ambiguity.
        normalized_names = [dataset_id(query.name) for query in self.queries]
        if len(set(normalized_names)) != len(normalized_names):
            raise ValueError("query names must remain distinct after dataset normalization")
        queries = {query.name: query for query in self.queries}
        transforms = {transform.name: transform for transform in self.transforms}
        completed_transforms: set[str] = set()
        for transform in self.transforms:
            query = queries.get(transform.query_name)
            if not query:
                raise ValueError(f"transform references unknown query: {transform.query_name}")
            if transform.operation in DIMENSION_TRANSFORM_OPERATIONS and transform.dimension is None:
                raise ValueError(f"{transform.operation} requires a dimension")
            if transform.dimension is not None and transform.dimension not in query.dimensions:
                if transform.dimension != "time_bucket" or not query.time_grouping:
                    raise ValueError(f"transform dimension {transform.dimension} is not produced by {query.name}")
            if transform.operation in BINARY_TRANSFORM_OPERATIONS:
                secondary_sources = int(transform.secondary_query_name is not None) + int(
                    transform.secondary_transform_name is not None
                )
                if secondary_sources != 1:
                    raise ValueError(
                        f"{transform.operation} requires exactly one secondary query or transform"
                    )
                if transform.secondary_query_name and transform.secondary_query_name not in queries:
                    raise ValueError(f"{transform.operation} references an unknown secondary query")
                if transform.secondary_transform_name:
                    if transform.secondary_transform_name not in completed_transforms:
                        raise ValueError(
                            f"{transform.operation} must reference an earlier secondary transform"
                        )
                    secondary = transforms[transform.secondary_transform_name]
                    if secondary.operation not in SCALAR_TRANSFORM_OPERATIONS:
                        raise ValueError(
                            f"{transform.operation} requires a scalar secondary transform"
                        )
            elif transform.secondary_query_name or transform.secondary_transform_name:
                raise ValueError(
                    f"{transform.operation} does not accept a secondary query or transform"
                )
            if transform.operation == "prorate":
                if transform.target_start_date is None or transform.target_end_date is None:
                    raise ValueError("prorate requires a target_start_date and target_end_date")
                if transform.target_end_date < transform.target_start_date:
                    raise ValueError("prorate target_end_date must not be before target_start_date")
            elif transform.target_start_date is not None or transform.target_end_date is not None:
                raise ValueError(
                    f"{transform.operation} does not accept a target date range"
                )
            if transform.operation == "change_drivers":
                if not transform.period_dimension or transform.period_dimension not in query.dimensions:
                    raise ValueError("change_drivers requires a period_dimension produced by its query")
                if transform.period_dimension == transform.dimension:
                    raise ValueError("change_drivers needs distinct period and driver dimensions")
            completed_transforms.add(transform.name)
        return self


class AnalysisToolProposal(BaseModel):
    """Declarative tool proposed by the agent and compiled by the harness."""

    name: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=10, max_length=500)
    intent_signature: str = Field(min_length=3, max_length=160)
    plan: AnalysisPlan


def semantic_catalog() -> dict[str, Any]:
    """Full versioned contract shared by the planner, validator and compiler."""
    return semantic_schema_registry().prompt_contract()


def _resolved_dimensions(entity: str) -> dict[str, Any]:
    return semantic_schema_registry().dimensions_for(entity)


def validate_finance_query_plan(plan: FinanceQueryPlan) -> dict[str, Any]:
    """Validate semantic names, relationship paths, types and cost before SQL compilation."""
    registry = semantic_schema_registry()
    metrics = registry.metrics_by_name
    metric = metrics.get(plan.metric)
    if not metric:
        raise SemanticValidationError(f"Unknown governed metric: {plan.metric}")
    entity = plan.entity or metric.base_entity
    if entity != metric.base_entity:
        raise SemanticValidationError(f"Metric {plan.metric} must use base entity {metric.base_entity}")
    entities = registry.entities_by_name
    base = entities.get(entity)
    if not base or (registry.policy.require_tenant_scope and not base.tenant_key):
        raise SemanticValidationError(f"Entity {entity} cannot be queried with authenticated tenant scope")
    if plan.time_grouping:
        if not base.event_date_key or plan.metric not in metrics:
            raise SemanticValidationError(f"Entity {entity} has no governed event time")
        if plan.time_grouping.grain in SUBDAY_TIME_GRAINS and not base.event_time_key:
            raise SemanticValidationError(f"Entity {entity} has no governed sub-day event time")
        if plan.time_grouping.grain not in base.supported_time_grains:
            raise SemanticValidationError(
                f"Time grain {plan.time_grouping.grain} is not supported for {entity}"
            )
        if plan.time_grouping.fill_gaps:
            if metric.aggregation not in {"sum", "count", "conditional_sum", "net_sum", "cash_flow_sum"}:
                raise SemanticValidationError("Gap filling is limited to additive metrics")
    if plan.time_pivot:
        if not base.event_date_key:
            raise SemanticValidationError(f"Entity {entity} has no governed event time")
        if plan.time_pivot.column_component == "hour_of_day" and not base.event_time_key:
            raise SemanticValidationError(f"Entity {entity} has no governed sub-day event time")

    available_dimensions = _resolved_dimensions(entity)
    required_relationships: list[str] = []
    for name in plan.dimensions:
        dimension = available_dimensions.get(name)
        if not dimension:
            if name == "time_bucket":
                raise SemanticValidationError(
                    "time_bucket is the output of the time_grouping operator, not a dimension; "
                    "express calendar bucketing with time_grouping or the month dimension"
                )
            raise SemanticValidationError(f"Dimension {name} is not valid for {entity}")
        required_relationships.extend(dimension.relationship_path)

    relationships = registry.relationships_by_name
    approved = list(dict.fromkeys([*plan.relationships, *required_relationships]))
    for name in approved:
        relationship = relationships.get(name)
        if not relationship or not relationship.queryable or relationship.source_entity != entity:
            raise SemanticValidationError(f"Relationship {name} is not an approved path from {entity}")
    if len(approved) > registry.policy.max_relationships_per_query:
        raise SemanticValidationError("Query exceeds the governed relationship count limit")

    base_fields = {field.name: field for field in base.fields}
    for item in plan.filters:
        field = base_fields.get(item.field)
        if not field:
            dimension = available_dimensions.get(item.field)
            if not dimension:
                raise SemanticValidationError(f"Filter field {item.field} is not valid for {entity}")
            required_relationships.extend(dimension.relationship_path)
            target_entity = entity
            for relationship_name in dimension.relationship_path:
                relationship = relationships[relationship_name]
                target_entity = relationship.target_entity
            field = next((candidate for candidate in entities[target_entity].fields if candidate.name == dimension.field), None)
        if not field or item.operator not in field.filter_operators:
            raise SemanticValidationError(f"Operator {item.operator} is not valid for {item.field}")
        values = item.value if isinstance(item.value, list) else [item.value]
        if item.operator == "between" and len(values) != 2:
            raise SemanticValidationError(f"Filter {item.field} requires exactly two values")
        if field.semantic_type == "money_minor" and any(isinstance(value, float) and not value.is_integer() for value in values):
            raise SemanticValidationError(f"Money filter {item.field} must use integer minor units")
        if field.semantic_type in {"date", "time"}:
            for value in values:
                if isinstance(value, date):
                    continue
                try:
                    (time if field.semantic_type == "time" else date).fromisoformat(str(value))
                except ValueError as exc:
                    kind = "time" if field.semantic_type == "time" else "date"
                    raise SemanticValidationError(f"{kind.title()} filter {item.field} must use ISO {kind}s") from exc

    required_relationships = list(dict.fromkeys(required_relationships))
    unneeded_relationships = set(plan.relationships) - set(required_relationships)
    if unneeded_relationships:
        raise SemanticValidationError(f"Relationships are not required by this query: {sorted(unneeded_relationships)}")
    approved = list(dict.fromkeys([*approved, *required_relationships]))
    if len(approved) > registry.policy.max_relationships_per_query:
        raise SemanticValidationError("Query exceeds the governed relationship count limit")
    if plan.limit * max(1, len(plan.dimensions)) > registry.policy.max_estimated_cells:
        raise SemanticValidationError("Query exceeds the governed result-size limit")
    if metric.time_semantics == "event_window" and (plan.end_date - plan.start_date).days > registry.policy.max_window_days:
        raise SemanticValidationError("Query exceeds the governed event window")
    if plan.time_grouping and plan.time_grouping.fill_gaps:
        # Gap-filling guarantees at least one row per bucket, so a bucket
        # count above the limit is certain to fail at execution; reject it at
        # validation, where a stored dashboard tile can still be refused.
        buckets = len(_time_bucket_values(plan))
        if buckets > plan.limit:
            raise SemanticValidationError(
                f"Gap-filled {plan.time_grouping.grain} grouping over this window needs "
                f"{buckets} rows, above the requested limit of {plan.limit}"
            )
    return {
        "registry_version": registry.version,
        "schema_hash": registry.schema_hash,
        "entity": entity,
        "metric": metric.name,
        "relationships": approved,
        "time_semantics": metric.time_semantics,
        "estimated_cells": plan.limit * max(1, len(plan.dimensions)),
    }


def _month_expression(column, dialect: str):
    return func.strftime("%Y-%m", column) if dialect == "sqlite" else func.to_char(column, "YYYY-MM")


def _transaction_datetime_expression(dialect: str, target_timezone: str | None = None):
    if dialect == "sqlite":
        return func.to_local_datetime(Transaction.transaction_at, target_timezone or "UTC")
    return func.timezone(target_timezone or "UTC", Transaction.transaction_at)


def _local_datetime_expression(column, dialect: str, timezone_name: str):
    if dialect == "sqlite":
        return func.to_local_datetime(column, timezone_name)
    return func.timezone(timezone_name, column)


def _event_date_column(entity: str):
    registry_entity = semantic_schema_registry().entities_by_name[entity]
    return getattr(MODEL_BINDINGS[entity], registry_entity.event_date_key)


def _event_time_column(entity: str, dialect: str, timezone_name: str):
    if entity != "transactions":
        raise SemanticValidationError(f"Entity {entity} has no governed sub-day event time")
    return _transaction_datetime_expression(dialect, timezone_name)


def _time_bucket_expression(entity: str, grain: str, dialect: str, timezone_name: str):
    if grain in SUBDAY_TIME_GRAINS:
        column = _event_time_column(entity, dialect, timezone_name)
    else:
        column = _event_date_column(entity)
        if isinstance(column.property.columns[0].type, DateTime):
            column = _local_datetime_expression(column, dialect, timezone_name)
    if dialect == "sqlite":
        if grain == "quarter":
            quarter = cast((cast(func.strftime("%m", column), Integer) + 2) / 3, Integer)
            return func.printf("%s-Q%d", func.strftime("%Y", column), quarter)
        return func.strftime(TIME_GRAIN_SPECS[grain].sqlite_format, column)
    return func.to_char(column, TIME_GRAIN_SPECS[grain].postgres_format)


def _time_component_expression(entity: str, component: str, dialect: str, timezone_name: str):
    if component == "hour_of_day":
        column = _event_time_column(entity, dialect, timezone_name)
        return func.strftime("%H", column) if dialect == "sqlite" else func.to_char(column, "HH24")
    column = _event_date_column(entity)
    if isinstance(column.property.columns[0].type, DateTime):
        column = _local_datetime_expression(column, dialect, timezone_name)
    if dialect == "sqlite":
        formats = {"day_of_week": "%w", "day_of_month": "%d", "month_of_year": "%m"}
        return func.strftime(formats[component], column)
    formats = {"day_of_week": "ID", "day_of_month": "DD", "month_of_year": "MM"}
    return func.to_char(column, formats[component])


def _time_bucket_values(plan: FinanceQueryPlan) -> list[str]:
    """Return the finite, ordered bucket domain for a governed time window."""
    grouping = plan.time_grouping
    if not grouping:
        return []
    grain = grouping.grain
    if grain in SUBDAY_TIME_GRAINS:
        step = timedelta(minutes=1) if grain == "minute" else timedelta(hours=1)
        start = plan.start_datetime or datetime.combine(plan.start_date, time.min)
        end = plan.end_datetime or datetime.combine(plan.end_date, time.max)
        current = start.replace(second=0, microsecond=0)
        if grain == "hour":
            current = current.replace(minute=0)
        values = []
        while current <= end:
            values.append(current.strftime("%Y-%m-%d %H:%M") if grain == "minute" else current.strftime("%Y-%m-%d %H:00"))
            current += step
        return values

    def label(value: date) -> str:
        if grain == "day":
            return value.isoformat()
        if grain == "week":
            year, week, _ = value.isocalendar()
            return f"{year}-W{week:02d}"
        if grain == "month":
            return value.strftime("%Y-%m")
        if grain == "quarter":
            return f"{value.year}-Q{((value.month - 1) // 3) + 1}"
        return str(value.year)

    values: list[str] = []
    seen: set[str] = set()
    current = plan.start_date
    while current <= plan.end_date:
        bucket = label(current)
        if bucket not in seen:
            values.append(bucket)
            seen.add(bucket)
        current += timedelta(days=1)
    return values


def _fill_time_gaps(plan: FinanceQueryPlan, labels: list[str], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Zero-fill absent additive buckets without inventing dimension members."""
    if not plan.time_grouping or not plan.time_grouping.fill_gaps:
        return rows
    buckets = _time_bucket_values(plan)
    series_dimensions = [name for name in labels if name != "time_bucket"]
    series = {
        tuple(row.get(name) for name in series_dimensions)
        for row in rows
    }
    if not series_dimensions:
        series = {()}
    if not series:
        # A category or merchant with no observations is unknown, not zero.
        return rows
    existing = {
        (tuple(row.get(name) for name in series_dimensions), str(row.get("time_bucket"))): row
        for row in rows
    }
    filled = []
    ordered_series = sorted(series, key=lambda item: tuple(str(value) for value in item))
    ordered_buckets = buckets if plan.order == "asc" else list(reversed(buckets))
    for values in ordered_series:
        dimensions = dict(zip(series_dimensions, values))
        for bucket in ordered_buckets:
            filled.append(existing.get((values, bucket), {**dimensions, "time_bucket": bucket, "value": 0}))
    if len(filled) > plan.limit:
        raise SemanticValidationError(
            f"Gap-filled result needs {len(filled)} rows, above the governed limit of {plan.limit}"
        )
    return filled


def _resolved_dimension(entity: str, name: str):
    registry = semantic_schema_registry()
    dimension = next(
        item for item in registry.dimensions
        if item.base_entity == entity and item.name == name
    )
    target_entity = entity
    relationships = registry.relationships_by_name
    for relationship_name in dimension.relationship_path:
        target_entity = relationships[relationship_name].target_entity
    target = registry.entities_by_name[target_entity]
    return dimension, target_entity, target


def _dimension_binding(entity: str, name: str, dialect: str, timezone_name: str):
    dimension, target_entity, target = _resolved_dimension(entity, name)
    projection_name = dimension.projection_field or dimension.field
    field = next(item for item in target.fields if item.name == projection_name)
    expression = getattr(MODEL_BINDINGS[target_entity], field.column)
    if isinstance(expression.property.columns[0].type, DateTime) and field.semantic_type == "date":
        expression = _local_datetime_expression(expression, dialect, timezone_name)
        if dimension.transform != "month":
            expression = func.strftime("%Y-%m-%d", expression) if dialect == "sqlite" else func.to_char(expression, "YYYY-MM-DD")
    if dimension.transform == "month":
        expression = _month_expression(expression, dialect)
    if dimension.null_label is not None:
        expression = func.coalesce(expression, dimension.null_label)
    return expression, list(dimension.relationship_path)


JOIN_ADAPTERS = {
    "transaction_category": lambda statement, _user_id: statement.outerjoin(Category, Category.id == Transaction.category_id),
    "transaction_subcategory": lambda statement, _user_id: statement.outerjoin(Subcategory, Subcategory.id == Transaction.subcategory_id),
    "transaction_account": lambda statement, user_id: statement.outerjoin(Account, and_(Account.id == Transaction.account_id, Account.user_id == user_id)),
    "transaction_tags": lambda statement, user_id: statement.outerjoin(TransactionTag, TransactionTag.transaction_id == Transaction.id).outerjoin(Tag, and_(Tag.id == TransactionTag.tag_id, Tag.user_id == user_id)),
    "balance_snapshot_account": lambda statement, user_id: statement.outerjoin(Account, and_(Account.id == AccountBalanceSnapshot.account_id, Account.user_id == user_id)),
    "holding_account": lambda statement, user_id: statement.outerjoin(Account, and_(Account.id == InvestmentHolding.account_id, Account.user_id == user_id)),
    "valuation_holding": lambda statement, user_id: statement.outerjoin(InvestmentHolding, and_(InvestmentHolding.id == InvestmentValuationSnapshot.holding_id, InvestmentHolding.user_id == user_id)),
    "budget_category": lambda statement, _user_id: statement.outerjoin(Category, Category.id == Budget.category_id),
    "contribution_goal": lambda statement, user_id: statement.outerjoin(Goal, and_(Goal.id == GoalContribution.goal_id, Goal.user_id == user_id)),
    "recurring_merchant": lambda statement, _user_id: statement.outerjoin(Merchant, Merchant.id == RecurringTransaction.merchant_id),
}

QUERYABLE_JOIN_BINDINGS = frozenset(JOIN_ADAPTERS)


def _apply_joins(stmt, relationship_names: set[str], user_id: UUID):
    for relationship_name in sorted(relationship_names):
        stmt = JOIN_ADAPTERS[relationship_name](stmt, user_id)
    return stmt


def _metric_expression(metric: SemanticMetric, model):
    if metric.name == "gross_spend":
        return func.coalesce(func.sum(case((Transaction.transaction_type == TransactionType.EXPENSE, Transaction.amount_minor), else_=0)), 0)
    if metric.name == "net_spend":
        return func.coalesce(func.sum(case(
            (Transaction.transaction_type == TransactionType.EXPENSE, Transaction.amount_minor),
            (Transaction.transaction_type.in_((TransactionType.REFUND, TransactionType.REIMBURSEMENT)), -Transaction.amount_minor),
            else_=0,
        )), 0)
    if metric.name == "income":
        return func.coalesce(func.sum(case((Transaction.transaction_type == TransactionType.INCOME, Transaction.amount_minor), else_=0)), 0)
    if metric.name == "debt_service":
        return func.coalesce(func.sum(case((Transaction.transaction_type == TransactionType.LOAN_PAYMENT, Transaction.amount_minor), else_=0)), 0)
    if metric.name == "net_cash_flow":
        return func.coalesce(func.sum(case(
            (Transaction.transaction_type.in_((TransactionType.INCOME, TransactionType.REFUND, TransactionType.REIMBURSEMENT, TransactionType.CASH_DEPOSIT)), Transaction.amount_minor),
            (Transaction.transaction_type.in_((TransactionType.EXPENSE, TransactionType.LOAN_PAYMENT, TransactionType.INVESTMENT, TransactionType.CASH_WITHDRAWAL)), -Transaction.amount_minor),
            else_=0,
        )), 0)
    field = None
    if metric.field:
        entity = semantic_schema_registry().entities_by_name[metric.base_entity]
        field_spec = next(item for item in entity.fields if item.name == metric.field)
        field = getattr(model, field_spec.column)
    if metric.aggregation == "average":
        return func.coalesce(func.avg(field), 0)
    if metric.aggregation == "minimum":
        return func.coalesce(func.min(field), 0)
    if metric.aggregation == "maximum":
        return func.coalesce(func.max(field), 0)
    if metric.aggregation == "count":
        return func.count(func.distinct(model.id))
    return func.coalesce(func.sum(field), 0)


def _filter_binding(entity: str, field: str):
    registry = semantic_schema_registry()
    registry_entity = registry.entities_by_name[entity]
    field_spec = next((item for item in registry_entity.fields if item.name == field), None)
    if field_spec:
        return getattr(MODEL_BINDINGS[entity], field_spec.column), []
    dimension, target_entity, target = _resolved_dimension(entity, field)
    field_spec = next(item for item in target.fields if item.name == dimension.field)
    return getattr(MODEL_BINDINGS[target_entity], field_spec.column), list(dimension.relationship_path)


def _apply_filter(stmt, column, item: FinanceFilter, timezone_name: str):
    values = item.value if isinstance(item.value, list) else [item.value]
    if isinstance(column.property.columns[0].type, DateTime) and all(isinstance(value, date) and not isinstance(value, datetime) for value in values):
        if item.operator == "eq":
            start_at, end_at = utc_range_for_local_dates(values[0], values[0], timezone_name)
            return stmt.where(column >= start_at, column < end_at)
        if item.operator == "between":
            start_at, end_at = utc_range_for_local_dates(values[0], values[1], timezone_name)
            return stmt.where(column >= start_at, column < end_at)
        if item.operator == "gte":
            return stmt.where(column >= from_local_parts(values[0], None, timezone_name))
        if item.operator == "gt":
            _, end_at = utc_range_for_local_dates(values[0], values[0], timezone_name)
            return stmt.where(column >= end_at)
        if item.operator == "lt":
            return stmt.where(column < from_local_parts(values[0], None, timezone_name))
        if item.operator == "lte":
            _, end_at = utc_range_for_local_dates(values[0], values[0], timezone_name)
            return stmt.where(column < end_at)
    if item.operator == "eq":
        return stmt.where(column == values[0])
    if item.operator == "neq":
        return stmt.where(or_(column.is_(None), column != values[0]))
    if item.operator == "in":
        return stmt.where(column.in_(values))
    if item.operator == "not_in":
        return stmt.where(or_(column.is_(None), column.not_in(values)))
    if item.operator == "contains":
        return stmt.where(func.lower(column).contains(str(values[0]).lower()))
    if item.operator == "gt":
        return stmt.where(column > values[0])
    if item.operator == "gte":
        return stmt.where(column >= values[0])
    if item.operator == "lt":
        return stmt.where(column < values[0])
    if item.operator == "lte":
        return stmt.where(column <= values[0])
    return stmt.where(column.between(values[0], values[1]))


def execute_finance_query(db: Session, user_id: UUID, plan: FinanceQueryPlan) -> dict[str, Any]:
    """Compile a validated semantic plan to parameterized SQLAlchemy SELECTs only."""
    validation = validate_finance_query_plan(plan)
    registry = semantic_schema_registry()
    metric = registry.metrics_by_name[plan.metric]
    entity = validation["entity"]
    model = MODEL_BINDINGS[entity]
    currency = user_currency(db, user_id)
    timezone_name = (
        plan.time_grouping.timezone if plan.time_grouping
        else plan.time_pivot.timezone if plan.time_pivot
        else user_timezone(db, user_id)
    )
    registry_entity = registry.entities_by_name[entity]
    dialect = db.get_bind().dialect.name
    metric_expression = _metric_expression(metric, model).label("value")
    columns: list[Any] = []
    labels: list[str] = []
    joins = set(validation["relationships"])
    for dimension in plan.dimensions:
        expression, required = _dimension_binding(entity, dimension, dialect, timezone_name)
        joins.update(required)
        columns.append(expression.label(dimension))
        labels.append(dimension)
    if plan.time_grouping:
        columns.append(_time_bucket_expression(entity, plan.time_grouping.grain, dialect, plan.time_grouping.timezone).label("time_bucket"))
        labels.append("time_bucket")
    if plan.time_pivot:
        columns.extend([
            _time_bucket_expression(entity, plan.time_pivot.row_grain, dialect, plan.time_pivot.timezone).label("time_bucket"),
            _time_component_expression(entity, plan.time_pivot.column_component, dialect, plan.time_pivot.timezone).label("time_segment"),
        ])
        labels.extend(TIME_PIVOT_DIMENSIONS)
    for item in plan.filters:
        _, required = _filter_binding(entity, item.field)
        joins.update(required)

    stmt = select(*columns, metric_expression).select_from(model)
    stmt = _apply_joins(stmt, joins, user_id)
    tenant_key = registry_entity.tenant_key
    money_currency_column = None
    if metric.result_type == "money_minor":
        currency_field = next(
            (field for field in registry_entity.fields if field.semantic_type == "currency"),
            None,
        )
        if currency_field is None:
            raise SemanticValidationError(
                f"Money metric {metric.name} has no governed currency field"
            )
        money_currency_column = getattr(model, currency_field.column)
    if entity == "transactions":
        stmt = apply_canonical_transaction_scope(
            stmt,
            user_id,
            currency=currency if money_currency_column is not None else None,
        )
    else:
        stmt = stmt.where(getattr(model, tenant_key) == user_id)
        if money_currency_column is not None:
            stmt = stmt.where(money_currency_column == currency)
    if metric.time_semantics == "event_window":
        event_key = registry_entity.event_date_key
        event_column = getattr(model, event_key)
        event_field = next(item for item in registry_entity.fields if item.column == event_key)
        if event_field.data_type == "datetime":
            start_instant, end_instant = utc_range_for_local_dates(plan.start_date, plan.end_date, timezone_name)
            stmt = stmt.where(event_column >= start_instant, event_column < end_instant)
        else:
            stmt = stmt.where(event_column.between(plan.start_date, plan.end_date))
        if plan.start_datetime and plan.end_datetime:
            if entity != "transactions":
                raise SemanticValidationError("Sub-day event windows are only available for transactions")
            start_instant = resolve_event_time(transaction_at=plan.start_datetime, timezone_name=timezone_name)
            end_instant = resolve_event_time(transaction_at=plan.end_datetime, timezone_name=timezone_name)
            stmt = stmt.where(
                Transaction.transaction_at.between(start_instant, end_instant),
            )
    for field, value in metric.fixed_filters.items():
        column, _ = _filter_binding(entity, field)
        values = value if isinstance(value, list) else [value]
        stmt = stmt.where(column.in_(values))
    for item in plan.filters:
        column, _ = _filter_binding(entity, item.field)
        stmt = _apply_filter(stmt, column, item, timezone_name)

    if columns:
        stmt = stmt.group_by(*columns)
    if plan.time_grouping or plan.time_pivot:
        temporal_columns = columns[-2:] if plan.time_pivot else columns[-1:]
        stmt = stmt.order_by(*[
            column.asc() if plan.order == "asc" else column.desc()
            for column in temporal_columns
        ])
    else:
        stmt = stmt.order_by(metric_expression.desc() if plan.order == "desc" else metric_expression.asc())
    stmt = stmt.limit(plan.limit)
    rows = db.execute(stmt).all()
    rendered = []
    for row in rows:
        values = list(row)
        metric_value = values.pop()
        rendered.append({**dict(zip(labels, values)), "value": int(metric_value or 0)})
    rendered = _fill_time_gaps(plan, labels, rendered)
    return {
        "name": plan.name,
        "entity": entity,
        "metric": plan.metric,
        "metric_definition": metric.description,
        "dimensions": labels,
        "relationships": sorted(joins),
        "registry_version": validation["registry_version"],
        "schema_hash": validation["schema_hash"],
        "time_semantics": metric.time_semantics,
        "currency": currency if metric.result_type == "money_minor" else None,
        "start": plan.start_date.isoformat(),
        "end": plan.end_date.isoformat(),
        "start_datetime": plan.start_datetime.isoformat() if plan.start_datetime else None,
        "end_datetime": plan.end_datetime.isoformat() if plan.end_datetime else None,
        "time_grouping": plan.time_grouping.model_dump(mode="json") if plan.time_grouping else None,
        "time_pivot": plan.time_pivot.model_dump(mode="json") if plan.time_pivot else None,
        "requires_transaction_time": False,
        "rows": rendered,
    }


def validate_runtime_registry_coverage() -> None:
    """Fail at import/startup when the declarative registry outruns compiler adapters."""
    registry = semantic_schema_registry()
    queryable_relationships = {relationship.name for relationship in registry.relationships if relationship.queryable}
    if queryable_relationships != QUERYABLE_JOIN_BINDINGS:
        missing = sorted(queryable_relationships - QUERYABLE_JOIN_BINDINGS)
        obsolete = sorted(QUERYABLE_JOIN_BINDINGS - queryable_relationships)
        raise RuntimeError(f"Semantic join adapter drift; missing={missing}, obsolete={obsolete}")


validate_runtime_registry_coverage()
