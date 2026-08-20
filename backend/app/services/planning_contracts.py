from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


def _camel_case(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.title() for part in tail)


class BudgetSetupSeed(BaseModel):
    """Stable category identity retained while the budget amount is collected."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        alias_generator=_camel_case,
    )

    schema_version: Literal[1] = 1
    category_id: UUID | None = None
    category_name: str | None = Field(default=None, max_length=120)
    budget_id: UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    currency: str = Field(min_length=3, max_length=3)


class BudgetSetupContract(BudgetSetupSeed):
    amount_minor: int = Field(gt=0)
