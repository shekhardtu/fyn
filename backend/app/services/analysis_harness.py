from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import hashlib
import json
import re
from time import perf_counter
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..event_time import now_utc

from ..domain import AnalysisToolStatus, ExecutionStatus
from ..models import AnalysisToolRun, AnalysisToolTemplate, UserAnalysisTool
from .finance_time import names_absolute_finance_date, reanchor_finance_date, shift_month
from .intelligence import IntelligenceResult, execute_analysis_plan
from .manifest import NATIVE_SOURCE_KIND, native_manifest_fingerprint
from .semantic import AnalysisPlan, AnalysisToolProposal, AnalysisTransform, SemanticValidationError, validate_finance_query_plan
from .semantic_registry import semantic_schema_registry
from .template_retrieval import ANALYSIS_TEMPLATE_VERSION, _tokenize, retrieve_templates


HarnessCallback = Callable[[str, str, str, Optional[str]], None]
PARAMETER_REFERENCE_KEY = "$parameter"


class AnalysisTraceStage:
    INTENT_RESOLUTION = "intent_resolution"
    DATE_RESOLUTION = "date_resolution"
    TEMPLATE_CANDIDATES = "template_candidates"
    TEMPLATE_MATCH = "template_match"
    PARAMETER_BINDING = "parameter_binding"
    TEMPLATE_VALIDATION = "template_validation"
    TEMPLATE_REPAIR = "template_repair"
    TOOL_EXECUTION = "tool_execution"
    RESULT_VERIFICATION = "result_verification"


CURRENT_INCLUSIVE_THREE_MONTH_TOKENS = frozenset({"last", "three", "months"})
COMPLETE_PERIOD_TOKENS = frozenset({"complete", "full"})
DERIVED_ANALYSIS_TOKENS = frozenset({
    "compare", "comparison", "larger", "largest", "rank", "ranking",
    "share", "change", "trend", "drivers", "increase", "decrease",
    "project", "projected", "projection", "forecast", "prorate", "save", "savings",
})


class HarnessValidationError(ValueError):
    """The governed harness refused to produce a result.

    ``code`` is a stable machine label for the refusal family. The full
    human-readable reason lives in the durable stage trace (``agent_events``),
    which is the single source of truth for what failed; this exception only
    routes the failure to accurate run metadata.
    """

    # code -> (failure_stage, error_code) for the run record.
    ROUTING: dict[str, tuple[str, str]] = {
        "tool_not_available": (AnalysisTraceStage.TEMPLATE_CANDIDATES, "analysis_template_not_available"),
        "no_proposal": (AnalysisTraceStage.INTENT_RESOLUTION, "analysis_proposal_missing"),
        "rejected": (AnalysisTraceStage.TEMPLATE_VALIDATION, "analysis_plan_rejected"),
        "unverifiable": (AnalysisTraceStage.RESULT_VERIFICATION, "analysis_result_unverifiable"),
    }

    def __init__(self, message: str, *, code: str = "rejected") -> None:
        super().__init__(message)
        self.code = code if code in self.ROUTING else "rejected"

    @property
    def failure_stage(self) -> str:
        return self.ROUTING[self.code][0]

    @property
    def error_code(self) -> str:
        return self.ROUTING[self.code][1]


@dataclass
class HarnessResult:
    result: IntelligenceResult
    template: AnalysisToolTemplate
    user_tool: UserAnalysisTool
    run: AnalysisToolRun
    reused: bool


def _parameter_type(value: Any, *, semantic_type: str | None = None) -> str:
    if semantic_type:
        return semantic_type
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    return "string"


def _canonical_template(proposal: AnalysisToolProposal) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate a generated tool's reusable structure from per-run values.

    The stored artifact is safe to reuse because dates, filter values, limits,
    user timezone, and presentation text are parameters. The fully bound
    proposal remains the only executable object and is validated on every run.
    """

    parameters: list[dict[str, Any]] = []
    bindings: dict[str, Any] = {}

    def parameter(name: str, value: Any, *, semantic_type: str | None = None) -> dict[str, str]:
        parameters.append({
            "name": name,
            "type": _parameter_type(value, semantic_type=semantic_type),
            "required": True,
        })
        bindings[name] = value
        return {PARAMETER_REFERENCE_KEY: name}

    query_names = {
        query.name: f"query_{index + 1}"
        for index, query in enumerate(proposal.plan.queries)
    }
    transform_names = {
        transform.name: f"transform_{index + 1}"
        for index, transform in enumerate(proposal.plan.transforms)
    }
    queries: list[dict[str, Any]] = []
    for index, query in enumerate(proposal.plan.queries, start=1):
        prefix = f"query_{index}"
        item: dict[str, Any] = {
            "name": prefix,
            "metric": query.metric,
            "dimensions": list(query.dimensions),
            "relationships": list(query.relationships),
            "filters": [
                {
                    "field": finance_filter.field,
                    "operator": finance_filter.operator,
                    "value": parameter(
                        f"{prefix}.filter_{filter_index}.value",
                        finance_filter.model_dump(mode="json")["value"],
                    ),
                }
                for filter_index, finance_filter in enumerate(query.filters, start=1)
            ],
            "start_date": parameter(
                f"{prefix}.start_date", query.start_date.isoformat(), semantic_type="date"
            ),
            "end_date": parameter(
                f"{prefix}.end_date", query.end_date.isoformat(), semantic_type="date"
            ),
            "order": query.order,
            "limit": parameter(f"{prefix}.limit", query.limit),
        }
        if query.entity is not None:
            item["entity"] = query.entity
        if query.start_datetime is not None:
            item["start_datetime"] = parameter(
                f"{prefix}.start_datetime",
                query.start_datetime.isoformat(),
                semantic_type="datetime",
            )
            item["end_datetime"] = parameter(
                f"{prefix}.end_datetime",
                query.end_datetime.isoformat(),
                semantic_type="datetime",
            )
        if query.time_grouping is not None:
            item["time_grouping"] = {
                "field": query.time_grouping.field,
                "grain": query.time_grouping.grain,
                "timezone": parameter(
                    f"{prefix}.time_grouping.timezone", query.time_grouping.timezone
                ),
                "fill_gaps": query.time_grouping.fill_gaps,
            }
        if query.time_pivot is not None:
            item["time_pivot"] = {
                "row_grain": query.time_pivot.row_grain,
                "column_component": query.time_pivot.column_component,
                "timezone": parameter(
                    f"{prefix}.time_pivot.timezone", query.time_pivot.timezone
                ),
            }
        queries.append(item)

    transforms: list[dict[str, Any]] = []
    for index, transform in enumerate(proposal.plan.transforms, start=1):
        prefix = f"transform_{index}"
        item = {
            "name": prefix,
            "operation": transform.operation,
            "query_name": query_names[transform.query_name],
            "limit": parameter(f"{prefix}.limit", transform.limit),
            "window": parameter(f"{prefix}.window", transform.window),
        }
        if transform.dimension is not None:
            item["dimension"] = transform.dimension
        if transform.secondary_query_name is not None:
            item["secondary_query_name"] = query_names[transform.secondary_query_name]
        if transform.secondary_transform_name is not None:
            item["secondary_transform_name"] = transform_names[
                transform.secondary_transform_name
            ]
        if transform.period_dimension is not None:
            item["period_dimension"] = transform.period_dimension
        if transform.target_start_date is not None:
            item["target_start_date"] = parameter(
                f"{prefix}.target_start_date",
                transform.target_start_date.isoformat(),
                semantic_type="date",
            )
            item["target_end_date"] = parameter(
                f"{prefix}.target_end_date",
                transform.target_end_date.isoformat(),
                semantic_type="date",
            )
        transforms.append(item)

    registry = semantic_schema_registry()
    plan_template: dict[str, Any] = {
        "objective": proposal.plan.objective,
        "analysis_type": proposal.plan.analysis_type,
        "queries": queries,
        "transforms": transforms,
        "context_sources": list(proposal.plan.context_sources),
    }
    # Emitted only when present so every pre-existing template hash is stable.
    if proposal.plan.service_inputs:
        plan_template["service_inputs"] = {
            key: parameter(f"service_inputs.{key}", proposal.plan.service_inputs[key])
            for key in sorted(proposal.plan.service_inputs)
        }
    specification = {
        "templateVersion": ANALYSIS_TEMPLATE_VERSION,
        "sourceManifest": {
            "kind": NATIVE_SOURCE_KIND,
            "semanticVersion": registry.version,
            "hash": native_manifest_fingerprint(),
        },
        "parameterSchema": parameters,
        "planTemplate": plan_template,
    }
    return specification, bindings


def _specification_hash(specification: dict[str, Any]) -> str:
    encoded = json.dumps(specification, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _uses_current_manifest(template: AnalysisToolTemplate) -> bool:
    registry = semantic_schema_registry()
    return (
        template.template_version == ANALYSIS_TEMPLATE_VERSION
        and template.semantic_registry_version == registry.version
        and template.source_manifest_hash == native_manifest_fingerprint()
    )


def _analysis_templates(db: Session, *, active_only: bool = False):
    statement = select(AnalysisToolTemplate)
    if active_only:
        statement = statement.where(AnalysisToolTemplate.status == AnalysisToolStatus.ACTIVE)
    return statement


def delete_obsolete_analysis_templates(db: Session) -> int:
    """Remove stale cache definitions while preserving user-scoped run traces."""
    obsolete = [
        template
        for template in db.scalars(select(AnalysisToolTemplate))
        if not _uses_current_manifest(template)
    ]
    if not obsolete:
        return 0
    template_ids = [template.id for template in obsolete]
    db.execute(delete(UserAnalysisTool).where(UserAnalysisTool.template_id.in_(template_ids)))
    for run in db.scalars(
        select(AnalysisToolRun).where(AnalysisToolRun.template_id.in_(template_ids))
    ):
        run.template_id = None
        run.user_tool_id = None
    db.execute(delete(AnalysisToolTemplate).where(AnalysisToolTemplate.id.in_(template_ids)))
    db.flush()
    return len(template_ids)


def _non_month_dimension(query, *, fallback_to_first: bool = False) -> str | None:
    dimension = next(
        (item for item in reversed(query.dimensions) if item != "month"),
        None,
    )
    return dimension or (query.dimensions[0] if fallback_to_first else None)


def _capability_metadata(proposal: AnalysisToolProposal) -> tuple[str, str, str]:
    """Build shared retrieval text from structure only, never customer values."""
    plan = proposal.plan
    metrics = sorted({query.metric for query in plan.queries})
    dimensions = sorted({item for query in plan.queries for item in query.dimensions})
    filter_fields = sorted({item.field for query in plan.queries for item in query.filters})
    operations = [transform.operation for transform in plan.transforms]
    name = " ".join([
        plan.objective.replace("_", " ").title(),
        "/".join(metric.replace("_", " ") for metric in metrics) or plan.analysis_type.replace("_", " "),
        "analysis",
    ])[:120]
    parts = [
        f"objective {plan.objective}",
        f"analysis {plan.analysis_type}",
        *(f"metric {metric}" for metric in metrics),
        *(f"dimension {dimension}" for dimension in dimensions),
        *(f"filter {field}" for field in filter_fields),
        *(f"transform {operation}" for operation in operations),
    ]
    signature = " | ".join(parts)[:240]
    description = (
        "Reusable read-only finance analysis with runtime-bound dates, filters, "
        "limits, timezone, and presentation values. " + signature
    )
    return name, description, signature


def discover_analysis_templates(db: Session, user_id: UUID, prompt: str, limit: int = 5) -> list[dict[str, Any]]:
    """Retrieve shared templates; no user data or prior bindings leave their scope.

    Thin adapter over the hybrid retriever, kept for the Planner-context call
    site: it prunes obsolete templates first and renders candidates as plain
    prompt-safe dictionaries.
    """
    delete_obsolete_analysis_templates(db)
    return [
        {
            "template_id": str(item.template.id),
            "capability_name": item.template.capability_name,
            "capability_description": item.template.capability_description,
            "capability_signature": item.template.capability_signature,
            "template_version": item.template.template_version,
            "parameter_schema": item.template.parameter_schema,
            "plan_template": item.template.plan_template,
            "saved_by_user": item.saved_by_user,
            "retrieval_score": round(item.fused_score, 4),
        }
        for item in retrieve_templates(db, user_id, prompt, limit=limit)
    ]


def normalize_replay_question(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", text.casefold()))


def analysis_question_hash(text: str) -> str:
    return hashlib.sha256(normalize_replay_question(text).encode()).hexdigest()


class ReplayDisposition(str, Enum):
    """Whether a rebound plan is itself an answer or only reusable work."""

    FINAL = "final"
    COMPOSE = "compose"


_SELF_CONTAINED_DESCRIPTIVE_QUESTION = re.compile(
    r"^(?:how\s+(?:much|many)|what\s+(?:is|are|was|were|did)|"
    r"show|list|display|give\s+me|which|where\s+did|top|break\s*down|"
    r"chart|graph|plot)\b",
    re.I,
)
_COMPOSITION_REQUEST = re.compile(
    r"\b(?:guarantee|guaranteed|ensure|certain(?:ty)?|promise|recommend|advice|"
    r"should|could\s+i|can\s+i|safe\s+to|afford|why|explain|forecast|predict|"
    r"project(?:ion|ed)?|strategy|plan\s+to|how\s+can\s+i)\b",
    re.I,
)
_CONTEXT_DEPENDENT_REPLAY = re.compile(
    r"\b(?:that|those|them|it|same|above|earlier|previous\s+(?:one|answer|result)|"
    r"what\s+about|how\s+about|instead|as\s+before)\b",
    re.I,
)


def question_requires_answer_composition(
    question: str,
    proposal: AnalysisToolProposal | None = None,
) -> bool:
    """Conservatively reserve terminal replay for answer-complete reads.

    A template proves that a computation is executable, not that its generic
    renderer fulfils an interpretive request.  Only self-contained descriptive
    reads may terminate at the harness; every other result returns to the
    Operator for question-aware composition.
    """
    if proposal is not None and proposal.plan.objective != "descriptive":
        return True
    normalized = " ".join(question.split())
    return bool(
        not _SELF_CONTAINED_DESCRIPTIVE_QUESTION.search(normalized)
        or _COMPOSITION_REQUEST.search(normalized)
        or _CONTEXT_DEPENDENT_REPLAY.search(normalized)
    )


def _materialize_template_node(node: Any, values: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if set(node) == {PARAMETER_REFERENCE_KEY}:
            return values[node[PARAMETER_REFERENCE_KEY]]
        return {key: _materialize_template_node(item, values) for key, item in node.items()}
    if isinstance(node, list):
        return [_materialize_template_node(item, values) for item in node]
    return node


@dataclass
class AnalysisReplay:
    proposal: AnalysisToolProposal
    template_id: UUID
    source_run_id: UUID
    disposition: ReplayDisposition


def bind_repeat_analysis(db: Session, user_id: UUID, question: str, today: date) -> AnalysisReplay | None:
    """Deterministically re-bind a stored template for an exact repeat question.

    The planner is a compiler: it earns its latency once per novel intent.
    When the same user repeats a question whose prior run succeeded against a
    still-active template, every parameter is machine-derivable — dates
    re-anchor to today by their recorded relationship to the prior run date,
    and every other bound value carries over verbatim from that user's own
    run. Any ambiguity (absolute month or year wording, an unrecognized date
    anchor, sub-day windows) returns None and the planner runs as usual.
    """
    normalized = normalize_replay_question(question)
    if (
        not normalized
        or names_absolute_finance_date(normalized)
        or _CONTEXT_DEPENDENT_REPLAY.search(question)
    ):
        return None
    question_hash = analysis_question_hash(question)
    prior = db.scalar(
        select(AnalysisToolRun)
        .where(
            AnalysisToolRun.user_id == user_id,
            AnalysisToolRun.status == ExecutionStatus.COMPLETED,
            AnalysisToolRun.question_hash == question_hash,
            AnalysisToolRun.template_id.is_not(None),
            AnalysisToolRun.run_date.is_not(None),
        )
        .order_by(AnalysisToolRun.created_at.desc())
        .limit(1)
    )
    if not prior:
        return None
    template = db.scalar(
        _analysis_templates(db, active_only=True).where(
            AnalysisToolTemplate.id == prior.template_id
        )
    )
    if not template or not _uses_current_manifest(template):
        return None
    # Imported here: agents imports this module's contracts, so the predicate
    # travels to the caller rather than the module to the predicate.
    from .agents import requests_chart

    if requests_chart(question) and not (template.plan_template or {}).get("visualizations"):
        # A stored plan that draws nothing cannot satisfy a request to draw.
        # Replay is a latency optimization, never a downgrade of the ask: the
        # first chartless answer to "chart my spending" would otherwise be
        # replayed forever, permanently answering a chart request with a table.
        return None
    try:
        parameter_types = {item["name"]: item.get("type") for item in template.parameter_schema}
        if "datetime" in parameter_types.values():
            return None
        values: dict[str, Any] = {}
        for name, parameter_type in parameter_types.items():
            if name not in prior.parameters:
                return None
            stored = prior.parameters[name]
            if parameter_type == "date":
                rebound = reanchor_finance_date(date.fromisoformat(str(stored)), prior.run_date, today)
                if rebound is None:
                    return None
                values[name] = rebound.isoformat()
            else:
                values[name] = stored
        display_names = prior.display_names or {}
        plan_dict = _materialize_template_node(template.plan_template, values)
        for query in plan_dict.get("queries", []):
            query["name"] = display_names.get(query["name"], query["name"])
        for transform in plan_dict.get("transforms", []):
            transform["name"] = display_names.get(transform["name"], transform["name"])
            for reference in ("query_name", "secondary_query_name", "secondary_transform_name"):
                if transform.get(reference):
                    transform[reference] = display_names.get(transform[reference], transform[reference])
    except (KeyError, TypeError, ValueError):
        return None
    plan_dict["safe_reasoning_summary"] = [
        "Replayed this user's previously validated plan for the identical question.",
        f"Re-anchored its date windows from {prior.run_date.isoformat()} to {today.isoformat()} deterministically.",
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
    disposition = (
        ReplayDisposition.COMPOSE
        if question_requires_answer_composition(question, proposal)
        else ReplayDisposition.FINAL
    )
    return AnalysisReplay(
        proposal=proposal,
        template_id=template.id,
        source_run_id=prior.id,
        disposition=disposition,
    )


def validate_analysis_tool(proposal: AnalysisToolProposal, today: date) -> dict[str, Any]:
    """Apply static policy checks before any query is executed."""
    plan = proposal.plan
    checks: list[dict[str, Any]] = [
        {"name": "declarative_only", "passed": True, "detail": "The proposal contains no code or SQL."},
        {"name": "tenant_scope", "passed": True, "detail": "The compiler injects the authenticated user id."},
        {"name": "read_only", "passed": True, "detail": "Only governed SELECT operations are available."},
    ]
    if plan.missing_information:
        checks.append({"name": "required_inputs", "passed": False, "detail": "Required information is still missing."})
    else:
        checks.append({"name": "required_inputs", "passed": True, "detail": "The plan has enough inputs to execute."})
    if plan.analysis_type == "semantic_query":
        query_count = len(plan.queries)
        checks.append({
            "name": "query_presence",
            "passed": query_count > 0,
            "detail": (
                f"{query_count} governed semantic queries supplied."
                if query_count
                else "The plan did not include a governed financial-data query."
            ),
        })
        semantic_errors: list[str] = []
        registry_versions: set[str] = set()
        for query in plan.queries:
            try:
                validation = validate_finance_query_plan(query)
                registry_versions.add(validation["registry_version"])
            except SemanticValidationError as exc:
                semantic_errors.append(f"{query.name}: {exc}")
        checks.append({
            "name": "semantic_schema",
            "passed": not semantic_errors,
            "detail": (
                f"All queries conform to semantic registry {', '.join(sorted(registry_versions))}."
                if not semantic_errors
                else "; ".join(semantic_errors)
            ),
        })
    else:
        checks.append({
            "name": "domain_service",
            "passed": True,
            "detail": f"The {plan.analysis_type} capability executes through a deterministic domain service.",
        })
    has_declarative_execution = bool(plan.queries or plan.transforms or plan.visualizations)
    execution_shape_valid = (
        plan.analysis_type == "semantic_query" or not has_declarative_execution
    )
    checks.append({
        "name": "execution_shape",
        "passed": execution_shape_valid,
        "detail": (
            "Declarative queries, transforms, and views execute through semantic_query."
            if execution_shape_valid and plan.analysis_type == "semantic_query"
            else f"The {plan.analysis_type} domain service owns its fixed execution shape."
            if execution_shape_valid
            else (
                f"The plan declared {plan.analysis_type} but also supplied custom queries, transforms, "
                "or views; executing that service would ignore the declared analysis."
            )
        ),
    })
    future_queries = [query.name for query in plan.queries if query.end_date > today]
    checks.append({
        "name": "historical_window",
        "passed": not future_queries,
        "detail": "Historical queries do not include future records." if not future_queries else f"Future range requested by: {', '.join(future_queries)}",
    })
    driver_roles_valid = all(
        transform.operation != "change_drivers"
        or (transform.period_dimension == "month" and transform.dimension != "month")
        for transform in plan.transforms
    )
    checks.append({
        "name": "change_driver_roles",
        "passed": driver_roles_valid,
        "detail": "Time is the period axis and a financial attribute is the driver axis." if driver_roles_valid else "A change-driver transform reversed its time and driver axes.",
    })
    intent_words = _tokenize(f"{proposal.name} {proposal.intent_signature} {proposal.description}")
    current_inclusive_three_months = CURRENT_INCLUSIVE_THREE_MONTH_TOKENS.issubset(intent_words) and not (COMPLETE_PERIOD_TOKENS & intent_words)
    expected_start = shift_month(today.replace(day=1), -2)
    rolling_window_valid = not current_inclusive_three_months or all(
        query.start_date == expected_start and query.end_date == today for query in plan.queries
    )
    checks.append({
        "name": "relative_period_policy",
        "passed": rolling_window_valid,
        "detail": "Relative month windows follow the current-inclusive product policy." if rolling_window_valid else f"Last three months must be {expected_start.isoformat()} through {today.isoformat()} unless complete months were requested.",
    })
    recommendation_grounded = plan.objective != "recommendation" or bool(plan.context_sources) or plan.analysis_type in {"avoidable_expenses", "loan_strategy", "three_month_allocation"}
    checks.append({
        "name": "recommendation_context",
        "passed": recommendation_grounded,
        "detail": "Recommendations include user-specific planning context." if plan.objective == "recommendation" and recommendation_grounded else "No recommendation context is required." if plan.objective != "recommendation" else "The recommendation has no goals, budgets, accounts, loans, or recurring-expense context.",
    })
    estimated_rows = sum(query.limit for query in plan.queries)
    checks.append({
        "name": "bounded_result",
        "passed": estimated_rows <= 800,
        "detail": f"At most {estimated_rows} aggregate rows can be returned.",
    })
    if plan.analysis_type == "affordability":
        purchase_minor = plan.service_inputs.get("purchase_minor")
        service_inputs_valid = (
            set(plan.service_inputs) == {"purchase_minor"}
            and isinstance(purchase_minor, int)
            and purchase_minor > 0
        )
        service_inputs_detail = (
            "The affordability service received a positive purchase amount."
            if service_inputs_valid
            else "affordability requires exactly service_inputs.purchase_minor as a positive integer in minor units."
        )
    else:
        service_inputs_valid = not plan.service_inputs
        service_inputs_detail = (
            "No service inputs are required."
            if service_inputs_valid
            else f"The {plan.analysis_type} service does not accept service_inputs."
        )
    checks.append({
        "name": "service_inputs",
        "passed": service_inputs_valid,
        "detail": service_inputs_detail,
    })
    intent_tokens = _tokenize(f"{proposal.name} {proposal.intent_signature}")
    needs_transform = bool(intent_tokens & DERIVED_ANALYSIS_TOKENS)
    checks.append({
        "name": "analytical_completion",
        "passed": not needs_transform or bool(plan.transforms) or plan.analysis_type != "semantic_query",
        "detail": (
            "A deterministic result transform completes the requested comparison."
            if needs_transform and plan.transforms
            else "No derived comparison was requested."
            if not needs_transform
            else "The requested comparison is missing a deterministic result transform."
        ),
    })
    passed = all(check["passed"] for check in checks)
    return {"passed": passed, "checks": checks, "validated_at": now_utc().isoformat()}


def _repair_incomplete_analysis(proposal: AnalysisToolProposal, today: date) -> AnalysisToolProposal | None:
    """Repair narrow deterministic contract defects; never invent missing facts."""
    plan = proposal.plan
    if plan.missing_information:
        return None
    if any(query.start_date > today for query in plan.queries):
        # A query that lies entirely in the future has no evidence to bound
        # it to; nothing mechanical can repair that.
        return None
    repaired = proposal.model_copy(deep=True)
    changed = False
    if repaired.plan.analysis_type != "semantic_query":
        if not repaired.plan.queries:
            return None
        # A populated declarative plan is self-describing. A dedicated service
        # label would discard it wholesale, so normalize the executor family;
        # no customer fact or financial assumption is invented by this repair.
        repaired.plan.analysis_type = "semantic_query"
        changed = True
    # A month named in full ("August 2026") legitimately ends after today when
    # asked mid-month. The evidence-bounded reading is the product's own date
    # policy, so the window clamps to today instead of the whole analysis
    # being refused — the repair the old blanket future-date bail-out blocked.
    for query in repaired.plan.queries:
        if query.end_date > today:
            query.end_date = today
            changed = True
    intent_tokens = _tokenize(f"{proposal.name} {proposal.intent_signature}")
    all_intent_tokens = _tokenize(f"{proposal.name} {proposal.intent_signature} {proposal.description}")
    if not repaired.plan.transforms and intent_tokens & {
        "project", "projected", "projection", "forecast", "prorate", "save", "savings",
    }:
        # The source and target periods are financial assumptions, not a
        # mechanical repair. Planner must express them explicitly with the
        # governed prorate primitive before this tool can execute.
        return None
    if CURRENT_INCLUSIVE_THREE_MONTH_TOKENS.issubset(all_intent_tokens) and not (COMPLETE_PERIOD_TOKENS & all_intent_tokens):
        expected_start = shift_month(today.replace(day=1), -2)
        for query in repaired.plan.queries:
            if query.start_date != expected_start or query.end_date != today:
                query.start_date = expected_start
                query.end_date = today
                changed = True
    # `time_bucket` is the *output* of the time_grouping operator, but planner
    # models keep writing it into dimensions when asked for monthly results.
    # Without a time_grouping that shape has exactly one governed meaning — the
    # month dimension — so normalizing it is a repair, not a guess.
    for query in repaired.plan.queries:
        if "time_bucket" in query.dimensions and not query.time_grouping:
            query.dimensions = list(dict.fromkeys(
                "month" if name == "time_bucket" else name
                for name in query.dimensions
            ))
            changed = True
            for transform in repaired.plan.transforms:
                if transform.query_name != query.name:
                    continue
                if transform.dimension == "time_bucket":
                    transform.dimension = "month"
                if transform.period_dimension == "time_bucket":
                    transform.period_dimension = "month"
    queries = {query.name: query for query in repaired.plan.queries}
    for transform in repaired.plan.transforms:
        query = queries.get(transform.query_name)
        if transform.operation == "change_drivers" and query and "month" in query.dimensions:
            driver = _non_month_dimension(query)
            if driver and (transform.period_dimension != "month" or transform.dimension == "month"):
                transform.period_dimension = "month"
                transform.dimension = driver
                changed = True
    if not repaired.plan.transforms:
        derived_tokens = intent_tokens & DERIVED_ANALYSIS_TOKENS
        query = next((item for item in repaired.plan.queries if item.dimensions), None)
        if derived_tokens and query:
            period_dimension = None
            if intent_tokens & {"drivers"} and "month" in query.dimensions and len(query.dimensions) > 1:
                operation = "change_drivers"
                period_dimension = "month"
                dimension = _non_month_dimension(query)
            elif intent_tokens & {"change", "trend", "increase", "decrease"} and "month" in query.dimensions:
                operation = "period_change"
                dimension = "month"
            elif intent_tokens & {"share"}:
                operation = "share_of_total"
                dimension = _non_month_dimension(query, fallback_to_first=True)
            elif intent_tokens & {"rank", "ranking", "largest"}:
                operation = "rank"
                dimension = _non_month_dimension(query, fallback_to_first=True)
            else:
                operation = "compare_totals"
                # A time-grouped query compares across its produced buckets
                # (July vs August), not across an incidental dimension such as
                # currency.
                dimension = (
                    "time_bucket"
                    if query.time_grouping
                    else _non_month_dimension(query, fallback_to_first=True)
                )
            repaired.plan.transforms = [AnalysisTransform(
                name=f"{proposal.name} deterministic result",
                operation=operation,
                query_name=query.name,
                dimension=dimension,
                period_dimension=period_dimension,
            )]
            changed = True
    return AnalysisToolProposal.model_validate(repaired.model_dump()) if changed else None


def _verify_result(proposal: AnalysisToolProposal, result: IntelligenceResult) -> dict[str, Any]:
    checks = [
        {
            "name": "rendered_result",
            "passed": bool(result.message.strip() or result.widgets),
            "detail": "The result carries rendered markdown or a typed interactive widget.",
        },
        {"name": "evidence_lineage", "passed": bool(result.citations), "detail": f"Returned {len(result.citations)} structured data reference(s)."},
        {
            "name": "nonempty_message",
            "passed": bool(result.message.strip()),
            "detail": "A user-facing summary was produced." if result.message.strip() else "No user-facing summary was produced.",
        },
    ]
    if proposal.plan.analysis_type == "semantic_query":
        semantic_citations = [citation for citation in result.citations if citation.entity_type == "semantic_query"]
        checks.append({
            "name": "query_lineage",
            "passed": len(semantic_citations) == len(proposal.plan.queries),
            "detail": (
                "Every generated semantic query has a data reference."
                if len(semantic_citations) == len(proposal.plan.queries)
                else f"{len(semantic_citations)} of {len(proposal.plan.queries)} generated semantic queries have a data reference."
            ),
        })
        # Postconditions verify the executor's own results: markdown rendering
        # carries no machine-readable payload the way widget JSON did.
        query_results = {
            item.get("name"): item for item in result.query_results if item.get("name")
        }
        bounded = True
        ordered = True
        exclusions_preserved = True
        for query in proposal.plan.queries:
            rows = query_results.get(query.name, {}).get("rows", [])
            bounded = bounded and len(rows) <= query.limit
            if query.time_grouping or query.time_pivot:
                values = [str(row.get("time_bucket", "")) for row in rows]
            else:
                values = [int(row.get("value", 0)) for row in rows]
            ordered_values = sorted(values, reverse=query.order == "desc")
            ordered = ordered and values == ordered_values
            for item in query.filters:
                if item.operator not in {"neq", "not_in"} or item.field not in query.dimensions:
                    continue
                excluded = item.value if isinstance(item.value, list) else [item.value]
                normalized = {str(value).casefold() for value in excluded}
                exclusions_preserved = exclusions_preserved and all(
                    str(row.get(item.field, "")).casefold() not in normalized for row in rows
                )
        checks.extend([
            {"name": "result_limit", "passed": bounded, "detail": "Every result respects its governed row limit."},
            {"name": "result_order", "passed": ordered, "detail": "Every result respects its governed metric or temporal ordering."},
            {"name": "negative_filters", "passed": exclusions_preserved, "detail": "Excluded dimension values are absent from result rows."},
        ])
    return {"passed": all(check["passed"] for check in checks), "checks": checks}


def execute_analysis_template(
    db: Session,
    user_id: UUID,
    # None for executions that belong to no thread, such as a dashboard tile
    # read; the run row records the missing conversation honestly.
    conversation_id: UUID | None,
    today: date,
    proposal: AnalysisToolProposal | None,
    candidate_template_id: UUID | None = None,
    callback: HarnessCallback | None = None,
    question: str | None = None,
) -> HarnessResult:
    """Bind, validate, execute, and audit one shared analysis template."""
    started = perf_counter()
    trace: list[dict[str, Any]] = []

    def stage(
        stage_id: str,
        label: str,
        status: str,
        detail: str | None = None,
        values: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "stage": stage_id,
            "status": status,
            "detail": detail,
            "recordedAt": now_utc().isoformat(),
        }
        if values is not None:
            item["values"] = values
        trace.append(item)
        if callback:
            callback(stage_id, label, status, detail)

    reused = False
    template: AnalysisToolTemplate | None = None
    user_tool: UserAnalysisTool | None = None
    run = AnalysisToolRun(
        user_id=user_id,
        conversation_id=conversation_id,
        status=ExecutionStatus.RUNNING,
        # Customer prose stays single-sourced in Message; the run stores only
        # its deterministic replay identity.
        question_hash=analysis_question_hash(question) if question else None,
        run_date=today,
        parameters={},
        trace=[],
    )
    db.add(run)
    db.flush()
    try:
        delete_obsolete_analysis_templates(db)
        if not proposal:
            stage(
                AnalysisTraceStage.INTENT_RESOLUTION,
                "No executable analysis intent was supplied",
                "failed",
                "The request did not produce a complete bound analysis plan, so no template was selected or executed.",
            )
            raise HarnessValidationError(
                "The agent did not supply a bound analysis tool proposal",
                code="no_proposal",
            )

        stage(
            AnalysisTraceStage.INTENT_RESOLUTION,
            "Resolved the customer request",
            "completed",
            f"{proposal.intent_signature}: {proposal.description}",
            {"intentSignature": proposal.intent_signature, "objective": proposal.plan.objective},
        )
        date_windows = [
            {
                "query": query.name,
                "startDate": query.start_date.isoformat(),
                "endDate": query.end_date.isoformat(),
                "timezone": (
                    query.time_grouping.timezone
                    if query.time_grouping
                    else query.time_pivot.timezone
                    if query.time_pivot
                    else None
                ),
            }
            for query in proposal.plan.queries
        ]
        stage(
            AnalysisTraceStage.DATE_RESOLUTION,
            "Resolved inclusive financial date windows",
            "completed",
            "; ".join(
                f"{item['query']}: {item['startDate']} through {item['endDate']}"
                for item in date_windows
            ) or "This analysis does not query dated financial records.",
            date_windows,
        )

        stage(AnalysisTraceStage.TEMPLATE_VALIDATION, "Validating the bound analysis plan", "running")
        validation = validate_analysis_tool(proposal, today)
        if not validation["passed"]:
            repaired = _repair_incomplete_analysis(proposal, today)
            if not repaired:
                failed_checks = [
                    str(check["detail"]).rstrip(".")
                    for check in validation["checks"]
                    if not check["passed"]
                ]
                failure_detail = (
                    f"Analysis plan rejected: {'; '.join(failed_checks)}."
                    if failed_checks
                    else "Analysis plan rejected: one or more safety or completeness checks failed."
                )
                stage(
                    AnalysisTraceStage.TEMPLATE_VALIDATION,
                    "The analysis plan was rejected",
                    "failed",
                    failure_detail,
                    {"failedChecks": failed_checks},
                )
                raise HarnessValidationError("Generated analysis tool failed validation")
            stage(
                AnalysisTraceStage.TEMPLATE_VALIDATION,
                "The analysis plan needs a deterministic repair",
                "completed",
                "The harness can complete the requested derived analysis without changing customer data.",
            )
            stage(AnalysisTraceStage.TEMPLATE_REPAIR, "Repairing the analysis contract", "running")
            proposal = repaired
            validation = validate_analysis_tool(proposal, today)
            if not validation["passed"]:
                repair_failures = [
                    str(check["detail"]).rstrip(".")
                    for check in validation["checks"]
                    if not check["passed"]
                ]
                stage(
                    AnalysisTraceStage.TEMPLATE_REPAIR,
                    "The repaired analysis plan was rejected",
                    "failed",
                    "Analysis plan rejected after repair: " + "; ".join(repair_failures) + ".",
                    {"failedChecks": repair_failures},
                )
                raise HarnessValidationError("Generated analysis tool failed validation after repair")
            stage(
                AnalysisTraceStage.TEMPLATE_REPAIR,
                "The repaired analysis plan passed validation",
                "completed",
                f"{len(validation['checks'])} policy checks passed.",
            )
        else:
            stage(
                AnalysisTraceStage.TEMPLATE_VALIDATION,
                "The bound analysis plan passed validation",
                "completed",
                f"{len(validation['checks'])} policy checks passed.",
            )

        specification, runtime_bindings = _canonical_template(proposal)
        fingerprint = _specification_hash(specification)
        run.parameters = runtime_bindings
        # Recorded after validation and repair so the map names the plan that
        # actually executed. These keys mirror _canonical_template's ordering.
        run.display_names = {
            **{f"query_{index + 1}": query.name for index, query in enumerate(proposal.plan.queries)},
            **{f"transform_{index + 1}": transform.name for index, transform in enumerate(proposal.plan.transforms)},
        }
        stage(
            AnalysisTraceStage.PARAMETER_BINDING,
            "Bound current request values to template parameters",
            "completed",
            f"Bound {len(runtime_bindings)} current-run values; none are stored in the shared template.",
            runtime_bindings,
        )

        candidate: AnalysisToolTemplate | None = None
        if candidate_template_id:
            candidate = db.scalar(
                _analysis_templates(db, active_only=True).where(
                    AnalysisToolTemplate.id == candidate_template_id
                )
            )
        stage(
            AnalysisTraceStage.TEMPLATE_CANDIDATES,
            "Checked the retrieved template candidate",
            "completed",
            (
                f"Candidate {candidate_template_id} is available for structural validation."
                if candidate
                else "No usable candidate was supplied; the harness checked the shared registry by structure."
            ),
            {"candidateTemplateId": str(candidate_template_id) if candidate_template_id else None},
        )
        if candidate and candidate.template_hash == fingerprint:
            template = candidate
            reused = True
            stage(
                AnalysisTraceStage.TEMPLATE_MATCH,
                "The candidate exactly matches the current ask",
                "completed",
                "All metrics, dimensions, filters, transforms, and output structure match; current values were rebound.",
                {"templateId": str(template.id), "matched": True},
            )
        else:
            if candidate:
                stage(
                    AnalysisTraceStage.TEMPLATE_MATCH,
                    "The candidate does not match the current ask",
                    "completed",
                    "Its governed structure differs, so it was not executed for this request.",
                    {"templateId": str(candidate.id), "matched": False},
                )
            template = db.scalar(
                select(AnalysisToolTemplate).where(
                    AnalysisToolTemplate.template_hash == fingerprint,
                    AnalysisToolTemplate.status == AnalysisToolStatus.ACTIVE,
                )
            )
            if template:
                reused = True
                stage(
                    AnalysisTraceStage.TEMPLATE_MATCH,
                    "Found an identical shared template",
                    "completed",
                    "The structure matches exactly; only this run's user-scoped values and data will be used.",
                    {"templateId": str(template.id), "matched": True},
                )
            else:
                manifest_identity = specification["sourceManifest"]
                capability_name, capability_description, capability_signature = _capability_metadata(proposal)
                template = AnalysisToolTemplate(
                    capability_name=capability_name,
                    capability_description=capability_description,
                    capability_signature=capability_signature,
                    template_version=specification["templateVersion"],
                    status=AnalysisToolStatus.ACTIVE,
                    semantic_registry_version=manifest_identity["semanticVersion"],
                    source_manifest_hash=manifest_identity["hash"],
                    parameter_schema=specification["parameterSchema"],
                    plan_template=specification["planTemplate"],
                    template_hash=fingerprint,
                    validation_report={
                        "passed": True,
                        "checks": [
                            {"name": check["name"], "passed": check["passed"]}
                            for check in validation["checks"]
                        ],
                    },
                    created_by_user_id=user_id,
                )
                db.add(template)
                db.flush()
                stage(
                    AnalysisTraceStage.TEMPLATE_MATCH,
                    "Created a new shared template",
                    "completed",
                    "No existing structure matched. The new definition contains parameters only and is reusable across users.",
                    {"templateId": str(template.id), "matched": False, "created": True},
                )

        user_tool = db.scalar(
            select(UserAnalysisTool).where(
                UserAnalysisTool.user_id == user_id,
                UserAnalysisTool.template_id == template.id,
            )
        )
        if not user_tool:
            user_tool = UserAnalysisTool(
                user_id=user_id,
                template_id=template.id,
                name=proposal.name,
                description=proposal.description,
                intent_signature=proposal.intent_signature.casefold().strip(),
                status=AnalysisToolStatus.ACTIVE,
            )
            db.add(user_tool)
            db.flush()
        run.template_id = template.id
        run.user_tool_id = user_tool.id

        stage(AnalysisTraceStage.TOOL_EXECUTION, "Executing governed finance queries", "running")
        with db.begin_nested():
            result = execute_analysis_plan(db, user_id, today, proposal.plan)
        stage(
            AnalysisTraceStage.TOOL_EXECUTION,
            "Governed analysis completed",
            "completed",
            f"Returned {len(result.widgets)} typed result widget(s) from user-scoped records.",
        )
        stage(AnalysisTraceStage.RESULT_VERIFICATION, "Verifying evidence and result contract", "running")
        verification = _verify_result(proposal, result)
        if not verification["passed"]:
            verification_failures = [
                check["detail"]
                for check in verification["checks"]
                if not check["passed"]
            ]
            stage(
                AnalysisTraceStage.RESULT_VERIFICATION,
                "Result contract could not be verified",
                "failed",
                "Analysis result rejected: " + "; ".join(verification_failures)
                if verification_failures
                else "The produced result did not satisfy the declared contract.",
                {"failedChecks": verification_failures},
            )
            raise HarnessValidationError("Generated tool returned an unverifiable result", code="unverifiable")
        stage(
            AnalysisTraceStage.RESULT_VERIFICATION,
            "Result contract verified",
            "completed",
            f"{len(verification['checks'])} evidence and result checks passed.",
            verification,
        )
        template.success_count += 1
        template.last_used_at = now_utc()
        user_tool.success_count += 1
        user_tool.last_used_at = now_utc()
        serialized_result = json.dumps(
            {
                "widgets": [widget.model_dump(mode="json") for widget in result.widgets],
                "citations": [citation.model_dump(mode="json") for citation in result.citations],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        run.status = ExecutionStatus.COMPLETED
        run.result_hash = hashlib.sha256(serialized_result.encode()).hexdigest()
        run.duration_ms = round((perf_counter() - started) * 1000)
        run.trace = trace
        return HarnessResult(
            result=result,
            template=template,
            user_tool=user_tool,
            run=run,
            reused=reused,
        )
    except Exception as error:
        if template:
            template.failure_count += 1
        if user_tool:
            user_tool.failure_count += 1
        run.status = ExecutionStatus.FAILED
        run.error_code = (
            error.error_code
            if isinstance(error, HarnessValidationError)
            else type(error).__name__
        )
        run.duration_ms = round((perf_counter() - started) * 1000)
        run.trace = trace
        db.flush()
        raise
