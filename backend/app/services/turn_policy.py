from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from ..operation_types import (
    AccessMode,
    AuthorizationOutcome,
    ConfirmationPolicy,
    ContextRelationship,
    DataEffect,
    IntentAuthority,
    RequestedEffect,
)
from .capabilities import CapabilitySpec
from .turn_signals import detect_turn_signals


class TurnIntentContract(BaseModel):
    """Server-authored effect ceiling carried from intake to dispatch."""

    schema_version: Literal[1] = 1
    context_relationship: ContextRelationship = ContextRelationship.STANDALONE
    authority: IntentAuthority = IntentAuthority.USER_TURN
    requested_access: AccessMode = AccessMode.UNKNOWN
    requested_effect: RequestedEffect = RequestedEffect.UNKNOWN
    read_evidence: list[str] = Field(default_factory=list, max_length=8)
    write_evidence: list[str] = Field(default_factory=list, max_length=8)
    ambiguous: bool = False


@dataclass(frozen=True)
class EffectAuthorization:
    outcome: AuthorizationOutcome
    code: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.outcome is AuthorizationOutcome.ALLOW


def resolve_turn_intent(
    text: str,
    context_relationship: ContextRelationship,
    *,
    authority: IntentAuthority = IntentAuthority.USER_TURN,
    implicit_transaction_entry: bool = False,
) -> TurnIntentContract:
    signals = detect_turn_signals(text)
    ambiguous = signals.financial_read_request and signals.mutation_request
    if ambiguous:
        requested_access = AccessMode.UNKNOWN
        requested_effect = RequestedEffect.UNKNOWN
    elif signals.financial_read_request:
        requested_access = AccessMode.READ
        requested_effect = RequestedEffect.NONE
    elif signals.mutation_request or signals.transaction_event:
        requested_access = AccessMode.WRITE
        requested_effect = RequestedEffect.MUTATION
    elif implicit_transaction_entry and context_relationship is ContextRelationship.STANDALONE:
        requested_access = AccessMode.WRITE
        requested_effect = RequestedEffect.DRAFT
    else:
        requested_access = AccessMode.UNKNOWN
        requested_effect = RequestedEffect.UNKNOWN
    return TurnIntentContract(
        context_relationship=context_relationship,
        authority=authority,
        requested_access=requested_access,
        requested_effect=requested_effect,
        read_evidence=list(signals.read_evidence),
        write_evidence=list(signals.write_evidence),
        ambiguous=ambiguous,
    )


def authorize_capability(
    intent: TurnIntentContract,
    capability: CapabilitySpec,
) -> EffectAuthorization:
    """Apply the request-to-effect policy at the execution boundary."""
    if intent.authority is IntentAuthority.SERVER_CONTINUATION:
        return EffectAuthorization(
            AuthorizationOutcome.ALLOW,
            "server_continuation",
            "A validated server continuation carries the original user's authority.",
        )
    if capability.maximum_effect in {DataEffect.NONE, DataEffect.DRAFT}:
        return EffectAuthorization(
            AuthorizationOutcome.ALLOW,
            "non_mutating_capability",
            "The capability cannot mutate durable financial data.",
        )
    if intent.requested_effect is RequestedEffect.NONE:
        return EffectAuthorization(
            AuthorizationOutcome.DENY,
            "read_to_mutation_escalation",
            "A read-only request cannot authorize a mutation-capable operation.",
        )
    if intent.ambiguous:
        return EffectAuthorization(
            AuthorizationOutcome.CLARIFY,
            "conflicting_effect_evidence",
            "The turn contains both read and mutation instructions.",
        )
    if intent.requested_effect is RequestedEffect.MUTATION:
        return EffectAuthorization(
            AuthorizationOutcome.ALLOW,
            "explicit_mutation",
            "The user explicitly requested or described a mutation.",
        )
    if (
        intent.requested_effect is RequestedEffect.DRAFT
        and capability.invokes("transaction.record@1")
    ):
        return EffectAuthorization(
            AuthorizationOutcome.ALLOW,
            "standalone_transaction_shortcut",
            "A standalone transaction shorthand may enter the governed draft workflow.",
        )
    if intent.context_relationship is not ContextRelationship.STANDALONE:
        return EffectAuthorization(
            AuthorizationOutcome.CLARIFY,
            "contextual_effect_ambiguous",
            "A contextual value does not independently authorize a data mutation.",
        )
    if capability.invokes("transaction.record@1"):
        # Preserve established natural ledger shorthand such as "two hundred
        # for dessert". Explicit read evidence was already denied above.
        return EffectAuthorization(
            AuthorizationOutcome.ALLOW,
            "standalone_transaction_statement",
            "A standalone non-query transaction statement may enter the governed draft workflow.",
        )
    if capability.confirmation is ConfirmationPolicy.REQUIRED:
        return EffectAuthorization(
            AuthorizationOutcome.ALLOW,
            "required_confirmation",
            "The capability cannot mutate until the user approves its typed action.",
        )
    return EffectAuthorization(
        AuthorizationOutcome.DENY,
        "mutation_authority_missing",
        "The turn did not establish authority for this mutation-capable operation.",
    )
