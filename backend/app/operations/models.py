from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..operation_types import (
    AccessMode,
    ConfirmationPolicy,
    DataEffect,
    ValidationMode,
)


OPERATION_ID_PATTERN = r"^[a-z][a-z0-9_.-]{1,99}$"
REFERENCE_PATTERN = re.compile(r"^\$\{(?:input\.[A-Za-z][A-Za-z0-9_]*|steps\.[a-z][a-z0-9_]*\.output\.[A-Za-z][A-Za-z0-9_]*|item)\}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CommonInstructions(StrictModel):
    api_version: Literal["fyn.ai/v1"] = Field(alias="apiVersion")
    kind: Literal["CommonInstructions"]
    instructions: list[str] = Field(min_length=1, max_length=100)


class OperationMetadata(StrictModel):
    id: str = Field(pattern=OPERATION_ID_PATTERN)
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=120)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    owner: str = Field(min_length=1, max_length=80)
    enabled: bool = True


class OperationDiscovery(StrictModel):
    description: str = Field(min_length=3, max_length=500)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    examples: list[str] = Field(default_factory=list, max_length=30)
    negative_examples: list[str] = Field(default_factory=list, alias="negativeExamples", max_length=30)
    model_selectable: bool = Field(default=True, alias="modelSelectable")


class OperationEligibility(StrictModel):
    required_permissions: list[str] = Field(default_factory=list, alias="requiredPermissions", max_length=20)
    expected_effect: DataEffect = Field(alias="expectedEffect")
    access: AccessMode = AccessMode.WORKFLOW


class OperationInput(StrictModel):
    schema_: dict[str, Any] = Field(alias="schema")

    @field_validator("schema_")
    @classmethod
    def object_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError("Operation input schema must have type=object")
        if value.get("additionalProperties", False) is not False:
            raise ValueError("Operation inputs must set additionalProperties=false")
        return value


class OperationClarification(StrictModel):
    required_fields: list[str] = Field(default_factory=list, alias="requiredFields", max_length=30)
    prompt: str | None = Field(default=None, max_length=500)


class OperationInstructions(StrictModel):
    selection: str = Field(min_length=3, max_length=1000)
    argument_collection: str | None = Field(default=None, alias="argumentCollection", max_length=1000)
    response: str | None = Field(default=None, max_length=1000)


OperationRouteStrategy = Literal["decision", "planner", "managed", "protocol"]
OperationRouteTarget = Literal[
    "transaction",
    "query",
    "taxonomy",
    "presentation",
    "clarification",
    "reply",
    "request",
]


class OperationRouting(StrictModel):
    """Compile a file-owned proposal into an engine-owned route.

    Bindings deliberately target a small engine protocol rather than Python
    callables.  An operation can bind its complete validated input object or a
    top-level input field; it cannot name classes, modules, SQL, or code.
    """

    strategy: OperationRouteStrategy
    bindings: dict[OperationRouteTarget, str] = Field(default_factory=dict, max_length=8)

    @field_validator("bindings")
    @classmethod
    def safe_input_bindings(cls, value: dict[str, str]) -> dict[str, str]:
        for target, reference in value.items():
            if not re.fullmatch(r"\$\{input(?:\.[A-Za-z][A-Za-z0-9_]*)?\}", reference):
                raise ValueError(
                    f"Routing binding {target} must reference the validated input object or a top-level field"
                )
        return value

    @model_validator(mode="after")
    def strategy_targets(self):
        targets = set(self.bindings)
        if self.strategy == "planner" and not targets <= {"request", "presentation"}:
            raise ValueError("Planner operations may bind only request and presentation")
        if self.strategy == "decision" and "request" in targets:
            raise ValueError("Decision operations cannot bind a planner request")
        if self.strategy in {"managed", "protocol"} and targets:
            raise ValueError(f"{self.strategy} operations cannot declare route bindings")
        return self


class WorkflowStep(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    uses: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,99}@[1-9][0-9]*$")
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    for_each: str | None = Field(default=None, alias="forEach")

    @field_validator("for_each")
    @classmethod
    def input_array_reference(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"\$\{input\.[A-Za-z][A-Za-z0-9_]*\}", value):
            raise ValueError("forEach must reference a validated top-level input array")
        return value

    @field_validator("with_")
    @classmethod
    def safe_bindings(cls, value: dict[str, Any]) -> dict[str, Any]:
        for binding in value.values():
            if isinstance(binding, str) and binding.startswith("${") and not REFERENCE_PATTERN.fullmatch(binding):
                raise ValueError(f"Unsupported workflow reference: {binding}")
            if isinstance(binding, (dict, list)):
                raise ValueError("Workflow bindings support only literals and scalar references")
        return value


class OperationExecution(StrictModel):
    label: str = Field(min_length=3, max_length=120)
    metric: str | None = Field(default=None, max_length=100)
    validation: ValidationMode = ValidationMode.DETERMINISTIC
    steps: list[WorkflowStep] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_step_ids(self):
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("Workflow step ids must be unique")
        return self


class OperationApproval(StrictModel):
    minimum: ConfirmationPolicy
    title: str | None = Field(default=None, max_length=160)
    summary: str | None = Field(default=None, max_length=500)


class PresentationMessage(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=500)


class OperationPresentation(StrictModel):
    success: PresentationMessage
    failure: PresentationMessage


class OperationTest(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    request: str = Field(min_length=1, max_length=1000)
    expected_inputs: dict[str, Any] | None = Field(default=None, alias="expectedInputs")
    expected_effect: DataEffect | None = Field(default=None, alias="expectedEffect")
    expected_approval: ConfirmationPolicy | None = Field(default=None, alias="expectedApproval")
    expected_match: bool = Field(default=True, alias="expectedMatch")


class OperationDefinition(StrictModel):
    api_version: Literal["fyn.ai/v1"] = Field(alias="apiVersion")
    kind: Literal["Operation"]
    metadata: OperationMetadata
    discovery: OperationDiscovery
    eligibility: OperationEligibility
    input: OperationInput
    clarification: OperationClarification | None = None
    instructions: OperationInstructions
    routing: OperationRouting
    execution: OperationExecution
    approval: OperationApproval
    presentation: OperationPresentation
    tests: list[OperationTest] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def coherent_policy(self):
        effect = self.eligibility.expected_effect
        approval = self.approval.minimum
        if effect is DataEffect.NONE and approval is not ConfirmationPolicy.NEVER:
            raise ValueError("Effect-free operations cannot require confirmation")
        if effect is DataEffect.MUTATION and approval is ConfirmationPolicy.NEVER:
            raise ValueError("Mutation operations must require confirmation")
        return self


class CompiledOperation(StrictModel):
    definition: OperationDefinition
    checksum: str
    source: Literal["core", "managed"]
    source_path: str
    derived_effect: DataEffect
    derived_confirmation: ConfirmationPolicy
    derived_permissions: list[str]

    @property
    def id(self) -> str:
        return self.definition.metadata.id

    @property
    def version(self) -> int:
        return self.definition.metadata.version

    @property
    def enabled(self) -> bool:
        return self.definition.metadata.enabled


class CatalogHealth(StrictModel):
    status: Literal["ok", "degraded", "uninitialized"] = "uninitialized"
    catalog_hash: str | None = Field(default=None, alias="catalogHash")
    generation: int = 0
    core_count: int = Field(default=0, alias="coreCount")
    managed_count: int = Field(default=0, alias="managedCount")
    last_loaded_at: str | None = Field(default=None, alias="lastLoadedAt")
    last_error_at: str | None = Field(default=None, alias="lastErrorAt")
    last_error_code: str | None = Field(default=None, alias="lastErrorCode")
