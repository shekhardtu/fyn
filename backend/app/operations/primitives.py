from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..models import User
from ..operation_types import ConfirmationPolicy, DataEffect
from ..services.taxonomy import TaxonomyRepository


class PrimitiveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EmptyPrimitiveInput(PrimitiveInput):
    pass


class CreateCategoryInput(PrimitiveInput):
    name: str = Field(min_length=1, max_length=80)


class CreateSubcategoryInput(PrimitiveInput):
    parent_id: UUID = Field(alias="parentId")
    name: str = Field(min_length=1, max_length=80)


class AnnotateSourceColumnInput(PrimitiveInput):
    source_id: UUID = Field(alias="sourceId")
    field: str = Field(min_length=1, max_length=120)
    statement: str = Field(min_length=1, max_length=2000)
    role: str | None = Field(default=None, max_length=40)


PrimitiveCallable = Callable[[Session, User, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class PrimitiveDefinition:
    id: str
    version: int
    input_model: type[BaseModel]
    effect: DataEffect
    confirmation: ConfirmationPolicy
    permissions: frozenset[str]
    ops_authorable: bool
    transactional: bool
    idempotent: bool
    output_fields: frozenset[str]
    execute: PrimitiveCallable | None
    runtime_method: str | None = None

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.version}"


def _create_category(db: Session, user: User, values: dict[str, Any]) -> dict[str, Any]:
    request = CreateCategoryInput.model_validate(values)
    repository = TaxonomyRepository(db, user.id)
    existing = next(
        (item for item in repository.expense_categories() if item.name.casefold() == request.name.casefold()),
        None,
    )
    created = existing is None
    category = existing or repository.create_category(
        request.name.strip(), "circle-ellipsis", f"custom-{uuid4().hex}"
    )
    if created and not any(item.name.casefold() == "other" for item in repository.subcategories(category.id)):
        repository.create_subcategory(category, "Other", "other")
    return {"id": str(category.id), "name": category.name, "created": created}


def _create_subcategory(db: Session, user: User, values: dict[str, Any]) -> dict[str, Any]:
    request = CreateSubcategoryInput.model_validate(values)
    repository = TaxonomyRepository(db, user.id)
    category = repository.category(request.parent_id, expense_only=True)
    if not category:
        raise ValueError("Unknown or inaccessible parent category")
    existing = next(
        (item for item in repository.subcategories(category.id) if item.name.casefold() == request.name.casefold()),
        None,
    )
    created = existing is None
    subcategory = existing or repository.create_subcategory(
        category, request.name.strip(), f"custom-{uuid4().hex}"
    )
    return {
        "id": str(subcategory.id),
        "parentId": str(category.id),
        "name": subcategory.name,
        "created": created,
    }


def _annotate_source_column(db: Session, user: User, values: dict[str, Any]) -> dict[str, Any]:
    from ..services.spreadsheet import annotate_source_field

    request = AnnotateSourceColumnInput.model_validate(values)
    manifest = annotate_source_field(
        db, user, request.source_id, request.field, request.statement, role=request.role
    )
    return {
        "sourceId": str(request.source_id),
        "field": request.field,
        "manifestVersion": manifest.version,
    }


PRIMITIVES: tuple[PrimitiveDefinition, ...] = (
    # Protected runtime primitives are the only bridge from declarative core
    # operations to trusted application services. They are deliberately not
    # Ops-authorable; the conversation engine injects their authenticated
    # runtime context and invokes them through the operation workflow runner.
    PrimitiveDefinition("agent.respond", 1, EmptyPrimitiveInput, DataEffect.NONE, ConfirmationPolicy.NEVER, frozenset(), False, True, True, frozenset(), None, "respond"),
    PrimitiveDefinition("agent.clarify", 1, EmptyPrimitiveInput, DataEffect.DRAFT, ConfirmationPolicy.NEVER, frozenset(), False, True, True, frozenset(), None, "clarify"),
    PrimitiveDefinition("agent.unknown", 1, EmptyPrimitiveInput, DataEffect.NONE, ConfirmationPolicy.NEVER, frozenset(), False, True, True, frozenset(), None, "unknown"),
    PrimitiveDefinition("transaction.record", 1, EmptyPrimitiveInput, DataEffect.MUTATION, ConfirmationPolicy.CONDITIONAL, frozenset({"transactions.write"}), False, True, True, frozenset(), None, "record_transaction"),
    PrimitiveDefinition("transaction.find_removal", 1, EmptyPrimitiveInput, DataEffect.MUTATION, ConfirmationPolicy.REQUIRED, frozenset({"transactions.write"}), False, True, True, frozenset(), None, "remove_transaction"),
    PrimitiveDefinition("taxonomy.change", 1, EmptyPrimitiveInput, DataEffect.MUTATION, ConfirmationPolicy.REQUIRED, frozenset({"taxonomy.write"}), False, True, True, frozenset(), None, "change_taxonomy"),
    PrimitiveDefinition("planning.run", 1, EmptyPrimitiveInput, DataEffect.MUTATION, ConfirmationPolicy.REQUIRED, frozenset({"planning.write"}), False, True, True, frozenset(), None, "run_planning"),
    PrimitiveDefinition("finance.query", 1, EmptyPrimitiveInput, DataEffect.NONE, ConfirmationPolicy.NEVER, frozenset({"transactions.read"}), False, True, True, frozenset(), None, "run_query"),
    PrimitiveDefinition("transaction.search", 1, EmptyPrimitiveInput, DataEffect.NONE, ConfirmationPolicy.NEVER, frozenset({"transactions.read"}), False, True, True, frozenset(), None, "search_transactions"),
    PrimitiveDefinition("calculator.loan", 1, EmptyPrimitiveInput, DataEffect.NONE, ConfirmationPolicy.NEVER, frozenset(), False, True, True, frozenset(), None, "run_query"),
    PrimitiveDefinition("calculator.investment", 1, EmptyPrimitiveInput, DataEffect.NONE, ConfirmationPolicy.NEVER, frozenset(), False, True, True, frozenset(), None, "run_query"),
    PrimitiveDefinition("analysis.run", 1, EmptyPrimitiveInput, DataEffect.NONE, ConfirmationPolicy.NEVER, frozenset({"transactions.read"}), False, True, True, frozenset(), None, "run_analysis"),
    PrimitiveDefinition("analysis.bundle", 1, EmptyPrimitiveInput, DataEffect.NONE, ConfirmationPolicy.NEVER, frozenset({"transactions.read"}), False, True, True, frozenset(), None, "run_query_bundle"),
    PrimitiveDefinition("managed.dispatch", 1, EmptyPrimitiveInput, DataEffect.MUTATION, ConfirmationPolicy.REQUIRED, frozenset(), False, True, True, frozenset(), None, "run_managed_operation"),
    PrimitiveDefinition(
        id="taxonomy.create_category",
        version=1,
        input_model=CreateCategoryInput,
        effect=DataEffect.MUTATION,
        confirmation=ConfirmationPolicy.REQUIRED,
        permissions=frozenset({"taxonomy.write"}),
        ops_authorable=True,
        transactional=True,
        idempotent=True,
        output_fields=frozenset({"id", "name", "created"}),
        execute=_create_category,
    ),
    PrimitiveDefinition(
        id="manifest.annotate_column",
        version=1,
        input_model=AnnotateSourceColumnInput,
        effect=DataEffect.MUTATION,
        confirmation=ConfirmationPolicy.REQUIRED,
        permissions=frozenset({"sources.write"}),
        ops_authorable=True,
        transactional=True,
        idempotent=True,
        output_fields=frozenset({"sourceId", "field", "manifestVersion"}),
        execute=_annotate_source_column,
    ),
    PrimitiveDefinition(
        id="taxonomy.create_subcategory",
        version=1,
        input_model=CreateSubcategoryInput,
        effect=DataEffect.MUTATION,
        confirmation=ConfirmationPolicy.REQUIRED,
        permissions=frozenset({"taxonomy.write"}),
        ops_authorable=True,
        transactional=True,
        idempotent=True,
        output_fields=frozenset({"id", "parentId", "name", "created"}),
        execute=_create_subcategory,
    ),
)

_BY_REFERENCE = {item.reference: item for item in PRIMITIVES}

if any(not item.ops_authorable and not item.runtime_method for item in PRIMITIVES):
    raise RuntimeError("Every protected primitive requires an engine runtime method")
if any(item.ops_authorable and item.execute is None for item in PRIMITIVES):
    raise RuntimeError("Every Ops-authorable primitive requires a trusted executor")


def primitive(reference: str) -> PrimitiveDefinition:
    try:
        return _BY_REFERENCE[reference]
    except KeyError as exc:
        raise ValueError(f"Unknown governed primitive: {reference}") from exc


def primitive_catalog() -> tuple[PrimitiveDefinition, ...]:
    return PRIMITIVES
