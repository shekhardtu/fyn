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


class ChangeDriverResult(BaseModel):
    id: str
    label: str
    current_minor: int
    previous_minor: int
    change_minor: int


class ChangeDriversResult(MonthlyComparisonResult):
    drivers: list[ChangeDriverResult]


class CashPositionResult(BaseModel):
    income_minor: int
    expenses_minor: int
    net_minor: int
    currency: str = Field(min_length=3, max_length=3)


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
