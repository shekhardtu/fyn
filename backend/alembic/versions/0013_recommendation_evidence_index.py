"""Index the window the category recommender reads on every draft.

The recommender scans one user's recent categorized transactions to build its
decayed evidence. Without a composite index that degrades to a per-user scan
plus a sort as history grows.

Revision ID: 0013_recommendation_evidence
Revises: 0012_merchant_ownership
"""

from alembic import op


revision = "0013_recommendation_evidence"
down_revision = "0012_merchant_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_transactions_user_date_category",
        "transactions",
        ["user_id", "transaction_date", "category_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_user_date_category", table_name="transactions")
