from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from inspect import Parameter, Signature, signature
import json
import sys
from typing import Any, ForwardRef, get_type_hints

from agno.tools import tool
from agno.tools.function import Function
from pydantic import BaseModel, create_model


@dataclass(frozen=True)
class ToolContract:
    name: str
    description: str
    strict: bool = False
    input_model: type[BaseModel] | None = None
    output_model: type[BaseModel] | None = None


def tool_contract(
    *,
    name: str | None = None,
    description: str,
    strict: bool = False,
    input_model: type[BaseModel] | None = None,
    output_model: type[BaseModel] | None = None,
):
    """Annotate an ordinary domain function as a model-callable capability."""
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        function.__tool_contract__ = ToolContract(  # type: ignore[attr-defined]
            name=name or function.__name__,
            description=description,
            strict=strict,
            input_model=input_model,
            output_model=output_model,
        )
        return function
    return decorate


def contract_for(function: Callable[..., Any]) -> ToolContract | None:
    return getattr(function, "__tool_contract__", None)


def _public_signature(function: Callable[..., Any], bound_args: Sequence[Any]) -> tuple[Callable[..., Any], Signature, dict[str, Any]]:
    """Bind trusted leading dependencies while preserving a model-visible schema."""
    bound = partial(function, *bound_args)
    public_signature = signature(bound)
    try:
        resolved_hints = get_type_hints(function)
    except TypeError:
        # Python 3.9 cannot natively evaluate PEP 604 annotations written under
        # ``from __future__ import annotations``. This is already a conditional
        # project dependency for Pydantic, so use the same backport here.
        from eval_type_backport import eval_type_backport

        globalns = vars(sys.modules[function.__module__])
        resolved_hints = {
            name: eval_type_backport(ForwardRef(value) if isinstance(value, str) else value, globalns, globalns)
            for name, value in getattr(function, "__annotations__", {}).items()
        }
    public_hints = {
        name: resolved_hints[name]
        for name in public_signature.parameters
        if name in resolved_hints
    }
    if "return" in resolved_hints:
        public_hints["return"] = resolved_hints["return"]
    return bound, public_signature, public_hints


def _parameters_schema(public_signature: Signature, public_hints: dict[str, Any], strict: bool) -> dict[str, Any]:
    fields = {
        name: (
            public_hints.get(name, Any),
            ... if parameter.default is Parameter.empty else parameter.default,
        )
        for name, parameter in public_signature.parameters.items()
    }
    schema = create_model("BoundToolArguments", **fields).model_json_schema()
    schema.pop("title", None)
    if strict:
        schema["additionalProperties"] = False
        schema["required"] = list(fields)
    return schema


def bind_existing_tool(
    function: Callable[..., Any],
    *bound_args: Any,
    name: str | None = None,
    description: str | None = None,
    strict: bool | None = None,
) -> Function:
    """Expose an existing function as an Agno tool without changing that function.

    Trusted infrastructure arguments such as a database session and authenticated
    user ID are bound positionally and therefore omitted from the model-visible
    JSON schema. The remaining signature and resolved type annotations come from
    the source function, keeping it as the single implementation.
    """
    contract = contract_for(function)
    resolved_name = name or (contract.name if contract else function.__name__)
    resolved_description = description or (contract.description if contract else function.__doc__)
    resolved_strict = strict if strict is not None else (contract.strict if contract else False)
    if not resolved_description:
        raise ValueError(f"Tool {resolved_name} requires a description or @tool_contract annotation")
    bound, public_signature, public_hints = _public_signature(function, bound_args)
    if contract and contract.input_model:
        contract_fields = set(contract.input_model.model_fields)
        signature_fields = set(public_signature.parameters)
        if contract_fields != signature_fields:
            raise ValueError(
                f"Tool {resolved_name} input model fields {sorted(contract_fields)} "
                f"do not match public signature {sorted(signature_fields)}"
            )

    def invoke(*args: Any, **kwargs: Any) -> Any:
        if contract and contract.input_model:
            supplied = public_signature.bind(*args, **kwargs)
            validated = contract.input_model.model_validate(supplied.arguments)
            result = bound(**validated.model_dump())
        else:
            result = bound(*args, **kwargs)
        if contract and contract.output_model:
            result = contract.output_model.model_validate(result).model_dump(mode="json", exclude_none=True)
        # Agno tool results must be safe to place in a model message. Converting
        # at this one boundary also handles dates, UUIDs, Decimals and Pydantic
        # values returned by future SSOT functions.
        return json.loads(json.dumps(result, default=str))

    invoke.__name__ = resolved_name
    invoke.__doc__ = resolved_description
    invoke.__annotations__ = public_hints
    invoke.__signature__ = public_signature.replace(  # type: ignore[attr-defined]
        parameters=[
            parameter.replace(annotation=public_hints.get(parameter.name, Parameter.empty))
            for parameter in public_signature.parameters.values()
        ],
        return_annotation=public_hints.get("return", Signature.empty),
    )
    bound_tool = tool(name=resolved_name, description=resolved_description, strict=resolved_strict)(invoke)
    # Agno 2.x renders datetime.date as an untyped object. Pydantic owns the
    # validation wrapper already, so use its standards-compliant JSON schema at
    # the same boundary (for example, date becomes string/format=date).
    if contract and contract.input_model:
        bound_tool.parameters = contract.input_model.model_json_schema()
        bound_tool.parameters.pop("title", None)
        if resolved_strict:
            bound_tool.parameters["additionalProperties"] = False
            bound_tool.parameters["required"] = list(contract.input_model.model_fields)
    else:
        bound_tool.parameters = _parameters_schema(public_signature, public_hints, resolved_strict)
    return bound_tool
