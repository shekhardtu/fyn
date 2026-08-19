from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from ..models import User
from ..operation_types import ConfirmationPolicy
from .catalog import OperationCatalogManager
from .models import CompiledOperation
from .primitives import primitive


class OperationInputError(ValueError):
    pass


class OperationChangedError(ValueError):
    pass


@dataclass(frozen=True)
class OperationExecutionResult:
    operation_id: str
    version: int
    checksum: str
    outputs: dict[str, Any]
    message: str


def validate_operation_inputs(
    operation: CompiledOperation,
    values: dict[str, Any],
    *,
    require_complete: bool = True,
) -> dict[str, Any]:
    schema = dict(operation.definition.input.schema_)
    if not require_complete:
        schema.pop("required", None)
    errors = sorted(Draft202012Validator(schema).iter_errors(values), key=lambda item: list(item.path))
    if errors:
        readable = "; ".join(
            f"{'.'.join(map(str, error.path)) or 'input'}: {error.message}" for error in errors[:5]
        )
        raise OperationInputError(readable)
    return values


def missing_required_inputs(operation: CompiledOperation, values: dict[str, Any]) -> list[str]:
    return [
        name for name in operation.definition.input.schema_.get("required", [])
        if name not in values or values[name] in (None, "", [])
    ]


def bind_operation_route_inputs(
    operation: CompiledOperation,
    values: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the operation file's bounded route bindings.

    The returned keys belong to the small engine routing protocol declared in
    ``OperationRouting``.  No operation id or primitive name is interpreted
    here, so adding an operation that uses an existing strategy and primitive
    cannot create another source-code branch.
    """
    validate_operation_inputs(operation, values, require_complete=False)
    resolved: dict[str, Any] = {}
    for target, reference in operation.definition.routing.bindings.items():
        if reference == "${input}":
            resolved[target] = dict(values)
            continue
        field = reference.removeprefix("${input.").removesuffix("}")
        if field in values and values[field] is not None:
            resolved[target] = values[field]
    return resolved


def operation_inputs_from_route(
    operation: CompiledOperation,
    route_values: dict[str, Any],
    *,
    request: str | None = None,
) -> dict[str, Any]:
    """Reconstruct workflow inputs from a typed engine route.

    This is the inverse of ``bind_operation_route_inputs`` for protected core
    operations selected through structured output or deterministic recovery.
    A file may explicitly declare a top-level ``request`` input; the engine
    then binds the current user message without exposing mutable server state.
    """
    values: dict[str, Any] = {}
    for target, reference in operation.definition.routing.bindings.items():
        value = route_values.get(target)
        if value is None:
            continue
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json", exclude_none=True)
        if reference == "${input}":
            if not isinstance(value, dict):
                raise OperationInputError(f"Routing target {target} must bind an input object")
            declared = operation.definition.input.schema_.get("properties", {})
            values.update({key: item for key, item in value.items() if key in declared})
            continue
        field = reference.removeprefix("${input.").removesuffix("}")
        values[field] = value
    properties = operation.definition.input.schema_.get("properties", {})
    if request is not None and "request" in properties and "request" not in values:
        values["request"] = request
    validate_operation_inputs(operation, values, require_complete=False)
    return values


_REFERENCE = re.compile(r"^\$\{(.+)\}$")


def _resolve(value: Any, inputs: dict[str, Any], outputs: dict[str, Any], item: Any = None) -> Any:
    if not isinstance(value, str):
        return value
    matched = _REFERENCE.fullmatch(value)
    if not matched:
        return value
    path = matched.group(1)
    if path == "item":
        return item
    if path.startswith("input."):
        return inputs[path.split(".", 1)[1]]
    if path.startswith("steps."):
        _, step_id, output_literal, field = path.split(".", 3)
        if output_literal != "output":
            raise OperationInputError(f"Invalid step reference: {value}")
        return outputs[step_id][field]
    raise OperationInputError(f"Unsupported reference: {value}")


def render_operation_text(template: str | None, inputs: dict[str, Any]) -> str:
    if not template:
        return ""
    rendered = template
    for key, value in inputs.items():
        rendered = rendered.replace(f"${{input.{key}}}", str(value))
    return rendered


def resolve_current_operation(
    catalog: OperationCatalogManager,
    operation_id: str,
    version: int,
    checksum: str,
) -> CompiledOperation:
    operation = catalog.snapshot().operation(operation_id)
    if not operation:
        raise OperationChangedError("The operation is no longer available")
    if operation.version != version or operation.checksum != checksum:
        raise OperationChangedError("The operation changed after it was prepared")
    return operation


def execute_operation(
    db: Session,
    user: User,
    operation: CompiledOperation,
    values: dict[str, Any],
) -> OperationExecutionResult:
    validate_operation_inputs(operation, values)
    # All authorable v1 primitives are database-local and transactional. A
    # savepoint makes a multi-step operation one mutation inside the request's
    # outer unit of work.
    with db.begin_nested():
        def invoke(target, arguments):
            if not target.ops_authorable or target.execute is None:
                raise ValueError(f"Primitive {target.reference} cannot execute as a managed operation")
            return target.execute(db, user, arguments)

        outputs = execute_operation_steps(operation, values, invoke)
        db.flush()
    message = render_operation_text(operation.definition.presentation.success.message, values)
    return OperationExecutionResult(
        operation_id=operation.id,
        version=operation.version,
        checksum=operation.checksum,
        outputs=outputs,
        message=message,
    )


StepInvoker = Callable[[Any, dict[str, Any]], Any]


def execute_operation_steps(
    operation: CompiledOperation,
    values: dict[str, Any],
    invoke: StepInvoker,
) -> dict[str, Any]:
    """Execute one compiled workflow through an injected trusted invoker.

    Both protected core operations and Ops-authored operations use this exact
    step/reference/input-validation loop. The only difference is which trusted
    primitive invoker the server injects at the security boundary.
    """
    validate_operation_inputs(operation, values)
    outputs: dict[str, Any] = {}
    for step in operation.definition.execution.steps:
        target = primitive(step.uses)
        iterable = [None]
        if step.for_each:
            field = step.for_each.removeprefix("${input.").removesuffix("}")
            iterable = values[field]
        step_results = []
        for item in iterable:
            arguments = {
                key: _resolve(value, values, outputs, item)
                for key, value in step.with_.items()
            }
            validated = target.input_model.model_validate(arguments).model_dump(
                mode="json", by_alias=True
            )
            step_results.append(invoke(target, validated))
        outputs[step.id] = step_results if step.for_each else step_results[0]
    return outputs


def requires_confirmation(operation: CompiledOperation) -> bool:
    return operation.derived_confirmation is not ConfirmationPolicy.NEVER
