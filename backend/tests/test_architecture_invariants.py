from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

from app import models
from app.config import DEFAULT_CURRENCY
from app.database import Base
from app.services import agents, calculators, grounding_tools, taxonomy
from app.services import conversation
from app.services.agent_tools import contract_for
from app.services.runtime_tools import RUNTIME_TOOL_REGISTRY
from app.operations.catalog import OperationCatalogManager
from app.operations.primitives import primitive_catalog
from app.operations.tools import build_operation_proposal_tool
from app.schemas import WidgetType
from app.validation import DATA_FIELD_KEY_PATTERN, DATA_RESOURCE_ID_PATTERN, SEMANTIC_IDENTIFIER_PATTERN


def test_every_mapped_entity_uses_the_shared_primary_key_policy():
    for mapper in Base.registry.mappers:
        assert issubclass(mapper.class_, models.UUIDPrimaryKeyMixin), mapper.class_.__name__


def test_standard_storage_policies_are_declared_by_mixins():
    for mapper in Base.registry.mappers:
        model = mapper.class_
        table = mapper.local_table
        if "user_id" in table.c and not table.c.user_id.nullable:
            assert issubclass(model, models.UserOwnedMixin), model.__name__
        if "currency" in table.c:
            default = table.c.currency.default
            if default is not None and default.is_scalar and default.arg == DEFAULT_CURRENCY:
                assert issubclass(model, models.CurrencyMixin), model.__name__
        if "conversation_id" in table.c and table.c.conversation_id.foreign_keys:
            foreign_key = next(iter(table.c.conversation_id.foreign_keys))
            if foreign_key.ondelete == "CASCADE":
                assert issubclass(model, models.ConversationChildMixin), model.__name__
        if "transaction_id" in table.c and table.c.transaction_id.foreign_keys:
            foreign_key = next(iter(table.c.transaction_id.foreign_keys))
            if foreign_key.ondelete == "CASCADE":
                assert issubclass(model, models.TransactionChildMixin), model.__name__
        if "confidence" in table.c:
            default = table.c.confidence.default
            if default is not None and default.is_scalar and default.arg == Decimal("1"):
                assert issubclass(model, models.ConfidenceMixin), model.__name__
        if {"scope", "owner_user_id"} <= set(table.c.keys()):
            assert issubclass(model, models.ScopedOwnershipMixin), model.__name__


def test_every_annotated_domain_tool_is_discovered_without_a_second_function_list():
    modules = (taxonomy, grounding_tools, calculators)
    annotated = {
        value
        for module in modules
        for value in vars(module).values()
        if callable(value)
        and getattr(value, "__module__", None) == module.__name__
        and contract_for(value) is not None
    }
    registered = {spec.function for spec in RUNTIME_TOOL_REGISTRY}
    assert registered == annotated


def test_classes_do_not_redeclare_the_same_member_in_one_body():
    """Catch Python's silent overwrite of duplicate fields and methods."""
    app_root = Path(__file__).parents[1] / "app"
    duplicates: list[str] = []
    for path in app_root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            names: dict[str, list[int]] = {}
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.setdefault(member.name, []).append(member.lineno)
                elif isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                    names.setdefault(member.target.id, []).append(member.lineno)
                elif isinstance(member, ast.Assign):
                    for target in member.targets:
                        if isinstance(target, ast.Name):
                            names.setdefault(target.id, []).append(member.lineno)
            duplicates.extend(
                f"{path.relative_to(app_root)}:{node.name}.{name}:{lines}"
                for name, lines in names.items()
                if len(lines) > 1
            )
    assert duplicates == []


def test_shared_identifier_patterns_have_one_source_definition():
    app_root = Path(__file__).parents[1] / "app"
    patterns = (
        SEMANTIC_IDENTIFIER_PATTERN,
        DATA_FIELD_KEY_PATTERN,
        DATA_RESOURCE_ID_PATTERN,
    )
    owners = {
        pattern: [
            path.relative_to(app_root)
            for path in app_root.rglob("*.py")
            if pattern in path.read_text()
        ]
        for pattern in patterns
    }
    assert owners == {pattern: [Path("validation.py")] for pattern in patterns}


def test_protected_models_are_not_loaded_with_unscoped_db_get():
    """User-owned and scoped-taxonomy rows must pass their repository boundary."""
    app_root = Path(__file__).parents[1] / "app"
    protected_names = {
        mapper.class_.__name__
        for mapper in Base.registry.mappers
        if issubclass(
            mapper.class_,
            (models.UserOwnedMixin, models.ScopedOwnershipMixin),
        )
    }
    violations: list[str] = []
    for path in app_root.rglob("*.py"):
        if path.name == "repositories.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "db"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in protected_names
            ):
                continue
            violations.append(
                f"{path.relative_to(app_root)}:{node.lineno}:{node.args[0].id}"
            )
    assert violations == []


def _refs_with_siblings(node, path: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(node, dict):
        if "$ref" in node and len(node) > 1:
            siblings = sorted(key for key in node if key != "$ref")
            findings.append(f"{path or '<root>'} -> $ref + {siblings}")
        for key, value in node.items():
            findings += _refs_with_siblings(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            findings += _refs_with_siblings(item, f"{path}[{index}]")
    return findings


def test_llm_output_schemas_never_put_a_keyword_beside_a_reference():
    """Every agent contract has to survive the model provider's schema check.

    A defaulted enum field renders as `{"$ref": ..., "default": ...}`, which the
    provider rejects outright — and the failure is quiet in the worst way: the
    call errors, the harness falls back to the deterministic path, and the
    feature simply stops using the model. The schemas are discovered from the
    `output_schema=` arguments rather than listed here, so an agent added later
    is covered without anyone remembering to add it.
    """
    source = Path(__file__).parents[1] / "app" / "services" / "agents.py"
    tree = ast.parse(source.read_text())
    names = {
        keyword.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "output_schema" and isinstance(keyword.value, ast.Name)
    }
    assert names, "No agent output schemas were discovered; the search is broken."

    problems: dict[str, list[str]] = {}
    for name in sorted(names):
        model = getattr(agents, name)
        findings = _refs_with_siblings(model.model_json_schema())
        if findings:
            problems[name] = findings
    assert problems == {}


def test_routes_return_their_response_model_rather_than_a_dump_of_it():
    """One layer owns serialization, and it is not the route body.

    A route that answers `Model(...).model_dump(by_alias=True)` builds the
    camelCase body the client wants and then hands it back to FastAPI, which
    re-validates it against the same `response_model` — by field name, not by
    serialization alias. Every aliased field then reads as missing and the route
    answers 500 for reasons no test that calls the function directly can see.
    """
    app_root = Path(__file__).parents[1] / "app"
    violations: list[str] = []
    for path in sorted(app_root.glob("api*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            declares_model = any(
                isinstance(decorator, ast.Call)
                and any(keyword.arg == "response_model" for keyword in decorator.keywords)
                for decorator in node.decorator_list
            )
            if not declares_model:
                continue
            for statement in ast.walk(node):
                if (
                    isinstance(statement, ast.Return)
                    and isinstance(statement.value, ast.Call)
                    and isinstance(statement.value.func, ast.Attribute)
                    and statement.value.func.attr == "model_dump"
                ):
                    violations.append(f"{path.name}:{statement.lineno}:{node.name}")
    assert violations == []


def test_widget_construction_uses_the_canonical_type_enum():
    app_root = Path(__file__).parents[1] / "app"
    widget_values = {item.value for item in WidgetType}
    violations: list[str] = []
    for path in app_root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Widget"
            ):
                continue
            type_keyword = next(
                (item for item in node.keywords if item.arg == "type"),
                None,
            )
            if (
                type_keyword
                and isinstance(type_keyword.value, ast.Constant)
                and type_keyword.value.value in widget_values
            ):
                violations.append(
                    f"{path.relative_to(app_root)}:{node.lineno}:{type_keyword.value.value}"
                )
    assert violations == []


def _strict_object_problems(schema: object, path: str = "input") -> list[str]:
    if not isinstance(schema, dict):
        return []
    problems: list[str] = []
    if schema.get("type") == "object":
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is not False:
            problems.append(f"{path}: object is open")
        if set(schema.get("required", [])) != set(properties):
            problems.append(f"{path}: not every property is required")
        for name, child in properties.items():
            problems += _strict_object_problems(child, f"{path}.{name}")
    if schema.get("type") == "array":
        problems += _strict_object_problems(schema.get("items"), f"{path}[]")
    for index, child in enumerate(schema.get("anyOf", [])):
        problems += _strict_object_problems(child, f"{path}.anyOf[{index}]")
    return problems


def test_every_model_selectable_operation_compiles_to_a_closed_strict_tool():
    operations_root = Path(__file__).parents[1] / "operations"
    managed_root = Path(__file__).parents[1] / "managed-operations"
    snapshot = OperationCatalogManager(operations_root, managed_root).load(initial=True)
    failures: dict[str, list[str]] = {}
    for operation in snapshot.operations.values():
        if not operation.enabled or not operation.definition.discovery.model_selectable:
            continue
        tool = build_operation_proposal_tool(operation)
        problems = _strict_object_problems(tool.parameters)
        if tool.strict is not True:
            problems.append("tool strict flag is not true")
        if problems:
            failures[operation.id] = problems
    assert failures == {}


def test_protected_primitive_runtime_wiring_is_registry_owned():
    runtime = conversation._ConversationPrimitiveRuntime
    missing = {
        item.reference: item.runtime_method
        for item in primitive_catalog()
        if not item.ops_authorable
        and not callable(getattr(runtime, str(item.runtime_method), None))
    }
    assert missing == {}


def test_legacy_operation_routing_seams_do_not_return():
    app_root = Path(__file__).parents[1] / "app"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in app_root.rglob("*.py")
    )
    forbidden = {
        "GOVERNED_HANDOFF_TOOL",
        "_handoff_to_governed_workflow",
        "_exact_core_operation_decision",
        "managed_operation_decision",
        "runtime_handlers",
        "strict=False",
    }
    assert {value for value in forbidden if value in source} == set()
