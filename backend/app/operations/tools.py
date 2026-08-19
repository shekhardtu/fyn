from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Iterable

from agno.tools.function import Function
from pydantic import BaseModel, ConfigDict, Field

from ..services.agent_tools import bind_schema_tool
from .execution import validate_operation_inputs
from .models import CompiledOperation


# OpenAI strict tools intentionally support a smaller language than the local
# Draft 2020-12 validator. These constraints stay authoritative on the server,
# but must not make an otherwise valid operation tool impossible to register.
# See the Structured Outputs supported-schema table. ``money`` is Fyn's local
# semantic format and likewise remains an execution-time constraint.
_LOCAL_ONLY_PROPOSAL_KEYWORDS = {"default", "minLength", "maxLength", "uniqueItems"}
_PROVIDER_FORMATS = {
    "date-time",
    "time",
    "date",
    "duration",
    "email",
    "hostname",
    "ipv4",
    "ipv6",
    "uuid",
}


class OperationProposal(BaseModel):
    """A model proposal bound to one immutable filesystem operation revision."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    version: int = Field(ge=1)
    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    inputs: dict[str, Any] = Field(default_factory=dict)


def operation_tool_name(operation: CompiledOperation) -> str:
    """Return a provider-safe, deterministic name for one operation revision."""
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", operation.id).strip("_")
    prefix = "propose_operation__"
    candidate = f"{prefix}{normalized}"
    if len(candidate) <= 64:
        return candidate
    digest = hashlib.sha256(operation.id.encode("utf-8")).hexdigest()[:10]
    return f"{candidate[:53]}_{digest}"


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(schema)
    if "anyOf" in value:
        value["anyOf"] = [*value["anyOf"], {"type": "null"}]
        return value
    # Agno normalizes a list-valued ``type`` for function tools by retaining
    # its first item. ``anyOf`` preserves the nullable proposal contract all
    # the way to the provider while remaining valid strict JSON Schema.
    outer = {
        key: value.pop(key)
        for key in ("title", "description")
        if key in value
    }
    return {**outer, "anyOf": [value, {"type": "null"}]}


def strict_proposal_schema(operation: CompiledOperation) -> dict[str, Any]:
    """Compile an operation input schema for strict model-side proposals.

    Strict function calling requires every property to appear in ``required``.
    Domain-required and optional values are both nullable at this proposal
    boundary: null means the customer did not supply that value, and the
    generic operation form will collect it. Before execution, the original
    non-null filesystem schema remains authoritative.
    """
    schema = copy.deepcopy(operation.definition.input.schema_)

    def normalize(value: dict[str, Any], *, root: bool = False) -> dict[str, Any]:
        for keyword in _LOCAL_ONLY_PROPOSAL_KEYWORDS:
            value.pop(keyword, None)
        if value.get("format") not in (None, *_PROVIDER_FORMATS):
            value.pop("format", None)
        if value.get("type") == "object":
            properties = value.get("properties", {})
            normalized = {
                name: _nullable(normalize(child))
                for name, child in properties.items()
            }
            value["properties"] = normalized
            value["required"] = list(normalized)
            value["additionalProperties"] = False
        elif value.get("type") == "array" and isinstance(value.get("items"), dict):
            value["items"] = normalize(value["items"])
        return value if root else value

    return normalize(schema, root=True)


def _clean_proposal_inputs(values: dict[str, Any]) -> dict[str, Any]:
    # Null is the strict-tool representation of an input that was not present
    # in the customer request. Nested nulls are real values and remain intact.
    return {name: value for name, value in values.items() if value is not None}


def build_operation_proposal_tool(operation: CompiledOperation) -> Function:
    """Expose a filesystem operation as a non-executing, strictly typed tool."""

    def propose(**values: Any) -> dict[str, Any]:
        inputs = _clean_proposal_inputs(values)
        validate_operation_inputs(operation, inputs, require_complete=False)
        return OperationProposal(
            operation_id=operation.id,
            version=operation.version,
            checksum=operation.checksum,
            inputs=inputs,
        ).model_dump(mode="json")

    discovery = operation.definition.discovery
    description = " ".join(
        part
        for part in (
            f"Propose the governed operation '{operation.definition.metadata.title}'.",
            discovery.description,
            operation.definition.instructions.selection,
            operation.definition.instructions.argument_collection,
            f"Effect: {operation.derived_effect.value}.",
            "Use null for any value the customer did not supply; never guess it.",
        )
        if part
    )
    tool = bind_schema_tool(
        propose,
        name=operation_tool_name(operation),
        description=description,
        parameters=strict_proposal_schema(operation),
        strict=True,
        stop_after_tool_call=True,
    )
    # This trusted mapping is never sent as a model-authored argument.
    tool._fyn_operation_id = operation.id  # type: ignore[attr-defined]
    tool._fyn_operation_version = operation.version  # type: ignore[attr-defined]
    tool._fyn_operation_checksum = operation.checksum  # type: ignore[attr-defined]
    return tool


def build_operation_proposal_tools(
    operations: Iterable[CompiledOperation],
) -> list[Function]:
    tools = [build_operation_proposal_tool(operation) for operation in operations]
    names = [tool.name for tool in tools]
    if len(names) != len(set(names)):
        raise ValueError("Operation proposal tool names must be unique")
    return tools


def proposal_from_tool_execution(
    execution: Any,
    operations: Iterable[CompiledOperation],
) -> OperationProposal | None:
    """Validate one completed proposal call against its server-owned binding."""
    by_name = {operation_tool_name(operation): operation for operation in operations}
    operation = by_name.get(str(getattr(execution, "tool_name", "")))
    if operation is None or getattr(execution, "tool_call_error", False):
        return None
    values = _clean_proposal_inputs(dict(getattr(execution, "tool_args", None) or {}))
    validate_operation_inputs(operation, values, require_complete=False)
    return OperationProposal(
        operation_id=operation.id,
        version=operation.version,
        checksum=operation.checksum,
        inputs=values,
    )
