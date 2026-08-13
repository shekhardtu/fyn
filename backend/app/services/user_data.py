from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..database import Base
from ..models import (
    AIAction,
    Account,
    AccountBalanceSnapshot,
    AnalysisTool,
    AnalysisToolRun,
    AuditLog,
    Budget,
    Category,
    Conversation,
    FinancialInsight,
    FinancialObservation,
    Goal,
    GoalContribution,
    Import,
    ImportRecord,
    InvestmentHolding,
    InvestmentValuationSnapshot,
    Loan,
    LoanScenario,
    Message,
    Merchant,
    MerchantAlias,
    OtpChallenge,
    ReconciliationCandidate,
    ReconciliationDecision,
    RecurringTransaction,
    SavedAnalysis,
    Subcategory,
    Subscription,
    Tag,
    Transaction,
    TransactionCategoryHint,
    TransactionDraft,
    TransactionFieldValue,
    TransactionSource,
    TransactionTag,
    User,
    UserIdentity,
    UserPreference,
    UserSession,
)
from .user_memory import clear_user_memories, export_user_memories


def _camel_table_name(table_name: str) -> str:
    head, *tail = table_name.split("_")
    return head + "".join(item.title() for item in tail)


@dataclass(frozen=True)
class OwnedDataSpec:
    model: type
    owner_column: str = "user_id"
    export_key: str | None = None
    # Credentials are deleted with the account but never written into an export
    # file. A session digest or a one-time-code hash is security material for
    # protecting the account, not a record of anything the account holder did.
    exportable: bool = True

    @property
    def key(self) -> str:
        return self.export_key or _camel_table_name(self.model.__tablename__)


@dataclass(frozen=True)
class DependentDataSpec:
    model: type
    parent_model: type
    parent_column: str

    @property
    def key(self) -> str:
        return _camel_table_name(self.model.__tablename__)


# This order is also a safe explicit deletion order when a database does not
# enforce every ON DELETE action (for example, a lightweight test database).
OWNED_USER_DATA: tuple[OwnedDataSpec, ...] = (
    OwnedDataSpec(AnalysisToolRun),
    OwnedDataSpec(AIAction),
    OwnedDataSpec(Subscription),
    OwnedDataSpec(RecurringTransaction),
    OwnedDataSpec(LoanScenario),
    OwnedDataSpec(Loan),
    OwnedDataSpec(GoalContribution),
    OwnedDataSpec(Goal),
    OwnedDataSpec(Budget),
    OwnedDataSpec(TransactionCategoryHint),
    OwnedDataSpec(ReconciliationCandidate),
    OwnedDataSpec(Import),
    OwnedDataSpec(TransactionDraft),
    OwnedDataSpec(Transaction),
    OwnedDataSpec(FinancialObservation, export_key="observations"),
    OwnedDataSpec(AnalysisTool),
    OwnedDataSpec(SavedAnalysis),
    OwnedDataSpec(FinancialInsight),
    OwnedDataSpec(UserPreference, export_key="preferences"),
    OwnedDataSpec(InvestmentValuationSnapshot),
    OwnedDataSpec(InvestmentHolding),
    OwnedDataSpec(AccountBalanceSnapshot),
    OwnedDataSpec(Account),
    OwnedDataSpec(Conversation),
    OwnedDataSpec(Tag),
    OwnedDataSpec(Merchant, owner_column="owner_user_id"),
    OwnedDataSpec(Subcategory, owner_column="owner_user_id"),
    OwnedDataSpec(Category, owner_column="owner_user_id"),
    OwnedDataSpec(AuditLog),
    # Deleting these is what releases the phone number and email address for a
    # different account to claim.
    OwnedDataSpec(UserIdentity, export_key="signInMethods"),
    OwnedDataSpec(UserSession, exportable=False),
    OwnedDataSpec(OtpChallenge, exportable=False),
)


DEPENDENT_USER_DATA: tuple[DependentDataSpec, ...] = (
    DependentDataSpec(ReconciliationDecision, ReconciliationCandidate, "candidate_id"),
    DependentDataSpec(ImportRecord, Import, "import_id"),
    DependentDataSpec(TransactionFieldValue, Transaction, "transaction_id"),
    DependentDataSpec(TransactionTag, Transaction, "transaction_id"),
    DependentDataSpec(TransactionSource, Transaction, "transaction_id"),
    DependentDataSpec(Message, Conversation, "conversation_id"),
    DependentDataSpec(MerchantAlias, Merchant, "merchant_id"),
)


def validate_user_data_registry() -> None:
    """Fail when a new user-owned SQLAlchemy model is not in the lifecycle."""
    expected = {
        mapper.class_
        for mapper in Base.registry.mappers
        if mapper.class_ is not User
        and ({"user_id", "owner_user_id"} & set(mapper.local_table.columns.keys()))
    }
    registered = {item.model for item in OWNED_USER_DATA}
    if expected != registered:
        missing = sorted(model.__name__ for model in expected - registered)
        extra = sorted(model.__name__ for model in registered - expected)
        raise RuntimeError(f"User-data registry drift; missing={missing}, extra={extra}")
    keys = [item.key for item in (*OWNED_USER_DATA, *DEPENDENT_USER_DATA)]
    if len(keys) != len(set(keys)):
        raise RuntimeError("User-data export keys must be unique")


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def serialize_rows(rows: list[Any]) -> list[dict]:
    return [
        {
            column.key: _json_value(getattr(row, column.key))
            for column in row.__table__.columns
        }
        for row in rows
    ]


def _owned_rows(db: Session, user_id: UUID) -> dict[type, list[Any]]:
    return {
        spec.model: list(db.scalars(
            select(spec.model).where(getattr(spec.model, spec.owner_column) == user_id)
        ))
        for spec in OWNED_USER_DATA
    }


def export_user_data(db: Session, user: User) -> dict[str, Any]:
    owned = _owned_rows(db, user.id)
    payload: dict[str, Any] = {"user": serialize_rows([user])[0]}
    for spec in OWNED_USER_DATA:
        if not spec.exportable:
            continue
        payload[spec.key] = serialize_rows(owned[spec.model])
    for spec in DEPENDENT_USER_DATA:
        parent_ids = [item.id for item in owned[spec.parent_model]]
        rows = list(db.scalars(
            select(spec.model).where(getattr(spec.model, spec.parent_column).in_(parent_ids))
        )) if parent_ids else []
        payload[spec.key] = serialize_rows(rows)
    payload["userMemories"] = export_user_memories(user.id)
    return payload


def delete_user_data(db: Session, user: User) -> int:
    """Delete all registered relational data and Agno memory for one user."""
    deleted_memories = clear_user_memories(user.id)
    owned = _owned_rows(db, user.id)
    for spec in DEPENDENT_USER_DATA:
        parent_ids = [item.id for item in owned[spec.parent_model]]
        if parent_ids:
            db.execute(delete(spec.model).where(
                getattr(spec.model, spec.parent_column).in_(parent_ids)
            ))
    for spec in OWNED_USER_DATA:
        db.execute(delete(spec.model).where(
            getattr(spec.model, spec.owner_column) == user.id
        ))
    db.delete(user)
    db.commit()
    return deleted_memories


validate_user_data_registry()
