"""Add immutable document assets and acceptance evidence.

Revision ID: 0006_document_evidence
Revises: 0005_transaction_amendments
"""
from __future__ import annotations

import hashlib
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006_document_evidence"
down_revision: Union[str, None] = "0005_transaction_amendments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def upgrade() -> None:
    op.add_column("personal_loan_agreements", sa.Column("intent", sa.String(length=40), nullable=True))
    op.create_index("ix_personal_loan_agreements_intent", "personal_loan_agreements", ["intent"], unique=False)
    op.execute(sa.text(
        "UPDATE personal_loan_agreements a SET intent = CASE WHEN p.role = 'lender' THEN 'record_given' ELSE 'record_received' END "
        "FROM shared_records r JOIN shared_record_participants p ON p.shared_record_id = r.id AND p.member_user_id = r.created_by_user_id "
        "WHERE a.shared_record_id = r.id"
    ))
    op.execute(sa.text("UPDATE personal_loan_agreements SET intent = 'record_given' WHERE intent IS NULL"))
    op.alter_column("personal_loan_agreements", "intent", nullable=False)
    op.add_column("document_revisions", sa.Column("manifest_hash", sa.String(length=64), nullable=True))
    op.add_column("document_revisions", sa.Column("evidence_hash", sa.String(length=64), nullable=True))
    op.create_index("ix_document_revisions_manifest_hash", "document_revisions", ["manifest_hash"], unique=False)
    op.create_index("ix_document_revisions_evidence_hash", "document_revisions", ["evidence_hash"], unique=False)

    empty_manifest_hash = _hash([])
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, content_hash FROM document_revisions")).mappings()
    for row in rows:
        evidence_hash = _hash({"contentHash": row["content_hash"], "manifestHash": empty_manifest_hash})
        connection.execute(
            sa.text("UPDATE document_revisions SET manifest_hash = :manifest_hash, evidence_hash = :evidence_hash WHERE id = :id"),
            {"manifest_hash": empty_manifest_hash, "evidence_hash": evidence_hash, "id": row["id"]},
        )
    op.alter_column("document_revisions", "manifest_hash", nullable=False)
    op.alter_column("document_revisions", "evidence_hash", nullable=False)

    op.add_column("document_acceptances", sa.Column("manifest_hash", sa.String(length=64), nullable=True))
    op.add_column("document_acceptances", sa.Column("evidence_hash", sa.String(length=64), nullable=True))
    op.add_column("document_acceptances", sa.Column("statement_version", sa.Integer(), server_default=sa.text("1"), nullable=False))
    op.add_column("document_acceptances", sa.Column("statement_text", sa.String(length=500), server_default="I reviewed this exact revision and acknowledge the shared record.", nullable=False))
    op.add_column("document_acceptances", sa.Column("auth_method", sa.String(length=40), server_default="verified_session", nullable=False))
    op.add_column("document_acceptances", sa.Column("actor_identifier_masked", sa.String(length=320), nullable=True))
    op.add_column("document_acceptances", sa.Column("actor_timezone", sa.String(length=80), server_default="Asia/Kolkata", nullable=False))
    op.add_column("document_acceptances", sa.Column("request_ip_hash", sa.String(length=64), nullable=True))
    op.add_column("document_acceptances", sa.Column("user_agent_hash", sa.String(length=64), nullable=True))
    op.create_index("ix_document_acceptances_evidence_hash", "document_acceptances", ["evidence_hash"], unique=False)
    op.create_check_constraint("ck_document_acceptance_statement_version", "document_acceptances", "statement_version > 0")

    acceptance_rows = connection.execute(sa.text(
        "SELECT a.id, r.manifest_hash, r.evidence_hash FROM document_acceptances a "
        "JOIN document_revisions r ON r.id = a.revision_id"
    )).mappings()
    for row in acceptance_rows:
        connection.execute(
            sa.text("UPDATE document_acceptances SET manifest_hash = :manifest_hash, evidence_hash = :evidence_hash WHERE id = :id"),
            {"manifest_hash": row["manifest_hash"], "evidence_hash": row["evidence_hash"], "id": row["id"]},
        )
    op.alter_column("document_acceptances", "manifest_hash", nullable=False)
    op.alter_column("document_acceptances", "evidence_hash", nullable=False)
    op.alter_column("document_acceptances", "statement_version", server_default=None)
    op.alter_column("document_acceptances", "statement_text", server_default=None)
    op.alter_column("document_acceptances", "auth_method", server_default=None)
    op.alter_column("document_acceptances", "actor_timezone", server_default=None)

    op.create_table(
        "document_assets",
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("uploaded_by_participant_id", sa.Uuid(), nullable=True),
        sa.Column("original_filename", sa.String(length=240), nullable=False),
        sa.Column("media_type", sa.String(length=80), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=160), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("classification", sa.String(length=50), nullable=False),
        sa.Column("description", sa.String(length=240), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("byte_size > 0", name="ck_document_asset_positive_size"),
        sa.ForeignKeyConstraint(["document_id"], ["shared_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["uploaded_by_participant_id"], ["shared_record_participants.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    for name in ("owner_user_id", "document_id", "uploaded_by_participant_id", "sha256", "state", "classification"):
        op.create_index(f"ix_document_assets_{name}", "document_assets", [name], unique=False)

    op.create_table(
        "document_revision_assets",
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("display_order >= 0", name="ck_document_revision_asset_order"),
        sa.ForeignKeyConstraint(["asset_id"], ["document_assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["revision_id"], ["document_revisions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision_id", "asset_id", name="uq_document_revision_asset"),
        sa.UniqueConstraint("asset_id", name="uq_document_asset_single_revision"),
    )
    op.create_index("ix_document_revision_assets_revision_id", "document_revision_assets", ["revision_id"], unique=False)
    op.create_index("ix_document_revision_assets_asset_id", "document_revision_assets", ["asset_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_document_revision_assets_asset_id", table_name="document_revision_assets")
    op.drop_index("ix_document_revision_assets_revision_id", table_name="document_revision_assets")
    op.drop_table("document_revision_assets")
    for name in reversed(("owner_user_id", "document_id", "uploaded_by_participant_id", "sha256", "state", "classification")):
        op.drop_index(f"ix_document_assets_{name}", table_name="document_assets")
    op.drop_table("document_assets")
    op.drop_constraint("ck_document_acceptance_statement_version", "document_acceptances", type_="check")
    op.drop_index("ix_document_acceptances_evidence_hash", table_name="document_acceptances")
    for name in ("user_agent_hash", "request_ip_hash", "actor_timezone", "actor_identifier_masked", "auth_method", "statement_text", "statement_version", "evidence_hash", "manifest_hash"):
        op.drop_column("document_acceptances", name)
    op.drop_index("ix_document_revisions_evidence_hash", table_name="document_revisions")
    op.drop_index("ix_document_revisions_manifest_hash", table_name="document_revisions")
    op.drop_column("document_revisions", "evidence_hash")
    op.drop_column("document_revisions", "manifest_hash")
    op.drop_index("ix_personal_loan_agreements_intent", table_name="personal_loan_agreements")
    op.drop_column("personal_loan_agreements", "intent")
