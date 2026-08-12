"""Sweep generated tools created before the governed semantic registry.

Revision ID: 0006_semantic_registry_sweep
Revises: 0005_semantic_retrieval
"""
from alembic import op


revision = "0006_semantic_registry_sweep"
down_revision = "0005_semantic_retrieval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Generated tools are reproducible cache entries, not financial truth.
    # Removing the old registry generation prevents stale plans and retrieval
    # embeddings from surviving the semantic-contract boundary.
    op.execute("DELETE FROM analysis_tool_runs")
    op.execute("DELETE FROM analysis_tools")


def downgrade() -> None:
    # Deleted generated cache entries cannot and should not be reconstructed.
    pass
