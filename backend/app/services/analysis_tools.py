"""Dynamic analysis tools for the single agent loop.

The agent's toolset is assembled per turn: the top retrieved pool templates
compile to ``bind_template__*`` tools that fill and RUN a validated stored
analysis, and ``run_financial_analysis`` lets the model author a novel
``AnalysisPlan`` that the harness validates, executes, and templatizes back
into the pool — the toolset literally grows out of answered questions.

Every tool executes through the governed harness, so validation, tenancy,
template caching, and the durable audit trail are identical to every other
path. A failed validation returns its check details as the tool result so the
model can correct the plan in the same loop instead of a separate repair pass.
The model never renders these payloads blindly: the composed reply is verified
against them by the evidence postcondition before it ships.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..schemas import DataReference, WidgetType
from .agent_tools import bind_schema_tool
from .external_db import build_external_tools
from .federation import build_federation_tool
from .intelligence import tool_facing_rows
from .spreadsheet import build_spreadsheet_tools
from .analysis_sandbox import build_python_analysis_tool, record_dataset
from .sql_analysis import build_sql_analysis_tool
from .analysis_harness import (
    AnalysisReplay,
    HarnessResult,
    HarnessValidationError,
    execute_analysis_template,
)
from .semantic import AnalysisToolProposal
from .template_binding import (
    _model_visible_parameters,
    _public_name,
    bind_tool_name,
    materialize_binding,
    tenancy_safe_parameters,
)
from .template_retrieval import retrieve_templates


RUN_ANALYSIS_TOOL_NAME = "run_financial_analysis"
REPLAY_ANALYSIS_TOOL_NAME = "replay_validated_analysis"

# The plan shape the model must satisfy, stated where the model can read it.
# Every rule below is one the contract enforces and the tool's prose left
# implicit — a chart request used to fail three times over a missing query
# name and an encoding channel written as a bare string.
ANALYSIS_PLAN_CONTRACT = """Required plan shape, exactly:
- Every entry in queries[] needs a short `name` (it is how transforms and chart
  views reference that query) and, when ordering, `order` of exactly "asc" or "desc".
- To draw a chart, add visualizations[] — WITHOUT it a chart request returns only
  numbers. Each view is:
  {"id": "snake_case_id", "title": "Human title", "dataset": "<the query's name, snake_cased>",
   "mark": one of bar|line|area|point|rect|arc|tick,
   "encoding": {channel: {"field": "<a field the query produces>",
                          "type": quantitative|nominal|ordinal|temporal,
                          "value_type": string|number|date|datetime|money_minor|percentage|category}}}
- An encoding channel is always an OBJECT, never a bare field name, and `field`
  and `type` are both required on it. Channels: x, y, color, size, theta, row,
  column, tooltip[].
- Use x+y for bar/line/area/point/rect/tick; use theta+color for arc.
- Money measures carry "value_type": "money_minor" so the renderer formats rupees.
Worked chart example:
{"objective": "descriptive", "analysis_type": "semantic_query",
 "safe_reasoning_summary": ["Group this month's spending by category"],
 "queries": [{"name": "spend_by_category", "metric": "gross_spend", "dimensions": ["category"],
              "start_date": "2026-08-01", "end_date": "2026-08-18", "order": "desc", "limit": 20}],
 "visualizations": [{"id": "spend_by_category_chart", "title": "Spending by category",
                     "dataset": "spend_by_category", "mark": "bar",
                     "encoding": {"x": {"field": "category", "type": "nominal", "value_type": "category"},
                                  "y": {"field": "value", "type": "quantitative", "value_type": "money_minor"}}}]}"""

_TYPE_SCHEMAS: dict[str, dict[str, Any]] = {
    "date": {"type": "string", "description": "Inclusive ISO date, YYYY-MM-DD."},
    "datetime": {"type": "string", "description": "ISO 8601 timestamp."},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "array": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}},
    "string": {"type": "string"},
}


@dataclass
class AnalysisToolContext:
    """Trusted per-turn bindings plus the evidence collected across tool calls."""

    db: Session
    user_id: UUID
    conversation_id: UUID
    today: date
    timezone_name: str
    question: str
    citations: list[DataReference] = field(default_factory=list)
    # Result rows every governed lane records under a stable name, and the
    # only data the bounded Python lane is ever handed.
    datasets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _harness_payload(context: AnalysisToolContext, outcome: HarnessResult) -> dict[str, Any]:
    result = outcome.result
    payload: dict[str, Any] = {
        "kind": "governed_analysis",
        # Deterministic grounded markdown; the model may quote or recompose it.
        "message": result.message,
        "query_results": [
            {
                "name": item.get("name"),
                "metric": item.get("metric"),
                "currency": item.get("currency"),
                "dimensions": item.get("dimensions"),
                "start": item.get("start"),
                "end": item.get("end"),
                "rows": rows,
                # The name the Python lane reads these same rows back under.
                "dataset_name": record_dataset(
                    context, f"analysis_{item.get('name') or 'result'}", rows
                ),
            }
            for item, rows in (
                (item, tool_facing_rows(item)) for item in result.query_results
            )
        ],
        "reused_template": outcome.reused,
    }
    chart_widgets = [
        widget.model_dump(mode="json")
        for widget in result.widgets
        if widget.type is WidgetType.DATA_CHART
    ]
    if chart_widgets:
        payload["widgets"] = chart_widgets
    if result.chart_notes:
        # A refused chart spec is reported, never silently dropped.
        payload["chart_errors"] = result.chart_notes
    return payload


def _failure_payload(error: HarnessValidationError) -> dict[str, Any]:
    return {
        "error": {
            "stage": error.failure_stage,
            "code": error.error_code,
            "detail": str(error),
            "hint": "Correct the plan against the semantic catalog and call the tool again, or answer without it.",
        }
    }


def _template_run_tool(context: AnalysisToolContext, template) -> Any:
    def run_template(**filled: Any) -> dict[str, Any]:
        proposal = materialize_binding(
            template,
            dict(filled),
            today=context.today,
            timezone_name=context.timezone_name,
        )
        if proposal is None:
            return {"error": {
                "code": "template_binding_rejected",
                "detail": "The filled parameters did not survive type parsing and static policy checks.",
                "hint": f"Fill every parameter exactly, or author a fresh plan with {RUN_ANALYSIS_TOOL_NAME}.",
            }}
        try:
            outcome = execute_analysis_template(
                context.db,
                context.user_id,
                context.conversation_id,
                context.today,
                proposal,
                template.id,
                question=context.question,
            )
        except HarnessValidationError as error:
            return _failure_payload(error)
        context.citations.extend(outcome.result.citations)
        return _harness_payload(context, outcome)

    visible = _model_visible_parameters(template)
    properties = {
        _public_name(item["name"]): dict(_TYPE_SCHEMAS[item.get("type", "string")])
        for item in visible
    }
    parameter_lines = ", ".join(
        f"{item['name']} ({item.get('type', 'string')})" for item in visible
    ) or "none"
    return bind_schema_tool(
        run_template,
        name=bind_tool_name(template),
        description=(
            f"Run a validated stored analysis: {template.capability_name}. "
            f"{template.capability_description} Parameters: {parameter_lines}. "
            "Money parameters are integer minor units; dates are inclusive ISO dates. "
            "Call this only when it answers the request exactly."
        ),
        parameters={
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
        strict=True,
    )


def _repeat_run_tool(context: AnalysisToolContext, replay: AnalysisReplay) -> Any:
    """Expose an exact rebound plan as evidence, never as terminal prose."""

    def replay_validated_analysis() -> dict[str, Any]:
        try:
            outcome = execute_analysis_template(
                context.db,
                context.user_id,
                context.conversation_id,
                context.today,
                replay.proposal,
                replay.template_id,
                question=context.question,
            )
        except HarnessValidationError as error:
            return _failure_payload(error)
        context.citations.extend(outcome.result.citations)
        return _harness_payload(context, outcome)

    return bind_schema_tool(
        replay_validated_analysis,
        name=REPLAY_ANALYSIS_TOOL_NAME,
        description=(
            "Run the exact previously validated analysis plan for this identical question, "
            "with its relative dates rebound to today. Use its result as evidence, then "
            "compose an answer that addresses every constraint in the current question. "
            "The tool's generic message is evidence context, not a final answer."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        strict=True,
    )


def _run_analysis_tool(context: AnalysisToolContext) -> Any:
    def run_financial_analysis(name: str, intent_signature: str, plan_json: str) -> dict[str, Any]:
        try:
            plan_payload = json.loads(plan_json)
        except (json.JSONDecodeError, TypeError):
            return {"error": {
                "code": "invalid_plan_json",
                "detail": "plan_json must be a JSON object matching the AnalysisPlan contract.",
            }}
        try:
            proposal = AnalysisToolProposal.model_validate({
                "name": name[:120],
                "description": f"Governed analysis authored for: {intent_signature}."[:500],
                "intent_signature": intent_signature[:160],
                "plan": plan_payload,
            })
        except PydanticValidationError as error:
            return {"error": {
                "code": "invalid_analysis_plan",
                "detail": "; ".join(
                    f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                    for item in error.errors()[:8]
                ),
                "hint": "Correct the plan against the semantic catalog and call the tool again.",
            }}
        try:
            outcome = execute_analysis_template(
                context.db,
                context.user_id,
                context.conversation_id,
                context.today,
                proposal,
                question=context.question,
            )
        except HarnessValidationError as error:
            return _failure_payload(error)
        context.citations.extend(outcome.result.citations)
        return _harness_payload(context, outcome)

    return bind_schema_tool(
        run_financial_analysis,
        name=RUN_ANALYSIS_TOOL_NAME,
        description=(
            "Author and run a governed financial analysis over canonical records. "
            "plan_json is a JSON AnalysisPlan per the supplied semantic catalog: objective, "
            "analysis_type, queries[] (metric, dimensions, filters, start_date, end_date, order, "
            "limit, optional time_grouping), transforms[], context_sources[], service_inputs{}, "
            "safe_reasoning_summary[], and optional visualizations[]. The harness validates, "
            "executes with tenant scoping, and saves the plan as a reusable shared template. A "
            "result with an `error` key explains exactly which governed check failed so the plan "
            "can be corrected and retried.\n\n"
            + ANALYSIS_PLAN_CONTRACT
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short human title for the analysis."},
                "intent_signature": {"type": "string", "description": "Compact restatement of the analytical intent."},
                "plan_json": {"type": "string", "description": "The AnalysisPlan as a JSON object, serialized to a string."},
            },
            "required": ["name", "intent_signature", "plan_json"],
            "additionalProperties": False,
        },
        strict=True,
    )


def build_analysis_tools(
    context: AnalysisToolContext,
    exact_replay: AnalysisReplay | None = None,
) -> list[Any]:
    """Assemble this turn's dynamic analysis toolset.

    In the default SQL mode the model receives one maximally expressive
    tenant-governed query surface rather than the legacy finite transform
    grammar. Hybrid mode remains as a rollback/compatibility option.
    """
    settings = get_settings()
    sql_only = (
        settings.sql_lane_enabled
        and getattr(settings, "analysis_query_mode", "hybrid") == "sql"
        and getattr(settings, "primary_agent_enabled", True)
    )
    tools: list[Any] = []
    if not sql_only:
        if exact_replay is not None:
            tools.append(_repeat_run_tool(context, exact_replay))
        for item in retrieve_templates(context.db, context.user_id, context.question):
            if exact_replay is not None and item.template.id == exact_replay.template_id:
                # The no-argument rebound tool is the authoritative representation
                # of this exact template; exposing a second fillable copy invites
                # the model to re-author values the server has already derived.
                continue
            if tenancy_safe_parameters(item.template):
                tools.append(_template_run_tool(context, item.template))
        tools.append(_run_analysis_tool(context))
    if settings.sql_lane_enabled:
        tools.append(build_sql_analysis_tool(context))
    tools.extend(build_spreadsheet_tools(context))
    if settings.external_source_lane_enabled:
        tools.extend(build_external_tools(context))
    # Federation last: it presupposes both single-source lanes and is only
    # mounted when this user actually owns a non-native source to join to.
    if settings.federation_lane_enabled:
        tools.extend(build_federation_tool(context))
    # The Python lane reads no source of its own: it only computes over rows
    # the lanes above recorded, so it is mounted after all of them.
    if settings.python_lane_enabled:
        tools.append(build_python_analysis_tool(context))
    return tools
