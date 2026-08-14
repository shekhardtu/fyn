from __future__ import annotations

import hashlib
import json

from .config import CSV_UPLOAD_MAX_BYTES
from .domain import EDITABLE_TRANSACTION_TYPES
from .services.tool_models import AffordabilityResult, InvestmentProjectionResult, LoanPaymentResult, LoanPrepaymentResult
from .schemas import (
    ACTION_PAYLOAD_MODELS,
    AgentActivityEvent,
    AgentDecisionDiagnostic,
    AgentDiagnosticsOut,
    AgentModelSet,
    AgentResponse,
    AgentInterruptOut,
    AgentRunOut,
    AgentThreadStateOut,
    AuthStatusOut,
    BootstrapResponse,
    BootstrapUser,
    ConversationCreatedOut,
    ConversationOut,
    ConversationPage,
    ConversationSummaryOut,
    CategoryDirectoryOut,
    CategoryDirectorySubcategoryOut,
    DataChartAxis,
    DataChartSeries,
    DataReference,
    DataDeletionOut,
    DataTableColumn,
    DataTableRowAction,
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
    OverviewCategoryOut,
    OverviewOut,
    OverviewPeriodOut,
    OverviewSubcategoryOut,
    OverviewSummaryOut,
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
    VisualEncodingContract,
    VisualFieldEncoding,
    VisualizationLayout,
    VisualizationView,
    Widget,
    WidgetAction,
    WidgetActionId,
    WIDGET_DATA_MODELS,
    WidgetType,
    WidgetUpdate,
)


FRONTEND_CONTRACT_MODELS = (
    WidgetAction,
    *tuple(dict.fromkeys(ACTION_PAYLOAD_MODELS.values())),
    DataTableColumn,
    DataTableRowAction,
    DataChartAxis,
    DataChartSeries,
    VisualFieldEncoding,
    VisualEncodingContract,
    VisualizationView,
    VisualizationLayout,
    *tuple(dict.fromkeys(WIDGET_DATA_MODELS.values())),
    Widget,
    PendingAction,
    DataReference,
    WidgetUpdate,
    AgentResponse,
    AgentInterruptOut,
    AgentRunOut,
    AgentThreadStateOut,
    MessageOut,
    ConversationOut,
    ConversationSummaryOut,
    ConversationPage,
    BootstrapUser,
    BootstrapResponse,
    OverviewPeriodOut,
    OverviewSummaryOut,
    OverviewSubcategoryOut,
    OverviewCategoryOut,
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
