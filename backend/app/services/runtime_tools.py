from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from inspect import signature
from types import ModuleType
from typing import Callable, Literal

from sqlalchemy.orm import Session

from ..models import User
from . import calculators, grounding_tools, taxonomy
from .agent_tools import bind_existing_tool, contract_for


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


def build_runtime_tools(db: Session, user: User, today: date) -> list:
    context = {"db": db, "user": user, "user_id": user.id, "today": today}
    return [
        bind_existing_tool(spec.function, *(context[name] for name in spec.dependencies))
        for spec in RUNTIME_TOOL_REGISTRY
    ]


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
