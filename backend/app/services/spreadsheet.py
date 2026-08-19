"""Uploaded spreadsheet data sources: profile, manifest, annotate, query.

Phase 3 of the analyst platform blueprint. A spreadsheet becomes a governed
source through the same manifest machinery as the native ledger: a
deterministic profiler scans structure and cell-level value catalogs, a
heuristic pass (pre-filled by known export fingerprints) drafts column
semantics with ``inferred`` provenance, and the user's ``user_stated``
annotations are stored as rows that survive every re-scan and win over
inference. Each upload replaces the source's raw rows wholesale and posts a
new manifest version through ``post_manifest_version``; unchanged content is
a no-op.

Querying is deterministic and tenant-scoped: no model-authored code touches
these rows. The agent reaches uploads through ``query_uploaded_source``, a
strict tool returning typed error payloads the model can correct against.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import DataSource, SourceAnnotation, SourceManifest, SourceRecord, User
from .agent_tools import bind_schema_tool
from .analysis_sandbox import record_dataset
from .manifest import (
    EXTERNAL_SOURCE_KIND,
    MAX_CATALOG_VALUES,
    ManifestVersionConflict,
    active_manifest,
    post_manifest_version,
)

SPREADSHEET_SOURCE_KIND = "spreadsheet"
# Source kinds whose manifests accept user_stated field annotations. External
# fields are addressed as "table.column"; spreadsheet fields as bare headers.
ANNOTATABLE_SOURCE_KINDS = (SPREADSHEET_SOURCE_KIND, EXTERNAL_SOURCE_KIND)
MAX_UPLOAD_ROWS = 5000
MAX_UPLOAD_COLUMNS = 60
QUERY_ROW_CAP = 250

_MONEY_HEADER = re.compile(r"amount|amt|debit|credit|value|total|balance|price", re.I)
_MERCHANT_HEADER = re.compile(r"merchant|payee|party|vendor|paid to", re.I)
_DESCRIPTION_HEADER = re.compile(r"description|narration|details|memo|remarks", re.I)
_CATEGORY_HEADER = re.compile(r"category|head|type of expense", re.I)
_ACCOUNT_HEADER = re.compile(r"account|bank|card", re.I)
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d %b %Y")

# The structured roles a user may state for a column. The free-text statement
# always accompanies the role; the role is the half deterministic code obeys.
ANNOTATABLE_ROLES = frozenset({
    "money", "date", "merchant", "category", "account", "description", "number", "text",
})

# Known export shapes pre-fill column roles with higher confidence than bare
# header heuristics. Matching is by normalized header subset coverage.
EXPORT_FINGERPRINTS: tuple[dict[str, Any], ...] = (
    {
        "name": "generic_bank_statement",
        "roles": {
            "date": "date", "description": "description", "debit": "money",
            "credit": "money", "balance": "money",
        },
    },
    {
        "name": "generic_bookkeeping",
        "roles": {
            "date": "date", "account": "account", "amount": "money",
            "narration": "description", "category": "category",
        },
    },
)


def _normalize_header(header: str) -> str:
    return re.sub(r"\s+", " ", header.strip().casefold())


def _cell_type(value: str) -> str:
    text = value.strip()
    if not text:
        return "null"
    try:
        int(text.replace(",", ""))
        return "integer"
    except ValueError:
        pass
    try:
        Decimal(text.replace(",", ""))
        return "decimal"
    except InvalidOperation:
        pass
    for fmt in _DATE_FORMATS:
        try:
            datetime.strptime(text, fmt)
            return "date"
        except ValueError:
            continue
    return "string"


def profile_rows(headers: list[str], rows: list[list[str]]) -> dict[str, Any]:
    """Deterministic structural + cell-level profile of a tabular upload."""
    if not headers:
        raise ValueError("upload_has_no_headers")
    normalized = [_normalize_header(item) for item in headers]
    duplicates = [name for name, count in Counter(normalized).items() if count > 1]
    if duplicates:
        raise ValueError(f"duplicate_headers: {sorted(duplicates)}")
    if len(headers) > MAX_UPLOAD_COLUMNS:
        raise ValueError("too_many_columns")
    if len(rows) > MAX_UPLOAD_ROWS:
        raise ValueError("too_many_rows")

    columns: list[dict[str, Any]] = []
    for index, header in enumerate(headers):
        values = [str(row[index]) if index < len(row) and row[index] is not None else "" for row in rows]
        types = Counter(_cell_type(value) for value in values)
        null_count = types.pop("null", 0)
        inferred = types.most_common(1)[0][0] if types else "string"
        # Integer columns with any decimal cells are decimal overall.
        if inferred == "integer" and types.get("decimal"):
            inferred = "decimal"
        present = [value.strip() for value in values if value.strip()]
        frequency = Counter(present)
        distinct = len(frequency)
        catalog = None
        if 0 < distinct <= MAX_CATALOG_VALUES and inferred == "string":
            catalog = {
                "values": [value for value, _ in frequency.most_common(MAX_CATALOG_VALUES)],
                "distinct": distinct,
                "truncated": False,
            }
        columns.append({
            "name": header,
            "normalized": _normalize_header(header),
            "type": inferred,
            "null_count": null_count,
            "distinct": distinct,
            "catalog": catalog,
            "money_hint": bool(_MONEY_HEADER.search(header)) and inferred in {"integer", "decimal"},
        })
    return {"columns": columns, "row_count": len(rows)}


def match_fingerprint(headers: list[str]) -> dict[str, Any] | None:
    normalized = {_normalize_header(item) for item in headers}
    best, best_score = None, 0.0
    for fingerprint in EXPORT_FINGERPRINTS:
        expected = set(fingerprint["roles"])
        score = len(expected & normalized) / len(expected)
        if score > best_score:
            best, best_score = fingerprint, score
    return best if best is not None and best_score >= 0.6 else None


def semantic_draft(profile: dict[str, Any], fingerprint: dict[str, Any] | None) -> dict[str, Any]:
    """Heuristic column roles with ``inferred`` provenance."""
    roles = (fingerprint or {}).get("roles", {})
    columns: dict[str, Any] = {}
    for column in profile["columns"]:
        header, normalized = column["name"], column["normalized"]
        if normalized in roles:
            role, confidence = roles[normalized], 0.9
        elif column["money_hint"]:
            role, confidence = "money", 0.8
        elif column["type"] == "date":
            role, confidence = "date", 0.7
        elif _MERCHANT_HEADER.search(header):
            role, confidence = "merchant", 0.6
        elif _DESCRIPTION_HEADER.search(header):
            role, confidence = "description", 0.6
        elif _CATEGORY_HEADER.search(header):
            role, confidence = "category", 0.6
        elif _ACCOUNT_HEADER.search(header):
            role, confidence = "account", 0.6
        elif column["type"] in {"integer", "decimal"}:
            role, confidence = "number", 0.4
        else:
            role, confidence = "text", 0.4
        columns[header] = {"role": role, "confidence": confidence}
    return columns


def _annotations_section(db: Session, source: DataSource) -> dict[str, dict[str, Any]]:
    rows = db.scalars(
        select(SourceAnnotation)
        .where(SourceAnnotation.data_source_id == source.id)
        .order_by(SourceAnnotation.field)
    )
    return {row.field: {"statement": row.statement, "role": row.role} for row in rows}


def _content_hash(headers: list[str], rows: list[list[str]]) -> str:
    """Identity over the actual cells, not the profile.

    The profile is lossy (a cell edit can leave types, counts, and catalogs
    unchanged), so the manifest digest must include the content itself —
    otherwise a changed re-upload could be judged 'unchanged' and its row
    replacement silently rolled back with the transaction.
    """
    payload = json.dumps([headers, rows], sort_keys=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _build_document(db: Session, source: DataSource, headers: list[str], rows: list[list[str]]) -> dict[str, Any]:
    profile = profile_rows(headers, rows)
    draft = semantic_draft(profile, match_fingerprint(headers))
    return {
        "kind": SPREADSHEET_SOURCE_KIND,
        "name": source.name,
        "physical": {"provenance": "profiled", "content_hash": _content_hash(headers, rows), **profile},
        "semantics": {"provenance": "inferred", "columns": draft},
        "annotations": {"provenance": "user_stated", "fields": _annotations_section(db, source)},
    }


def stated_field_role(document: dict[str, Any], field: str) -> str | None:
    """The user's stated role for a field, or None. Shared across source kinds."""
    stated = document.get("annotations", {}).get("fields", {}).get(field, {})
    if isinstance(stated, dict) and stated.get("role"):
        return stated["role"]
    return None


def _column_role(manifest: SourceManifest, field: str) -> str:
    """user_stated beats inferred — the provenance law, applied where it counts."""
    stated = stated_field_role(manifest.document, field)
    if stated is not None:
        return stated
    return manifest.document["semantics"]["columns"].get(field, {}).get("role", "text")


def known_manifest_fields(document: dict[str, Any]) -> set[str]:
    """The annotatable field names of a manifest, across both source shapes.

    Spreadsheets profile a flat column list; external databases profile a table
    map whose fields are addressed as "table.column".
    """
    physical = document["physical"]
    if "tables" in physical:
        return {
            f"{table_name}.{column['name']}"
            for table_name, section in physical["tables"].items()
            for column in section["columns"]
        }
    return {column["name"] for column in physical["columns"]}


def ensure_spreadsheet_manifest(
    db: Session, user: User, source_name: str, headers: list[str], rows: list[list[str]]
) -> tuple[DataSource, SourceManifest]:
    """Store an upload's rows and post its manifest version.

    Re-uploading the same name replaces the row set and bumps the manifest
    version when content changed; identical content is a no-op. The
    ``user_stated`` annotations section is loaded from stored rows, so it
    survives every re-upload by construction.
    """
    profile_rows(headers, rows)  # validate caps/headers before any write
    for attempt in (1, 2):
        source = db.scalar(select(DataSource).where(
            DataSource.user_id == user.id,
            DataSource.kind == SPREADSHEET_SOURCE_KIND,
            DataSource.name == source_name,
        ))
        if source is None:
            source = DataSource(user_id=user.id, kind=SPREADSHEET_SOURCE_KIND, name=source_name)
            db.add(source)
            db.flush()
        db.execute(delete(SourceRecord).where(SourceRecord.data_source_id == source.id))
        for index, row in enumerate(rows):
            record = {header: (str(row[i]).strip() if i < len(row) and row[i] is not None else "") for i, header in enumerate(headers)}
            db.add(SourceRecord(user_id=user.id, data_source_id=source.id, row_index=index, record=record))
        db.flush()
        try:
            manifest = post_manifest_version(db, source, _build_document(db, source, headers, rows))
        except ManifestVersionConflict:
            # The rollback also undid the row replacement; redo everything
            # against the winner's state exactly once, then fail loudly.
            if attempt == 2:
                raise
            continue
        return source, manifest


def _owned_source(
    db: Session, user_id: UUID, data_source_id: UUID, kinds: tuple[str, ...]
) -> DataSource | None:
    """The source IS the tenant boundary: every lookup binds owner and kind."""
    return db.scalar(select(DataSource).where(
        DataSource.id == data_source_id,
        DataSource.user_id == user_id,
        DataSource.kind.in_(kinds),
        DataSource.status == "active",
    ))


def _owned_spreadsheet(db: Session, user_id: UUID, data_source_id: UUID) -> DataSource | None:
    return _owned_source(db, user_id, data_source_id, (SPREADSHEET_SOURCE_KIND,))


def _stored_table(db: Session, source: DataSource, manifest: SourceManifest) -> tuple[list[str], list[list[str]]]:
    headers = [column["name"] for column in manifest.document["physical"]["columns"]]
    records = db.scalars(
        select(SourceRecord)
        .where(SourceRecord.data_source_id == source.id)
        .order_by(SourceRecord.row_index)
    )
    return headers, [[record.record.get(header, "") for header in headers] for record in records]


def annotate_source_field(
    db: Session,
    user: User,
    data_source_id: UUID,
    field: str,
    statement: str,
    *,
    role: str | None = None,
) -> SourceManifest:
    """Record one authoritative user statement and repost the manifest.

    ``role`` is the structured half of the statement: when given, it wins
    over the inferred role everywhere deterministic code consumes roles.
    """
    if role is not None and role not in ANNOTATABLE_ROLES:
        raise ValueError(f"unknown_role: {role}")
    statement = statement.strip()
    if not statement:
        raise ValueError("empty_statement")
    for attempt in (1, 2):
        source = _owned_source(db, user.id, data_source_id, ANNOTATABLE_SOURCE_KINDS)
        if source is None:
            raise ValueError("unknown_source")
        manifest = active_manifest(db, source)
        if manifest is None:
            raise ValueError("source_has_no_manifest")
        known = known_manifest_fields(manifest.document)
        if field not in known:
            raise ValueError(f"unknown_field: {field}")
        existing = db.scalar(select(SourceAnnotation).where(
            SourceAnnotation.data_source_id == source.id,
            SourceAnnotation.field == field,
        ))
        if existing is None:
            db.add(SourceAnnotation(
                user_id=user.id, data_source_id=source.id,
                field=field, statement=statement, role=role,
            ))
        else:
            existing.statement = statement
            if role is not None:
                existing.role = role
        db.flush()
        if source.kind == SPREADSHEET_SOURCE_KIND:
            headers, rows = _stored_table(db, source, manifest)
            document = _build_document(db, source, headers, rows)
        else:
            # External sources re-profile on rescan, not on annotation: repost
            # the current profiled/inferred sections with the fresh user_stated
            # tier. Provenance law holds — the statement wins immediately and
            # survives the next rescan by construction.
            document = {
                **manifest.document,
                "annotations": {"provenance": "user_stated", "fields": _annotations_section(db, source)},
            }
        try:
            return post_manifest_version(db, source, document)
        except ManifestVersionConflict:
            # The rollback also undid the annotation write; redo it against
            # the winner's state exactly once, then fail loudly.
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def _numeric(value: str) -> Decimal | None:
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None
    # Decimal parses "NaN"/"Infinity" as valid non-finite values; treating them
    # as numbers crashes integer rendering and gte/lte comparisons downstream.
    return number if number.is_finite() else None


# A money column may already hold minor units. A bank export's `amount_minor`
# and a sheet's `Budget` are both money, but only one of them needs scaling —
# multiplying the first by 100 reports ₹2,760 as ₹2,76,000.
_MINOR_UNIT_HEADER = re.compile(r"(?:^|[_\s\-])(?:minor|paise|cents?)(?:$|[_\s\-])|_minor$", re.I)


def money_already_minor(field: str | None) -> bool:
    """Whether a money column's stored values are already minor units."""
    return bool(field and _MINOR_UNIT_HEADER.search(str(field)))


def scale_money(number: Decimal, field: str | None) -> int:
    """One money value in integer minor units, scaled only when it must be."""
    return int(number) if money_already_minor(field) else int(round(number * 100))


def aggregate_value_key(group_by: str | None, money: bool) -> str:
    """The key one aggregated result row carries its number under.

    A source may legitimately hold a column called ``value`` (or ``value_minor``),
    and a row is ``{group_key: ..., value_key: ...}`` — so grouping by that
    column would collapse the two entries into one and silently replace every
    group key with its own aggregate. The number moves aside instead, keeping
    the ``_minor`` suffix that marks integer minor units.
    """
    key = "value_minor" if money else "value"
    return f"metric_{key}" if group_by == key else key


def _matches(record: dict[str, Any], item: dict[str, Any]) -> bool:
    actual = str(record.get(item["field"], "")).strip()
    expected = str(item["value"]).strip()
    operator = item["operator"]
    if operator == "eq":
        return actual.casefold() == expected.casefold()
    if operator == "neq":
        return actual.casefold() != expected.casefold()
    if operator == "contains":
        return expected.casefold() in actual.casefold()
    left, right = _numeric(actual), _numeric(expected)
    if left is None or right is None:
        return False
    return left >= right if operator == "gte" else left <= right


def query_source(
    db: Session,
    user_id: UUID,
    data_source_id: UUID,
    *,
    metric: str,
    value_field: str | None = None,
    group_by: str | None = None,
    filters: list[dict[str, Any]] | None = None,
    limit: int = QUERY_ROW_CAP,
) -> dict[str, Any]:
    """Deterministic aggregation over one owned upload, manifest-validated."""
    source = _owned_spreadsheet(db, user_id, data_source_id)
    if source is None:
        raise ValueError("unknown_source")
    manifest = active_manifest(db, source)
    if manifest is None:
        raise ValueError("source_has_no_manifest")
    if metric not in {"sum", "count", "average"}:
        raise ValueError(f"unknown_metric: {metric}")
    known = {column["name"] for column in manifest.document["physical"]["columns"]}
    for name in filter(None, [value_field, group_by, *[item.get("field") for item in filters or []]]):
        if name not in known:
            raise ValueError(f"unknown_field: {name}")
    for item in filters or []:
        if item.get("operator") not in {"eq", "neq", "contains", "gte", "lte"}:
            raise ValueError(f"unknown_operator: {item.get('operator')}")
    if metric in {"sum", "average"} and not value_field:
        raise ValueError("value_field_required")
    limit = max(1, min(int(limit), QUERY_ROW_CAP))

    money = bool(value_field) and _column_role(manifest, value_field) == "money"
    value_key = aggregate_value_key(group_by, money)

    groups: dict[str, list[Decimal]] = defaultdict(list)
    matched = 0
    for record_row in db.scalars(
        select(SourceRecord)
        .where(SourceRecord.data_source_id == source.id, SourceRecord.user_id == user_id)
        .order_by(SourceRecord.row_index)
    ):
        record = record_row.record
        if any(not _matches(record, item) for item in filters or []):
            continue
        matched += 1
        key = str(record.get(group_by, "")).strip() if group_by else "all"
        if metric == "count":
            groups[key].append(Decimal(1))
        else:
            number = _numeric(str(record.get(value_field, "")))
            if number is not None:
                groups[key].append(number)

    def render(values: list[Decimal]) -> Any:
        if metric == "count":
            return int(sum(values))
        total = sum(values) if values else Decimal(0)
        result = (total / len(values)) if metric == "average" and values else total
        return scale_money(result, value_field) if money else float(result)

    rows_out = sorted(
        ({(group_by or "scope"): key, value_key: render(values)} for key, values in groups.items()),
        key=lambda item: (item[value_key] if isinstance(item[value_key], (int, float)) else 0),
        reverse=True,
    )[:limit]
    columns = [group_by or "scope", value_key]
    return {
        "columns": columns,
        "rows": rows_out,
        "row_count": len(rows_out),
        "matched_records": matched,
        "source_version": manifest.version,
    }


def owned_spreadsheets(db: Session, user_id: UUID) -> list[tuple[DataSource, SourceManifest]]:
    """This user's active uploads paired with their active manifests.

    The one listing seam other lanes (tool mounting, identity resolution) read,
    so the owner/kind/status boundary is written once.
    """
    sources = db.scalars(select(DataSource).where(
        DataSource.user_id == user_id,
        DataSource.kind == SPREADSHEET_SOURCE_KIND,
        DataSource.status == "active",
    ))
    resolved = []
    for source in sources:
        manifest = active_manifest(db, source)
        if manifest is not None:
            resolved.append((source, manifest))
    return resolved


def stated_notes(manifest: SourceManifest) -> str:
    """The user-stated meanings suffix every source catalog line ends with.

    A stated meaning is authoritative, so it belongs in front of the model
    wherever that source is offered — one rendering, shared by every lane.
    """
    stated = manifest.document.get("annotations", {}).get("fields", {})
    notes = "; ".join(
        f"{field}: {entry.get('statement', '')}" for field, entry in stated.items()
    )
    return f". User-stated meanings: {notes}" if notes else ""


def spreadsheet_catalog_line(source: DataSource, manifest: SourceManifest) -> str:
    """One prompt-facing line describing an upload: columns, roles, values.

    The profiled values travel with the column for the same reason they do on
    a connected database: a filter written against a guessed spelling returns
    nothing, and an empty result reads as absence rather than a near miss.
    """
    rendered = []
    for column in manifest.document["physical"]["columns"]:
        role = _column_role(manifest, column["name"])
        catalog = (column.get("catalog") or {}).get("values") or []
        sample = f" e.g. {', '.join(map(str, catalog[:6]))}" if catalog else ""
        rendered.append(f"{column['name']} ({role}){sample}")
    columns = ", ".join(rendered)
    return (
        f"- {source.name} (data_source_id={source.id}, v{manifest.version}): {columns}"
        + stated_notes(manifest)
    )


def build_spreadsheet_tools(context) -> list[Any]:
    """Mount the uploaded-source query lane when this user has uploads."""
    owned = owned_spreadsheets(context.db, context.user_id)
    if not owned:
        return []

    def query_uploaded_source(
        data_source_id: str,
        metric: str,
        value_field: str | None = None,
        group_by: str | None = None,
        filters: list | None = None,
    ) -> dict[str, Any]:
        try:
            result = query_source(
                context.db,
                context.user_id,
                UUID(str(data_source_id)),
                metric=metric,
                value_field=value_field,
                group_by=group_by,
                filters=[dict(item) for item in filters or []],
            )
        except (ValueError, KeyError) as error:
            return {"error": {
                "code": "invalid_source_query",
                "detail": str(error),
                "hint": "Use only this user's sources and the exact column names in this tool's description.",
            }}
        return {
            "kind": "uploaded_source_query",
            # The name the Python lane reads these same rows back under.
            "dataset_name": record_dataset(
                context, f"upload_{source_names.get(str(data_source_id), 'source')}", result["rows"]
            ),
            **result,
        }

    source_names = {str(source.id): source.name for source, _ in owned}
    catalog_lines = [spreadsheet_catalog_line(source, manifest) for source, manifest in owned]

    return [bind_schema_tool(
        query_uploaded_source,
        name="query_uploaded_source",
        description=(
            "Aggregate one of this user's uploaded spreadsheet sources (sum/count/average, "
            "optional group_by and filters). Money results use the _minor suffix (integer "
            "minor units). A result with an `error` key names the exact rejected input — "
            "correct it and retry at most twice.\n\nUploaded sources:\n" + "\n".join(catalog_lines)
        ),
        parameters={
            "type": "object",
            "properties": {
                "data_source_id": {"type": "string", "description": "The uploaded source id, from the list above."},
                "metric": {"type": "string", "enum": ["sum", "count", "average"]},
                "value_field": {"type": ["string", "null"], "description": "Column to aggregate; required for sum/average."},
                "group_by": {"type": ["string", "null"]},
                "filters": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "operator": {"type": "string", "enum": ["eq", "neq", "contains", "gte", "lte"]},
                            "value": {"type": "string"},
                        },
                        "required": ["field", "operator", "value"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["data_source_id", "metric", "value_field", "group_by", "filters"],
            "additionalProperties": False,
        },
        strict=True,
    )]
