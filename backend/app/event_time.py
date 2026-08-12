from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import DEFAULT_TIMEZONE


UTC = timezone.utc


def zone(name: str | None) -> ZoneInfo:
    """Resolve an IANA zone, falling back to the product default then UTC."""
    for candidate in (name, DEFAULT_TIMEZONE, "UTC"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def as_utc(value: datetime) -> datetime:
    """Normalize an instant for storage, interpreting naive values as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def now_utc() -> datetime:
    return datetime.now(UTC)


def local_now(timezone_name: str | None, *, current: datetime | None = None) -> datetime:
    return as_utc(current or now_utc()).astimezone(zone(timezone_name))


def local_date(value: datetime, timezone_name: str | None) -> date:
    return as_utc(value).astimezone(zone(timezone_name)).date()


def local_time(value: datetime, timezone_name: str | None) -> time:
    return as_utc(value).astimezone(zone(timezone_name)).time().replace(microsecond=0)


def local_date_string(value: datetime, timezone_name: str | None) -> str:
    return local_date(value, timezone_name).isoformat()


def local_time_string(value: datetime, timezone_name: str | None) -> str:
    return local_time(value, timezone_name).isoformat()


def from_local_parts(
    day: date,
    clock: time | str | None,
    timezone_name: str | None,
) -> datetime:
    """Interpret local calendar input in its declared zone and store it in UTC."""
    if isinstance(clock, str):
        clock = time.fromisoformat(clock)
    local_value = datetime.combine(day, clock or time.min, tzinfo=zone(timezone_name))
    return local_value.astimezone(UTC)


def resolve_event_time(
    *,
    transaction_at: datetime | None = None,
    day: date | None = None,
    clock: time | str | None = None,
    timezone_name: str | None = None,
    current: datetime | None = None,
    use_current_time: bool = False,
) -> datetime:
    """Resolve timestamp/date input to one canonical UTC instant.

    A supplied timestamp wins. A supplied calendar date is interpreted in the
    user's timezone. When the user supplied no date, callers set
    ``use_current_time`` and the exact current instant becomes the event time.
    """
    if transaction_at is not None:
        if transaction_at.tzinfo is None:
            transaction_at = transaction_at.replace(tzinfo=zone(timezone_name))
        return transaction_at.astimezone(UTC)
    current_utc = as_utc(current or now_utc())
    if day is None or use_current_time:
        return current_utc
    return from_local_parts(day, clock, timezone_name)


def utc_range_for_local_dates(
    start: date,
    end: date,
    timezone_name: str | None,
) -> tuple[datetime, datetime]:
    """Convert an inclusive local date range to a half-open UTC range."""
    start_at = from_local_parts(start, time.min, timezone_name)
    end_at = from_local_parts(end + timedelta(days=1), time.min, timezone_name)
    return start_at, end_at
