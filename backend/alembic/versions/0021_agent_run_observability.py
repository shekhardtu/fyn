"""Link durable runs to replies and retain first-response timing.

Revision ID: 0021_agent_run_observability
Revises: 0020_ag_ui_runtime
"""

from alembic import op
import sqlalchemy as sa

from app.migration_guards import (
    add_column_if_absent,
    create_foreign_key_if_absent,
    create_index_if_absent,
    drop_index_if_present,
    has_column,
)


revision = "0021_agent_run_observability"
down_revision = "0020_ag_ui_runtime"
branch_labels = None
depends_on = None


def _has_final_message_foreign_key() -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("agent_runs"):
        return False
    return any(
        item.get("constrained_columns") == ["final_message_id"]
        and item.get("referred_table") == "messages"
        for item in inspector.get_foreign_keys("agent_runs")
    )


def upgrade() -> None:
    add_column_if_absent("agent_runs", sa.Column("final_message_id", sa.Uuid(), nullable=True))
    add_column_if_absent("agent_runs", sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_final_message_foreign_key():
        create_foreign_key_if_absent(
            "fk_agent_runs_final_message_id_messages",
            "agent_runs",
            "messages",
            ["final_message_id"],
            ["id"],
            ondelete="SET NULL",
        )
    create_index_if_absent(
        "ix_agent_runs_final_message_id",
        "agent_runs",
        ["final_message_id"],
    )


def downgrade() -> None:
    drop_index_if_present("ix_agent_runs_final_message_id", "agent_runs")
    if has_column("agent_runs", "first_response_at"):
        op.drop_column("agent_runs", "first_response_at")
    if has_column("agent_runs", "final_message_id"):
        op.drop_column("agent_runs", "final_message_id")
