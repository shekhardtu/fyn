"""Add explicit user categorization hints.

Revision ID: 0019_category_hints
Revises: 0018_utc_defaults
"""

from alembic import op
import sqlalchemy as sa

from app.migration_guards import create_table_if_absent, drop_table_if_present


revision = "0019_category_hints"
down_revision = "0018_utc_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_table_if_absent(
        "transaction_category_hints",
        sa.Column("merchant_pattern", sa.String(length=160), nullable=False),
        sa.Column("normalized_pattern", sa.String(length=160), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("subcategory_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subcategory_id"], ["subcategories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "normalized_pattern", name="uq_user_transaction_category_hint"),
    )
    inspector = sa.inspect(op.get_bind())
    existing = {item["name"] for item in inspector.get_indexes("transaction_category_hints")}
    for name, columns in (
        ("ix_transaction_category_hints_user_id", ["user_id"]),
        ("ix_transaction_category_hints_category_id", ["category_id"]),
        ("ix_transaction_category_hints_subcategory_id", ["subcategory_id"]),
    ):
        if name not in existing:
            op.create_index(name, "transaction_category_hints", columns)


def downgrade() -> None:
    drop_table_if_present("transaction_category_hints")
