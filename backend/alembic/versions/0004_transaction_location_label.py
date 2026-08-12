"""Add privacy-preserving transaction location labels.

Revision ID: 0004_transaction_location_label
Revises: 0003_analysis_tool_harness
"""
from alembic import op
import sqlalchemy as sa


revision = "0004_transaction_location_label"
down_revision = "0003_analysis_tool_harness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    drafts = {column["name"] for column in inspector.get_columns("transaction_drafts")}
    transactions = {column["name"] for column in inspector.get_columns("transactions")}
    if "location_label" not in drafts:
        op.add_column("transaction_drafts", sa.Column("location_label", sa.String(length=160), nullable=True))
    if "location_label" not in transactions:
        op.add_column("transactions", sa.Column("location_label", sa.String(length=160), nullable=True))


def downgrade() -> None:
    op.drop_column("transactions", "location_label")
    op.drop_column("transaction_drafts", "location_label")
