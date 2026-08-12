from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
import json
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from pydantic import BaseModel, Field, WithJsonSchema, field_validator, model_validator

from ..config import Settings, get_settings
from ..domain import SpendNature, TaxonomyOperation, TransactionType, ValueEnum
from ..event_time import local_now as current_local_time
from ..validation import SemanticIdentifier
from ..visualization_contracts import RequestedVisualMark
from .capabilities import CapabilityId, SAFE_READ_CAPABILITIES, capability_for_metric
from .semantic import AnalysisPlan, AnalysisToolProposal, AnalysisTransform, FinanceFilter, FinanceQueryPlan, TimeGrouping, TimePivot, VisualEncoding, VisualEncodingSet, VisualizationSpec, semantic_catalog
from .semantic_registry import SortDirection, TIME_GRAIN_SPECS, TimeGrain, semantic_schema_registry
from .runtime_tools import runtime_tool_contract
from .user_memory import agent_memory_manager


RECENT_CONTEXT_MESSAGE_LIMIT = 5
QueryOperation = Literal["total", "breakdown", "rank", "list"]
QueryGroupBy = Literal["none", "category", "subcategory", "merchant", "account", "month"]
GROUPED_QUERY_OPERATIONS = frozenset({"rank", "breakdown"})
TEMPORAL_PRESENTATION_UNITS = frozenset({"date", "month"})
# FinanceQueryPlan caps a governed window at five years, so this is the widest
# period an "all records" request can be compiled into without being rejected.
_WIDEST_GOVERNED_WINDOW_DAYS = 1825


def _agent_enabled(settings) -> bool:
    return bool(settings.openai_api_key and settings.primary_agent_enabled)


def _enabled_agent_settings() -> Settings | None:
    settings = get_settings()
    return settings if _agent_enabled(settings) else None


def _responses_model(
    settings,
    model_id: str,
    *,
    reasoning_effort: str = "none",
    reasoning_summary: str | None = None,
    timeout: int | None = None,
) -> OpenAIResponses:
    options = {
        "id": model_id,
        "api_key": settings.openai_api_key,
        "reasoning_effort": reasoning_effort,
        "reasoning_summary": reasoning_summary,
        "verbosity": "low",
        "max_retries": 1,
    }
    if timeout is not None:
        options["timeout"] = timeout
    return OpenAIResponses(**options)


def _taxonomy_prompt(categories: list[dict]) -> str:
    """Render stable slugs together with the names users actually type."""
    rendered = []
    for item in categories:
        subcategories = []
        for subcategory in item.get("subcategories", []):
            if isinstance(subcategory, dict):
                subcategories.append(f"{subcategory.get('slug')} ({subcategory.get('name')})")
            else:  # Backward-compatible with tests and older callers.
                subcategories.append(str(subcategory))
        rendered.append(
            f"{item['slug']} ({item['name']}): {', '.join(subcategories)}"
        )
    return "; ".join(rendered)


def _agent_context(categories: list[dict]) -> tuple[Settings | None, str]:
    settings = _enabled_agent_settings()
    return settings, _taxonomy_prompt(categories) if settings else ""


def _memory_agent_options(user_id: UUID | str | None) -> dict:
    manager = agent_memory_manager() if user_id else None
    return {
        "memory_manager": manager,
        "add_memories_to_context": True,
        "update_memory_on_run": False,
        "user_id": str(user_id),
    } if manager else {}


def _format_recent_context(recent_context: list[dict]) -> str:
    return "\n".join(
        f"{item['role']}: {item['content'][:500]}"
        for item in recent_context[-RECENT_CONTEXT_MESSAGE_LIMIT:]
    )


class AIAssistedMatch(BaseModel):
    same_transaction: bool
    confidence: float = Field(ge=0, le=1)
    reason: str


def inline_enum(enum_type: type[Enum]):
    """An enum annotation that carries its values instead of a reference.

    Pydantic renders a bare enum field as `{"$ref": ..., "default": ...}`, and
    the model provider rejects a `$ref` that carries any sibling keyword — so a
    single defaulted enum field makes the whole contract unusable and every call
    on that route falls back to the deterministic path. Inlining the values
    removes the reference, leaving the default nothing to sit beside.

    Validation is unchanged: the annotation is still the enum, so a response is
    still coerced to it and still rejected when it is not one of these values.
    """
    return Annotated[
        enum_type,
        WithJsonSchema({"type": "string", "enum": [item.value for item in enum_type]}),
    ]


class TransactionInterpretation(BaseModel):
    transaction_type: inline_enum(TransactionType) = TransactionType.UNKNOWN
    amount_minor: int | None = Field(default=None, ge=1)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    merchant: str | None = None
    source_account: str | None = None
    destination_account: str | None = None
    transaction_date: date | None = None
    category_slug: str | None = None
    subcategory_slug: str | None = None
    transaction_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}(?::\d{2})?$")
    timezone: str | None = None
    location_label: str | None = Field(default=None, max_length=160)
    tags: list[str] = Field(default_factory=list, max_length=8)
    spend_nature: inline_enum(SpendNature) = SpendNature.UNKNOWN
    explicit_fields: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class QueryInterpretation(BaseModel):
    metric: SemanticIdentifier = "transaction_summary"
    result_mode: Literal["summary", "transaction_list", "complex_analysis"] = "summary"
    operation: QueryOperation = "total"
    group_by: QueryGroupBy = "none"
    sort_direction: SortDirection = "desc"
    transaction_type: TransactionType | None = None
    merchant: str | None = Field(default=None, max_length=160)
    category_slug: str | None = None
    subcategory_slug: str | None = None
    account: str | None = Field(default=None, max_length=120)
    tag: str | None = Field(default=None, max_length=80)
    min_amount_minor: int | None = Field(default=None, ge=0)
    max_amount_minor: int | None = Field(default=None, ge=0)
    start_date: date | None = None
    end_date: date | None = None
    limit: int = Field(default=50, ge=1, le=100)
    use_active_scope: bool = False
    scope_transaction_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @field_validator("transaction_type")
    @classmethod
    def known_transaction_type(cls, value: TransactionType | None) -> TransactionType | None:
        if value is TransactionType.UNKNOWN:
            raise ValueError("Queries require a known transaction type or no direction filter")
        return value


class QueryView(BaseModel):
    """A result shape compiled over a QueryBundle's single governed scope."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    result_mode: Literal["summary", "transaction_list"]
    operation: QueryOperation
    group_by: QueryGroupBy = "none"
    sort_direction: SortDirection = "desc"
    limit: int = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def validate_result_contract(self):
        if self.result_mode == "transaction_list" and self.operation != "list":
            raise ValueError("transaction_list views require operation=list")
        if self.result_mode == "summary" and self.operation == "list":
            raise ValueError("summary views cannot use operation=list")
        return self


class QueryBundleInterpretation(BaseModel):
    """One filter scope with several coordinated, deterministic presentations."""

    base_query: QueryInterpretation
    views: list[QueryView] = Field(min_length=2, max_length=4)
    refresh_from_active_analysis: bool = False

    @model_validator(mode="after")
    def validate_views(self):
        ids = [view.id for view in self.views]
        if len(ids) != len(set(ids)):
            raise ValueError("query-bundle view ids must be unique")
        return self


class TaxonomyInterpretation(BaseModel):
    operation: TaxonomyOperation
    name: str | None = Field(default=None, max_length=80)
    parent_category: str | None = Field(default=None, max_length=80)


class PresentationIntent(BaseModel):
    """How an analysis must be presented, separate from what data it reads."""

    mode: Literal["auto", "summary", "table", "chart"] = "auto"
    layout: Literal["single", "dashboard"] = "single"
    visual_goal: Literal["auto", "trend", "comparison", "composition", "distribution", "relationship", "density"] = "auto"
    requested_mark: RequestedVisualMark = "auto"
    # Legacy route hint retained for persisted decisions. The governed
    # visualization grammar below is authoritative for new analysis plans.
    chart_type: Literal["auto", "bar", "line", "area", "pie", "heatmap"] = "auto"
    # Grains the governed chart compiler can bind, plus the two calculator-only
    # units. CHART_CAPABILITIES is authoritative for which of these a database
    # chart can actually use; it is derived from the semantic registry, so this
    # list grows with the registry rather than capping it.
    unit_of_analysis: Literal[
        "auto", "transaction", "category", "subcategory", "merchant", "account",
        "transaction_type", "tag", "spend_nature", "location", "currency", "status",
        "month", "date", "installment", "calculation_step",
    ] = "auto"
    value_semantics: Literal["auto", "amount", "count", "percentage"] = "auto"
    time_grain: Union[Literal["auto"], TimeGrain] = "auto"
    rolling_value: int | None = Field(default=None, ge=1, le=10_000)
    rolling_unit: TimeGrain | None = None
    # Renderer-neutral field bindings for a dataset returned by an
    # authenticated deterministic tool. The domain layer validates every name
    # against the returned field catalog before rendering anything.
    x_field: SemanticIdentifier | None = None
    y_fields: list[SemanticIdentifier] = Field(default_factory=list, max_length=4)
    color_field: SemanticIdentifier | None = None


class CopilotRoute(ValueEnum):
    CONVERSATION = "conversation"
    TRANSACTION = "transaction"
    TRANSACTION_REMOVAL = "transaction_removal"
    TAXONOMY = "taxonomy"
    ANALYSIS = "analysis"
    PLANNING = "planning"
    UNKNOWN = "unknown"


class CopilotRouteDecision(BaseModel):
    route: CopilotRoute
    reply: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    reason: str = Field(max_length=300)
    safe_reasoning_summary: list[str] = Field(default_factory=list, max_length=4)
    query: QueryInterpretation | None = None
    query_bundle: QueryBundleInterpretation | None = None
    taxonomy: TaxonomyInterpretation | None = None
    presentation: PresentationIntent = Field(default_factory=PresentationIntent)


class ToolResultEnvelope(BaseModel):
    tool: str
    schema_name: str | None = None
    data: Any


class ToolGrounding(BaseModel):
    """Evidence captured from an actual successful runtime tool execution."""

    name: SemanticIdentifier
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: ToolResultEnvelope

    @field_validator("result", mode="before")
    @classmethod
    def wrap_legacy_result(cls, value):
        if isinstance(value, ToolResultEnvelope):
            return value
        if isinstance(value, dict) and {"tool", "data"} <= set(value):
            return value
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                try:
                    parsed = ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    parsed = value
        return {"tool": "legacy", "data": parsed}

    @model_validator(mode="after")
    def bind_and_bound_result(self):
        self.result.tool = self.name
        if len(json.dumps(self.result.model_dump(mode="json"), default=str)) > 500_000:
            raise ValueError("Tool result exceeds the grounding size limit")
        return self


class CompilationAssumption(BaseModel):
    """One narrowing or substitution the compiler applied to reach a plan.

    The compiler cannot always honour a prompt exactly: a period may be
    unstated, a part-to-whole may have no coherent whole, a requested mark may
    not fit the grain. Making each of those a declared value rather than a
    silent rewrite is what keeps the rest of the pipeline honest — the
    validator can tell a disclosed narrowing from a betrayed intent, and the
    user can see and correct what was assumed on their behalf.
    """

    code: Literal[
        "defaulted_period",
        "direction_composed",
        "direction_restricted",
        "grain_substituted",
        "mark_substituted",
        "scope_released",
    ]
    detail: str = Field(min_length=3, max_length=200)


class CopilotDecision(BaseModel):
    tool: CapabilityId
    transaction: TransactionInterpretation | None = None
    query: QueryInterpretation | None = None
    query_bundle: QueryBundleInterpretation | None = None
    taxonomy: TaxonomyInterpretation | None = None
    presentation: PresentationIntent = Field(default_factory=PresentationIntent)
    analysis_tool: AnalysisToolProposal | None = None
    assumptions: list[CompilationAssumption] = Field(default_factory=list, max_length=5)
    reuse_tool_id: UUID | None = None
    safe_reasoning_summary: list[str] = Field(default_factory=list, max_length=5)
    reply: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    reason: str = Field(max_length=300)
    tool_grounding: list[ToolGrounding] = Field(default_factory=list, max_length=4)
    validated_by: str | None = None
    validation_confidence: float | None = Field(default=None, ge=0, le=1)


class CopilotDecisionValidation(BaseModel):
    outcome: Literal["approve", "request_human_input", "reject"]
    confidence: float = Field(ge=0, le=1)
    issues: list[str] = Field(default_factory=list, max_length=5)
    repairs: list[Literal["bind_active_scope"]] = Field(default_factory=list, max_length=3)
    summary: str = Field(max_length=300)


ACCEPTED_COPILOT_VALIDATION_OUTCOMES = frozenset({"approve", "request_human_input"})


_PRESENTATION_UNIT_ALIASES: dict[str, set[str]] = {
    "transaction": {"transaction", "transactions", "record", "records", "entry", "entries"},
    "category": {"category", "categories"},
    "subcategory": {"subcategory", "subcategories"},
    "merchant": {"merchant", "merchants", "vendor", "vendors", "restaurant", "restaurants", "store", "stores"},
    "account": {"account", "accounts"},
    "transaction_type": {"type", "types", "direction", "directions"},
    "tag": {"tag", "tags"},
    "location": {"location", "locations", "place", "places"},
    "spend_nature": {"nature", "essentials", "discretionary"},
    "month": set(TIME_GRAIN_SPECS["month"].aliases),
    "date": set(TIME_GRAIN_SPECS["day"].aliases) | {"date", "dates", "time", "timeline"},
}


def _prompt_words(text: str) -> list[str]:
    return [part.strip(".,?!:;()[]{}\n") for part in text.casefold().split()]


def _explicit_presentation_unit(text: str) -> str | None:
    """Return the grain the prompt names outright, or None if it names none.

    The compiler needs this apart from the bound presentation: a grain the user
    typed is a constraint to honour, while a grain a model inferred is only a
    guess and may be replaced by a better-founded one.
    """
    words = _prompt_words(text)
    for index, word in enumerate(words[:-1]):
        if word != "by":
            continue
        for candidate in words[index + 1:index + 4]:
            if candidate in {"the", "each", "individual", "per"}:
                continue
            unit = next((name for name, aliases in _PRESENTATION_UNIT_ALIASES.items() if candidate in aliases), None)
            if unit:
                return unit
            break
    return None


# Words that ask for a picture rather than a number. "bar" and "line" are
# deliberately absent: both are ordinary English in a spending transcript.
_CHART_REQUEST_WORDS = frozenset({
    "chart", "charts", "graph", "graphs", "plot", "plots", "plotted",
    "visualise", "visualize", "visualisation", "visualization",
    "donut", "doughnut", "pie", "heatmap", "histogram", "scatter", "scatterplot",
    "dashboard",
})


def requests_chart(text: str) -> bool:
    """Report whether the prompt asks to be shown a chart.

    The router also classifies this, but as a model output it varies between
    samples and a successful runtime tool call used to override it. Reading it
    off the prompt makes the request a constraint the pipeline has to satisfy
    rather than a preference any later stage can drop.
    """
    return bool(_CHART_REQUEST_WORDS & set(_prompt_words(text)))


def _bind_explicit_presentation_unit(text: str, presentation: PresentationIntent) -> PresentationIntent:
    """Bind only an explicitly named `by <entity>` grain to the semantic schema."""
    words = _prompt_words(text)
    updates: dict = {}
    if requests_chart(text):
        updates["mode"] = "chart"
    if "dashboard" in words:
        updates.update({"mode": "chart", "layout": "dashboard"})
    if "heatmap" in words or ("heat" in words and "map" in words):
        updates.update({"chart_type": "heatmap", "visual_goal": "density", "requested_mark": "rect"})
    elif any(word in {"scatter", "scatterplot"} for word in words):
        updates.update({"visual_goal": "relationship", "requested_mark": "point"})
    elif any(word in {"histogram", "distribution"} for word in words):
        updates.update({"visual_goal": "distribution", "requested_mark": "bar"})
    elif any(word in {"donut", "pie", "composition", "share"} for word in words):
        updates.update({"visual_goal": "composition", "requested_mark": "arc"})
    explicit_grain = next(
        (grain for grain, spec in TIME_GRAIN_SPECS.items() if any(word in spec.aliases for word in words)),
        None,
    )
    if explicit_grain:
        updates.update({"unit_of_analysis": "date", "time_grain": explicit_grain})
        for index, word in enumerate(words[:-2]):
            if word != "last" or not words[index + 1].isdigit():
                continue
            rolling_unit = next(
                (grain for grain, spec in TIME_GRAIN_SPECS.items() if words[index + 2] in spec.aliases),
                None,
            )
            if rolling_unit:
                updates.update({"rolling_value": int(words[index + 1]), "rolling_unit": rolling_unit})
                break
    if updates:
        presentation = presentation.model_copy(update=updates)
    explicit_unit = _explicit_presentation_unit(text)
    if explicit_unit:
        return presentation.model_copy(update={"unit_of_analysis": explicit_unit})
    return presentation


_UNIVERSAL_QUANTIFIERS = frozenset({"all", "every"})
# Nouns that name a population of records rather than one financial direction.
_POPULATION_NOUNS = frozenset({
    "transaction", "transactions", "record", "records", "entry", "entries",
    "spending", "spendings", "spend", "spends", "expense", "expenses",
    "payment", "payments", "purchase", "purchases", "income", "earnings",
})
_RECORD_NOUNS = frozenset({"transaction", "transactions", "record", "records", "entry", "entries"})


def _quantified_nouns(text: str, nouns: frozenset[str]) -> bool:
    words = _prompt_words(text)
    return any(
        word in _UNIVERSAL_QUANTIFIERS and any(candidate in nouns for candidate in words[index + 1:index + 4])
        for index, word in enumerate(words)
    )


def states_universal_scope(text: str) -> bool:
    """Report whether the prompt quantifies over every transaction *direction*.

    Deterministic and lexical on purpose: "all transactions" is a constraint
    the user typed, so neither the router nor a repair model may reinterpret
    it, and every stage that has to respect it reads the same signal. Note the
    narrow reading — "all my spending" quantifies over a population but still
    names one direction, so it is deliberately not universal here.
    """
    return _quantified_nouns(text, _RECORD_NOUNS)


# Words that name one financial direction. A prompt naming two or more is
# asking for a comparison between them, which is a different governed shape
# from any single-direction metric.
_DIRECTION_WORDS: dict[str, frozenset[str]] = {
    "income": frozenset({"income", "earning", "earnings", "earned", "salary", "credits", "credited", "inflow", "inflows"}),
    "expense": frozenset({"expense", "expenses", "spending", "spendings", "spend", "spends", "spent", "debits", "outflow", "outflows"}),
    "transfer": frozenset({"transfer", "transfers"}),
    "refund": frozenset({"refund", "refunds", "refunded"}),
    "cash_withdrawal": frozenset({"withdrawal", "withdrawals", "withdrawn"}),
}


def names_multiple_directions(text: str) -> bool:
    """Report whether the prompt compares two or more financial directions.

    "Earning and expenses" is not a request for either metric; it is a request
    for both side by side, which only transaction_amount grouped by
    transaction_type can express. Reading it off the prompt keeps the router
    from proposing an income-only query that the validator then rightly
    rejects for dropping half the question.
    """
    words = set(_prompt_words(text))
    return sum(1 for aliases in _DIRECTION_WORDS.values() if words & aliases) >= 2


def releases_prior_scope(text: str) -> bool:
    """Report whether the prompt widens away from the records already shown.

    Broader than `states_universal_scope`: "all my spending" keeps its expense
    direction but is still no kind of refinement of the previous turn's food
    and delivery rows, so the conversational scope has to be let go either way.
    """
    return "everything" in _prompt_words(text) or _quantified_nouns(text, _POPULATION_NOUNS)


_PERIOD_CUE_WORDS = frozenset({
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "today", "yesterday", "tomorrow", "ytd", "mtd", "since", "until", "till",
    "between", "before", "after", "during", "last", "past", "previous", "next", "recent", "ago",
})


def states_explicit_period(text: str) -> bool:
    """Report whether the prompt itself names a period to analyse over.

    A universal request carries no period, so any dates on the typed query came
    from the previous analytical turn. Distinguishing the two is what stops
    "all transactions" from silently inheriting yesterday's two-day window.
    """
    words = _prompt_words(text)
    if any(word in _PERIOD_CUE_WORDS for word in words):
        return True
    if any(len(word) == 4 and word.isdigit() and word.startswith(("19", "20")) for word in words):
        return True
    return any(
        word in spec.aliases
        for spec in TIME_GRAIN_SPECS.values()
        for word in words
    )


def _directional_money_metrics() -> frozenset[str]:
    """Governed metrics that answer for one financial direction only."""
    return frozenset(
        metric.name
        for metric in semantic_schema_registry().metrics
        if "transaction_type" in (metric.fixed_filters or {})
    ) | {"net_spend"}


def _bind_explicit_universal_scope(text: str, query: QueryInterpretation | None) -> QueryInterpretation | None:
    """Apply an explicit universal quantifier over the transaction entity.

    This prevents a fresh "all transactions" request from accidentally
    inheriting filters from the prior analytical turn. It does not infer an
    intent or parse arbitrary language; the semantic router has already done
    that work.
    """
    every_direction = states_universal_scope(text)
    if not releases_prior_scope(text):
        return query
    if not query:
        if not every_direction:
            return None
        query = QueryInterpretation(
            metric="transaction_summary",
            result_mode="complex_analysis",
            operation="breakdown",
            limit=100,
        )
    updates: dict = {
        "merchant": None,
        "category_slug": None,
        "subcategory_slug": None,
        "account": None,
        "tag": None,
        "min_amount_minor": None,
        "max_amount_minor": None,
        "use_active_scope": False,
        "scope_transaction_ids": [],
    }
    # Dates are a filter like any other. A prompt that names no period has not
    # asked for the previous turn's window, so releasing the dates here is what
    # lets the compiler declare a period rather than inherit one unnoticed.
    if not states_explicit_period(text):
        updates |= {"start_date": None, "end_date": None}
    # Direction is released only by a prompt that quantifies over records.
    # "All my spending" widens the population while keeping its own direction,
    # and turning that into an all-directions query would answer a different
    # question than the one asked.
    if every_direction:
        updates |= {"metric": "transaction_summary", "transaction_type": None}
    return query.model_copy(update=updates)


def _bind_multi_direction_scope(text: str, query: QueryInterpretation | None) -> QueryInterpretation | None:
    """Release a single-direction metric when the prompt compares directions.

    A directional metric answers for one direction by construction, so leaving
    one in place would drop half of an "income versus expenses" question. The
    direction-neutral metric with transaction_type preserved is the only
    governed shape that keeps both sides.
    """
    if not query or not names_multiple_directions(text):
        return query
    if query.transaction_type is None and query.metric not in _directional_money_metrics():
        return query
    return query.model_copy(update={"metric": "transaction_summary", "transaction_type": None})


def _presentation_contract_issues(decision: CopilotDecision, text: str = "") -> list[str]:
    """Check generated query grain against the independent presentation contract."""
    presentation = decision.presentation
    if presentation.mode != "chart":
        return []
    proposal = decision.analysis_tool
    if decision.tool is not CapabilityId.RUN_ANALYSIS_HARNESS or not proposal:
        return ["Chart presentation requires the governed analysis harness."]
    if not proposal.plan.visualizations:
        return ["Chart presentation requires at least one visualization specification."]
    if presentation.layout == "dashboard" and len(proposal.plan.visualizations) < 2:
        return ["Dashboard presentation requires multiple independently governed views."]
    declared = {item.code for item in decision.assumptions}
    # A substitution the compiler declared has been disclosed to the user and
    # is a legitimate outcome. The same substitution left undeclared is a
    # silent rewrite of the request, and that is what this gate exists to stop.
    if presentation.requested_mark != "auto" and "mark_substituted" not in declared:
        substituted = [
            visualization.mark
            for visualization in proposal.plan.visualizations
            if visualization.mark != presentation.requested_mark
        ]
        if substituted:
            return [f"Chart must use the requested {presentation.requested_mark} mark, not {substituted[0]}, unless the substitution is declared."]
    if text and states_universal_scope(text) and "direction_restricted" not in declared:
        directional = _directional_money_metrics()
        narrowed = [query.name for query in proposal.plan.queries if query.metric in directional]
        if narrowed:
            return [f"Query “{narrowed[0]}” restricts an all-transaction request to one financial direction without declaring it."]
    if presentation.unit_of_analysis == "auto":
        return []
    capability = chart_capability(presentation.unit_of_analysis)
    if capability is None:
        return [f"No governed chart can be drawn at a {presentation.unit_of_analysis.replace('_', ' ')} grain."]
    marks = {visualization.mark for visualization in proposal.plan.visualizations}
    # A multi-view answer is coverage across panels, so a trend panel may hold a
    # mark the top-level grain could not carry alone. Only a single chart has to
    # satisfy the manifest outright — the same rule the grain check below uses.
    if len(proposal.plan.visualizations) == 1 and not marks <= capability.marks:
        unsupported = sorted(marks - capability.marks)[0]
        return [f"A {capability.grain.replace('_', ' ')} chart cannot be drawn with a {unsupported} mark."]
    temporal = capability.temporal
    heatmap = "rect" in marks
    # The manifest names the field each grain's query produces, so the gate no
    # longer restates the compiler's mapping and cannot drift from it.
    expected = "time_segment" if heatmap else "time_bucket" if temporal else capability.produces
    expected_grain = (
        "month" if presentation.unit_of_analysis == "month"
        else "day" if presentation.time_grain == "auto"
        else presentation.time_grain
    )
    queries = {query.name: query for query in proposal.plan.queries}
    candidates: list[tuple[VisualizationSpec, FinanceQueryPlan, str]] = []
    for visualization in proposal.plan.visualizations:
        query = queries.get(visualization.query_name)
        if not query:
            continue
        transform = next(
            (item for item in proposal.plan.transforms if item.name == visualization.transform_name),
            None,
        )
        visual_dimension = "label" if transform and transform.dimension == expected else expected
        candidates.append((visualization, query, visual_dimension))

    def preserves_requested_grain(candidate: tuple[VisualizationSpec, FinanceQueryPlan, str]) -> bool:
        visualization, query, visual_dimension = candidate
        produced_temporally = (
            (expected == "time_bucket" and (query.time_grouping or query.time_pivot))
            or (expected == "time_segment" and query.time_pivot)
        )
        if expected not in query.dimensions and not produced_temporally:
            return False
        if heatmap:
            if not query.time_pivot:
                return False
        elif temporal and (not query.time_grouping or query.time_grouping.grain != expected_grain):
            return False
        dimension_channel = visualization.encoding.color if visualization.mark == "arc" else visualization.encoding.x
        if not dimension_channel or dimension_channel.field != visual_dimension:
            return False
        if heatmap and (not visualization.encoding.y or visualization.encoding.y.field != "time_bucket"):
            return False
        return True

    # A dashboard is a composition of independently governed views. Its
    # top-level presentation contract describes required coverage, not a rule
    # that every panel must share one axis. A single chart remains strict.
    if any(preserves_requested_grain(candidate) for candidate in candidates):
        return []
    actual = ", ".join(
        (
            candidate[0].encoding.color.field
            if candidate[0].mark == "arc" and candidate[0].encoding.color
            else candidate[0].encoding.x.field
            if candidate[0].encoding.x
            else "no dimension field"
        )
        for candidate in candidates
    ) or "no dimension field"
    issues = [f"Chart query must preserve {presentation.unit_of_analysis} as its unit of analysis."]
    if heatmap:
        issues.append("Heatmap query must preserve a governed two-dimensional time pivot.")
    elif temporal:
        issues.append(f"Chart query must preserve the requested {expected_grain} time grain.")
    issues.append(f"Chart axis must use {expected}, not {actual}.")
    return issues


@dataclass(frozen=True)
class ChartCapability:
    """One chart shape the governed compiler can actually emit.

    This manifest is the single description of the compiler's reach. The router
    is shown it so it only proposes expressible charts, the compiler resolves
    against it instead of re-deciding in ad-hoc conditionals, and the contract
    gate checks compiled plans against it. Keeping one description means a
    request outside the surface is caught deterministically, in the same pass
    that builds the plan, rather than discovered later by a model reading prose
    rules that restate all of this in English.
    """

    grain: str
    produces: str
    marks: frozenset[str]
    preferred_mark: str
    temporal: bool = False
    note: str = ""


# Marks each family of grain can carry. Categorical grains partition a total,
# so they support part-to-whole; identity grains do not, because separate
# records share no total; ordered series need a real axis, so they are temporal.
_TEMPORAL_GRAIN_MARKS = frozenset({"line", "area", "bar", "point"})
_CATEGORICAL_GRAIN_MARKS = frozenset({"bar", "arc", "point"})
_IDENTITY_GRAIN_MARKS = frozenset({"bar", "point"})
# Dimensions the temporal capabilities below already cover, or that name an
# instant rather than a grouping the compiler can bucket.
_NON_GRAIN_DIMENSIONS = frozenset({"transaction_date", "month", "posted_date"})


def _build_chart_capabilities() -> tuple[ChartCapability, ...]:
    """Derive the chart surface from the semantic registry.

    Hand-listing the grains would make the manifest a second place to remember,
    and it would quietly cap the product at whatever was typed here. Reading it
    from the registry means every governed transaction dimension is chartable
    the day it is defined, and the surface can only describe things the query
    compiler can genuinely bind.
    """
    capabilities = [
        ChartCapability(
            grain="date", produces="time_bucket",
            marks=_TEMPORAL_GRAIN_MARKS | {"rect"}, preferred_mark="line", temporal=True,
            note="Time series at the requested grain. rect is the day-by-hour heatmap pivot.",
        ),
        ChartCapability(
            grain="month", produces="time_bucket",
            marks=_TEMPORAL_GRAIN_MARKS, preferred_mark="line", temporal=True,
            note="Monthly time series.",
        ),
    ]
    for dimension in semantic_schema_registry().dimensions:
        if dimension.base_entity != "transactions" or dimension.name in _NON_GRAIN_DIMENSIONS:
            continue
        identity = dimension.name == "transaction"
        capabilities.append(ChartCapability(
            grain=dimension.name,
            produces=dimension.name,
            marks=_IDENTITY_GRAIN_MARKS if identity else _CATEGORICAL_GRAIN_MARKS,
            preferred_mark="bar",
            note=(
                f"{dimension.description} No part-to-whole: separate records share no total."
                if identity else dimension.description
            ),
        ))
    return tuple(capabilities)


CHART_CAPABILITIES: tuple[ChartCapability, ...] = _build_chart_capabilities()

_CAPABILITY_BY_GRAIN = {capability.grain: capability for capability in CHART_CAPABILITIES}
_MARK_FOR_CHART_TYPE = {"bar": "bar", "line": "line", "area": "area", "pie": "arc", "heatmap": "rect"}
_MARK_FOR_VISUAL_GOAL = {
    "trend": "line", "comparison": "bar", "composition": "arc",
    "distribution": "bar", "relationship": "point", "density": "rect",
}


def chart_capability(grain: str) -> ChartCapability | None:
    return _CAPABILITY_BY_GRAIN.get(grain)


def chart_capability_catalog() -> list[dict[str, Any]]:
    """Render the manifest for a model prompt."""
    return [
        {
            "grain": capability.grain,
            "produces": capability.produces,
            "marks": sorted(capability.marks),
            "default_mark": capability.preferred_mark,
            "note": capability.note,
        }
        for capability in CHART_CAPABILITIES
    ]


@dataclass(frozen=True)
class ChartShape:
    capability: ChartCapability
    mark: str
    assumptions: tuple[CompilationAssumption, ...] = ()


def resolve_chart_shape(presentation: PresentationIntent) -> ChartShape | None:
    """Pick the mark this grain can carry, or decline the grain outright.

    Declining is a real answer: `installment` and `calculation_step` belong to
    calculator datasets and have no governed transaction dimension, so
    compiling them would emit a query the semantic registry cannot validate.
    """
    capability = chart_capability(presentation.unit_of_analysis)
    if capability is None:
        return None
    requested = presentation.requested_mark
    if requested == "auto":
        requested = (
            _MARK_FOR_CHART_TYPE.get(presentation.chart_type)
            or _MARK_FOR_VISUAL_GOAL.get(presentation.visual_goal)
            or capability.preferred_mark
        )
    mark = requested if requested in capability.marks else capability.preferred_mark
    assumptions: tuple[CompilationAssumption, ...] = ()
    if presentation.requested_mark != "auto" and mark != presentation.requested_mark:
        assumptions = (CompilationAssumption(
            code="mark_substituted",
            detail=(
                f"Drawn as a {mark} chart: a {presentation.requested_mark} cannot express "
                f"a {capability.grain.replace('_', ' ')} grain."
            ),
        ),)
    return ChartShape(capability=capability, mark=mark, assumptions=assumptions)


def _chartable_grounding(tool_grounding: list[ToolGrounding]) -> ToolGrounding | None:
    """Return the first grounded result that can actually be drawn.

    A runtime tool succeeding is not the same as a runtime tool answering a
    chart request: `spending_summary` returns one scalar, which no axis can be
    bound to. Checking that here keeps a successful-but-unchartable call from
    committing the turn to a visual route it cannot complete.
    """
    for item in tool_grounding:
        data = item.result.data
        if isinstance(data, str):
            try:
                data = ast.literal_eval(data)
            except (ValueError, SyntaxError):
                continue
        if not isinstance(data, dict) or data.get("kind") != "computed_dataset":
            continue
        rows = data.get("rows")
        fields = data.get("fields")
        if not isinstance(rows, list) or not rows or not isinstance(fields, list):
            continue
        roles = {field.get("name"): field.get("role") for field in fields if isinstance(field, dict)}
        if "dimension" in roles.values() and "measure" in roles.values():
            return item
    return None


def _inferred_presentation_unit(query: QueryInterpretation, presentation: PresentationIntent) -> str | None:
    """Choose a defensible grain for a chart whose request named none.

    Returns None when nothing in the typed intent supports a choice, which
    hands the request back to the model-authored factory rather than inventing
    a grouping the prompt cannot justify.
    """
    if query.group_by and query.group_by != "none":
        return query.group_by
    if presentation.time_grain != "auto" or presentation.rolling_unit:
        return "date"
    return {
        "trend": "date",
        "density": "date",
        "composition": "category",
        "comparison": "category",
        "distribution": "category",
    }.get(presentation.visual_goal)


@dataclass(frozen=True)
class ChartCompilation:
    """A compiled chart plus everything the compiler had to assume to reach it.

    `presentation` is the contract the plan actually satisfies, which is not
    always the one the router asked for. Downstream checks validate against
    this effective contract and read `assumptions` to see why it differs.
    """

    proposal: AnalysisToolProposal
    presentation: PresentationIntent
    assumptions: tuple[CompilationAssumption, ...] = ()


def _compile_governed_chart(
    route: CopilotRouteDecision,
    current_date: date,
    user_timezone: str,
    text: str = "",
) -> ChartCompilation | None:
    """Compile a typed chart intent without allowing a model to author query code.

    The router decides semantics; this compiler merely maps that decision onto
    the versioned finance schema and visualization protocol. Where the two
    cannot be reconciled exactly, the compiler resolves the conflict itself and
    declares the resolution instead of narrowing the request in silence.
    """
    query = route.query
    presentation = route.presentation
    if not query or presentation.mode != "chart" or presentation.layout == "dashboard":
        return None

    assumptions: list[CompilationAssumption] = []
    if (
        names_multiple_directions(text)
        and not _explicit_presentation_unit(text)
        and presentation.unit_of_analysis in {"auto", "category", "transaction"}
    ):
        # The comparison the prompt asked for *is* the grain. Grouping by
        # anything else answers a question about composition instead.
        presentation = presentation.model_copy(update={
            "unit_of_analysis": "transaction_type",
            "x_field": None,
            "y_fields": [],
            "color_field": None,
        })
    if presentation.unit_of_analysis == "auto":
        # Handing plan authoring to a model because no grain was named is the
        # one path here that lets an LLM write the query, and it is the path
        # that produces plans referencing fields their own query never emits.
        # A grain the compiler can defend and declare beats a generated plan.
        inferred = _inferred_presentation_unit(query, presentation)
        if not inferred:
            return None
        presentation = presentation.model_copy(update={"unit_of_analysis": inferred})
        assumptions.append(CompilationAssumption(
            code="grain_substituted",
            detail=f"No grouping was named, so this is grouped by {inferred.replace('_', ' ')}.",
        ))
    # "All transactions" is the router's declared universal quantifier: no
    # direction was requested, so no direction may be imposed. Carrying that
    # signal into metric selection is what stops an all-transaction chart from
    # quietly becoming an expenses-only chart.
    universal = query.transaction_type is None and query.metric in {"transaction_summary", "transaction_count"}
    composition = presentation.requested_mark == "arc" or presentation.visual_goal == "composition" or presentation.chart_type == "pie"
    named_unit = _explicit_presentation_unit(text) if text else None
    if universal and composition and presentation.value_semantics != "count":
        # A part-to-whole needs a whole. Expenses and income share no total, so
        # a mixed-direction composition over any other grain is not a chart
        # that can be drawn honestly — it can only be drawn by direction, or by
        # picking one direction and saying so.
        if named_unit in {None, "transaction", "transaction_type"}:
            # x_field/color_field are renderer bindings the router chose for the
            # grain it guessed. Carrying them past a grain change leaves the
            # effective contract describing two different charts at once.
            presentation = presentation.model_copy(update={
                "unit_of_analysis": "transaction_type",
                "x_field": None,
                "y_fields": [],
                "color_field": None,
            })
            assumptions.append(CompilationAssumption(
                code="direction_composed",
                detail="Composed by transaction type: expenses and income share no common total.",
            ))
        else:
            query = query.model_copy(update={"metric": "gross_spend", "transaction_type": TransactionType.EXPENSE})
            universal = False
            assumptions.append(CompilationAssumption(
                code="direction_restricted",
                detail=f"Restricted to expenses so the {named_unit.replace('_', ' ')} shares total 100%; income and transfers are excluded.",
            ))

    # One lookup settles both "can this grain be drawn at all" and "which mark
    # can carry it", replacing the chain of ad-hoc downgrades that used to
    # decide the same thing later and silently.
    shape = resolve_chart_shape(presentation)
    if shape is None:
        return None
    assumptions.extend(shape.assumptions)
    mark = shape.mark
    unit = presentation.unit_of_analysis
    temporal = shape.capability.temporal
    time_grain = "month" if unit == "month" else "day" if presentation.time_grain == "auto" else presentation.time_grain
    heatmap = mark == "rect"
    dimension = "time_segment" if heatmap else "time_bucket" if temporal else unit
    if unit == "transaction":
        dimensions = ["transaction", "merchant", "transaction_date"]
    elif temporal:
        dimensions = []
    else:
        dimensions = [dimension]
    series_field = None

    if presentation.value_semantics == "count":
        metric = "transaction_count"
        value_label = "Transaction count"
    elif query.transaction_type == TransactionType.INCOME:
        metric = "income"
        value_label = "Income"
    elif universal:
        # Any grain, not only a temporal one: an undirected request compiles to
        # the direction-neutral money metric with transaction_type preserved,
        # so expenses, income, refunds and transfers stay distinguishable.
        metric = "transaction_amount"
        value_label = "Transaction amount"
        if unit != "transaction_type":
            dimensions.append("transaction_type")
            series_field = "transaction_type"
    elif query.metric in {metric.name for metric in semantic_schema_registry().metrics}:
        metric = query.metric
        value_label = query.metric.replace("_", " ").title()
    else:
        metric = "gross_spend"
        value_label = "Amount"
    if metric == "transaction_amount" and query.transaction_type is None and unit != "transaction_type":
        if "transaction_type" not in dimensions:
            dimensions.append("transaction_type")
        series_field = "transaction_type"

    filters: list[FinanceFilter] = []
    for field, value, operator in (
        ("category", query.category_slug, "eq"),
        ("subcategory", query.subcategory_slug, "eq"),
        ("merchant", query.merchant, "contains"),
        ("account", query.account, "contains"),
        ("tag", query.tag, "eq"),
        ("amount", query.min_amount_minor, "gte"),
        ("amount", query.max_amount_minor, "lte"),
    ):
        if value is not None:
            filters.append(FinanceFilter(field=field, operator=operator, value=value))
    if metric == "transaction_count" and query.transaction_type:
        filters.append(FinanceFilter(field="transaction_type", value=query.transaction_type))

    # The mark came from the manifest above; this is only its display name.
    chart_type = {"rect": "heatmap", "arc": "pie"}.get(mark, mark)

    start_date = query.start_date or current_date.replace(day=1)
    end_date = query.end_date or current_date
    if query.start_date is None and not (presentation.rolling_value and presentation.rolling_unit):
        if universal:
            # "All transactions" states a scope, not a period. Month-to-date
            # would silently answer a narrower question than the one asked.
            start_date = current_date - timedelta(days=_WIDEST_GOVERNED_WINDOW_DAYS)
            assumptions.append(CompilationAssumption(
                code="defaulted_period",
                detail=(
                    "No period was given, so this covers the widest governed window: "
                    f"five years to {end_date:%-d %b %Y}."
                ),
            ))
        else:
            assumptions.append(CompilationAssumption(
                code="defaulted_period",
                detail=f"No period was given, so this covers month to date ({start_date:%-d %b} to {end_date:%-d %b}).",
            ))
    start_datetime = None
    end_datetime = None
    if presentation.rolling_value and presentation.rolling_unit:
        local_now = current_local_time(user_timezone).replace(tzinfo=None, microsecond=0)
        end_datetime = local_now
        rolling_value = presentation.rolling_value
        rolling_unit = presentation.rolling_unit
        if rolling_unit in {"minute", "hour", "day", "week"}:
            start_datetime = local_now - {
                "minute": timedelta(minutes=rolling_value),
                "hour": timedelta(hours=rolling_value),
                "day": timedelta(days=rolling_value),
                "week": timedelta(weeks=rolling_value),
            }[rolling_unit]
        else:
            import calendar
            month_count = rolling_value * {"month": 1, "quarter": 3, "year": 12}[rolling_unit]
            month_index = local_now.year * 12 + local_now.month - 1 - month_count
            target_year, month_zero = divmod(month_index, 12)
            target_day = min(local_now.day, calendar.monthrange(target_year, month_zero + 1)[1])
            start_datetime = local_now.replace(year=target_year, month=month_zero + 1, day=target_day)
        start_date = start_datetime.date()
        end_date = end_datetime.date()
    # Names a population, never a filter: reviewers of the compiled plan read
    # the old "Financial" label as an invented transaction filter.
    scope = (
        query.category_slug.replace("-", " ").title()
        if query.category_slug
        else "Spending" if metric == "gross_spend"
        else "All transactions"
    )
    unit_label = (time_grain if temporal else unit).replace("_", " ").title()
    query_name = f"{scope} by {unit_label}"
    mark = {"heatmap": "rect", "pie": "arc"}.get(chart_type, chart_type)
    value_encoding = VisualEncoding(
        field="value",
        type="quantitative",
        title=value_label,
        value_type="money_minor" if metric != "transaction_count" else "number",
    )
    dimension_encoding = VisualEncoding(
        field=dimension,
        type="temporal" if dimension == "time_bucket" else "ordinal" if heatmap else "nominal",
        title="Hour of day" if heatmap else unit_label,
        value_type="datetime" if dimension == "time_bucket" else "category",
        sort="ascending" if temporal or heatmap else None,
    )
    series_encoding = VisualEncoding(
        field=series_field,
        type="nominal",
        title=series_field.replace("_", " ").title(),
        value_type="category",
    ) if series_field else None
    detail_encodings = [
        VisualEncoding(field="merchant", type="nominal", title="Merchant", value_type="category"),
        VisualEncoding(field="transaction_date", type="temporal", title="Date", value_type="datetime"),
    ] if unit == "transaction" else []
    transforms: list[AnalysisTransform] = []
    transform_name = None
    if mark == "arc":
        # A composition is a derived share over canonical amounts. Preserve the
        # governed money query as evidence, then derive basis points in the
        # deterministic transform layer so the chart exposes both values.
        transform_name = f"{query_name} shares"
        transforms = [AnalysisTransform(
            name=transform_name,
            operation="share_of_total",
            query_name=query_name,
            dimension=dimension,
            limit=min(query.limit, 20),
        )]
        label_encoding = VisualEncoding(
            field="label", type="nominal", title=unit_label, value_type="category"
        )
        share_encoding = VisualEncoding(
            field="basis_points", type="quantitative", title="Share", value_type="percentage"
        )
        # value_encoding already carries the metric's own result type. Hard
        # coding money here mislabelled every count composition as currency.
        encoding = VisualEncodingSet(
            theta=share_encoding,
            color=label_encoding,
            tooltip=[label_encoding, value_encoding, share_encoding],
        )
    elif mark == "rect":
        row_encoding = VisualEncoding(
            field="time_bucket",
            type="temporal",
            title="Day",
            value_type="datetime",
            sort="ascending",
        )
        encoding = VisualEncodingSet(
            x=dimension_encoding,
            y=row_encoding,
            color=value_encoding,
            row=series_encoding,
            tooltip=[row_encoding, dimension_encoding, value_encoding, *([series_encoding] if series_encoding else [])],
        )
    else:
        encoding = VisualEncodingSet(
            x=dimension_encoding,
            y=value_encoding,
            color=series_encoding,
            tooltip=[dimension_encoding, *detail_encodings, value_encoding, *([series_encoding] if series_encoding else [])],
        )
    plan = AnalysisPlan(
        objective="descriptive",
        analysis_type="semantic_query",
        queries=[FinanceQueryPlan(
            name=query_name,
            metric=metric,
            dimensions=dimensions,
            filters=filters,
            start_date=start_date,
            end_date=end_date,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            time_grouping=TimeGrouping(grain=time_grain, timezone=user_timezone) if temporal and not heatmap else None,
            time_pivot=TimePivot(row_grain="day", column_component="hour_of_day", timezone=user_timezone) if heatmap else None,
            order="asc" if temporal else query.sort_direction,
            limit=query.limit,
        )],
        transforms=transforms,
        visualizations=[VisualizationSpec(
            name=f"{query_name} chart",
            query_name=query_name,
            transform_name=transform_name,
            mark=mark,
            encoding=encoding,
            title=f"{scope} {value_label.lower()} by {unit_label.lower()}",
            rationale=f"The selected visual grammar preserves {unit_label.lower()} as the unit of analysis.",
        )],
        safe_reasoning_summary=[
            f"Filter the governed records to {scope}",
            f"Preserve {unit_label.lower()} as the analysis grain",
            f"Render the validated {chart_type} chart",
        ],
    )
    return ChartCompilation(
        proposal=AnalysisToolProposal(
            name=f"{query_name} chart",
            description=f"Plot governed {scope.lower()} records by {unit_label.lower()} using {value_label.lower()}.",
            intent_signature=f"{scope.lower()} {unit.lower()} {presentation.value_semantics} chart",
            plan=plan,
        ),
        presentation=presentation,
        assumptions=tuple(assumptions),
    )


def build_financial_copilot(
    categories: list[dict],
    current_date: date,
    user_timezone: str,
    reusable_tools: list[dict] | None = None,
    enable_reasoning: bool = True,
    model_id: str | None = None,
    user_id: UUID | str | None = None,
    runtime_tools: list[Any] | None = None,
):
    """Build the semantic router with authenticated read-only runtime tools."""
    settings, taxonomy = _agent_context(categories)
    if settings is None:
        return None
    retrieved = json.dumps(reusable_tools or [], default=str)
    routing_schema = json.dumps(semantic_schema_registry().routing_contract(), default=str)
    return Agent(
        name="fyn AI Router",
        model=_responses_model(
            settings,
            model_id or settings.router_model,
            reasoning_effort="low" if enable_reasoning else "none",
            reasoning_summary="concise" if enable_reasoning else None,
            timeout=35,
        ),
        output_schema=CopilotRouteDecision,
        tools=runtime_tools or None,
        tool_call_limit=4 if runtime_tools else None,
        reasoning=False,
        **_memory_agent_options(user_id),
        instructions=[
            "Route the current prompt. Use only the supplied runtime tools for exact read-only database facts or deterministic calculations; do not perform extraction, analysis, calculation, or database work without them.",
            "Runtime tools are authenticated, read-only bindings to existing domain functions. Call the smallest sufficient tool for a simple factual inventory, lookup, count, or deterministic calculation. Never infer a database fact from memories or taxonomy prompt context when a runtime tool can read it.",
            "After a runtime tool fully answers the request, return route=conversation and write a concise reply supported only by the returned result. Do not call another tool when the first result is sufficient.",
            "A chart, graph, table, or export derived from a deterministic calculation must call a runtime tool that returns kind=computed_dataset. Keep route=analysis and presentation.mode=chart, then bind x_field and y_fields only to names in that returned field catalog. Do not send calculator output through the database semantic-query factory.",
            "For a computed dataset, x_field is the requested analysis step (for example installment) and y_fields are the requested measures. Multiple y_fields are valid and are converted to a governed long-form series by the domain layer. Never invent a field that the tool did not return.",
            "conversation is for ordinary conversation, capability discussion that needs no financial facts, or an answer grounded by a successful runtime tool call.",
            "transaction is for a financial event the user wants recorded, including incomplete or ambiguous record requests such as 'Add 500'. Keep uncertain transaction_type as unknown; the persisted draft state machine will render HITL selectors for missing fields.",
            "transaction_removal is for finding, deleting, removing, or undoing an existing financial record. Never route it as a new transaction.",
            "taxonomy is for creating or managing categories or subcategories. Populate taxonomy. A missing new name is valid because the domain workflow will ask for it. Never deny this capability in conversation.",
            "analysis is for read-only financial questions, comparisons, diagnostics, recommendations, or scenarios that are not fully answered by one supplied runtime tool and need governed widgets, record lists, coordinated views, or generated analysis.",
            "For a read-only analysis route, populate query. Use result_mode=transaction_list when the user asks to show, list, find, or see individual transactions (especially 'all expenses'). Preserve merchant, category, type, amount, account, tag, and date clues.",
            "Populate presentation independently from query semantics. For graph/chart/plot/visual/dashboard requests set presentation.mode=chart. Use layout=dashboard whenever the prompt requests a dashboard or several coordinated visual panels; this forces the multi-view analysis factory and must never collapse to one chart. Describe the analytical visual_goal as trend/comparison/composition/distribution/relationship/density and set requested_mark only when the user explicitly requests a mark; otherwise leave it auto so the analysis planner can choose from the data shape. Preserve unit_of_analysis and whether values represent amount, count, or percentage. For temporal requests choose time_grain from minute/hour/day/week/month/quarter/year and set rolling_value plus rolling_unit for rolling windows. A visual request always uses result_mode=complex_analysis and must never be downgraded to a list or scalar summary. Installment and calculation_step are valid units only for authenticated computed datasets, not database queries.",
            "Treat explicit entity nouns literally. In 'by transactions amount', transactions means one value per canonical transaction; it never means merchant totals. Use merchant grouping only when the user explicitly asks for merchant, vendor, restaurant, store, or business grouping.",
            "A single user turn may request several coordinated result shapes. When it asks for transaction rows plus a summary, breakdown, or ranking over the same records, populate query_bundle instead of query. Put every shared filter and date in base_query and put only presentation/aggregation choices in two to four views. Never force a composite request into one result_mode.",
            "For 'show again', 'refresh that table', or an equivalent request after a grounded query, copy the prior query definition into query_bundle.base_query and set refresh_from_active_analysis=true. This reruns current canonical records and must not bind stale entity IDs. By contrast, 'only those shown records' is an exact result-set refinement and uses use_active_scope=true.",
            "The active domain workflow may contain activeAnalysisState from the last grounded analysis. For a contextual analytical follow-up, first resolve the current message as a delta over that state: inherit its metric, period, dimensions, ordering and filters unless the user changes them. For example, after a highest-category query, 'and other than Food?' means the same ranking and period with category != Food.",
            "QueryInterpretation is the low-latency contract for queries it can express exactly. If the request needs an exclusion/negative filter, multiple filter values with nontrivial logic, a derived metric, set operation, nested condition, percentile, or any other operation QueryInterpretation cannot represent, use result_mode=complex_analysis so the governed analysis factory can emit generic typed FinanceFilter operators and transforms. Never silently drop an unsupported condition.",
            "A taxonomy word is not a merchant. If coffee, dining, travel, food, fuel, groceries, etc. resolves to a category/subcategory, do not also set merchant unless the user explicitly names a business or uses a merchant cue such as at/from/merchant/store. Avoid redundant filters that make the query narrower than the request.",
            "The active domain workflow may contain activeDataScope from the last grounded result. Treat it as optional context, never as a required filter. For contextual follow-ups such as these/those/the shown transactions/just those/the second one, set query.use_active_scope=true and preserve its period and filters unless the user explicitly changes them. Independent requests such as 'show last 5 transactions' must set use_active_scope=false even when activeDataScope exists. Return a complete refined query. Never populate scope_transaction_ids yourself; the domain layer binds the authoritative IDs.",
            "Use result_mode=summary for totals, grouped rankings, or breakdowns. Individual-record highest/lowest queries use result_mode=transaction_list, operation=rank, group_by=none, and limit=1. Grouped highest/lowest queries use result_mode=summary, operation=rank, and the requested group_by dimension. Set operation=breakdown plus group_by for grouped distributions. Never discard the requested dimension.",
            "Distinguish a grouping dimension from a filter. 'Which category is highest?' ranks groups with group_by=category. 'Highest spend in the Entertainment category' fixes category_slug=entertainment and ranks individual transactions with group_by=none, operation=rank, result_mode=transaction_list, and limit=1. Never group by the same single value already fixed by a filter.",
            "Use metric=income_summary with transaction_type=income for earnings, salary, income, or money-received totals. Use spending_summary only for expenses; use transaction_summary for neutral record queries.",
            "When the request maps to a governed semantic metric in the capability contract, use that exact metric id and result_mode=complex_analysis. Do not substitute a nearby dedicated scenario such as affordability.",
            "Questions about account balances, budgets, goals, loan portfolios, recurring patterns, subscriptions, or cross-entity finance data require analysis with result_mode=complex_analysis unless a dedicated scenario tool exactly matches.",
            "For spending questions with no explicit period, default start_date to the first day of the current month and end_date to the current date. Use complex_analysis only for diagnostics, recommendations, comparisons, and scenarios that require the analysis tool factory.",
            "planning is only for creating or contributing to a budget or goal.",
            "unknown is only for requests that cannot enter any safe typed workflow. Never use unknown merely because a transaction field is missing; use transaction and let HITL resolve it. reply is allowed only for conversation or unknown. A conversation reply may claim financial facts only when grounded by a successful runtime tool call; an unknown reply must not claim them.",
            "When reply is allowed, it may use concise GitHub-flavored Markdown for headings, lists, and emphasis. Do not put authoritative financial tables, record identifiers, forms, or actions in Markdown; those are emitted by validated domain widgets.",
            "safe_reasoning_summary must contain one to five short, user-safe plan steps. Describe what will be checked and which capability will run; never expose hidden chain-of-thought, internal tokens, or private deliberation.",
            f"Current date: {current_date.isoformat()}. User timezone: {user_timezone}.",
            f"Available expense taxonomy names for routing context: {taxonomy}",
            f"Queryable finance capabilities for routing (the analysis factory receives the full schema): {routing_schema}",
            (
                "Governed chart surface. These are the only grains the deterministic chart compiler can emit, "
                "with the marks each one can carry and the dimension field its query produces. Choose "
                "unit_of_analysis from this list, and choose requested_mark only from that grain's marks; a "
                "request outside the surface is rejected before any data is read. "
                f"{json.dumps(chart_capability_catalog(), default=str)}"
            ),
            f"Semantically retrieved validated capabilities (context only; do not copy answers): {retrieved}",
        ],
    )


def build_transaction_intelligence(categories: list[dict], current_date: date, user_timezone: str, user_id: UUID | str | None = None):
    settings, taxonomy = _agent_context(categories)
    if settings is None:
        return None
    return Agent(
        name="Transaction Intelligence",
        model=_responses_model(settings, settings.transaction_model, timeout=25),
        output_schema=TransactionInterpretation,
        reasoning=False,
        **_memory_agent_options(user_id),
        instructions=[
            "Extract only the financial event in the current user message. Do not create or save it.",
            "Recent conversation is context only. Use it to resolve explicit references such as 'same again' or 'make that 500', but never copy an older event into an unrelated current message.",
            "Amounts are integer minor units. Never fabricate merchants, accounts, dates, locations, or tags.",
            "Resolve relative dates from the supplied current date and list only directly stated values in explicit_fields.",
            "Category, subcategory, spend nature, and merchant normalization may be inferred; use unknown/null when weak.",
            f"Current date: {current_date.isoformat()}. User timezone: {user_timezone}.",
            f"Allowed taxonomy, including user-created names and their stable slugs: {taxonomy}",
            "Return the exact supplied category and subcategory slugs. When the current message explicitly names a taxonomy label, that hierarchy overrides a generic Other inference.",
        ],
    )


def build_analysis_tool_factory(categories: list[dict], current_date: date, user_timezone: str, enable_reasoning: bool = True, reusable_tools: list[dict] | None = None, user_id: UUID | str | None = None):
    settings, taxonomy = _agent_context(categories)
    if settings is None:
        return None
    return Agent(
        name="Finance Analysis Tool Factory",
        model=_responses_model(
            settings,
            settings.analysis_model,
            reasoning_effort="low" if enable_reasoning else "none",
            reasoning_summary="concise" if enable_reasoning else None,
            timeout=45,
        ),
        output_schema=AnalysisToolProposal,
        reasoning=enable_reasoning,
        reasoning_min_steps=1,
        reasoning_max_steps=2,
        **_memory_agent_options(user_id),
        instructions=[
            "Create a declarative, reusable, read-only finance analysis capability for exactly the user's request.",
            "Never emit SQL, Python, financial answers, or write commands. The deterministic harness validates, compiles, executes, and saves this specification.",
            "Resolve inclusive dates using the current date. Use only the supplied semantic catalog and taxonomy.",
            "Product date policy: 'last three months' includes the current month-to-date and the two preceding calendar months. Use previous complete months only when the user explicitly says complete/full months.",
            "Use up to eight focused queries. Add deterministic transforms for comparison, ranking, shares, period change, and change drivers.",
            "When the user requests a chart, graph, plot, or visual, create a renderer-neutral visualization grammar: choose a mark from bar/line/area/point/rect/arc/tick and map only query-produced fields to x/y/color/size/theta/row/column/tooltip encodings. Use line or area for ordered time, bar for categorical comparison, rect with x+y+quantitative color for a heatmap, and arc with theta+color only for small compositions. For layout=dashboard, create every requested panel as its own governed query/transform/view; never substitute one relevant chart for the complete dashboard. Never emit frontend code, arbitrary expressions, transforms, URLs, or unproduced fields.",
            "When the prompt includes an authoritative presentation contract, preserve its mode, unit_of_analysis, value_semantics, and visual intent. The governed grammar may express it with different marks and encodings, but it must not change the requested analytical grain.",
            "For unit_of_analysis=transaction, include dimensions transaction, merchant, and transaction_date; encode transaction on x, amount on y, and merchant/date in tooltip. Never replace transaction grain with merchant grouping.",
            "For temporal analysis use the compositional time_grouping operator and stable time_bucket output. Never invent transaction_hour or a separate dimension per time grain. Sub-day analysis is valid only when the entity exposes a governed event_time field.",
            "Reusable plans contribute structure only. Never copy missing_information that describes historical data availability; the governed executor checks current canonical records at run time.",
            "For transaction-level charts, query dimensions must include transaction plus useful tooltip fields such as merchant and transaction_date. Encode x=transaction and y=value. Do not aggregate multiple transactions into one merchant unless the user asks for merchant totals.",
            "For contextual follow-ups, activeAnalysisState is authoritative. Preserve its metric, dates, dimensions, filters, order and limit unless the current user message explicitly changes them. In particular, a prior top/highest query with limit=1 remains limit=1 when the user adds an exclusion.",
            "Add transforms only when the user explicitly requests a comparison, rank, share, trend, or explanation. A grouped result such as balances by account needs no rank transform unless top/highest/lowest was requested.",
            "For why-spending-changed analysis, query month plus category or merchant and use change_drivers.",
            "Recommendations must request the smallest relevant context_sources. If essential facts are unavailable, put them in missing_information rather than guessing.",
            "safe_reasoning_summary contains short user-visible workflow steps, never private chain-of-thought.",
            f"Current date: {current_date.isoformat()}. User timezone: {user_timezone}.",
            f"Expense taxonomy: {taxonomy}",
            f"Governed semantic catalog: {json.dumps(semantic_catalog(), default=str)}",
            f"Semantically retrieved validated plans: {json.dumps(reusable_tools or [], default=str)}. Reuse their governed structure when it fits, but resolve the current filters and dates independently.",
        ],
    )


def _runtime_tool_grounding(run_output: Any, runtime_tools: list[Any] | None) -> list[ToolGrounding]:
    """Keep evidence only for tools installed by this authenticated run."""
    allowed_names = {
        str(tool.name)
        for tool in (runtime_tools or [])
        if getattr(tool, "name", None)
    }
    grounding = []
    for execution in getattr(run_output, "tools", None) or []:
        name = getattr(execution, "tool_name", None)
        result = getattr(execution, "result", None)
        if (
            not name
            or name not in allowed_names
            or getattr(execution, "tool_call_error", False)
            or result is None
        ):
            continue
        structured = result
        if isinstance(result, str):
            try:
                structured = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                try:
                    structured = ast.literal_eval(result)
                except (ValueError, SyntaxError):
                    continue
        contract = runtime_tool_contract(name)
        schema_name = None
        if contract and contract.output_model:
            try:
                validated = contract.output_model.model_validate(structured)
            except Exception:
                continue
            structured = validated.model_dump(mode="json", exclude_none=True)
            schema_name = contract.output_model.__name__
        grounding.append(ToolGrounding(
            name=name,
            arguments=dict(getattr(execution, "tool_args", None) or {}),
            result=ToolResultEnvelope(tool=name, schema_name=schema_name, data=structured),
        ))
    return grounding[:4]


def interpret_with_financial_copilot(
    text: str,
    categories: list[dict],
    current_date: date,
    user_timezone: str,
    recent_context: list[dict],
    reusable_tools: list[dict] | None = None,
    workflow_context: dict | None = None,
    enable_reasoning: bool = True,
    router_model_id: str | None = None,
    user_id: UUID | str | None = None,
    runtime_tools: list[Any] | None = None,
    user_currency: str | None = None,
) -> CopilotDecision | None:
    context = _format_recent_context(recent_context)
    user_currency = user_currency or get_settings().default_currency
    prompt = (
        f"Active domain workflow (authoritative):\n{json.dumps(workflow_context or {}, default=str)}\n\n"
        f"Authenticated user's persisted currency: {user_currency}.\n"
        f"Recent conversation (context only):\n{context or '(none)'}\n\nCurrent user message:\n{text}"
    )
    router = build_financial_copilot(
        categories,
        current_date,
        user_timezone,
        reusable_tools,
        False,
        router_model_id,
        user_id,
        runtime_tools,
    )
    if not router:
        return None
    route_result = router.run(prompt, user_id=str(user_id)) if user_id else router.run(prompt)
    route = route_result.content if isinstance(route_result.content, CopilotRouteDecision) else CopilotRouteDecision.model_validate(route_result.content)
    tool_grounding = _runtime_tool_grounding(route_result, runtime_tools)
    route.presentation = _bind_explicit_presentation_unit(text, route.presentation)
    route.query = _bind_explicit_universal_scope(text, route.query)
    route.query = _bind_multi_direction_scope(text, route.query)
    wants_chart = route.presentation.mode == "chart"
    if wants_chart and _chartable_grounding(tool_grounding):
        return CopilotDecision(
            tool=CapabilityId.VISUALIZE_COMPUTATION,
            presentation=route.presentation,
            safe_reasoning_summary=route.safe_reasoning_summary,
            confidence=route.confidence,
            reason=route.reason,
            tool_grounding=tool_grounding,
        )
    if tool_grounding and not wants_chart:
        # Runtime evidence outranks a mistaken route label. A model may call a
        # sufficient calculator and still classify the turn as "analysis";
        # discarding that successful result would allow a later model to
        # recompute or paraphrase financial figures without evidence.
        return CopilotDecision(
            tool=CapabilityId.CONVERSATION,
            reply=route.reply if route.route is CopilotRoute.CONVERSATION else None,
            safe_reasoning_summary=route.safe_reasoning_summary,
            confidence=route.confidence,
            reason=route.reason,
            tool_grounding=tool_grounding,
        )
    if wants_chart and route.route in {CopilotRoute.CONVERSATION, CopilotRoute.UNKNOWN}:
        # A chart was asked for and no tool returned anything chartable. The
        # governed compiler can still answer from canonical records, so the
        # turn is routed there instead of ending in a model-authored apology
        # about the one scalar a calculator happened to return.
        route.route = CopilotRoute.ANALYSIS
    if route.route is CopilotRoute.TRANSACTION:
        extractor = build_transaction_intelligence(categories, current_date, user_timezone, user_id)
        if not extractor:
            return None
        extraction_prompt = (
            f"Recent conversation (context only):\n{context or '(none)'}\n\n"
            f"Current user message (extract this event):\n{text}"
        )
        extracted_result = extractor.run(extraction_prompt, user_id=str(user_id)) if user_id else extractor.run(extraction_prompt)
        transaction = extracted_result.content if isinstance(extracted_result.content, TransactionInterpretation) else TransactionInterpretation.model_validate(extracted_result.content)
        tool = CapabilityId.CREATE_TRANSACTION_DRAFT
    elif route.route is CopilotRoute.TRANSACTION_REMOVAL:
        transaction = None
        tool = CapabilityId.FIND_TRANSACTIONS_FOR_REMOVAL
    elif route.route is CopilotRoute.TAXONOMY:
        return CopilotDecision(tool=CapabilityId.MANAGE_TAXONOMY, taxonomy=route.taxonomy, safe_reasoning_summary=route.safe_reasoning_summary, confidence=route.confidence, reason=route.reason)
    elif route.route is CopilotRoute.ANALYSIS:
        if route.presentation.mode == "chart":
            compilation = _compile_governed_chart(route, current_date, user_timezone, text)
            if compilation:
                proposal = compilation.proposal
                presentation = compilation.presentation
                assumptions = list(compilation.assumptions)
            else:
                factory = build_analysis_tool_factory(categories, current_date, user_timezone, enable_reasoning, reusable_tools, user_id)
                if not factory:
                    return None
                factory_prompt = (
                    f"{prompt}\n\nAuthoritative presentation contract from the semantic router:\n"
                    f"{json.dumps(route.presentation.model_dump(mode='json'), default=str)}"
                )
                proposal_result = factory.run(factory_prompt, user_id=str(user_id)) if user_id else factory.run(factory_prompt)
                proposal = proposal_result.content if isinstance(proposal_result.content, AnalysisToolProposal) else AnalysisToolProposal.model_validate(proposal_result.content)
                presentation = route.presentation
                assumptions = []
            return CopilotDecision(
                tool=CapabilityId.RUN_ANALYSIS_HARNESS,
                analysis_tool=proposal,
                presentation=presentation,
                assumptions=assumptions,
                safe_reasoning_summary=proposal.plan.safe_reasoning_summary,
                confidence=route.confidence,
                reason=route.reason,
            )
        if route.query_bundle:
            return CopilotDecision(
                tool=CapabilityId.RUN_QUERY_BUNDLE,
                query_bundle=route.query_bundle,
                presentation=route.presentation,
                safe_reasoning_summary=route.safe_reasoning_summary,
                confidence=route.confidence,
                reason=route.reason,
            )
        if route.query and route.query.metric in {metric.name for metric in semantic_schema_registry().metrics}:
            route.query.result_mode = "complex_analysis"
        metric_tool = capability_for_metric(route.query.metric if route.query else None)
        if metric_tool:
            return CopilotDecision(tool=metric_tool, query=route.query, presentation=route.presentation, safe_reasoning_summary=route.safe_reasoning_summary, confidence=route.confidence, reason=route.reason)
        if route.query and route.query.result_mode == "transaction_list":
            return CopilotDecision(tool=CapabilityId.SEARCH_TRANSACTIONS, query=route.query, presentation=route.presentation, safe_reasoning_summary=route.safe_reasoning_summary, confidence=route.confidence, reason=route.reason)
        if route.query and route.query.result_mode == "summary":
            advanced_filters = any((
                route.query.merchant,
                route.query.subcategory_slug,
                route.query.account,
                route.query.tag,
                route.query.min_amount_minor is not None,
                route.query.max_amount_minor is not None,
                route.query.transaction_type not in (None, TransactionType.EXPENSE),
                route.query.operation in GROUPED_QUERY_OPERATIONS,
                route.query.group_by != "none",
            ))
            if advanced_filters:
                if route.query.transaction_type is None:
                    route.query.transaction_type = TransactionType.EXPENSE
                return CopilotDecision(tool=CapabilityId.SEARCH_TRANSACTIONS, query=route.query, presentation=route.presentation, safe_reasoning_summary=route.safe_reasoning_summary, confidence=route.confidence, reason=route.reason)
            return CopilotDecision(tool=CapabilityId.GET_SPENDING_SUMMARY, query=route.query, presentation=route.presentation, safe_reasoning_summary=route.safe_reasoning_summary, confidence=route.confidence, reason=route.reason)
        factory = build_analysis_tool_factory(categories, current_date, user_timezone, enable_reasoning, reusable_tools, user_id)
        if not factory:
            return None
        proposal_result = factory.run(prompt, user_id=str(user_id) if user_id else None)
        proposal = proposal_result.content if isinstance(proposal_result.content, AnalysisToolProposal) else AnalysisToolProposal.model_validate(proposal_result.content)
        return CopilotDecision(tool=CapabilityId.RUN_ANALYSIS_HARNESS, analysis_tool=proposal, presentation=route.presentation, safe_reasoning_summary=proposal.plan.safe_reasoning_summary, confidence=route.confidence, reason=route.reason)
    else:
        transaction = None
        tool = {
            CopilotRoute.CONVERSATION: CapabilityId.CONVERSATION,
            CopilotRoute.PLANNING: CapabilityId.PLANNING,
            CopilotRoute.UNKNOWN: CapabilityId.UNKNOWN,
        }[route.route]
    return CopilotDecision(
        tool=tool,
        transaction=transaction,
        reply=route.reply,
        safe_reasoning_summary=route.safe_reasoning_summary,
        confidence=route.confidence,
        reason=route.reason,
        tool_grounding=tool_grounding if tool is CapabilityId.CONVERSATION else [],
    )


def validate_copilot_decision(
    text: str,
    decision: CopilotDecision,
    current_date: date,
    user_timezone: str,
    workflow_context: dict | None = None,
    recent_context: list[dict] | None = None,
) -> CopilotDecisionValidation | None:
    """Use a fast independent critic to detect semantic contract loss before execution."""
    settings = _enabled_agent_settings()
    if settings is None:
        return None
    governed_schema = json.dumps(
        semantic_catalog() if decision.tool in SAFE_READ_CAPABILITIES else {
            "version": semantic_schema_registry().version,
            "financial_rules": semantic_schema_registry().financial_rules,
        },
        default=str,
    )
    validator = Agent(
        name="Financial Decision Validator",
        model=_responses_model(settings, settings.validator_model, timeout=20),
        output_schema=CopilotDecisionValidation,
        reasoning=False,
        instructions=[
            "Independently verify that the typed decision preserves the user's financial intent before any remaining domain workflow executes.",
            "tool_grounding is runtime-injected evidence from successful authenticated read-only tool executions that already occurred. A conversation decision with this evidence is a valid read response when its reply is supported by the named tool result; do not require an analysis route merely because the prompt asks for a financial or taxonomy fact. A conversation decision without tool_grounding still cannot claim database facts.",
            "Check action, transaction direction/type, requested metric, result shape, grouping dimension, merchant/category/account/tag/amount filters, and date period.",
            "Treat presentation as a required independent contract. A chart/graph/plot request requires presentation.mode=chart, tool=run_analysis_harness, at least one AnalysisPlan visualization, and a query whose unit of analysis and value semantics match presentation. Reject any chart request reduced to search_transactions, a table, or a scalar summary.",
            "For a multi-view dashboard, validate presentation as coverage: at least one view must preserve each explicitly requested analytical grain, metric, or composition. Do not require every panel to use the same axis; time-trend panels may use time_bucket while a category-composition panel uses category/label. Every view still must reference only fields produced by its own governed query or deterministic transform.",
            "When presentation.layout=dashboard, require at least two governed visualizations and verify that none of the measures or panels explicitly requested in the current prompt was omitted. Reject a single-chart substitute even when that chart is individually valid.",
            "For run_query_bundle, validate the bundle as one request: every requested result shape must have a view, all views must share base_query filters and dates by construction, and rows plus aggregates must preserve the same financial direction. Approve a refreshed prior query only when refresh_from_active_analysis=true and base_query reproduces its filters and period without stale scope_transaction_ids.",
            "For category or subcategory creation, require tool=manage_taxonomy and verify operation, requested name when present, and parent category when present. A missing name is valid and should lead to clarification.",
            "Use outcome=request_human_input when the chosen workflow is correct but a user-resolvable field is missing or ambiguous. In particular, create_transaction_draft with an amount but unknown transaction type is a safe HITL draft, not a rejection.",
            "Use outcome=reject only when the selected action/tool conflicts with the user's intent or would create a semantic or safety error. Do not reject merely because a safe typed workflow must ask a clarification question.",
            "Reject tool=unknown when the user is clearly initiating a financial record and a create_transaction_draft workflow can safely collect the missing type or details through HITL.",
            "When activeDataScope is supplied, treat it as optional conversation context. Verify that contextual refinements preserve it: a follow-up referring to these/those/the shown transactions must set use_active_scope=true; reject accidental expansion to unrelated records.",
            "activeAnalysisState describes the previous analytical question and is distinct from activeDataScope. Validate contextual analytical follow-ups against the inherited metric, dimensions, period, ordering and filters. If a follow-up introduces a condition that QueryInterpretation cannot express, require a run_analysis_harness decision with an equivalent governed FinanceFilter; never approve a plan that drops the condition.",
            "When activeAnalysisState is a ranked query, verify the generated FinanceQueryPlan preserves its sort direction and limit unless the current prompt explicitly changes them. A previous highest/top-one request must not become an unrestricted breakdown after adding a filter.",
            "For a highest/lowest request within one explicitly filtered category, merchant, account, or subcategory, require an individual-record rank (result_mode=transaction_list, operation=rank, group_by=none, limit=1) unless the prompt explicitly names another grouping dimension. Grouping by the same field already fixed to one value does not answer the question.",
            "An independent request such as 'show last 5 transactions' does not refer to activeDataScope, must use use_active_scope=false, and is valid without prior entity IDs. Never reject an independent query for an empty active-scope binding and never request bind_active_scope for it.",
            "If a genuinely contextual decision is otherwise semantically correct and its only defect is a missing active-scope binding, reject it with repairs=['bind_active_scope']. Do not request this repair for an independent query or for a genuinely wrong tool, metric, operation, direction, filter, or period.",
            "Reject a query that sets a generic taxonomy term as both merchant and category/subcategory without an explicit merchant cue or named business in the prompt.",
            "Reject expense tools for income/earnings questions, creation tools for deletion requests, summaries for requested record lists, and totals for ranking questions.",
            "Do not calculate, answer the user, infer database facts, or rewrite the decision. Return only the validation contract.",
            "For generated analysis decisions, validate every metric, entity, dimension, filter and relationship against the supplied governed semantic schema. Reject invented fields, disconnected joins, incompatible metric/entity pairs, or sensitive/raw projections.",
            "The temporal contract is compositional: FinanceQueryPlan.time_grouping.field=event_time is a governed virtual operator, and its stable produced output is time_bucket. time_bucket is therefore valid in a visualization encoding even though it is not a stored database column or ordinary dimension. start_date/end_date are the indexed candidate prefilter; start_datetime/end_datetime are the exact rolling bounds, so supplying both is valid.",
            "Date policy is evidence-bounded: 'this month' means month-to-date, from the first local calendar day through the supplied current date. Never require future dates or a future month end for a current-month financial answer.",
            "A temporal heatmap uses FinanceQueryPlan.time_pivot and produces time_bucket for rows plus time_segment for columns. Its renderer-neutral VisualizationSpec uses mark=rect with x=time_segment, y=time_bucket, and color=value. This is a valid governed projection, not an invented database field.",
            "A composition chart may query canonical money with gross_spend, derive share_of_total in the deterministic transform layer, and encode the transform's label as color plus basis_points as theta while retaining value as a money tooltip. That is the preferred governed part-to-whole contract and satisfies percentage semantics without changing the source metric.",
            "For a mixed-direction amount visualization, metric=transaction_amount is valid only when query dimensions include transaction_type and the visualization encodes transaction_type as color or a row/column facet. This keeps expenses, income, refunds, transfers, and other directions separate; approve that shape when it matches the prompt and never reinterpret it as spending or net cash flow.",
            "A part-to-whole chart requires one coherent total. An all-transaction composition therefore compiles either to a composition over transaction_type itself, or to one declared direction; both are correct governed answers. Do not demand that mixed directions be summed into a single pie.",
            "The decision's `assumptions` list is the compiler's disclosure of what it had to assume: an unstated period, a narrowed direction, a substituted mark or grain. Each one is shown to the user, who can correct it. Judge the plan against the prompt as qualified by these declared assumptions, and reject a narrowing only when it is absent from that list. A declared assumption is a disclosure, never an issue in itself.",
            f"Current date: {current_date.isoformat()}. User timezone: {user_timezone}.",
            f"Authoritative semantic schema (versioned entities, fields, relationships, metrics and policy): {governed_schema}",
        ],
    )
    payload = json.dumps(decision.model_dump(mode="json", exclude_none=True), default=str)
    context = json.dumps(workflow_context or {}, default=str)
    dialogue = _format_recent_context(recent_context or [])
    result = validator.run(
        f"Recent conversation (context only):\n{dialogue or '(none)'}\n\n"
        f"Current user prompt:\n{text}\n\n"
        f"Active domain workflow:\n{context}\n\nTyped decision to validate:\n{payload}"
    )
    validation = result.content if isinstance(result.content, CopilotDecisionValidation) else CopilotDecisionValidation.model_validate(result.content)
    contract_issues = _presentation_contract_issues(decision, text)
    if contract_issues:
        return CopilotDecisionValidation(
            outcome="reject",
            confidence=1.0,
            issues=contract_issues[:5],
            summary=" ".join(contract_issues)[:300],
        )
    return validation


def build_reconciliation_assistant():
    """Create the narrow Agno assistant only when an API key is configured.

    The caller must still apply deterministic thresholds and never let this
    assistant merge records directly.
    """
    settings = _enabled_agent_settings()
    if settings is None:
        return None
    return Agent(
        name="Reconciliation evaluator",
        model=_responses_model(settings, settings.reconciliation_model),
        output_schema=AIAssistedMatch,
        instructions=[
            "Compare only the supplied incoming observation and canonical transaction candidate. Candidate generation and invariant checks have already happened deterministically.",
            "Treat false merges as more dangerous than false splits.",
            "Use merchant aliases, transaction versus posted dates, source/account metadata, references, and description semantics as corroborating or contradicting evidence. Matching amount alone is never sufficient.",
            "same_transaction means both records describe one real-world financial event. Return false when evidence is conflicting or insufficient; uncertainty belongs in confidence and reason.",
            "Return structured evidence; never mutate or merge data.",
        ],
    )


def evaluate_reconciliation_match(
    observation: dict[str, Any],
    candidate: dict[str, Any],
    deterministic_signals: dict[str, Any],
) -> AIAssistedMatch | None:
    """Request bounded reconciliation advice without granting mutation access."""
    assistant = build_reconciliation_assistant()
    if not assistant:
        return None
    payload = {
        "incoming_observation": observation,
        "canonical_candidate": candidate,
        "deterministic_signals": deterministic_signals,
    }
    result = assistant.run(json.dumps(payload, default=str))
    return (
        result.content
        if isinstance(result.content, AIAssistedMatch)
        else AIAssistedMatch.model_validate(result.content)
    )
