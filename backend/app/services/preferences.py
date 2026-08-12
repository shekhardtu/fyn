from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import UserPreference


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
