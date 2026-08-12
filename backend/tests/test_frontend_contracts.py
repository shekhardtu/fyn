from __future__ import annotations

import ast
import importlib.util
import inspect
import json
from pathlib import Path
import textwrap

from app.contracts import frontend_contract_bundle
from app.schemas import WidgetActionId
from app.services.conversation import handle_action


ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "frontend" / "src" / "lib" / "generated"
GENERATOR_PATH = ROOT / "backend" / "scripts" / "generate_frontend_contracts.py"


def _generator():
    spec = importlib.util.spec_from_file_location("frontend_contract_generator", GENERATOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_types(bundle: dict) -> str:
    return _generator().render_types(bundle)


def _render_zod(bundle: dict) -> str:
    return _generator().render_zod(bundle)


def test_frontend_contract_artifacts_match_backend_models():
    """A backend contract change must regenerate every frontend artifact.

    The Zod module is included because it is what the browser now validates
    against: leaving it behind would let the frontend enforce a contract the
    backend has already moved on from, silently.
    """
    bundle = frontend_contract_bundle()

    assert json.loads((GENERATED / "contracts.json").read_text()) == bundle
    assert (GENERATED / "contracts.ts").read_text() == _render_types(bundle)
    assert (GENERATED / "contracts.zod.ts").read_text() == _render_zod(bundle)


def test_widget_action_registry_exactly_matches_action_handler():
    """The public action enum and dispatcher must evolve as one contract."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(handle_action)))
    handled: set[str] = set()

    def action_value(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "WidgetActionId"
        ):
            return getattr(WidgetActionId, node.attr).value
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not isinstance(node.left, ast.Name) or node.left.id != "action":
            continue
        for comparator in node.comparators:
            if value := action_value(comparator):
                handled.add(value)
            elif isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                handled.update(
                    value
                    for item in comparator.elts
                    if (value := action_value(item)) is not None
                )

    assert handled == {item.value for item in WidgetActionId}
