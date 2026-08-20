from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

from .config import CSV_UPLOAD_MAX_BYTES
from .domain import EDITABLE_TRANSACTION_TYPES
from .services.tool_models import AffordabilityResult, InvestmentProjectionResult, LoanPaymentResult, LoanPrepaymentResult
from .visualization_contracts import VisualEncodingContract, VisualFieldEncoding, VisualizationView
from .schemas import (
    ACTION_PAYLOAD_MODELS,
    ChartLineage,
    AgentActivityEvent,
    AgentModelPassMetrics,
    AgentRunMetrics,
    AgentDecisionDiagnostic,
    AgentDiagnosticsOut,
    AgentModelSet,
    AgentResponse,
    AgentSettingsOut,
    AgentInterruptOut,
    AgentRunOut,
    AgentThreadStateOut,
    AuthStatusOut,
    BootstrapResponse,
    BootstrapUser,
    ConversationCreatedOut,
    ConversationOut,
    ConversationPage,
    ConversationRenameIn,
    ConversationSummaryOut,
    CategoryDirectoryOut,
    CategoryDirectorySubcategoryOut,
    ClarificationOptionData,
    DataReference,
    DataDeletionOut,
    FinancialMessageOut,
    GoogleSignInIn,
    HealthOut,
    IdentityOut,
    ImportResultOut,
    LocationPreferenceOut,
    MessageOut,
    OtpSentOut,
    OtpStartIn,
    OtpVerifyIn,
    OverviewAccountOut,
    OverviewBudgetOut,
    OverviewCategoryOut,
    OverviewOut,
    OverviewPeriodOut,
    OverviewSubcategoryOut,
    OverviewSummaryOut,
    OverviewTransactionOut,
    OverviewTrendPointOut,
    PendingAction,
    PrivacyStatusOut,
    ProfileOut,
    ReconciliationResultOut,
    ReconciliationReviewOut,
    SourceRevocationOut,
    SignOutOut,
    TaxonomyCreateIn,
    TransactionCategoryHintIn,
    TransactionCategoryHintOut,
    TransactionListItemOut,
    TransactionUpdateIn,
    Widget,
    WidgetAction,
    WidgetActionId,
    WIDGET_DATA_MODELS,
    WidgetType,
    WidgetUpdate,
)


FRONTEND_CONTRACT_MODELS: tuple[type[BaseModel], ...] = (
    WidgetAction,
    *tuple(dict.fromkeys(ACTION_PAYLOAD_MODELS.values())),
    VisualFieldEncoding,
    VisualEncodingContract,
    VisualizationView,
    ChartLineage,
    ClarificationOptionData,
    AgentModelPassMetrics,
    AgentRunMetrics,
    *tuple(dict.fromkeys(WIDGET_DATA_MODELS.values())),
    Widget,
    PendingAction,
    DataReference,
    WidgetUpdate,
    AgentResponse,
    AgentSettingsOut,
    AgentInterruptOut,
    AgentRunOut,
    AgentThreadStateOut,
    MessageOut,
    ConversationOut,
    ConversationSummaryOut,
    ConversationRenameIn,
    ConversationPage,
    BootstrapUser,
    BootstrapResponse,
    OverviewPeriodOut,
    OverviewSummaryOut,
    OverviewSubcategoryOut,
    OverviewCategoryOut,
    OverviewTrendPointOut,
    OverviewTransactionOut,
    OverviewAccountOut,
    OverviewBudgetOut,
    OverviewOut,
    CategoryDirectorySubcategoryOut,
    CategoryDirectoryOut,
    TaxonomyCreateIn,
    TransactionCategoryHintOut,
    TransactionCategoryHintIn,
    TransactionListItemOut,
    TransactionUpdateIn,
    ImportResultOut,
    AgentModelSet,
    HealthOut,
    AgentDecisionDiagnostic,
    AgentDiagnosticsOut,
    ConversationCreatedOut,
    FinancialMessageOut,
    ReconciliationResultOut,
    ReconciliationReviewOut,
    PrivacyStatusOut,
    LocationPreferenceOut,
    SourceRevocationOut,
    DataDeletionOut,
    OtpStartIn,
    OtpVerifyIn,
    OtpSentOut,
    GoogleSignInIn,
    IdentityOut,
    ProfileOut,
    AuthStatusOut,
    SignOutOut,
    AffordabilityResult,
    LoanPaymentResult,
    LoanPrepaymentResult,
    InvestmentProjectionResult,
    AgentActivityEvent,
)


def frontend_contract_bundle() -> dict:
    payload = {
        "schemaVersion": 1,
        "enums": {
            "widgetTypes": [item.value for item in WidgetType],
            "widgetActions": [item.value for item in WidgetActionId],
            "editableTransactionTypes": [
                item.value for item in EDITABLE_TRANSACTION_TYPES
            ],
        },
        "limits": {
            "csvUploadBytes": CSV_UPLOAD_MAX_BYTES,
        },
        "schemas": {
            model.__name__: model.model_json_schema(mode="serialization", by_alias=True)
            for model in FRONTEND_CONTRACT_MODELS
        },
        "widgetDataModels": {
            widget_type.value: model.__name__
            for widget_type, model in WIDGET_DATA_MODELS.items()
        },
        "actionPayloadModels": {
            action.value: model.__name__
            for action, model in ACTION_PAYLOAD_MODELS.items()
        },
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**payload, "schemaHash": fingerprint}
