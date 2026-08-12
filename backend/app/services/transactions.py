from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from ..domain import TransactionStatus, TransactionType
from ..models import Transaction, TransactionSource


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
