"""The governed SQL authoring lane, mounted as one tool on the agent loop.

Phase 2 of the analyst platform blueprint, completing the lane the gate and
RLS layer (sql_gate and the migration baseline) made safe: the model authors one
PostgreSQL SELECT for requests the AnalysisPlan grammar cannot express, the
gate validates it against the source manifest, the database enforces tenancy,
and a rejected or failed statement returns its exact reason as the tool
result so the model corrects it inside the same bounded loop.

Every successfully executed statement is written back to the shared template
pool as a value-free SQL template: filter literals become typed parameters,
capability metadata is derived from the AST alone (never from user text), and
the row is keyed to the current source-manifest fingerprint. Stored SQL
templates surface as worked examples in the tool's own description — the
Vanna-style memory loop, under this product's privacy line.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import sqlglot
from sqlglot import exp
from sqlalchemy import select

from ..domain import AnalysisToolStatus
from ..event_time import now_utc
from ..models import AnalysisToolTemplate
from .manifest import native_manifest_fingerprint, scan_native_schema, user_value_catalog
from .agent_tools import bind_schema_tool
from .analysis_sandbox import record_dataset
from .answer_validation import compile_answer_contract
from .semantic_registry import semantic_schema_registry
from .sql_gate import (
    AUTHORING_DIALECT,
    SqlCompilationError,
    SqlGateError,
    execute_governed_sql,
)

RUN_SQL_TOOL_NAME = "run_governed_sql"
DESCRIBE_SQL_SCHEMA_TOOL_NAME = "describe_financial_schema"
SQL_TEMPLATE_VERSION = "governed-sql-template.v1"
MAX_SQL_EXAMPLES = 2
CORE_SQL_ENTITIES = ("transactions", "accounts", "categories", "subcategories")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")

# Predicate operators whose literal operands are user values, not structure.
_VALUE_PREDICATES = (
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike,
)


def _render_entity_schema(entity_names: set[str]) -> str:
    """Render selected physical entities and relationships without user data."""
    registry = semantic_schema_registry()
    physical = scan_native_schema()
    lines: list[str] = []
    for entity in registry.entities:
        if entity.name not in entity_names:
            continue
        columns = physical[entity.name]["columns"]
        rendered_columns = ", ".join(
            f"{column['name']} {column['type']}"
            for column in columns
        )
        lines.append(
            f"- {entity.table} ({entity.grain}; {entity.description}): "
            f"{rendered_columns}"
        )
    relationships = [
        f"- {item.source_entity}.{item.source_field} -> "
        f"{item.target_entity}.{item.target_field} ({item.cardinality})"
        for item in registry.relationships
        if item.source_entity in entity_names and item.target_entity in entity_names
    ]
    rendered = "Tenant-governed physical schema:\n" + "\n".join(lines)
    if relationships:
        rendered += "\n\nDeclared relationships within this selection:\n" + "\n".join(relationships)
    return rendered


def sql_schema_context(entity_names: set[str] | None = None) -> str:
    """Selected physical schema plus curated financial semantics for SQL authoring.

    Tenant-governed tables are the boundary. Inside it, the author sees every
    physical column for the selected entities and can use the complete
    PostgreSQL SELECT language. With no selection this retains the historical
    full-schema representation used by manifest and gate tests.
    """
    registry = semantic_schema_registry()
    selected = entity_names or {entity.name for entity in registry.entities}
    return (
        _render_entity_schema(selected)
        + "\n\nCanonical financial semantics:\n- "
        + "\n- ".join(registry.financial_rules)
    )


def _entity_directory() -> str:
    registry = semantic_schema_registry()
    return "\n".join(
        f"- {entity.name}: {entity.description}"
        for entity in registry.entities
    )


def sql_examples(db, question: str) -> list[dict[str, str]]:
    """Recent value-free SQL templates valid for the current manifest."""
    rows = list(db.scalars(
        select(AnalysisToolTemplate)
        .where(
            AnalysisToolTemplate.template_version == SQL_TEMPLATE_VERSION,
            AnalysisToolTemplate.status == AnalysisToolStatus.ACTIVE.value,
            AnalysisToolTemplate.source_manifest_hash == native_manifest_fingerprint(),
        )
        .order_by(
            AnalysisToolTemplate.last_used_at.desc().nullslast(),
            AnalysisToolTemplate.created_at.desc(),
        )
        .limit(MAX_SQL_EXAMPLES)
    ))
    return [
        {"purpose": row.capability_description, "sql": row.plan_template.get("sql", "")}
        for row in rows
        if isinstance(row.plan_template, dict)
    ]


def _literal_type(literal: exp.Literal) -> str:
    if literal.is_string:
        return "date" if _ISO_DATE.match(literal.name or "") else "string"
    return "number"


def _parameterize_sql(sql: str) -> tuple[str, list[dict[str, Any]]]:
    """Replace filter literals with typed placeholders.

    Only literals compared against a column become parameters — string
    arguments that are structure (``date_trunc('month', …)``) stay in place.
    The result is the value-free artifact safe for the shared pool.
    """
    expression = sqlglot.parse_one(sql, read=AUTHORING_DIALECT)
    parameters: list[dict[str, Any]] = []

    def parameter_for(literal: exp.Literal, column: exp.Column | None) -> exp.Placeholder:
        name = f"p{len(parameters) + 1}"
        parameters.append({
            "name": name,
            "type": _literal_type(literal),
            "required": True,
            "column": column.name if column is not None else None,
        })
        return exp.Placeholder(this=name)

    def transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, _VALUE_PREDICATES):
            column = node.this if isinstance(node.this, exp.Column) else (
                node.expression if isinstance(node.expression, exp.Column) else None
            )
            if column is not None:
                for side in ("this", "expression"):
                    if isinstance(node.args.get(side), exp.Literal):
                        node.set(side, parameter_for(node.args[side], column))
        elif isinstance(node, exp.Between) and isinstance(node.this, exp.Column):
            for side in ("low", "high"):
                if isinstance(node.args.get(side), exp.Literal):
                    node.set(side, parameter_for(node.args[side], node.this))
        elif isinstance(node, exp.In) and isinstance(node.this, exp.Column):
            node.set("expressions", [
                parameter_for(item, node.this) if isinstance(item, exp.Literal) else item
                for item in node.expressions
            ])
        return node

    return expression.transform(transform).sql(dialect=AUTHORING_DIALECT), parameters


def _structural_metadata(sql: str) -> tuple[str, str, str]:
    """Capability metadata derived from the AST alone — never from user text."""
    expression = sqlglot.parse_one(sql, read=AUTHORING_DIALECT)
    tables = sorted({table.name for table in expression.find_all(exp.Table)})
    aggregates = sorted({
        node.sql_name().lower() for node in expression.find_all(exp.AggFunc)
    })
    columns = sorted({column.name for column in expression.find_all(exp.Column)})
    name = f"SQL {'/'.join(aggregates) or 'read'} over {', '.join(tables)}"[:120]
    signature = f"sql | {','.join(tables)} | {','.join(aggregates)} | {','.join(columns)}"[:240]
    description = (
        f"Governed SQL analysis over {', '.join(tables)}"
        + (f" aggregating with {', '.join(aggregates)}" if aggregates else "")
        + f", touching columns {', '.join(columns)}."
    )[:500]
    return name, signature, description


def memorize_sql_template(db, user_id, gated_sql: str) -> bool:
    """Write one verified statement back to the pool, value-free and deduped."""
    parameterized, parameters = _parameterize_sql(gated_sql)
    name, signature, description = _structural_metadata(parameterized)
    semantic_registry_version = semantic_schema_registry().version
    source_manifest_hash = native_manifest_fingerprint()
    plan_template = {"kind": "sql", "dialect": AUTHORING_DIALECT, "sql": parameterized}
    specification = {
        "templateVersion": SQL_TEMPLATE_VERSION,
        "sourceManifest": {
            "kind": "native_ledger",
            "semanticVersion": semantic_registry_version,
            "hash": source_manifest_hash,
        },
        "parameterSchema": parameters,
        "planTemplate": plan_template,
    }
    fingerprint = hashlib.sha256(
        json.dumps(specification, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = db.scalar(
        select(AnalysisToolTemplate).where(AnalysisToolTemplate.template_hash == fingerprint)
    )
    if existing is not None:
        existing.success_count += 1
        existing.last_used_at = now_utc()
        db.flush()
        return False
    db.add(AnalysisToolTemplate(
        capability_name=name,
        capability_description=description,
        capability_signature=signature,
        template_version=SQL_TEMPLATE_VERSION,
        status=AnalysisToolStatus.ACTIVE.value,
        semantic_registry_version=semantic_registry_version,
        source_manifest_hash=source_manifest_hash,
        parameter_schema=parameters,
        plan_template=plan_template,
        template_hash=fingerprint,
        validation_report={"passed": True, "checks": [{"name": "sql_gate", "passed": True}]},
        success_count=1,
        last_used_at=now_utc(),
        created_by_user_id=user_id,
    ))
    db.flush()
    return True


def build_sql_analysis_tools(context) -> list[Any]:
    """Mount a compact direct SQL tool plus optional schema discovery.

    Common ledger queries can run directly against a small stable core schema.
    The model can request uncommon entities, tenant-scoped categorical values,
    and reusable examples only when its own reasoning needs them. No per-user
    data or question-specific contract enters the initial tool definition,
    keeping the provider prefix small and cacheable.
    """
    answer_contract = compile_answer_contract(context.question)

    registry = semantic_schema_registry()
    entity_names = [entity.name for entity in registry.entities]

    def describe_financial_schema(entities: list[str]) -> dict[str, Any]:
        selected = set(entities)
        unknown = sorted(selected.difference(entity_names))
        if unknown:
            return {"error": {
                "code": "unknown_schema_entity",
                "detail": f"Unknown governed entities: {', '.join(unknown)}.",
                "available_entities": entity_names,
            }}
        if not selected:
            return {"error": {
                "code": "empty_schema_selection",
                "detail": "Select at least one governed entity.",
            }}
        examples = sql_examples(context.db, context.question)
        relevant_examples = []
        for example in examples:
            try:
                tables = {
                    table.name
                    for table in sqlglot.parse_one(
                        example["sql"], read=AUTHORING_DIALECT
                    ).find_all(exp.Table)
                }
            except sqlglot.errors.SqlglotError:
                continue
            if tables.intersection(selected):
                relevant_examples.append(example)
        return {
            "kind": "governed_financial_schema",
            "entities": sorted(selected),
            "schema": sql_schema_context(selected),
            "user_values": user_value_catalog(
                context.db, context.user_id, sorted(selected)
            ),
            "worked_examples": relevant_examples,
            "instruction": (
                "Use only physical table and column names returned here. Never add a "
                "user_id predicate; tenancy is injected and enforced separately."
            ),
        }

    def run_governed_sql(purpose: str, sql: str) -> dict[str, Any]:
        template_saved: bool | str
        try:
            result = execute_governed_sql(context.db, context.user_id, sql)
        except SqlGateError as error:
            return {"error": {
                "code": error.code,
                "detail": str(error),
                "hint": "Correct the SQL against the governed schema in this tool's description and retry (at most twice).",
            }}
        except SqlCompilationError as error:
            return {"error": {
                "code": error.code,
                "stage": "semantic_compilation",
                "detail": str(error),
                "hint": (
                    "PostgreSQL rejected the statement during deterministic semantic "
                    "compilation; correct its result shape or expression types."
                ),
            }}
        except Exception as error:  # execution-level failure, fed back for one correction
            return {"error": {
                "code": "execution_error",
                "detail": f"{type(error).__name__}: {error}",
                "hint": "The statement passed the gate but failed to execute; correct it and retry once.",
            }}
        try:
            memorize_sql_template(context.db, context.user_id, result["sql"])
            template_saved = True
        except Exception as error:
            # The answer still ships; the failed write-back stays visible in
            # the durable tool payload instead of disappearing.
            template_saved = f"failed: {type(error).__name__}"
        rows = [dict(zip(result["columns"], row)) for row in result["rows"]]
        return {
            "kind": "governed_sql",
            "purpose": purpose,
            "columns": result["columns"],
            "result_schema": result["result_schema"],
            "rows": rows,
            "answer_contract": [
                item.code.value for item in answer_contract.obligations
            ],
            # The name the Python lane reads these same rows back under.
            "dataset_name": record_dataset(context, "sql_result", rows),
            "row_count": result["row_count"],
            "empty_result": result["row_count"] == 0,
            "empty_result_guidance": (
                "The statement executed successfully and its final result set is empty. "
                "Treat that as authoritative for this exact query; do not rerun only to "
                "confirm the same absence."
                if result["row_count"] == 0
                else None
            ),
            "limit": result["limit"],
            "tables": result["tables"],
            "semantic_compile_ms": result["semantic_compile_ms"],
            "template_saved": template_saved,
        }

    run_description = (
        "Author and run one arbitrary analytical PostgreSQL query graph over the authenticated "
        "tenant's governed financial schema. CTEs, joins, subqueries, unions, conditional "
        "aggregation, window functions, statistical functions, and custom expressions are "
        "available. Build the final derived answer in one statement rather than returning "
        "intermediate datasets for the reader to combine. Never filter by user_id — tenancy "
        "is injected and enforced by the database. Money columns are integer minor units: "
        "alias money results with an _minor suffix (e.g. total_minor). A result carrying an "
        "`error` key explains an invalid schema reference or operational refusal: correct it "
        "and retry at most twice. The most common spending and account schema is below. For "
        "any other entity, an uncertain join, or a user-specific categorical value, first call "
        f"{DESCRIBE_SQL_SCHEMA_TOOL_NAME} with only the entities you need.\n\n"
        + sql_schema_context(set(CORE_SQL_ENTITIES))
    )
    run_tool = bind_schema_tool(
        run_governed_sql,
        name=RUN_SQL_TOOL_NAME,
        description=run_description,
        parameters={
            "type": "object",
            "properties": {
                "purpose": {"type": "string", "description": "One short sentence: what this query answers."},
                "sql": {
                    "type": "string",
                    "description": (
                        "A PostgreSQL read query with one final result set. It may contain any "
                        "number of SELECTs through CTEs, nested subqueries, joins and set operations."
                    ),
                },
            },
            "required": ["purpose", "sql"],
            "additionalProperties": False,
        },
        strict=True,
    )
    describe_tool = bind_schema_tool(
        describe_financial_schema,
        name=DESCRIBE_SQL_SCHEMA_TOOL_NAME,
        description=(
            "Return exact physical columns, relationships, authenticated categorical values, "
            "and validated value-free SQL examples for selected governed financial entities. "
            "Use it only when the compact core schema on run_governed_sql is insufficient. "
            "Request the smallest relevant set. Available entities:\n"
            + _entity_directory()
        ),
        parameters={
            "type": "object",
            "properties": {
                "entities": {
                    "type": "array",
                    "items": {"type": "string", "enum": entity_names},
                    "description": "The smallest set of governed entities needed for the query.",
                },
            },
            "required": ["entities"],
            "additionalProperties": False,
        },
        strict=True,
    )
    return [run_tool, describe_tool]


def build_sql_analysis_tool(context) -> Any:
    """Compatibility accessor for callers executing the SQL tool directly."""
    return build_sql_analysis_tools(context)[0]
