from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from inspect import signature
from types import ModuleType
from typing import Callable, get_args, Literal

from sqlalchemy.orm import Session

from ..models import User
from . import analytics, calculators, taxonomy
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
                dependencies.append(parameter.name)
            else:
                public_parameter_seen = True
        return tuple(dependencies)


_DEPENDENCY_NAMES = frozenset(get_args(Dependency))
_RUNTIME_TOOL_MODULES = (taxonomy, analytics, calculators)


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
