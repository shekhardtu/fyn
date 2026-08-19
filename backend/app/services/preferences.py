from __future__ import annotations

from enum import Enum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import UserPreference


ANSWER_VALIDATION_PREFERENCE_KEY = "agent:answer_validation"
ANSWER_STYLE_PREFERENCE_KEY = "agent:answer_style"


class AnswerValidationMode(str, Enum):
    """User-selectable checks applied after a successful tool run.

    These modes never alter query admission, tenant policies, or mutation
    guards.  They only control whether the drafted prose is checked before it
    is published.
    """

    FULL = "full"
    EVIDENCE_ONLY = "evidence_only"
    OFF = "off"


class AnswerStyle(str, Enum):
    """How the Operator presents an answer after the facts are available."""

    EXPLAINED = "explained"
    CONCISE = "concise"


def user_preference(db: Session, user_id: UUID, key: str) -> UserPreference | None:
    return db.scalar(select(UserPreference).where(
        UserPreference.user_id == user_id,
        UserPreference.key == key,
    ))


def set_user_preference(
    db: Session,
    user_id: UUID,
    key: str,
    value: dict,
    *,
    authority: str = "user",
) -> UserPreference:
    preference = user_preference(db, user_id, key)
    if preference:
        preference.value = value
        preference.authority = authority
    else:
        preference = UserPreference(
            user_id=user_id,
            key=key,
            value=value,
            authority=authority,
        )
        db.add(preference)
    return preference


def answer_validation_mode(
    db: Session,
    user_id: UUID,
) -> AnswerValidationMode:
    preference = user_preference(db, user_id, ANSWER_VALIDATION_PREFERENCE_KEY)
    raw_mode = preference.value.get("mode") if preference and isinstance(preference.value, dict) else None
    try:
        return AnswerValidationMode(raw_mode)
    except (TypeError, ValueError):
        return AnswerValidationMode.FULL


def set_answer_validation_mode(
    db: Session,
    user_id: UUID,
    mode: AnswerValidationMode,
) -> UserPreference:
    return set_user_preference(
        db,
        user_id,
        ANSWER_VALIDATION_PREFERENCE_KEY,
        {"mode": mode.value},
    )


def answer_style(
    db: Session,
    user_id: UUID,
) -> AnswerStyle:
    preference = user_preference(db, user_id, ANSWER_STYLE_PREFERENCE_KEY)
    raw_style = (
        preference.value.get("style")
        if preference and isinstance(preference.value, dict)
        else None
    )
    try:
        return AnswerStyle(raw_style)
    except (TypeError, ValueError):
        return AnswerStyle.EXPLAINED


def set_answer_style(
    db: Session,
    user_id: UUID,
    style: AnswerStyle,
) -> UserPreference:
    return set_user_preference(
        db,
        user_id,
        ANSWER_STYLE_PREFERENCE_KEY,
        {"style": style.value},
    )
