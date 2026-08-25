from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Table, delete, inspect, select, update
from sqlalchemy.orm import Session

from ..database import Base
from ..event_time import now_utc
from ..models import (
    AIAction,
    Account,
    AccountBalanceSnapshot,
    AgentEvent,
    AgentInterrupt,
    AgentRun,
    AnalysisToolRun,
    AnalysisToolTemplate,
    AuditLog,
    Budget,
    Category,
    Conversation,
    Dashboard,
    DashboardTile,
    DataSource,
    EntityLink,
    FinancialInsight,
    FinancialObservation,
    Goal,
    GoalContribution,
    Import,
    ImportRecord,
    InvestmentHolding,
    InvestmentValuationSnapshot,
    Loan,
    LoanCashflow,
    LoanReminder,
    LoanScenario,
    LoanSecurityItem,
    LoanTermVersion,
    Message,
    Merchant,
    MerchantAlias,
    OtpChallenge,
    ReconciliationCandidate,
    ReconciliationDecision,
    RecurringTransaction,
    SavedAnalysis,
    SourceAnnotation,
    SourceManifest,
    SourceRecord,
    SharedDocument,
    SharedRecord,
    SharedRecordEvent,
    SharedRecordInvitation,
    SharedRecordParticipant,
    DocumentAcceptance,
    DocumentChange,
    DocumentRevision,
    NotificationOutbox,
    PersonalLoanAgreement,
    CommandReceipt,
    Subcategory,
    Subscription,
    Tag,
    Transaction,
    TransactionCategoryHint,
    TransactionDraft,
    TransactionFieldValue,
    TransactionRevision,
    TransactionSource,
    TransactionTag,
    User,
    UserIdentity,
    UserPreference,
    UserAnalysisTool,
    UserSession,
    UserTrait,
)
from .user_memory import clear_user_memories, export_user_memories


def _camel_table_name(table_name: str) -> str:
    head, *tail = table_name.split("_")
    return head + "".join(item.title() for item in tail)


@dataclass(frozen=True)
class OwnedDataSpec:
    model: type[Base]
    owner_column: str = "user_id"
    export_key: str | None = None
    # Credentials are deleted with the account but never written into an export
    # file. A session digest or a one-time-code hash is security material for
    # protecting the account, not a record of anything the account holder did.
    exportable: bool = True
    # Columns whose values are connection/credential material stored alongside
    # otherwise-exportable rows (for example DataSource.config holding an
    # external database url). They are dropped from the export entirely.
    redacted_columns: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        table = cast(Table, inspect(self.model).local_table)
        return self.export_key or _camel_table_name(table.name)


@dataclass(frozen=True)
class DependentDataSpec:
    model: type[Base]
    parent_model: type[Base]
    parent_column: str

    @property
    def key(self) -> str:
        table = cast(Table, inspect(self.model).local_table)
        return _camel_table_name(table.name)


# This order is also a safe explicit deletion order when a database does not
# enforce every ON DELETE action (for example, a lightweight test database).
OWNED_USER_DATA: tuple[OwnedDataSpec, ...] = (
    OwnedDataSpec(AgentRun),
    OwnedDataSpec(AnalysisToolRun),
    OwnedDataSpec(AIAction),
    OwnedDataSpec(DashboardTile),
    OwnedDataSpec(Dashboard),
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
    OwnedDataSpec(SourceRecord),
    OwnedDataSpec(SourceAnnotation),
    # Deleted before DataSource: source_id is SET NULL, but this tuple doubles
    # as the explicit order for databases that do not enforce every ON DELETE.
    OwnedDataSpec(EntityLink),
    # config may hold an external connection url with embedded credentials —
    # security material for reaching the source, not a record of user activity.
    OwnedDataSpec(DataSource, redacted_columns=("config",)),
    OwnedDataSpec(TransactionDraft),
    OwnedDataSpec(Transaction),
    OwnedDataSpec(FinancialObservation, export_key="observations"),
    OwnedDataSpec(UserAnalysisTool),
    OwnedDataSpec(SavedAnalysis),
    OwnedDataSpec(FinancialInsight),
    OwnedDataSpec(UserPreference, export_key="preferences"),
    OwnedDataSpec(UserTrait),
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
    DependentDataSpec(AgentInterrupt, AgentRun, "run_id"),
    DependentDataSpec(AgentEvent, AgentRun, "run_id"),
    DependentDataSpec(ReconciliationDecision, ReconciliationCandidate, "candidate_id"),
    DependentDataSpec(ImportRecord, Import, "import_id"),
    DependentDataSpec(TransactionFieldValue, Transaction, "transaction_id"),
    DependentDataSpec(TransactionRevision, Transaction, "transaction_id"),
    DependentDataSpec(TransactionTag, Transaction, "transaction_id"),
    DependentDataSpec(TransactionSource, Transaction, "transaction_id"),
    DependentDataSpec(Message, Conversation, "conversation_id"),
    DependentDataSpec(MerchantAlias, Merchant, "merchant_id"),
    DependentDataSpec(SourceManifest, DataSource, "data_source_id"),
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
    keys = [item.key for item in OWNED_USER_DATA]
    keys.extend(item.key for item in DEPENDENT_USER_DATA)
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


def serialize_rows(rows: list[Any], *, redacted_columns: tuple[str, ...] = ()) -> list[dict]:
    serialized: list[dict] = []
    for row in rows:
        mapper = row.__mapper__
        table = cast(Table, row.__table__)
        serialized.append(
            {
                # Keep stable database-column keys in the export, but resolve
                # values through the ORM attribute. These differ when a model
                # deliberately avoids a reserved SQLAlchemy name, such as
                # AgentInterrupt.metadata -> metadata_payload.
                column.name: _json_value(
                    getattr(row, mapper.get_property_by_column(column).key)
                )
                for column in table.columns
                if column.name not in redacted_columns
            }
        )
    return serialized


def _owned_rows(db: Session, user_id: UUID) -> dict[type[Base], list[Base]]:
    return {
        spec.model: list(db.scalars(
            select(spec.model).where(getattr(spec.model, spec.owner_column) == user_id)
        ))
        for spec in OWNED_USER_DATA
    }


def export_user_data(db: Session, user: User) -> dict[str, Any]:
    owned = _owned_rows(db, user.id)
    payload: dict[str, Any] = {"user": serialize_rows([user])[0]}
    for owned_spec in OWNED_USER_DATA:
        if not owned_spec.exportable:
            continue
        payload[owned_spec.key] = serialize_rows(
            owned[owned_spec.model], redacted_columns=owned_spec.redacted_columns
        )
    for dependent_spec in DEPENDENT_USER_DATA:
        parent_ids = [cast(UUID, getattr(item, "id")) for item in owned[dependent_spec.parent_model]]
        rows: list[Base] = list(db.scalars(
            select(dependent_spec.model).where(getattr(dependent_spec.model, dependent_spec.parent_column).in_(parent_ids))
        )) if parent_ids else []
        payload[dependent_spec.key] = serialize_rows(rows)
    payload["userMemories"] = export_user_memories(user.id)
    participant_rows = list(db.scalars(select(SharedRecordParticipant).where(
        SharedRecordParticipant.member_user_id == user.id
    )))
    record_ids = [row.shared_record_id for row in participant_rows]
    if record_ids:
        records = list(db.scalars(select(SharedRecord).where(SharedRecord.id.in_(record_ids))))
        documents = list(db.scalars(select(SharedDocument).where(SharedDocument.shared_record_id.in_(record_ids))))
        document_ids = [row.id for row in documents]
        revisions = list(db.scalars(select(DocumentRevision).where(
            DocumentRevision.document_id.in_(document_ids)
        ))) if document_ids else []
        revision_ids = [row.id for row in revisions]
        agreements = list(db.scalars(select(PersonalLoanAgreement).where(
            PersonalLoanAgreement.shared_record_id.in_(record_ids)
        )))
        agreement_ids = [row.id for row in agreements]
        payload["sharedRecords"] = serialize_rows(records)
        payload["sharedRecordParticipants"] = serialize_rows(list(db.scalars(
            select(SharedRecordParticipant).where(SharedRecordParticipant.shared_record_id.in_(record_ids))
        )))
        payload["sharedRecordInvitations"] = serialize_rows(
            list(db.scalars(select(SharedRecordInvitation).where(SharedRecordInvitation.shared_record_id.in_(record_ids)))),
            redacted_columns=("destination_hash", "destination_ciphertext", "token_hash"),
        )
        payload["sharedDocuments"] = serialize_rows(documents)
        payload["documentRevisions"] = serialize_rows(revisions)
        payload["documentChanges"] = serialize_rows(list(db.scalars(select(DocumentChange).where(
            DocumentChange.revision_id.in_(revision_ids)
        )))) if revision_ids else []
        payload["documentAcceptances"] = serialize_rows(list(db.scalars(select(DocumentAcceptance).where(
            DocumentAcceptance.revision_id.in_(revision_ids)
        )))) if revision_ids else []
        payload["personalLoanAgreements"] = serialize_rows(agreements)
        for key, model in (
            ("loanTermVersions", LoanTermVersion),
            ("loanSecurityItems", LoanSecurityItem),
            ("loanCashflows", LoanCashflow),
            ("loanReminders", LoanReminder),
        ):
            payload[key] = serialize_rows(list(db.scalars(select(model).where(
                model.agreement_id.in_(agreement_ids)
            )))) if agreement_ids else []
        payload["sharedRecordEvents"] = serialize_rows(list(db.scalars(select(SharedRecordEvent).where(
            SharedRecordEvent.shared_record_id.in_(record_ids)
        ))))
        payload["notificationOutbox"] = serialize_rows(
            list(db.scalars(select(NotificationOutbox).where(NotificationOutbox.shared_record_id.in_(record_ids)))),
            redacted_columns=("destination_ciphertext", "context_ciphertext"),
        )
    else:
        for key in (
            "sharedRecords", "sharedRecordParticipants", "sharedRecordInvitations",
            "sharedDocuments", "documentRevisions", "documentChanges",
            "documentAcceptances", "personalLoanAgreements", "loanTermVersions",
            "loanSecurityItems", "loanCashflows", "loanReminders",
            "sharedRecordEvents", "notificationOutbox",
        ):
            payload[key] = []
    return payload


def delete_user_data(db: Session, user: User) -> int:
    """Delete all registered relational data and Agno memory for one user."""
    deleted_memories = clear_user_memories(user.id)
    participants = list(db.scalars(select(SharedRecordParticipant).where(
        SharedRecordParticipant.member_user_id == user.id
    )))
    participant_ids = [item.id for item in participants]
    record_ids = [item.shared_record_id for item in participants]
    if participant_ids:
        # A jointly acknowledged record remains available to the other person,
        # but account ownership, delivery endpoints, and authentication audit
        # links are detached explicitly for SQLite as well as PostgreSQL.
        db.execute(delete(NotificationOutbox).where(
            NotificationOutbox.recipient_participant_id.in_(participant_ids)
        ))
        db.execute(delete(SharedRecordInvitation).where(
            SharedRecordInvitation.participant_id.in_(participant_ids)
        ))
        db.execute(update(SharedRecordParticipant).where(
            SharedRecordParticipant.id.in_(participant_ids)
        ).values(member_user_id=None, state="account_deleted", hidden_at=now_utc()))
    if record_ids:
        db.execute(update(SharedRecord).where(
            SharedRecord.id.in_(record_ids),
            SharedRecord.created_by_user_id == user.id,
        ).values(created_by_user_id=None))
        db.execute(update(DocumentAcceptance).where(
            DocumentAcceptance.actor_user_id == user.id
        ).values(actor_user_id=None))
        db.execute(update(CommandReceipt).where(
            CommandReceipt.actor_user_id == user.id
        ).values(actor_user_id=None))
    owned = _owned_rows(db, user.id)
    for dependent_spec in DEPENDENT_USER_DATA:
        parent_ids = [cast(UUID, getattr(item, "id")) for item in owned[dependent_spec.parent_model]]
        if parent_ids:
            db.execute(delete(dependent_spec.model).where(
                getattr(dependent_spec.model, dependent_spec.parent_column).in_(parent_ids)
            ))
    for owned_spec in OWNED_USER_DATA:
        db.execute(delete(owned_spec.model).where(
            getattr(owned_spec.model, owned_spec.owner_column) == user.id
        ))
    # Templates are shared, value-free infrastructure. Keep the reusable
    # definition but erase the optional creator audit link before deleting the
    # account, including on SQLite where SET NULL may not be enforced.
    db.execute(
        update(AnalysisToolTemplate)
        .where(AnalysisToolTemplate.created_by_user_id == user.id)
        .values(created_by_user_id=None)
    )
    db.delete(user)
    db.commit()
    return deleted_memories


validate_user_data_registry()
