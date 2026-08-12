from __future__ import annotations

from enum import Enum
from typing import Literal


class ValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


ACTIVE_STATUS = "active"
TAXONOMY_FIELD_NAMES = ("category", "subcategory")


class TransactionType(ValueEnum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    INVESTMENT = "investment"
    LOAN_PAYMENT = "loan_payment"
    REFUND = "refund"
    REIMBURSEMENT = "reimbursement"
    CASH_WITHDRAWAL = "cash_withdrawal"
    CASH_DEPOSIT = "cash_deposit"
    UNKNOWN = "unknown"


class SpendNature(ValueEnum):
    ESSENTIAL = "essential"
    DISCRETIONARY = "discretionary"
    POTENTIALLY_AVOIDABLE = "potentially_avoidable"
    UNKNOWN = "unknown"


class TransactionStatus(ValueEnum):
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"


class TaxonomyScope(ValueEnum):
    SYSTEM = "system"
    USER = "user"


class DraftState(ValueEnum):
    RECEIVED = "RECEIVED"
    CLASSIFIED = "CLASSIFIED"
    EXTRACTED = "EXTRACTED"
    ENRICHED = "ENRICHED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    READY_FOR_CONFIRMATION = "READY_FOR_CONFIRMATION"
    USER_APPROVED = "USER_APPROVED"
    RECONCILING = "RECONCILING"
    COMMITTED = "COMMITTED"
    CANCELLED = "CANCELLED"


class WidgetActionId(ValueEnum):
    """Stable command identifiers shared by domain workflows and UI widgets."""

    SET_SPEND_NATURE = "set_spend_nature"
    START_ADD_CATEGORY = "start_add_category"
    START_ADD_SUBCATEGORY = "start_add_subcategory"
    CANCEL_ADD_CATEGORY = "cancel_add_category"
    CANCEL_TAXONOMY_CHANGE = "cancel_taxonomy_change"
    CREATE_CATEGORY = "create_category"
    CREATE_SUBCATEGORY = "create_subcategory"
    SELECT_CATEGORY = "select_category"
    SELECT_TRANSACTION_TYPE = "select_transaction_type"
    SELECT_SUBCATEGORY = "select_subcategory"
    CHANGE_CATEGORY = "change_category"
    SELECT_ACCOUNT = "select_account"
    SAVE_BUDGET = "save_budget"
    SAVE_GOAL = "save_goal"
    CONTRIBUTE_GOAL = "contribute_goal"
    COMMIT_IMPORT = "commit_import"
    CALCULATE_LOAN_SCENARIO = "calculate_loan_scenario"
    CALCULATE_INVESTMENT_SCENARIO = "calculate_investment_scenario"
    COMMIT_TRANSACTION = "commit_transaction"
    EDIT_TRANSACTION = "edit_transaction"
    UPDATE_TRANSACTION_DRAFT = "update_transaction_draft"
    EDIT_SAVED_TRANSACTION = "edit_saved_transaction"
    CANCEL_SAVED_TRANSACTION_EDIT = "cancel_saved_transaction_edit"
    UPDATE_SAVED_TRANSACTION = "update_saved_transaction"
    REQUEST_REMOVE_TRANSACTION = "request_remove_transaction"
    CONFIRM_REMOVE_TRANSACTION = "confirm_remove_transaction"
    CANCEL_REMOVE_TRANSACTION = "cancel_remove_transaction"
    MERGE_RECONCILIATION = "merge_reconciliation"
    SEPARATE_RECONCILIATION = "separate_reconciliation"


TaxonomyOperation = Literal[
    WidgetActionId.CREATE_CATEGORY,
    WidgetActionId.CREATE_SUBCATEGORY,
]


class IdentityProvider(ValueEnum):
    """A way one account can be recognised at sign-in."""

    PHONE = "phone"
    EMAIL = "email"
    GOOGLE = "google"


class IdentitySource(ValueEnum):
    """How ownership of an identifier was proven."""

    OTP = "otp"
    GOOGLE = "google"


class OtpChannel(ValueEnum):
    PHONE = "phone"
    EMAIL = "email"


class OtpPurpose(ValueEnum):
    LOGIN = "login"
    LINK = "link"


# An account may hold at most one identifier per provider, so linking a second
# phone or email is a replacement rather than an addition. Google is keyed by
# its stable subject and carries its own email row for uniqueness.
IDENTITY_CHANNELS: dict[OtpChannel, IdentityProvider] = {
    OtpChannel.PHONE: IdentityProvider.PHONE,
    OtpChannel.EMAIL: IdentityProvider.EMAIL,
}
SIGN_IN_PROVIDERS = frozenset(IdentityProvider)


class ObservationProcessingState(ValueEnum):
    RECEIVED = "received"
    ATTACHED = "attached"
    NEEDS_REVIEW = "needs_review"


class ReconciliationOutcome(ValueEnum):
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    MATCHED = "MATCHED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NOT_MATCHED = "NOT_MATCHED"


class ReconciliationResolution(ValueEnum):
    SAME_TRANSACTION = "same_transaction"
    SEPARATE_TRANSACTION = "separate_transaction"


class ImportStatus(ValueEnum):
    PROCESSING = "processing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    READY_FOR_REVIEW = "ready_for_review"
    COMPLETED = "completed"


class ImportRecordStatus(ValueEnum):
    INVALID = "invalid"
    STAGED = "staged"
    DUPLICATE = "duplicate"
    NEEDS_REVIEW = "needs_review"
    IMPORTED = "imported"


class ExecutionStatus(ValueEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AnalysisToolStatus(ValueEnum):
    DRAFT = "draft"
    VALIDATING = "validating"
    ACTIVE = "active"


class FinancialSourceType(ValueEnum):
    MANUAL = "manual"
    SMS = "sms"
    EMAIL = "email"
    BANK = "bank"
    CSV = "csv"
    PDF = "pdf"
    RECEIPT = "receipt"
    API = "api"


EDITABLE_TRANSACTION_TYPES = tuple(
    item for item in TransactionType if item is not TransactionType.UNKNOWN
)
MESSAGE_SOURCE_TYPES = frozenset({FinancialSourceType.SMS, FinancialSourceType.EMAIL})
REVOCABLE_SOURCE_TYPES = frozenset({
    FinancialSourceType.SMS,
    FinancialSourceType.EMAIL,
    FinancialSourceType.CSV,
    FinancialSourceType.BANK,
    FinancialSourceType.API,
})
