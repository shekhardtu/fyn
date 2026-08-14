"""Add the durable AG-UI run, event, and interrupt runtime.

Revision ID: 0020_ag_ui_runtime
Revises: 0019_category_hints
"""

from alembic import op
import sqlalchemy as sa

from app.migration_guards import create_table_if_absent, drop_table_if_present


revision = "0020_ag_ui_runtime"
down_revision = "0019_category_hints"
branch_labels = None
depends_on = None


def _create_indexes(table: str, indexes: tuple[tuple[str, list[str]], ...]) -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {item["name"] for item in inspector.get_indexes(table)}
    for name, columns in indexes:
        if name not in existing:
            op.create_index(name, table, columns)


def upgrade() -> None:
    create_table_if_absent(
        "agent_runs",
        sa.Column("parent_run_id", sa.Uuid(), nullable=True),
        sa.Column("blocked_by_run_id", sa.Uuid(), nullable=True),
        sa.Column("client_message_id", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["parent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["blocked_by_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "client_message_id", name="uq_agent_run_user_client_message"),
    )
    _create_indexes(
        "agent_runs",
        (
            ("ix_agent_runs_parent_run_id", ["parent_run_id"]),
            ("ix_agent_runs_blocked_by_run_id", ["blocked_by_run_id"]),
            ("ix_agent_runs_client_message_id", ["client_message_id"]),
            ("ix_agent_runs_status", ["status"]),
            ("ix_agent_runs_user_id", ["user_id"]),
            ("ix_agent_runs_conversation_id", ["conversation_id"]),
            ("ix_agent_run_thread_status_created", ["conversation_id", "status", "created_at"]),
        ),
    )

    create_table_if_absent(
        "agent_events",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_agent_event_run_sequence"),
    )
    _create_indexes(
        "agent_events",
        (
            ("ix_agent_events_run_id", ["run_id"]),
            ("ix_agent_events_event_type", ["event_type"]),
        ),
    )

    create_table_if_absent(
        "agent_interrupts",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("resolved_by_run_id", sa.Uuid(), nullable=True),
        sa.Column("tool_call_id", sa.String(length=120), nullable=False),
        sa.Column("widget_id", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.String(length=120), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("response_schema", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_by_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tool_call_id"),
    )
    _create_indexes(
        "agent_interrupts",
        (
            ("ix_agent_interrupts_run_id", ["run_id"]),
            ("ix_agent_interrupts_resolved_by_run_id", ["resolved_by_run_id"]),
            ("ix_agent_interrupts_widget_id", ["widget_id"]),
            ("ix_agent_interrupts_status", ["status"]),
            ("ix_agent_interrupts_expires_at", ["expires_at"]),
            ("ix_agent_interrupt_run_status", ["run_id", "status"]),
        ),
    )


def downgrade() -> None:
    drop_table_if_present("agent_interrupts")
    drop_table_if_present("agent_events")
    drop_table_if_present("agent_runs")
