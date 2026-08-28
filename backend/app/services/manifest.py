"""Source manifests: generated semantic descriptions of connected data sources.

Phase 1 of the analyst platform blueprint. Every queryable origin gets a
DataSource row and versioned SourceManifest documents produced by a
deterministic scan, so adding a source means generating a manifest, not
authoring Python. The native canonical ledger is the first governed source:
its curated semantics still come from the in-code registry declaration, while
the scanner contributes the profiled physical layer and per-user value
catalogs (cell-level context). Future sources reuse exactly this pipeline
with user annotations as the curated tier.

Provenance rules: ``curated`` and ``profiled`` sections are product/machine
authored and refresh on every scan; ``user_stated`` annotations (later phase)
are authoritative and must survive re-scans. Value catalogs are derived from
one user's rows and are therefore user-scoped context — they are never stored
in the shared manifest document.
"""
from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Account, DataSource, SourceManifest, Transaction
from .semantic_registry import MODEL_BINDINGS, semantic_schema_registry
from .transactions import apply_canonical_transaction_scope

NATIVE_SOURCE_KIND = "native_ledger"
NATIVE_SOURCE_NAME = "Canonical financial ledger"
# Declared beside the manifest machinery so both annotation (spreadsheet.py)
# and connection (external_db.py) code share one spelling without a cycle.
EXTERNAL_SOURCE_KIND = "external_db"

# Semantic types whose values form a bounded vocabulary worth showing a model.
# Identifier, money, date and free-text types never become value catalogs, and
# sensitive fields are excluded before this set is even consulted.
CATALOG_SEMANTIC_TYPES = frozenset({"category", "merchant", "account", "status", "enum", "currency"})

# Value catalogs are prompt context, so they are capped, not complete. The
# truncation is recorded in the catalog itself; silent truncation would read
# as "these are all the values" when they are not.
MAX_CATALOG_VALUES = 24
MAX_MERCHANT_VALUES = 30

# Phase 1 catalogs cover the entities users filter by value. Taxonomy
# categories already reach the planner through the taxonomy prompt.
CATALOG_ENTITIES = ("transactions", "accounts")


def scan_native_schema() -> dict[str, Any]:
    """Profile the physical layer behind every governed entity.

    Deterministic and value-free: table names, columns, types, nullability,
    and which columns the governed contract binds. This is the section a
    future non-native source gets from its own scanner instead.
    """
    registry = semantic_schema_registry()
    entities: dict[str, Any] = {}
    for entity in registry.entities:
        model = MODEL_BINDINGS[entity.name]
        # One physical column may carry several governed fields (for example
        # transaction_at serves both transaction_date and transaction_time).
        governed_columns: dict[str, list[str]] = {}
        for field in entity.fields:
            governed_columns.setdefault(field.column, []).append(field.name)
        entities[entity.name] = {
            "table": entity.table,
            "columns": [
                {
                    "name": column.name,
                    "type": str(column.type),
                    "nullable": bool(column.nullable),
                    "governed_fields": governed_columns.get(column.name, []),
                }
                for column in model.__table__.columns
            ],
            "catalog_fields": [
                field.name
                for field in entity.fields
                if field.semantic_type in CATALOG_SEMANTIC_TYPES and not field.sensitive
            ],
        }
    return entities


def manifest_fingerprint(document: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def native_manifest_document() -> dict[str, Any]:
    """The native ledger's manifest: curated semantics plus profiled physical.

    The registry remains the curated author for this one source; the manifest
    is the artifact consumers key on, so a registry change lands here as a new
    version with a new hash.
    """
    registry = semantic_schema_registry()
    return {
        "kind": NATIVE_SOURCE_KIND,
        "name": NATIVE_SOURCE_NAME,
        "semantics": {"provenance": "curated", **registry.prompt_contract()},
        "physical": {"provenance": "profiled", "entities": scan_native_schema()},
    }


@lru_cache(maxsize=1)
def native_manifest_fingerprint() -> str:
    """The identity consumers key on for the native source.

    Pure function of the cached registry plus the deterministic scan, so it is
    process-stable and always equals the hash of the manifest row that startup
    posts. Templates and retrieval compare against this, not the raw registry
    hash, so a physical schema change invalidates them too.
    """
    return manifest_fingerprint(native_manifest_document())


def active_manifest(db: Session, source: DataSource) -> SourceManifest | None:
    return db.scalar(
        select(SourceManifest)
        .where(SourceManifest.data_source_id == source.id, SourceManifest.status == "active")
        .order_by(SourceManifest.version.desc())
        .limit(1)
    )


class ManifestVersionConflict(RuntimeError):
    """A concurrent writer claimed this source's next version first.

    The whole losing transaction has been rolled back — including any rows
    the caller flushed alongside the manifest — so the caller must redo its
    work against fresh state, not report success.
    """


def post_manifest_version(
    db: Session,
    source: DataSource,
    document: dict[str, Any],
    *,
    adopt_winner_on_conflict: bool = False,
) -> SourceManifest:
    """Idempotently persist ``document`` as the source's active manifest.

    Unchanged content is a no-op; changed content supersedes the active
    version and posts the next one. On a concurrent-writer conflict the
    losing transaction is rolled back and ManifestVersionConflict raised —
    except for callers whose document is derived state identical for every
    writer (the native startup path), which may adopt the winner's row.
    Every source — native or user-uploaded — versions through this one path.
    """
    digest = manifest_fingerprint(document)
    current = active_manifest(db, source)
    if current is not None and current.manifest_hash == digest:
        return current

    if current is not None:
        current.status = "superseded"
    latest_version = db.scalar(
        select(func.coalesce(func.max(SourceManifest.version), 0)).where(
            SourceManifest.data_source_id == source.id
        )
    )
    manifest = SourceManifest(
        data_source_id=source.id,
        version=int(latest_version or 0) + 1,
        status="active",
        manifest_hash=digest,
        document=document,
    )
    db.add(manifest)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        winner = active_manifest(db, source)
        if adopt_winner_on_conflict and winner is not None:
            return winner
        raise ManifestVersionConflict(
            f"Source {source.id} version {manifest.version} was claimed concurrently"
        ) from error
    return manifest


def ensure_native_manifest(db: Session) -> SourceManifest:
    """Idempotently persist the current native manifest. Runs at startup."""
    source = db.scalar(
        select(DataSource).where(DataSource.kind == NATIVE_SOURCE_KIND, DataSource.user_id.is_(None))
    )
    if source is None:
        source = DataSource(kind=NATIVE_SOURCE_KIND, name=NATIVE_SOURCE_NAME, user_id=None)
        db.add(source)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            source = db.scalar(
                select(DataSource).where(DataSource.kind == NATIVE_SOURCE_KIND, DataSource.user_id.is_(None))
            )
            if source is None:
                raise

    return post_manifest_version(
        db, source, native_manifest_document(), adopt_winner_on_conflict=True
    )


def _catalog_entry(rows: Sequence[Row[Any]], distinct: int, cap: int) -> dict[str, Any]:
    return {
        "values": [str(value) for value, _ in rows[:cap]],
        "distinct": distinct,
        "truncated": distinct > cap,
    }


def user_value_catalog(
    db: Session,
    user_id: UUID,
    entity_names: Sequence[str] = CATALOG_ENTITIES,
) -> dict[str, Any]:
    """Cell-level context: the values actually present in one user's data.

    Only governed, non-sensitive categorical fields qualify, ordered by
    frequency so a cap keeps the most useful values. Everything is computed
    inside the canonical tenant scope; nothing here may ever be shared across
    users or written into a manifest document.
    """
    registry = semantic_schema_registry()
    catalog: dict[str, Any] = {}
    requested_entities = set(entity_names)
    for entity_name in CATALOG_ENTITIES:
        if entity_name not in requested_entities:
            continue
        entity = registry.entities_by_name[entity_name]
        model = MODEL_BINDINGS[entity_name]
        fields: dict[str, Any] = {}
        for field in entity.fields:
            if field.semantic_type not in CATALOG_SEMANTIC_TYPES or field.sensitive:
                continue
            column = getattr(model, field.column)
            cap = MAX_MERCHANT_VALUES if field.semantic_type == "merchant" else MAX_CATALOG_VALUES
            grouped = select(column, func.count().label("uses")).where(column.is_not(None))
            distinct_stmt = select(func.count(func.distinct(column))).where(column.is_not(None))
            if model is Transaction:
                grouped = apply_canonical_transaction_scope(grouped, user_id)
                distinct_stmt = apply_canonical_transaction_scope(distinct_stmt, user_id)
            elif model is Account:
                grouped = grouped.where(Account.user_id == user_id)
                distinct_stmt = distinct_stmt.where(Account.user_id == user_id)
            else:  # pragma: no cover - CATALOG_ENTITIES is a closed set above
                continue
            rows = list(
                db.execute(
                    grouped.group_by(column).order_by(func.count().desc(), column).limit(cap)
                )
            )
            if not rows:
                continue
            distinct = int(db.scalar(distinct_stmt) or 0)
            fields[field.name] = _catalog_entry(rows, distinct, cap)
        if fields:
            catalog[entity_name] = fields
    return catalog
