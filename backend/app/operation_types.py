from __future__ import annotations

from .domain import ValueEnum


class AccessMode(ValueEnum):
    CONVERSATION = "conversation"
    READ = "read"
    COMPUTE = "compute"
    WRITE = "write"
    WORKFLOW = "workflow"
    UNKNOWN = "unknown"


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
