"""Browser sessions carried by an opaque cookie.

The cookie holds 256 bits of randomness and the database holds only its SHA-256
digest, so a copy of the table cannot be replayed as a live session. There is
nothing to peppered-hash here — the token has no guessable structure, unlike a
six-digit code.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..event_time import as_utc, now_utc
from ..models import User, UserSession


# Below this much remaining life a session in active use is extended, so a daily
# user is never signed out mid-task while an abandoned one still expires.
RENEWAL_THRESHOLD = 0.5
# Recording every request would write once per API call for no added meaning.
LAST_USED_RESOLUTION = timedelta(hours=1)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return now_utc()


def session_lifetime() -> timedelta:
    return timedelta(days=get_settings().session_ttl_days)


def issue_session(db: Session, user: User) -> str:
    """Start a session and return the raw cookie value, which is never stored."""
    _prune(db, user.id)
    token = secrets.token_urlsafe(32)
    db.add(UserSession(
        user_id=user.id,
        token_hash=_digest(token),
        expires_at=_now() + session_lifetime(),
        last_used_at=_now(),
    ))
    db.commit()
    return token


def resolve_session(db: Session, token: str | None) -> User | None:
    """The signed-in account behind a cookie, refreshing the session in place."""
    if not token:
        return None
    record = db.scalar(select(UserSession).where(UserSession.token_hash == _digest(token)))
    if record is None or record.revoked_at is not None:
        return None

    now = _now()
    if as_utc(record.expires_at) <= now:
        return None

    user = db.get(User, record.user_id)
    if user is None:
        return None

    lifetime = session_lifetime()
    remaining = as_utc(record.expires_at) - now
    changed = False
    if remaining < lifetime * RENEWAL_THRESHOLD:
        record.expires_at = now + lifetime
        changed = True
    if now - as_utc(record.last_used_at) > LAST_USED_RESOLUTION:
        record.last_used_at = now
        changed = True
    if changed:
        db.commit()
    return user


def revoke_session(db: Session, token: str | None) -> None:
    if not token:
        return
    record = db.scalar(select(UserSession).where(UserSession.token_hash == _digest(token)))
    if record is not None and record.revoked_at is None:
        record.revoked_at = _now()
        db.commit()


def revoke_all_sessions(db: Session, user_id: UUID) -> int:
    """Sign out every browser, used when the sign-in methods themselves change."""
    records = list(db.scalars(
        select(UserSession).where(
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
        )
    ))
    for record in records:
        record.revoked_at = _now()
    db.flush()
    return len(records)


def _prune(db: Session, user_id: UUID) -> None:
    """Drop this account's dead sessions whenever it opens a new one."""
    db.execute(
        delete(UserSession).where(
            UserSession.user_id == user_id,
            or_(UserSession.expires_at <= _now(), UserSession.revoked_at.is_not(None)),
        )
    )
