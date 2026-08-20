from __future__ import annotations

import ast
import re
from datetime import date
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.run.agent import (
    ReasoningContentDeltaEvent,
    ReasoningStep,
    ReasoningStepEvent,
    RunContentEvent,
    RunOutput,
    ToolCallCompletedEvent,
)
from pydantic import BaseModel, Field, WithJsonSchema, field_validator, model_validator

from ..config import Settings, get_settings
from ..domain import SpendNature, TaxonomyOperation, TransactionType
from ..validation import SEMANTIC_IDENTIFIER_PATTERN, SemanticIdentifier
from ..visualization_contracts import RequestedVisualMark
from ..operations import operation_catalog
from ..operations.execution import (
    OperationInputError,
    bind_operation_route_inputs,
    validate_operation_inputs,
)
from ..operations.models import CompiledOperation
from ..operations.tools import (
    OperationProposal,
    build_operation_proposal_tools,
    proposal_from_tool_execution,
)
from .agent_policies import AgentMode, policy_instructions, policy_name
from .agent_run_metrics import record_agno_run_metrics
from .answer_presentation import (
    AnswerPresentation,
    answer_presentation,
    operator_style_rules,
    repair_style_rule,
    turn_style_contract,
)
from .capabilities import (
    CapabilityId,
    capability_for_primitive,
    capability_invokes,
    safe_read_capabilities,
)
from .finance_time import FinanceRunContext
from .preferences import AnswerStyle
from .semantic import AnalysisToolProposal, semantic_catalog
from .sql_analysis import RUN_SQL_TOOL_NAME
from .semantic_registry import SortDirection, TIME_GRAIN_SPECS, TimeGrain, semantic_schema_registry
from .runtime_tools import runtime_tool_contract
from .user_memory import agent_memory_manager


RECENT_CONTEXT_TURN_LIMIT = 5
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
    verbosity: Literal["low", "medium", "high"] = "low",
    timeout: int | None = None,
) -> OpenAIResponses:
    options = {
        "id": model_id,
        "api_key": settings.openai_api_key,
        "reasoning_effort": reasoning_effort,
        "reasoning_summary": reasoning_summary,
        "verbosity": verbosity,
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


def _with_user_memory(agent: Agent, user_id: UUID | str | None) -> Agent:
    """Attach the dedicated memory store without enabling Agno session storage.

    Agno warns whenever an Agent has a MemoryManager but no Agent-level db,
    even when that manager already owns its Postgres db. Attaching after Agent
    initialization keeps the app conversation tables as the only session
    history while still enabling the real user memory manager.
    """
    if not user_id:
        return agent
    manager = agent_memory_manager()
    agent.user_id = str(user_id)
    if manager:
        if manager.model is None:
            manager.model = agent.model
        agent.memory_manager = manager
        agent.add_memories_to_context = True
        agent.update_memory_on_run = False
    return agent


def _format_recent_context(recent_context: list[dict]) -> str:
    """Serialize selected complete turns without clipping their replies.

    Selection and bounding happen at the turn level in the conversation
    service. JSON keeps role/content boundaries explicit and avoids turning a
    long assistant reply into an ambiguous prompt fragment.
    """
    rendered = []
    for item in recent_context:
        entry = {"role": str(item["role"]), "content": str(item["content"])}
        # The conversation service supplies only bounded lineage: query scope
        # and response-surface shape, never ledger rows or entity IDs. Keeping
        # it here lets a correction distinguish, for example, all-time from
        # month-to-date results instead of merely repeating the newest prose.
        if item.get("grounding"):
            entry["grounding"] = item["grounding"]
        if item.get("responseSurfaces"):
            entry["responseSurfaces"] = item["responseSurfaces"]
        rendered.append(entry)
    return json.dumps(
        rendered,
        ensure_ascii=False,
    )


class AIAssistedMatch(BaseModel):
    same_transaction: bool
    confidence: float = Field(ge=0, le=1)
    reason: str


InlineTransactionType = Annotated[
    TransactionType,
    WithJsonSchema({"type": "string", "enum": [item.value for item in TransactionType]}),
]
InlineSpendNature = Annotated[
    SpendNature,
    WithJsonSchema({"type": "string", "enum": [item.value for item in SpendNature]}),
]


class TransactionInterpretation(BaseModel):
    """Model contract with enum values inlined for provider compatibility.

    Pydantic otherwise emits a default beside an enum ``$ref``, a schema shape
    the model provider rejects. The annotations remain the concrete enums, so
    runtime validation is unchanged.
    """

    transaction_type: InlineTransactionType = TransactionType.UNKNOWN
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
    spend_nature: InlineSpendNature = SpendNature.UNKNOWN
    explicit_fields: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class QueryInterpretation(BaseModel):
    metric: SemanticIdentifier = "transaction_summary"
    result_mode: Literal["summary"] = "summary"
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


class ResolvedIntentContract(BaseModel):
    """Server-authored intent carried across a clarification interrupt.

    A clarification selection changes fields on an already chosen intent; it
    must not turn the original prompt plus a prose answer into a brand-new
    routing problem.  Only governed read capabilities are supported initially.
    Other clarification types are persisted as an explicit legacy transition
    until they have an equally precise domain contract.
    """

    schema_version: Literal[1] = 1
    context_mode: Literal["standalone", "follow_up", "correction"] = "standalone"
    capability: CapabilityId
    query: QueryInterpretation

    @model_validator(mode="after")
    def safe_governed_read(self):
        if self.capability not in safe_read_capabilities():
            raise ValueError("A resolved intent may execute only a governed read capability")
        if self.query.scope_transaction_ids:
            raise ValueError("A continuation cannot persist authoritative transaction IDs")
        if self.context_mode == "standalone" and self.query.use_active_scope:
            raise ValueError("A standalone continuation cannot inherit the prior result set")
        return self


class QueryView(BaseModel):
    """A result shape compiled over a QueryBundle's single governed scope."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    result_mode: Literal["summary"] = "summary"
    operation: QueryOperation
    group_by: QueryGroupBy = "none"
    sort_direction: SortDirection = "desc"
    limit: int = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def validate_result_contract(self):
        # Record listing left the bundle with the table widget; the Operator
        # reads rows through the transaction_list tool instead.
        if self.operation == "list":
            raise ValueError("Governed views cannot use operation=list")
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
    subcategories: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("name", "parent_category", mode="before")
    @classmethod
    def normalize_taxonomy_name(cls, value):
        if value is None:
            return None
        normalized = " ".join(str(value).split())
        return normalized or None

    @field_validator("subcategories", mode="before")
    @classmethod
    def normalize_subcategory_names(cls, value):
        if value is None:
            return []
        return [" ".join(str(item).split()) for item in value]

    @model_validator(mode="after")
    def validate_compound_path(self):
        if any(not item or len(item) > 80 for item in self.subcategories):
            raise ValueError("Subcategory names must be between 1 and 80 characters")
        normalized = [item.casefold() for item in self.subcategories]
        if len(normalized) != len(set(normalized)):
            raise ValueError("A taxonomy path cannot contain duplicate subcategories")
        if self.operation == "create_taxonomy_path":
            if not self.name or not self.subcategories:
                raise ValueError("A taxonomy path requires a category and at least one subcategory")
            if self.parent_category:
                raise ValueError("A taxonomy path creates its own parent category")
        elif self.subcategories:
            raise ValueError("Subcategories are only valid for create_taxonomy_path")
        return self


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


class ClarificationOption(BaseModel):
    """One safe interpretation the customer can choose explicitly."""

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=240)
    # This is server-side continuation context, not executable arguments. The
    # continuation compiler turns supported choices into typed transitions;
    # unsupported workflows are visibly marked as a one-route legacy resume.
    resolution: str = Field(min_length=3, max_length=500)
    # A cancellation is terminal: choosing it closes the HITL boundary without
    # asking the model to reinterpret the original mutating request.
    disposition: Literal["continue", "cancel"] = "continue"
    # A validator may resolve an incomplete taxonomy route into a complete,
    # executable plan. Keeping that plan typed prevents a confirmed mutation
    # from being translated back into prose and routed a second time.
    taxonomy: TaxonomyInterpretation | None = None

    @model_validator(mode="after")
    def cancel_has_no_executable_plan(self):
        if self.disposition == "cancel" and self.taxonomy is not None:
            raise ValueError("A cancellation option cannot carry a taxonomy plan")
        return self


class ClarificationRequest(BaseModel):
    """A material ambiguity that must be resolved before execution."""

    question: str = Field(min_length=3, max_length=500)
    reason: str = Field(min_length=3, max_length=500)
    conflict_fields: list[str] = Field(default_factory=list, max_length=8)
    options: list[ClarificationOption] = Field(min_length=2, max_length=6)
    allow_custom: bool = False
    custom_label: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def unique_options(self):
        option_ids = [option.id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("Clarification option ids must be unique")
        if self.allow_custom and "custom" in option_ids:
            raise ValueError("The custom clarification id is reserved")
        return self


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


GovernedWorkflow = Literal[
    "clarification",
    "transaction",
    "transaction_removal",
    "taxonomy",
    "advanced_analysis",
    "planning",
]


class OperatorResult(BaseModel):
    """The Operator's direct answer or terminal filesystem operation."""

    reply: str | None = None
    tool_grounding: list[ToolGrounding] = Field(default_factory=list, max_length=8)
    operation: OperationProposal | None = None
    streamed_live: bool = False
    reasoning_trace: str = ""

    @model_validator(mode="after")
    def one_terminal_result(self):
        terminal_count = sum(
            value is not None
            for value in (self.reply, self.operation)
        )
        if terminal_count > 1:
            raise ValueError("The Operator may return only one terminal result")
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
        "correction_scope_reconciled",
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
    candidate_template_id: UUID | None = None
    safe_reasoning_summary: list[str] = Field(default_factory=list, max_length=5)
    reply: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    reason: str = Field(max_length=300)
    tool_grounding: list[ToolGrounding] = Field(default_factory=list, max_length=8)
    validated_by: str | None = None
    validation_confidence: float | None = Field(default=None, ge=0, le=1)
    clarification: ClarificationRequest | None = None
    # Internal task outcome is deliberately separate from AG-UI transport
    # success. It is not model-authored and is set only by domain policy.
    task_status: Literal["succeeded", "degraded", "failed"] = "succeeded"
    failure_stage: str | None = Field(default=None, max_length=80)
    error_code: str | None = Field(default=None, max_length=80)
    operation_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{1,99}$")
    operation_version: int | None = Field(default=None, ge=1)
    operation_checksum: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    operation_inputs: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bind_clarification_to_capability(self):
        if capability_invokes(self.tool, "agent.clarify@1") and self.clarification is None:
            raise ValueError("The clarification capability requires a structured clarification")
        if not capability_invokes(self.tool, "agent.clarify@1") and self.clarification is not None:
            raise ValueError("Only the clarification capability may carry a clarification")
        operation_values = (self.operation_id, self.operation_version, self.operation_checksum)
        has_operation_revision = all(value is not None for value in operation_values)
        if any(value is not None for value in operation_values) and not has_operation_revision:
            raise ValueError("A filesystem operation revision requires id, version, and checksum")
        if has_operation_revision:
            operation = operation_catalog().snapshot().operation(str(self.operation_id))
            if operation is None:
                raise ValueError("The filesystem operation is not active")
            if operation.source == "core" and operation.id != self.tool.value:
                raise ValueError("A protected operation revision must match the selected capability")
            if operation.source == "managed" and not capability_invokes(self.tool, "managed.dispatch@1"):
                raise ValueError("A managed operation must use the generic managed dispatcher")
        elif capability_invokes(self.tool, "managed.dispatch@1"):
            raise ValueError("The managed-operation capability requires an id, version, and checksum")
        if len(json.dumps(self.operation_inputs, default=str)) > 50_000:
            raise ValueError("Operation inputs exceed the supported size")
        return self

    @model_validator(mode="after")
    def shed_chart_bindings_without_a_chart(self):
        """Chart-only presentation fields are noise on a non-chart decision.

        The planner may sample them freely because the schema allows it, and
        the model critic then judges the mismatch — a coin flip that rejected
        legitimate summary answers (a scalar ratio request, for one). The
        ambiguous state is normalized away here so it can never reach a model's
        judgment: a harness plan with no visualization and a non-chart mode
        keeps its analytical grain and value semantics but carries no marks or
        field bindings.
        """
        if (
            capability_invokes(self.tool, "analysis.run@1")
            and self.analysis_tool is not None
            and self.presentation.mode != "chart"
        ):
            presentation = self.presentation
            presentation.x_field = None
            presentation.y_fields = []
            presentation.color_field = None
            presentation.requested_mark = "auto"
            presentation.chart_type = "auto"
            presentation.visual_goal = "auto"
        return self




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

    Operator also classifies this, but as a model output it varies between
    samples and a successful runtime tool call used to override it. Reading it
    off the prompt makes the request a constraint the pipeline has to satisfy
    rather than a preference any later stage can drop.
    """
    return bool(_CHART_REQUEST_WORDS & set(_prompt_words(text)))


_INTERNAL_DIAGNOSTIC_TOKENS = re.compile(
    r"\b(?:transform|semantic|catalog|registry|executor|harness|plan(?:ner)?|schema|"
    r"contract|governed|capability|tool(?:ing)?|pipeline|dimension|metric)\b",
    re.I,
)


def contains_internal_analysis_diagnostic(missing_information: list[str]) -> bool:
    """Detect planner trace language leaking into user-facing missing inputs.

    missing_information may only name facts the customer can provide; wording
    about transforms, catalogs, schemas, or the executor is an internal
    limitation and must fail the plan instead of being asked of the user.
    """
    return any(
        _INTERNAL_DIAGNOSTIC_TOKENS.search(str(item or ""))
        for item in missing_information
    )


def filesystem_operation_decision(
    operation_id: str | None,
    operation_inputs: dict[str, Any] | None,
    *,
    confidence: float,
    reason: str,
    safe_reasoning_summary: list[str] | None = None,
    expected_version: int | None = None,
    expected_checksum: str | None = None,
) -> CopilotDecision | None:
    """Bind a model selection to one immutable active filesystem revision.

    An invented id, stale revision, or unexpected argument fails closed here.
    Core operation inputs are compiled through file-owned route bindings;
    managed inputs remain attached to the generic approval/execution path.
    """
    if not operation_id:
        return None
    operation = operation_catalog().snapshot().operation(operation_id)
    if operation is None:
        return None
    if expected_version is not None and operation.version != expected_version:
        return None
    if expected_checksum is not None and operation.checksum != expected_checksum:
        return None
    inputs = dict(operation_inputs or {})
    metric = inputs.get("metric")
    if isinstance(metric, str) and not re.fullmatch(SEMANTIC_IDENTIFIER_PATTERN, metric):
        # The metric is an advisory label on the typed query contract. A model
        # phrasing such as "grocery transactions" must not fail an otherwise
        # valid proposal — normalize it deterministically, or drop it so the
        # contract's default applies. The financial fields stay untouched.
        normalized = re.sub(r"[^a-z0-9_]+", "_", metric.casefold()).strip("_")[:64]
        if normalized and re.fullmatch(SEMANTIC_IDENTIFIER_PATTERN, normalized):
            inputs["metric"] = normalized
        else:
            inputs.pop("metric", None)
    try:
        validate_operation_inputs(operation, inputs, require_complete=False)
    except OperationInputError:
        return None
    summary = safe_reasoning_summary or [
        f"Use the governed {operation.definition.metadata.title} operation",
        "Validate its inputs and approval policy before execution",
    ]
    if operation.source == "managed":
        return CopilotDecision(
            tool=capability_for_primitive("managed.dispatch@1"),
            operation_id=operation.id,
            operation_version=operation.version,
            operation_checksum=operation.checksum,
            operation_inputs=inputs,
            confidence=confidence,
            reason=reason,
            safe_reasoning_summary=summary,
        )
    if operation.definition.routing.strategy != "decision":
        return None
    try:
        routed = bind_operation_route_inputs(operation, inputs)
        return CopilotDecision.model_validate({
            "tool": operation.id,
            **routed,
            "operation_id": operation.id,
            "operation_version": operation.version,
            "operation_checksum": operation.checksum,
            "operation_inputs": inputs,
            "confidence": confidence,
            "reason": reason,
            "safe_reasoning_summary": summary,
        })
    except (OperationInputError, ValueError):
        return None


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
    the user typed, so neither Operator nor a repair pass may reinterpret
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
    transaction_type can express. Reading it off the prompt keeps Operator
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
    intent or parse arbitrary language; the Operator has already done
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
            result_mode="summary",
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


def build_operator(
    categories: list[dict],
    current_date: date,
    user_timezone: str,
    *,
    model_id: str | None = None,
    user_id: UUID | str | None = None,
    runtime_tools: list[Any] | None = None,
    analysis_tools: list[Any] | None = None,
    reusable_tools: list[dict] | None = None,
    enable_reasoning: bool = True,
    answer_style: AnswerStyle = AnswerStyle.EXPLAINED,
    presentation: AnswerPresentation | None = None,
    operation_candidates: list[CompiledOperation] | None = None,
):
    """Build the single agent loop for one Operator turn.

    Authenticated read/calculation/analysis tools may execute. Every mutation
    or richer governed workflow can only emit one strict, operation-specific
    proposal; proposals stop the model turn and never execute directly.
    """
    settings, taxonomy = _agent_context(categories)
    if settings is None:
        return None
    selected_presentation = presentation or answer_presentation(answer_style)
    operation_tools = build_operation_proposal_tools(operation_candidates or [])
    tools = [*(runtime_tools or []), *(analysis_tools or []), *operation_tools]
    analysis_tool_names = {
        getattr(tool, "name", None) for tool in (analysis_tools or [])
    }
    sql_only_analysis = (
        RUN_SQL_TOOL_NAME in analysis_tool_names
        and "run_financial_analysis" not in analysis_tool_names
        and not any(str(name).startswith("bind_template__") for name in analysis_tool_names)
    )
    if sql_only_analysis:
        analysis_rules = [
            f"For every native-ledger financial analysis, call {RUN_SQL_TOOL_NAME}. Author the complete answer as one arbitrary PostgreSQL SELECT using the full schema in the tool description.",
            "Use CTEs, joins, subqueries, conditional aggregation, window functions, statistical functions, and derived expressions as needed. Compute averages, deltas, percentages, thresholds, rankings, and other answer fields inside SQL; never ask the reader to combine intermediate tables.",
            "Shape the SELECT for the final presentation: return one row per unit the user is comparing and only columns that help answer the question. Raw intermediate relations belong in CTEs, not in the response.",
            "For partial-period comparisons, align like-for-like elapsed days or label a projection explicitly. Treat a missing period as zero only when the data establishes that it is a real zero rather than missing coverage.",
            "The database injects and enforces the authenticated tenant. Never add or accept a user_id supplied by the model or user. Money is stored in integer minor units; alias money outputs with an _minor suffix.",
            "Tool rows are authoritative. Compose the answer from them using the most graspable single table or chart-like Markdown shape; do not expose SQL or invent arithmetic outside the returned columns.",
            "Answer in plain financial language: define terms such as baseline or average when they matter, explain what drove the result and why it matters, and include a short comparison-method note when useful. Never mention query plans, transforms, CTEs, executors, or other implementation vocabulary.",
            "Do not make the reader retain values across sections or perform mental subtraction. Put each compared value, its baseline, absolute difference, percentage difference, and meaningful driver together in the same row or adjacent callout.",
            "Before querying, privately check the requested grain, date alignment, canonical transaction types, currency, missing-period coverage, join fanout, denominators, money units, and whether the question permits causation or only association. After querying, sanity-check totals and identities before answering; correct the SQL if a result violates them. Do not reveal this internal checklist or chain of thought.",
            "Product date policy: 'last three months' means the current month-to-date plus the two preceding calendar months unless complete/full months are requested. 'Current' in a metric request means month-to-date.",
        ]
    else:
        analysis_rules = [
            "For any derived or complex financial question — summaries, breakdowns, comparisons, rankings, shares, trends, change drivers, projections, scenarios — run a governed analysis tool and compose the answer from its results.",
            "Prefer a bind_template__* tool when one matches the request exactly: fill every parameter from this request (dates as inclusive ISO dates, money as integer minor units) and it runs a pre-validated stored analysis directly.",
            "When no stored template fits, author a complete plan with run_financial_analysis. If its result carries an `error` key, correct the plan against the reported check and retry once or twice; never present an errored call as an answer.",
            "Analysis tool results are the authoritative values: `value_minor` fields are integer minor units (divide by 100 for the major unit), and `message` is a pre-verified grounded rendering you may quote or recompose.",
            "Compose the final answer as rich GitHub-flavored Markdown in whatever structure serves the answer best — headings, tables of any shape, nested lists, emphasis, short narrative. There are no fixed response components. Every figure must come verbatim from tool results; never do your own arithmetic.",
            f"Governed semantic catalog for run_financial_analysis plans: {json.dumps(semantic_catalog(), default=str)}",
            "Product date policy: 'last three months' means the current month-to-date plus the two preceding calendar months unless complete/full months are requested. 'Current' in a metric request means month-to-date.",
        ] if analysis_tools else []
    if RUN_SQL_TOOL_NAME in analysis_tool_names and not sql_only_analysis:
        analysis_rules.extend([
            f"When neither a stored template nor the AnalysisPlan grammar can express the request — an unusual join, exclusion, compound condition, window function, or custom aggregation — author one PostgreSQL SELECT with {RUN_SQL_TOOL_NAME}. Its tool description carries the exact table/column contract, this user's value catalog, and worked examples; use only tables and columns it lists.",
            f"{RUN_SQL_TOOL_NAME} rules: never filter by user_id (the database enforces tenancy), and alias money results with an _minor suffix. If its result carries an `error` key, correct the SQL against the reported code and retry at most twice; then answer from other evidence or say plainly what could not be verified.",
        ])
    return _with_user_memory(Agent(
        name=policy_name(AgentMode.OPERATE),
        # Agno sends vendor telemetry synchronously after yielding the final
        # run output. Its telemetry client has a 60-second network timeout, so
        # an unavailable analytics endpoint can otherwise keep an already
        # answered financial run active and leave the composer locked.
        telemetry=False,
        model=_responses_model(
            settings,
            model_id or settings.operator_model,
            reasoning_effort=(
                getattr(settings, "operator_analysis_reasoning_effort", "high")
                if enable_reasoning and sql_only_analysis
                else "low"
                if enable_reasoning
                else "none"
            ),
            reasoning_summary=(
                ("detailed" if settings.environment != "production" else "concise")
                if enable_reasoning
                else None
            ),
            timeout=35,
            verbosity=selected_presentation.provider_verbosity,
        ),
        tools=tools or None,
        tool_call_limit=(8 if analysis_tools else 4) if tools else None,
        reasoning=False,
        instructions=policy_instructions(
            AgentMode.OPERATE,
            task_rules=(
                [
                    "Own the current turn from understanding through the final answer when it is ordinary conversation or can be answered completely by the supplied authenticated read-only tools.",
                    "Read the recent complete turns and active domain context first. Resolve references to prior turns, but never copy a prior answer as the answer to a different current question.",
                    "For a claim about the user's records, taxonomy, spending, income, balances, recurring expenses, or a numeric scenario, call the smallest sufficient authenticated tool before answering. Tool results are the only source of financial facts.",
                    "When several supplied tools are independently required for the requested comparison, call only those tools. Do not repeat a tool call with identical arguments.",
                    "Answer directly from successful tool results. Start with the result, then explain the relevant scope, comparison, implication, or assumption. Preserve dates, currency, uncertainty, and record limits exactly.",
                    "Never estimate, extrapolate, or infer a financial figure, and never present taxonomy prompt context as if it were a database result. You may state one exact difference or total between figures a tool returned when it makes the comparison clearer — that is arithmetic over evidence, not a new fact. Anything further, including shares, percentages, ratios, multiples, and averages, must come from the tool that computed it.",
                    "When asked which subcategories a category has, call read_user_expense_taxonomy and list exactly the children it returned for exactly the category asked about — every one of them, nothing added, nothing renamed. Answering a subcategory question with the list of categories is answering a different question. If the category has no children, say so; if the name is not in the taxonomy, say that instead of offering the nearest match.",
                    "Never silently discard, override, or guess a supplied input when two plausible interpretations would materially change the result. Select the strict clarification operation with two to six concise choices only for an explicit material conflict or two genuinely plausible supplied interpretations; include a custom-answer path when the listed choices may not cover the customer’s intent. Do not select clarification merely because a valid read or analysis omits a period, filter, grouping, or optional preference: the selected operation owns safe defaults and missing-input collection. Set disposition=cancel on an option that abandons the request or makes no change.",
                    "Select exactly one filesystem operation proposal for any transaction create/update/delete/removal, category or subcategory change, budget or goal workflow, or export. Charts, dashboards, prior-result refinements and advanced analysis are NOT operations: they are answered by the analysis tools on this turn, which own the governed chart grammar. Listing individual transactions is NOT an operation either: call the transaction_list tool and write the answer yourself.",
                    "Populate the selected operation's typed fields from the current message and explicit conversation context. Use null for unknown optional values. A category plus its requested subcategories is one taxonomy operation, never several partial proposals.",
                    "For a planner-backed analysis operation, pass a self-contained request that preserves the requested layout, mark, analytical grain, value semantics, filters, and period.",
                    "For charts, dashboards, or exports, populate presentation independently from query semantics and preserve the requested layout, mark, analytical grain, value semantics, and time grain when stated.",
                    "When query inherits a prior analytical request, copy its exact dates, filters, direction, grouping, ordering, and limit from the recent grounding lineage unless the user changes them. Never replace an all-time period with this month merely because the current message omits dates.",
                    "Inherit that prior query only for an explicit continuation such as those, same, again, previous, and, or what about. A self-contained request starts a new query and must not copy an unstated transaction type, merchant, category, account, tag, amount bound, or date from recent grounding.",
                    "Treat correction language such as 'no', 'but', 'I mean', 'that is not what I asked', or a challenged count as a reconciliation request. Compare the relevant recent answers and their grounding scopes, identify which filter or period differs, acknowledge the mismatch, and issue the corrected tool call or handoff. Do not repeat only the latest answer.",
                    "When the current question repeats a recent question from this conversation, the earlier answer evidently did not satisfy them: never re-issue it verbatim. Acknowledge it was answered, answer again with a different framing or breakdown, and ask specifically what was missing or what they expected to see.",
                    "Filesystem operation tools are terminal control decisions. Call exactly one before writing any answer; supply null instead of guessing missing values. The server validates, asks for missing data, and applies approval before execution.",
                    "For ordinary conversation, respond naturally to the current message. Do not use a canned greeting, repeat the user's wording, append a generic question, or describe internal routing.",
                    "Requests to rename this chat, conversation, thread, or its page title are app-settings requests, not finance analysis: a deterministic confirmation flow already handles them, including asking for a missing title. If one still reaches you, ask conversationally what the new title should be; never hand such a request to a governed workflow and never claim a rename happened — you have no tool that renames.",
                    "'Current' in a metric request ('current ratio', 'current spending') means the month-to-date window from the first day of this month through today; call the right tool with those dates instead of asking which period.",
                    "Requests for question ideas or suggestions are normally answered by a deterministic suggestion flow before routing; if one still reaches you, offer at most three short, self-contained example questions with explicit periods and no blanks or placeholders.",
                    "Use GitHub-flavored Markdown only when it materially improves a substantive answer.",
                    "When the user wants the records themselves — a list, a table, 'show me', 'in tabular form' — call transaction_list and write the answer yourself in whatever Markdown presents it best for that question. Nothing downstream reshapes your Markdown, so the layout is entirely your call. Copy each amount, date, and name exactly as the tool returned it, and when the tool reports truncated, say the list is capped and give the tool's total for the full match.",
                    *operator_style_rules(selected_presentation),
                    *analysis_rules,
                    *operation_catalog().snapshot().common_instructions,
                    "Use the finance_runtime dependency as the authoritative local date, timezone, and inclusive-date policy for this run.",
                    f"Available expense taxonomy names for tool arguments only: {taxonomy}",
                ]
            ),
            output_contract="Return either grounded prose or one terminal strict filesystem operation proposal.",
        ),
        **FinanceRunContext(current_date, user_timezone, user_id).agno_options(),
    ), user_id)


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
    return grounding[:8]


def run_operator(
    text: str,
    categories: list[dict],
    current_date: date,
    user_timezone: str,
    recent_context: list[dict],
    *,
    workflow_context: dict | None = None,
    model_id: str | None = None,
    user_id: UUID | str | None = None,
    runtime_tools: list[Any] | None = None,
    analysis_tools: list[Any] | None = None,
    on_delta: Callable[[str], None] | None = None,
    on_reasoning_delta: Callable[[str], None] | None = None,
    allow_live_deltas: bool = False,
    answer_style: AnswerStyle = AnswerStyle.EXPLAINED,
    presentation: AnswerPresentation | None = None,
) -> OperatorResult | None:
    """Run one Operator turn and retain authenticated tool evidence.

    Personal-finance prompts are normally buffered by the caller until numeric
    evidence checks pass. Clearly non-financial conversation may forward the
    provider's exact deltas while this same run is still producing them.
    """
    runtime_settings = get_settings()
    operation_candidates = list(operation_catalog().candidate_operations(
        text,
        limit=runtime_settings.operation_candidate_limit,
        managed_only=False,
    ))
    selected_presentation = presentation or answer_presentation(answer_style)
    operator = build_operator(
        categories,
        current_date,
        user_timezone,
        model_id=model_id,
        user_id=user_id,
        runtime_tools=runtime_tools,
        analysis_tools=analysis_tools,
        answer_style=selected_presentation.style,
        presentation=selected_presentation,
        operation_candidates=operation_candidates,
    )
    if operator is None:
        return None
    prompt = (
        f"Active domain context (authoritative; select a filesystem operation if it requires a governed workflow):\n"
        f"{json.dumps(workflow_context or {}, ensure_ascii=False, default=str)}\n\n"
        f"Recent complete conversation turns:\n{_format_recent_context(recent_context) or '(none)'}\n\n"
        f"Current user message:\n{text}\n\n"
        "User-selected answer presentation contract (mandatory for this turn):\n"
        f"{turn_style_contract(selected_presentation)}"
    )
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    completed_tools = []
    final_output: RunOutput | None = None
    streamed_live = False
    stream = operator.run(
        prompt,
        user_id=str(user_id) if user_id else None,
        stream=True,
        stream_events=True,
        yield_run_output=True,
    )
    for event in stream:
        reasoning_delta = ""
        if isinstance(event, ReasoningContentDeltaEvent):
            reasoning_delta = event.reasoning_content
        elif isinstance(event, ReasoningStepEvent):
            content = event.content
            if isinstance(content, ReasoningStep):
                reasoning_delta = content.reasoning or event.reasoning_content
            elif isinstance(content, dict):
                reasoning_delta = str(content.get("reasoning") or event.reasoning_content or "")
            else:
                reasoning_delta = event.reasoning_content
        elif isinstance(event, RunContentEvent):
            reasoning_delta = event.reasoning_content or ""

        if reasoning_delta:
            reasoning_parts.append(reasoning_delta)
            if on_reasoning_delta:
                on_reasoning_delta(reasoning_delta)

        if isinstance(event, RunContentEvent) and isinstance(event.content, str) and event.content:
            content_parts.append(event.content)
            if allow_live_deltas and on_delta:
                on_delta(event.content)
                streamed_live = True
        elif isinstance(event, ToolCallCompletedEvent) and event.tool is not None:
            completed_tools.append(event.tool)
        elif isinstance(event, RunOutput):
            final_output = event
            # RunOutput is Agno's terminal stream value. Do not advance the
            # generator again: framework bookkeeping after this yield is not
            # part of the customer response and must never delay RUN_FINISHED.
            break

    if final_output is not None:
        record_agno_run_metrics(
            final_output,
            stage="operator_response",
            model=model_id or runtime_settings.operator_model,
        )

    final_reasoning = (
        final_output.reasoning_content
        if final_output and isinstance(final_output.reasoning_content, str)
        else ""
    )
    streamed_reasoning = "".join(reasoning_parts)
    if final_reasoning and not streamed_reasoning:
        reasoning_parts.append(final_reasoning)
        if on_reasoning_delta:
            on_reasoning_delta(final_reasoning)
    elif final_reasoning and final_reasoning.startswith(streamed_reasoning):
        remainder = final_reasoning[len(streamed_reasoning):]
        if remainder:
            reasoning_parts.append(remainder)
            if on_reasoning_delta:
                on_reasoning_delta(remainder)
    reasoning_trace = "".join(reasoning_parts)

    executions = list(final_output.tools or []) if final_output else completed_tools
    if not executions:
        executions = completed_tools
    operation_proposal = next(
        (
            proposal
            for execution in executions
            if (proposal := proposal_from_tool_execution(execution, operation_candidates))
            is not None
        ),
        None,
    )
    evidence_tools = [*(runtime_tools or []), *(analysis_tools or [])]
    if operation_proposal is not None:
        return OperatorResult(
            operation=operation_proposal,
            tool_grounding=_runtime_tool_grounding(
                SimpleNamespace(tools=executions),
                evidence_tools,
            ),
            streamed_live=streamed_live,
            reasoning_trace=reasoning_trace,
        )
    grounding = _runtime_tool_grounding(
        SimpleNamespace(tools=executions),
        evidence_tools,
    )
    streamed_text = "".join(content_parts)
    final_text = final_output.content if final_output and isinstance(final_output.content, str) else None
    reply = streamed_text or final_text
    return OperatorResult(
        reply=reply,
        tool_grounding=grounding,
        streamed_live=streamed_live,
        reasoning_trace=reasoning_trace,
    )


class GroundedAnswerRepair(BaseModel):
    markdown: str = Field(min_length=1, max_length=30_000)


def repair_grounded_answer(
    question: str,
    original_answer: str,
    obligations: list[str],
    evidence: list[dict[str, Any]],
    current_date: date,
    user_timezone: str,
    answer_style: AnswerStyle = AnswerStyle.EXPLAINED,
    presentation: AnswerPresentation | None = None,
) -> str | None:
    """Recompose valid evidence once when only answer coverage is missing.

    This pass receives typed result facts, not database access. It cannot add a
    fact or calculate a new value; the same deterministic evidence validator
    checks its output before anything reaches the transcript.
    """

    settings = _enabled_agent_settings()
    if settings is None:
        return None
    selected_presentation = presentation or answer_presentation(answer_style)
    composer = Agent(
        name="Grounded answer repair",
        telemetry=False,
        model=_responses_model(
            settings,
            settings.operator_model,
            reasoning_effort="low",
            verbosity=selected_presentation.provider_verbosity,
            timeout=15,
        ),
        output_schema=GroundedAnswerRepair,
        reasoning=False,
        instructions=[
            "Rewrite the answer so it fulfils every supplied obligation using only the supplied typed evidence.",
            "Never calculate, estimate, interpolate, rename an entity, or introduce a financial number that is absent from the evidence.",
            "Preserve the evidence's units and scope. Money evidence is already expressed in major currency units for writing.",
            repair_style_rule(selected_presentation),
            "Do not mention validation, tools, SQL, evidence IDs, databases, or this repair pass.",
        ],
        **FinanceRunContext(current_date, user_timezone).agno_options(),
    )
    result = composer.run(json.dumps({
        "question": question,
        "original_answer": original_answer,
        "missing_obligations": obligations,
        "typed_evidence": evidence[:300],
    }, ensure_ascii=False, default=str))
    record_agno_run_metrics(
        result,
        stage="grounded_answer_repair",
        model=settings.operator_model,
    )
    repaired = (
        result.content
        if isinstance(result.content, GroundedAnswerRepair)
        else GroundedAnswerRepair.model_validate(result.content)
    )
    return repaired.markdown.strip()


class RelatedQuestionSuggestions(BaseModel):
    questions: list[str] = Field(default_factory=list, max_length=3)


def suggest_related_questions(
    question: str,
    answer: str,
    recent_turns: list[dict],
    capability_notes: list[str],
    current_date: date,
    user_timezone: str,
) -> list[str]:
    """One fast pass proposing the user's likely next analytical step.

    Modeled on how research assistants surface follow-ups: generated after the
    answer settles, weighted hardest toward the immediate question and answer,
    and bounded to what the product can actually answer so a tapped suggestion
    never leads into a clarification loop or a refusal.
    """
    settings = _enabled_agent_settings()
    if settings is None:
        return []
    suggester = Agent(
        name=policy_name(AgentMode.SUGGEST),
        telemetry=False,
        model=_responses_model(settings, settings.suggester_model, timeout=12),
        output_schema=RelatedQuestionSuggestions,
        reasoning=False,
        instructions=policy_instructions(
            AgentMode.SUGGEST,
            task_rules=[
                "Propose up to three follow-up questions, written in the user's own voice, that they are most likely to want next.",
                "Weight the just-completed question and answer far above older turns. Reference the concrete entities, categories, merchants, periods, or figures that answer actually contains.",
                "Prefer this progression: one drill-down into the current answer, one comparison or trend over time, and one action step (budget, goal, or cleanup) when the context genuinely supports it.",
                "Each question must be short enough to scan as a tappable chip — at most about nine words — and fully self-contained, with an explicit period and no pronouns that need this conversation to resolve.",
                "Only suggest what the listed capabilities can answer end to end. Never suggest connecting accounts, exporting, charts of unsupported grains, or anything outside them.",
                "Never repeat or trivially rephrase a question the user already asked in this conversation. If nothing genuinely useful remains, return fewer questions or none.",
                "Use the finance_runtime dependency as the authoritative local date, timezone, and inclusive-date policy for this run.",
                "Capabilities that can answer suggested questions:\n" + "\n".join(f"- {note}" for note in capability_notes),
            ],
            output_contract="Return exactly one RelatedQuestionSuggestions object.",
        ),
        **FinanceRunContext(current_date, user_timezone).agno_options(),
    )
    dialogue = _format_recent_context(recent_turns or [])
    result = suggester.run(
        f"Recent conversation (context only):\n{dialogue or '(none)'}\n\n"
        f"Question just asked:\n{question}\n\n"
        f"Answer just given:\n{answer}"
    )
    record_agno_run_metrics(result, stage="related_question_suggester", model=settings.suggester_model)
    content = result.content if isinstance(result.content, RelatedQuestionSuggestions) else RelatedQuestionSuggestions.model_validate(result.content)
    already_asked = {
        " ".join(str(turn.get("content", "")).casefold().split())
        for turn in recent_turns or []
        if turn.get("role") == "user"
    }
    suggestions: list[str] = []
    for item in content.questions:
        text = " ".join(str(item).split())
        if not text or len(text) > 160:
            continue
        if " ".join(text.casefold().split()) in already_asked:
            continue
        if text not in suggestions:
            suggestions.append(text)
    return suggestions[:3]


def build_reconciler():
    """Create the narrow reconciliation agent when model access is configured.

    The caller must still apply deterministic thresholds and never let this
    assistant merge records directly.
    """
    settings = _enabled_agent_settings()
    if settings is None:
        return None
    return Agent(
        name=policy_name(AgentMode.RECONCILE),
        telemetry=False,
        model=_responses_model(settings, settings.reconciler_model),
        output_schema=AIAssistedMatch,
        instructions=policy_instructions(
            AgentMode.RECONCILE,
            task_rules=[
            "Compare only the supplied incoming observation and canonical transaction candidate. Candidate generation and invariant checks have already happened deterministically.",
            "Treat false merges as more dangerous than false splits.",
            "Use merchant aliases, transaction versus posted dates, source/account metadata, references, and description semantics as corroborating or contradicting evidence. Matching amount alone is never sufficient.",
            "same_transaction means both records describe one real-world financial event. Return false when evidence is conflicting or insufficient; uncertainty belongs in confidence and reason.",
            "Return structured evidence; never mutate or merge data.",
            ],
            output_contract="Return exactly one AIAssistedMatch object.",
        ),
    )


def evaluate_reconciliation_match(
    observation: dict[str, Any],
    candidate: dict[str, Any],
    deterministic_signals: dict[str, Any],
) -> AIAssistedMatch | None:
    """Request bounded reconciliation advice without granting mutation access."""
    reconciler = build_reconciler()
    if not reconciler:
        return None
    payload = {
        "incoming_observation": observation,
        "canonical_candidate": candidate,
        "deterministic_signals": deterministic_signals,
    }
    result = reconciler.run(json.dumps(payload, default=str))
    record_agno_run_metrics(
        result,
        stage="reconciliation",
        model=get_settings().reconciler_model,
    )
    return (
        result.content
        if isinstance(result.content, AIAssistedMatch)
        else AIAssistedMatch.model_validate(result.content)
    )
