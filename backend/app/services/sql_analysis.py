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
SQL_TEMPLATE_VERSION = "governed-sql-template.v1"
MAX_SQL_EXAMPLES = 2
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")

# Predicate operators whose literal operands are user values, not structure.
_VALUE_PREDICATES = (
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike,
)


def sql_schema_context() -> str:
    """Full physical schema plus curated financial semantics for SQL authoring.

    Tenant-governed tables are the boundary. Inside it, the author sees every
    physical column and can use the complete PostgreSQL SELECT language.
    """
    registry = semantic_schema_registry()
    physical = scan_native_schema()
    lines: list[str] = []
    for entity in registry.entities:
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
    ]
    return (
        "Tenant-governed physical schema (all listed columns are available):\n"
        + "\n".join(lines)
        + "\n\nDeclared relationships:\n"
        + "\n".join(relationships)
        + "\n\nCanonical financial semantics:\n- "
        + "\n- ".join(registry.financial_rules)
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
    specification = {
        "templateVersion": SQL_TEMPLATE_VERSION,
        "sourceManifest": {
            "kind": "native_ledger",
            "semanticVersion": semantic_schema_registry().version,
            "hash": native_manifest_fingerprint(),
        },
        "parameterSchema": parameters,
        "planTemplate": {"kind": "sql", "dialect": AUTHORING_DIALECT, "sql": parameterized},
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
        semantic_registry_version=specification["sourceManifest"]["semanticVersion"],
        source_manifest_hash=specification["sourceManifest"]["hash"],
        parameter_schema=parameters,
        plan_template=specification["planTemplate"],
        template_hash=fingerprint,
        validation_report={"passed": True, "checks": [{"name": "sql_gate", "passed": True}]},
        success_count=1,
        last_used_at=now_utc(),
        created_by_user_id=user_id,
    ))
    db.flush()
    return True


def build_sql_analysis_tool(context) -> Any:
    """Mount the governed SQL lane for one turn.

    The tool's description carries the full authoring contract — schema,
    this user's value catalog, and recent worked examples — so the agent
    needs no separate prompt plumbing.
    """
    catalog = user_value_catalog(context.db, context.user_id)
    examples = sql_examples(context.db, context.question)
    answer_contract = compile_answer_contract(context.question)

    def run_governed_sql(purpose: str, sql: str) -> dict[str, Any]:
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
            "limit": result["limit"],
            "tables": result["tables"],
            "semantic_compile_ms": result["semantic_compile_ms"],
            "template_saved": template_saved,
        }

    description = (
        "Author and run one arbitrary analytical PostgreSQL query graph over the authenticated "
        "tenant's complete governed schema. CTEs, joins, subqueries, unions, conditional "
        "aggregation, window functions, statistical functions, and custom expressions are "
        "available. Build the final derived answer in one statement rather than returning "
        "intermediate datasets for the reader to combine. Never filter by user_id — tenancy "
        "is injected and enforced by the database. Money columns are integer minor units: "
        "alias money results with an _minor suffix (e.g. total_minor). A result carrying an "
        "`error` key explains an invalid schema reference or operational refusal: correct it "
        "and retry at most twice.\n\n"
        + sql_schema_context()
        + "\n\n"
        + answer_contract.prompt()
        + "\n\nValues present in this user's data (bind filters to these exact strings): "
        + json.dumps(catalog, default=str)
        + (
            "\n\nWorked examples from the validated pool (parameter placeholders like %(p1)s "
            "must be replaced with real literal values):\n"
            + "\n".join(f"-- {item['purpose']}\n{item['sql']}" for item in examples)
            if examples
            else ""
        )
    )
    return bind_schema_tool(
        run_governed_sql,
        name=RUN_SQL_TOOL_NAME,
        description=description,
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
