from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .catalog import OperationCatalogError, compile_operation, operation_catalog, operation_match_score
from .models import OperationDefinition
from .execution import (
    OperationInputError,
    bind_operation_route_inputs,
    execute_operation_steps,
    validate_operation_inputs,
)
from .primitives import primitive_catalog
from .tools import build_operation_proposal_tool


def _compiled(path: Path):
    return compile_operation(path, "managed")


def _validate(path: Path) -> int:
    operation = _compiled(path)
    print(f"valid: {operation.id}@{operation.version} ({operation.checksum[:12]})")
    return 0


def _explain(path: Path) -> int:
    operation = _compiled(path)
    definition = operation.definition
    print(json.dumps({
        "id": operation.id,
        "version": operation.version,
        "checksum": operation.checksum,
        "effect": operation.derived_effect,
        "confirmation": operation.derived_confirmation,
        "permissions": operation.derived_permissions,
        "requiredInputs": definition.input.schema_.get("required", []),
        "steps": [
            {"id": step.id, "uses": step.uses, "forEach": step.for_each}
            for step in definition.execution.steps
        ],
    }, indent=2, default=str))
    return 0


def _test_operation(operation) -> list[str]:
    failures: list[str] = []
    if not operation.definition.tests:
        return [f"{operation.id}: operation has no embedded contract tests"]
    positive_cases = [case for case in operation.definition.tests if case.expected_match]
    if not positive_cases:
        failures.append(f"{operation.id}: operation has no positive contract test")
    required_inputs = set(operation.definition.input.schema_.get("required", []))
    if required_inputs and not any(case.expected_inputs is not None for case in positive_cases):
        failures.append(
            f"{operation.id}: a positive contract test must supply expectedInputs for "
            + ", ".join(sorted(required_inputs))
        )

    if operation.definition.discovery.model_selectable:
        try:
            tool = build_operation_proposal_tool(operation)
            schema = tool.parameters
            if tool.strict is not True:
                failures.append(f"{operation.id}: proposal tool is not strict")
            if schema.get("additionalProperties") is not False:
                failures.append(f"{operation.id}: proposal tool input is not closed")
            if set(schema.get("required", [])) != set(schema.get("properties", {})):
                failures.append(f"{operation.id}: strict proposal tool does not require every property")
        except Exception as exc:
            failures.append(f"{operation.id}: proposal tool compilation failed: {exc}")

    for case in operation.definition.tests:
        score = operation_match_score(operation, case.request)
        matched = score > 0
        if matched != case.expected_match:
            failures.append(
                f"{operation.id}/{case.name}: expected match={case.expected_match}, score={score:.3f}"
            )
        if case.expected_effect is not None and case.expected_effect is not operation.derived_effect:
            failures.append(
                f"{operation.id}/{case.name}: expected effect={case.expected_effect}, got {operation.derived_effect}"
            )
        if case.expected_approval is not None and case.expected_approval is not operation.derived_confirmation:
            failures.append(
                f"{operation.id}/{case.name}: expected approval={case.expected_approval}, "
                f"got {operation.derived_confirmation}"
            )
        if not case.expected_match:
            continue
        values = dict(case.expected_inputs or {})
        try:
            validate_operation_inputs(operation, values)
            if operation.definition.routing.strategy in {"decision", "planner"}:
                bind_operation_route_inputs(operation, values)

            def dry_run(target, arguments):
                output = {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "parentId": "00000000-0000-0000-0000-000000000001",
                    "name": "contract-test",
                    "created": True,
                }
                return {field: output.get(field, "contract-test") for field in target.output_fields}

            execute_operation_steps(operation, values, dry_run)
        except (OperationInputError, ValueError) as exc:
            failures.append(f"{operation.id}/{case.name}: journey dry-run failed: {exc}")
    return failures


def _test(path: Path) -> int:
    operation = _compiled(path)
    failures = _test_operation(operation)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"passed: {len(operation.definition.tests)} embedded tests")
    return 0


def _validate_catalog() -> int:
    snapshot = operation_catalog().load(initial=True)
    print(
        f"valid catalog: {len(snapshot.core_operations)} core, "
        f"{len(snapshot.managed_operations)} managed ({snapshot.catalog_hash[:12]})"
    )
    return 0


def _test_catalog() -> int:
    snapshot = operation_catalog().load(initial=True)
    failures = [
        failure
        for operation in snapshot.operations.values()
        for failure in _test_operation(operation)
    ]
    for operation in snapshot.operations.values():
        if not operation.enabled or not operation.definition.discovery.model_selectable:
            continue
        for case in operation.definition.tests:
            if not case.expected_match:
                continue
            candidates = operation_catalog().candidate_operations(
                case.request,
                limit=12,
                managed_only=False,
            )
            if operation.id not in {candidate.id for candidate in candidates}:
                failures.append(
                    f"{operation.id}/{case.name}: discovery did not retrieve the expected operation"
                )
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    test_count = sum(len(operation.definition.tests) for operation in snapshot.operations.values())
    selectable_count = sum(
        operation.enabled and operation.definition.discovery.model_selectable
        for operation in snapshot.operations.values()
    )
    step_count = sum(
        len(operation.definition.execution.steps)
        for operation in snapshot.operations.values()
    )
    print(
        f"passed: {test_count} journeys across {len(snapshot.operations)} operations; "
        f"{selectable_count} strict proposal tools; {step_count} typed workflow steps"
    )
    return 0


def _model_eval_catalog(
    operation_id: str | None,
    *,
    include_negative: bool,
) -> int:
    """Measure deployed-model selection against file-owned examples.

    This is intentionally separate from deterministic catalog tests: it costs
    model calls and may move when a model or prompt changes.  Operation files
    remain the single eval-case source, so Ops does not maintain a second
    dataset just to test the same capability contract.
    """
    from ..config import get_settings
    from ..services.agents import run_operator

    settings = get_settings()
    if not settings.openai_api_key:
        print("model_unavailable: OPENAI_API_KEY is required", file=sys.stderr)
        return 2
    snapshot = operation_catalog().load(initial=True)
    operations = [
        operation
        for operation in snapshot.operations.values()
        if operation.enabled
        and operation.definition.discovery.model_selectable
        and (operation_id is None or operation.id == operation_id)
    ]
    if operation_id is not None and not operations:
        print(f"unknown_or_unselectable_operation: {operation_id}", file=sys.stderr)
        return 2

    results: list[dict[str, object]] = []
    for operation in operations:
        for case in operation.definition.tests:
            if not case.expected_match and not include_negative:
                continue
            try:
                outcome = run_operator(
                    case.request,
                    [],
                    date.today(),
                    settings.default_timezone,
                    [],
                    model_id=settings.operator_model,
                )
                selected = (
                    outcome.operation.operation_id
                    if outcome is not None and outcome.operation is not None
                    else None
                )
                error = None
            except Exception as exc:
                selected = None
                error = f"{type(exc).__name__}: {exc}"
            passed = (
                selected == operation.id
                if case.expected_match
                else selected != operation.id
            ) and error is None
            results.append({
                "operationId": operation.id,
                "case": case.name,
                "request": case.request,
                "expectedSelection": operation.id if case.expected_match else f"not:{operation.id}",
                "actualSelection": selected,
                "passed": passed,
                "error": error,
            })
            print(json.dumps(results[-1], ensure_ascii=False, default=str))

    passed_count = sum(bool(item["passed"]) for item in results)
    print(
        json.dumps({
            "summary": {
                "model": settings.operator_model,
                "passed": passed_count,
                "failed": len(results) - passed_count,
                "total": len(results),
                "catalogHash": snapshot.catalog_hash,
            }
        })
    )
    return 0 if passed_count == len(results) else 1


def _primitives() -> int:
    print(json.dumps([
        {
            "reference": item.reference,
            "effect": item.effect,
            "confirmation": item.confirmation,
            "permissions": sorted(item.permissions),
            "inputSchema": item.input_model.model_json_schema(by_alias=True),
        }
        for item in primitive_catalog() if item.ops_authorable
    ], indent=2, default=str))
    return 0


def _schema() -> int:
    print(json.dumps(OperationDefinition.model_json_schema(by_alias=True), indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and inspect Fyn operation files")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "explain", "test"):
        child = subparsers.add_parser(command)
        child.add_argument("path", type=Path)
    subparsers.add_parser("primitives")
    subparsers.add_parser("schema")
    subparsers.add_parser("validate-catalog")
    subparsers.add_parser("test-catalog")
    model_eval = subparsers.add_parser("model-eval-catalog")
    model_eval.add_argument("--operation")
    model_eval.add_argument("--include-negative", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "validate":
            return _validate(args.path)
        if args.command == "explain":
            return _explain(args.path)
        if args.command == "test":
            return _test(args.path)
        if args.command == "primitives":
            return _primitives()
        if args.command == "validate-catalog":
            return _validate_catalog()
        if args.command == "test-catalog":
            return _test_catalog()
        if args.command == "model-eval-catalog":
            return _model_eval_catalog(
                args.operation,
                include_negative=args.include_negative,
            )
        return _schema()
    except OperationCatalogError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
