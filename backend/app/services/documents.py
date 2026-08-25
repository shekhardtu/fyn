"""Reusable immutable document revisions for collaborative product modules."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..event_time import now_utc
from ..models import (
    DocumentAcceptance,
    DocumentChange,
    DocumentRevision,
    SharedDocument,
    SharedRecordParticipant,
)
from .shared_records import SharedRecordConflict, SharedRecordError, payload_hash


ACCEPTANCE_STATEMENT_VERSION = 1
ACCEPTANCE_STATEMENT = "I reviewed this exact revision and its supporting documents, and I acknowledge this shared record."


def loan_document_content(
    *,
    lender_name: str,
    borrower_name: str,
    principal_minor: int,
    currency: str,
    money_date: str,
    due_date: str,
    interest_rate_bps: int,
    interest_period: str,
    interest_mode: str,
    interest_method: str,
    calculation_basis: str,
    rounding_policy: str,
    total_interest_minor: int,
    total_repayable_minor: int,
    note: str | None,
    security_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The stable structured source rendered by every loan-facing surface."""
    return {
        "schemaVersion": 2,
        "title": "Shared repayment plan",
        "parties": {
            "lender": lender_name,
            "borrower": borrower_name,
        },
        "terms": {
            "principalMinor": principal_minor,
            "currency": currency,
            "moneyDate": money_date,
            "dueDate": due_date,
            "interestRateBps": interest_rate_bps,
            "interestPeriod": interest_period,
            "interestMode": interest_mode,
            "interestMethod": interest_method,
            "calculationBasis": calculation_basis,
            "roundingPolicy": rounding_policy,
            "totalInterestMinor": total_interest_minor,
            "totalRepayableMinor": total_repayable_minor,
            "note": note,
        },
        "assuranceItems": security_items or [],
        "plainLanguage": (
            f"{borrower_name} acknowledges a repayment plan with {lender_name}. "
            "Fyn records the shared understanding and reminders; it does not hold funds or decide disputes."
        ),
    }


def _field_values(content: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    parties = content.get("parties") or {}
    terms = content.get("terms") or {}
    for key, value in parties.items():
        values[f"parties.{key}"] = value
    for key, value in terms.items():
        values[f"terms.{key}"] = value
    if "plainLanguage" in content:
        values["plainLanguage"] = content["plainLanguage"]
    for index, item in enumerate(content.get("assuranceItems") or []):
        values[f"assuranceItems.{index}"] = item
    return values


def _label(path: str) -> str:
    if path.startswith("assuranceItems."):
        try:
            number = int(path.rsplit(".", 1)[-1]) + 1
        except ValueError:
            number = 1
        return f"Assurance item {number}"
    labels = {
        "terms.principalMinor": "Principal",
        "terms.currency": "Currency",
        "terms.moneyDate": "Money date",
        "terms.dueDate": "Return date",
        "terms.interestRateBps": "Interest rate",
        "terms.interestPeriod": "Interest period",
        "terms.interestMode": "Interest calculation",
        "terms.interestMethod": "Interest method",
        "terms.calculationBasis": "Calculation basis",
        "terms.roundingPolicy": "Rounding policy",
        "terms.totalInterestMinor": "Total interest",
        "terms.totalRepayableMinor": "Total repayable",
        "terms.note": "Note",
        "parties.lender": "Lender",
        "parties.borrower": "Borrower",
        "plainLanguage": "Plain-language summary",
    }
    return labels.get(path, path.rsplit(".", 1)[-1])


def create_document(
    db: Session,
    *,
    shared_record_id: UUID,
    kind: str,
    title: str,
    template_key: str,
) -> SharedDocument:
    document = SharedDocument(
        shared_record_id=shared_record_id,
        kind=kind,
        title=title,
        status="draft",
        template_key=template_key,
        template_version=1,
    )
    db.add(document)
    db.flush()
    return document


def create_revision(
    db: Session,
    *,
    document: SharedDocument,
    author: SharedRecordParticipant,
    content: dict[str, Any],
    source_snapshot_hash: str,
    base_revision: DocumentRevision | None = None,
) -> DocumentRevision:
    latest = db.scalar(
        select(func.max(DocumentRevision.revision_number)).where(DocumentRevision.document_id == document.id)
    ) or 0
    if latest and base_revision is None:
        raise SharedRecordConflict("Choose the document revision you want to change.")
    if base_revision is not None:
        current = db.scalar(
            select(DocumentRevision)
            .where(DocumentRevision.document_id == document.id)
            .order_by(DocumentRevision.revision_number.desc())
            .limit(1)
            .with_for_update()
        )
        if current is None or current.id != base_revision.id:
            raise SharedRecordConflict("The document changed while you were editing. Review the latest revision before proposing again.")

    digest = payload_hash(content)
    empty_manifest_hash = payload_hash([])
    revision = DocumentRevision(
        document_id=document.id,
        revision_number=latest + 1,
        base_revision_id=base_revision.id if base_revision else None,
        authored_by_participant_id=author.id,
        state="proposed",
        content=content,
        change_summary=[],
        content_schema_version=1,
        source_snapshot_hash=source_snapshot_hash,
        content_hash=digest,
        manifest_hash=empty_manifest_hash,
        evidence_hash=payload_hash({"contentHash": digest, "manifestHash": empty_manifest_hash}),
        proposed_at=now_utc(),
    )
    db.add(revision)
    db.flush()

    before = _field_values(base_revision.content) if base_revision else {}
    after = _field_values(content)
    summaries: list[dict[str, Any]] = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        summary = f"Changed {_label(path)}" if path in before else f"Added {_label(path)}"
        change = DocumentChange(
            revision_id=revision.id,
            authored_by_participant_id=author.id,
            field_path=path,
            before_value={"value": before.get(path)} if path in before else None,
            after_value={"value": after.get(path)} if path in after else None,
            summary=summary,
        )
        db.add(change)
        summaries.append({
            "fieldPath": path,
            "label": _label(path),
            "before": before.get(path),
            "after": after.get(path),
            "summary": summary,
        })
    revision.change_summary = summaries
    document.status = "proposed"
    db.flush()
    return revision


def accept_revision(
    db: Session,
    *,
    document: SharedDocument,
    revision: DocumentRevision,
    participant: SharedRecordParticipant,
    actor_user_id: UUID,
    actor_identifier_masked: str | None = None,
    actor_timezone: str = "Asia/Kolkata",
    request_ip_hash: str | None = None,
    user_agent_hash: str | None = None,
) -> tuple[DocumentAcceptance, bool]:
    if revision.document_id != document.id or revision.state not in {"proposed", "accepted"}:
        raise SharedRecordConflict("That document revision is no longer awaiting agreement.")
    if participant.shared_record_id != document.shared_record_id or participant.member_user_id != actor_user_id:
        raise SharedRecordError("You are not a participant in this document.")
    if revision.content_hash != payload_hash(revision.content):
        raise SharedRecordConflict("The document content no longer matches its recorded hash.")
    from .document_assets import refresh_revision_evidence
    previous_manifest_hash = revision.manifest_hash
    previous_evidence_hash = revision.evidence_hash
    refresh_revision_evidence(db, revision)
    if revision.manifest_hash != previous_manifest_hash or revision.evidence_hash != previous_evidence_hash:
        raise SharedRecordConflict("The supporting-document manifest changed. Review the latest revision before acknowledging it.")

    acceptance = db.scalar(select(DocumentAcceptance).where(
        DocumentAcceptance.revision_id == revision.id,
        DocumentAcceptance.participant_id == participant.id,
    ))
    if acceptance is None:
        acceptance = DocumentAcceptance(
            revision_id=revision.id,
            participant_id=participant.id,
            content_hash=revision.content_hash,
            manifest_hash=revision.manifest_hash,
            evidence_hash=revision.evidence_hash,
            action="accepted",
            actor_user_id=actor_user_id,
            accepted_at=now_utc(),
            statement_version=ACCEPTANCE_STATEMENT_VERSION,
            statement_text=ACCEPTANCE_STATEMENT,
            auth_method="verified_session",
            actor_identifier_masked=actor_identifier_masked,
            actor_timezone=actor_timezone,
            request_ip_hash=request_ip_hash,
            user_agent_hash=user_agent_hash,
        )
        db.add(acceptance)
        db.flush()
    elif acceptance.evidence_hash != revision.evidence_hash:
        raise SharedRecordConflict("The saved acknowledgement refers to different content.")

    required = set(db.scalars(select(SharedRecordParticipant.id).where(
        SharedRecordParticipant.shared_record_id == document.shared_record_id,
        SharedRecordParticipant.role.in_(("lender", "borrower")),
    )))
    accepted = set(db.scalars(select(DocumentAcceptance.participant_id).where(
        DocumentAcceptance.revision_id == revision.id,
        DocumentAcceptance.action == "accepted",
    )))
    finalized = bool(required) and required <= accepted
    if finalized:
        prior = list(db.scalars(select(DocumentRevision).where(
            DocumentRevision.document_id == document.id,
            DocumentRevision.id != revision.id,
            DocumentRevision.state == "accepted",
        )))
        for item in prior:
            item.state = "superseded"
        revision.state = "accepted"
        revision.finalized_at = now_utc()
        document.status = "accepted"
        document.current_revision_number = revision.revision_number
    db.flush()
    return acceptance, finalized
