"""External read-only database sources: connect, profile, manifest, query.

An external database becomes a governed source through the same manifest
machinery as uploads and the native ledger: a deterministic profiler reflects
an allowlisted set of tables (columns, types, nullability, per-column value
catalogs from a bounded row sample, and a row-count estimate), the shared
header heuristics draft column semantics with ``inferred`` provenance, and the
user's ``user_stated`` annotations — addressed as ``table.column`` — survive
every rescan and win over inference.

Two laws govern this module:

* Privacy: ``DataSource.config`` holds the connection url, which may embed
  credentials. It must never appear in a manifest document, a tool
  description, a query payload, an error detail, a log line, or a user export
  — so a url this module refuses comes back as a stable code and nothing else,
  never as the text that was attempted.
* Read-only: every engine is created read-only (sqlite ``mode=ro`` for
  file-backed databases; postgres ``default_transaction_read_only=on``), and
  querying executes exactly one parameterized SELECT built with SQLAlchemy
  Core — never string interpolation, never model-authored SQL.

The source row is the tenant boundary: every lookup binds owner and kind
through the shared ``_owned_source`` helper.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import MetaData, Table, create_engine, func, inspect, select
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnClause, ColumnElement

from ..config import get_settings
from ..models import DataSource, SourceManifest, User
from .agent_tools import bind_schema_tool
from .manifest import (
    EXTERNAL_SOURCE_KIND,
    MAX_CATALOG_VALUES,
    ManifestVersionConflict,
    active_manifest,
    post_manifest_version,
)
from .spreadsheet import (
    QUERY_ROW_CAP,
    _MONEY_HEADER,
    _annotations_section,
    _normalize_header,
    _numeric,
    _owned_source,
    aggregate_value_key,
    scale_money,
    match_fingerprint,
    semantic_draft,
    stated_field_role,
    stated_notes,
)

SUPPORTED_SCHEMES = frozenset({"sqlite", "postgresql"})
SAMPLE_ROW_LIMIT = 500
FILTER_OPERATORS = frozenset({"eq", "neq", "contains", "gte", "lte"})
METRICS = frozenset({"sum", "count", "average"})


# --- read-only engines --------------------------------------------------------

def _sqlite_file(database: str) -> str:
    """The file a sqlite url names, rejected unless it is one absolute path.

    A relative path resolves against whatever directory the server happens to
    be running in, so ``../../etc/passwd`` selects a file by accident of cwd
    rather than by the user's intent. A connection names a database the user
    owns, and that is always an absolute path with no traversal segment.
    """
    path = database[len("file:"):] if database.startswith("file:") else database
    if "\x00" in path or not path.startswith("/") or ".." in PurePosixPath(path).parts:
        raise ValueError("unsafe_database_path")
    return path


def _read_only_url(url: URL) -> tuple[URL, dict[str, Any]]:
    if url.get_backend_name() == "sqlite":
        database = url.database or ""
        if database and database != ":memory:":
            # File-backed sqlite opens through the sqlite3 URI form so the
            # driver itself enforces mode=ro; an INSERT raises OperationalError.
            url = url.set(
                database=f"file:{_sqlite_file(database)}",
                query={**dict(url.query), "mode": "ro", "uri": "true"},
            )
        return url, {}
    # postgres: a url naming no database points at nothing the user owns, and
    # whatever stands in its place is about to be read as a host by a driver.
    if not (url.database or "").strip():
        raise ValueError("missing_database_name")
    # every transaction on this engine starts read-only.
    return url, {"options": "-c default_transaction_read_only=on"}


def _reject_self_target(parsed: URL) -> None:
    """Refuse a url that points back at this application's own database.

    An external source is read through a role that owns its tables, so RLS —
    enabled, not forced, and scoped to ``fyn_analyst`` — does not apply. A
    source aimed at fyn's own database would therefore read every tenant's
    rows inside a governed-looking payload. The target identity, not the
    credentials, is what makes that dangerous: comparing host/port/database
    (or the resolved sqlite file) catches it whoever the url authenticates as.
    """
    try:
        own = make_url(get_settings().database_url)
    except ArgumentError:  # pragma: no cover - a broken app url fails earlier
        return
    if parsed.get_backend_name() != own.get_backend_name():
        return
    if parsed.get_backend_name() == "sqlite":
        target, mine = (parsed.database or ""), (own.database or "")
        if not target or not mine:
            return
        if Path(target[len("file:"):] if target.startswith("file:") else target).resolve() == Path(mine).resolve():
            raise ValueError("self_target_forbidden")
        return
    same_database = (parsed.database or "").strip() == (own.database or "").strip()
    same_port = (parsed.port or 5432) == (own.port or 5432)
    if same_database and same_port and _same_host(parsed.host, own.host):
        raise ValueError("self_target_forbidden")


def _same_host(left: str | None, right: str | None) -> bool:
    loopback = {"localhost", "127.0.0.1", "::1", ""}
    normalized_left = (left or "").strip().casefold()
    normalized_right = (right or "").strip().casefold()
    if normalized_left == normalized_right:
        return True
    return normalized_left in loopback and normalized_right in loopback


def create_read_only_engine(url: str) -> Engine:
    """A read-only engine for one connection url, or a typed rejection.

    Every rejection carries a stable code and nothing else. The url is
    credential material, so neither the url nor a driver's message describing
    it may travel out of this function — a parse failure, an unusable path and
    a missing DBAPI all become ``ValueError`` codes raised ``from None``.
    """
    try:
        parsed = make_url(url)
    except ArgumentError:
        raise ValueError("invalid_url") from None
    backend = parsed.get_backend_name()
    if backend not in SUPPORTED_SCHEMES:
        raise ValueError(f"unsupported_scheme: {backend}")
    _reject_self_target(parsed)
    allowlist = {item.strip().casefold() for item in get_settings().external_source_hosts.split(",") if item.strip()}
    if backend != "sqlite" and allowlist and (parsed.host or "").strip().casefold() not in allowlist:
        # An empty allowlist keeps local development usable; a configured one
        # is the deployment's statement of which hosts a source may name.
        raise ValueError("host_not_allowed")
    read_only, connect_args = _read_only_url(parsed)
    try:
        return create_engine(read_only, connect_args=connect_args)
    except (ArgumentError, ImportError):
        # A missing driver must not escape an agent tool as a bare ImportError.
        raise ValueError(f"driver_unavailable: {backend}") from None


# One engine per source, keyed by the config url so a changed url can never be
# served by a stale pool: a mismatch disposes the old engine and rebuilds.
_ENGINE_CACHE: dict[UUID, tuple[str, Engine]] = {}


def release_source_engine(source_id: UUID) -> None:
    """Drop a source's pooled connections and its cached url.

    Called when a source stops being reachable through the product — status
    change or deletion — so a credential-bearing pool cannot outlive the row
    that authorized it.
    """
    cached = _ENGINE_CACHE.pop(source_id, None)
    if cached is not None:
        cached[1].dispose()


def _source_engine(source: DataSource) -> Engine:
    url = str((source.config or {}).get("url") or "")
    cached = _ENGINE_CACHE.get(source.id)
    if cached is not None and cached[0] == url:
        return cached[1]
    if cached is not None:
        cached[1].dispose()
    engine = create_read_only_engine(url)
    _ENGINE_CACHE[source.id] = (url, engine)
    return engine


# --- profiling ----------------------------------------------------------------

def _column_kind(column_type: Any) -> str:
    """Map SQL types onto the spreadsheet profiler's type vocabulary."""
    if isinstance(column_type, (sa.Numeric, sa.Float)):
        return "decimal"
    if isinstance(column_type, sa.Integer):
        return "integer"
    if isinstance(column_type, (sa.Date, sa.DateTime)):
        return "date"
    return "string"


def profile_external_table(engine: Engine, table_name: str) -> dict[str, Any]:
    """Deterministic profile of one reflected table: structure plus a bounded
    row sample for value catalogs, in the spreadsheet profiler's shape so the
    shared ``semantic_draft`` heuristics apply unchanged."""
    table = Table(table_name, MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        sample = connection.execute(select(table).limit(SAMPLE_ROW_LIMIT)).mappings().all()
        row_count = int(connection.execute(select(func.count()).select_from(table)).scalar() or 0)
    columns: list[dict[str, Any]] = []
    for column in table.columns:
        kind = _column_kind(column.type)
        values = [row[column.name] for row in sample if row[column.name] is not None]
        catalog = None
        if kind == "string":
            frequency = Counter(str(value).strip() for value in values if str(value).strip())
            distinct = len(frequency)
            if 0 < distinct <= MAX_CATALOG_VALUES:
                catalog = {
                    "values": [value for value, _ in frequency.most_common(MAX_CATALOG_VALUES)],
                    "distinct": distinct,
                    "truncated": row_count > len(sample),
                }
        columns.append({
            "name": column.name,
            "normalized": _normalize_header(column.name),
            "type": kind,
            "nullable": bool(column.nullable),
            "null_count": len(sample) - len(values),
            "catalog": catalog,
            "money_hint": bool(_MONEY_HEADER.search(column.name)) and kind in {"integer", "decimal"},
        })
    return {"columns": columns, "row_count": row_count}


def _profile_source(engine: Engine, tables: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not tables:
        raise ValueError("no_tables_selected")
    known = set(inspect(engine).get_table_names())
    physical_tables: dict[str, Any] = {}
    semantic_tables: dict[str, Any] = {}
    for table_name in tables:
        if table_name not in known:
            raise ValueError(f"unknown_table: {table_name}")
        profile = profile_external_table(engine, table_name)
        headers = [column["name"] for column in profile["columns"]]
        physical_tables[table_name] = {
            "columns": [
                {key: column[key] for key in ("name", "type", "nullable", "catalog")}
                for column in profile["columns"]
            ],
            "row_count": profile["row_count"],
        }
        semantic_tables[table_name] = {
            "columns": semantic_draft(profile, match_fingerprint(headers))
        }
    return physical_tables, semantic_tables


def _build_document(
    db: Session, source: DataSource, physical_tables: dict[str, Any], semantic_tables: dict[str, Any]
) -> dict[str, Any]:
    # The connection url is credential material and must never enter this
    # document: manifests reach prompts and client surfaces.
    return {
        "kind": EXTERNAL_SOURCE_KIND,
        "name": source.name,
        "physical": {"provenance": "profiled", "tables": physical_tables},
        "semantics": {"provenance": "inferred", "tables": semantic_tables},
        "annotations": {"provenance": "user_stated", "fields": _annotations_section(db, source)},
    }


# --- connect / rescan ---------------------------------------------------------

def connect_external_database(
    db: Session, user: User, name: str, url: str, tables: list[str]
) -> tuple[DataSource, SourceManifest]:
    """Register an external database and post its first manifest version.

    Validation happens before any write: the scheme must be sqlite or
    postgresql, and every allowlisted table must exist. Reconnecting the same
    name updates the config (a changed url invalidates the cached engine) and
    posts a new version only when the profile actually changed.
    """
    tables = [str(table) for table in tables]
    engine = create_read_only_engine(url)  # raises unsupported_scheme first
    try:
        physical_tables, semantic_tables = _profile_source(engine, tables)
    except SQLAlchemyError as error:
        # A driver message names host, port and database. The url is
        # credential material, so only the exception class travels out.
        engine.dispose()
        raise ValueError(f"external_source_unavailable: {type(error).__name__}") from None
    except Exception:
        engine.dispose()
        raise
    for attempt in (1, 2):
        source = db.scalar(select(DataSource).where(
            DataSource.user_id == user.id,
            DataSource.kind == EXTERNAL_SOURCE_KIND,
            DataSource.name == name,
        ))
        if source is None:
            source = DataSource(user_id=user.id, kind=EXTERNAL_SOURCE_KIND, name=name)
            db.add(source)
        source.config = {"url": url, "tables": tables}
        db.flush()
        try:
            manifest = post_manifest_version(
                db, source, _build_document(db, source, physical_tables, semantic_tables)
            )
        except ManifestVersionConflict:
            # The rollback also undid the config write; redo everything against
            # the winner's state exactly once, then fail loudly. The engine
            # built for this attempt is disposed either way — a pool holding a
            # credential-bearing url must not outlive the call that failed.
            if attempt == 2:
                engine.dispose()
                raise
            continue
        cached = _ENGINE_CACHE.pop(source.id, None)
        if cached is not None and cached[1] is not engine:
            cached[1].dispose()
        _ENGINE_CACHE[source.id] = (url, engine)
        return source, manifest
    raise AssertionError("unreachable")


def rescan_external_source(db: Session, user: User, data_source_id: UUID) -> SourceManifest:
    """Re-profile the allowlisted tables and post a new manifest version.

    Annotations survive by construction: the ``user_stated`` section is loaded
    from stored rows, so only the profiled/inferred tiers refresh.
    """
    for attempt in (1, 2):
        source = _owned_source(db, user.id, data_source_id, (EXTERNAL_SOURCE_KIND,))
        if source is None:
            raise ValueError("unknown_source")
        tables = [str(table) for table in (source.config or {}).get("tables") or []]
        try:
            physical_tables, semantic_tables = _profile_source(_source_engine(source), tables)
        except SQLAlchemyError as error:
            raise ValueError(f"external_source_unavailable: {type(error).__name__}") from None
        try:
            return post_manifest_version(
                db, source, _build_document(db, source, physical_tables, semantic_tables)
            )
        except ManifestVersionConflict:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


# --- querying -----------------------------------------------------------------

def _column_role(manifest: SourceManifest, table: str, column: str) -> str:
    """user_stated beats inferred, addressed as table.column for external sources."""
    stated = stated_field_role(manifest.document, f"{table}.{column}")
    if stated is not None:
        return stated
    return (
        manifest.document["semantics"]["tables"]
        .get(table, {}).get("columns", {}).get(column, {}).get("role", "text")
    )


def build_query_statement(
    manifest: SourceManifest,
    *,
    table: str,
    metric: str,
    value_field: str | None,
    group_by: str | None,
    filters: list[dict[str, Any]] | None,
    limit: int,
) -> Select:
    """One manifest-validated SELECT with every value bound, never interpolated.

    Column and table names come exclusively from the profiled manifest, and
    filter/limit values travel as bind parameters, so no caller-supplied text
    ever reaches the SQL string.
    """
    tables = manifest.document["physical"]["tables"]
    if table not in tables:
        raise ValueError(f"unknown_table: {table}")
    section = tables[table]
    known = {column["name"]: column["type"] for column in section["columns"]}
    for name in filter(None, [value_field, group_by, *[item.get("field") for item in filters or []]]):
        if name not in known:
            raise ValueError(f"unknown_field: {name}")
    if metric not in METRICS:
        raise ValueError(f"unknown_metric: {metric}")
    for item in filters or []:
        if item.get("operator") not in FILTER_OPERATORS:
            raise ValueError(f"unknown_operator: {item.get('operator')}")
        if "\x00" in str(item.get("value", "")):
            # SQLite matches and compares over NUL-terminated C strings, so an
            # embedded NUL truncates the value inside the driver and silently
            # widens the filter — `contains "Shop 1\0extra"` becomes `%Shop 1`.
            raise ValueError(f"unsupported_filter_value: {item.get('field')}")
    if metric in {"sum", "average"} and not value_field:
        raise ValueError("value_field_required")
    limit = max(1, min(int(limit), QUERY_ROW_CAP))

    columns: dict[str, ColumnClause[Any]] = {
        name: sa.column(name) for name in known
    }
    relation = sa.table(table, *columns.values())

    def bound_value(name: str, raw: Any) -> Any:
        if known[name] in {"integer", "decimal"}:
            number = _numeric(str(raw))
            if number is None:
                raise ValueError(f"non_numeric_filter_value: {name}")
            return float(number)
        return str(raw)

    conditions = []
    for item in filters or []:
        column = columns[item["field"]]
        operator = item["operator"]
        if operator == "contains":
            # A plain substring match, same as the uploaded-source lane: LIKE
            # wildcards in the user's value are escaped so they match literally
            # rather than silently widening the filter.
            needle = str(item["value"])
            for wildcard in ("\\", "%", "_"):
                needle = needle.replace(wildcard, f"\\{wildcard}")
            conditions.append(sa.cast(column, sa.String).like(f"%{needle}%", escape="\\"))
            continue
        value = bound_value(item["field"], item["value"])
        if operator == "eq":
            conditions.append(column == value)
        elif operator == "neq":
            conditions.append(column != value)
        elif operator == "gte":
            conditions.append(column >= value)
        else:
            conditions.append(column <= value)

    aggregate: ColumnElement[Any]
    if metric == "count":
        aggregate = func.count()
    elif metric == "sum":
        assert value_field is not None
        aggregate = func.sum(columns[value_field])
    else:
        assert value_field is not None
        aggregate = func.avg(columns[value_field])

    if group_by:
        group_column = columns[group_by]
        statement = (
            select(group_column.label(group_by), aggregate.label("value"))
            .select_from(relation)
            .group_by(group_column)
            .order_by(sa.desc(aggregate), group_column)
            .limit(limit)
        )
    else:
        statement = select(aggregate.label("value")).select_from(relation)
    if conditions:
        statement = statement.where(sa.and_(*conditions))
    return statement


def query_external_source(
    db: Session,
    user_id: UUID,
    data_source_id: UUID,
    *,
    table: str,
    metric: str,
    value_field: str | None = None,
    group_by: str | None = None,
    filters: list[dict[str, Any]] | None = None,
    limit: int = QUERY_ROW_CAP,
) -> dict[str, Any]:
    """Deterministic aggregation over one owned external source.

    Exactly one parameterized SELECT executes against the read-only engine.
    Money columns (drafted or user-stated) render as integer minor units under
    the ``value_minor`` key, matching the uploaded-source lane.
    """
    source = _owned_source(db, user_id, data_source_id, (EXTERNAL_SOURCE_KIND,))
    if source is None:
        raise ValueError("unknown_source")
    manifest = active_manifest(db, source)
    if manifest is None:
        raise ValueError("source_has_no_manifest")
    statement = build_query_statement(
        manifest, table=table, metric=metric, value_field=value_field,
        group_by=group_by, filters=filters, limit=limit,
    )
    engine = _source_engine(source)
    with engine.connect() as connection:
        fetched = connection.execute(statement).all()

    money = metric != "count" and value_field is not None and _column_role(manifest, table, value_field) == "money"
    value_key = aggregate_value_key(group_by, money)

    def render(raw: Any) -> Any:
        number = Decimal(str(raw)) if raw is not None else Decimal(0)
        if metric == "count":
            return int(number)
        return scale_money(number, value_field) if money else float(number)

    if group_by:
        rows = [{group_by: key, value_key: render(raw)} for key, raw in fetched]
    else:
        rows = [{"scope": "all", value_key: render(fetched[0][0] if fetched else None)}]
    return {
        "columns": [group_by or "scope", value_key],
        "rows": rows,
        "row_count": len(rows),
        "table": table,
        "source_version": manifest.version,
    }


def column_value_counts(
    source: DataSource,
    manifest: SourceManifest,
    table: str,
    column: str,
    *,
    limit: int = QUERY_ROW_CAP,
) -> list[tuple[str, int]]:
    """Value frequencies for one profiled column, most common first.

    The engine stays inside this module so the privacy and read-only laws have
    exactly one enforcement point: callers hand over a source they already
    resolved through the tenant boundary and get back plain strings and counts.
    Table and column names are validated against the manifest and the SELECT is
    built with SQLAlchemy Core, so no caller string reaches the SQL text. The
    read is deliberately bounded — identity resolution wants the spellings a
    source actually uses, not a full column dump.
    """
    tables = manifest.document["physical"]["tables"]
    if table not in tables:
        raise ValueError(f"unknown_table: {table}")
    if column not in {item["name"] for item in tables[table]["columns"]}:
        raise ValueError(f"unknown_field: {column}")
    limit = max(1, min(int(limit), QUERY_ROW_CAP))
    target: ColumnClause[Any] = sa.column(column)
    occurrences = func.count()
    statement = (
        select(target, occurrences.label("occurrences"))
        .select_from(sa.table(table, target))
        .where(target.is_not(None))
        .group_by(target)
        .order_by(sa.desc(occurrences), target)
        .limit(limit)
    )
    with _source_engine(source).connect() as connection:
        return [(str(value), int(count)) for value, count in connection.execute(statement).all()]


# --- agent tool ---------------------------------------------------------------

def owned_external_sources(db: Session, user_id: UUID) -> list[tuple[DataSource, SourceManifest]]:
    """This user's active connections paired with their active manifests.

    The one listing seam other lanes (tool mounting, identity resolution) read,
    so the owner/kind/status boundary is written once.
    """
    sources = db.scalars(select(DataSource).where(
        DataSource.user_id == user_id,
        DataSource.kind == EXTERNAL_SOURCE_KIND,
        DataSource.status == "active",
    ))
    resolved = []
    for source in sources:
        manifest = active_manifest(db, source)
        if manifest is not None:
            resolved.append((source, manifest))
    return resolved


def external_catalog_line(source: DataSource, manifest: SourceManifest) -> str:
    """One prompt-facing line describing a connection: tables, columns, roles.

    The connection url is deliberately absent — this line reaches prompts.
    """
    table_lines = []
    for table_name, section in manifest.document["physical"]["tables"].items():
        rendered = []
        for column in section["columns"]:
            role = _column_role(manifest, table_name, column["name"])
            # The profiled values travel with the column. Without them a filter
            # is a guess: "Blue Tokai" finds nothing in a column holding "Blue
            # Tokai Coffee", and an empty result reads as "you have no such
            # records" instead of "that is not how the value is spelled".
            catalog = (column.get("catalog") or {}).get("values") or []
            sample = f" e.g. {', '.join(map(str, catalog[:6]))}" if catalog else ""
            rendered.append(f"{column['name']} ({role}){sample}")
        table_lines.append(f"{table_name}: " + ", ".join(rendered))
    return (
        f"- {source.name} (data_source_id={source.id}, v{manifest.version}): "
        + " | ".join(table_lines)
        + stated_notes(manifest)
    )


def build_external_tools(context) -> list[Any]:
    """Mount the external-database query lane when this user has connections."""
    owned = owned_external_sources(context.db, context.user_id)
    if not owned:
        return []

    def query_external_database(
        data_source_id: str,
        table: str,
        metric: str,
        value_field: str | None = None,
        group_by: str | None = None,
        filters: list | None = None,
    ) -> dict[str, Any]:
        try:
            return {"kind": "external_source_query", **query_external_source(
                context.db,
                context.user_id,
                UUID(str(data_source_id)),
                table=table,
                metric=metric,
                value_field=value_field,
                group_by=group_by,
                filters=[dict(item) for item in filters or []],
            )}
        except (ValueError, KeyError) as error:
            return {"error": {
                "code": "invalid_source_query",
                "detail": str(error),
                "hint": "Use only this user's sources and the exact table/column names in this tool's description.",
            }}
        except SQLAlchemyError as error:
            # Driver messages can echo connection material; report only the
            # class name, never the message or the url.
            return {"error": {
                "code": "external_source_unavailable",
                "detail": type(error).__name__,
                "hint": "The external database rejected the query or is unreachable. Tell the user; do not retry blindly.",
            }}

    catalog_lines = [external_catalog_line(source, manifest) for source, manifest in owned]

    return [bind_schema_tool(
        query_external_database,
        name="query_external_database",
        description=(
            "Aggregate one table of this user's connected external databases "
            "(sum/count/average, optional group_by and filters). Money results use the "
            "_minor suffix (integer minor units). A result with an `error` key names the "
            "exact rejected input — correct it and retry at most twice.\n\n"
            "External sources:\n" + "\n".join(catalog_lines)
        ),
        parameters={
            "type": "object",
            "properties": {
                "data_source_id": {"type": "string", "description": "The external source id, from the list above."},
                "table": {"type": "string", "description": "One table name of that source, from the list above."},
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
            "required": ["data_source_id", "table", "metric", "value_field", "group_by", "filters"],
            "additionalProperties": False,
        },
        strict=True,
    )]
