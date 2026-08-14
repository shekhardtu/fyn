import { z } from "zod";

import {
  actionPayloadSchemas,
  schemas as contractSchemas,
  widgetDataSchemas,
} from "@/lib/generated/contracts.zod";
import {
  widgetTypes,
  widgetTypeIds,
  widgetActions,
  widgetActionIds,
  editableTransactionTypes,
  editableTransactionTypeIds,
  type AgentResponse,
  type AgentActivityEvent,
  type AgentInterruptOut,
  type AgentRunOut,
  type AgentThreadMetricsOut,
  type AgentThreadStateOut,
  type AuthStatusOut,
  type Bootstrap,
  type CategoryDirectoryOut,
  type CategoryDirectorySubcategoryOut,
  type ConversationPage,
  type ConversationOut,
  type ConversationCreatedOut,
  type ConversationSummary,
  type DataChartData,
  type DataTableColumn,
  type DataTableData,
  type DataTableRowAction,
  type DataVisualizationData,
  type IdentityOut,
  type ImportResult,
  type Message,
  type OtpSentOut,
  type OverviewOut,
  type PrivacyStatusOut,
  type ProfileOut,
  type TransactionListItemOut,
  type TransactionCategoryHintOut,
  type TransactionUpdateIn,
  type VisualEncodingContract,
  type VisualFieldEncoding,
  type Widget,
  type WidgetAction,
  type WidgetActionId,
} from "@/lib/generated/contracts";

export { widgetTypes, widgetTypeIds, widgetActions, widgetActionIds, editableTransactionTypes, editableTransactionTypeIds };

/** The schemas are built by the contract generator now, not converted from JSON
 *  Schema in the browser. Same shapes, same rejections — there is a test that
 *  holds the two to each other — without shipping Zod's converter or the 189 KB
 *  bundle it converted, and without the ~39ms it cost on every page load. */
function generatedSchema<T>(name: keyof typeof contractSchemas): z.ZodType<T> {
  return contractSchemas[name] as unknown as z.ZodType<T>;
}

export const widgetActionSchema = generatedSchema<WidgetAction>("WidgetAction");

export function parseActionPayload(action: WidgetActionId, payload: Record<string, unknown>) {
  return actionPayloadSchemas[action].parse(payload) as Record<string, unknown>;
}
export const dataTableColumnSchema = generatedSchema<DataTableColumn>("DataTableColumn");
export const dataTableRowActionSchema = generatedSchema<DataTableRowAction>("DataTableRowAction");
export const dataTableDataSchema = generatedSchema<DataTableData>("DataTableData");

const generatedDataChartSchema = generatedSchema<DataChartData>("DataChartData");
export const dataChartDataSchema = generatedDataChartSchema.superRefine((chart, context) => {
  if (chart.chartType === "heatmap" && !chart.yAxis) {
    context.addIssue({ code: "custom", message: "Heatmaps require a y-axis dimension", path: ["yAxis"] });
  }
});

export const visualFieldEncodingSchema = generatedSchema<VisualFieldEncoding>("VisualFieldEncoding");
export const visualEncodingSchema = generatedSchema<VisualEncodingContract>("VisualEncodingContract");
export const dataVisualizationDataSchema = generatedSchema<DataVisualizationData>("DataVisualizationData");

const generatedWidgetSchema = generatedSchema<Widget>("Widget");

export const widgetSchema = generatedWidgetSchema.superRefine((widget, context) => {
  const registered = widgetDataSchemas[widget.type].safeParse(widget.data);
  if (!registered.success) {
    context.addIssue({ code: "custom", message: `Invalid ${widget.type.replaceAll("_", "-")} contract`, path: ["data"] });
  }
});

export const messageSchema = generatedSchema<Message>("MessageOut").superRefine((message, context) => {
  message.widgets.forEach((widget, index) => {
    if (!widgetSchema.safeParse(widget).success) {
      context.addIssue({ code: "custom", message: "Invalid message widget", path: ["widgets", index] });
    }
  });
});
export const bootstrapSchema = generatedSchema<Bootstrap>("BootstrapResponse");
export const conversationSummarySchema = generatedSchema<ConversationSummary>("ConversationSummaryOut");
export const conversationPageSchema = generatedSchema<ConversationPage>("ConversationPage");
export const conversationSchema = generatedSchema<ConversationOut>("ConversationOut");
export const conversationCreatedSchema = generatedSchema<ConversationCreatedOut>("ConversationCreatedOut");
export const overviewSchema = generatedSchema<OverviewOut>("OverviewOut");
export const categoryDirectoryEntrySchema = generatedSchema<CategoryDirectoryOut>("CategoryDirectoryOut");
export const categoryDirectorySchema = categoryDirectoryEntrySchema.array();
export const categorySubcategorySchema = generatedSchema<CategoryDirectorySubcategoryOut>("CategoryDirectorySubcategoryOut");
export const transactionCategoryHintSchema = generatedSchema<TransactionCategoryHintOut>("TransactionCategoryHintOut");
export const transactionListSchema = generatedSchema<TransactionListItemOut>("TransactionListItemOut").array();
export const transactionListItemSchema = generatedSchema<TransactionListItemOut>("TransactionListItemOut");
export const privacyStatusSchema = generatedSchema<PrivacyStatusOut>("PrivacyStatusOut");
export const agentResponseSchema = generatedSchema<AgentResponse>("AgentResponse").superRefine((response, context) => {
  response.widgets.forEach((widget, index) => {
    if (!widgetSchema.safeParse(widget).success) {
      context.addIssue({ code: "custom", message: "Invalid response widget", path: ["widgets", index] });
    }
  });
});
export const authStatusSchema = generatedSchema<AuthStatusOut>("AuthStatusOut");
export const profileSchema = generatedSchema<ProfileOut>("ProfileOut");
export const otpSentSchema = generatedSchema<OtpSentOut>("OtpSentOut");
export const importResultSchema = generatedSchema<ImportResult>("ImportResultOut");
export const agentActivityEventSchema = generatedSchema<AgentActivityEvent>("AgentActivityEvent");
export const agentThreadStateSchema = generatedSchema<AgentThreadStateOut>("AgentThreadStateOut");
export const agentThreadMetricsSchema = generatedSchema<AgentThreadMetricsOut>("AgentThreadMetricsOut");

export type {
  AgentResponse,
  AgentActivityEvent,
  AgentInterruptOut,
  AgentRunOut,
  AgentThreadMetricsOut,
  AgentThreadStateOut,
  AuthStatusOut,
  Bootstrap,
  CategoryDirectoryOut,
  ConversationPage,
  ConversationOut,
  ConversationCreatedOut,
  ConversationSummary,
  DataChartData,
  DataTableColumn,
  DataTableData,
  DataTableRowAction,
  DataVisualizationData,
  IdentityOut,
  ImportResult,
  Message,
  OtpSentOut,
  OverviewOut,
  PrivacyStatusOut,
  ProfileOut,
  TransactionListItemOut,
  TransactionCategoryHintOut,
  TransactionUpdateIn,
  Widget,
  WidgetActionId,
};
export type { CategoryDirectorySubcategoryOut };
