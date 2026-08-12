from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select

from app.models import User
from app.schemas import ObservationIn
from app.seed import DEFAULT_USER_EMAIL
from app.services.reconciliation import ingest_observation


@dataclass(frozen=True)
class PairCase:
    name: str
    same_transaction: bool
    first: ObservationIn
    second: ObservationIn


def _observation(source: str, identity: str, amount: int, merchant: str, day: date, transaction_type: str = "expense", posted: date | None = None) -> ObservationIn:
    identity_field = {"external_transaction_id": identity} if source in {"bank", "csv"} else {"source_message_id": identity}
    return ObservationIn(source_type=source, transaction_type=transaction_type, amount_minor=amount, merchant=merchant, transaction_date=day, posted_date=posted, **identity_field)


def benchmark_cases() -> list[PairCase]:
    return [
        PairCase("exact source replay", True, _observation("bank", "exact-1", 101_000, "Cafe Noir", date(2026, 1, 1)), _observation("bank", "exact-1", 101_000, "CAFE NOIR", date(2026, 1, 1))),
        PairCase("manual-style SMS and email", True, _observation("sms", "cross-sms", 102_000, "Toit", date(2026, 1, 2)), _observation("email", "cross-email", 102_000, "TOIT", date(2026, 1, 2))),
        PairCase("bank and receipt", True, _observation("bank", "receipt-bank", 103_000, "Amazon Fresh", date(2026, 1, 3)), _observation("receipt", "receipt-1", 103_000, "Amazon Fresh", date(2026, 1, 3))),
        PairCase("pending and posted", True, _observation("bank", "pending-bank", 104_000, "Indigo", date(2026, 1, 4), posted=date(2026, 1, 5)), _observation("sms", "posted-sms", 104_000, "INDIGO", date(2026, 1, 5))),
        PairCase("different merchants same amount", False, _observation("bank", "merchant-a", 105_000, "Toit", date(2026, 1, 6)), _observation("bank", "merchant-b", 105_000, "Amazon", date(2026, 1, 6))),
        PairCase("same merchant consecutive visits", False, _observation("bank", "coffee-a", 106_000, "Starbucks", date(2026, 1, 7)), _observation("bank", "coffee-b", 106_000, "Starbucks", date(2026, 1, 8))),
        PairCase("debit and refund", False, _observation("bank", "refund-debit", 107_000, "Toit", date(2026, 1, 9)), _observation("bank", "refund-credit", 107_000, "Toit", date(2026, 1, 9), transaction_type="refund")),
        PairCase("transfer legs", False, _observation("bank", "transfer-out", 108_000, "Own transfer", date(2026, 1, 10), transaction_type="transfer"), _observation("bank", "transfer-in", 108_000, "Own transfer", date(2026, 1, 10), transaction_type="income")),
        PairCase("monthly recurring charges", False, _observation("bank", "recurring-jan", 109_000, "Netflix", date(2026, 1, 11)), _observation("bank", "recurring-feb", 109_000, "Netflix", date(2026, 2, 11))),
        PairCase("same day repeated purchases", False, _observation("bank", "repeat-a", 110_000, "Starbucks", date(2026, 1, 12)), _observation("bank", "repeat-b", 110_000, "Starbucks", date(2026, 1, 12))),
    ]


def test_reconciliation_benchmark_prioritizes_zero_false_merges(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    true_positive = false_positive = true_negative = false_negative = 0
    outcomes = []
    for case in benchmark_cases():
        first = ingest_observation(db, user.id, case.first)
        second = ingest_observation(db, user.id, case.second)
        predicted_same = first.transaction_id is not None and first.transaction_id == second.transaction_id
        outcomes.append((case.name, case.same_transaction, predicted_same, second.decision))
        if case.same_transaction and predicted_same:
            true_positive += 1
        elif case.same_transaction:
            false_negative += 1
        elif predicted_same:
            false_positive += 1
        else:
            true_negative += 1

    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    false_merge_rate = false_positive / (false_positive + true_negative)
    false_split_rate = false_negative / (false_negative + true_positive)
    report = {"precision": precision, "recall": recall, "false_merge_rate": false_merge_rate, "false_split_rate": false_split_rate, "outcomes": outcomes}

    assert false_merge_rate == 0, report
    assert precision == 1, report
    assert recall >= 0.95, report
    assert false_split_rate <= 0.05, report
