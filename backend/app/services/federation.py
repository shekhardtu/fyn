"""Deterministic federation: one governed native query joined to one source.

A user's money question rarely lives in one place — canonical spending is in
the ledger, the budget that spending is measured against is in a spreadsheet,
and the merchant list to check it against may be in a connected database. This
module answers those questions without inventing a third query engine: each
side runs through the lane that already governs it, and the join happens in
Python on keys the caller declares.

Three laws hold this lane together:

* One grammar per side. The native side is a ``FinanceQueryPlan`` executed by
  ``execute_finance_query``, so metric validation, relationship approval and
  tenant scoping are exactly the ones every other native answer gets. The
  source side is ``query_source`` or ``query_external_source`` per the source
  kind, so manifest validation and the source-row tenant boundary come along
  unchanged. Nothing here composes SQL.
* Nothing is silently dropped. The join is an inner join, so rows without a
  partner would otherwise vanish; they are counted (and named, up to a cap) as
  ``unmatched_native`` / ``unmatched_source``. Both sides also report whether
  their row cap truncated them.
* Lineage names both sources. A federated number is only as trustworthy as the
  two manifests it came from, so the result carries the native manifest
  fingerprint alongside the source's id, manifest hash and version.

Money keeps the project-wide representation on both sides: integer minor units
under a ``_minor`` key, never floats.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from ..event_time import now_utc
from ..models import DataSource, SourceManifest
from .agent_tools import bind_schema_tool
from .extraction import normalize_merchant
from .external_db import (
    external_catalog_line,
    owned_external_sources,
    query_external_source,
)
from .manifest import EXTERNAL_SOURCE_KIND, active_manifest, native_manifest_fingerprint
from .semantic import FinanceQueryPlan, execute_finance_query
from .spreadsheet import (
    QUERY_ROW_CAP,
    SPREADSHEET_SOURCE_KIND,
    _owned_source,
    owned_spreadsheets,
    query_source,
    spreadsheet_catalog_line,
)

# Every non-native governed source may stand on the source side of a join.
FEDERATABLE_SOURCE_KINDS = (SPREADSHEET_SOURCE_KIND, EXTERNAL_SOURCE_KIND)
JOIN_MATCHES = frozenset({"exact", "merchant"})

# Unmatched rows are always counted; naming them is capped because the result
# is prompt context, not a data dump. The count remains exact either way.
MAX_REPORTED_UNMATCHED_KEYS = 20


def _required(spec: dict[str, Any], key: str, scope: str) -> Any:
    value = spec.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"missing_{scope}_field: {key}")
    return value


def _join_key(value: Any, match: str) -> str | None:
    """The comparable form of one side's key, or None when it cannot join.

    ``exact`` compares trimmed values case-insensitively — the same equality
    the uploaded-source lane's ``eq`` operator already applies, so a join
    behaves like a filter the user could have written by hand. ``merchant``
    runs both sides through the canonical merchant normalizer first, so an
    ALL-CAPS statement descriptor meets a tidy merchant list.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if match == "merchant":
        return normalize_merchant(text) or None
    return text.casefold()


# --- the two sides ------------------------------------------------------------

def _run_native(
    db: Session, user_id: UUID, native: dict[str, Any], join_field: str
) -> tuple[FinanceQueryPlan, dict[str, Any]]:
    """The native side, through the governed grammar and nothing else."""
    dimensions = [str(item) for item in native.get("dimensions") or []]
    if join_field not in dimensions:
        # The key must be a projected dimension or there is nothing to join on.
        raise ValueError(f"unknown_join_field: native.{join_field}")
    plan = FinanceQueryPlan(
        name=str(native.get("name") or "native side")[:100],
        metric=str(_required(native, "metric", "native")),
        dimensions=dimensions,
        filters=[dict(item) for item in native.get("filters") or []],
        start_date=_required(native, "start_date", "native"),
        end_date=_required(native, "end_date", "native"),
        limit=int(native.get("limit") or QUERY_ROW_CAP),
    )
    return plan, execute_finance_query(db, user_id, plan)


def _resolve_source(
    db: Session, user_id: UUID, spec: dict[str, Any], join_field: str
) -> tuple[DataSource, dict[str, Any]]:
    """Bind the source row and its call, before anything expensive runs.

    Ownership and shape are settled here so an unreachable source or an
    unjoinable key never costs a full governed native scan first.
    """
    raw_id = _required(spec, "data_source_id", "source")
    try:
        data_source_id = UUID(str(raw_id))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("unknown_source") from None
    source = _owned_source(db, user_id, data_source_id, FEDERATABLE_SOURCE_KINDS)
    if source is None:
        raise ValueError("unknown_source")

    group_by = str(_required(spec, "group_by", "source"))
    if join_field != group_by:
        # The source result is keyed by its group_by column; joining on
        # anything else would compare a column that is not in the rows.
        raise ValueError(f"unknown_join_field: source.{join_field}")
    call = {
        "metric": str(_required(spec, "metric", "source")),
        "value_field": spec.get("value_field") or None,
        "group_by": group_by,
        "filters": [dict(item) for item in spec.get("filters") or []],
    }
    if source.kind == SPREADSHEET_SOURCE_KIND:
        if spec.get("table"):
            raise ValueError("table_not_supported_for_spreadsheet_source")
    else:
        call["table"] = str(_required(spec, "table", "source"))
    return source, call


def _run_source(
    db: Session, user_id: UUID, source: DataSource, call: dict[str, Any]
) -> tuple[SourceManifest, dict[str, Any]]:
    """The source side, dispatched by kind to the lane that governs it."""
    lane = query_source if source.kind == SPREADSHEET_SOURCE_KIND else query_external_source
    result = lane(db, user_id, source.id, **call)
    manifest = active_manifest(db, source)
    if manifest is None:  # pragma: no cover - both query lanes reject this first
        raise ValueError("source_has_no_manifest")
    return manifest, result


# --- the join -----------------------------------------------------------------

def query_across_sources(
    db: Session,
    user_id: UUID,
    *,
    native: dict[str, Any],
    source: dict[str, Any],
    join_on: dict[str, Any],
) -> dict[str, Any]:
    """Join one governed native query to one owned non-native source.

    ``native`` is ``{metric, dimensions?, filters?, start_date, end_date}``;
    ``source`` is ``{data_source_id, table?, metric, value_field?, group_by,
    filters?}``; ``join_on`` is ``{native_field, source_field, match}``. Both
    join fields must be projected keys of their side — a native dimension and
    the source's group_by column — and every rejection is a ValueError with a
    stable code.
    """
    match = str(join_on.get("match") or "exact")
    if match not in JOIN_MATCHES:
        raise ValueError(f"unknown_join_match: {match}")
    native_field = str(_required(join_on, "native_field", "join"))
    source_field = str(_required(join_on, "source_field", "join"))

    owned, call = _resolve_source(db, user_id, source, source_field)
    plan, native_result = _run_native(db, user_id, native, native_field)
    manifest, source_result = _run_source(db, user_id, owned, call)

    native_rows: list[dict[str, Any]] = native_result["rows"]
    source_rows: list[dict[str, Any]] = source_result["rows"]
    # A money metric declares its currency; a count declares none. The source
    # lane already decided its own key from the column's governed role.
    native_value_key = "native_value_minor" if native_result["currency"] else "native_value"
    source_raw_key = source_result["columns"][-1]
    source_value_key = f"source_{source_raw_key}"

    extra_native_dimensions = [
        name for name in native_result["dimensions"]
        if name != native_field and name not in {"key", "native_key", "source_key"}
    ]
    index: dict[str, list[int]] = {}
    for position, row in enumerate(source_rows):
        key = _join_key(row.get(source_field), match)
        if key is not None:
            index.setdefault(key, []).append(position)

    joined: list[dict[str, Any]] = []
    matched_native: set[int] = set()
    matched_source: set[int] = set()
    for native_position, native_row in enumerate(native_rows):
        key = _join_key(native_row.get(native_field), match)
        # A blank or unnormalizable key joins to nothing and stays unmatched.
        for source_position in index.get(key, []) if key is not None else ():
            source_row = source_rows[source_position]
            matched_native.add(native_position)
            matched_source.add(source_position)
            joined.append({
                "key": key,
                # Both originals travel with the pair: under a normalized match
                # the compared key is neither side's displayed value.
                "native_key": native_row.get(native_field),
                "source_key": source_row.get(source_field),
                # Every other native dimension travels too. Projecting a
                # dimension and then dropping it produces rows identical except
                # for their value — indistinguishable to a reader, and a
                # repeated source value that double-counts when summed.
                **{
                    name: native_row.get(name)
                    for name in extra_native_dimensions
                },
                native_value_key: native_row.get("value"),
                source_value_key: source_row.get(source_raw_key),
            })
    joined.sort(key=lambda item: (
        item["key"],
        str(item["native_key"]),
        str(item["source_key"]),
        *(str(item.get(name)) for name in extra_native_dimensions),
    ))

    unmatched_native = [
        row.get(native_field) for position, row in enumerate(native_rows)
        if position not in matched_native
    ]
    unmatched_source = [
        row.get(source_field) for position, row in enumerate(source_rows)
        if position not in matched_source
    ]
    return {
        "columns": [
            "key", "native_key", "source_key",
            *extra_native_dimensions,
            native_value_key, source_value_key,
        ],
        "rows": joined,
        "row_count": len(joined),
        "join": {"native_field": native_field, "source_field": source_field, "match": match},
        "native": {
            "metric": plan.metric,
            "dimensions": native_result["dimensions"],
            "currency": native_result["currency"],
            "start": native_result["start"],
            "end": native_result["end"],
            "row_count": len(native_rows),
            "truncated": len(native_rows) >= plan.limit,
        },
        "source": {
            "name": owned.name,
            "kind": owned.kind,
            "table": source_result.get("table"),
            "metric": call["metric"],
            "row_count": len(source_rows),
            "truncated": len(source_rows) >= QUERY_ROW_CAP,
        },
        # An inner join hides whatever did not pair; these say what it hid.
        "unmatched_native": len(unmatched_native),
        "unmatched_source": len(unmatched_source),
        "unmatched_native_keys": unmatched_native[:MAX_REPORTED_UNMATCHED_KEYS],
        "unmatched_source_keys": unmatched_source[:MAX_REPORTED_UNMATCHED_KEYS],
        "lineage": {
            "native": {"manifestHash": native_manifest_fingerprint()},
            "source": {
                "dataSourceId": str(owned.id),
                "manifestHash": manifest.manifest_hash,
                "version": manifest.version,
            },
            "joinedAt": now_utc().isoformat(),
        },
    }


# --- agent tool ---------------------------------------------------------------

def _federatable_catalog(db: Session, user_id: UUID) -> list[str]:
    """One catalog line per owned non-native source, reusing each lane's own
    renderer so a role or annotation reads identically in every description."""
    return [
        *(spreadsheet_catalog_line(source, manifest)
          for source, manifest in owned_spreadsheets(db, user_id)),
        *(external_catalog_line(source, manifest)
          for source, manifest in owned_external_sources(db, user_id)),
    ]


def build_federation_tool(context) -> list[Any]:
    """Mount the federation lane when this user has a non-native source."""
    catalog = _federatable_catalog(context.db, context.user_id)
    if not catalog:
        return []

    def run_query_across_sources(
        native_metric: str,
        native_dimensions: list | None,
        native_filters: list | None,
        start_date: str,
        end_date: str,
        data_source_id: str,
        table: str | None,
        source_metric: str,
        source_value_field: str | None,
        source_group_by: str,
        source_filters: list | None,
        native_join_field: str,
        source_join_field: str,
        match: str,
    ) -> dict[str, Any]:
        try:
            return {"kind": "federated_query", **query_across_sources(
                context.db,
                context.user_id,
                native={
                    "metric": native_metric,
                    "dimensions": [str(item) for item in native_dimensions or []],
                    "filters": [dict(item) for item in native_filters or []],
                    "start_date": start_date,
                    "end_date": end_date,
                },
                source={
                    "data_source_id": data_source_id,
                    "table": table,
                    "metric": source_metric,
                    "value_field": source_value_field,
                    "group_by": source_group_by,
                    "filters": [dict(item) for item in source_filters or []],
                },
                join_on={
                    "native_field": native_join_field,
                    "source_field": source_join_field,
                    "match": match,
                },
            )}
        except (ValueError, KeyError) as error:
            return {"error": {
                "code": "invalid_federated_query",
                "detail": str(error),
                "hint": (
                    "native_join_field must be one of native_dimensions and source_join_field "
                    "must equal source_group_by. Use only this user's sources and the exact "
                    "column names in this tool's description."
                ),
            }}

    filters_schema = {
        "type": ["array", "null"],
        "items": {
            "type": "object",
            "properties": {
                "field": {"type": "string"},
                "operator": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["field", "operator", "value"],
            "additionalProperties": False,
        },
    }
    return [bind_schema_tool(
        run_query_across_sources,
        name="query_across_sources",
        description=(
            "Join one governed native-ledger query to one of this user's non-native sources "
            "on a declared key. Both sides run through the lane that already governs them — "
            "the native side is a governed semantic query (metric, dimensions, filters, "
            "inclusive date window, tenant-scoped), the source side aggregates one uploaded "
            "spreadsheet or one table of a connected database — and the join itself is "
            "deterministic Python, never generated SQL.\n"
            "Join contract: native_join_field must be one of native_dimensions, "
            "source_join_field must equal source_group_by, and match is either 'exact' "
            "(trimmed, case-insensitive) or 'merchant' (both sides normalized first, so "
            "'BLUE TOKAI  ' meets 'Blue Tokai'). Each result row carries the compared key "
            "plus both original values.\n"
            "Nothing is dropped quietly: rows with no partner are reported as "
            "unmatched_native/unmatched_source with their keys, and each side reports "
            "whether its row cap truncated it. Money stays in integer minor units under "
            "_minor keys, and lineage names both sources (native manifest hash, plus the "
            "source's id, manifest hash and version) — cite it when you use the answer. "
            "Amounts from the two sides are only comparable if the user's source really "
            "holds the same currency; say so rather than assuming it.\n"
            "A result with an `error` key names the exact rejected input — correct it and "
            "retry at most twice.\n\nNon-native sources:\n" + "\n".join(catalog)
        ),
        parameters={
            "type": "object",
            "properties": {
                "native_metric": {
                    "type": "string",
                    "description": "A governed metric from the semantic catalog, e.g. gross_spend.",
                },
                "native_dimensions": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Governed dimensions to project; must include native_join_field.",
                },
                "native_filters": {**filters_schema, "description": "Governed filters on the native side."},
                "start_date": {"type": "string", "description": "Inclusive ISO start date, YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "Inclusive ISO end date, YYYY-MM-DD."},
                "data_source_id": {"type": "string", "description": "The non-native source id, from the list above."},
                "table": {
                    "type": ["string", "null"],
                    "description": "Required for a connected database source; must be null for an upload.",
                },
                "source_metric": {"type": "string", "enum": ["sum", "count", "average"]},
                "source_value_field": {
                    "type": ["string", "null"],
                    "description": "Source column to aggregate; required for sum/average.",
                },
                "source_group_by": {
                    "type": "string",
                    "description": "Source column the result is keyed by; must equal source_join_field.",
                },
                "source_filters": {**filters_schema, "description": "Filters on the source side (eq, neq, contains, gte, lte)."},
                "native_join_field": {"type": "string", "description": "The native dimension to join on."},
                "source_join_field": {"type": "string", "description": "The source column to join on."},
                "match": {"type": "string", "enum": ["exact", "merchant"]},
            },
            "required": [
                "native_metric", "native_dimensions", "native_filters", "start_date", "end_date",
                "data_source_id", "table", "source_metric", "source_value_field",
                "source_group_by", "source_filters", "native_join_field", "source_join_field",
                "match",
            ],
            "additionalProperties": False,
        },
        strict=True,
    )]
