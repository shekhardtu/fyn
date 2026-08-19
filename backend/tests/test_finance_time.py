from datetime import date
from uuid import uuid4

from app.services.finance_time import (
    FinanceRunContext,
    month_bounds,
    names_absolute_finance_date,
    reanchor_finance_date,
    resolve_finance_period,
    shift_month,
)


def test_shift_month_preserves_first_of_month_anchors_and_clamps_month_ends():
    # Every finance-policy caller passes a first-of-month anchor; that behavior
    # must match the fixed lane's old day-1 helper exactly.
    assert shift_month(date(2026, 8, 1), -1) == date(2026, 7, 1)
    assert shift_month(date(2026, 1, 1), -2) == date(2025, 11, 1)
    assert shift_month(date(2026, 8, 1), 5) == date(2027, 1, 1)
    # Arbitrary anchors clamp to the target month's length (replay re-derivation).
    assert shift_month(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert shift_month(date(2024, 1, 31), 1) == date(2024, 2, 29)


def test_month_bounds_returns_inclusive_calendar_month():
    assert month_bounds(date(2026, 8, 17)) == (date(2026, 8, 1), date(2026, 8, 31))
    assert month_bounds(date(2024, 2, 10)) == (date(2024, 2, 1), date(2024, 2, 29))


def test_product_periods_share_one_inclusive_policy():
    today = date(2026, 8, 17)

    current = resolve_finance_period("Show current spending", today, "Asia/Kolkata")
    rolling = resolve_finance_period("Compare the last three months", today, "Asia/Kolkata")

    assert (current.start_date, current.end_date) == (date(2026, 8, 1), today)
    assert (rolling.start_date, rolling.end_date) == (date(2026, 6, 1), today)
    assert current.source == rolling.source == "policy"


def test_relative_dates_use_the_supplied_local_date():
    india = FinanceRunContext(date(2026, 8, 17), "Asia/Kolkata")
    new_york = FinanceRunContext(date(2026, 8, 16), "America/New_York")

    assert india.resolve("expenses yesterday").start_date == date(2026, 8, 16)
    assert new_york.resolve("expenses yesterday").start_date == date(2026, 8, 15)


def test_ambiguous_numeric_range_is_never_guessed():
    result = resolve_finance_period(
        "Show expenses from 04/05/2026 to 06/07/2026",
        date(2026, 8, 17),
        "Asia/Kolkata",
    )

    assert result.status == "ambiguous"
    assert [(item.start_date, item.end_date) for item in result.options] == [
        (date(2026, 5, 4), date(2026, 7, 6)),
        (date(2026, 4, 5), date(2026, 6, 7)),
    ]


def test_explicit_calendar_text_uses_bounded_dateparser_fallback():
    result = resolve_finance_period(
        "Show income from 1 August 2026 to 16 August 2026",
        date(2026, 8, 17),
        "Asia/Kolkata",
    )

    assert result.status == "resolved"
    assert (result.start_date, result.end_date) == (date(2026, 8, 1), date(2026, 8, 16))
    assert result.source == "dateparser"


def test_agno_context_is_hidden_typed_runtime_data_from_the_same_ssot():
    user_id = uuid4()
    options = FinanceRunContext(date(2026, 8, 17), "Asia/Kolkata", user_id).agno_options()

    assert options["add_datetime_to_context"] is True
    assert options["timezone_identifier"] == "Asia/Kolkata"
    assert options["dependencies"]["finance_runtime"] == {
        "local_date": "2026-08-17",
        "timezone": "Asia/Kolkata",
        "date_policy": {
            "inclusive_bounds": True,
            "current_month": "month_to_date",
            "last_three_months": "current_month_to_date_plus_two_preceding_calendar_months",
        },
        "authenticated_user_id": str(user_id),
    }


def test_relative_replay_dates_use_the_same_finance_time_policy():
    prior_today = date(2026, 8, 17)
    today = date(2026, 9, 4)

    assert reanchor_finance_date(date(2026, 8, 1), prior_today, today) == date(2026, 9, 1)
    assert reanchor_finance_date(prior_today, prior_today, today) == today
    assert reanchor_finance_date(date(2026, 7, 31), prior_today, today) == date(2026, 8, 31)
    assert reanchor_finance_date(date(2020, 1, 1), prior_today, today) is None


def test_absolute_calendar_language_disables_relative_replay():
    assert names_absolute_finance_date("Compare expenses in August 2026") is True
    assert names_absolute_finance_date("Compare expenses this month") is False
