"""Add semantic finance intelligence records.

Revision ID: 0002_finance_intelligence
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_finance_intelligence"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    draft_columns = {column["name"] for column in inspector.get_columns("transaction_drafts")}
    transaction_columns = {column["name"] for column in inspector.get_columns("transactions")}
    if "tags" not in draft_columns:
        op.add_column("transaction_drafts", sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    if "spend_nature" not in draft_columns:
        op.add_column("transaction_drafts", sa.Column("spend_nature", sa.String(length=30), nullable=False, server_default="unknown"))
    if "field_provenance" not in draft_columns:
        op.add_column("transaction_drafts", sa.Column("field_provenance", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    if "spend_nature" not in transaction_columns:
        op.add_column("transactions", sa.Column("spend_nature", sa.String(length=30), nullable=False, server_default="unknown"))
        op.create_index("ix_transactions_spend_nature", "transactions", ["spend_nature"])

    if not inspector.has_table("tags"):
        op.create_table(
        "tags",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("normalized_name", sa.String(length=80), nullable=False),
        sa.Column("color", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "normalized_name", name="uq_user_tag_name"),
        )
        op.create_index("ix_tags_user_id", "tags", ["user_id"])
    if not inspector.has_table("transaction_tags"):
        op.create_table(
        "transaction_tags",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("tag_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transaction_id", "tag_id", name="uq_transaction_tag"),
        )
        op.create_index("ix_transaction_tags_tag_id", "transaction_tags", ["tag_id"])
        op.create_index("ix_transaction_tags_transaction_id", "transaction_tags", ["transaction_id"])
    if not inspector.has_table("transaction_field_values"):
        op.create_table(
        "transaction_field_values",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("field_name", sa.String(length=60), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("origin", sa.String(length=40), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("source_observation_id", sa.Uuid(), nullable=True),
        sa.Column("user_confirmed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_observation_id"], ["financial_observations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_transaction_field_values_field_name", "transaction_field_values", ["field_name"])
        op.create_index("ix_transaction_field_values_transaction_id", "transaction_field_values", ["transaction_id"])
    if not inspector.has_table("loans"):
        op.create_table(
        "loans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=140), nullable=False),
        sa.Column("loan_type", sa.String(length=40), nullable=False),
        sa.Column("lender", sa.String(length=140), nullable=True),
        sa.Column("outstanding_principal_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("annual_rate_percent", sa.Numeric(7, 4), nullable=False),
        sa.Column("rate_type", sa.String(length=20), nullable=False),
        sa.Column("remaining_tenure_months", sa.Integer(), nullable=False),
        sa.Column("current_emi_minor", sa.Integer(), nullable=True),
        sa.Column("prepayment_fee_percent", sa.Numeric(7, 4), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_loans_account_id", "loans", ["account_id"])
        op.create_index("ix_loans_status", "loans", ["status"])
        op.create_index("ix_loans_user_id", "loans", ["user_id"])
    if not inspector.has_table("loan_scenarios"):
        op.create_table(
        "loan_scenarios",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("objective", sa.String(length=40), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["loan_id"], ["loans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_loan_scenarios_loan_id", "loan_scenarios", ["loan_id"])
        op.create_index("ix_loan_scenarios_user_id", "loan_scenarios", ["user_id"])


def downgrade() -> None:
    op.drop_table("loan_scenarios")
    op.drop_table("loans")
    op.drop_table("transaction_field_values")
    op.drop_table("transaction_tags")
    op.drop_table("tags")
    op.drop_index("ix_transactions_spend_nature", table_name="transactions")
    op.drop_column("transactions", "spend_nature")
    op.drop_column("transaction_drafts", "field_provenance")
    op.drop_column("transaction_drafts", "spend_nature")
    op.drop_column("transaction_drafts", "tags")
