from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from ..domain import SpendNature, TransactionStatus, TransactionType
from ..event_time import as_utc
from ..models import Transaction, TransactionFieldValue, TransactionSource
from .extraction import normalize_merchant
from .merchants import MerchantRepository
from .tags import TagRepository
from .taxonomy import TaxonomyRepository


_SYSTEM_MANAGED_FIELDS = frozenset({"id", "created_at", "updated_at", "deleted_at"})
_CREATE_FIELDS = frozenset(
    column.key for column in Transaction.__table__.columns
    if column.key not in _SYSTEM_MANAGED_FIELDS
)
_REQUIRED_CREATE_FIELDS = frozenset(
    column.key for column in Transaction.__table__.columns
    if column.key in _CREATE_FIELDS
    and not column.nullable
    and column.default is None
    and column.server_default is None
)


def apply_canonical_transaction_scope(
    statement: Select,
    user_id: UUID,
    *,
    currency: str | None = None,
) -> Select:
    """Apply the canonical tenant and soft-delete boundary for transactions."""
    conditions = [
        Transaction.user_id == user_id,
        Transaction.deleted_at.is_(None),
    ]
    if currency is not None:
        conditions.append(Transaction.currency == currency)
    return statement.where(*conditions)


def canonical_transactions(
    user_id: UUID,
    *,
    currency: str | None = None,
) -> Select[tuple[Transaction]]:
    """Start a query containing only one user's active canonical records."""
    return apply_canonical_transaction_scope(
        select(Transaction),
        user_id,
        currency=currency,
    )


def apply_expense_transaction_scope(
    statement: Select,
    user_id: UUID,
    *,
    currency: str | None = None,
) -> Select:
    """Apply the canonical active-expense boundary to an arbitrary query."""
    return apply_canonical_transaction_scope(
        statement,
        user_id,
        currency=currency,
    ).where(Transaction.transaction_type == TransactionType.EXPENSE)


def expense_transactions(
    user_id: UUID,
    *,
    currency: str | None = None,
) -> Select[tuple[Transaction]]:
    return apply_expense_transaction_scope(
        select(Transaction),
        user_id,
        currency=currency,
    )


def active_transaction(
    db: Session,
    user_id: UUID,
    transaction_id: UUID,
) -> Transaction | None:
    """Resolve one transaction through the canonical tenant/delete scope."""
    return db.scalar(
        canonical_transactions(user_id).where(Transaction.id == transaction_id)
    )


def owned_transaction_source(
    db: Session,
    user_id: UUID,
    *conditions,
) -> TransactionSource | None:
    """Resolve provenance only through the owning canonical transaction."""
    return db.scalar(
        select(TransactionSource)
        .join(Transaction, Transaction.id == TransactionSource.transaction_id)
        .where(Transaction.user_id == user_id, *conditions)
    )


def create_transaction(db: Session, /, **values: Any) -> Transaction:
    """Bind source-specific values to the canonical ORM record and persist it.

    Writable and required fields are derived from the mapped table so adding a
    column does not require a second manually maintained constructor contract.
    """
    unknown = set(values) - _CREATE_FIELDS
    if unknown:
        raise ValueError(f"Unsupported transaction fields: {', '.join(sorted(unknown))}")
    missing = _REQUIRED_CREATE_FIELDS - set(values)
    if missing:
        raise ValueError(f"Missing transaction fields: {', '.join(sorted(missing))}")
    values.setdefault("status", TransactionStatus.PROVISIONAL)
    transaction = Transaction(**values)
    db.add(transaction)
    db.flush()
    return transaction


UNSET = object()


def _taxonomy_path(db: Session, user_id: UUID, category_id: UUID | str | None, subcategory_id: UUID | str | None):
    taxonomy = TaxonomyRepository(db, user_id)
    category = taxonomy.category(UUID(str(category_id)), expense_only=True) if category_id else None
    if category_id and not category:
        raise ValueError("Unknown category")
    subcategory = taxonomy.subcategory(UUID(str(subcategory_id)), category_id=category.id) if subcategory_id and category else None
    if subcategory_id and not subcategory:
        raise ValueError("Unknown subcategory")
    return category, subcategory


def _canonicalize_merchant(db: Session, user_id: UUID, transaction: Transaction) -> None:
    normalized = normalize_merchant(transaction.merchant_name)
    if not normalized:
        transaction.merchant_id = None
        return
    canonical = MerchantRepository(db, user_id).get_or_create(transaction.merchant_name, normalized)
    transaction.merchant_id = canonical.id
    transaction.merchant_name = canonical.canonical_name


def _record_user_values(db: Session, transaction_id: UUID, values: dict[str, object], *, origin: str) -> None:
    db.add_all([
        TransactionFieldValue(
            transaction_id=transaction_id,
            field_name=field_name,
            value={"value": value},
            origin=origin,
            confidence=Decimal("1"),
            user_confirmed=True,
        )
        for field_name, value in values.items()
    ])


def create_manual_transaction(
    db: Session,
    user_id: UUID,
    *,
    currency: str,
    amount_minor: int,
    merchant: str | None,
    transaction_at: datetime,
    transaction_type: str | TransactionType,
    category_id: UUID | str | None,
    subcategory_id: UUID | str | None,
    spend_nature: str | SpendNature,
    location: str | None,
) -> Transaction:
    """Create a confirmed user-entered transaction through canonical services."""
    if amount_minor <= 0:
        raise ValueError("Transaction amount must be greater than zero")
    kind = TransactionType(str(transaction_type))
    category, subcategory = _taxonomy_path(db, user_id, category_id, subcategory_id) if kind is TransactionType.EXPENSE else (None, None)
    transaction = create_transaction(
        db,
        user_id=user_id,
        transaction_type=kind.value,
        amount_minor=amount_minor,
        currency=currency,
        merchant_name=str(merchant or "").strip()[:160] or None,
        category_id=category.id if category else None,
        subcategory_id=subcategory.id if subcategory else None,
        transaction_at=as_utc(transaction_at),
        posted_at=as_utc(transaction_at),
        location_label=str(location or "").strip()[:160] or None,
        location_source="user" if location and location.strip() else None,
        spend_nature=SpendNature(str(spend_nature)).value,
        status=TransactionStatus.CONFIRMED.value,
    )
    _canonicalize_merchant(db, user_id, transaction)
    _record_user_values(db, transaction.id, {
        "amount_minor": transaction.amount_minor,
        "merchant": transaction.merchant_name,
        "transaction_at": transaction.transaction_at.isoformat(),
        "transaction_type": transaction.transaction_type,
        "category_id": str(transaction.category_id) if transaction.category_id else None,
        "subcategory_id": str(transaction.subcategory_id) if transaction.subcategory_id else None,
        "spend_nature": transaction.spend_nature,
        "location": transaction.location_label,
    }, origin="manual_entry")
    db.flush()
    return transaction


def update_saved_transaction(
    db: Session,
    user_id: UUID,
    transaction_id: UUID,
    *,
    amount_minor: int,
    merchant: object = UNSET,
    transaction_at: datetime | object = UNSET,
    transaction_type: str | TransactionType | object = UNSET,
    category_id: UUID | str | None | object = UNSET,
    subcategory_id: UUID | str | None | object = UNSET,
    spend_nature: str | SpendNature | object = UNSET,
    location: object = UNSET,
    tags: object = UNSET,
) -> Transaction:
    """Apply a user correction through one canonical transaction boundary.

    Chat widgets and the standalone Transactions page both call here. The
    service owns tenant scoping, taxonomy validation, merchant identity, tags,
    and provenance; HTTP and conversation handlers only adapt their payloads.
    """
    transaction = active_transaction(db, user_id, transaction_id)
    if not transaction:
        raise ValueError("Unknown transaction")
    if amount_minor <= 0:
        raise ValueError("Transaction amount must be greater than zero")

    changed_fields: dict[str, object] = {"amount_minor": amount_minor}
    transaction.amount_minor = amount_minor
    if merchant is not UNSET:
        transaction.merchant_name = str(merchant or "").strip()[:160] or None
        changed_fields["merchant"] = transaction.merchant_name
    if transaction_at is not UNSET:
        if not isinstance(transaction_at, datetime):
            raise ValueError("Transaction date and time are required")
        transaction.transaction_at = as_utc(transaction_at)
        changed_fields["transaction_at"] = transaction.transaction_at.isoformat()
    if transaction_type is not UNSET:
        transaction.transaction_type = TransactionType(str(transaction_type)).value
        changed_fields["transaction_type"] = transaction.transaction_type
    if location is not UNSET:
        transaction.location_label = str(location or "").strip()[:160] or None
        transaction.location_source = "user" if transaction.location_label else None
        changed_fields["location"] = transaction.location_label
    if spend_nature is not UNSET:
        transaction.spend_nature = SpendNature(str(spend_nature)).value
        changed_fields["spend_nature"] = transaction.spend_nature

    if category_id is not UNSET:
        category, subcategory = _taxonomy_path(db, user_id, category_id, None if subcategory_id is UNSET else subcategory_id)
        transaction.category_id = category.id if category else None
        transaction.subcategory_id = subcategory.id if subcategory else None
        changed_fields["category_id"] = str(category.id) if category else None
        if subcategory_id is not UNSET:
            changed_fields["subcategory_id"] = str(subcategory.id) if subcategory else None
    elif subcategory_id is not UNSET:
        _category, subcategory = _taxonomy_path(db, user_id, transaction.category_id, subcategory_id)
        transaction.subcategory_id = subcategory.id if subcategory else None
        changed_fields["subcategory_id"] = str(subcategory.id) if subcategory else None
    if tags is not UNSET:
        raw_tags = tags if isinstance(tags, list) else str(tags or "").split(",")
        changed_fields["tags"] = TagRepository(db, user_id).replace_transaction_tags(
            transaction.id,
            raw_tags,
            source="user",
            confidence=Decimal("1"),
        )

    _canonicalize_merchant(db, user_id, transaction)
    if "merchant" in changed_fields:
        changed_fields["merchant"] = transaction.merchant_name
    _record_user_values(db, transaction.id, changed_fields, origin="user_correction")
    db.flush()
    return transaction
