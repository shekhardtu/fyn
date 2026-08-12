from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..domain import (
    FinancialSourceType,
    ObservationProcessingState,
    ReconciliationOutcome,
    ReconciliationResolution,
    TransactionStatus,
    ValueEnum,
)
from ..event_time import as_utc, now_utc
from ..models import (
    FinancialObservation,
    ReconciliationCandidate,
    ReconciliationDecision,
    Transaction,
    TransactionSource,
)
from ..schemas import ObservationIn, ReconciliationResultOut
from .agents import evaluate_reconciliation_match
from .extraction import MERCHANT_RULES, normalize_merchant
from .repositories import UserScopedRepository
from .taxonomy import TaxonomyRepository
from .transactions import active_transaction, canonical_transactions, create_transaction, owned_transaction_source


class ReconciliationSignal(ValueEnum):
    AMOUNT = "amount"
    MERCHANT = "merchant"
    DATE = "date"
    ACCOUNT = "account"
    REFERENCE = "reference"
    DESCRIPTION = "description"
    DIRECTION = "direction"


@dataclass(frozen=True)
class ReconciliationConfig:
    weights: dict[ReconciliationSignal, Decimal] = field(default_factory=lambda: {
        ReconciliationSignal.AMOUNT: Decimal("0.30"),
        ReconciliationSignal.MERCHANT: Decimal("0.25"),
        ReconciliationSignal.DATE: Decimal("0.15"),
        ReconciliationSignal.ACCOUNT: Decimal("0.10"),
        ReconciliationSignal.REFERENCE: Decimal("0.10"),
        ReconciliationSignal.DESCRIPTION: Decimal("0.05"),
        ReconciliationSignal.DIRECTION: Decimal("0.05"),
    })
    auto_match_threshold: Decimal = Decimal("0.82")
    review_threshold: Decimal = Decimal("0.58")
    ambiguity_margin: Decimal = Decimal("0.07")
    ai_assistance_enabled: bool = True
    ai_match_confidence_threshold: Decimal = Decimal("0.95")
    ai_evidence_lift_cap: Decimal = Decimal("0.20")
    date_window_days: int = 3
    source_authority: dict[str, int] = field(default_factory=lambda: {
        FinancialSourceType.BANK: 90,
        FinancialSourceType.CSV: 85,
        FinancialSourceType.SMS: 65,
        FinancialSourceType.EMAIL: 70,
        FinancialSourceType.RECEIPT: 80,
        FinancialSourceType.MANUAL: 50,
        FinancialSourceType.API: 85,
        FinancialSourceType.PDF: 80,
    })
    field_authority: dict[str, dict[str, int]] = field(default_factory=lambda: {
        "merchant": {
            FinancialSourceType.RECEIPT: 95,
            FinancialSourceType.EMAIL: 85,
            FinancialSourceType.API: 80,
            FinancialSourceType.BANK: 75,
            FinancialSourceType.CSV: 75,
            FinancialSourceType.SMS: 65,
            FinancialSourceType.MANUAL: 60,
            FinancialSourceType.PDF: 80,
        },
        "amount": {
            FinancialSourceType.BANK: 95,
            FinancialSourceType.CSV: 90,
            FinancialSourceType.API: 90,
            FinancialSourceType.SMS: 75,
            FinancialSourceType.EMAIL: 70,
            FinancialSourceType.RECEIPT: 70,
            FinancialSourceType.MANUAL: 60,
            FinancialSourceType.PDF: 80,
        },
        "category": {
            "user": 100,
            "merchant_preference": 90,
            FinancialSourceType.RECEIPT: 65,
            FinancialSourceType.EMAIL: 60,
            FinancialSourceType.BANK: 50,
            FinancialSourceType.SMS: 45,
            FinancialSourceType.MANUAL: 70,
        },
    })

    def __post_init__(self) -> None:
        normalized = {
            ReconciliationSignal(key): Decimal(value)
            for key, value in self.weights.items()
        }
        if set(normalized) != set(ReconciliationSignal):
            raise ValueError("Reconciliation weights must cover every governed signal")
        if sum(normalized.values()) != Decimal("1"):
            raise ValueError("Reconciliation weights must total 1")
        object.__setattr__(self, "weights", normalized)


DEFAULT_CONFIG = ReconciliationConfig()


def observation_hash(user_id: UUID, payload: ObservationIn) -> str:
    identity = {
        "user_id": str(user_id),
        "source_type": payload.source_type,
        "source_message_id": payload.source_message_id,
        "external_transaction_id": payload.external_transaction_id,
        "amount_minor": payload.amount_minor,
        "currency": payload.currency.upper(),
        "merchant": normalize_merchant(payload.merchant),
        "transaction_type": payload.transaction_type,
        "transaction_at": as_utc(payload.transaction_at or now_utc()).isoformat(),
        "description": (payload.description or "").strip().lower(),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def merchant_similarity(left: str | None, right: str | None) -> Decimal:
    left_n, right_n = normalize_merchant(left), normalize_merchant(right)
    if not left_n or not right_n:
        return Decimal("0.5")
    if left_n == right_n:
        return Decimal("1")
    if left_n in right_n or right_n in left_n:
        return Decimal("0.92")
    return Decimal(str(round(SequenceMatcher(None, left_n, right_n).ratio(), 4)))


def score_match(observation: FinancialObservation, transaction: Transaction, config: ReconciliationConfig = DEFAULT_CONFIG) -> tuple[Decimal, dict]:
    same_source_ids = {
        source.external_id
        for source in transaction.sources
        if source.source_type == observation.source_type and source.external_id
    }
    if observation.external_transaction_id and same_source_ids and observation.external_transaction_id not in same_source_ids:
        return Decimal("0"), {"conflicting_same_source_id": True}
    canonical_instants = [as_utc(transaction.transaction_at)]
    if transaction.posted_at:
        canonical_instants.append(as_utc(transaction.posted_at))
    observation_instants = [as_utc(observation.transaction_at)]
    if observation.posted_at:
        observation_instants.append(as_utc(observation.posted_at))
    seconds = min(abs((observed - canonical).total_seconds()) for observed in observation_instants for canonical in canonical_instants)
    days = int(seconds // 86_400)
    description_score = Decimal(str(round(SequenceMatcher(None, (observation.description or "").lower(), (transaction.description or "").lower()).ratio(), 4))) if observation.description and transaction.description else Decimal("0.5")
    account_score = Decimal("0.5")
    # A source-account mask is strong only when the canonical account relationship is known.
    if observation.source_account and transaction.account_id:
        account_score = Decimal("0.7")
    signals = {
        ReconciliationSignal.AMOUNT: Decimal("1") if observation.amount_minor == transaction.amount_minor and observation.currency == transaction.currency else Decimal("0"),
        ReconciliationSignal.MERCHANT: merchant_similarity(observation.merchant_normalized, transaction.merchant_name),
        ReconciliationSignal.DATE: Decimal("1") if days == 0 else Decimal("0.72") if days == 1 else Decimal("0.35") if days <= config.date_window_days else Decimal("0"),
        ReconciliationSignal.ACCOUNT: account_score,
        ReconciliationSignal.REFERENCE: Decimal("1") if observation.reference_number and observation.reference_number in (transaction.description or "") else Decimal("0"),
        ReconciliationSignal.DESCRIPTION: description_score,
        ReconciliationSignal.DIRECTION: Decimal("1") if observation.transaction_type == transaction.transaction_type else Decimal("0"),
    }
    score = sum(config.weights[key] * value for key, value in signals.items())
    serializable = {key.value: float(value) for key, value in signals.items()}
    serializable["date_distance_days"] = days
    return score.quantize(Decimal("0.0001")), serializable


def candidate_transactions(db: Session, observation: FinancialObservation, config: ReconciliationConfig = DEFAULT_CONFIG) -> list[Transaction]:
    start = observation.transaction_at - timedelta(days=config.date_window_days)
    end = observation.transaction_at + timedelta(days=config.date_window_days)
    return list(db.scalars(canonical_transactions(observation.user_id, currency=observation.currency).where(
        Transaction.amount_minor == observation.amount_minor,
        Transaction.transaction_type == observation.transaction_type,
        or_(Transaction.transaction_at.between(start, end), Transaction.posted_at.between(start, end)),
    )))


def attach_observation(
    db: Session,
    observation: FinancialObservation,
    transaction: Transaction,
    score: Decimal,
    config: ReconciliationConfig = DEFAULT_CONFIG,
    *,
    field_values: dict | None = None,
) -> None:
    source_fields = field_values or {
        "amount_minor": observation.amount_minor,
        "merchant_raw": observation.merchant_raw,
        "transaction_at": as_utc(observation.transaction_at).isoformat(),
        "posted_at": as_utc(observation.posted_at).isoformat() if observation.posted_at else None,
    }
    db.add(TransactionSource(
        transaction_id=transaction.id,
        observation_id=observation.id,
        source_type=observation.source_type,
        external_id=observation.external_transaction_id,
        source_account=observation.source_account,
        source_message_id=observation.source_message_id,
        source_hash=observation.source_hash,
        observed_at=observation.observed_at,
        raw_reference=observation.raw_reference,
        confidence=score,
        field_values=source_fields,
    ))
    observation.processing_state = ObservationProcessingState.ATTACHED
    if config.source_authority.get(observation.source_type, 0) >= 65:
        transaction.status = TransactionStatus.CONFIRMED
    merchant_authority = config.field_authority.get("merchant", {})
    if observation.merchant_raw and merchant_authority.get(observation.source_type, 0) > merchant_authority.get(FinancialSourceType.MANUAL, 60):
        if not transaction.merchant_name or merchant_similarity(observation.merchant_raw, transaction.merchant_name) >= Decimal("0.7"):
            transaction.merchant_name = observation.merchant_raw.title()


def _new_transaction(db: Session, observation: FinancialObservation, config: ReconciliationConfig = DEFAULT_CONFIG) -> Transaction:
    transaction = create_transaction(
        db,
        user_id=observation.user_id,
        transaction_type=observation.transaction_type,
        amount_minor=observation.amount_minor,
        currency=observation.currency,
        merchant_name=observation.merchant_raw,
        transaction_at=observation.transaction_at,
        posted_at=observation.posted_at,
        description=observation.description,
        status=TransactionStatus.CONFIRMED if config.source_authority.get(observation.source_type, 0) >= 80 else TransactionStatus.PROVISIONAL,
        confidence=observation.confidence,
    )
    attach_observation(db, observation, transaction, observation.confidence, config)
    return transaction


def _category_compatible(db: Session, observation: FinancialObservation, transaction: Transaction) -> bool:
    if transaction.merchant_name or not transaction.category_id or not observation.merchant_normalized:
        return False
    mapping = None
    for alias, candidate in MERCHANT_RULES.items():
        if observation.merchant_normalized == alias or observation.merchant_normalized.startswith(alias + " "):
            mapping = candidate
            break
    if not mapping:
        return False
    category = TaxonomyRepository(db, observation.user_id).category(
        transaction.category_id,
    )
    return bool(category and category.slug == mapping[0])


def _ai_match_advice(
    observation: FinancialObservation,
    transaction: Transaction,
    signals: dict,
):
    """Get bounded advice for one deterministically generated candidate.

    No database session, model mutation tool, raw document, or unrestricted
    financial history crosses this boundary. An unavailable model must never
    interrupt ingestion.
    """
    observation_payload = {
        "source_type": observation.source_type,
        "source_account": observation.source_account,
        "transaction_type": observation.transaction_type,
        "amount_minor": observation.amount_minor,
        "currency": observation.currency,
        "merchant_raw": observation.merchant_raw,
        "merchant_normalized": observation.merchant_normalized,
        "transaction_at": as_utc(observation.transaction_at).isoformat(),
        "posted_at": as_utc(observation.posted_at).isoformat() if observation.posted_at else None,
        "reference_number": observation.reference_number,
        "description": observation.description,
    }
    candidate_payload = {
        "transaction_type": transaction.transaction_type,
        "amount_minor": transaction.amount_minor,
        "currency": transaction.currency,
        "merchant": transaction.merchant_name,
        "transaction_at": as_utc(transaction.transaction_at).isoformat(),
        "posted_at": as_utc(transaction.posted_at).isoformat() if transaction.posted_at else None,
        "description": transaction.description,
        "source_types": sorted({source.source_type for source in transaction.sources}),
        "source_accounts": sorted({source.source_account for source in transaction.sources if source.source_account}),
    }
    try:
        return evaluate_reconciliation_match(observation_payload, candidate_payload, signals)
    except Exception:
        # Reconciliation remains available during model/network outages. The
        # unresolved candidate is persisted for HITL immediately below.
        return None


def _existing_observation(
    db: Session,
    user_id: UUID,
    payload: ObservationIn,
    source_hash: str,
) -> FinancialObservation | None:
    """Resolve idempotency keys in their canonical priority order."""
    identifiers = (
        (FinancialObservation.source_hash, source_hash),
        (FinancialObservation.source_message_id, payload.source_message_id),
        (FinancialObservation.external_transaction_id, payload.external_transaction_id),
    )
    for column, value in identifiers:
        if value is None:
            continue
        existing = db.scalar(select(FinancialObservation).where(
            FinancialObservation.user_id == user_id,
            FinancialObservation.source_type == payload.source_type,
            column == value,
        ))
        if existing:
            return existing
    return None


def ingest_observation(db: Session, user_id: UUID, payload: ObservationIn, config: ReconciliationConfig = DEFAULT_CONFIG) -> ReconciliationResultOut:
    payload = payload.model_copy(update={
        "transaction_at": as_utc(payload.transaction_at or now_utc()),
        "posted_at": as_utc(payload.posted_at) if payload.posted_at else None,
        "observed_at": as_utc(payload.observed_at or now_utc()),
    })
    source_hash = observation_hash(user_id, payload)
    existing = _existing_observation(db, user_id, payload, source_hash)
    if existing:
        source = owned_transaction_source(
            db,
            user_id,
            TransactionSource.observation_id == existing.id,
        )
        return ReconciliationResultOut(
            observation_id=existing.id,
            transaction_id=source.transaction_id if source else None,
            decision=ReconciliationOutcome.IDEMPOTENT_REPLAY,
            idempotent_replay=True,
        )

    observation = FinancialObservation(
        user_id=user_id,
        source_type=payload.source_type,
        source_message_id=payload.source_message_id,
        source_hash=source_hash,
        external_transaction_id=payload.external_transaction_id,
        source_account=payload.source_account,
        transaction_type=payload.transaction_type,
        amount_minor=payload.amount_minor,
        currency=payload.currency.upper(),
        merchant_raw=payload.merchant,
        merchant_normalized=normalize_merchant(payload.merchant),
        transaction_at=payload.transaction_at,
        posted_at=payload.posted_at,
        reference_number=payload.reference_number,
        description=payload.description,
        observed_at=payload.observed_at,
        raw_reference=payload.raw_reference,
    )
    db.add(observation)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        replay = _existing_observation(db, user_id, payload, source_hash)
        source = owned_transaction_source(
            db,
            user_id,
            TransactionSource.observation_id == replay.id,
        ) if replay else None
        return ReconciliationResultOut(observation_id=replay.id, transaction_id=source.transaction_id if source else None, decision=ReconciliationOutcome.IDEMPOTENT_REPLAY, idempotent_replay=True)

    # External unique IDs are authoritative within a source.
    if payload.external_transaction_id:
        exact_source = owned_transaction_source(
            db,
            user_id,
            TransactionSource.source_type == payload.source_type,
            TransactionSource.external_id == payload.external_transaction_id,
        )
        if exact_source:
            transaction = UserScopedRepository(db, user_id).get(
                Transaction,
                exact_source.transaction_id,
            )
            if transaction is None:
                raise ValueError("Transaction source references an unavailable transaction")
            attach_observation(db, observation, transaction, Decimal("1"), config)
            db.commit()
            return ReconciliationResultOut(observation_id=observation.id, transaction_id=transaction.id, decision=ReconciliationOutcome.MATCHED, score=1.0, signals={"external_transaction_id": True})

    scored = []
    for transaction in candidate_transactions(db, observation, config):
        score, signals = score_match(observation, transaction, config)
        scored.append((score, transaction, signals))
    scored.sort(key=lambda item: item[0], reverse=True)

    if scored:
        top_score, top_transaction, top_signals = scored[0]
        ambiguous = len(scored) > 1 and scored[1][0] >= top_score - config.ambiguity_margin
        # A single same-day manual draft with no merchant can be corroborated by a
        # merchant whose learned taxonomy agrees with the user's chosen category.
        compatible_manual_draft = (
            len(scored) == 1
            and top_score >= config.review_threshold
            and top_signals.get("date_distance_days") == 0
            and _category_compatible(db, observation, top_transaction)
            and len(top_transaction.sources) == 1
            and top_transaction.sources[0].source_type == FinancialSourceType.MANUAL
        )
        if compatible_manual_draft:
            top_signals["category_compatible"] = True
            top_score = max(top_score, config.auto_match_threshold)
        strong_deterministic = (
            len(scored) == 1
            and top_signals.get("amount") == 1.0
            and top_signals.get("merchant") == 1.0
            and top_signals.get("date") == 1.0
            and top_signals.get("direction") == 1.0
        )
        if strong_deterministic:
            top_signals["strong_deterministic"] = True
            top_score = max(top_score, config.auto_match_threshold)
        if top_score >= config.auto_match_threshold and not ambiguous:
            attach_observation(db, observation, top_transaction, top_score, config)
            db.add(ReconciliationCandidate(user_id=user_id, observation_id=observation.id, transaction_id=top_transaction.id, score=top_score, confidence=top_score, matching_signals=top_signals, decision=ReconciliationOutcome.MATCHED))
            db.commit()
            return ReconciliationResultOut(observation_id=observation.id, transaction_id=top_transaction.id, decision=ReconciliationOutcome.MATCHED, score=float(top_score), signals=top_signals)
        if top_score >= config.review_threshold:
            advice = _ai_match_advice(observation, top_transaction, top_signals) if config.ai_assistance_enabled else None
            if advice is not None:
                ai_confidence = Decimal(str(advice.confidence))
                adjudicated_confidence = min(
                    ai_confidence,
                    top_score + config.ai_evidence_lift_cap,
                ).quantize(Decimal("0.0001"))
                top_signals["ai_assisted"] = {
                    "same_transaction": advice.same_transaction,
                    "confidence": float(ai_confidence),
                    "reason": advice.reason,
                    "deterministic_score": float(top_score),
                    "adjudicated_confidence": float(adjudicated_confidence),
                }
                ai_policy_match = (
                    advice.same_transaction
                    and ai_confidence >= config.ai_match_confidence_threshold
                    and adjudicated_confidence >= config.auto_match_threshold
                    and not ambiguous
                )
                if ai_policy_match:
                    attach_observation(db, observation, top_transaction, adjudicated_confidence, config)
                    candidate = ReconciliationCandidate(
                        user_id=user_id,
                        observation_id=observation.id,
                        transaction_id=top_transaction.id,
                        score=top_score,
                        confidence=adjudicated_confidence,
                        matching_signals=top_signals,
                        decision=ReconciliationOutcome.MATCHED,
                    )
                    db.add(candidate)
                    db.flush()
                    db.add(ReconciliationDecision(
                        candidate_id=candidate.id,
                        decision=ReconciliationOutcome.MATCHED,
                        decided_by="system",
                        reason="Deterministic reconciliation policy approved high-confidence AI-assisted evidence",
                    ))
                    db.commit()
                    return ReconciliationResultOut(
                        observation_id=observation.id,
                        transaction_id=top_transaction.id,
                        decision=ReconciliationOutcome.MATCHED,
                        score=float(adjudicated_confidence),
                        signals=top_signals,
                    )
            for score, transaction, signals in scored[:3]:
                db.add(ReconciliationCandidate(user_id=user_id, observation_id=observation.id, transaction_id=transaction.id, score=score, confidence=score, matching_signals=signals, decision=ReconciliationOutcome.NEEDS_REVIEW))
            observation.processing_state = ObservationProcessingState.NEEDS_REVIEW
            db.commit()
            return ReconciliationResultOut(observation_id=observation.id, transaction_id=None, decision=ReconciliationOutcome.NEEDS_REVIEW, score=float(top_score), signals=top_signals)

    transaction = _new_transaction(db, observation, config)
    db.commit()
    return ReconciliationResultOut(observation_id=observation.id, transaction_id=transaction.id, decision=ReconciliationOutcome.NOT_MATCHED, score=float(scored[0][0]) if scored else None, signals=scored[0][2] if scored else {})


def resolve_reconciliation(db: Session, user_id: UUID, candidate_id: UUID, decision: ReconciliationResolution | str) -> Transaction:
    decision = ReconciliationResolution(decision)
    candidate = UserScopedRepository(db, user_id).get(ReconciliationCandidate, candidate_id)
    if not candidate:
        raise ValueError("Reconciliation candidate not found")
    owned = UserScopedRepository(db, user_id)
    observation = owned.get(FinancialObservation, candidate.observation_id)
    if observation is None:
        raise ValueError("Reconciliation observation not found")
    existing_source = owned_transaction_source(
        db,
        user_id,
        TransactionSource.observation_id == observation.id,
    )
    if existing_source:
        transaction = owned.get(Transaction, existing_source.transaction_id)
        if transaction is None:
            raise ValueError("Reconciled transaction not found")
        return transaction
    related = list(db.scalars(select(ReconciliationCandidate).where(
        ReconciliationCandidate.user_id == user_id,
        ReconciliationCandidate.observation_id == observation.id,
    )))
    if decision is ReconciliationResolution.SAME_TRANSACTION:
        transaction = active_transaction(db, user_id, candidate.transaction_id)
        if transaction is None:
            raise ValueError("Reconciliation transaction not found")
        attach_observation(db, observation, transaction, candidate.score)
        final_decision = ReconciliationOutcome.MATCHED
        reason = "User confirmed both observations represent the same transaction"
    elif decision is ReconciliationResolution.SEPARATE_TRANSACTION:
        transaction = _new_transaction(db, observation)
        final_decision = ReconciliationOutcome.NOT_MATCHED
        reason = "User confirmed this is a separate transaction"
    else:
        raise ValueError("Unknown reconciliation decision")
    for item in related:
        item.decision = final_decision if item.id == candidate.id else ReconciliationOutcome.NOT_MATCHED
    db.add(ReconciliationDecision(candidate_id=candidate.id, decision=final_decision, decided_by="user", reason=reason))
    db.commit()
    db.refresh(transaction)
    return transaction
