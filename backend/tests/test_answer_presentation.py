from app.services.answer_presentation import (
    answer_presentation,
    operator_style_rules,
    repair_style_rule,
    turn_style_contract,
)
from app.services.preferences import AnswerStyle


def test_explained_template_values_reach_every_answer_building_prompt():
    presentation = answer_presentation(AnswerStyle.EXPLAINED)

    assert presentation.provider_verbosity == "medium"
    assert presentation.simple_lookup_min_sentences == 1
    assert presentation.simple_lookup_max_sentences == 2
    assert presentation.financial_term_limit == 2

    operator = " ".join(operator_style_rules(presentation))
    turn = turn_style_contract(presentation)
    repair = repair_style_rule(presentation)

    for rendered in (operator, turn, repair):
        assert "1 to 2" in rendered
        assert "interpret" in rendered
    assert "transaction list" in operator
    assert "financial copilot and patient teacher" in operator
    assert "no more than 2" in repair


def test_concise_template_values_remain_compact_everywhere():
    presentation = answer_presentation(AnswerStyle.CONCISE)

    assert presentation.provider_verbosity == "low"
    assert presentation.simple_lookup_min_sentences == 0
    assert presentation.simple_lookup_max_sentences == 1
    assert presentation.financial_term_limit == 0

    operator = " ".join(operator_style_rules(presentation))
    turn = turn_style_contract(presentation)
    repair = repair_style_rule(presentation)

    assert "every final answer" in operator
    assert "shortest complete answer" in operator
    assert "0 to 1" in turn
    assert "no more than 0" in repair


def test_trace_values_expose_configuration_without_copying_prompt_prose():
    values = answer_presentation(AnswerStyle.EXPLAINED).trace_values()

    assert values["style"] == "explained"
    assert values["persona"] == "a financial copilot and patient teacher"
    assert values["knowledge_level"] == "adaptive to the user's language"
    assert values["simple_lookup_min_sentences"] == 1
    assert values["simple_lookup_max_sentences"] == 2
    assert values["provider_verbosity"] == "medium"
    assert "evidence_interpretation" not in values
    assert "analytical_depth" not in values
