import pytest

from app.operation_types import (
    AuthorizationOutcome,
    ContextRelationship,
    IntentAuthority,
    RequestedEffect,
)
from app.services.capabilities import capability_for_primitive, capability_spec
from app.services.turn_policy import authorize_capability, resolve_turn_intent


def _capability(primitive: str):
    return capability_spec(capability_for_primitive(primitive))


@pytest.mark.parametrize(
    ("text", "relationship", "implicit_transaction", "expected_effect"),
    [
        ("show expenses above 8000", ContextRelationship.STANDALONE, False, RequestedEffect.NONE),
        ("drop Swiggy, keep the same period, and show expenses above 8000", ContextRelationship.FOLLOW_UP, False, RequestedEffect.NONE),
        ("remove the Toit expense from the list", ContextRelationship.STANDALONE, False, RequestedEffect.MUTATION),
        ("5000", ContextRelationship.STANDALONE, True, RequestedEffect.DRAFT),
        ("24", ContextRelationship.FOLLOW_UP, True, RequestedEffect.UNKNOWN),
    ],
)
def test_turn_intent_resolves_effect_independently_of_extracted_numbers(
    text,
    relationship,
    implicit_transaction,
    expected_effect,
):
    intent = resolve_turn_intent(
        text,
        relationship,
        implicit_transaction_entry=implicit_transaction,
    )

    assert intent.requested_effect is expected_effect


def test_read_intent_cannot_be_escalated_to_a_transaction_write():
    intent = resolve_turn_intent(
        "drop Swiggy, keep the same period, and show expenses above 8000",
        ContextRelationship.FOLLOW_UP,
    )

    authorization = authorize_capability(intent, _capability("transaction.record@1"))

    assert authorization.outcome is AuthorizationOutcome.DENY
    assert authorization.code == "read_to_mutation_escalation"


def test_read_intent_can_execute_reads_and_prepare_non_mutating_planning_views():
    intent = resolve_turn_intent(
        "show my travel budget",
        ContextRelationship.STANDALONE,
    )

    assert authorize_capability(intent, _capability("transaction.search@1")).allowed
    assert authorize_capability(intent, _capability("planning.run@1")).allowed


def test_standalone_amount_keeps_the_transaction_shortcut():
    intent = resolve_turn_intent(
        "5000",
        ContextRelationship.STANDALONE,
        implicit_transaction_entry=True,
    )

    authorization = authorize_capability(intent, _capability("transaction.record@1"))

    assert authorization.allowed
    assert authorization.code == "standalone_transaction_shortcut"


def test_contextual_number_does_not_independently_authorize_a_transaction():
    intent = resolve_turn_intent(
        "24",
        ContextRelationship.FOLLOW_UP,
        implicit_transaction_entry=True,
    )

    authorization = authorize_capability(intent, _capability("transaction.record@1"))

    assert authorization.outcome is AuthorizationOutcome.CLARIFY
    assert authorization.code == "contextual_effect_ambiguous"


def test_validated_server_continuation_can_resume_its_governed_effect():
    intent = resolve_turn_intent(
        "24",
        ContextRelationship.FOLLOW_UP,
        authority=IntentAuthority.SERVER_CONTINUATION,
    )

    authorization = authorize_capability(intent, _capability("transaction.record@1"))

    assert authorization.allowed
    assert authorization.code == "server_continuation"
