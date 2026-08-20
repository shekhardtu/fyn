from __future__ import annotations

from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .agents import ResolvedIntentContract, TaxonomyInterpretation
from .planning_contracts import BudgetSetupContract, BudgetSetupSeed


class _ContinuationModel(BaseModel):
    """Strict durable state: resumptions must never accept model-shaped extras."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CancelContinuation(_ContinuationModel):
    kind: Literal["cancel"] = "cancel"
    label: str = Field(min_length=1, max_length=100)


class GovernedQueryContinuation(_ContinuationModel):
    kind: Literal["governed_query"] = "governed_query"
    label: str = Field(min_length=1, max_length=100)
    intent: ResolvedIntentContract


class GovernedTaxonomyContinuation(_ContinuationModel):
    kind: Literal["governed_taxonomy"] = "governed_taxonomy"
    label: str = Field(min_length=1, max_length=100)
    taxonomy: TaxonomyInterpretation


class GovernedBudgetContinuation(_ContinuationModel):
    kind: Literal["governed_budget"] = "governed_budget"
    label: str = Field(min_length=1, max_length=100)
    budget: BudgetSetupContract


class LegacyPromptContinuation(_ContinuationModel):
    """Compatibility path for workflows that do not yet expose typed slots.

    The name is deliberately visible in stored traces. It prevents an
    unsupported continuation from masquerading as a zero-pass typed resume.
    """

    kind: Literal["legacy_prompt"] = "legacy_prompt"
    label: str = Field(min_length=1, max_length=100)
    resolution: str = Field(min_length=1, max_length=500)


ClarificationTransition = Annotated[
    Union[
        CancelContinuation,
        GovernedQueryContinuation,
        GovernedTaxonomyContinuation,
        GovernedBudgetContinuation,
        LegacyPromptContinuation,
    ],
    Field(discriminator="kind"),
]
CLARIFICATION_TRANSITION_ADAPTER: TypeAdapter[ClarificationTransition] = TypeAdapter(
    ClarificationTransition
)


class ClarificationContinuationEnvelope(_ContinuationModel):
    """One versioned server-owned state machine for a clarification pause."""

    schema_version: Literal[3, 4] = Field(default=4, alias="schemaVersion")
    clarification_id: UUID = Field(alias="clarificationId")
    source_message_id: UUID = Field(alias="sourceMessageId")
    original_request: str = Field(min_length=1, max_length=20_000, alias="originalRequest")
    options: dict[str, ClarificationTransition] = Field(min_length=1, max_length=7)
    allow_custom: bool = Field(default=False, alias="allowCustom")
    custom_strategy: Literal["route_once", "budget_amount"] = Field(default="route_once", alias="customStrategy")
    custom_budget: BudgetSetupSeed | None = Field(default=None, alias="customBudget")
    clarification_depth: int = Field(default=0, ge=0, le=2, alias="clarificationDepth")
    clarification_fingerprint: str | None = Field(
        default=None,
        min_length=16,
        max_length=64,
        alias="clarificationFingerprint",
    )

    @model_validator(mode="after")
    def require_cancel_transition(self):
        cancel = self.options.get("cancel")
        if not isinstance(cancel, CancelContinuation):
            raise ValueError("A clarification continuation requires a cancel transition")
        if self.custom_strategy == "budget_amount" and self.custom_budget is None:
            raise ValueError("A budget amount continuation requires its typed budget context")
        if self.custom_strategy == "route_once" and self.custom_budget is not None:
            raise ValueError("Only a budget amount continuation may carry budget context")
        return self


def parse_clarification_transition(value: object) -> ClarificationTransition:
    return CLARIFICATION_TRANSITION_ADAPTER.validate_python(value)
