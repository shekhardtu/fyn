from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timezone
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _sqlite_local_datetime(value, timezone_name: str | None) -> str | None:
    """SQLite test/dev equivalent of PostgreSQL's ``timezone(zone, value)``."""
    if value is None:
        return None
    instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    try:
        target = ZoneInfo(timezone_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        target = ZoneInfo("UTC")
    return instant.astimezone(target).replace(tzinfo=None).isoformat(sep=" ")


@event.listens_for(Engine, "connect")
def _configure_temporal_session(dbapi_connection, _connection_record) -> None:
    if isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.create_function("to_local_datetime", 2, _sqlite_local_datetime)


def _engine_args(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    if url.startswith("postgresql"):
        # TIMESTAMPTZ stores instants independently of a zone. Pinning every
        # application connection to UTC makes reads and raw SQL agree too.
        return {"connect_args": {"options": "-c timezone=UTC"}}
    return {}


settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True, **_engine_args(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
