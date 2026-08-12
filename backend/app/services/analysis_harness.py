from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
import re
from time import perf_counter
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from openai import OpenAI

from ..config import get_settings
from ..domain import AnalysisToolStatus, ExecutionStatus
from ..models import AnalysisTool, AnalysisToolRun
from ..schemas import WidgetType
from .analytics import shift_month
from .intelligence import IntelligenceResult, execute_analysis_plan
from .repositories import UserScopedRepository
from .semantic import AnalysisToolProposal, AnalysisTransform, SemanticValidationError, validate_finance_query_plan
from .semantic_registry import semantic_schema_registry


HarnessCallback = Callable[[str, str, str, Optional[str]], None]
CURRENT_INCLUSIVE_THREE_MONTH_TOKENS = frozenset({"last", "three", "months"})
COMPLETE_PERIOD_TOKENS = frozenset({"complete", "full"})
DERIVED_ANALYSIS_TOKENS = frozenset({
    "compare", "comparison", "larger", "largest", "rank", "ranking",
    "share", "change", "trend", "drivers", "increase", "decrease",
})


class HarnessValidationError(ValueError):
    pass


@dataclass
class HarnessResult:
    result: IntelligenceResult
    tool: AnalysisTool
    run: AnalysisToolRun
    reused: bool


def _canonical_spec(proposal: AnalysisToolProposal) -> dict[str, Any]:
    specification = proposal.model_dump(mode="json", exclude_none=True)
    registry = semantic_schema_registry()
    specification["semanticRegistry"] = {"version": registry.version, "schemaHash": registry.schema_hash}
    return specification


def _specification_hash(specification: dict[str, Any]) -> str:
    encoded = json.dumps(specification, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _uses_current_registry(specification: dict[str, Any]) -> bool:
    registry = semantic_schema_registry()
    identity = specification.get("semanticRegistry", {})
    return identity.get("version") == registry.version and identity.get("schemaHash") == registry.schema_hash


def _analysis_tools(db: Session, user_id: UUID, *, active_only: bool = False):
    statement = UserScopedRepository(db, user_id).statement(AnalysisTool)
    if active_only:
        statement = statement.where(AnalysisTool.status == AnalysisToolStatus.ACTIVE)
    return statement


def delete_obsolete_analysis_tools(db: Session, user_id: UUID | None = None) -> int:
    """Sweep generated tools whose semantic contract no longer matches current code."""
    statement = select(AnalysisTool)
    if user_id:
        statement = _analysis_tools(db, user_id)
    obsolete = [tool for tool in db.scalars(statement) if not _uses_current_registry(tool.specification)]
    if not obsolete:
        return 0
    tool_ids = [tool.id for tool in obsolete]
    db.execute(delete(AnalysisToolRun).where(AnalysisToolRun.tool_id.in_(tool_ids)))
    db.execute(delete(AnalysisTool).where(AnalysisTool.id.in_(tool_ids)))
    db.flush()
    return len(tool_ids)


def _tokenize(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) > 2 and token not in {"the", "and", "for", "this", "that", "with"}
    }


def _non_month_dimension(query, *, fallback_to_first: bool = False) -> str | None:
    dimension = next(
        (item for item in reversed(query.dimensions) if item != "month"),
        None,
    )
    return dimension or (query.dimensions[0] if fallback_to_first else None)


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator if denominator else 0.0


def _retrieval_document(tool: AnalysisTool) -> str:
    return f"Intent: {tool.intent_signature}\nName: {tool.name}\nDescription: {tool.description}\nValidated plan: {json.dumps(tool.specification, sort_keys=True, default=str)}"


def discover_analysis_tools(db: Session, user_id: UUID, prompt: str, limit: int = 5) -> list[dict[str, Any]]:
    """Retrieve validated plans semantically, with lexical fallback on outages."""
    delete_obsolete_analysis_tools(db, user_id)
    prompt_tokens = _tokenize(prompt)
    tools = list(db.scalars(
        _analysis_tools(db, user_id, active_only=True)
        .order_by(AnalysisTool.last_used_at.desc().nullslast(), AnalysisTool.success_count.desc())
        .limit(50)
    ))
    ranked: list[tuple[float, int, AnalysisTool]] = []
    settings = get_settings()
    if tools and settings.openai_api_key and settings.primary_agent_enabled:
        try:
            missing = [tool for tool in tools if not tool.retrieval_embedding or tool.retrieval_embedding_model != settings.embedding_model]
            inputs = [prompt, *[_retrieval_document(tool) for tool in missing]]
            response = OpenAI(api_key=settings.openai_api_key, timeout=15, max_retries=1).embeddings.create(
                model=settings.embedding_model,
                input=inputs,
                dimensions=512,
            )
            prompt_embedding = response.data[0].embedding
            for tool, item in zip(missing, response.data[1:], strict=True):
                tool.retrieval_embedding = item.embedding
                tool.retrieval_embedding_model = settings.embedding_model
            for tool in tools:
                score = _cosine_similarity(prompt_embedding, tool.retrieval_embedding or [])
                if score >= 0.15:
                    ranked.append((score, tool.success_count, tool))
        except Exception:
            ranked = []
    if not ranked:
        for tool in tools:
            tool_tokens = _tokenize(f"{tool.intent_signature} {tool.name} {tool.description}")
            overlap = len(prompt_tokens & tool_tokens)
            if overlap:
                ranked.append((float(overlap), tool.success_count, tool))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [
        {
            "id": str(tool.id),
            "name": tool.name,
            "description": tool.description,
            "intent_signature": tool.intent_signature,
            "version": tool.version,
            "validated_specification": tool.specification,
            "retrieval_score": round(score, 4),
        }
        for score, _, tool in ranked[:limit]
    ]


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
        checks.append({
            "name": "query_presence",
            "passed": bool(plan.queries),
            "detail": f"{len(plan.queries)} governed semantic queries supplied.",
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
    visualization_requested = bool({"chart", "graph", "plot", "visual", "visualization"} & intent_words)
    checks.append({
        "name": "visualization_presence",
        "passed": not visualization_requested or bool(plan.visualizations),
        "detail": (
            f"The plan declares {len(plan.visualizations)} governed visualization specification(s)."
            if plan.visualizations
            else "No visualization was requested."
            if not visualization_requested
            else "The request asks for a visualization but the plan did not declare one."
        ),
    })
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
    return {"passed": passed, "checks": checks, "validated_at": datetime.now(timezone.utc).isoformat()}


def _repair_incomplete_analysis(proposal: AnalysisToolProposal, today: date) -> AnalysisToolProposal | None:
    """Repair narrow deterministic contract defects; never invent missing facts."""
    plan = proposal.plan
    if plan.analysis_type != "semantic_query" or plan.missing_information:
        return None
    if any(query.end_date > today for query in plan.queries):
        return None
    repaired = proposal.model_copy(deep=True)
    changed = False
    intent_tokens = _tokenize(f"{proposal.name} {proposal.intent_signature}")
    all_intent_tokens = _tokenize(f"{proposal.name} {proposal.intent_signature} {proposal.description}")
    if CURRENT_INCLUSIVE_THREE_MONTH_TOKENS.issubset(all_intent_tokens) and not (COMPLETE_PERIOD_TOKENS & all_intent_tokens):
        expected_start = shift_month(today.replace(day=1), -2)
        for query in repaired.plan.queries:
            if query.start_date != expected_start or query.end_date != today:
                query.start_date = expected_start
                query.end_date = today
                changed = True
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
                dimension = _non_month_dimension(query, fallback_to_first=True)
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
        {"name": "typed_widgets", "passed": bool(result.widgets), "detail": f"Returned {len(result.widgets)} typed widget(s)."},
        {"name": "evidence_lineage", "passed": bool(result.citations), "detail": f"Returned {len(result.citations)} structured data reference(s)."},
        {"name": "nonempty_message", "passed": bool(result.message.strip()), "detail": "A user-facing summary was produced."},
    ]
    if proposal.plan.analysis_type == "semantic_query":
        semantic_citations = [citation for citation in result.citations if citation.entity_type == "semantic_query"]
        checks.append({
            "name": "query_lineage",
            "passed": len(semantic_citations) == len(proposal.plan.queries),
            "detail": "Every generated semantic query has a data reference.",
        })
        analysis_widget = next((widget for widget in result.widgets if widget.type is WidgetType.ANALYSIS_TABLE), None)
        query_results = {
            item.get("name"): item
            for item in ((analysis_widget.data.get("queryResults", []) if analysis_widget else []))
        }
        for chart in (widget for widget in result.widgets if widget.type is WidgetType.DATA_CHART):
            query_result = chart.data.get("queryResult") or {}
            if query_result.get("name"):
                query_results[query_result["name"]] = query_result
        for visualization in (widget for widget in result.widgets if widget.type is WidgetType.DATA_VISUALIZATION):
            for query_result in (visualization.data.get("queryResults") or {}).values():
                if query_result.get("name"):
                    query_results[query_result["name"]] = query_result
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
            {
                "name": "visualization_contract",
                "passed": sum(
                    len(widget.data.get("views") or [])
                    for widget in result.widgets
                    if widget.type is WidgetType.DATA_VISUALIZATION
                ) == len(proposal.plan.visualizations),
                "detail": "Every requested view was returned through the governed visualization grammar.",
            },
        ])
    return {"passed": all(check["passed"] for check in checks), "checks": checks}


def execute_generated_tool(
    db: Session,
    user_id: UUID,
    conversation_id: UUID,
    today: date,
    proposal: AnalysisToolProposal | None,
    requested_tool_id: UUID | None = None,
    callback: HarnessCallback | None = None,
) -> HarnessResult:
    """Validate, compile, execute, verify, and persist a generated analysis tool."""
    started = perf_counter()
    trace: list[dict[str, Any]] = []

    def stage(stage_id: str, label: str, status: str, detail: str | None = None) -> None:
        trace.append({"stage": stage_id, "status": status, "detail": detail})
        if callback:
            callback(stage_id, label, status, detail)

    reused = False
    tool = None
    predecessor = None
    delete_obsolete_analysis_tools(db, user_id)
    if requested_tool_id:
        stage("tool_discovery", "Checking the reusable tool registry", "running")
        tool = db.scalar(_analysis_tools(db, user_id, active_only=True).where(
            AnalysisTool.id == requested_tool_id,
        ))
        if not tool:
            stage("tool_discovery", "Reusable tool was not eligible", "failed", "The id was absent, inactive, or belonged to another user.")
            raise HarnessValidationError("Requested analysis tool is not available")
        proposal = AnalysisToolProposal.model_validate(tool.specification)
        reused = True
        stage("tool_discovery", "Reusing a validated analysis tool", "completed", f"{tool.name} v{tool.version}")
    if not proposal:
        raise HarnessValidationError("The agent did not supply an analysis tool proposal")

    specification = _canonical_spec(proposal)
    fingerprint = _specification_hash(specification)
    if not tool:
        tool = db.scalar(_analysis_tools(db, user_id).where(
            AnalysisTool.specification_hash == fingerprint,
        ))
        if tool and tool.status == AnalysisToolStatus.ACTIVE:
            reused = True
            stage("tool_discovery", "Reusing an identical validated tool", "completed", f"{tool.name} v{tool.version}")
        elif not tool:
            stage("tool_synthesis", "Creating a declarative analysis tool", "running")
            normalized_intent = proposal.intent_signature.casefold().strip()
            predecessor = db.scalar(_analysis_tools(db, user_id, active_only=True).where(
                AnalysisTool.intent_signature == normalized_intent,
            ).order_by(AnalysisTool.version.desc()))
            tool = AnalysisTool(
                user_id=user_id,
                name=proposal.name,
                description=proposal.description,
                intent_signature=normalized_intent,
                version=(predecessor.version + 1) if predecessor else 1,
                status=AnalysisToolStatus.VALIDATING,
                specification=specification,
                specification_hash=fingerprint,
                validation_report={},
            )
            db.add(tool)
            db.flush()
            stage("tool_synthesis", "Created a declarative analysis tool", "completed", proposal.name)

    stage("tool_validation", "Validating generated tool", "running")
    validation = validate_analysis_tool(proposal, today)
    tool.validation_report = validation
    if not validation["passed"]:
        repaired = _repair_incomplete_analysis(proposal, today)
        if not repaired:
            failed_checks = [
                f"{check['name']}: {check['detail']}"
                for check in validation["checks"]
                if not check["passed"]
            ]
            db.delete(tool)
            db.flush()
            stage(
                "tool_validation",
                "Generated tool was rejected",
                "failed",
                "; ".join(failed_checks) or "One or more safety or completeness checks failed.",
            )
            raise HarnessValidationError("Generated analysis tool failed validation")
        stage("tool_validation", "Tool needs a deterministic repair", "completed", "The requested derived comparison was missing.")
        stage("tool_repair", "Repairing the generated analysis contract", "running")
        old_tool = tool
        specification = _canonical_spec(repaired)
        fingerprint = _specification_hash(specification)
        tool = db.scalar(_analysis_tools(db, user_id).where(
            AnalysisTool.specification_hash == fingerprint,
        ))
        if tool and tool.status == AnalysisToolStatus.ACTIVE:
            reused = True
        elif not tool:
            tool = AnalysisTool(
                user_id=user_id,
                name=repaired.name,
                description=repaired.description,
                intent_signature=repaired.intent_signature.casefold().strip(),
                version=old_tool.version,
                status=AnalysisToolStatus.VALIDATING,
                specification=specification,
                specification_hash=fingerprint,
                validation_report={},
            )
            db.add(tool)
            db.flush()
        proposal = repaired
        validation = validate_analysis_tool(proposal, today)
        tool.validation_report = validation
        if not validation["passed"]:
            if tool is not old_tool:
                db.delete(tool)
            db.delete(old_tool)
            db.flush()
            stage("tool_repair", "Generated tool repair was rejected", "failed")
            raise HarnessValidationError("Generated analysis tool failed validation after repair")
        if old_tool is not tool:
            db.delete(old_tool)
            db.flush()
        stage("tool_repair", "Repaired tool passed validation", "completed", f"Saved as version {tool.version}")
    else:
        stage("tool_validation", "Generated tool passed validation", "completed", f"{len(validation['checks'])} checks passed")

    run = AnalysisToolRun(user_id=user_id, tool_id=tool.id, conversation_id=conversation_id, status=ExecutionStatus.RUNNING, trace=[])
    db.add(run)
    db.flush()
    try:
        stage("tool_execution", "Executing governed finance queries", "running")
        with db.begin_nested():
            result = execute_analysis_plan(db, user_id, today, proposal.plan)
        stage("tool_execution", "Governed analysis completed", "completed", f"{len(result.widgets)} typed result widget(s)")
        stage("tool_verification", "Verifying evidence and result contract", "running")
        verification = _verify_result(proposal, result)
        if not verification["passed"]:
            raise HarnessValidationError("Generated tool returned an unverifiable result")
        stage("tool_verification", "Result contract verified", "completed", f"{len(verification['checks'])} checks passed")
        tool.status = AnalysisToolStatus.ACTIVE
        if predecessor and predecessor is not tool:
            db.execute(delete(AnalysisToolRun).where(AnalysisToolRun.tool_id == predecessor.id))
            db.delete(predecessor)
        tool.success_count += 1
        tool.last_used_at = datetime.now(timezone.utc)
        tool.validation_report = {**validation, "result_verification": verification}
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
        return HarnessResult(result=result, tool=tool, run=run, reused=reused)
    except Exception as error:
        if reused:
            tool.failure_count += 1
            run.status = ExecutionStatus.FAILED
            run.error_code = type(error).__name__
            run.duration_ms = round((perf_counter() - started) * 1000)
            run.trace = trace
        else:
            db.delete(run)
            db.delete(tool)
            db.flush()
        raise
