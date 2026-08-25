"""Represent monthly and yearly simple-interest terms explicitly.

Revision ID: 0007_interest_periods
Revises: 0006_document_evidence
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0007_interest_periods"
down_revision: Union[str, None] = "0006_document_evidence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The old column stored a yearly rate. Renaming preserves every accepted
    # term value while the explicit period backfill preserves its meaning.
    op.alter_column("loan_term_versions", "annual_rate_bps", new_column_name="interest_rate_bps")
    op.add_column("loan_term_versions", sa.Column("interest_period", sa.String(length=16), server_default="yearly", nullable=False))
    op.add_column("loan_term_versions", sa.Column("calculation_basis", sa.String(length=30), nullable=True))
    op.add_column("loan_term_versions", sa.Column("rounding_policy", sa.String(length=30), server_default="half_up_minor_unit", nullable=False))
    op.execute(sa.text(
        "UPDATE loan_term_versions SET "
        "interest_method = CASE WHEN interest_rate_bps = 0 THEN 'none' ELSE 'simple_yearly' END, "
        "calculation_basis = CASE WHEN interest_rate_bps = 0 THEN 'not_applicable' ELSE 'actual_365' END"
    ))
    op.alter_column("loan_term_versions", "interest_period", server_default=None)
    op.alter_column("loan_term_versions", "calculation_basis", nullable=False)
    op.alter_column("loan_term_versions", "rounding_policy", server_default=None)
    op.create_check_constraint(
        "ck_loan_term_interest_period",
        "loan_term_versions",
        "interest_period IN ('monthly', 'yearly')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_loan_term_interest_period", "loan_term_versions", type_="check")
    op.execute(sa.text(
        "UPDATE loan_term_versions SET interest_method = "
        "CASE WHEN interest_rate_bps = 0 THEN 'none' ELSE 'simple_annual' END"
    ))
    op.drop_column("loan_term_versions", "rounding_policy")
    op.drop_column("loan_term_versions", "calculation_basis")
    op.drop_column("loan_term_versions", "interest_period")
    op.alter_column("loan_term_versions", "interest_rate_bps", new_column_name="annual_rate_bps")
