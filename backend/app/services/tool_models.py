from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from ..validation import DataFieldKey
from ..visualization_contracts import VisualFieldRole, VisualFieldType, VisualValueType


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyInput(ToolInput):
    pass


class OptionalDateRangeInput(ToolInput):
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def paired_ordered_range(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("Provide both start and end, or neither")
        if self.start and self.end and self.end < self.start:
            raise ValueError("End date must be on or after start date")
        return self


class DateRangeInput(ToolInput):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered_range(self):
        if self.end < self.start:
            raise ValueError("End date must be on or after start date")
        return self


class SpendingSummaryInput(DateRangeInput):
    category_slug: str | None = Field(default=None, min_length=1, max_length=60)


class SubcategoryBreakdownInput(DateRangeInput):
    category_slug: str = Field(min_length=1, max_length=60)


class TransactionListInput(ToolInput):
    """Filters for one bounded, tenant-scoped read of canonical transactions.

    Every field is an exact match against governed data. Slugs and enums are
    never guessed: an unknown slug returns no rows rather than a near match,
    so a filter the user did not ask for cannot silently widen the answer.
    """

    transaction_type: Literal["expense", "income", "refund", "transfer", "adjustment"] | None = None
    merchant: str | None = Field(default=None, min_length=1, max_length=160)
    category_slug: str | None = Field(default=None, min_length=1, max_length=120)
    subcategory_slug: str | None = Field(default=None, min_length=1, max_length=120)
    account: str | None = Field(default=None, min_length=1, max_length=120)
    tag: str | None = Field(default=None, min_length=1, max_length=80)
    min_amount_minor: int | None = Field(default=None, ge=0)
    max_amount_minor: int | None = Field(default=None, ge=0)
    start: date | None = None
    end: date | None = None
    sort_by: Literal["transaction_at", "amount"] = "transaction_at"
    sort_direction: Literal["asc", "desc"] = "desc"
    limit: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="after")
    def ordered_bounds(self):
        if self.start and self.end and self.end < self.start:
            raise ValueError("End date must be on or after start date")
        if (
            self.min_amount_minor is not None
            and self.max_amount_minor is not None
            and self.max_amount_minor < self.min_amount_minor
        ):
            raise ValueError("max_amount_minor must be at least min_amount_minor")
        return self


class LoanPaymentInput(ToolInput):
    principal_minor: int = Field(gt=0)
    annual_rate_percent: float = Field(ge=0, le=100)
    tenure_months: int = Field(gt=0, le=600)


class LoanWithPrepaymentInput(LoanPaymentInput):
    prepayment_minor: int = Field(default=0, ge=0)


class FixedPaymentInput(ToolInput):
    principal_minor: int = Field(gt=0)
    annual_rate_percent: float = Field(ge=0, le=100)
    payment_minor: int = Field(gt=0)
    max_months: int = Field(default=1200, gt=0, le=1200)


class LoanStrategyInput(LoanWithPrepaymentInput):
    fee_percent: float = Field(default=0, ge=0, le=100)


class InvestmentProjectionInput(ToolInput):
    monthly_contribution_minor: int = Field(ge=0)
    current_value_minor: int = Field(default=0, ge=0)
    annual_return_percent: float = Field(ge=-100, le=100)
    years: int = Field(gt=0, le=100)


class AffordabilityInput(ToolInput):
    purchase_minor: int = Field(gt=0)
    liquid_savings_minor: int = Field(ge=0)
    monthly_income_minor: int = Field(ge=0)
    monthly_essential_spend_minor: int = Field(ge=0)
    emergency_months: int = Field(default=6, ge=1, le=24)


class TaxonomySubcategoryResult(BaseModel):
    slug: str
    name: str


class TaxonomyCategoryResult(BaseModel):
    slug: str
    name: str
    subcategories: list[TaxonomySubcategoryResult]


class TaxonomyResult(RootModel[list[TaxonomyCategoryResult]]):
    pass


class SpendingSummaryResult(BaseModel):
    total_minor: int
    count: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    start: date
    end: date
    category: str | None = None


class BreakdownRow(BaseModel):
    id: str
    label: str
    amount_minor: int
    count: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)


class BreakdownResult(RootModel[list[BreakdownRow]]):
    pass


class MonthlyComparisonResult(BaseModel):
    current: SpendingSummaryResult
    previous: SpendingSummaryResult
    difference_minor: int
    percent_change: float | None


class CashPositionResult(BaseModel):
    income_minor: int
    expenses_minor: int
    net_minor: int
    currency: str = Field(min_length=3, max_length=3)
    # Derived deterministically so a ratio answer is tool evidence, not model
    # arithmetic. Null when there are no expenses to divide by.
    income_to_expense_ratio: float | None = None
    start: str | None = None
    end: str | None = None


class RecurringExpenseResult(BaseModel):
    id: str
    merchant: str
    amount_minor: int
    currency: str = Field(min_length=3, max_length=3)
    cadence: Literal["weekly", "monthly"]
    occurrences: int = Field(ge=2)
    last_date: date


class RecurringExpensesResult(RootModel[list[RecurringExpenseResult]]):
    pass


class TransactionRow(BaseModel):
    """One canonical transaction, already authorized and formatted for reading."""

    id: str
    merchant: str
    transaction_type: str
    category: str | None = None
    subcategory: str | None = None
    account: str | None = None
    tags: list[str] = Field(default_factory=list)
    transaction_at: str
    transaction_date: date
    status: str
    amount_minor: int
    amount: str
    currency: str = Field(min_length=3, max_length=3)


class TransactionListResult(BaseModel):
    """A bounded transaction read plus the totals the model must not recompute."""

    rows: list[TransactionRow]
    returned: int = Field(ge=0)
    total_minor: int
    total: str
    currency: str = Field(min_length=3, max_length=3)
    # True when the filter matched more records than `limit` returned, so a
    # reply can say the list is capped instead of implying it is complete.
    truncated: bool = False


class LoanPaymentResult(BaseModel):
    emi_minor: int
    total_payment_minor: int
    total_interest_minor: int
    tenure_months: int = Field(ge=0)


class LoanPrepaymentResult(BaseModel):
    baseline: LoanPaymentResult
    after_prepayment: LoanPaymentResult
    interest_saved_minor: int
    emi_reduction_minor: int


class FixedPaymentResult(BaseModel):
    tenure_months: int = Field(ge=0)
    total_interest_minor: int
    payment_minor: int = Field(ge=0)


class InvestmentProjectionResult(BaseModel):
    projected_value_minor: int
    invested_minor: int
    estimated_returns_minor: int
    years: int
    assumed_annual_return_percent: float


class AffordabilityResult(BaseModel):
    affordable_now: bool
    purchase_minor: int
    emergency_reserve_minor: int
    available_after_reserve_minor: int
    monthly_surplus_minor: int
    gap_minor: int
    months_to_goal: int | None
    rule: str


class ComputedField(BaseModel):
    """One renderer-neutral field in a deterministic computed dataset."""

    name: DataFieldKey
    label: str = Field(min_length=1, max_length=80)
    type: VisualFieldType
    value_type: VisualValueType
    role: VisualFieldRole


class ComputedDatasetResult(BaseModel):
    kind: Literal["computed_dataset"]
    name: str
    title: str
    description: str
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    fields: list[ComputedField]
    default_dimension: str
    default_measures: list[str]
    rows: list[dict[str, Any]] = Field(max_length=600)
    summary: dict[str, Any]


class LoanStrategyResult(RootModel[dict[str, Any]]):
    pass
