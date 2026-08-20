from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class GoalAmountSeed(BaseModel):
    """Stable goal identity retained while a target or contribution is collected."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        alias_generator=_camel_case,
    )

    schema_version: Literal[1] = 1
    operation: Literal["save_goal", "contribute_goal"]
    goal_id: UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    currency: str = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def require_goal_for_contribution(self):
        if self.operation == "contribute_goal" and self.goal_id is None:
            raise ValueError("A contribution continuation requires its goal id")
        if self.operation == "save_goal" and self.goal_id is not None:
            raise ValueError("A new-goal continuation cannot carry an existing goal id")
        return self


class GoalAmountContract(GoalAmountSeed):
    amount_minor: int = Field(gt=0)
