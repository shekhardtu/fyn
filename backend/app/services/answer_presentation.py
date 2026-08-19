from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from .preferences import AnswerStyle


ProviderVerbosity = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class AnswerPresentation:
    """Typed values shared by every prompt that builds financial prose.

    The application owns the templates and this typed value contract. User
    settings may select or override validated fields in the future, but raw
    prompt fragments never cross this boundary.
    """

    style: AnswerStyle
    style_label: str
    persona: str
    professional_lens: str
    knowledge_level: str
    simple_lookup_min_sentences: int
    simple_lookup_max_sentences: int
    financial_term_limit: int
    evidence_interpretation: str
    analytical_depth: str
    provider_verbosity: ProviderVerbosity

    def template_values(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "style": self.style.value,
        }

    def trace_values(self) -> dict[str, Any]:
        """Non-sensitive configuration recorded with a run for A/B review."""
        values = self.template_values()
        values.pop("evidence_interpretation")
        values.pop("analytical_depth")
        return values


def answer_presentation(style: AnswerStyle) -> AnswerPresentation:
    if style is AnswerStyle.CONCISE:
        return AnswerPresentation(
            style=style,
            style_label="Concise",
            persona="a direct financial copilot",
            professional_lens="general personal finance",
            knowledge_level="adaptive to the user's language",
            simple_lookup_min_sentences=0,
            simple_lookup_max_sentences=1,
            financial_term_limit=0,
            evidence_interpretation=(
                "State only the conclusion and the smallest useful evidence, "
                "plus one scope or comparison note when needed."
            ),
            analytical_depth=(
                "Prefer the shortest complete answer and do not add tutorial exposition."
            ),
            provider_verbosity="low",
        )
    return AnswerPresentation(
        style=style,
        style_label="Explained",
        persona="a financial copilot and patient teacher",
        professional_lens="general personal finance",
        knowledge_level="adaptive to the user's language",
        simple_lookup_min_sentences=1,
        simple_lookup_max_sentences=2,
        financial_term_limit=2,
        evidence_interpretation=(
            "Give every displayed table, list, chart, or key figure an adjacent "
            "plain-language interpretation that tells the reader what to notice."
        ),
        analytical_depth=(
            "For multi-period, driver, comparison, or scenario questions, explain the "
            "important relationship, direction, driver, implication, and one relevant caveat."
        ),
        provider_verbosity="medium",
    )


def _render(template: str, presentation: AnswerPresentation) -> str:
    return template.format_map(presentation.template_values()).strip()


_CONCISE_OPERATOR_TEMPLATE = """
The user selected the {style_label} answer style. Apply it to every final answer.
Write as {persona}, using a {professional_lens} lens at a level {knowledge_level}.
{evidence_interpretation} {analytical_depth}
Do not repeat the same fact in prose and a table unless the repetition states the takeaway.
"""

_EXPLAINED_OPERATOR_TEMPLATE = """
The user selected the {style_label} answer style. Apply it to every substantive financial
or data read, including a direct lookup, record list, taxonomy list, total, comparison,
and complex analysis. Write as {persona}, using a {professional_lens} lens at a level
{knowledge_level}. Lead with the conclusion, then explain what the displayed evidence means.
{evidence_interpretation} Never end after only an introductory sentence, raw evidence, and a
total; the user must not be left to interpret the screen alone.
For a simple lookup, add {simple_lookup_min_sentences} to {simple_lookup_max_sentences} useful
interpretive sentences. For a transaction list, explain the returned count, total, selected
scope, and any concentration or classification directly established by the rows. When one
matching record supplies the whole returned total, say that plainly.
{analytical_depth} Briefly define at most {financial_term_limit} financial terms when their
meaning is necessary to understand the answer.
Only translate a relationship into intuitive language when the tool result already supplies
the required share, ratio, difference, or comparison. Never invent a derived financial fact.
"""

_TURN_TEMPLATE = """
{style_label_upper} — {evidence_interpretation}
For a simple lookup, use {simple_lookup_min_sentences} to {simple_lookup_max_sentences}
interpretive sentences. {analytical_depth}
"""

_REPAIR_TEMPLATE = """
Use the {style_label} style for this repaired answer. Write as {persona} for a reader whose
knowledge level is {knowledge_level}. {evidence_interpretation} For a simple lookup, use
{simple_lookup_min_sentences} to {simple_lookup_max_sentences} useful interpretive sentences.
{analytical_depth} Define no more than {financial_term_limit} necessary financial terms.
"""


def operator_style_rules(presentation: AnswerPresentation) -> list[str]:
    template = (
        _CONCISE_OPERATOR_TEMPLATE
        if presentation.style is AnswerStyle.CONCISE
        else _EXPLAINED_OPERATOR_TEMPLATE
    )
    return [
        line.strip()
        for line in _render(template, presentation).splitlines()
        if line.strip()
    ]


def turn_style_contract(presentation: AnswerPresentation) -> str:
    values = presentation.template_values()
    values["style_label_upper"] = presentation.style_label.upper()
    return _TURN_TEMPLATE.format_map(values).strip()


def repair_style_rule(presentation: AnswerPresentation) -> str:
    return _render(_REPAIR_TEMPLATE, presentation)
