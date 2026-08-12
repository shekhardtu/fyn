"""Add historical balances, investments and goal contribution facts.

Revision ID: 0009_financial_history
Revises: 0008_manual_time_utc
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_financial_history"
down_revision = "0008_manual_time_utc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_balance_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("balance_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "observed_at", "source_type", name="uq_account_balance_observation"),
    )
    op.create_index("ix_account_balance_snapshots_user_id", "account_balance_snapshots", ["user_id"])
    op.create_index("ix_account_balance_snapshots_account_id", "account_balance_snapshots", ["account_id"])
    op.create_index("ix_account_balance_snapshots_observed_at", "account_balance_snapshots", ["observed_at"])
    op.create_index("ix_account_balance_history", "account_balance_snapshots", ["user_id", "observed_at", "account_id"])

    op.create_table(
        "investment_holdings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=True),
        sa.Column("asset_type", sa.String(length=40), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("cost_basis_minor", sa.Integer(), nullable=False),
        sa.Column("current_value_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("valued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        ("ix_investment_holdings_user_id", ["user_id"]),
        ("ix_investment_holdings_account_id", ["account_id"]),
        ("ix_investment_holdings_symbol", ["symbol"]),
        ("ix_investment_holdings_asset_type", ["asset_type"]),
        ("ix_investment_holdings_status", ["status"]),
    ):
        op.create_index(name, "investment_holdings", columns)

    op.create_table(
        "investment_valuation_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("holding_id", sa.Uuid(), nullable=False),
        sa.Column("market_value_minor", sa.Integer(), nullable=False),
        sa.Column("cost_basis_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["holding_id"], ["investment_holdings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("holding_id", "observed_at", "source_type", name="uq_investment_valuation_observation"),
    )
    op.create_index("ix_investment_valuation_snapshots_user_id", "investment_valuation_snapshots", ["user_id"])
    op.create_index("ix_investment_valuation_snapshots_holding_id", "investment_valuation_snapshots", ["holding_id"])
    op.create_index("ix_investment_valuation_snapshots_observed_at", "investment_valuation_snapshots", ["observed_at"])
    op.create_index("ix_investment_value_history", "investment_valuation_snapshots", ["user_id", "observed_at", "holding_id"])

    op.create_table(
        "goal_contributions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("goal_id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=True),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("contribution_date", sa.Date(), nullable=False),
        sa.Column("note", sa.String(length=240), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_goal_contributions_user_id", "goal_contributions", ["user_id"])
    op.create_index("ix_goal_contributions_goal_id", "goal_contributions", ["goal_id"])
    op.create_index("ix_goal_contributions_transaction_id", "goal_contributions", ["transaction_id"])
    op.create_index("ix_goal_contributions_contribution_date", "goal_contributions", ["contribution_date"])
    op.create_index("ix_goal_contribution_history", "goal_contributions", ["user_id", "goal_id", "contribution_date"])


def downgrade() -> None:
    op.drop_table("goal_contributions")
    op.drop_table("investment_valuation_snapshots")
    op.drop_table("investment_holdings")
    op.drop_table("account_balance_snapshots")
