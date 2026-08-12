"""Add generated analysis tool registry and execution ledger.

Revision ID: 0003_analysis_tool_harness
Revises: 0002_finance_intelligence
"""
from alembic import op
import sqlalchemy as sa


revision = "0003_analysis_tool_harness"
down_revision = "0002_finance_intelligence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("analysis_tools"):
        op.create_table(
        "analysis_tools",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("intent_signature", sa.String(length=160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("specification", sa.JSON(), nullable=False),
        sa.Column("specification_hash", sa.String(length=64), nullable=False),
        sa.Column("validation_report", sa.JSON(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "specification_hash", name="uq_analysis_tool_specification"),
        )
        op.create_index("ix_analysis_tools_user_id", "analysis_tools", ["user_id"])
        op.create_index("ix_analysis_tools_intent_signature", "analysis_tools", ["intent_signature"])
        op.create_index("ix_analysis_tools_status", "analysis_tools", ["status"])
        op.create_index("ix_analysis_tools_specification_hash", "analysis_tools", ["specification_hash"])
        op.create_index("ix_analysis_tool_discovery", "analysis_tools", ["user_id", "status", "intent_signature"])

    if not inspector.has_table("analysis_tool_runs"):
        op.create_table(
        "analysis_tool_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("trace", sa.JSON(), nullable=False),
        sa.Column("result_hash", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tool_id"], ["analysis_tools.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_analysis_tool_runs_user_id", "analysis_tool_runs", ["user_id"])
        op.create_index("ix_analysis_tool_runs_tool_id", "analysis_tool_runs", ["tool_id"])
        op.create_index("ix_analysis_tool_runs_conversation_id", "analysis_tool_runs", ["conversation_id"])
        op.create_index("ix_analysis_tool_runs_status", "analysis_tool_runs", ["status"])


def downgrade() -> None:
    op.drop_table("analysis_tool_runs")
    op.drop_index("ix_analysis_tool_discovery", table_name="analysis_tools")
    op.drop_table("analysis_tools")
