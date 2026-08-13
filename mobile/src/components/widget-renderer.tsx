import { memo, useMemo, useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";

import { ChartView } from "@/components/charts";
import { Banner, Button, Card, CardHeader, Chip, Divider, EmptyNote, Field, FieldLabel, Money, Pill, Type } from "@/components/ui";
import { formatCount, formatDay, formatDimension, formatDuration, formatMoney, parseAmountToMinor, parseNumber } from "@/lib/format";
import {
  dataChartDataSchema,
  dataTableDataSchema,
  dataVisualizationDataSchema,
  widgetActionIds,
  widgetActions,
  widgetTypeIds,
  type Widget,
  type WidgetActionId,
} from "@/lib/protocol";
import {
  isMoneyValueType,
  readAmountMinor,
  readField,
  readRowCurrency,
  readVisualChannels,
} from "@/lib/generated/contracts.readers";
import { radius, space, type Palette } from "@/lib/theme";
import { useStyles, useTheme } from "@/lib/appearance";

/**
 * Every typed widget the harness can send, rendered natively.
 *
 * The model never emits UI. It emits a contract, and this file is the only
 * place that decides what a contract looks like — which is why the same 25
 * types can be re-authored for a phone without the backend knowing it happened.
 */

export type WidgetActionHandler = (
  widgetId: string,
  action: WidgetActionId,
  payload: Record<string, unknown>,
  options?: { markUsed?: boolean },
) => void;

type Primitive = string | number | boolean | null | undefined;
type Data = Record<string, unknown>;

function str(value: unknown, fallback = "") { return typeof value === "string" ? value : fallback; }
function num(value: unknown) { const parsed = typeof value === "number" ? value : Number(value ?? 0); return Number.isFinite(parsed) ? parsed : 0; }
function rows(data: unknown): Data[] { return Array.isArray(data) ? data as Data[] : []; }

function plural(count: unknown, one: string, many = `${one}s`) {
  return `${formatCount(count, 0)} ${num(count) === 1 ? one : many}`;
}
function options(data: Data) { return Array.isArray(data.options) ? data.options as Array<Record<string, Primitive>> : []; }

function isWidgetActionId(value: unknown): value is WidgetActionId {
  return typeof value === "string" && (widgetActions as readonly string[]).includes(value);
}

/** What a completed control submitted, so the card can show its own receipt
 *  after it has gone read-only — and still show it after a cold start. */
function completionValues(widget: Widget): Data {
  const completion = widget.data.completion;
  if (!completion || typeof completion !== "object") return {};
  const values = (completion as Data).values;
  return values && typeof values === "object" ? values as Data : {};
}

type BodyProps = {
  widget: Widget;
  data: Data;
  currency: string;
  disabled: boolean;
  /** Retired for good — answered, cancelled, or superseded by a later turn.
   *  Distinct from `disabled`, which is also true for the moment a request is
   *  in flight and the control is coming back. */
  spent: boolean;
  pending: boolean;
  onAction: WidgetActionHandler;
};

// ── Shared pieces ────────────────────────────────────────────────────────────

/** The declared actions, as buttons.
 *
 *  Most widgets need nothing more than this: the server names the label, the
 *  action and the payload, and the client's only job is to submit it. That is
 *  the reason the HITL layer ported at all — the decision about what may be
 *  pressed was never here. */
function DeclaredActions({ widget, disabled, pending, onAction }: Omit<BodyProps, "data" | "currency" | "spent">) {
  const styles = useStyles(makeStyles);
  const actions = widget.actions ?? [];
  if (!actions.length) return null;
  return (
    <View style={styles.actionRow}>
      {actions.map((action) => (
        <Button
          key={action.id}
          onPress={() => onAction(widget.id, action.action, (action.payload ?? {}) as Data)}
          disabled={disabled}
          busy={pending}
          variant={action.style === "primary" ? "filled" : action.style === "danger" ? "danger" : "outline"}
          style={{ flexGrow: 1, flexBasis: actions.length > 2 ? "45%" : undefined }}
        >
          {action.label}
        </Button>
      ))}
    </View>
  );
}

/** The receipt a spent control leaves behind. */
function Receipt({ widget }: { widget: Widget }) {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  const lifecycle = widget.data.lifecycle;
  if (lifecycle !== "completed" && lifecycle !== "cancelled") return null;
  const values = completionValues(widget);
  const label = str(values.label) || str((widget.data.completion as Data)?.label);
  return (
    <View style={[styles.receipt, lifecycle === "cancelled" && { backgroundColor: color.sunken }]}>
      <Type size="meta" weight="semibold" color={lifecycle === "cancelled" ? "muted" : "secondary"}>
        {lifecycle === "cancelled" ? "Cancelled" : label ? `Chosen · ${label}` : "Done"}
      </Type>
    </View>
  );
}

function Rows({ items }: { items: Array<[string, React.ReactNode]> }) {
  const styles = useStyles(makeStyles);
  return (
    <View style={styles.detailList}>
      {items.map(([label, value]) => (
        <View key={label} style={styles.detailRow}>
          <Type size="note" color="muted">{label}</Type>
          <View style={styles.detailValue}>{typeof value === "string" ? <Type size="note" color="ink" weight="medium">{value}</Type> : value}</View>
        </View>
      ))}
    </View>
  );
}

function Meter({ percent, tone = "secondary" }: { percent: number; tone?: "secondary" | "out" }) {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  const clamped = Math.max(0, Math.min(100, percent));
  return (
    <View style={styles.meterTrack} accessibilityRole="progressbar" accessibilityValue={{ now: Math.round(clamped), min: 0, max: 100 }}>
      <View style={[styles.meterFill, { width: `${clamped}%`, backgroundColor: tone === "out" ? color.moneyOut : color.secondary }]} />
    </View>
  );
}

function optionId(option: Record<string, Primitive>, index: number) {
  return str(option.id ?? option.categoryId ?? option.subcategoryId ?? option.accountId ?? option.value, String(index));
}

/** A list of server-provided options, one tap each. Used by every selector.
 *
 *  Deduplicated by id, because a suggestion is also a catalogue entry and
 *  arrives in both arrays carrying the same id. Where a widget renders the two
 *  arrays as separate lists that overlap is deliberate — the shortcut is worth
 *  the repetition — but a single list must never show one option twice, and
 *  React cannot key it if it does. */
function OptionList({ items, disabled, selectedId, onPick }: {
  items: Array<Record<string, Primitive>>;
  disabled: boolean;
  selectedId?: string | null;
  onPick: (option: Record<string, Primitive>) => void;
}) {
  const styles = useStyles(makeStyles);
  const seen = new Set<string>();
  const unique = items.filter((option, index) => {
    const id = optionId(option, index);
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });
  if (!unique.length) return null;
  return (
    <View style={styles.optionWrap}>
      {unique.map((option, index) => {
        const id = optionId(option, index);
        return (
          <Chip
            key={id}
            label={str(option.label ?? option.name, id)}
            detail={typeof option.detail === "string" ? option.detail : typeof option.hint === "string" ? option.hint : null}
            selected={selectedId === id}
            disabled={disabled}
            onPress={() => onPick(option)}
          />
        );
      })}
    </View>
  );
}

/** Resolves the id a spent control submitted back to the label it was shown as. */
function chosenLabel(widget: Widget, values: Data) {
  const data = widget.data as Data;
  const candidates = [
    ...(Array.isArray(data.suggestions) ? data.suggestions as Array<Record<string, Primitive>> : []),
    ...options(data),
  ];
  const submitted = [values.categoryId, values.subcategoryId, values.accountId, values.optionId, values.transactionType]
    .find((value) => typeof value === "string" && value);
  if (!submitted) return "";
  const match = candidates.find((option, index) => optionId(option, index) === submitted
    || str(option.transactionType ?? option.value) === submitted);
  return match ? str(match.label ?? match.name) : "";
}

/**
 * What a retired selector shows instead of its options.
 *
 * A dozen dead chips is most of a phone screen spent saying "you cannot press
 * these". Scrolling back through a conversation should read as a record of what
 * was decided, so a spent control collapses to the decision itself and the
 * transcript stays skimmable.
 */
function SpentSelector({ widget, note }: { widget: Widget; note?: string }) {
  const styles = useStyles(makeStyles);
  const lifecycle = widget.data.lifecycle;
  const values = completionValues(widget);
  // The receipt records which id was submitted, not what it was called. The
  // widget still carries the list it was chosen from, so the name is resolved
  // here rather than asking the server to denormalise it into every receipt.
  const chosen = str(values.label) || str(values.name) || chosenLabel(widget, values);
  const answered = lifecycle === "completed";
  const cancelled = lifecycle === "cancelled";

  // Three different endings, and saying the wrong one is worse than saying
  // nothing: a control that was superseded by a later turn was never answered,
  // so reporting a choice for it would be a small lie in a financial record.
  const ending = cancelled
    ? "Cancelled"
    : chosen || (answered ? note ?? "Done" : null) || "Superseded by a later message";

  return (
    <View style={styles.spent}>
      <Type size="note" color={chosen || answered ? "ink" : "muted"} weight={chosen ? "medium" : "regular"}>
        {ending}
      </Type>
    </View>
  );
}

// ── Selectors ────────────────────────────────────────────────────────────────

function CategorySelector({ widget, data, spent, disabled, pending, onAction }: BodyProps) {
  const styles = useStyles(makeStyles);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  if (spent) return <SpentSelector widget={widget} note="Category chosen" />;
  const draftId = str(data.draftId);
  const suggestions = Array.isArray(data.suggestions) ? data.suggestions as Array<Record<string, Primitive>> : [];
  const catalogue = options(data);
  const chosen = str(completionValues(widget).categoryId) || null;

  return (
    <View style={styles.body}>
      {suggestions.length ? (
        <>
          <FieldLabel>Suggested</FieldLabel>
          <OptionList
            items={suggestions}
            disabled={disabled}
            selectedId={chosen}
            onPick={(option) => onAction(widget.id, widgetActionIds.select_category, { draftId, categoryId: str(option.id ?? option.categoryId) })}
          />
        </>
      ) : null}

      {catalogue.length ? (
        <>
          {suggestions.length ? <FieldLabel>All categories</FieldLabel> : null}
          <OptionList
            items={catalogue}
            disabled={disabled}
            selectedId={chosen}
            onPick={(option) => onAction(widget.id, widgetActionIds.select_category, { draftId, categoryId: str(option.id ?? option.categoryId) })}
          />
        </>
      ) : null}

      {data.allowCreate && !disabled ? (
        creating ? (
          <View style={{ gap: space.snug }}>
            <FieldLabel>New category</FieldLabel>
            <Field value={name} onChangeText={setName} placeholder="Groceries" autoFocus autoCapitalize="words" returnKeyType="done" />
            <View style={styles.actionRow}>
              <Button
                onPress={() => onAction(widget.id, widgetActionIds.create_category, { draftId, name: name.trim() })}
                disabled={!name.trim()}
                busy={pending}
                style={{ flex: 1 }}
              >
                Create
              </Button>
              <Button variant="ghost" onPress={() => { setCreating(false); setName(""); }} style={{ flex: 1 }}>Cancel</Button>
            </View>
          </View>
        ) : (
          <Button variant="ghost" onPress={() => setCreating(true)} style={{ alignSelf: "flex-start" }}>+ New category</Button>
        )
      ) : null}
    </View>
  );
}

function SubcategorySelector({ widget, data, spent, disabled, pending, onAction }: BodyProps) {
  const styles = useStyles(makeStyles);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  if (spent) return <SpentSelector widget={widget} note="Subcategory chosen" />;
  const draftId = str(data.draftId);
  const suggestions = Array.isArray(data.suggestions) ? data.suggestions as Array<Record<string, Primitive>> : [];
  const catalogue = options(data);
  const chosen = str(completionValues(widget).subcategoryId) || null;
  const all = [...suggestions, ...catalogue];

  return (
    <View style={styles.body}>
      <Type size="note" color="muted">Under {str(data.category)}</Type>
      <OptionList
        items={all}
        disabled={disabled}
        selectedId={chosen}
        onPick={(option) => onAction(widget.id, widgetActionIds.select_subcategory, { draftId, subcategoryId: str(option.id ?? option.subcategoryId) })}
      />
      {data.allowCreate && !disabled ? (
        creating ? (
          <View style={{ gap: space.snug }}>
            <Field value={name} onChangeText={setName} placeholder="New subcategory" autoFocus autoCapitalize="words" />
            <View style={styles.actionRow}>
              <Button
                onPress={() => onAction(widget.id, widgetActionIds.create_subcategory, { draftId, categoryId: str(data.categoryId), name: name.trim() })}
                disabled={!name.trim()}
                busy={pending}
                style={{ flex: 1 }}
              >
                Create
              </Button>
              <Button variant="ghost" onPress={() => { setCreating(false); setName(""); }} style={{ flex: 1 }}>Cancel</Button>
            </View>
          </View>
        ) : (
          <Button variant="ghost" onPress={() => setCreating(true)} style={{ alignSelf: "flex-start" }}>+ New subcategory</Button>
        )
      ) : null}
    </View>
  );
}

function TransactionTypeSelector({ widget, data, spent, disabled, onAction }: BodyProps) {
  const styles = useStyles(makeStyles);
  if (spent) return <SpentSelector widget={widget} note="Type chosen" />;
  const draftId = str(data.draftId);
  const chosen = str(completionValues(widget).transactionType) || null;
  return (
    <View style={styles.body}>
      <OptionList
        items={options(data)}
        disabled={disabled}
        selectedId={chosen}
        onPick={(option) => onAction(widget.id, widgetActionIds.select_transaction_type, {
          draftId,
          optionId: str(option.id) || undefined,
          transactionType: str(option.transactionType ?? option.value) || undefined,
        })}
      />
    </View>
  );
}

function AccountSelector({ widget, data, spent, disabled, onAction }: BodyProps) {
  const styles = useStyles(makeStyles);
  if (spent) return <SpentSelector widget={widget} note="Account chosen" />;
  const draftId = str(data.draftId);
  const role = str(data.role);
  const chosen = str(completionValues(widget).accountId) || null;
  return (
    <View style={styles.body}>
      <OptionList
        items={options(data)}
        disabled={disabled}
        selectedId={chosen}
        onPick={(option) => onAction(widget.id, widgetActionIds.select_account, {
          draftId,
          role,
          optionId: str(option.id) || undefined,
          accountId: str(option.accountId ?? option.id) || undefined,
        })}
      />
    </View>
  );
}

function TaxonomyEditor({ widget, data, spent, disabled, pending, onAction }: BodyProps) {
  const styles = useStyles(makeStyles);
  const [name, setName] = useState(str(data.name));
  if (spent) return <SpentSelector widget={widget} note="Saved" />;
  const draftId = str(data.draftId) || undefined;
  const categoryId = str(data.categoryId) || undefined;
  const subcategory = str(data.operation).includes("subcategory");

  return (
    <View style={styles.body}>
      <FieldLabel hint={subcategory && data.parentCategory ? `Under ${str(data.parentCategory)}` : undefined}>
        {subcategory ? "Subcategory name" : "Category name"}
      </FieldLabel>
      <Field value={name} onChangeText={setName} placeholder="Name" editable={!disabled} autoFocus={!disabled} autoCapitalize="words" />
      <View style={styles.actionRow}>
        <Button
          onPress={() => onAction(
            widget.id,
            subcategory ? widgetActionIds.create_subcategory : widgetActionIds.create_category,
            subcategory ? { draftId, categoryId: categoryId ?? "", name: name.trim() } : { draftId, categoryId, name: name.trim() },
          )}
          disabled={disabled || !name.trim()}
          busy={pending}
          style={{ flex: 1 }}
        >
          Save
        </Button>
        <Button
          variant="ghost"
          onPress={() => onAction(widget.id, widgetActionIds.cancel_taxonomy_change, { draftId, categoryId })}
          disabled={disabled}
          style={{ flex: 1 }}
        >
          Cancel
        </Button>
      </View>
    </View>
  );
}

// ── Transactions ─────────────────────────────────────────────────────────────

function typeTone(transactionType: string) {
  return transactionType === "income" || transactionType === "refund" ? "in" as const : "out" as const;
}

function ConfirmationCard({ data, currency }: BodyProps) {
  const styles = useStyles(makeStyles);
  const inferred = Array.isArray(data.inferredFields) ? data.inferredFields as string[] : [];
  const type = str(data.transactionType, "expense");
  const details: Array<[string, React.ReactNode]> = [];
  if (data.merchant) details.push(["Merchant", str(data.merchant)]);
  if (data.category) details.push(["Category", [str(data.category), str(data.subcategory)].filter(Boolean).join(" › ")]);
  if (data.sourceAccount) details.push(["From", str(data.sourceAccount)]);
  if (data.destinationAccount) details.push(["To", str(data.destinationAccount)]);
  details.push(["When", formatDay(str(data.transactionAt).slice(0, 10))]);
  if (data.location) details.push(["Where", str(data.location)]);
  if (data.spendNature) details.push(["Nature", formatDimension(data.spendNature)]);

  return (
    <View style={styles.body}>
      <View style={styles.amountRow}>
        <Money value={formatMoney(data.amountMinor, currency)} size="display" color={typeTone(type)} />
        <Pill tone={typeTone(type)}>{formatDimension(type)}</Pill>
      </View>
      <Rows items={details} />
      {inferred.length ? (
        <Type size="meta" color="muted">
          Inferred by fyn AI: {inferred.map((field) => formatDimension(field)).join(", ")}. Correct anything that's wrong before saving.
        </Type>
      ) : null}
    </View>
  );
}

function TransactionPreview({ data, currency }: BodyProps) {
  const styles = useStyles(makeStyles);
  const type = str(data.transactionType, "expense");
  const sources = num(data.sourceCount);
  return (
    <View style={styles.body}>
      <View style={styles.amountRow}>
        <Money value={formatMoney(data.amountMinor, currency)} size="title" color={typeTone(type)} />
        <Type size="note" color="muted">{formatDay(str(data.transactionAt).slice(0, 10))}</Type>
      </View>
      <Rows items={[
        ...(data.category ? [["Category", [str(data.category), str(data.subcategory)].filter(Boolean).join(" › ")] as [string, React.ReactNode]] : []),
        ["Status", formatDimension(data.status)],
        ...(sources > 1 ? [["Sources", `${sources} matched records`] as [string, React.ReactNode]] : []),
      ]} />
    </View>
  );
}

/** The one widget that is a form rather than a set of choices. */
function TransactionEdit({ widget, data, disabled, pending, onAction }: BodyProps) {
  const styles = useStyles(makeStyles);
  const currency = str(data.currency, "INR");
  const saved = Boolean(data.transactionId);
  const [amount, setAmount] = useState(data.amountMinor != null ? String(num(data.amountMinor) / 100) : "");
  const [merchant, setMerchant] = useState(str(data.merchant));
  const [location, setLocation] = useState(str(data.location));
  const amountMinor = parseAmountToMinor(amount);
  const invalid = amount.trim().length > 0 && amountMinor === null;

  function submit() {
    if (amountMinor === null) return;
    if (saved) {
      onAction(widget.id, widgetActionIds.update_saved_transaction, {
        transactionId: str(data.transactionId),
        amountMinor,
        merchant: merchant.trim() || undefined,
        location: location.trim() || undefined,
      });
      return;
    }
    onAction(widget.id, widgetActionIds.update_transaction_draft, {
      draftId: str(data.draftId),
      amountMinor,
      merchant: merchant.trim() || undefined,
    });
  }

  return (
    <View style={styles.body}>
      <View>
        <FieldLabel>Amount</FieldLabel>
        <Field
          value={amount}
          onChangeText={setAmount}
          placeholder="0"
          keyboardType="decimal-pad"
          editable={!disabled}
          invalid={invalid}
          accessibilityLabel={`Amount in ${currency}`}
        />
        {invalid ? <Type size="meta" color="danger" style={{ marginTop: space.tight }}>Enter an amount like 1,500 or 250.75.</Type> : null}
      </View>
      <View>
        <FieldLabel>Merchant</FieldLabel>
        <Field value={merchant} onChangeText={setMerchant} placeholder="Where it was spent" editable={!disabled} autoCapitalize="words" />
      </View>
      {saved ? (
        <View>
          <FieldLabel>Location</FieldLabel>
          <Field value={location} onChangeText={setLocation} placeholder="Optional" editable={!disabled} autoCapitalize="words" />
        </View>
      ) : null}
      <View style={styles.actionRow}>
        <Button onPress={submit} disabled={disabled || amountMinor === null} busy={pending} style={{ flex: 1 }}>Save changes</Button>
        {saved ? (
          <Button
            variant="ghost"
            onPress={() => onAction(widget.id, widgetActionIds.cancel_saved_transaction_edit, { transactionId: str(data.transactionId) })}
            disabled={disabled}
            style={{ flex: 1 }}
          >
            Cancel
          </Button>
        ) : null}
      </View>
    </View>
  );
}

function TransactionList({ widget, data, currency, disabled, onAction }: BodyProps) {
  const styles = useStyles(makeStyles);
  const items = rows(data.transactions);
  if (!items.length) return <EmptyNote>No transactions here yet.</EmptyNote>;
  return (
    <View>
      {items.map((row, index) => {
        const actions = Array.isArray(row.actions) ? row.actions as Array<Record<string, unknown>> : [];
        const type = str(row.transactionType, "expense");
        return (
          <View key={str(row.id, String(index))}>
            {index ? <Divider /> : null}
            <View style={styles.listRow}>
              <View style={{ flex: 1, minWidth: 0 }}>
                <Type size="control" weight="medium" color="ink" numberOfLines={1}>{str(row.merchant ?? row.title, "Transaction")}</Type>
                <Type size="meta" color="muted" numberOfLines={1}>
                  {[formatDay(str(row.transactionAt).slice(0, 10)), str(row.category)].filter(Boolean).join(" · ")}
                </Type>
              </View>
              <Money value={formatMoney(row.amountMinor, str(row.currency, currency))} size="control" color={typeTone(type)} />
            </View>
            {actions.length ? (
              <View style={[styles.actionRow, { paddingHorizontal: space.gutter, paddingBottom: space.base }]}>
                {actions.map((action, actionIndex) => {
                  const id = action.action;
                  if (!isWidgetActionId(id)) return null;
                  return (
                    <Button
                      key={str(action.id, String(actionIndex))}
                      variant={action.style === "danger" ? "danger" : "outline"}
                      size="control"
                      disabled={disabled}
                      onPress={() => onAction(widget.id, id, (action.payload as Data) ?? { transactionId: str(row.id) }, { markUsed: false })}
                    >
                      {str(action.label, "Do")}
                    </Button>
                  );
                })}
              </View>
            ) : null}
          </View>
        );
      })}
    </View>
  );
}

// ── Tables ───────────────────────────────────────────────────────────────────

function cellText(row: Data, key: string, type: string | undefined, currency: string) {
  const value = row[key];
  if (type === "money") return formatMoney(value, currency);
  if (type === "number") return formatCount(value);
  if (type === "date" || type === "datetime") return formatDay(value);
  if (type === "percentage") return `${formatCount(value, 1)}%`;
  if (type === "tags") return Array.isArray(value) && value.length ? value.map((tag) => formatDimension(tag)).join(", ") : "—";
  if (type === "boolean") return value ? "Yes" : "No";
  if (value === null || value === undefined || value === "") return "—";
  return formatDimension(value);
}

/**
 * A table on a 390pt screen.
 *
 * The web renders columns side by side; that is not available here and faking
 * it with a horizontal scroll makes every row unreadable. Instead each row
 * becomes a stacked record: the highest-priority column is the row's title, the
 * currency column is its figure, and the rest are labelled underneath. The
 * contract already ranks the columns, so this is a re-layout, not a guess.
 */
function DataTable({ widget, data, currency, disabled, onAction }: BodyProps) {
  const styles = useStyles(makeStyles);
  const parsed = dataTableDataSchema.safeParse(data);
  if (!parsed.success) return <Banner>This table didn’t match the expected shape.</Banner>;
  const table = parsed.data;
  const items = table.rows ?? [];
  if (!items.length) return <EmptyNote>{table.emptyMessage ?? "Nothing to show yet."}</EmptyNote>;

  const columns = table.columns;
  const primary = columns.find((column) => column.priority === "primary") ?? columns[0];
  const figure = columns.find((column) => column.type === "money" && column.key !== primary?.key);
  const rest = columns.filter((column) => column.key !== primary?.key && column.key !== figure?.key);
  const rowActions = table.rowActions ?? [];

  return (
    <View>
      {items.map((row, index) => {
        const record = row as Data;
        const id = str(record[table.rowIdKey ?? "id"], String(index));
        return (
          <View key={id}>
            {index ? <Divider /> : null}
            <View style={styles.tableRecord}>
              <View style={styles.amountRow}>
                <Type size="control" weight="medium" color="ink" style={{ flex: 1 }} numberOfLines={2}>
                  {primary ? cellText(record, primary.key, primary.type, readRowCurrency(record, primary.currencyKey, currency)) : id}
                </Type>
                {figure ? <Money value={cellText(record, figure.key, "money", readRowCurrency(record, figure.currencyKey, currency))} size="control" /> : null}
              </View>
              {rest.length ? (
                <View style={styles.tableFields}>
                  {rest.map((column) => (
                    <View key={column.key} style={styles.tableField}>
                      <Type size="meta" color="muted">{column.label}</Type>
                      <Type size="note" color="body" tabular={column.type === "number" || column.type === "percentage" || column.type === "money"}>
                        {cellText(record, column.key, column.type, readRowCurrency(record, column.currencyKey, currency))}
                      </Type>
                    </View>
                  ))}
                </View>
              ) : null}
              {rowActions.length ? (
                <View style={styles.actionRow}>
                  {rowActions.map((action) => (
                    <Button
                      key={action.id}
                      size="control"
                      variant={action.style === "danger" ? "danger" : "outline"}
                      disabled={disabled}
                      onPress={() => onAction(widget.id, action.action, { [action.payloadKey]: record[action.resourceKey] }, { markUsed: false })}
                    >
                      {action.label}
                    </Button>
                  ))}
                </View>
              ) : null}
            </View>
          </View>
        );
      })}
    </View>
  );
}

function AnalysisTable({ data, currency }: BodyProps) {
  const styles = useStyles(makeStyles);
  const columns = rows(data.columns);
  const items = rows(data.rows);
  if (!items.length) return <EmptyNote>Nothing to show for this analysis.</EmptyNote>;
  const tableCurrency = str(data.currency, currency);
  const primary = columns[0];
  const rest = columns.slice(1);

  return (
    <View>
      {items.map((row, index) => (
        <View key={index}>
          {index ? <Divider /> : null}
          <View style={styles.tableRecord}>
            {primary ? (
              <Type size="control" weight="medium" color="ink">
                {cellText(row, str(primary.key), str(primary.type) || undefined, tableCurrency)}
              </Type>
            ) : null}
            <View style={styles.tableFields}>
              {rest.map((column) => (
                <View key={str(column.key)} style={styles.tableField}>
                  <Type size="meta" color="muted">{str(column.label, str(column.key))}</Type>
                  <Type size="note" color="body" tabular>
                    {cellText(row, str(column.key), str(column.type) || undefined, tableCurrency)}
                  </Type>
                </View>
              ))}
            </View>
          </View>
        </View>
      ))}
    </View>
  );
}

// ── Money summaries ──────────────────────────────────────────────────────────

function FinancialSummary({ data, currency }: BodyProps) {
  const styles = useStyles(makeStyles);
  const breakdown = rows(data.breakdown);
  const total = num(data.amountMinor);
  return (
    <View style={styles.body}>
      <View>
        <Money value={formatMoney(total, currency)} size="display" />
        <Type size="note" color="muted" style={{ marginTop: space.tight }}>
          {[str(data.periodTitle) || str(data.period), data.count != null ? plural(data.count, "transaction") : null].filter(Boolean).join(" · ")}
        </Type>
      </View>
      {data.description ? <Type size="note" color="body">{str(data.description)}</Type> : null}
      {breakdown.length ? (
        <View style={{ gap: space.snug }}>
          {breakdown.map((entry, index) => {
            const value = num(readAmountMinor(entry));
            const share = total > 0 ? (value / total) * 100 : 0;
            return (
              <View key={index} style={{ gap: space.tight }}>
                <View style={styles.amountRow}>
                  <Type size="note" color="body" style={{ flex: 1 }} numberOfLines={1}>{formatDimension(readField(entry, "label", "category"))}</Type>
                  <Money value={formatMoney(value, currency)} size="note" weight="medium" />
                </View>
                <Meter percent={share} />
              </View>
            );
          })}
        </View>
      ) : null}
    </View>
  );
}

function BudgetProgress({ data, currency }: BodyProps) {
  const styles = useStyles(makeStyles);
  const percent = num(data.percentUsed);
  const over = percent > 100;
  return (
    <View style={styles.body}>
      <View style={styles.amountRow}>
        <Money value={formatMoney(data.spentMinor, currency)} size="title" color={over ? "out" : "ink"} />
        <Type size="note" color="muted">of {formatMoney(data.amountMinor, currency)}</Type>
      </View>
      <Meter percent={percent} tone={over ? "out" : "secondary"} />
      <Type size="note" color={over ? "danger" : "muted"}>
        {over
          ? `${formatMoney(Math.abs(num(data.remainingMinor)), currency)} over budget`
          : `${formatMoney(data.remainingMinor, currency)} left · ${formatCount(percent, 0)}% used`}
      </Type>
    </View>
  );
}

function GoalProgress({ data, currency }: BodyProps) {
  const styles = useStyles(makeStyles);
  return (
    <View style={styles.body}>
      <View style={styles.amountRow}>
        <Money value={formatMoney(data.currentMinor, currency)} size="title" color="in" />
        <Type size="note" color="muted">of {formatMoney(data.targetMinor, currency)}</Type>
      </View>
      <Meter percent={num(data.percentComplete)} />
      <Type size="note" color="muted">
        {formatMoney(data.remainingMinor, currency)} to go · {formatCount(num(data.percentComplete), 0)}% saved
      </Type>
    </View>
  );
}

function AvoidableExpenses({ widget, data, currency, disabled, onAction }: BodyProps) {
  const styles = useStyles(makeStyles);
  const items = rows(data.transactions);
  return (
    <View style={styles.body}>
      <View>
        <Type size="meta" weight="semibold" color="muted">POTENTIAL MONTHLY SAVING</Type>
        <Money value={formatMoney(data.potentialMinor, currency)} size="title" color="in" style={{ marginTop: space.tight }} />
      </View>
      {items.length ? (
        <View style={{ gap: space.base }}>
          {items.map((row, index) => {
            const id = str(row.transactionId ?? row.id, String(index));
            const nature = str(row.spendNature);
            return (
              <View key={id} style={{ gap: space.snug }}>
                <View style={styles.amountRow}>
                  <Type size="note" color="body" style={{ flex: 1 }} numberOfLines={1}>{str(row.merchant, "Expense")}</Type>
                  <Money value={formatMoney(row.amountMinor, currency)} size="note" weight="medium" color="out" />
                </View>
                {/* Marked `markUsed: false` — labelling one row must not retire
                    every other row's control on the same card. */}
                <View style={styles.optionWrap}>
                  {["essential", "discretionary"].map((choice) => (
                    <Chip
                      key={choice}
                      label={choice === "essential" ? "Essential" : "Could skip"}
                      selected={nature === choice}
                      disabled={disabled}
                      onPress={() => onAction(widget.id, widgetActionIds.set_spend_nature, { transactionId: id, spendNature: choice }, { markUsed: false })}
                    />
                  ))}
                </View>
              </View>
            );
          })}
        </View>
      ) : <EmptyNote>Nothing looks avoidable in this period.</EmptyNote>}
    </View>
  );
}

// ── Calculators ──────────────────────────────────────────────────────────────

function LoanCalculator({ widget, data, disabled, pending, onAction }: BodyProps) {
  const styles = useStyles(makeStyles);
  const currency = str(data.currency, "INR");
  const [principal, setPrincipal] = useState(data.principalMinor != null ? String(num(data.principalMinor) / 100) : "");
  const [rate, setRate] = useState(data.annualRatePercent != null ? String(num(data.annualRatePercent)) : "");
  const [tenure, setTenure] = useState(data.tenureMonths != null ? String(num(data.tenureMonths)) : "");
  const [prepayment, setPrepayment] = useState(data.prepaymentMinor != null ? String(num(data.prepaymentMinor) / 100) : "");

  const principalMinor = parseAmountToMinor(principal);
  const annualRatePercent = parseNumber(rate);
  const tenureMonths = parseNumber(tenure);
  const prepaymentMinor = prepayment.trim() ? parseAmountToMinor(prepayment) : 0;
  const ready = principalMinor !== null && annualRatePercent !== null && tenureMonths !== null && tenureMonths > 0;
  const result = data.result as Data | null;

  return (
    <View style={styles.body}>
      <View style={styles.fieldGrid}>
        <View style={styles.fieldHalf}><FieldLabel>Principal</FieldLabel><Field value={principal} onChangeText={setPrincipal} keyboardType="decimal-pad" placeholder="0" editable={!disabled} /></View>
        <View style={styles.fieldHalf}><FieldLabel>Rate % p.a.</FieldLabel><Field value={rate} onChangeText={setRate} keyboardType="decimal-pad" placeholder="9.5" editable={!disabled} /></View>
        <View style={styles.fieldHalf}><FieldLabel>Months</FieldLabel><Field value={tenure} onChangeText={setTenure} keyboardType="number-pad" placeholder="240" editable={!disabled} /></View>
        <View style={styles.fieldHalf}><FieldLabel>Prepayment</FieldLabel><Field value={prepayment} onChangeText={setPrepayment} keyboardType="decimal-pad" placeholder="Optional" editable={!disabled} /></View>
      </View>
      <Button
        onPress={() => onAction(widget.id, widgetActionIds.calculate_loan_scenario, { principalMinor, annualRatePercent, tenureMonths, prepaymentMinor }, { markUsed: false })}
        disabled={disabled || !ready}
        busy={pending}
        block
      >
        Calculate
      </Button>
      {result ? (
        <Rows items={[
          ["Monthly EMI", formatMoney(result.emiMinor ?? result.monthlyPaymentMinor, currency)],
          ["Total interest", formatMoney(result.totalInterestMinor, currency)],
          ["Total paid", formatMoney(result.totalPaidMinor, currency)],
          ...(result.monthsSaved != null ? [["Months saved", formatCount(result.monthsSaved, 0)] as [string, React.ReactNode]] : []),
          ...(result.interestSavedMinor != null ? [["Interest saved", formatMoney(result.interestSavedMinor, currency)] as [string, React.ReactNode]] : []),
        ]} />
      ) : null}
    </View>
  );
}

function InvestmentProjection({ widget, data, disabled, pending, onAction }: BodyProps) {
  const styles = useStyles(makeStyles);
  const currency = str(data.currency, "INR");
  const [monthly, setMonthly] = useState(String(num(data.monthlyContributionMinor) / 100 || ""));
  const [current, setCurrent] = useState(data.currentValueMinor != null ? String(num(data.currentValueMinor) / 100) : "");
  const [rate, setRate] = useState(data.annualReturnPercent != null ? String(num(data.annualReturnPercent)) : "12");
  const [years, setYears] = useState(data.years != null ? String(num(data.years)) : "10");

  const monthlyContributionMinor = parseAmountToMinor(monthly);
  const currentValueMinor = current.trim() ? parseAmountToMinor(current) : 0;
  const annualReturnPercent = parseNumber(rate);
  const projectionYears = parseNumber(years);
  const ready = monthlyContributionMinor !== null && annualReturnPercent !== null && projectionYears !== null && projectionYears > 0;
  const result = data.result as Data | null;

  return (
    <View style={styles.body}>
      <View style={styles.fieldGrid}>
        <View style={styles.fieldHalf}><FieldLabel>Monthly</FieldLabel><Field value={monthly} onChangeText={setMonthly} keyboardType="decimal-pad" placeholder="5000" editable={!disabled} /></View>
        <View style={styles.fieldHalf}><FieldLabel>Already saved</FieldLabel><Field value={current} onChangeText={setCurrent} keyboardType="decimal-pad" placeholder="0" editable={!disabled} /></View>
        <View style={styles.fieldHalf}><FieldLabel>Return % p.a.</FieldLabel><Field value={rate} onChangeText={setRate} keyboardType="decimal-pad" editable={!disabled} /></View>
        <View style={styles.fieldHalf}><FieldLabel>Years</FieldLabel><Field value={years} onChangeText={setYears} keyboardType="number-pad" editable={!disabled} /></View>
      </View>
      <Button
        onPress={() => onAction(widget.id, widgetActionIds.calculate_investment_scenario, { monthlyContributionMinor, currentValueMinor, annualReturnPercent, years: projectionYears }, { markUsed: false })}
        disabled={disabled || !ready}
        busy={pending}
        block
      >
        Project
      </Button>
      {result ? (
        <Rows items={[
          ["Projected value", formatMoney(result.futureValueMinor ?? result.projectedMinor, currency)],
          ["You contribute", formatMoney(result.totalContributedMinor, currency)],
          ["Growth", formatMoney(result.totalGrowthMinor ?? result.gainMinor, currency)],
        ]} />
      ) : null}
    </View>
  );
}

function ScenarioAnalysis({ data }: BodyProps) {
  const styles = useStyles(makeStyles);
  const currency = str(data.currency, "INR");
  const affordable = data.affordable_now === true;
  return (
    <View style={styles.body}>
      <Pill tone={affordable ? "in" : "out"}>{affordable ? "Affordable now" : "Not yet affordable"}</Pill>
      <Rows items={[
        ...(data.purchase_minor != null ? [["Purchase", formatMoney(data.purchase_minor, currency)] as [string, React.ReactNode]] : []),
        ...(data.reserve_required_minor != null ? [["Reserve to keep", formatMoney(data.reserve_required_minor, currency)] as [string, React.ReactNode]] : []),
        ...(data.available_after_reserve_minor != null ? [["Available", formatMoney(data.available_after_reserve_minor, currency)] as [string, React.ReactNode]] : []),
        ...(data.gap_minor != null ? [["Shortfall", formatMoney(data.gap_minor, currency)] as [string, React.ReactNode]] : []),
        ...(data.monthly_surplus_minor != null ? [["Monthly surplus", formatMoney(data.monthly_surplus_minor, currency)] as [string, React.ReactNode]] : []),
        ...(data.months_to_goal != null ? [["Months to afford", formatCount(data.months_to_goal, 0)] as [string, React.ReactNode]] : []),
      ]} />
      {data.rule ? <Type size="meta" color="muted">{str(data.rule)}</Type> : null}
    </View>
  );
}

function LoanStrategy({ data }: BodyProps) {
  const styles = useStyles(makeStyles);
  const loans = rows(data.loans);
  if (!loans.length) return <EmptyNote>No loans on file to compare.</EmptyNote>;
  return (
    <View>
      {loans.map((loan, index) => (
        <View key={index}>
          {index ? <Divider /> : null}
          <View style={styles.tableRecord}>
            <View style={styles.amountRow}>
              <Type size="control" weight="medium" color="ink" style={{ flex: 1 }}>{str(loan.name, `Loan ${index + 1}`)}</Type>
              <Money value={formatMoney(loan.balanceMinor ?? loan.outstandingMinor, str(loan.currency, "INR"))} size="control" />
            </View>
            <View style={styles.tableFields}>
              {loan.annualRatePercent != null ? (
                <View style={styles.tableField}><Type size="meta" color="muted">Rate</Type><Type size="note" color="body" tabular>{formatCount(loan.annualRatePercent, 2)}%</Type></View>
              ) : null}
              {loan.priority != null ? (
                <View style={styles.tableField}><Type size="meta" color="muted">Priority</Type><Type size="note" color="body">{formatDimension(loan.priority)}</Type></View>
              ) : null}
            </View>
          </View>
        </View>
      ))}
    </View>
  );
}

// ── Review surfaces ──────────────────────────────────────────────────────────

function ReconciliationReview({ data, currency }: BodyProps) {
  const styles = useStyles(makeStyles);
  const incoming = (data.incoming ?? {}) as Data;
  const existing = (data.existing ?? {}) as Data;
  const score = num(data.score);
  return (
    <View style={styles.body}>
      <Pill tone={score >= 0.8 ? "secondary" : "muted"}>Match confidence {formatCount(score * 100, 0)}%</Pill>
      {[["Incoming", incoming], ["Already recorded", existing]].map(([label, record]) => (
        <View key={label as string} style={styles.compare}>
          <Type size="meta" weight="semibold" color="muted">{String(label).toUpperCase()}</Type>
          <View style={[styles.amountRow, { marginTop: space.tight }]}>
            <Type size="note" color="body" style={{ flex: 1 }} numberOfLines={1}>{str((record as Data).merchant, "—")}</Type>
            <Money value={formatMoney((record as Data).amountMinor, currency)} size="note" weight="medium" />
          </View>
          <Type size="meta" color="muted">{formatDay(str((record as Data).transactionAt).slice(0, 10))}</Type>
        </View>
      ))}
    </View>
  );
}

function ImportReview({ data }: BodyProps) {
  const styles = useStyles(makeStyles);
  return (
    <View style={styles.body}>
      {data.idempotentReplay ? <Banner tone="attention">This file was imported before. Nothing new will be added.</Banner> : null}
      <Rows items={[
        ["Rows found", formatCount(data.total, 0)],
        ["Ready to import", formatCount(data.highConfidence, 0)],
        ["Need review", formatCount(data.needsReview, 0)],
        ["Duplicates", formatCount(data.duplicates, 0)],
      ]} />
    </View>
  );
}

function AgentActivity({ data }: BodyProps) {
  const styles = useStyles(makeStyles);
  const steps = rows(data.steps);
  return (
    <View style={styles.body}>
      <Type size="meta" color="muted">{str(data.engine)} · {str(data.model)} · {formatDuration(data.totalMs)}</Type>
      {steps.map((step, index) => (
        <View key={index} style={styles.amountRow}>
          <Type size="note" color="body" style={{ flex: 1 }} numberOfLines={1}>{str(step.label)}</Type>
          <Type size="meta" color="muted" tabular>{formatDuration(step.durationMs)}</Type>
        </View>
      ))}
    </View>
  );
}

// ── Charts ───────────────────────────────────────────────────────────────────

function DataChart({ data, currency }: BodyProps) {
  const parsed = dataChartDataSchema.safeParse(data);
  if (!parsed.success) return <Banner>This chart didn’t match the expected shape.</Banner>;
  const chart = parsed.data;
  if (!chart.rows?.length) return <EmptyNote>{chart.emptyMessage ?? "No data to plot."}</EmptyNote>;
  return (
    <View style={{ paddingHorizontal: space.gutter, paddingBottom: space.gutter }}>
      <ChartView
        kind={chart.chartType}
        rows={chart.rows as Data[]}
        xKey={chart.xAxis.key}
        series={chart.series.map((entry) => ({
          key: entry.key,
          label: entry.label,
          // A series that says it is money but names no currency still needs
          // one, or the axis prints paise.
          currency: entry.currency ?? (isMoneyValueType(entry.valueType) ? currency : undefined),
          valueType: entry.valueType ?? undefined,
        }))}
      />
    </View>
  );
}

function DataVisualization({ data, currency }: BodyProps) {
  const parsed = dataVisualizationDataSchema.safeParse(data);
  if (!parsed.success) return <Banner>This visualization didn’t match the expected shape.</Banner>;
  const visual = parsed.data;
  const views = visual.views ?? [];
  if (!views.length) return <EmptyNote>{visual.emptyMessage ?? "Nothing to plot."}</EmptyNote>;

  return (
    <View style={{ gap: space.gutter, paddingHorizontal: space.gutter, paddingBottom: space.gutter }}>
      {views.map((view) => {
        const dataset = (visual.datasets as Record<string, unknown>)[view.dataset];
        const plotted = rows(dataset);
        if (!plotted.length) return <EmptyNote key={view.id}>{visual.emptyMessage ?? "No data to plot."}</EmptyNote>;
        const encoding = view.encoding as unknown as Data;
        const arc = view.mark === "arc";
        // Which channels a mark actually plots, and whether its measure is
        // money in minor units, are questions with one right answer — so they
        // are answered in the generated readers both clients share rather than
        // here. A pie is the case that makes it matter: it has no axes, and
        // reading `x`/`y` for one plots fields that are not there.
        const { category, measure } = readVisualChannels(view.mark, encoding);
        return (
          <View key={view.id} style={{ gap: space.snug }}>
            {views.length > 1 ? <Type size="note" weight="semibold" color="ink">{view.title}</Type> : null}
            <ChartView
              kind={view.mark === "line" ? "line" : view.mark === "area" ? "area" : view.mark === "point" ? "scatter" : arc ? "pie" : view.mark === "rect" ? "heatmap" : "bar"}
              rows={plotted}
              xKey={category.field}
              yKey={arc ? undefined : category.field}
              groupKey={undefined}
              series={[{ key: measure.field, label: measure.title, currency: measure.money ? currency : undefined }]}
            />
          </View>
        );
      })}
    </View>
  );
}

// ── Dispatch ─────────────────────────────────────────────────────────────────

const BODIES: Partial<Record<Widget["type"], (props: BodyProps) => React.ReactNode>> = {
  [widgetTypeIds.agent_activity]: AgentActivity,
  [widgetTypeIds.category_selector]: CategorySelector,
  [widgetTypeIds.subcategory_selector]: SubcategorySelector,
  [widgetTypeIds.transaction_type_selector]: TransactionTypeSelector,
  [widgetTypeIds.account_selector]: AccountSelector,
  [widgetTypeIds.taxonomy_editor]: TaxonomyEditor,
  [widgetTypeIds.confirmation_card]: ConfirmationCard,
  [widgetTypeIds.transaction_preview]: TransactionPreview,
  [widgetTypeIds.transaction_edit]: TransactionEdit,
  [widgetTypeIds.transaction_list]: TransactionList,
  [widgetTypeIds.data_table]: DataTable,
  [widgetTypeIds.analysis_table]: AnalysisTable,
  [widgetTypeIds.data_chart]: DataChart,
  [widgetTypeIds.data_visualization]: DataVisualization,
  [widgetTypeIds.financial_summary]: FinancialSummary,
  [widgetTypeIds.budget_progress]: BudgetProgress,
  [widgetTypeIds.goal_progress]: GoalProgress,
  [widgetTypeIds.avoidable_expenses]: AvoidableExpenses,
  [widgetTypeIds.loan_calculator]: LoanCalculator,
  [widgetTypeIds.loan_strategy]: LoanStrategy,
  [widgetTypeIds.investment_projection]: InvestmentProjection,
  [widgetTypeIds.scenario_analysis]: ScenarioAnalysis,
  [widgetTypeIds.reconciliation_review]: ReconciliationReview,
  [widgetTypeIds.import_review]: ImportReview,
};

/** Which widgets carry their own heading rather than the card's. */
const HEADERLESS = new Set<Widget["type"]>([widgetTypeIds.insight_card]);

export const WidgetView = memo(function WidgetView({ widget, currency, disabled, spent, pending, onAction }: {
  widget: Widget;
  currency: string;
  disabled: boolean;
  spent: boolean;
  pending: boolean;
  onAction: WidgetActionHandler;
}) {
  const data = widget.data as Data;
  const Body = BODIES[widget.type];
  const props: BodyProps = { widget, data, currency, disabled, spent, pending, onAction };
  const styles = useStyles(makeStyles);
  const color = useTheme();

  // The insight card is prose in a box, and giving it a header would print its
  // title twice.
  if (widget.type === widgetTypeIds.insight_card) {
    const caution = str(data.tone) === "caution";
    return (
      <Card style={caution ? { borderColor: color.attention, backgroundColor: color.attentionTint } : undefined}>
        <View style={[styles.body, { padding: space.gutter }]}>
          {data.eyebrow ? <Type size="meta" weight="semibold" color={caution ? "attention" : "secondary"} style={{ letterSpacing: 0.8 }}>{str(data.eyebrow).toUpperCase()}</Type> : null}
          <Type size="body" weight="semibold" color="ink">{str(data.title)}</Type>
          <Type size="note" color="body">{str(data.body)}</Type>
        </View>
        <DeclaredActionsFooter {...props} />
      </Card>
    );
  }

  return (
    <Card>
      {HEADERLESS.has(widget.type) ? null : (
        <CardHeader
          title={str(data.title, formatDimension(widget.type))}
          body={typeof data.body === "string" ? data.body : null}
          caution={str(data.tone) === "caution"}
          trailing={<Receipt widget={widget} />}
        />
      )}
      {Body ? <Body {...props} /> : <EmptyNote>This card isn’t supported in this version of the app yet.</EmptyNote>}
      <DeclaredActionsFooter {...props} />
    </Card>
  );
});

function DeclaredActionsFooter(props: BodyProps) {
  const styles = useStyles(makeStyles);
  if (!(props.widget.actions ?? []).length) return null;
  // A retired card keeps its receipt, not its buttons.
  if (props.spent) return null;
  return (
    <View style={styles.footer}>
      <DeclaredActions widget={props.widget} disabled={props.disabled} pending={props.pending} onAction={props.onAction} />
    </View>
  );
}

const makeStyles = (color: Palette) => StyleSheet.create({
  body: { gap: space.base, paddingHorizontal: space.gutter, paddingVertical: space.base },
  footer: { paddingHorizontal: space.gutter, paddingBottom: space.gutter, paddingTop: space.tight },
  actionRow: { flexDirection: "row", flexWrap: "wrap", gap: space.snug },
  amountRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: space.base },
  detailList: { gap: space.snug },
  detailRow: { flexDirection: "row", alignItems: "flex-start", justifyContent: "space-between", gap: space.base },
  detailValue: { flexShrink: 1, alignItems: "flex-end" },
  optionWrap: { flexDirection: "row", flexWrap: "wrap", gap: space.snug },
  receipt: { paddingHorizontal: space.snug, paddingVertical: 3, borderRadius: radius.chip, backgroundColor: color.secondaryTint },
  meterTrack: { height: 6, borderRadius: 3, backgroundColor: color.sunken, overflow: "hidden" },
  meterFill: { height: "100%", borderRadius: 3 },
  listRow: { flexDirection: "row", alignItems: "center", gap: space.base, paddingHorizontal: space.gutter, paddingVertical: space.base },
  tableRecord: { gap: space.snug, paddingHorizontal: space.gutter, paddingVertical: space.base },
  tableFields: { flexDirection: "row", flexWrap: "wrap", gap: space.base },
  tableField: { minWidth: 90 },
  fieldGrid: { flexDirection: "row", flexWrap: "wrap", gap: space.base },
  fieldHalf: { flexGrow: 1, flexBasis: "45%" },
  compare: { padding: space.base, borderRadius: radius.control, backgroundColor: color.sunken },
  spent: { paddingHorizontal: space.gutter, paddingVertical: space.base },
});
