from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# These existing planning and balance columns use PostgreSQL INTEGER.
# Validate their actual storage range before attempting a write.
MAX_FINANCE_AMOUNT_MINOR = 2_147_483_647


def camel_case(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.title() for part in tail)


class FinanceForm(BaseModel):
    model_config = ConfigDict(populate_by_name=True, from_attributes=True, extra="forbid", alias_generator=camel_case, str_strip_whitespace=True)

    @field_validator("amount_minor", "target_minor", "balance_minor", mode="before", check_fields=False)
    @classmethod
    def validate_amount_range(cls, value):
        if isinstance(value, int) and abs(value) > MAX_FINANCE_AMOUNT_MINOR:
            raise ValueError(f"Enter an amount no greater than {MAX_FINANCE_AMOUNT_MINOR / 100:,.2f}.")
        return value


class BudgetSaveIn(FinanceForm):
    name: str = Field(min_length=1, max_length=120)
    amount_minor: int = Field(gt=0, le=MAX_FINANCE_AMOUNT_MINOR)
    category_id: UUID | None = None


class BudgetRecordOut(BudgetSaveIn):
    id: UUID
    currency: str
    period: str


class GoalSaveIn(FinanceForm):
    name: str = Field(min_length=1, max_length=120)
    target_minor: int = Field(gt=0, le=MAX_FINANCE_AMOUNT_MINOR)
    target_date: date | None = None


class GoalRecordOut(GoalSaveIn):
    id: UUID
    currency: str
    current_minor: int


class GoalContributionIn(FinanceForm):
    amount_minor: int = Field(gt=0, le=MAX_FINANCE_AMOUNT_MINOR)
    request_id: UUID


class AccountCreateIn(FinanceForm):
    name: str = Field(min_length=1, max_length=120)
    account_type: Literal["bank", "savings", "checking", "credit_card", "cash", "wallet", "investment", "other"]
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    balance_minor: int = Field(ge=-MAX_FINANCE_AMOUNT_MINOR, le=MAX_FINANCE_AMOUNT_MINOR)
    institution: str | None = Field(default=None, max_length=120)
    mask: str | None = Field(default=None, max_length=12)

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


class AccountRecordOut(FinanceForm):
    id: UUID
    name: str
    account_type: str
    currency: str
    balance_minor: int
    institution: str | None = None
    mask: str | None = None
