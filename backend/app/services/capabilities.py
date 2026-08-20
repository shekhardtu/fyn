from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain import ValueEnum
from ..operation_types import AccessMode, ConfirmationPolicy, DataEffect, ValidationMode
from ..operations.catalog import operation_catalog


def _enum_member(operation_id: str) -> str:
    """Turn a protected file id into a stable Python enum member name."""
    return re.sub(r"[^A-Z0-9]+", "_", operation_id.upper()).strip("_")


def _protected_capability_members() -> dict[str, str]:
    operations = operation_catalog().snapshot().core_operations
    members = {_enum_member(operation.id): operation.id for operation in operations}
    if len(members) != len(operations):
        raise RuntimeError("Protected operation ids must have unique normalized names")
    return members


# This enum is generated from the protected files at process start so Pydantic
# and model tool schemas retain a closed enum. Its membership is no longer
# duplicated in source code. Managed operations remain runtime-dynamic through
# the generic managed-operation capability and never need an enum member.
if TYPE_CHECKING:
    # The runtime enum's members come from protected operation files, so its
    # exact class cannot be declared statically without duplicating that source
    # of truth. ValueEnum supplies the shared member interface to the checker.
    CapabilityId = ValueEnum
else:
    CapabilityId = ValueEnum(
        "CapabilityId",
        _protected_capability_members(),
        module=__name__,
    )


@dataclass(frozen=True)
class CapabilitySpec:
    id: CapabilityId
    access: AccessMode
    primitive_refs: tuple[str, ...]
    execution_label: str
    maximum_effect: DataEffect
    confirmation: ConfirmationPolicy
    metric: str | None = None
    validation: ValidationMode = ValidationMode.DETERMINISTIC

    @property
    def is_safe_read(self) -> bool:
        return (
            self.access in {AccessMode.READ, AccessMode.COMPUTE}
            and self.maximum_effect is DataEffect.NONE
        )

    @property
    def requires_model_validation(self) -> bool:
        return self.validation is ValidationMode.MODEL

    @property
    def can_mutate(self) -> bool:
        return self.maximum_effect is DataEffect.MUTATION

    def invokes(self, reference: str) -> bool:
        return reference in self.primitive_refs


def _spec_from_operation(operation) -> CapabilitySpec:
    definition = operation.definition
    return CapabilitySpec(
        id=CapabilityId(operation.id),
        access=definition.eligibility.access,
        primitive_refs=tuple(step.uses for step in definition.execution.steps),
        execution_label=definition.execution.label,
        maximum_effect=operation.derived_effect,
        confirmation=operation.derived_confirmation,
        metric=definition.execution.metric,
        validation=definition.execution.validation,
    )


def capability_specs() -> tuple[CapabilitySpec, ...]:
    """Return live protected capability policy from the active file snapshot."""
    return tuple(
        _spec_from_operation(operation)
        for operation in operation_catalog().snapshot().core_operations
    )


def capability_spec(capability_id: CapabilityId | str) -> CapabilitySpec:
    operation = operation_catalog().snapshot().operation(str(capability_id))
    if operation is None or operation.source != "core":
        raise RuntimeError(f"Protected core operation is missing: {capability_id}")
    return _spec_from_operation(operation)


def capability_for_metric(metric: str | None) -> CapabilityId | None:
    if metric is None:
        return None
    return next((item.id for item in capability_specs() if item.metric == metric), None)


def capabilities_for_primitives(*references: str) -> frozenset[CapabilityId]:
    accepted = set(references)
    return frozenset(
        item.id for item in capability_specs()
        if accepted.intersection(item.primitive_refs)
    )


def capability_for_primitive(reference: str) -> CapabilityId:
    matches = capabilities_for_primitives(reference)
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one protected capability for {reference}, found {len(matches)}"
        )
    return next(iter(matches))


def capability_invokes(capability_id: CapabilityId | str, reference: str) -> bool:
    return capability_spec(capability_id).invokes(reference)


def safe_read_capabilities() -> frozenset[CapabilityId]:
    return frozenset(item.id for item in capability_specs() if item.is_safe_read)


def draft_capabilities() -> frozenset[CapabilityId]:
    return frozenset(
        item.id for item in capability_specs()
        if item.maximum_effect is DataEffect.DRAFT
    )


def mutation_capabilities() -> frozenset[CapabilityId]:
    return frozenset(item.id for item in capability_specs() if item.can_mutate)


def query_capabilities() -> frozenset[CapabilityId]:
    return capabilities_for_primitives(
        "finance.query@1",
        "transaction.search@1",
        "calculator.loan@1",
        "calculator.investment@1",
    )


def validate_capability_catalog() -> None:
    specs = capability_specs()
    if {item.id.value for item in specs} != {
        operation.id for operation in operation_catalog().snapshot().core_operations
    }:
        raise RuntimeError("Capability enum must match protected operation files exactly")
    if any(not item.primitive_refs for item in specs):
        raise RuntimeError("Every capability must invoke at least one declared primitive")
    if any(
        item.is_safe_read and item.maximum_effect is not DataEffect.NONE
        for item in specs
    ):
        raise RuntimeError("Safe read capabilities cannot have draft or mutation effects")
    if any(
        item.maximum_effect is DataEffect.NONE
        and item.confirmation is not ConfirmationPolicy.NEVER
        for item in specs
    ):
        raise RuntimeError("Effect-free capabilities cannot require confirmation")
    if any(
        item.maximum_effect is DataEffect.MUTATION
        and item.confirmation is ConfirmationPolicy.NEVER
        for item in specs
    ):
        raise RuntimeError("Mutation-capable workflows must declare confirmation")


validate_capability_catalog()
