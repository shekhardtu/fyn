"""Deterministic compilation and materialization for template binding.

The Binder model pass picks one retrieved template and fills its parameter
form; everything on either side of that single call is deterministic and lives
here. ``compile_bind_tools`` turns each candidate's stored parameter schema
into a strict tool (so the model can only produce a validatable fill), and
``materialize_binding`` turns a fill back into a fully bound
``AnalysisToolProposal`` — type-parsed, tenancy-checked, clamped, and passed
through the same static validation as Planner output. Any defect returns
``None`` and the ladder continues to the Planner: a wrong binding is never
reachable, only a slower answer.
"""
from __future__ import annotations

from datetime import date
import re
from typing import Any

from agno.tools.function import Function

from ..models import AnalysisToolTemplate
from .agent_tools import bind_schema_tool
from .analysis_harness import _materialize_template_node, validate_analysis_tool
from .semantic import AnalysisPlan, AnalysisToolProposal


ABSTAIN_TOOL_NAME = "abstain_no_exact_template"

# Trusted runtime bindings are harness-injected, never model-fillable: the
# timezone parameters are excluded from every compiled schema, and any template
# whose parameters fall outside the canonical namespaces is refused outright —
# that shape would be the only way a user identity could hide in a template.
_ALLOWED_PARAMETER_NAME = re.compile(r"(query_\d+|transform_\d+|service_inputs)\.[a-z0-9_.]+")
_SERVER_INJECTED_SUFFIXES = (".time_grouping.timezone", ".time_pivot.timezone")

_TYPE_SCHEMAS: dict[str, dict[str, Any]] = {
    "date": {"type": "string", "description": "Inclusive ISO date, YYYY-MM-DD."},
    "datetime": {"type": "string", "description": "ISO 8601 timestamp."},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "array": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}},
    "string": {"type": "string"},
}


def tenancy_safe_parameters(template: AnalysisToolTemplate) -> bool:
    return all(
        _ALLOWED_PARAMETER_NAME.fullmatch(str(item.get("name", "")))
        for item in template.parameter_schema
    )


def _public_name(parameter_name: str) -> str:
    # Dots are not valid keyword characters for provider tool schemas; the
    # double underscore is a safe bijection because canonical parameter names
    # never contain one.
    return parameter_name.replace(".", "__")


def _model_visible_parameters(template: AnalysisToolTemplate) -> list[dict[str, Any]]:
    return [
        item for item in template.parameter_schema
        if not str(item["name"]).endswith(_SERVER_INJECTED_SUFFIXES)
    ]


def bind_tool_name(template: AnalysisToolTemplate) -> str:
    return f"bind_template__{template.template_hash[:12]}"


def _accept_fill(**values: Any) -> dict[str, Any]:
    # The fill is read from the recorded tool call, not from this return value.
    return {"received": True}


def compile_bind_tools(
    candidates: list[AnalysisToolTemplate],
) -> tuple[list[Function], dict[str, AnalysisToolTemplate]]:
    """Compile candidates to strict fill tools plus a server-owned name map.

    The returned mapping is the only way a tool call resolves back to a
    template — a model-authored template id is never trusted.
    """
    tools: list[Function] = []
    mapping: dict[str, AnalysisToolTemplate] = {}
    for template in candidates:
        if not tenancy_safe_parameters(template):
            continue
        if any("__" in str(item["name"]) for item in template.parameter_schema):
            continue
        visible = _model_visible_parameters(template)
        properties = {
            _public_name(item["name"]): {
                "anyOf": [dict(_TYPE_SCHEMAS[item.get("type", "string")]), {"type": "null"}],
                "description": (
                    f"Value for {item['name']}. Use null only when the user did not state it."
                ),
            }
            for item in visible
        }
        schema = {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }
        parameter_lines = ", ".join(f"{item['name']} ({item.get('type', 'string')})" for item in visible) or "none"
        name = bind_tool_name(template)
        tool = bind_schema_tool(
            _accept_fill,
            name=name,
            description=(
                f"{template.capability_name}. {template.capability_description} "
                f"Signature: {template.capability_signature}. "
                f"Parameters: {parameter_lines}. "
                "Call this only when the template answers the request exactly; never guess a value."
            ),
            parameters=schema,
            strict=True,
            stop_after_tool_call=True,
        )
        tools.append(tool)
        mapping[name] = template
    abstain = bind_schema_tool(
        _accept_fill,
        name=ABSTAIN_TOOL_NAME,
        description=(
            "Declare that none of the offered templates answers the current request exactly, "
            "or that a required value was not stated by the user. Abstaining hands the request "
            "to the full planner."
        ),
        parameters={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
            "additionalProperties": False,
        },
        strict=True,
        stop_after_tool_call=True,
    )
    tools.append(abstain)
    return tools, mapping


def _parse_value(name: str, parameter_type: str, raw: Any, today: date) -> Any:
    if parameter_type == "date":
        parsed = date.fromisoformat(str(raw))
        # Only executed query windows are clamped; a prorate target range may
        # legitimately extend into the future.
        if re.fullmatch(r"query_\d+\.end_date", name) and parsed > today:
            parsed = today
        return parsed.isoformat()
    if parameter_type == "integer":
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError(f"{name} requires an integer")
        return raw
    if parameter_type == "number":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{name} requires a number")
        return raw
    if parameter_type == "boolean":
        if not isinstance(raw, bool):
            raise ValueError(f"{name} requires a boolean")
        return raw
    if parameter_type == "array":
        if not isinstance(raw, list):
            raise ValueError(f"{name} requires an array")
        return raw
    return str(raw)


def materialize_binding(
    template: AnalysisToolTemplate,
    filled: dict[str, Any],
    *,
    today: date,
    timezone_name: str,
) -> AnalysisToolProposal | None:
    """Deterministically rebuild a bound proposal from a model fill, or refuse."""
    if not tenancy_safe_parameters(template):
        return None
    try:
        values: dict[str, Any] = {}
        for item in template.parameter_schema:
            name = str(item["name"])
            parameter_type = str(item.get("type", "string"))
            if name.endswith(_SERVER_INJECTED_SUFFIXES):
                values[name] = timezone_name
                continue
            if parameter_type == "datetime":
                # Sub-day windows carry assumptions binding cannot verify.
                return None
            raw = filled.get(_public_name(name))
            if raw is None:
                return None
            values[name] = _parse_value(name, parameter_type, raw, today)
        plan_dict = _materialize_template_node(template.plan_template, values)
    except (KeyError, TypeError, ValueError):
        return None
    plan_dict["safe_reasoning_summary"] = [
        "Matched a validated shared analysis template to this request.",
        "Bound this run's dates and values into its parameters deterministically.",
    ]
    try:
        proposal = AnalysisToolProposal(
            name=template.capability_name[:120],
            description=template.capability_description[:500],
            intent_signature=template.capability_signature[:160],
            plan=AnalysisPlan.model_validate(plan_dict),
        )
    except Exception:
        return None
    if not validate_analysis_tool(proposal, today)["passed"]:
        return None
    return proposal
