from datetime import date

from sqlalchemy import func, select

from app.models import ReconciliationCandidate, Transaction, TransactionSource, User
from app.schemas import ObservationIn
from app.seed import default_user
from app.services import reconciliation as reconciliation_service
from app.services.agents import AIAssistedMatch
from app.services.conversation import get_or_create_conversation, handle_action, handle_chat
from app.services.reconciliation import ReconciliationConfig, ingest_observation, resolve_reconciliation


def _manual_toit(db, user, day: date):
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, f"Spent ₹2,000 at Toit on {day.isoformat()}")
    draft_id = response.widgets[0].data["draftId"]
    handle_action(db, user, conversation, "commit_transaction", {"draftId": draft_id})
    transaction = db.scalar(select(Transaction).order_by(Transaction.created_at.desc()))
    transaction.transaction_date = day
    db.commit()
    return transaction


def test_manual_sms_email_become_one_canonical_transaction(db):
    user = default_user(db)
    transaction = _manual_toit(db, user, date(2026, 8, 10))
    sms = ingest_observation(db, user.id, ObservationIn(
        source_type="sms", source_message_id="sms-1", transaction_type="expense", amount_minor=200_000,
        merchant="TOIT", transaction_date=date(2026, 8, 10), description="₹2,000 debited at TOIT",
    ))
    assert sms.decision == "MATCHED"
    assert sms.transaction_id == transaction.id
    email = ingest_observation(db, user.id, ObservationIn(
        source_type="email", source_message_id="email-1", transaction_type="expense", amount_minor=200_000,
        merchant="Toit", transaction_date=date(2026, 8, 10), description="Your payment of ₹2,000 at Toit was successful",
    ))
    assert email.decision == "MATCHED"
    assert email.transaction_id == transaction.id
    assert db.scalar(select(func.count(Transaction.id))) == 1
    assert db.scalar(select(func.count(TransactionSource.id))) == 3


def test_categorized_bare_manual_entry_is_corroborated_by_toit_sources(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "₹2,000")
    draft_id = response.widgets[0].data["draftId"]
    food_id = next(item["id"] for item in response.widgets[0].data["options"] if item["slug"] == "food")
    response = handle_action(db, user, conversation, "select_category", {"draftId": draft_id, "categoryId": food_id})
    dining_id = next(item["id"] for item in response.widgets[0].data["options"] if item["slug"] == "dining")
    handle_action(db, user, conversation, "select_subcategory", {"draftId": draft_id, "subcategoryId": dining_id})
    handle_action(db, user, conversation, "commit_transaction", {"draftId": draft_id})
    transaction = db.scalar(select(Transaction))

    sms = ingest_observation(db, user.id, ObservationIn(source_type="sms", source_message_id="bare-sms", transaction_type="expense", amount_minor=200_000, merchant="TOIT", transaction_date=date.today()))
    email = ingest_observation(db, user.id, ObservationIn(source_type="email", source_message_id="bare-email", transaction_type="expense", amount_minor=200_000, merchant="Toit", transaction_date=date.today()))
    assert sms.transaction_id == email.transaction_id == transaction.id
    assert db.scalar(select(func.count(Transaction.id))) == 1
    assert db.scalar(select(func.count(TransactionSource.id))) == 3


def test_ingestion_is_idempotent(db):
    user = default_user(db)
    payload = ObservationIn(source_type="sms", source_message_id="same-message", transaction_type="expense", amount_minor=85_000, merchant="Swiggy", transaction_date=date(2026, 8, 10))
    first = ingest_observation(db, user.id, payload)
    second = ingest_observation(db, user.id, payload)
    assert first.transaction_id == second.transaction_id
    assert second.idempotent_replay is True
    assert db.scalar(select(func.count(Transaction.id))) == 1


def test_external_transaction_id_is_idempotent_even_if_payload_changes(db):
    user = default_user(db)
    first = ObservationIn(source_type="bank", external_transaction_id="stable-bank-id", transaction_type="expense", amount_minor=85_000, merchant="Swiggy", transaction_date=date(2026, 8, 10))
    changed = ObservationIn(source_type="bank", external_transaction_id="stable-bank-id", transaction_type="expense", amount_minor=85_100, merchant="SWIGGY ONLINE", transaction_date=date(2026, 8, 11))
    original = ingest_observation(db, user.id, first)
    replay = ingest_observation(db, user.id, changed)
    assert replay.idempotent_replay is True
    assert replay.transaction_id == original.transaction_id
    assert db.scalar(select(func.count(Transaction.id))) == 1


def test_external_transaction_ids_are_scoped_to_the_user(db):
    first_user = default_user(db)
    second_user = User(email="second@example.com")
    db.add(second_user)
    db.commit()
    payload = ObservationIn(
        source_type="bank",
        external_transaction_id="bank-shared-id",
        transaction_type="expense",
        amount_minor=85_000,
        merchant="Swiggy",
        transaction_date=date(2026, 8, 10),
    )

    first = ingest_observation(db, first_user.id, payload)
    second = ingest_observation(db, second_user.id, payload)

    assert first.transaction_id != second.transaction_id
    assert db.scalar(select(func.count(Transaction.id))) == 2


def test_same_amount_different_merchants_do_not_merge(db):
    user = default_user(db)
    first = ingest_observation(db, user.id, ObservationIn(source_type="bank", external_transaction_id="bank-1", transaction_type="expense", amount_minor=200_000, merchant="Toit", transaction_date=date(2026, 8, 10)))
    second = ingest_observation(db, user.id, ObservationIn(source_type="bank", external_transaction_id="bank-2", transaction_type="expense", amount_minor=200_000, merchant="Amazon", transaction_date=date(2026, 8, 10)))
    assert first.transaction_id != second.transaction_id
    assert db.scalar(select(func.count(Transaction.id))) == 2


def test_same_merchant_amount_consecutive_dates_with_distinct_bank_ids_stay_separate(db):
    user = default_user(db)
    first = ingest_observation(db, user.id, ObservationIn(source_type="bank", external_transaction_id="day-1", transaction_type="expense", amount_minor=200_000, merchant="Starbucks", transaction_date=date(2026, 8, 10)))
    second = ingest_observation(db, user.id, ObservationIn(source_type="bank", external_transaction_id="day-2", transaction_type="expense", amount_minor=200_000, merchant="Starbucks", transaction_date=date(2026, 8, 11)))
    assert first.transaction_id != second.transaction_id
    assert db.scalar(select(func.count(Transaction.id))) == 2


def test_ai_evaluator_is_not_called_for_strong_deterministic_match(db, monkeypatch):
    user = default_user(db)
    first = ingest_observation(db, user.id, ObservationIn(
        source_type="bank", external_transaction_id="strong-bank", transaction_type="expense",
        amount_minor=200_000, merchant="Toit", transaction_date=date(2026, 8, 10),
    ))
    monkeypatch.setattr(
        reconciliation_service,
        "evaluate_reconciliation_match",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("AI must be bypassed")),
    )

    corroborating = ingest_observation(db, user.id, ObservationIn(
        source_type="sms", source_message_id="strong-sms", transaction_type="expense",
        amount_minor=200_000, merchant="TOIT", transaction_date=date(2026, 8, 10),
    ))

    assert corroborating.decision == "MATCHED"
    assert corroborating.transaction_id == first.transaction_id
    assert corroborating.signals["strong_deterministic"] is True


def test_ai_evaluator_is_not_called_below_review_threshold(db, monkeypatch):
    user = default_user(db)
    first = ingest_observation(db, user.id, ObservationIn(
        source_type="bank", external_transaction_id="low-bank", transaction_type="expense",
        amount_minor=200_000, merchant="Toit", transaction_date=date(2026, 8, 10),
    ))
    monkeypatch.setattr(
        reconciliation_service,
        "evaluate_reconciliation_match",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("AI must be bypassed")),
    )

    unrelated = ingest_observation(db, user.id, ObservationIn(
        source_type="receipt", source_message_id="low-receipt", transaction_type="expense",
        amount_minor=200_000, merchant="City Pharmacy", transaction_date=date(2026, 8, 13),
    ))

    assert unrelated.decision == "NOT_MATCHED"
    assert unrelated.transaction_id != first.transaction_id


def test_medium_candidate_uses_ai_advice_under_deterministic_policy(db, monkeypatch):
    user = default_user(db)
    bank = ingest_observation(db, user.id, ObservationIn(
        source_type="bank", external_transaction_id="ai-amazon-bank", transaction_type="expense",
        amount_minor=200_000, merchant="Amazon", transaction_date=date(2026, 8, 10),
    ))
    calls = []

    def advise(observation, candidate, signals):
        calls.append((observation, candidate, signals))
        return AIAssistedMatch(
            same_transaction=True,
            confidence=0.99,
            reason="The receipt expands the normalized merchant while amount, direction and date agree.",
        )

    monkeypatch.setattr(reconciliation_service, "evaluate_reconciliation_match", advise)
    receipt = ingest_observation(db, user.id, ObservationIn(
        source_type="receipt", source_message_id="ai-amazon-receipt", transaction_type="expense",
        amount_minor=200_000, merchant="Amazon Fresh", transaction_date=date(2026, 8, 10),
    ))

    assert len(calls) == 1
    assert receipt.decision == "MATCHED"
    assert receipt.transaction_id == bank.transaction_id
    assert receipt.signals["ai_assisted"]["same_transaction"] is True
    assert receipt.signals["ai_assisted"]["deterministic_score"] < 0.82
    assert db.scalar(select(func.count(Transaction.id))) == 1


def test_ai_cannot_auto_merge_ambiguous_candidates(db, monkeypatch):
    user = default_user(db)
    _manual_toit(db, user, date(2026, 8, 10))
    _manual_toit(db, user, date(2026, 8, 10))
    calls = []

    def advise(*args):
        calls.append(args)
        return AIAssistedMatch(same_transaction=True, confidence=1.0, reason="Top candidate looks similar.")

    monkeypatch.setattr(reconciliation_service, "evaluate_reconciliation_match", advise)
    result = ingest_observation(db, user.id, ObservationIn(
        source_type="email", source_message_id="ai-ambiguous", transaction_type="expense",
        amount_minor=200_000, merchant="Toit", transaction_date=date(2026, 8, 10),
    ))

    assert len(calls) == 1
    assert result.decision == "NEEDS_REVIEW"
    assert result.transaction_id is None
    assert result.signals["ai_assisted"]["same_transaction"] is True
    assert db.scalar(select(func.count(Transaction.id))) == 2


def test_ai_failure_falls_back_to_human_review(db, monkeypatch):
    user = default_user(db)
    ingest_observation(db, user.id, ObservationIn(
        source_type="bank", external_transaction_id="failure-bank", transaction_type="expense",
        amount_minor=200_000, merchant="Amazon", transaction_date=date(2026, 8, 10),
    ))
    monkeypatch.setattr(
        reconciliation_service,
        "evaluate_reconciliation_match",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("model unavailable")),
    )

    result = ingest_observation(db, user.id, ObservationIn(
        source_type="receipt", source_message_id="failure-receipt", transaction_type="expense",
        amount_minor=200_000, merchant="Amazon Fresh", transaction_date=date(2026, 8, 10),
    ))

    assert result.decision == "NEEDS_REVIEW"
    assert result.transaction_id is None


def test_refund_does_not_merge_with_debit(db):
    user = default_user(db)
    debit = ingest_observation(db, user.id, ObservationIn(source_type="bank", external_transaction_id="debit", transaction_type="expense", amount_minor=200_000, merchant="Toit", transaction_date=date(2026, 8, 10)))
    refund = ingest_observation(db, user.id, ObservationIn(source_type="bank", external_transaction_id="refund", transaction_type="refund", amount_minor=200_000, merchant="Toit", transaction_date=date(2026, 8, 10)))
    assert debit.transaction_id != refund.transaction_id


def test_ambiguous_match_is_reviewed_and_user_can_keep_separate(db):
    user = default_user(db)
    _manual_toit(db, user, date(2026, 8, 10))
    _manual_toit(db, user, date(2026, 8, 10))
    result = ingest_observation(db, user.id, ObservationIn(source_type="email", source_message_id="ambiguous", transaction_type="expense", amount_minor=200_000, merchant="Toit", transaction_date=date(2026, 8, 10)))
    assert result.decision == "NEEDS_REVIEW"
    conversation = get_or_create_conversation(db, user)
    response = handle_chat(db, user, conversation, "Show me duplicate transactions that need review")
    assert response.widgets[0].type == "reconciliation_review"
    candidate_id = response.widgets[0].data["candidateId"]
    response = handle_action(db, user, conversation, "separate_reconciliation", {"candidateId": candidate_id})
    assert "separate" in response.message.lower()
    assert db.scalar(select(func.count(Transaction.id))) == 3


def test_conflicting_merchant_observations_are_preserved_then_resolved_by_authority(db):
    user = default_user(db)
    bank = ingest_observation(db, user.id, ObservationIn(source_type="bank", external_transaction_id="amazon-bank", transaction_type="expense", amount_minor=200_000, merchant="Amazon", transaction_date=date(2026, 8, 10)))
    receipt = ingest_observation(db, user.id, ObservationIn(source_type="receipt", source_message_id="amazon-receipt", transaction_type="expense", amount_minor=200_000, merchant="Amazon Fresh", transaction_date=date(2026, 8, 10)))
    assert receipt.decision == "NEEDS_REVIEW"
    candidate = db.scalar(select(ReconciliationCandidate).where(ReconciliationCandidate.observation_id == receipt.observation_id))
    transaction = resolve_reconciliation(db, user.id, candidate.id, "same_transaction")
    assert transaction.id == bank.transaction_id
    assert transaction.merchant_name == "Amazon Fresh"
    assert {source.source_type for source in transaction.sources} == {"bank", "receipt"}


def test_source_authority_is_configurable(db):
    user = default_user(db)
    config = ReconciliationConfig(source_authority={**ReconciliationConfig().source_authority, "sms": 0})
    result = ingest_observation(db, user.id, ObservationIn(source_type="sms", source_message_id="low-authority", transaction_type="expense", amount_minor=90_000, merchant="Cafe", transaction_date=date(2026, 8, 10)), config)
    assert db.get(Transaction, result.transaction_id).status == "provisional"
