from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from inspect import signature
import json
from types import ModuleType
from typing import Any, Callable, Literal

from sqlalchemy.orm import Session

from ..models import User
from . import calculators, grounding_tools, taxonomy
from .agent_tools import bind_existing_tool, bind_schema_tool, contract_for
from .currency import format_money_minor


Dependency = Literal["db", "user", "user_id", "today"]


@dataclass(frozen=True)
class RuntimeToolSpec:
    function: Callable

    @property
    def name(self) -> str:
        contract = contract_for(self.function)
        if not contract:
            raise RuntimeError(f"{self.function.__name__} is missing @tool_contract")
        return contract.name

    @property
    def dependencies(self) -> tuple[Dependency, ...]:
        """Derive trusted leading arguments from the existing function."""
        dependencies: list[Dependency] = []
        public_parameter_seen = False
        for parameter in signature(self.function).parameters.values():
            if parameter.name in _DEPENDENCY_NAMES:
                if public_parameter_seen:
                    raise RuntimeError(
                        f"Tool {self.name} has infrastructure parameter {parameter.name} "
                        "after a model-visible parameter"
                    )
                dependencies.append(_DEPENDENCY_NAMES[parameter.name])
            else:
                public_parameter_seen = True
        return tuple(dependencies)


_DEPENDENCY_NAMES: dict[str, Dependency] = {
    "db": "db",
    "user": "user",
    "user_id": "user_id",
    "today": "today",
}
_RUNTIME_TOOL_MODULES = (taxonomy, grounding_tools, calculators)


def _annotated_functions(module: ModuleType) -> tuple[Callable, ...]:
    """Discover functions explicitly authorized by their tool annotation."""
    return tuple(
        value
        for value in vars(module).values()
        if callable(value)
        and getattr(value, "__module__", None) == module.__name__
        and contract_for(value) is not None
    )


RUNTIME_TOOL_REGISTRY: tuple[RuntimeToolSpec, ...] = tuple(
    RuntimeToolSpec(function)
    for module in _RUNTIME_TOOL_MODULES
    for function in _annotated_functions(module)
)

if len({item.name for item in RUNTIME_TOOL_REGISTRY}) != len(RUNTIME_TOOL_REGISTRY):
    raise RuntimeError("Runtime tool names must be unique")


FINANCIAL_CALCULATOR_TOOL_NAME = "run_financial_calculator"


def _schema_type_label(schema: dict) -> str:
    if "type" in schema:
        return str(schema["type"])
    variants = schema.get("anyOf") or []
    labels = [str(item.get("type")) for item in variants if item.get("type") != "null"]
    return " | ".join(labels) or "value"


def _calculator_display(value: Any, currency: str) -> Any:
    """Add small copy-ready money values without duplicating dataset rows."""

    if not isinstance(value, dict):
        return None
    displayed: dict[str, Any] = {}
    for key, item in value.items():
        if key == "rows":
            continue
        if (
            str(key).casefold().endswith("_minor")
            and isinstance(item, int)
            and not isinstance(item, bool)
        ):
            displayed[key] = format_money_minor(item, currency)
        elif isinstance(item, dict):
            nested = _calculator_display(item, currency)
            if nested:
                displayed[key] = nested
    return displayed or None


def _calculator_gateway(bound_calculators: list, currency: str) -> Any:
    by_name = {tool.name: tool for tool in bound_calculators}
    names = list(by_name)
    directory = []
    for tool in bound_calculators:
        properties = tool.parameters.get("properties", {})
        arguments = ", ".join(
            f"{name}: {_schema_type_label(schema)}"
            for name, schema in properties.items()
        )
        directory.append(f"- {tool.name}({arguments}): {tool.description}")

    def run_financial_calculator(calculator: str, arguments_json: str) -> dict:
        selected = by_name.get(calculator)
        if selected is None:
            return {"error": {
                "code": "unknown_financial_calculator",
                "detail": "Select one calculator from the provider schema enum.",
                "available": names,
            }}
        try:
            arguments = json.loads(arguments_json)
        except (json.JSONDecodeError, TypeError):
            return {"error": {
                "code": "invalid_calculator_arguments_json",
                "detail": "arguments_json must encode one JSON object.",
            }}
        if not isinstance(arguments, dict):
            return {"error": {
                "code": "invalid_calculator_arguments_shape",
                "detail": "arguments_json must encode one JSON object.",
            }}
        try:
            result = selected.entrypoint(**arguments)
        except Exception as error:
            return {"error": {
                "code": "invalid_calculator_arguments",
                "calculator": calculator,
                "detail": f"{type(error).__name__}: {error}"[:500],
            }}
        payload = {
            "kind": "deterministic_financial_calculation",
            "calculator": calculator,
            # Successful normalized inputs are part of the deterministic
            # calculation result. This lets answer validation safely restate
            # a scenario's principal, rate, and tenure without trusting model
            # prose or reading provider tool arguments as evidence.
            "inputs": arguments,
            "result": result,
        }
        display = _calculator_display(result, currency)
        if display:
            payload["display"] = display
        return payload

    return bind_schema_tool(
        run_financial_calculator,
        name=FINANCIAL_CALCULATOR_TOOL_NAME,
        description=(
            "Run exactly one deterministic finance calculator. Choose the capability yourself "
            "from the directory and pass its named inputs as one JSON object string. Money inputs "
            "are integer minor units. If the result has an error, correct the arguments once.\n"
            + "\n".join(directory)
        ),
        parameters={
            "type": "object",
            "properties": {
                "calculator": {"type": "string", "enum": names},
                "arguments_json": {
                    "type": "string",
                    "description": "A JSON object string containing exactly the selected calculator's named inputs.",
                },
            },
            "required": ["calculator", "arguments_json"],
            "additionalProperties": False,
        },
        strict=True,
    )


def build_runtime_tools(db: Session, user: User, today: date) -> list:
    context = {"db": db, "user": user, "user_id": user.id, "today": today}
    bound = [
        bind_existing_tool(spec.function, *(context[name] for name in spec.dependencies))
        for spec in RUNTIME_TOOL_REGISTRY
    ]
    direct = [
        tool
        for spec, tool in zip(RUNTIME_TOOL_REGISTRY, bound)
        if spec.function.__module__ != calculators.__name__
    ]
    calculator_tools = [
        tool
        for spec, tool in zip(RUNTIME_TOOL_REGISTRY, bound)
        if spec.function.__module__ == calculators.__name__
    ]
    if calculator_tools:
        direct.append(_calculator_gateway(calculator_tools, user.currency))
    return direct


def runtime_tool_contract(name: str):
    spec = next((item for item in RUNTIME_TOOL_REGISTRY if item.name == name), None)
    return contract_for(spec.function) if spec else None


def capability_notes() -> list[str]:
    """One-line answerability notes for suggestion grounding.

    Derived from the same registry the Operator binds, so a suggested question
    can never reference a capability the product does not have.
    """
    notes = [
        f"{contract.name}: {contract.description}"
        for contract in (contract_for(spec.function) for spec in RUNTIME_TOOL_REGISTRY)
        if contract
    ]
    notes.append(
        "Governed workflows: record or edit transactions, category and subcategory changes, "
        "budgets and goals, transaction lists, charts and dashboards over recorded transactions."
    )
    # Analytical reads are not runtime tools — they execute through the template
    # pool and the governed harness — so the registry cannot describe them and
    # this note must, or the suggester would rule out questions fyn can answer.
    notes.append(
        "Governed analyses: spending totals and breakdowns by category, subcategory, merchant, "
        "account or month for any period; month-over-month and period comparisons; income, "
        "expenses and net cash position; recurring expenses and subscriptions; change drivers "
        "and affordability checks."
    )
    return notes
