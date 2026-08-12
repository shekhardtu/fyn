"""Give current-time instants database-level UTC defaults.

ORM defaults protect normal application writes. Database defaults close the
same hole for bulk imports, maintenance SQL, and any future write path that
does not instantiate an ORM model first.

Revision ID: 0018_utc_defaults
Revises: 0017_widget_timestamps
"""

from alembic import op
import sqlalchemy as sa

from app.migration_guards import has_column


revision = "0018_utc_defaults"
down_revision = "0017_widget_timestamps"
branch_labels = None
depends_on = None


EVENT_DEFAULTS = (
    ("user_identities", "verified_at"),
    ("user_sessions", "last_used_at"),
    ("account_balance_snapshots", "observed_at"),
    ("investment_holdings", "valued_at"),
    ("investment_valuation_snapshots", "observed_at"),
    ("transaction_drafts", "transaction_at"),
    ("transactions", "transaction_at"),
    ("financial_observations", "transaction_at"),
    ("financial_observations", "observed_at"),
    ("transaction_sources", "observed_at"),
    ("goal_contributions", "contribution_at"),
)


def _defaulted_columns() -> list[tuple[str, str]]:
    inspector = sa.inspect(op.get_bind())
    columns = list(EVENT_DEFAULTS)
    for table in inspector.get_table_names():
        names = {column["name"] for column in inspector.get_columns(table)}
        columns.extend((table, name) for name in ("created_at", "updated_at") if name in names)
    return list(dict.fromkeys(columns))


def upgrade() -> None:
    for table, column in _defaulted_columns():
        if has_column(table, column):
            op.alter_column(
                table,
                column,
                existing_type=sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )


def downgrade() -> None:
    for table, column in _defaulted_columns():
        if has_column(table, column):
            op.alter_column(
                table,
                column,
                existing_type=sa.DateTime(timezone=True),
                server_default=None,
            )
