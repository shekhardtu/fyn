"""Make UTC timestamps canonical for financial events.

Existing split date/time/timezone values are interpreted in their recorded
zone and backfilled to PostgreSQL ``TIMESTAMPTZ`` columns. When an old record
only carried an inferred current date, its creation/observation instant is the
best available event-time evidence. Explicit date-only records become local
midnight, preserving their calendar meaning deterministically.

Revision ID: 0016_utc_timestamps
Revises: 0015_fyn_ai_rename
"""

from alembic import op
import sqlalchemy as sa

from app.migration_guards import (
    add_column_if_absent,
    create_index_if_absent,
    drop_index_if_present,
    has_column,
)


revision = "0016_utc_timestamps"
down_revision = "0015_fyn_ai_rename"
branch_labels = None
depends_on = None


def _drop_legacy_indexes() -> None:
    for table, name in (
        ("transactions", "ix_transactions_transaction_date"),
        ("transactions", "ix_transactions_posted_date"),
        ("transactions", "ix_transaction_user_date_type"),
        ("transactions", "ix_transactions_user_date_category"),
        ("transactions", "ix_reconciliation_window"),
        ("financial_observations", "ix_financial_observations_transaction_date"),
        ("financial_observations", "ix_observation_candidates"),
    ):
        drop_index_if_present(name, table)


def _create_utc_indexes() -> None:
    for name, table, columns in (
        ("ix_transaction_drafts_transaction_at", "transaction_drafts", ["transaction_at"]),
        ("ix_transactions_transaction_at", "transactions", ["transaction_at"]),
        ("ix_transactions_posted_at", "transactions", ["posted_at"]),
        ("ix_financial_observations_transaction_at", "financial_observations", ["transaction_at"]),
        ("ix_reconciliation_window", "transactions", ["user_id", "account_id", "amount_minor", "currency", "transaction_type", "transaction_at"]),
        ("ix_transaction_user_at_type", "transactions", ["user_id", "transaction_at", "transaction_type"]),
        ("ix_transactions_user_at_category", "transactions", ["user_id", "transaction_at", "category_id"]),
        ("ix_observation_candidates", "financial_observations", ["user_id", "amount_minor", "currency", "transaction_type", "transaction_at"]),
    ):
        create_index_if_absent(name, table, columns)


def upgrade() -> None:
    for table in ("transaction_drafts", "transactions", "financial_observations"):
        add_column_if_absent(table, sa.Column("transaction_at", sa.DateTime(timezone=True), nullable=True))
    for table in ("transactions", "financial_observations"):
        add_column_if_absent(table, sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True))
    add_column_if_absent("goal_contributions", sa.Column("contribution_at", sa.DateTime(timezone=True), nullable=True))

    if has_column("transaction_drafts", "transaction_date"):
        op.execute("""
            UPDATE transaction_drafts AS draft
            SET transaction_at = CASE
                WHEN draft.field_provenance::jsonb #>> '{transaction_date,origin}' = 'inferred'
                    THEN draft.created_at
                ELSE (
                    draft.transaction_date::text || ' ' || COALESCE(draft.transaction_time, '00:00:00')
                )::timestamp AT TIME ZONE COALESCE(NULLIF(draft.timezone, ''), owner.timezone, 'UTC')
            END
            FROM users AS owner
            WHERE owner.id = draft.user_id AND draft.transaction_at IS NULL
        """)
        op.execute("""
            DO $$ BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM transaction_drafts AS draft
                    JOIN users AS owner ON owner.id = draft.user_id
                    WHERE draft.transaction_date IS NOT NULL
                      AND (draft.transaction_at AT TIME ZONE COALESCE(NULLIF(draft.timezone, ''), owner.timezone, 'UTC'))::date
                          <> draft.transaction_date
                ) THEN
                    RAISE EXCEPTION 'transaction draft UTC backfill changed a calendar date';
                END IF;
            END $$
        """)

    if has_column("transactions", "transaction_date"):
        op.execute("""
            WITH backfill AS (
                SELECT transaction.id,
                       owner.timezone AS owner_timezone,
                       (
                           SELECT candidate.observed_at
                           FROM transaction_sources AS candidate
                           WHERE candidate.transaction_id = transaction.id
                           ORDER BY candidate.observed_at ASC
                           LIMIT 1
                       ) AS source_observed_at
                FROM transactions AS transaction
                JOIN users AS owner ON owner.id = transaction.user_id
                WHERE transaction.transaction_at IS NULL
            )
            UPDATE transactions AS transaction
            SET transaction_at = CASE
                    WHEN transaction.transaction_time IS NOT NULL THEN (
                        transaction.transaction_date::text || ' ' || transaction.transaction_time
                    )::timestamp AT TIME ZONE COALESCE(NULLIF(transaction.timezone, ''), backfill.owner_timezone, 'UTC')
                    WHEN backfill.source_observed_at IS NOT NULL
                         AND (backfill.source_observed_at AT TIME ZONE backfill.owner_timezone)::date = transaction.transaction_date
                        THEN backfill.source_observed_at
                    ELSE transaction.transaction_date::timestamp AT TIME ZONE backfill.owner_timezone
                END,
                posted_at = CASE
                    WHEN transaction.posted_date IS NULL THEN NULL
                    ELSE transaction.posted_date::timestamp AT TIME ZONE backfill.owner_timezone
                END
            FROM backfill
            WHERE backfill.id = transaction.id
        """)
        op.execute("""
            DO $$ BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM transactions AS transaction
                    JOIN users AS owner ON owner.id = transaction.user_id
                    WHERE (transaction.transaction_at AT TIME ZONE COALESCE(NULLIF(transaction.timezone, ''), owner.timezone, 'UTC'))::date
                          <> transaction.transaction_date
                ) THEN
                    RAISE EXCEPTION 'transaction UTC backfill changed a calendar date';
                END IF;
            END $$
        """)

    if has_column("financial_observations", "transaction_date"):
        op.execute("""
            UPDATE financial_observations AS observation
            SET transaction_at = CASE
                    WHEN observation.observed_at IS NOT NULL
                         AND (observation.observed_at AT TIME ZONE owner.timezone)::date = observation.transaction_date
                        THEN observation.observed_at
                    ELSE observation.transaction_date::timestamp AT TIME ZONE owner.timezone
                END,
                posted_at = CASE
                    WHEN observation.posted_date IS NULL THEN NULL
                    ELSE observation.posted_date::timestamp AT TIME ZONE owner.timezone
                END
            FROM users AS owner
            WHERE owner.id = observation.user_id AND observation.transaction_at IS NULL
        """)
        op.execute("""
            DO $$ BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM financial_observations AS observation
                    JOIN users AS owner ON owner.id = observation.user_id
                    WHERE (observation.transaction_at AT TIME ZONE owner.timezone)::date
                          <> observation.transaction_date
                ) THEN
                    RAISE EXCEPTION 'financial observation UTC backfill changed a calendar date';
                END IF;
            END $$
        """)

    if has_column("goal_contributions", "contribution_date"):
        op.execute("""
            WITH backfill AS (
                SELECT contribution.id,
                       CASE
                           WHEN transaction.transaction_at IS NOT NULL
                                AND (transaction.transaction_at AT TIME ZONE owner.timezone)::date = contribution.contribution_date
                               THEN transaction.transaction_at
                           WHEN (contribution.created_at AT TIME ZONE owner.timezone)::date = contribution.contribution_date
                               THEN contribution.created_at
                           ELSE contribution.contribution_date::timestamp AT TIME ZONE owner.timezone
                       END AS contribution_at
                FROM goal_contributions AS contribution
                JOIN users AS owner ON owner.id = contribution.user_id
                LEFT JOIN transactions AS transaction ON transaction.id = contribution.transaction_id
                WHERE contribution.contribution_at IS NULL
            )
            UPDATE goal_contributions AS contribution
            SET contribution_at = backfill.contribution_at
            FROM backfill
            WHERE backfill.id = contribution.id
        """)
        op.execute("""
            DO $$ BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM goal_contributions AS contribution
                    JOIN users AS owner ON owner.id = contribution.user_id
                    WHERE (contribution.contribution_at AT TIME ZONE owner.timezone)::date
                          <> contribution.contribution_date
                ) THEN
                    RAISE EXCEPTION 'goal contribution UTC backfill changed a calendar date';
                END IF;
            END $$
        """)

    # Fresh databases already have non-null defaults from the current models.
    # Upgraded databases have now been completely backfilled.
    for table in ("transaction_drafts", "transactions", "financial_observations"):
        op.execute(f"UPDATE {table} SET transaction_at = created_at WHERE transaction_at IS NULL")
        op.alter_column(table, "transaction_at", existing_type=sa.DateTime(timezone=True), nullable=False)
    op.execute("UPDATE goal_contributions SET contribution_at = created_at WHERE contribution_at IS NULL")
    op.alter_column("goal_contributions", "contribution_at", existing_type=sa.DateTime(timezone=True), nullable=False)

    _drop_legacy_indexes()
    if has_column("goal_contributions", "contribution_date"):
        drop_index_if_present("ix_goal_contributions_contribution_date", "goal_contributions")
        drop_index_if_present("ix_goal_contribution_history", "goal_contributions")
    for table, columns in (
        ("transaction_drafts", ("transaction_date", "transaction_time", "timezone")),
        ("transactions", ("transaction_date", "transaction_time", "posted_date", "timezone")),
        ("financial_observations", ("transaction_date", "posted_date")),
        ("goal_contributions", ("contribution_date",)),
    ):
        for column in columns:
            if has_column(table, column):
                op.drop_column(table, column)
    _create_utc_indexes()
    create_index_if_absent("ix_goal_contributions_contribution_at", "goal_contributions", ["contribution_at"])
    create_index_if_absent("ix_goal_contribution_history", "goal_contributions", ["user_id", "goal_id", "contribution_at"])


def downgrade() -> None:
    for table in ("transaction_drafts", "transactions"):
        add_column_if_absent(table, sa.Column("transaction_date", sa.Date(), nullable=True))
        add_column_if_absent(table, sa.Column("transaction_time", sa.String(length=8), nullable=True))
        add_column_if_absent(table, sa.Column("timezone", sa.String(length=80), nullable=True))
    for table in ("transactions", "financial_observations"):
        add_column_if_absent(table, sa.Column("posted_date", sa.Date(), nullable=True))
    add_column_if_absent("financial_observations", sa.Column("transaction_date", sa.Date(), nullable=True))
    add_column_if_absent("goal_contributions", sa.Column("contribution_date", sa.Date(), nullable=True))

    op.execute("""
        UPDATE transaction_drafts AS draft
        SET transaction_date = (draft.transaction_at AT TIME ZONE owner.timezone)::date,
            transaction_time = to_char(draft.transaction_at AT TIME ZONE owner.timezone, 'HH24:MI:SS'),
            timezone = owner.timezone
        FROM users AS owner WHERE owner.id = draft.user_id
    """)
    op.execute("""
        UPDATE transactions AS transaction
        SET transaction_date = (transaction.transaction_at AT TIME ZONE owner.timezone)::date,
            transaction_time = to_char(transaction.transaction_at AT TIME ZONE owner.timezone, 'HH24:MI:SS'),
            posted_date = CASE WHEN transaction.posted_at IS NULL THEN NULL ELSE (transaction.posted_at AT TIME ZONE owner.timezone)::date END,
            timezone = owner.timezone
        FROM users AS owner WHERE owner.id = transaction.user_id
    """)
    op.execute("""
        UPDATE financial_observations AS observation
        SET transaction_date = (observation.transaction_at AT TIME ZONE owner.timezone)::date,
            posted_date = CASE WHEN observation.posted_at IS NULL THEN NULL ELSE (observation.posted_at AT TIME ZONE owner.timezone)::date END
        FROM users AS owner WHERE owner.id = observation.user_id
    """)
    op.execute("""
        UPDATE goal_contributions AS contribution
        SET contribution_date = (contribution.contribution_at AT TIME ZONE owner.timezone)::date
        FROM users AS owner WHERE owner.id = contribution.user_id
    """)
    for table in ("transaction_drafts", "transactions", "financial_observations"):
        op.alter_column(table, "transaction_date", existing_type=sa.Date(), nullable=False)
    op.alter_column("goal_contributions", "contribution_date", existing_type=sa.Date(), nullable=False)

    for table, name in (
        ("transactions", "ix_reconciliation_window"),
        ("transactions", "ix_transaction_user_at_type"),
        ("transactions", "ix_transactions_user_at_category"),
        ("financial_observations", "ix_observation_candidates"),
        ("goal_contributions", "ix_goal_contributions_contribution_at"),
        ("goal_contributions", "ix_goal_contribution_history"),
    ):
        drop_index_if_present(name, table)
    for table, column in (
        ("transaction_drafts", "transaction_at"),
        ("transactions", "transaction_at"),
        ("transactions", "posted_at"),
        ("financial_observations", "transaction_at"),
        ("financial_observations", "posted_at"),
        ("goal_contributions", "contribution_at"),
    ):
        if has_column(table, column):
            op.drop_column(table, column)

    for name, table, columns in (
        ("ix_reconciliation_window", "transactions", ["user_id", "account_id", "amount_minor", "currency", "transaction_type", "transaction_date"]),
        ("ix_transaction_user_date_type", "transactions", ["user_id", "transaction_date", "transaction_type"]),
        ("ix_transactions_user_date_category", "transactions", ["user_id", "transaction_date", "category_id"]),
        ("ix_observation_candidates", "financial_observations", ["user_id", "amount_minor", "currency", "transaction_type", "transaction_date"]),
        ("ix_goal_contributions_contribution_date", "goal_contributions", ["contribution_date"]),
        ("ix_goal_contribution_history", "goal_contributions", ["user_id", "goal_id", "contribution_date"]),
    ):
        create_index_if_absent(name, table, columns)
