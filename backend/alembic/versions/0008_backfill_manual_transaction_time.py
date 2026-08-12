"""Backfill trustworthy manual transaction observation times in UTC.

Only manual records observed on their stated local transaction date qualify.
Imported records and back-dated manual entries remain null because their
ingestion/entry timestamp is not evidence of the financial event time.

Revision ID: 0008_manual_time_utc
Revises: 0007_conversation_analysis_state
"""

from alembic import op
from app.migration_guards import has_column


revision = "0008_manual_time_utc"
down_revision = "0007_conversation_analysis_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh database is built from the current ORM schema by 0001. Once UTC
    # event timestamps became canonical, that schema no longer contained the
    # legacy split date/time columns this historical data migration targets.
    if not all(has_column("transactions", column) for column in ("transaction_date", "transaction_time", "timezone")):
        return
    op.execute("""
        UPDATE transaction_sources AS source
        SET field_values = (
            COALESCE(source.field_values::jsonb, '{}'::jsonb)
            || jsonb_build_object(
                'transaction_time_backfill',
                jsonb_build_object(
                    'origin', 'manual_observed_at',
                    'confidence', 0.60,
                    'observed_at', source.observed_at,
                    'stored_timezone', 'UTC',
                    'previous_timezone', transaction.timezone,
                    'previous_transaction_date', transaction.transaction_date
                )
            )
        )::json
        FROM transactions AS transaction
        JOIN users AS owner ON owner.id = transaction.user_id
        WHERE source.transaction_id = transaction.id
          AND source.source_type = 'manual'
          AND transaction.deleted_at IS NULL
          AND transaction.transaction_time IS NULL
          AND (source.observed_at AT TIME ZONE COALESCE(transaction.timezone, owner.timezone, 'UTC'))::date
              = transaction.transaction_date
    """)
    op.execute("""
        UPDATE transactions AS transaction
        SET transaction_date = (source.observed_at AT TIME ZONE 'UTC')::date,
            transaction_time = to_char(source.observed_at AT TIME ZONE 'UTC', 'HH24:MI:SS'),
            timezone = 'UTC'
        FROM transaction_sources AS source
        WHERE source.transaction_id = transaction.id
          AND source.source_type = 'manual'
          AND source.field_values::jsonb #>> '{transaction_time_backfill,origin}' = 'manual_observed_at'
          AND transaction.transaction_time IS NULL
    """)


def downgrade() -> None:
    if not all(has_column("transactions", column) for column in ("transaction_date", "transaction_time", "timezone")):
        return
    op.execute("""
        UPDATE transactions AS transaction
        SET transaction_time = NULL,
            transaction_date = (source.field_values::jsonb #>> '{transaction_time_backfill,previous_transaction_date}')::date,
            timezone = NULLIF(source.field_values::jsonb #>> '{transaction_time_backfill,previous_timezone}', '')
        FROM transaction_sources AS source
        WHERE source.transaction_id = transaction.id
          AND source.field_values::jsonb #>> '{transaction_time_backfill,origin}' = 'manual_observed_at'
    """)
    op.execute("""
        UPDATE transaction_sources
        SET field_values = (field_values::jsonb - 'transaction_time_backfill')::json
        WHERE field_values::jsonb #>> '{transaction_time_backfill,origin}' = 'manual_observed_at'
    """)
