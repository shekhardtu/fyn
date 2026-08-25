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
  type AgentSettingsOut,
  type AgentActivityEvent,
  type AgentInterruptOut,
  type AgentRunMetrics,
  type AgentRunOut,
  type AgentThreadStateOut,
  type AuthStatusOut,
  type Bootstrap,
  type CategoryDirectoryOut,
  type CategoryDirectorySubcategoryOut,
  type ConversationPage,
  type ConversationOut,
  type ConversationCreatedOut,
  type ConversationSummary,
  type ContactSuggestionOut,
  type CreatePersonalLoanIn,
  type DataChartData,
  type DocumentRevisionOut,
  type IdentityOut,
  type ImportResult,
  type InvitationPreviewOut,
  type LocationResolveOut,
  type LoanCommandOut,
  type LoanTermProposalIn,
  type Message,
  type OtpSentOut,
  type OverviewOut,
  type PersonalLoanDetailOut,
  type PersonalLoanListOut,
  type PersonalLoanSummaryOut,
  type PrivacyStatusOut,
  type ProfileOut,
  type RecordLoanPaymentIn,
  type ReminderOut,
  type SendLoanReminderIn,
  type TransactionListItemOut,
  type TransactionRevisionOut,
  type TransactionCategoryHintOut,
  type TransactionUpdateIn,
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
export const dataChartDataSchema = generatedSchema<DataChartData>("DataChartData");
export const transactionRevisionSchema = generatedSchema<TransactionRevisionOut>("TransactionRevisionOut");
export const transactionRevisionListSchema = transactionRevisionSchema.array();

export type { TransactionRevisionOut };

/** Dashboards are a REST surface rather than a widget lane, so their envelope
 *  is written here against the fixed API contract; the tile's chart itself
 *  re-uses the generated data_chart widget schema, so a tile can only carry
 *  what a conversation could have rendered. */
export const dashboardSummarySchema = z.object({
  id: z.string(),
  name: z.string(),
  tileCount: z.number().int().nonnegative(),
});
export const dashboardListSchema = z.object({ dashboards: dashboardSummarySchema.array() });
export const dashboardTileSchema = z.object({
  id: z.string(),
  title: z.string(),
  position: z.number().int(),
  executedAt: z.string(),
  chart: dataChartDataSchema.nullable(),
  error: z.object({ code: z.string(), detail: z.string() }).nullable(),
});
export const dashboardDetailSchema = z.object({
  id: z.string(),
  name: z.string(),
  tiles: dashboardTileSchema.array(),
});
export type DashboardSummary = z.infer<typeof dashboardSummarySchema>;
export type DashboardTile = z.infer<typeof dashboardTileSchema>;
export type DashboardDetail = z.infer<typeof dashboardDetailSchema>;

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
export const locationResolveSchema = generatedSchema<LocationResolveOut>("LocationResolveOut");
export const privacyStatusSchema = generatedSchema<PrivacyStatusOut>("PrivacyStatusOut");
export const agentSettingsSchema = generatedSchema<AgentSettingsOut>("AgentSettingsOut");
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
export const personalLoanListSchema = generatedSchema<PersonalLoanListOut>("PersonalLoanListOut");
export const contactSuggestionSchema = generatedSchema<ContactSuggestionOut>("ContactSuggestionOut");
export const personalLoanDetailSchema = generatedSchema<PersonalLoanDetailOut>("PersonalLoanDetailOut");
export const loanCommandSchema = generatedSchema<LoanCommandOut>("LoanCommandOut");
export const invitationPreviewSchema = generatedSchema<InvitationPreviewOut>("InvitationPreviewOut");
export const documentRevisionListSchema = generatedSchema<DocumentRevisionOut>("DocumentRevisionOut").array();
export const reminderSchema = generatedSchema<ReminderOut>("ReminderOut");

export type {
  AgentResponse,
  AgentSettingsOut,
  AgentActivityEvent,
  AgentInterruptOut,
  AgentRunMetrics,
  AgentRunOut,
  AgentThreadStateOut,
  AuthStatusOut,
  Bootstrap,
  CategoryDirectoryOut,
  ConversationPage,
  ConversationOut,
  ConversationCreatedOut,
  ConversationSummary,
  ContactSuggestionOut,
  CreatePersonalLoanIn,
  DataChartData,
  DocumentRevisionOut,
  IdentityOut,
  ImportResult,
  InvitationPreviewOut,
  LocationResolveOut,
  LoanCommandOut,
  LoanTermProposalIn,
  Message,
  OtpSentOut,
  OverviewOut,
  PersonalLoanDetailOut,
  PersonalLoanListOut,
  PersonalLoanSummaryOut,
  PrivacyStatusOut,
  ProfileOut,
  RecordLoanPaymentIn,
  ReminderOut,
  SendLoanReminderIn,
  TransactionListItemOut,
  TransactionCategoryHintOut,
  TransactionUpdateIn,
  Widget,
  WidgetActionId,
};
export type { CategoryDirectorySubcategoryOut };
