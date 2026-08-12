"""Phone and Google sign-in, linked identities, sessions, and one-time codes.

Existing rows are deliberately not backfilled into `user_identities`. The only
account that predates this migration is the seeded local user, whose address is
an unroutable `.local` placeholder; reserving it as a verified identity would
claim an identifier nobody can prove they own. That account is adopted by the
first real sign-in instead.

Revision ID: 0014_user_authentication
Revises: 0013_recommendation_evidence
"""

from alembic import op
from app.migration_guards import (
    add_column_if_absent,
    create_index_if_absent,
    create_table_if_absent,
    create_unique_constraint_if_absent,
)
import sqlalchemy as sa


revision = "0014_user_authentication"
down_revision = "0013_recommendation_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An account reachable only by phone has no address, so email stops being
    # required. The unique index survives the change and PostgreSQL keeps
    # allowing repeated NULLs under it.
    op.alter_column("users", "email", existing_type=sa.String(length=255), nullable=True)
    add_column_if_absent("users", sa.Column("phone", sa.String(length=20), nullable=True))
    create_unique_constraint_if_absent("uq_users_phone", "users", ["phone"])

    create_table_if_absent(
        "user_identities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("identifier", sa.String(length=320), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="otp"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # The rule that one phone number or address belongs to one account.
        sa.UniqueConstraint("provider", "identifier", name="uq_identity_provider_identifier"),
        sa.UniqueConstraint("user_id", "provider", name="uq_identity_user_provider"),
    )
    create_index_if_absent("ix_user_identities_user_id", "user_identities", ["user_id"])
    create_index_if_absent("ix_user_identities_provider", "user_identities", ["provider"])
    op.alter_column("user_identities", "source", server_default=None)

    create_table_if_absent(
        "user_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    create_index_if_absent("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    create_index_if_absent("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])

    create_table_if_absent(
        "otp_challenges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("purpose", sa.String(length=20), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("destination", sa.String(length=320), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("attempts_remaining", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    create_index_if_absent("ix_otp_challenges_user_id", "otp_challenges", ["user_id"])
    create_index_if_absent("ix_otp_challenges_destination", "otp_challenges", ["destination"])
    create_index_if_absent("ix_otp_challenges_expires_at", "otp_challenges", ["expires_at"])
    # Serves the send-rate window, which counts recent challenges per destination.
    create_index_if_absent("ix_otp_destination_window", "otp_challenges", ["destination", "purpose", "created_at"])
    op.alter_column("otp_challenges", "attempts_remaining", server_default=None)


def downgrade() -> None:
    op.drop_table("otp_challenges")
    op.drop_table("user_sessions")
    op.drop_table("user_identities")
    op.drop_constraint("uq_users_phone", "users", type_="unique")
    op.drop_column("users", "phone")
    # Rows without an address cannot satisfy the restored NOT NULL, so they are
    # given a stable placeholder derived from the primary key rather than
    # blocking the downgrade.
    op.execute(
        "UPDATE users SET email = 'account-' || id || '@placeholder.invalid' WHERE email IS NULL"
    )
    op.alter_column("users", "email", existing_type=sa.String(length=255), nullable=False)
