from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from ..config import get_settings
from ..operation_types import (
    CONFIRMATION_STRENGTH,
    EFFECT_STRENGTH,
    ConfirmationPolicy,
    DataEffect,
)
from .models import CatalogHealth, CommonInstructions, CompiledOperation, OperationDefinition
from .primitives import primitive, primitive_catalog


class OperationCatalogError(RuntimeError):
    def __init__(self, code: str, message: str, *, path: Path | None = None):
        self.code = code
        self.path = path
        prefix = f"{path}: " if path else ""
        super().__init__(prefix + message)


@dataclass(frozen=True)
class OperationCatalogSnapshot:
    generation: int
    catalog_hash: str
    common_instructions: tuple[str, ...]
    operations: Mapping[str, CompiledOperation]
    discovery_index: Mapping[str, tuple[str, ...]]
    loaded_at: datetime

    def operation(self, operation_id: str) -> CompiledOperation | None:
        item = self.operations.get(operation_id)
        return item if item and item.enabled else None

    @property
    def core_operations(self) -> tuple[CompiledOperation, ...]:
        return tuple(item for item in self.operations.values() if item.source == "core")

    @property
    def managed_operations(self) -> tuple[CompiledOperation, ...]:
        return tuple(item for item in self.operations.values() if item.source == "managed" and item.enabled)


_TOKEN = re.compile(r"[a-z0-9]+")
_SUPPORTED_TYPES = {"object", "array", "string", "integer", "number", "boolean"}
_SUPPORTED_SCHEMA_KEYS = {
    "$schema", "type", "title", "description", "properties", "required",
    "additionalProperties", "items", "enum", "default", "minLength", "maxLength",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minItems",
    "maxItems", "uniqueItems", "format", "pattern",
}
_SUPPORTED_FORMATS = {"date", "date-time", "money"}


def _tokens(value: str) -> set[str]:
    return set(_TOKEN.findall(value.casefold()))


def operation_match_score(operation: CompiledOperation, text: str) -> float:
    query = _tokens(text)
    discovery = operation.definition.discovery
    positive_documents = [discovery.description, *discovery.aliases, *discovery.examples]
    positive = max(
        (len(query & _tokens(document)) / max(1, len(query | _tokens(document))) for document in positive_documents),
        default=0.0,
    )
    negative = max(
        (len(query & _tokens(document)) / max(1, len(query | _tokens(document))) for document in discovery.negative_examples),
        default=0.0,
    )
    return positive - negative


def _build_discovery_index(operations: Mapping[str, CompiledOperation]) -> Mapping[str, tuple[str, ...]]:
    postings: dict[str, set[str]] = {}
    for operation in operations.values():
        discovery = operation.definition.discovery
        documents = (
            operation.definition.metadata.title,
            discovery.description,
            *discovery.aliases,
            *discovery.examples,
        )
        for token in _tokens(" ".join(documents)):
            postings.setdefault(token, set()).add(operation.id)
    return MappingProxyType({
        token: tuple(sorted(operation_ids))
        for token, operation_ids in postings.items()
    })


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_document(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            value = json.loads(text)
        else:
            documents = list(yaml.safe_load_all(text))
            if len(documents) != 1:
                raise OperationCatalogError("multiple_documents", "exactly one document is required", path=path)
            value = documents[0]
    except OperationCatalogError:
        raise
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise OperationCatalogError("parse_error", str(exc), path=path) from exc
    if not isinstance(value, dict):
        raise OperationCatalogError("invalid_document", "the document root must be an object", path=path)
    return value


def _canonical(definition: OperationDefinition) -> bytes:
    return json.dumps(
        definition.model_dump(mode="json", by_alias=True, exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_schema_subset(
    schema: object,
    *,
    path: str = "input.schema",
    depth: int = 0,
) -> None:
    if not isinstance(schema, dict):
        raise ValueError(f"{path} must be an object")
    if depth > 8:
        raise ValueError(f"{path} exceeds the maximum schema nesting depth")
    unknown = set(schema) - _SUPPORTED_SCHEMA_KEYS
    if unknown:
        raise ValueError(f"{path} uses unsupported schema keywords: {', '.join(sorted(unknown))}")
    schema_type = schema.get("type")
    if schema_type not in _SUPPORTED_TYPES:
        raise ValueError(f"{path}.type must be one of {', '.join(sorted(_SUPPORTED_TYPES))}")
    if schema.get("format") not in (None, *_SUPPORTED_FORMATS):
        raise ValueError(f"{path}.format is unsupported")
    if schema_type == "object":
        if schema.get("additionalProperties", False) is not False:
            raise ValueError(f"{path} must set additionalProperties=false")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError(f"{path}.properties must be an object")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(item not in properties for item in required):
            raise ValueError(f"{path}.required must name declared properties")
        for key, child in properties.items():
            _validate_schema_subset(
                child,
                path=f"{path}.properties.{key}",
                depth=depth + 1,
            )
    elif schema_type == "array":
        if "items" not in schema:
            raise ValueError(f"{path}.items is required")
        maximum = schema.get("maxItems")
        if not isinstance(maximum, int) or maximum < 1 or maximum > 100:
            raise ValueError(f"{path}.maxItems must bound the array between 1 and 100")
        child = schema["items"]
        if not isinstance(child, dict):
            raise ValueError(f"{path}.items must be a schema")
        _validate_schema_subset(child, path=f"{path}.items", depth=depth + 1)


def _validate_routing(definition: OperationDefinition, source: str) -> None:
    routing = definition.routing
    properties = _input_properties(definition)
    for target, reference in routing.bindings.items():
        if reference == "${input}":
            continue
        field = reference.removeprefix("${input.").removesuffix("}")
        if field not in properties:
            raise ValueError(f"Routing target {target} references unknown input {field}")
    if source == "managed" and routing.strategy != "managed":
        raise ValueError("Managed operations must use routing.strategy=managed")
    if source == "core" and routing.strategy == "managed":
        raise ValueError("Protected core operations cannot use routing.strategy=managed")
    if definition.discovery.model_selectable and routing.strategy == "protocol":
        raise ValueError("Protocol operations cannot be model-selectable")


def _input_properties(definition: OperationDefinition) -> set[str]:
    return set(definition.input.schema_.get("properties", {}))


def _validate_workflow(definition: OperationDefinition, source: str) -> tuple[DataEffect, ConfirmationPolicy, list[str]]:
    execution = definition.execution
    properties = _input_properties(definition)
    seen_steps: set[str] = set()
    step_outputs: dict[str, frozenset[str]] = {}
    effects: list[DataEffect] = []
    confirmations: list[ConfirmationPolicy] = []
    permissions: set[str] = set()
    for step in execution.steps:
        target = primitive(step.uses)
        if source == "managed" and not target.ops_authorable:
            raise ValueError(f"Primitive {step.uses} is not available to managed operations")
        effects.append(target.effect)
        confirmations.append(target.confirmation)
        permissions.update(target.permissions)
        model_fields = target.input_model.model_fields
        accepted_inputs = {
            key
            for name, field in model_fields.items()
            for key in (name, field.alias or name)
        }
        supplied = set(step.with_)
        unknown = supplied - accepted_inputs
        if unknown:
            raise ValueError(f"Step {step.id} supplies unknown primitive inputs: {', '.join(sorted(unknown))}")
        missing = {
            field.alias or name for name, field in model_fields.items()
            if field.is_required() and name not in supplied and (field.alias or name) not in supplied
        }
        if missing:
            raise ValueError(f"Step {step.id} is missing primitive inputs: {', '.join(sorted(missing))}")
        if step.for_each:
            field_name = step.for_each.removeprefix("${input.").removesuffix("}")
            field_schema = definition.input.schema_["properties"].get(field_name, {})
            if field_schema.get("type") != "array":
                raise ValueError(f"Step {step.id} forEach must reference an input array")
        for binding in step.with_.values():
            if not isinstance(binding, str) or not binding.startswith("${"):
                continue
            if binding.startswith("${input."):
                name = binding.removeprefix("${input.").removesuffix("}")
                if name not in properties:
                    raise ValueError(f"Step {step.id} references unknown input {name}")
            elif binding.startswith("${steps."):
                parts = binding.removeprefix("${steps.").removesuffix("}").split(".")
                producer = parts[0]
                if producer not in seen_steps:
                    raise ValueError(f"Step {step.id} references a step that has not completed: {producer}")
                if len(parts) != 3 or parts[1] != "output" or parts[2] not in step_outputs[producer]:
                    raise ValueError(f"Step {step.id} references an undeclared output from {producer}")
            elif binding == "${item}" and not step.for_each:
                raise ValueError(f"Step {step.id} may use item only with forEach")
        seen_steps.add(step.id)
        step_outputs[step.id] = target.output_fields if not step.for_each else frozenset()

    effect = max(effects, key=EFFECT_STRENGTH.__getitem__, default=DataEffect.NONE)
    primitive_confirmation = max(
        confirmations,
        key=CONFIRMATION_STRENGTH.__getitem__,
        default=ConfirmationPolicy.NEVER,
    )
    declared_confirmation = definition.approval.minimum
    confirmation = max(
        (primitive_confirmation, declared_confirmation),
        key=CONFIRMATION_STRENGTH.__getitem__,
    )
    if effect is not definition.eligibility.expected_effect:
        raise ValueError(
            f"expectedEffect={definition.eligibility.expected_effect} does not match derived effect={effect}"
        )
    if set(definition.eligibility.required_permissions) != permissions:
        raise ValueError(
            "requiredPermissions must exactly match primitive permissions: "
            + ", ".join(sorted(permissions))
        )
    return effect, confirmation, sorted(permissions)


def compile_operation(path: Path, source: str) -> CompiledOperation:
    try:
        definition = OperationDefinition.model_validate(_read_document(path))
        Draft202012Validator.check_schema(definition.input.schema_)
        _validate_schema_subset(definition.input.schema_)
        _validate_routing(definition, source)
        effect, confirmation, permissions = _validate_workflow(definition, source)
    except OperationCatalogError:
        raise
    except (ValidationError, ValueError) as exc:
        raise OperationCatalogError("validation_error", str(exc), path=path) from exc
    return CompiledOperation(
        definition=definition,
        checksum=hashlib.sha256(_canonical(definition)).hexdigest(),
        source=source,
        source_path=str(path),
        derived_effect=effect,
        derived_confirmation=confirmation,
        derived_permissions=permissions,
    )


def _operation_files(directory: Path) -> Iterable[Path]:
    if not directory.exists():
        return ()
    return sorted(
        path for path in directory.iterdir()
        if path.is_file() and path.suffix.casefold() in {".yaml", ".yml", ".json"}
    )


class OperationCatalogManager:
    def __init__(self, core_root: Path, managed_root: Path | None = None):
        self.core_root = core_root
        self.managed_root = managed_root
        self._lock = threading.RLock()
        self._snapshot: OperationCatalogSnapshot | None = None
        self._health = CatalogHealth()

    def load(self, *, initial: bool = False) -> OperationCatalogSnapshot:
        try:
            if self.managed_root is not None and not self.managed_root.is_dir():
                raise OperationCatalogError(
                    "managed_catalog_unavailable",
                    "The configured managed operation directory is unavailable",
                    path=self.managed_root,
                )
            common_path = self.core_root / "common.yaml"
            try:
                common = CommonInstructions.model_validate(_read_document(common_path))
            except (ValidationError, OperationCatalogError) as exc:
                raise OperationCatalogError("common_instructions_invalid", str(exc), path=common_path) from exc

            compiled: dict[str, CompiledOperation] = {}
            core_directory = self.core_root / "core"
            core_files = tuple(_operation_files(core_directory))
            if not core_files:
                raise OperationCatalogError("core_catalog_empty", "No protected core operations were found", path=core_directory)
            for source, files in (
                ("core", core_files),
                ("managed", tuple(_operation_files(self.managed_root)) if self.managed_root else ()),
            ):
                for path in files:
                    operation = compile_operation(path, source)
                    existing = compiled.get(operation.id)
                    if existing:
                        if source == "managed" and existing.source == "core":
                            raise OperationCatalogError("protected_operation_override", f"Managed file cannot override {operation.id}", path=path)
                        raise OperationCatalogError("duplicate_operation", f"Duplicate operation id {operation.id}", path=path)
                    compiled[operation.id] = operation

            core_operations = tuple(
                operation for operation in compiled.values()
                if operation.source == "core"
            )
            protected_references = {
                item.reference for item in primitive_catalog()
                if not item.ops_authorable
            }
            declared_references = {
                step.uses
                for operation in core_operations
                for step in operation.definition.execution.steps
            }
            if declared_references != protected_references:
                missing = protected_references - declared_references
                unknown = declared_references - protected_references
                raise OperationCatalogError(
                    "protected_primitive_wiring_incomplete",
                    "Protected operation wiring must cover the trusted primitive catalog exactly; "
                    f"missing={sorted(missing)}, unknown={sorted(unknown)}",
                    path=core_directory,
                )
            metrics = [
                operation.definition.execution.metric
                for operation in core_operations
                if operation.definition.execution.metric is not None
            ]
            if len(metrics) != len(set(metrics)):
                raise OperationCatalogError(
                    "duplicate_core_metric",
                    "Protected operation metrics must be unique",
                    path=core_directory,
                )
            with self._lock:
                previous = self._snapshot
            if previous is not None:
                previous_core_ids = {item.id for item in previous.core_operations}
                next_core_ids = {item.id for item in core_operations}
                if previous_core_ids != next_core_ids:
                    raise OperationCatalogError(
                        "core_topology_requires_restart",
                        "Adding, removing, or renaming a protected capability requires a process restart; "
                        "managed operation files remain fully hot-reloadable",
                        path=core_directory,
                    )

            active_material = [
                f"common:{hashlib.sha256(json.dumps(common.model_dump(mode='json', by_alias=True), sort_keys=True).encode()).hexdigest()}"
            ]
            active_material.extend(f"{key}:{item.version}:{item.checksum}" for key, item in sorted(compiled.items()))
            catalog_hash = hashlib.sha256("\n".join(active_material).encode()).hexdigest()
            with self._lock:
                generation = (self._snapshot.generation if self._snapshot else 0) + 1
                snapshot = OperationCatalogSnapshot(
                    generation=generation,
                    catalog_hash=catalog_hash,
                    common_instructions=tuple(common.instructions),
                    operations=MappingProxyType(dict(compiled)),
                    discovery_index=_build_discovery_index(compiled),
                    loaded_at=_now(),
                )
                self._snapshot = snapshot
                self._health = CatalogHealth(
                    status="ok",
                    catalogHash=catalog_hash,
                    generation=generation,
                    coreCount=sum(item.source == "core" for item in compiled.values()),
                    managedCount=sum(item.source == "managed" for item in compiled.values()),
                    lastLoadedAt=snapshot.loaded_at.isoformat(),
                )
                return snapshot
        except Exception as exc:
            error = exc if isinstance(exc, OperationCatalogError) else OperationCatalogError("catalog_load_failed", str(exc))
            with self._lock:
                previous = self._health
                self._health = CatalogHealth(
                    status="degraded" if self._snapshot else "uninitialized",
                    catalogHash=previous.catalog_hash,
                    generation=previous.generation,
                    coreCount=previous.core_count,
                    managedCount=previous.managed_count,
                    lastLoadedAt=previous.last_loaded_at,
                    lastErrorAt=_now().isoformat(),
                    lastErrorCode=error.code,
                )
            if initial or self._snapshot is None:
                raise error
            return self._snapshot

    def snapshot(self) -> OperationCatalogSnapshot:
        with self._lock:
            snapshot = self._snapshot
        return snapshot or self.load(initial=True)

    def health(self) -> CatalogHealth:
        with self._lock:
            return self._health.model_copy(deep=True)

    def candidates(self, text: str, *, limit: int = 12, managed_only: bool = True) -> list[tuple[CompiledOperation, float]]:
        snapshot = self.snapshot()
        candidate_ids = {
            operation_id
            for token in _tokens(text)
            for operation_id in snapshot.discovery_index.get(token, ())
        }
        ranked: list[tuple[CompiledOperation, float]] = []
        for operation_id in candidate_ids:
            operation = snapshot.operations[operation_id]
            if (
                not operation.enabled
                or not operation.definition.discovery.model_selectable
                or (managed_only and operation.source != "managed")
            ):
                continue
            score = operation_match_score(operation, text)
            if score > 0:
                ranked.append((operation, score))
        return sorted(ranked, key=lambda item: (-item[1], item[0].id))[:limit]

    def candidate_operations(
        self,
        text: str,
        *,
        limit: int = 12,
        managed_only: bool = True,
    ) -> tuple[CompiledOperation, ...]:
        eligible = tuple(
            operation
            for operation in self.snapshot().operations.values()
            if (
                operation.enabled
                and operation.definition.discovery.model_selectable
                and (not managed_only or operation.source == "managed")
            )
        )
        # For a bounded catalog, expose every eligible definition and let the
        # model compare complete descriptions and schemas. Requiring a shared
        # token here would turn model discovery into a hidden verb dictionary.
        if len(eligible) <= limit:
            return tuple(sorted(
                eligible,
                key=lambda operation: (-operation_match_score(operation, text), operation.id),
            ))
        return tuple(
            operation
            for operation, _score in self.candidates(
                text,
                limit=limit,
                managed_only=managed_only,
            )
        )

    def prompt_manifest(self, text: str, *, limit: int = 12) -> list[dict]:
        return [
            {
                "id": item.id,
                "version": item.version,
                "description": item.definition.discovery.description,
                "instructions": item.definition.instructions.selection,
                "inputSchema": item.definition.input.schema_,
                "effect": item.derived_effect,
                "confirmation": item.derived_confirmation,
            }
            for item, _score in self.candidates(text, limit=limit)
        ]


_catalog: OperationCatalogManager | None = None
_catalog_lock = threading.Lock()


def operation_catalog() -> OperationCatalogManager:
    global _catalog
    if _catalog is None:
        with _catalog_lock:
            if _catalog is None:
                settings = get_settings()
                _catalog = OperationCatalogManager(
                    Path(settings.operations_core_dir),
                    Path(settings.operations_managed_dir) if settings.operations_managed_dir else None,
                )
    return _catalog
