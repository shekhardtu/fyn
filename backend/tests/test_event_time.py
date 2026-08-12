from __future__ import annotations

from datetime import timezone

from sqlalchemy import Date, DateTime

from app.database import Base
from app.event_time import now_utc
from app.schemas import ObservationIn


def test_persisted_instants_are_timezone_aware_and_calendar_dates_are_explicit():
    calendar_dates = set()
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, DateTime):
                assert column.type.timezone, f"{table.name}.{column.name} must be TIMESTAMPTZ"
            elif isinstance(column.type, Date):
                calendar_dates.add(f"{table.name}.{column.name}")

    # These represent user-facing calendar concepts, not instants. Any new
    # DATE must be deliberately reviewed and added here.
    assert calendar_dates == {
        "goals.target_date",
        "recurring_transactions.next_expected_date",
    }


def test_current_time_fields_have_database_defaults_for_non_orm_writes():
    event_defaults = {
        "user_identities.verified_at",
        "user_sessions.last_used_at",
        "account_balance_snapshots.observed_at",
        "investment_holdings.valued_at",
        "investment_valuation_snapshots.observed_at",
        "transaction_drafts.transaction_at",
        "transactions.transaction_at",
        "financial_observations.transaction_at",
        "financial_observations.observed_at",
        "transaction_sources.observed_at",
        "goal_contributions.contribution_at",
    }
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            qualified = f"{table.name}.{column.name}"
            if column.name in {"created_at", "updated_at"} or qualified in event_defaults:
                assert column.server_default is not None, f"{qualified} must default to the current instant"


def test_observation_without_transaction_time_defaults_to_current_utc_instant():
    before = now_utc()
    payload = ObservationIn(
        source_type="sms",
        source_message_id="no-date",
        transaction_type="expense",
        amount_minor=1_000,
    )
    after = now_utc()

    assert before <= payload.transaction_at <= after
    assert payload.transaction_at.tzinfo is timezone.utc
