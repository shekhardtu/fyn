from __future__ import annotations

from .domain import ValueEnum


class AccessMode(ValueEnum):
    CONVERSATION = "conversation"
    READ = "read"
    COMPUTE = "compute"
    WRITE = "write"
    WORKFLOW = "workflow"
    UNKNOWN = "unknown"


class ContextRelationship(ValueEnum):
    """How the current turn relates to previously grounded conversation state."""

    STANDALONE = "standalone"
    FOLLOW_UP = "follow_up"
    CORRECTION = "correction"


class IntentAuthority(ValueEnum):
    """The source allowed to bind a turn to an executable effect."""

    USER_TURN = "user_turn"
    SERVER_CONTINUATION = "server_continuation"


class RequestedEffect(ValueEnum):
    """The strongest data effect supported by the current user intent."""

    NONE = "none"
    DRAFT = "draft"
    MUTATION = "mutation"
    UNKNOWN = "unknown"


class AuthorizationOutcome(ValueEnum):
    ALLOW = "allow"
    CLARIFY = "clarify"
    DENY = "deny"


class ValidationMode(ValueEnum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"


class DataEffect(ValueEnum):
    NONE = "none"
    DRAFT = "draft"
    MUTATION = "mutation"


class ConfirmationPolicy(ValueEnum):
    NEVER = "never"
    CONDITIONAL = "conditional"
    REQUIRED = "required"


EFFECT_STRENGTH = {
    DataEffect.NONE: 0,
    DataEffect.DRAFT: 1,
    DataEffect.MUTATION: 2,
}


CONFIRMATION_STRENGTH = {
    ConfirmationPolicy.NEVER: 0,
    ConfirmationPolicy.CONDITIONAL: 1,
    ConfirmationPolicy.REQUIRED: 2,
}
