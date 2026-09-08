from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


_FINANCIAL_SUBJECT = re.compile(
    r"\b(?:spend|spent|spending|savings?|breakdown|expenses?|rupees?|money|"
    r"transactions?|income|salary|cash\s+flow|recurring|subscription|afford|"
    r"emi|interest|sip|investment|budgets?|goals?|loan|invoices?|vendors?|merchants?|"
    r"sheet|spreadsheet|upload(?:ed)?|chart|graph|plot|dashboard|category|categories)\b",
    re.I,
)
_FINANCIAL_READ_VERB = re.compile(
    r"\b(?:show|list|find|display|filter|summarize|analyse|analyze|compare|calculate|"
    r"review|estimate|project|forecast)\b",
    re.I,
)
_FINANCIAL_READ_REQUEST = re.compile(
    r"(?:^\s*|(?:[,;]|\band\b|\bthen\b)\s*)"
    r"(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
    r"(?:show|list|find|display|filter|summarize|analyse|analyze|compare|calculate|"
    r"review|estimate|project|forecast)\b",
    re.I,
)
_FINANCIAL_QUESTION = re.compile(
    r"^\s*(?:how|what|why|which|where|when|total)\b",
    re.I,
)
_AMOUNT_COMPARISON = re.compile(
    r"\b(?:above|below|over|under|more\s+than|less\s+than|at\s+least|at\s+most|minimum|maximum)\b"
    r"\s*(?:(?:₹|rs\.?|inr|usd|eur|gbp)\s*)?[0-9]",
    re.I,
)
_MUTATION_VERB = r"(?:add|change|correct|create|delete|edit|enter|log|make|record|remove|rename|replace|save|set|setup|set\s+up|update)"
# A command prefix is an instruction to the assistant, not a capability
# question ("can I"), a future intention ("I will"), or a quoted verb.
_REQUEST_PREFIX = (
    r"^\s*(?:(?:okay|ok)[, ]+)?(?:please\s+)?"
    r"(?:(?:let['’]s|let\s+us)\s+|(?:can|could|would)\s+you\s+(?:please\s+)?|"
    r"i\s+(?:want|need|would\s+like)\s+to\s+)?"
)
_MUTATION_REQUEST = re.compile(
    rf"{_REQUEST_PREFIX}{_MUTATION_VERB}\b",
    re.I,
)
_PLANNING_SUBJECT = re.compile(r"\b(?:budgets?|goals?|savings)\b", re.I)
_PLANNING_NON_ACTION = re.compile(
    r"^\s*(?:(?:please\s+)?(?:how|why|what|whether|explain|discuss|if)\b|"
    r"(?:can|could|should|would)\s+i\b|i\s+(?:will|might|may|plan\s+to)\b)"
    r"|\b(?:don['’]t|do\s+not|not\s+now|not\s+yet|later|before\s+that|"
    r"discuss|discussion|explain|whether)\b",
    re.I,
)
_PLANNING_VERB = (
    r"set\s+up|save\s+for|saving\s+for|"
    r"create|set|setup|start|make|add|contribute|put|update|change|lower|raise|"
    r"delete|remove|show|list|view|track"
)
_PLANNING_REQUEST = re.compile(
    rf"{_REQUEST_PREFIX}(?P<verb>{_PLANNING_VERB})\b(?P<target>[^!?;\n]*)[!?]?\s*$",
    re.I,
)


class PlanningCommand(str, Enum):
    BUDGET_SET = "budget_set"
    BUDGET_DELETE = "budget_delete"
    BUDGET_VIEW = "budget_view"
    GOAL_CREATE = "goal_create"
    GOAL_CONTRIBUTE = "goal_contribute"
    GOAL_VIEW = "goal_view"

    @property
    def budget_mutation(self) -> bool:
        return self in {self.BUDGET_SET, self.BUDGET_DELETE}

    @property
    def mutation(self) -> bool:
        return self not in {self.BUDGET_VIEW, self.GOAL_VIEW}


def planning_command(text: str) -> PlanningCommand | None:
    """One conservative contract for planning intake, authority, and execution.

    Only direct, single-action requests enter deterministic planning. Questions,
    deferred intentions, negation and unsupported commands remain agent-routed;
    their embedded verbs never select a financial operation.
    """
    if _PLANNING_NON_ACTION.search(text):
        return None
    match = _PLANNING_REQUEST.match(text)
    if not match:
        return None
    verb = " ".join(match["verb"].lower().split())
    target = match["target"]
    # Do not select the first action of a compound instruction. The semantic
    # agent must resolve it through the ordinary authorization boundary.
    if re.search(rf"(?:\b(?:and|then)\b|[,.])\s+(?:please\s+)?(?:{_PLANNING_VERB})\b", target, re.I):
        return None
    subject = _PLANNING_SUBJECT.search(target)
    if subject is None and verb not in {"save for", "saving for"}:
        return None
    subjects = {"budget" if item[0].lower().startswith("budget") else "goal" for item in _PLANNING_SUBJECT.finditer(target)}
    if len(subjects) > 1:
        return None
    budget = subject is not None and subject[0].lower().startswith("budget")
    if verb in {"show", "list", "view", "track"}:
        return PlanningCommand.BUDGET_VIEW if budget else PlanningCommand.GOAL_VIEW
    if budget:
        if verb in {"delete", "remove"}:
            return PlanningCommand.BUDGET_DELETE
        if verb in {"create", "set", "set up", "setup", "start", "make", "add", "update", "change", "lower", "raise"}:
            return PlanningCommand.BUDGET_SET
    elif verb in {"add", "contribute", "put"}:
        return PlanningCommand.GOAL_CONTRIBUTE
    elif verb in {"create", "set", "set up", "setup", "start", "make", "save for", "saving for"}:
        return PlanningCommand.GOAL_CREATE
    return None


_TRANSACTION_EVENT = re.compile(
    r"\b(?:bought|credited|deposited|earned|invested|moved|paid|received|spent|transfer|transferred|withdrew)\b",
    re.I,
)
_TRANSACTION_MUTATION_CUE = re.compile(
    r"\b(?:add|bought|credited|create|deposited|earned|enter|invested|log|moved|paid|received|record|save|spent|transfer|transferred|withdrew)\b",
    re.I,
)
_EXPECTED_VALUE_QUESTION = re.compile(
    r"\b(?:how\s+(?:many|much)|what(?:\s+(?:monthly|annual|yearly|weekly|daily|target|total|required|desired)){0,3}\s+"
    r"(?:amount|date|duration|number|percentage|rate|target|tenure|value)|"
    r"what\s+should\s+(?:(?:the|your|this)\s+)?"
    r"(?:(?:monthly|annual|yearly|weekly|daily|target|total|required|desired)\s+){0,3}"
    r"(?:amount|date|duration|number|percentage|rate|target|tenure|value)\s+be|"
    r"(?:enter|provide|specify|supply)\s+(?:an?\s+|the\s+|your\s+)?(?:amount|date|duration|number|percentage|rate|target|tenure|value))\b",
    re.I,
)


@dataclass(frozen=True)
class TurnSignals:
    financial_subject: bool
    financial_read_request: bool
    amount_comparison: bool
    mutation_request: bool
    transaction_event: bool

    @property
    def read_evidence(self) -> tuple[str, ...]:
        evidence = []
        if self.financial_read_request:
            evidence.append("financial_read_request")
        if self.amount_comparison:
            evidence.append("amount_comparison")
        return tuple(evidence)

    @property
    def write_evidence(self) -> tuple[str, ...]:
        evidence = []
        if self.mutation_request:
            evidence.append("mutation_request")
        if self.transaction_event:
            evidence.append("transaction_event")
        return tuple(evidence)


def has_amount_comparison(text: str) -> bool:
    """Return whether a number is being used as a query bound, not an event amount."""
    return bool(_AMOUNT_COMPARISON.search(text))


def has_explicit_transaction_mutation_cue(text: str) -> bool:
    """Return whether the user explicitly described or requested a ledger write."""
    return bool(_TRANSACTION_MUTATION_CUE.search(text))


def looks_like_financial_query(text: str) -> bool:
    """Broad intake signal for a request to read or analyse financial data."""
    lowered = text.lower()
    financial_subject = _FINANCIAL_SUBJECT.search(lowered)
    request_signal = re.search(
        r"^\s*(?:how|what|why|show|list|compare|can|could|which|give|tell|total|"
        r"using|project|forecast|analy[sz]e|review|estimate|summarize)\b"
        r"|\b(?:project|forecast|analy[sz]e|compare|calculate|estimate|summarize)\b"
        r"|\?\s*$",
        lowered,
    )
    return bool(
        financial_subject
        and (request_signal or _FINANCIAL_READ_VERB.search(lowered) or has_amount_comparison(lowered))
    ) or any(
        token in lowered
        for token in (
            "how much", "why did", "compare", "breakdown", "biggest expense",
            "recurring", "subscription", "afford", "spending", "duplicate",
            "reconciliation", "need review", "prepay", "interest save", "emi",
            "increase my sip", "investment projection", "add up to", "invoices",
            "budget sheet", "uploaded", "chart", "graph",
        )
    )


def detect_turn_signals(text: str) -> TurnSignals:
    financial_subject = bool(_FINANCIAL_SUBJECT.search(text))
    amount_comparison = has_amount_comparison(text)
    planning = planning_command(text)
    planning_discussion = bool(_PLANNING_SUBJECT.search(text) and _PLANNING_NON_ACTION.search(text))
    mutation_request = not planning_discussion and bool(
        (planning is not None and planning.mutation) or _MUTATION_REQUEST.search(text)
    )
    strong_read_request = bool(
        financial_subject
        and (
            planning_discussion
            or (planning is not None and not planning.mutation)
            or _FINANCIAL_READ_REQUEST.search(text)
            or _FINANCIAL_QUESTION.search(text)
            or (amount_comparison and not mutation_request)
            or re.search(r"\b(?:afford|breakdown|recurring|reconciliation|projection)\b", text, re.I)
        )
    )
    return TurnSignals(
        financial_subject=financial_subject,
        financial_read_request=strong_read_request,
        amount_comparison=amount_comparison,
        mutation_request=mutation_request,
        transaction_event=bool(_TRANSACTION_EVENT.search(text)),
    )


def expects_value_answer(text: str) -> bool:
    """Detect an assistant question whose next short number is contextual input."""
    normalized = " ".join(text.split())
    return bool(_EXPECTED_VALUE_QUESTION.search(normalized) and ("?" in normalized or "provide" in normalized.lower()))
