from __future__ import annotations

from calendar import month_abbr, month_name, monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any
from uuid import UUID

from dateparser.search import search_dates


_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "fourteen": 14,
    "thirty": 30,
}
_AMBIGUOUS_NUMERIC_RANGE = re.compile(
    r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s*(?:to|through|until|-)\s*"
    r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b",
    re.I,
)
_DATE_CUE = re.compile(
    r"\b(?:today|yesterday|tomorrow|week|month|year|jan(?:uary)?|feb(?:ruary)?|"
    r"mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b|\d{1,2}[/-]\d{1,2}[/-]\d{4}",
    re.I,
)

FINANCE_DATE_POLICY = {
    "inclusive_bounds": True,
    "current_month": "month_to_date",
    "last_three_months": "current_month_to_date_plus_two_preceding_calendar_months",
}

_ABSOLUTE_DATE_TOKENS = frozenset(
    name.casefold() for name in (*month_name, *month_abbr) if name
)


@dataclass(frozen=True)
class FinanceRunContext:
    """Single source for agent and deterministic date/runtime semantics."""

    local_date: date
    timezone: str
    user_id: UUID | str | None = None

    def agno_options(self) -> dict[str, Any]:
        runtime: dict[str, Any] = {
            "local_date": self.local_date.isoformat(),
            "timezone": self.timezone,
            "date_policy": dict(FINANCE_DATE_POLICY),
        }
        if self.user_id is not None:
            runtime["authenticated_user_id"] = str(self.user_id)
        return {
            "dependencies": {"finance_runtime": runtime},
            "add_dependencies_to_context": True,
            "add_datetime_to_context": True,
            "timezone_identifier": self.timezone,
        }

    def resolve(self, text: str) -> "DateResolution":
        return resolve_finance_period(text, self.local_date, self.timezone)


@dataclass(frozen=True)
class DateOption:
    id: str
    label: str
    start_date: date
    end_date: date


@dataclass(frozen=True)
class DateResolution:
    status: str
    start_date: date | None = None
    end_date: date | None = None
    label: str | None = None
    source: str | None = None
    options: tuple[DateOption, ...] = ()


def shift_month(value: date, months: int) -> date:
    """Shift a date by whole months, clamping the day to the target month's length.

    Finance-policy callers always pass a first-of-month date, so the clamp is
    inert for them; it exists for arbitrary anchors such as replay re-derivation.
    """
    total = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def month_bounds(day: date) -> tuple[date, date]:
    return day.replace(day=1), day.replace(day=monthrange(day.year, day.month)[1])


def names_absolute_finance_date(text: str) -> bool:
    """Whether text pins a calendar month or year instead of a relative period."""
    normalized_tokens = set(re.findall(r"[a-z0-9']+", text.casefold()))
    return bool(normalized_tokens & _ABSOLUTE_DATE_TOKENS) or bool(
        re.search(r"\b(?:19|20)\d{2}\b", text)
    )


def reanchor_finance_date(value: date, prior_today: date, today: date) -> date | None:
    """Re-derive a stored relative date from the central finance-date policy.

    Calendar boundaries outrank rolling day offsets. A value that cannot be
    explained by a supported relative anchor is intentionally not replayable.
    """
    if value == prior_today:
        return today
    for months_back in range(12):
        prior_month = shift_month(prior_today.replace(day=1), -months_back)
        current_month = shift_month(today.replace(day=1), -months_back)
        prior_month_end = date(
            prior_month.year,
            prior_month.month,
            monthrange(prior_month.year, prior_month.month)[1],
        )
        current_month_end = date(
            current_month.year,
            current_month.month,
            monthrange(current_month.year, current_month.month)[1],
        )
        if value == prior_month:
            return current_month
        if value == prior_month_end:
            return current_month_end
    days_back = (prior_today - value).days
    if 0 < days_back <= 400:
        return today - timedelta(days=days_back)
    return None


def ambiguous_numeric_date_options(text: str) -> tuple[DateOption, ...]:
    """Return both valid numeric-range interpretations; never guess locale."""
    match = _AMBIGUOUS_NUMERIC_RANGE.search(text)
    if not match:
        return ()
    first_a, first_b, first_year, second_a, second_b, second_year = map(int, match.groups())
    if max(first_a, first_b, second_a, second_b) > 12:
        return ()
    try:
        day_first = (date(first_year, first_b, first_a), date(second_year, second_b, second_a))
        month_first = (date(first_year, first_a, first_b), date(second_year, second_a, second_b))
    except ValueError:
        return ()
    if day_first == month_first:
        return ()
    options = []
    for option_id, label, (start_date, end_date) in (
        ("day_month_year", "Day / month / year", day_first),
        ("month_day_year", "Month / day / year", month_first),
    ):
        if start_date <= end_date:
            options.append(DateOption(option_id, label, start_date, end_date))
    return tuple(options) if len(options) == 2 else ()


def resolve_finance_period(
    text: str,
    today: date,
    timezone_name: str = "UTC",
) -> DateResolution:
    """Resolve one inclusive finance period from a single central policy.

    Product semantics are handled first. Dateparser is the bounded fallback for
    explicit calendar text and receives the same relative base and timezone as
    the agents. Ambiguous numeric dates are returned for customer clarification.
    """
    lowered = text.casefold()
    options = ambiguous_numeric_date_options(text)
    if options:
        return DateResolution(status="ambiguous", source="numeric_range", options=options)

    if "last month" in lowered or "previous month" in lowered:
        previous_end = today.replace(day=1) - timedelta(days=1)
        return DateResolution("resolved", previous_end.replace(day=1), previous_end, "Last month", "policy")
    if "this month" in lowered or re.search(r"\bcurrent\s+(?:spending|expenses?|income|savings|ratio)\b", lowered):
        return DateResolution("resolved", today.replace(day=1), today, "This month", "policy")
    if "last year" in lowered or "previous year" in lowered:
        year = today.year - 1
        return DateResolution("resolved", date(year, 1, 1), date(year, 12, 31), "Last year", "policy")
    if "this year" in lowered:
        return DateResolution("resolved", date(today.year, 1, 1), today, "This year", "policy")
    if "day before yesterday" in lowered:
        target = today - timedelta(days=2)
        return DateResolution("resolved", target, target, "Day before yesterday", "policy")
    if "yesterday" in lowered:
        target = today - timedelta(days=1)
        return DateResolution("resolved", target, target, "Yesterday", "policy")
    if re.search(r"\btoday\b", lowered):
        return DateResolution("resolved", today, today, "Today", "policy")
    if "this week" in lowered:
        return DateResolution(
            "resolved",
            today - timedelta(days=today.weekday()),
            today,
            "This week",
            "policy",
        )
    if re.search(r"\b(?:last|past|previous)\s+week\b", lowered):
        return DateResolution("resolved", today - timedelta(days=6), today, "Last 7 days", "policy")

    day_match = re.search(
        r"\b(?:last|past|previous)\s+"
        r"(\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|fourteen|thirty)\s+days?\b",
        lowered,
    )
    if day_match:
        raw_days = day_match.group(1)
        days = int(raw_days) if raw_days.isdigit() else _NUMBER_WORDS[raw_days]
        days = max(1, min(days, 366))
        return DateResolution(
            "resolved",
            today - timedelta(days=days - 1),
            today,
            f"Last {days} days",
            "policy",
        )

    month_match = re.search(
        r"\b(?:last|past|previous)\s+(\d{1,2}|one|two|three|four|five|six)\s+months?\b",
        lowered,
    )
    if month_match:
        raw_months = month_match.group(1)
        months = int(raw_months) if raw_months.isdigit() else _NUMBER_WORDS[raw_months]
        months = max(1, min(months, 24))
        start = shift_month(today.replace(day=1), -(months - 1))
        return DateResolution("resolved", start, today, f"Last {months} months", "policy")

    if not _DATE_CUE.search(text):
        return DateResolution(status="none")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
        timezone_name = "UTC"
    relative_base = datetime.combine(today, time(hour=12), tzinfo=zone)
    matches = search_dates(
        text,
        languages=["en"],
        settings={
            "RELATIVE_BASE": relative_base,
            "TIMEZONE": timezone_name,
            "TO_TIMEZONE": timezone_name,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "past",
            "DATE_ORDER": "DMY",
            "STRICT_PARSING": True,
        },
    ) or []
    parsed_dates = [parsed.astimezone(zone).date() for _, parsed in matches]
    if not parsed_dates:
        return DateResolution(status="none")
    start_date = min(parsed_dates)
    end_date = max(parsed_dates)
    label = (
        start_date.strftime("%d %b %Y")
        if start_date == end_date
        else f"{start_date.strftime('%d %b %Y')} to {end_date.strftime('%d %b %Y')}"
    )
    return DateResolution("resolved", start_date, end_date, label, "dateparser")
