from __future__ import annotations

import re

from ..models import AgentRun, Message
from ..schemas import AgentRunEvaluationOut
from .extraction import looks_like_financial_query


_FINANCIAL_FIGURE = re.compile(
    r"(?:₹|\$|€|£|\b(?:inr|usd|eur|gbp|rs\.?|rupees?)\b)\s*[\d,]+|\b\d+(?:\.\d+)?\s*%",
    re.IGNORECASE,
)


def evaluate_agent_reply(
    run: AgentRun,
    message: Message | None,
    *,
    completed_activity_count: int = 0,
    previous_assistant_text: str | None = None,
) -> AgentRunEvaluationOut | None:
    """Produce transparent, deterministic run signals for the dashboard.

    These are operational checks, not an LLM judge and not a claim of semantic
    truth. `evidence_passed` mirrors the production integrity boundary:
    financial figures need a structured citation/widget; replies without such
    claims do not need fabricated provenance.
    """
    if message is None:
        return None
    text = message.content.strip()
    words = len(re.findall(r"\b[\w'-]+\b", text))
    citations = len(message.citations or [])
    # The activity trace explains execution timing; it is not financial
    # evidence. Only domain widgets count toward grounding.
    widgets = len([
        widget
        for widget in (message.widgets or [])
        if not isinstance(widget, dict) or widget.get("type") != "agent_activity"
    ])
    input_text = str((run.input_payload or {}).get("text") or "")
    grounding_required = looks_like_financial_query(input_text)
    has_structured_evidence = citations > 0 or widgets > 0
    contains_financial_figure = bool(_FINANCIAL_FIGURE.search(text))
    complete = bool(text or widgets)
    evidence_passed = has_structured_evidence or not contains_financial_figure
    grounded = has_structured_evidence if grounding_required else True
    normalized_input = " ".join(input_text.casefold().split())
    normalized_reply = " ".join(text.casefold().split())
    normalized_previous = " ".join((previous_assistant_text or "").casefold().split())
    acknowledgement = bool(re.fullmatch(r"(?:thanks|thank you|okay|ok|got it|great)[!. ]*", normalized_input))
    repeated_previous_reply = bool(normalized_previous and normalized_reply == normalized_previous)
    echoed_repeated_input = bool(
        normalized_previous
        and normalized_input == normalized_previous
        and normalized_reply
        and normalized_reply in normalized_input
    )
    repeated_open_question = bool(
        acknowledgement
        and previous_assistant_text
        and "?" in previous_assistant_text
        and "?" in text
    )
    contextual = not (repeated_previous_reply or echoed_repeated_input or repeated_open_question)

    signals: list[str] = []
    if citations:
        signals.append("structured citations")
    if widgets:
        signals.append("governed widgets")
    if completed_activity_count:
        signals.append("audited execution steps")
    if not contains_financial_figure:
        signals.append("no unsupported financial figure")
    if repeated_previous_reply:
        signals.append("repeated previous assistant reply")
    if echoed_repeated_input:
        signals.append("echoed repeated assistant wording")
    if repeated_open_question:
        signals.append("acknowledgement reopened the same question")

    # Depth is deliberately a bounded explanation heuristic. Simple
    # conversation earns full credit by being concise; a financial/structured
    # answer earns it from explanation, evidence and visible execution work.
    if grounding_required or has_structured_evidence:
        depth_score = min(
            100,
            (30 if words >= 12 else 15 if words else 0)
            + (25 if len(re.findall(r"[.!?](?:\s|$)", text)) >= 2 else 10 if text else 0)
            + (25 if has_structured_evidence else 0)
            + (20 if completed_activity_count >= 2 else 10 if completed_activity_count else 0),
        )
    else:
        depth_score = 100 if 2 <= words <= 180 else 70 if complete else 0

    quality_score = round(
        (35 if complete else 0)
        + (25 if evidence_passed else 0)
        + depth_score * 0.2
        + (20 if contextual else 0),
    )
    correctness_basis = (
        "structured_evidence"
        if has_structured_evidence
        else "claim_integrity"
        if evidence_passed
        else "unsupported_financial_figure"
    )
    return AgentRunEvaluationOut(
        complete=complete,
        evidence_passed=evidence_passed,
        contextual=contextual,
        grounded=grounded,
        grounding_required=grounding_required,
        depth_score=depth_score,
        quality_score=quality_score,
        response_words=words,
        citation_count=citations,
        widget_count=widgets,
        correctness_basis=correctness_basis,
        signals=signals,
    )
