from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Protocol, Sequence

from ..config import DEFAULT_CURRENCY
from ..event_time import from_local_parts, local_now, now_utc, resolve_event_time
from ..domain import FinancialSourceType, MESSAGE_SOURCE_TYPES, TransactionType
from ..models import Import as ImportJob
from ..schemas import ImportSummaryData, ObservationIn
from .extraction import extract_transaction


@dataclass
class AdaptedMessage:
    classification: str
    relevant: bool
    observation: ObservationIn | None = None
    reason: str = ""


class FinancialSourceAdapter(Protocol):
    source_type: FinancialSourceType

    def adapt(self, payload) -> Iterable[ObservationIn]: ...


def import_summary(job: ImportJob, *, idempotent_replay: bool) -> dict:
    """Serialize one import job through the shared API/widget contract."""
    return ImportSummaryData(
        import_id=job.id,
        status=job.status,
        total=job.total_records,
        high_confidence=job.high_confidence_records,
        needs_review=job.review_records,
        duplicates=job.duplicate_records,
        idempotent_replay=idempotent_replay,
    ).model_dump(mode="json", by_alias=True)


def classify_financial_message(text: str) -> tuple[str, bool, str]:
    lowered = text.lower()
    if re.search(r"\botp\b|one.?time password|verification code", lowered):
        return "otp", False, "Authentication messages are never financial observations"
    if any(token in lowered for token in ("offer", "cashback awaits", "sale", "discount", "pre-approved")):
        return "promotional", False, "Promotional message"
    if any(token in lowered for token in ("order total", "order placed", "invoice generated")) and not any(token in lowered for token in ("paid", "payment successful", "debited", "credited")):
        return "order_confirmation", False, "An order is not proof that a payment occurred"
    if "balance" in lowered and not any(token in lowered for token in ("debited", "credited", "spent", "paid")):
        return "balance_notification", False, "Balance-only message"
    if any(token in lowered for token in ("refunded", "refund of", "reversed")):
        return "refund", True, "Refund event"
    if any(token in lowered for token in ("debited", "spent using", "payment of", "payment successful", "purchase")):
        return "debit", True, "Payment event"
    if any(token in lowered for token in ("credited", "salary", "received")):
        return "credit", True, "Credit event"
    if "invoice" in lowered:
        return "invoice", False, "Invoice alone is not a settled transaction"
    return "unrelated", False, "No reliable financial event detected"


class MessageAdapter:
    def __init__(self, source_type: FinancialSourceType | str):
        source_type = FinancialSourceType(source_type)
        if source_type not in MESSAGE_SOURCE_TYPES:
            raise ValueError("Message adapter supports SMS or email")
        self.source_type = source_type

    def adapt_message(
        self,
        text: str,
        message_id: str,
        observed_at: datetime | None = None,
        timezone_name: str | None = None,
        default_currency: str = DEFAULT_CURRENCY,
    ) -> AdaptedMessage:
        classification, relevant, reason = classify_financial_message(text)
        if not relevant:
            return AdaptedMessage(classification=classification, relevant=False, reason=reason)
        current = observed_at or now_utc()
        local_current = local_now(timezone_name, current=current)
        extracted = extract_transaction(text, today=local_current.date(), default_currency=default_currency)
        transaction_type = TransactionType.REFUND if classification == "refund" else TransactionType.EXPENSE if classification == "debit" else TransactionType.INCOME
        if extracted.amount_minor is None:
            return AdaptedMessage(classification=classification, relevant=False, reason="No unambiguous amount found")
        observation = ObservationIn(
            source_type=self.source_type,
            source_message_id=message_id,
            transaction_type=transaction_type,
            amount_minor=extracted.amount_minor,
            currency=extracted.currency,
            merchant=extracted.merchant,
            transaction_at=resolve_event_time(
                day=extracted.transaction_date,
                timezone_name=timezone_name,
                current=current,
                use_current_time="transaction_date" in extracted.inferred_fields or extracted.transaction_date == local_current.date(),
            ),
            description=text,
            observed_at=observed_at,
        )
        return AdaptedMessage(classification=classification, relevant=True, observation=observation, reason=reason)


class CSVAdapter:
    source_type = FinancialSourceType.CSV
    aliases = {
        "date": ("date", "transaction date", "txn date", "value date"),
        "description": ("description", "narration", "details", "merchant"),
        "amount": ("amount", "transaction amount"),
        "debit": ("debit", "withdrawal", "debit amount"),
        "credit": ("credit", "deposit", "credit amount"),
        "currency": ("currency", "ccy"),
        "external_id": ("transaction id", "txn id", "reference", "reference number"),
    }

    @staticmethod
    def _minor(value: str) -> int:
        cleaned = re.sub(r"[^0-9.\-]", "", value or "")
        if not cleaned:
            return 0
        try:
            return abs(int((Decimal(cleaned) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
        except InvalidOperation as error:
            raise ValueError(f"Invalid amount: {value}") from error

    @staticmethod
    def _date(value: str) -> date:
        for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(value.strip(), pattern).date()
            except ValueError:
                continue
        raise ValueError(f"Unsupported date: {value}")

    def _column(self, headers: Sequence[str], logical: str) -> str | None:
        normalized = {header.strip().lower(): header for header in headers}
        return next((normalized[name] for name in self.aliases[logical] if name in normalized), None)

    def adapt(
        self,
        content: bytes,
        timezone_name: str | None = None,
        default_currency: str = DEFAULT_CURRENCY,
    ) -> list[tuple[int, ObservationIn | None, list[str]]]:
        decoded = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(decoded))
        headers = reader.fieldnames or []
        date_col = self._column(headers, "date")
        description_col = self._column(headers, "description")
        amount_col = self._column(headers, "amount")
        debit_col = self._column(headers, "debit")
        credit_col = self._column(headers, "credit")
        currency_col = self._column(headers, "currency")
        external_col = self._column(headers, "external_id")
        if not date_col or not description_col or not (amount_col or debit_col or credit_col):
            raise ValueError("CSV needs date, description, and amount/debit/credit columns")
        results: list[tuple[int, ObservationIn | None, list[str]]] = []
        file_digest = hashlib.sha256(content).hexdigest()[:16]
        for row_number, row in enumerate(reader, start=2):
            try:
                debit = self._minor(row.get(debit_col, "")) if debit_col else 0
                credit = self._minor(row.get(credit_col, "")) if credit_col else 0
                amount = self._minor(row.get(amount_col, "")) if amount_col else debit or credit
                if not amount:
                    raise ValueError("Amount is empty or zero")
                description = (row.get(description_col) or "").strip()
                external_id = (row.get(external_col) or "").strip() if external_col else f"{file_digest}:{row_number}"
                transaction_type = TransactionType.INCOME if credit and not debit else TransactionType.EXPENSE
                merchant = re.sub(r"\s+(?:UPI|POS|TXN|REF).*", "", description, flags=re.I).strip() or None
                results.append((row_number, ObservationIn(
                    source_type=self.source_type,
                    source_message_id=f"{file_digest}:{row_number}",
                    external_transaction_id=external_id,
                    transaction_type=transaction_type,
                    amount_minor=amount,
                    currency=(row.get(currency_col) or default_currency).strip().upper() if currency_col else default_currency,
                    merchant=merchant,
                    transaction_at=from_local_parts(self._date(row[date_col]), None, timezone_name),
                    description=description,
                ), []))
            except (ValueError, TypeError) as error:
                results.append((row_number, None, [str(error)]))
        return results
