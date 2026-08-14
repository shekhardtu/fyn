from __future__ import annotations

from dataclasses import dataclass
from ..domain import ValueEnum


class CapabilityId(ValueEnum):
    CONVERSATION = "conversation"
    REQUEST_CLARIFICATION = "request_clarification"
    CREATE_TRANSACTION_DRAFT = "create_transaction_draft"
    FIND_TRANSACTIONS_FOR_REMOVAL = "find_transactions_for_removal"
    MANAGE_TAXONOMY = "manage_taxonomy"
    SEARCH_TRANSACTIONS = "search_transactions"
    GET_SPENDING_SUMMARY = "get_spending_summary"
    GET_MONTHLY_COMPARISON = "get_monthly_comparison"
    GET_CHANGE_DRIVERS = "get_change_drivers"
    GET_BIGGEST_EXPENSES = "get_biggest_expenses"
    GET_RECURRING_EXPENSES = "get_recurring_expenses"
    CALCULATE_AFFORDABILITY = "calculate_affordability"
    CALCULATE_LOAN = "calculate_loan"
    CALCULATE_INVESTMENT_PROJECTION = "calculate_investment_projection"
    SHOW_RECONCILIATION_REVIEW = "show_reconciliation_review"
    RUN_ANALYSIS_HARNESS = "run_analysis_harness"
    RUN_QUERY_BUNDLE = "run_query_bundle"
    VISUALIZE_COMPUTATION = "visualize_computation"
    PLANNING = "planning"
    UNKNOWN = "unknown"


class AccessMode(ValueEnum):
    CONVERSATION = "conversation"
    READ = "read"
    COMPUTE = "compute"
    WRITE = "write"
    WORKFLOW = "workflow"
    UNKNOWN = "unknown"


class ExecutorKind(ValueEnum):
    CONVERSATION = "conversation"
    CLARIFICATION = "clarification"
    DRAFT = "draft"
    REMOVAL = "removal"
    TAXONOMY = "taxonomy"
    QUERY = "query"
    HARNESS = "harness"
    BUNDLE = "bundle"
    COMPUTED_VISUAL = "computed_visual"
    PLANNING = "planning"
    UNKNOWN = "unknown"


class ValidationMode(ValueEnum):
    """How much independent semantic review a routed decision needs.

    Typed read and calculator contracts are checked by deterministic domain
    code. A second model is reserved for mutation intent and generated or
    coordinated workflows where a plausible-looking route can carry a much
    larger semantic consequence.
    """

    DETERMINISTIC = "deterministic"
    MODEL = "model"


@dataclass(frozen=True)
class CapabilitySpec:
    id: CapabilityId
    access: AccessMode
    executor: ExecutorKind
    execution_label: str
    metric: str | None = None
    validation: ValidationMode = ValidationMode.DETERMINISTIC

    @property
    def is_safe_read(self) -> bool:
        return self.access in {AccessMode.READ, AccessMode.COMPUTE}

    @property
    def requires_model_validation(self) -> bool:
        return self.validation is ValidationMode.MODEL


CAPABILITY_REGISTRY: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(CapabilityId.CONVERSATION, AccessMode.CONVERSATION, ExecutorKind.CONVERSATION, "Writing contextual response"),
    CapabilitySpec(CapabilityId.REQUEST_CLARIFICATION, AccessMode.WORKFLOW, ExecutorKind.CLARIFICATION, "Preparing an interactive clarification"),
    CapabilitySpec(CapabilityId.CREATE_TRANSACTION_DRAFT, AccessMode.WRITE, ExecutorKind.DRAFT, "Extracting and validating transaction", validation=ValidationMode.MODEL),
    CapabilitySpec(CapabilityId.FIND_TRANSACTIONS_FOR_REMOVAL, AccessMode.READ, ExecutorKind.REMOVAL, "Finding transactions eligible for removal", validation=ValidationMode.MODEL),
    CapabilitySpec(CapabilityId.MANAGE_TAXONOMY, AccessMode.WORKFLOW, ExecutorKind.TAXONOMY, "Preparing a governed taxonomy change", validation=ValidationMode.MODEL),
    CapabilitySpec(CapabilityId.SEARCH_TRANSACTIONS, AccessMode.READ, ExecutorKind.QUERY, "Searching canonical transactions"),
    CapabilitySpec(CapabilityId.GET_SPENDING_SUMMARY, AccessMode.READ, ExecutorKind.QUERY, "Calculating spending summary"),
    CapabilitySpec(CapabilityId.GET_MONTHLY_COMPARISON, AccessMode.READ, ExecutorKind.QUERY, "Comparing monthly spending", "monthly_comparison"),
    CapabilitySpec(CapabilityId.GET_CHANGE_DRIVERS, AccessMode.READ, ExecutorKind.QUERY, "Finding spending change drivers", "change_drivers"),
    CapabilitySpec(CapabilityId.GET_BIGGEST_EXPENSES, AccessMode.READ, ExecutorKind.QUERY, "Finding biggest expenses", "biggest_expenses"),
    CapabilitySpec(CapabilityId.GET_RECURRING_EXPENSES, AccessMode.READ, ExecutorKind.QUERY, "Finding recurring expenses", "recurring_expenses"),
    CapabilitySpec(CapabilityId.CALCULATE_AFFORDABILITY, AccessMode.COMPUTE, ExecutorKind.QUERY, "Calculating affordability", "affordability"),
    CapabilitySpec(CapabilityId.CALCULATE_LOAN, AccessMode.COMPUTE, ExecutorKind.QUERY, "Opening deterministic loan calculator", "loan"),
    CapabilitySpec(CapabilityId.CALCULATE_INVESTMENT_PROJECTION, AccessMode.COMPUTE, ExecutorKind.QUERY, "Opening deterministic investment calculator", "investment_projection"),
    CapabilitySpec(CapabilityId.SHOW_RECONCILIATION_REVIEW, AccessMode.READ, ExecutorKind.QUERY, "Loading reconciliation review", "reconciliation_review"),
    CapabilitySpec(CapabilityId.RUN_ANALYSIS_HARNESS, AccessMode.READ, ExecutorKind.HARNESS, "Running the governed analysis harness", validation=ValidationMode.MODEL),
    CapabilitySpec(CapabilityId.RUN_QUERY_BUNDLE, AccessMode.READ, ExecutorKind.BUNDLE, "Running coordinated data views", validation=ValidationMode.MODEL),
    CapabilitySpec(CapabilityId.VISUALIZE_COMPUTATION, AccessMode.COMPUTE, ExecutorKind.COMPUTED_VISUAL, "Rendering a governed calculation dataset"),
    CapabilitySpec(CapabilityId.PLANNING, AccessMode.WORKFLOW, ExecutorKind.PLANNING, "Running planning workflow", validation=ValidationMode.MODEL),
    CapabilitySpec(CapabilityId.UNKNOWN, AccessMode.UNKNOWN, ExecutorKind.UNKNOWN, "Preparing clarification"),
)

_BY_ID = {item.id: item for item in CAPABILITY_REGISTRY}
_BY_METRIC = {item.metric: item.id for item in CAPABILITY_REGISTRY if item.metric}

if len(_BY_ID) != len(CapabilityId) or set(_BY_ID) != set(CapabilityId):
    raise RuntimeError("Capability registry must define every capability exactly once")
if {item.executor for item in CAPABILITY_REGISTRY} != set(ExecutorKind):
    raise RuntimeError("Capability registry must exercise every executor kind")


def capability_spec(capability_id: CapabilityId | str) -> CapabilitySpec:
    return _BY_ID[CapabilityId(capability_id)]


def capability_for_metric(metric: str | None) -> CapabilityId | None:
    return _BY_METRIC.get(metric)


def capabilities_for_executor(executor: ExecutorKind) -> frozenset[CapabilityId]:
    return frozenset(item.id for item in CAPABILITY_REGISTRY if item.executor == executor)


SAFE_READ_CAPABILITIES = frozenset(item.id for item in CAPABILITY_REGISTRY if item.is_safe_read)
QUERY_CAPABILITIES = capabilities_for_executor(ExecutorKind.QUERY)
