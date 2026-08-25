from __future__ import annotations

import re
from dataclasses import dataclass


_FINANCIAL_SUBJECT = re.compile(
    r"\b(?:spend|spent|spending|savings?|breakdown|expenses?|rupees?|money|"
    r"transactions?|income|salary|cash\s+flow|recurring|subscription|afford|"
    r"emi|interest|sip|investment|budget|loan|invoices?|vendors?|merchants?|"
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
_MUTATION_REQUEST = re.compile(
    rf"^\s*(?:okay[, ]+|ok[, ]+)?(?:please\s+)?{_MUTATION_VERB}\b"
    rf"|\b(?:can|could|would)\s+(?:you|i)\s+(?:please\s+)?{_MUTATION_VERB}\b"
    rf"|\b(?:want|need|would\s+like)\s+to\s+{_MUTATION_VERB}\b",
    re.I,
)
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
    mutation_request = bool(_MUTATION_REQUEST.search(text))
    strong_read_request = bool(
        financial_subject
        and (
            _FINANCIAL_READ_REQUEST.search(text)
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
