from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, TypedDict
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from ..domain import SpendNature, TransactionStatus, TransactionType
from ..event_time import as_utc, now_utc
from .geocoding import cached_label
from .preferences import user_preference
from ..models import Transaction, TransactionFieldValue, TransactionSource
from ..taxonomy_catalog import DefaultCategorySlug, TRANSACTION_CATEGORY_ROOTS, category_slug_matches_transaction_type
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


def transaction_log(user_id: UUID) -> Select[tuple[Transaction]]:
    """One user's full transaction log, soft-deleted rows included.

    Only for audit-style listings that render removed records explicitly;
    every calculation must keep using the canonical scope.
    """
    return select(Transaction).where(Transaction.user_id == user_id)


def remove_transaction(
    db: Session,
    user_id: UUID,
    transaction_id: UUID,
) -> Transaction:
    """Soft-delete one active transaction.

    The same tombstone the conversational removal writes: the record stays in
    the log as a struck-off entry and leaves every calculation. Removing a
    record that is already removed (or not yours) is a refusal, so a double
    click cannot look like two successful deletes.
    """
    transaction = active_transaction(db, user_id, transaction_id)
    if transaction is None:
        raise ValueError("Unknown transaction")
    transaction.deleted_at = now_utc()
    db.flush()
    return transaction


def restore_transaction(
    db: Session,
    user_id: UUID,
    transaction_id: UUID,
) -> Transaction:
    """Bring a soft-deleted transaction back into the canonical records.

    The inverse of a removal: clearing the tombstone is all it takes, because
    every calculation reads through the canonical scope. Restoring something
    that is not removed (or not yours) is refused rather than silently OK'd,
    so the client can tell a stale row from a successful restore.
    """
    transaction = db.scalar(
        transaction_log(user_id).where(
            Transaction.id == transaction_id,
            Transaction.deleted_at.is_not(None),
        )
    )
    if transaction is None:
        raise ValueError("Unknown transaction")
    transaction.deleted_at = None
    db.flush()
    return transaction


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


def _spend_nature_or_unknown(value: object | None) -> SpendNature:
    """Normalize optional transport values without weakening enum validation.

    Older persisted chat widgets can submit a present field as JSON ``null``.
    Treat null and common null-like strings as the absence they represent, but
    continue to reject any other value that is not part of ``SpendNature``.
    """
    if value is None:
        return SpendNature.UNKNOWN
    raw = str(value).strip()
    if not raw or raw.casefold() in {"none", "null", "undefined"}:
        return SpendNature.UNKNOWN
    return SpendNature(raw)


def canonical_transaction_classification(
    db: Session,
    user_id: UUID,
    transaction_type: TransactionType | str,
    category_id: UUID | str | None,
    subcategory_id: UUID | str | None,
    spend_nature: SpendNature | str | None,
) -> tuple[UUID | None, UUID | None, SpendNature]:
    """Return the only category/nature combination valid for a direction.

    This is the write invariant shared by chat drafts, manual API edits and
    imported/reconciled observations. Model output and client payloads are
    proposals; a canonical transaction never persists an expense category on
    income, an income category on an expense, or spend nature on a non-expense.
    """
    kind = TransactionType(str(transaction_type))
    taxonomy = TaxonomyRepository(db, user_id)
    supplied_category = taxonomy.category(UUID(str(category_id))) if category_id else None
    supplied_subcategory = taxonomy.subcategory(UUID(str(subcategory_id))) if subcategory_id else None

    if kind is TransactionType.EXPENSE:
        category = supplied_category if supplied_category and category_slug_matches_transaction_type(kind, supplied_category.slug) else taxonomy.category_by_slug(DefaultCategorySlug.OTHER, expense_only=True)
    elif kind in TRANSACTION_CATEGORY_ROOTS:
        category = taxonomy.category_by_slug(TRANSACTION_CATEGORY_ROOTS[kind])
    else:
        return None, None, SpendNature.UNKNOWN

    if category is None:
        return None, None, _spend_nature_or_unknown(spend_nature) if kind is TransactionType.EXPENSE else SpendNature.UNKNOWN

    subcategory = supplied_subcategory if supplied_subcategory and supplied_subcategory.category_id == category.id else None
    # When the root changes, preserve a meaningful leaf if the destination root
    # knows the same slug (for example Other), otherwise use its explicit Other
    # leaf. This avoids both cross-parent IDs and unlabeled category records.
    if subcategory is None and supplied_subcategory is not None:
        subcategory = taxonomy.subcategory_by_slug(category.id, supplied_subcategory.slug)
    if subcategory is None:
        subcategory = taxonomy.subcategory_by_slug(category.id, "other")

    nature = _spend_nature_or_unknown(spend_nature) if kind is TransactionType.EXPENSE else SpendNature.UNKNOWN
    return category.id, subcategory.id if subcategory else None, nature


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
    kind = TransactionType(str(values["transaction_type"]))
    category_id, subcategory_id, spend_nature = canonical_transaction_classification(
        db,
        UUID(str(values["user_id"])),
        kind,
        values.get("category_id"),
        values.get("subcategory_id"),
        values.get("spend_nature", SpendNature.UNKNOWN),
    )
    values.update(
        transaction_type=kind.value,
        category_id=category_id,
        subcategory_id=subcategory_id,
        spend_nature=spend_nature.value,
    )
    values.setdefault("status", TransactionStatus.PROVISIONAL)
    transaction = Transaction(**values)
    db.add(transaction)
    db.flush()
    return transaction


UNSET = object()


def _taxonomy_path(db: Session, user_id: UUID, category_id: object | None, subcategory_id: object | None):
    taxonomy = TaxonomyRepository(db, user_id)
    category = taxonomy.category(UUID(str(category_id)), expense_only=True) if category_id else None
    if category_id and not category:
        raise ValueError("Unknown category")
    subcategory = taxonomy.subcategory(UUID(str(subcategory_id)), category_id=category.id) if subcategory_id and category else None
    if subcategory_id and not subcategory:
        raise ValueError("Unknown subcategory")
    return category, subcategory


def _canonicalize_merchant(db: Session, user_id: UUID, transaction: Transaction) -> None:
    merchant_name = transaction.merchant_name
    normalized = normalize_merchant(merchant_name)
    if not normalized:
        transaction.merchant_id = None
        return
    assert merchant_name is not None
    canonical = MerchantRepository(db, user_id).get_or_create(merchant_name, normalized)
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


class DeviceFix(TypedDict):
    latitude: Decimal
    longitude: Decimal
    location_accuracy: int | None


def _accepted_device_fix(
    db: Session,
    user_id: UUID,
    latitude: float | None,
    longitude: float | None,
    accuracy: int | None,
) -> DeviceFix | None:
    """The coordinates a transaction may keep, or None if it may keep none.

    The preference is read here rather than trusted from the request. A client
    is free to send coordinates it was never granted — a tab left open across a
    settings change, a replayed payload, any caller holding a session — and the
    only place that can refuse them for certain is the one doing the writing.

    Both halves are required: a latitude without a longitude locates nothing,
    and storing half a fix would make the row look located when it is not.
    """
    if latitude is None or longitude is None:
        return None
    preference = user_preference(db, user_id, "location:enabled")
    if not (preference and (preference.value or {}).get("enabled") is True):
        return None
    return {
        "latitude": Decimal(str(latitude)),
        "longitude": Decimal(str(longitude)),
        "location_accuracy": accuracy,
    }


def _typed_location_source(label: str | None) -> str | None:
    """Provenance for a row carrying a typed label and no coordinates."""
    return "user" if label and label.strip() else None


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
    latitude: float | None = None,
    longitude: float | None = None,
    location_accuracy: int | None = None,
) -> Transaction:
    """Create a confirmed user-entered transaction through canonical services."""
    if amount_minor <= 0:
        raise ValueError("Transaction amount must be greater than zero")
    kind = TransactionType(str(transaction_type))
    category, subcategory = _taxonomy_path(db, user_id, category_id, subcategory_id) if kind is TransactionType.EXPENSE else (None, None)
    label = str(location or "").strip()[:160] or None
    fix = _accepted_device_fix(db, user_id, latitude, longitude, location_accuracy)
    fix_values: dict[str, object] = {**fix} if fix else {}
    if fix and not label:
        # Only what is already known for this cell. A name nobody has looked up
        # yet is filled in afterwards, because a save must not wait on a third
        # party — see services/geocoding.
        label = cached_label(db, float(fix["latitude"]), float(fix["longitude"]))
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
        location_label=label,
        **fix_values,
        # A fix outranks a typed label: the coordinates are the stronger claim
        # about where this happened, whatever the person calls the place.
        location_source="device" if fix else _typed_location_source(label),
        spend_nature=_spend_nature_or_unknown(spend_nature).value,
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
    latitude: float | None = None,
    longitude: float | None = None,
    location_accuracy: int | None = None,
    tags: object = UNSET,
) -> Transaction:
    """Apply a user correction through one canonical transaction boundary.

    Chat widgets and the standalone Transactions page both call here. The
    service owns tenant scoping, taxonomy validation, merchant identity, tags,
    and provenance; HTTP and conversation handlers only adapt their payloads.

    Coordinates are the one field group without an UNSET sentinel, because they
    have no "clear" gesture to express: a save either carries a fresh fix or it
    does not, and one that does not leaves any stored fix where it is. Re-saving
    an edit from a device that has since lost permission must not erase where
    the transaction actually happened.
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
        changed_fields["location"] = transaction.location_label
        # Renaming the place does not demote a stored fix to a typed one. The
        # coordinates still say where this happened; only their label changed.
        if transaction.latitude is None or transaction.longitude is None:
            transaction.location_source = _typed_location_source(transaction.location_label)
    fix = _accepted_device_fix(db, user_id, latitude, longitude, location_accuracy)
    if fix:
        transaction.latitude = fix["latitude"]
        transaction.longitude = fix["longitude"]
        transaction.location_accuracy = fix["location_accuracy"]
        transaction.location_source = "device"
        # The provenance log records that a fix arrived, never the fix itself:
        # copying coordinates into a second table doubles what one leak costs.
        changed_fields["location_source"] = "device"
    if spend_nature is not UNSET:
        transaction.spend_nature = _spend_nature_or_unknown(spend_nature).value
        changed_fields["spend_nature"] = transaction.spend_nature

    kind = TransactionType(transaction.transaction_type)
    # Category payloads are meaningful only for expenses. Other directions are
    # normalized from their type below, so a hidden stale form value can never
    # preserve the prior expense taxonomy after a type change.
    if kind is TransactionType.EXPENSE:
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

    canonical_category_id, canonical_subcategory_id, canonical_nature = canonical_transaction_classification(
        db,
        user_id,
        kind,
        transaction.category_id,
        transaction.subcategory_id,
        transaction.spend_nature,
    )
    if transaction.category_id != canonical_category_id:
        transaction.category_id = canonical_category_id
        changed_fields["category_id"] = str(canonical_category_id) if canonical_category_id else None
    if transaction.subcategory_id != canonical_subcategory_id:
        transaction.subcategory_id = canonical_subcategory_id
        changed_fields["subcategory_id"] = str(canonical_subcategory_id) if canonical_subcategory_id else None
    if transaction.spend_nature != canonical_nature.value:
        transaction.spend_nature = canonical_nature.value
        changed_fields["spend_nature"] = canonical_nature.value
    if tags is not UNSET:
        raw_tags: list[object] = (
            [item for item in tags]
            if isinstance(tags, list)
            else [item for item in str(tags or "").split(",")]
        )
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
