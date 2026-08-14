"""Record how each final answer reached the reader.

Revision ID: 0022_agent_run_delivery_mode
Revises: 0021_agent_run_observability
"""

from alembic import op
import sqlalchemy as sa

from app.migration_guards import add_column_if_absent, has_column


revision = "0022_agent_run_delivery_mode"
down_revision = "0021_agent_run_observability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_absent(
        "agent_runs",
        sa.Column(
            "delivery_mode",
            sa.String(length=32),
            nullable=False,
            server_default="verified_final",
        ),
    )


def downgrade() -> None:
    if has_column("agent_runs", "delivery_mode"):
        op.drop_column("agent_runs", "delivery_mode")
