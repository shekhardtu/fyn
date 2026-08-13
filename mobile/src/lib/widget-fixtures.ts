import { widgetTypeIds, type Widget } from "@/lib/protocol";

/**
 * One representative payload per widget type.
 *
 * The agent decides which widgets to emit, and for a good number of the
 * twenty-five it simply never does in ordinary use — the calculators and the
 * reconciliation surfaces need a conversation that is hard to provoke on
 * demand. That left most of the renderer shipped but never once drawn.
 *
 * These fixtures are shaped to match the generated contracts, so the gallery
 * screen exercises the same code path a real payload takes, including the Zod
 * parse. They are for the gallery only and never reach a real conversation.
 */

function widget(type: Widget["type"], data: Record<string, unknown>, actions: Widget["actions"] = []): Widget {
  return { id: `fixture-${type}`, type, version: 1, data, actions } as Widget;
}

const CATEGORIES = [
  { id: "cat-food", slug: "food", label: "Food", icon: "utensils" },
  { id: "cat-transport", slug: "transport", label: "Transport", icon: "car" },
  { id: "cat-bills", slug: "bills", label: "Bills", icon: "receipt" },
  { id: "cat-shopping", slug: "shopping", label: "Shopping", icon: "shopping-bag" },
];

export const WIDGET_FIXTURES: Widget[] = [
  widget(widgetTypeIds.insight_card, {
    eyebrow: "Worth knowing",
    title: "Your food spend is up 38% this month",
    body: "₹6,420 so far against ₹4,650 by this point in July. Two delivery orders account for most of it.",
    tone: "neutral",
  }),

  widget(widgetTypeIds.confirmation_card, {
    title: "Record this expense?",
    draftId: "draft-1",
    amountMinor: 124500,
    currency: "INR",
    merchant: "BigBasket",
    transactionType: "expense",
    transactionAt: "2026-08-12T18:30:00Z",
    category: "Food",
    subcategory: "Groceries",
    location: "Indiranagar",
    spendNature: "essential",
    tags: ["weekly"],
    status: "draft",
    inferredFields: ["category", "spendNature"],
  }, [
    { id: "commit", label: "Save it", action: "commit_transaction", style: "primary", payload: { draftId: "draft-1" } },
    { id: "edit", label: "Edit", action: "edit_transaction", style: "secondary", payload: { draftId: "draft-1" } },
  ]),

  widget(widgetTypeIds.transaction_preview, {
    title: "Third Wave Coffee",
    transactionId: "txn-1",
    amountMinor: 45000,
    currency: "INR",
    transactionAt: "2026-08-13T07:10:00Z",
    status: "Saved",
    sourceCount: 2,
    transactionType: "expense",
    category: "Food",
    subcategory: "Coffee",
  }, [
    { id: "edit", label: "Edit", action: "edit_saved_transaction", style: "secondary", payload: { transactionId: "txn-1" } },
    { id: "remove", label: "Remove", action: "request_remove_transaction", style: "ghost", payload: { transactionId: "txn-1" } },
  ]),

  widget(widgetTypeIds.transaction_edit, {
    title: "Edit this transaction",
    transactionId: "txn-1",
    amountMinor: 45000,
    currency: "INR",
    merchant: "Third Wave Coffee",
    transactionAt: "2026-08-13T07:10:00Z",
    transactionType: "expense",
    location: "Indiranagar",
    tags: [],
    categories: CATEGORIES,
  }),

  widget(widgetTypeIds.category_selector, {
    title: "Where should I categorize this?",
    body: "Ranked from your own history by time of day.",
    draftId: "draft-1",
    suggestions: [{ ...CATEGORIES[0], score: 0.47, reasons: ["Your #1 category"] }],
    options: CATEGORIES,
    allowCreate: true,
  }, [{ id: "select", label: "Select category", action: "select_category", style: "secondary", payload: { draftId: "draft-1" } }]),

  widget(widgetTypeIds.subcategory_selector, {
    title: "What type of food expense?",
    category: "Food",
    categoryId: "cat-food",
    draftId: "draft-1",
    suggestions: [{ id: "sub-coffee", slug: "coffee", label: "Coffee" }],
    options: [
      { id: "sub-coffee", slug: "coffee", label: "Coffee" },
      { id: "sub-groceries", slug: "groceries", label: "Groceries" },
      { id: "sub-dining", slug: "dining", label: "Dining out" },
    ],
    allowCreate: true,
  }),

  widget(widgetTypeIds.transaction_type_selector, {
    title: "Is this money in or out?",
    draftId: "draft-1",
    options: [
      { id: "expense", label: "Expense", transactionType: "expense", detail: "Money leaving" },
      { id: "income", label: "Income", transactionType: "income", detail: "Money arriving" },
      { id: "transfer", label: "Transfer", transactionType: "transfer", detail: "Between your accounts" },
    ],
  }),

  widget(widgetTypeIds.account_selector, {
    title: "Which account did this come from?",
    draftId: "draft-1",
    role: "source",
    options: [
      { id: "acc-hdfc", accountId: "acc-hdfc", label: "HDFC Savings", detail: "•••• 4821" },
      { id: "acc-icici", accountId: "acc-icici", label: "ICICI Current", detail: "•••• 9930" },
    ],
  }),

  widget(widgetTypeIds.taxonomy_editor, {
    operation: "create_category",
    name: "Pets",
    appliesToDraft: true,
    draftId: "draft-1",
  }),

  widget(widgetTypeIds.transaction_list, {
    title: "Recent transactions",
    body: "The last few things recorded.",
    transactions: [
      { id: "t1", merchant: "Swiggy", amountMinor: 92000, currency: "INR", transactionAt: "2026-08-11T20:00:00Z", category: "Food", transactionType: "expense" },
      { id: "t2", merchant: "Salary", amountMinor: 22000000, currency: "INR", transactionAt: "2026-08-01T09:00:00Z", category: null, transactionType: "income" },
      { id: "t3", merchant: "Uber", amountMinor: 28500, currency: "INR", transactionAt: "2026-08-06T08:15:00Z", category: "Transport", transactionType: "expense" },
    ],
  }),

  widget(widgetTypeIds.financial_summary, {
    title: "Spending · This month",
    amountMinor: 6914000,
    currency: "INR",
    count: 6,
    period: "Aug 01 – Aug 13",
    breakdown: [
      { id: "housing", label: "Housing", amount_minor: 4500000, count: 1, currency: "INR" },
      { id: "shopping", label: "Shopping", amount_minor: 1899000, count: 1, currency: "INR" },
      { id: "bills", label: "Bills", amount_minor: 246000, count: 1, currency: "INR" },
    ],
  }),

  widget(widgetTypeIds.budget_progress, {
    budgetId: "budget-1",
    title: "Food budget",
    body: "Monthly budget",
    amountMinor: 5000000,
    spentMinor: 3820000,
    remainingMinor: 1180000,
    percentUsed: 76.4,
    currency: "INR",
    categorySlug: "food",
  }),

  widget(widgetTypeIds.goal_progress, {
    goalId: "goal-1",
    title: "New car",
    body: "By December 2027",
    targetMinor: 30000000,
    currentMinor: 9500000,
    remainingMinor: 20500000,
    percentComplete: 31.7,
    currency: "INR",
  }, [{ id: "contribute", label: "Add to this goal", action: "contribute_goal", style: "primary", payload: { goalId: "goal-1", amountMinor: 500000 } }]),

  widget(widgetTypeIds.avoidable_expenses, {
    title: "Could you skip these?",
    body: "Marked from your own labels, not a judgement.",
    potentialMinor: 296000,
    currency: "INR",
    transactions: [
      { transactionId: "a1", merchant: "Swiggy", amountMinor: 92000, spendNature: "discretionary" },
      { transactionId: "a2", merchant: "PVR", amountMinor: 96000, spendNature: "" },
      { transactionId: "a3", merchant: "Spotify", amountMinor: 19900, spendNature: "essential" },
    ],
  }),

  widget(widgetTypeIds.loan_calculator, {
    title: "Home loan",
    body: "Deterministic — no model does this arithmetic.",
    principalMinor: 200000000,
    annualRatePercent: 9,
    tenureMonths: 240,
    prepaymentMinor: 0,
    currency: "INR",
    result: { emiMinor: 179950, totalInterestMinor: 231880000, totalPaidMinor: 431880000 },
  }),

  widget(widgetTypeIds.loan_strategy, {
    title: "Which loan to attack first",
    loans: [
      { name: "Personal loan", balanceMinor: 45000000, annualRatePercent: 14.5, currency: "INR", priority: "first" },
      { name: "Home loan", balanceMinor: 200000000, annualRatePercent: 9, currency: "INR", priority: "later" },
    ],
  }),

  widget(widgetTypeIds.investment_projection, {
    title: "Monthly SIP",
    monthlyContributionMinor: 1000000,
    currentValueMinor: 0,
    annualReturnPercent: 12,
    years: 10,
    currency: "INR",
    result: { futureValueMinor: 232339000, totalContributedMinor: 120000000, totalGrowthMinor: 112339000 },
  }),

  widget(widgetTypeIds.scenario_analysis, {
    title: "Can you afford the laptop?",
    currency: "INR",
    purchase_minor: 6000000,
    reserve_required_minor: 20000000,
    available_after_reserve_minor: 30000000,
    gap_minor: 0,
    monthly_surplus_minor: 4500000,
    months_to_goal: 0,
    affordable_now: true,
    rule: "Keeps a six-month reserve intact.",
  }),

  widget(widgetTypeIds.data_table, {
    title: "Largest expenses",
    columns: [
      { key: "merchant", label: "Merchant", type: "entity", align: "left", priority: "primary", currencyKey: null, secondaryKeys: [] },
      { key: "category", label: "Category", type: "text", align: "left", priority: "secondary", currencyKey: null, secondaryKeys: [] },
      { key: "transactionAt", label: "When", type: "datetime", align: "left", priority: "secondary", currencyKey: null, secondaryKeys: [] },
      { key: "amountMinor", label: "Amount", type: "money", align: "right", priority: "primary", currencyKey: "currency", secondaryKeys: [] },
    ],
    rows: [
      { id: "r1", merchant: "Rent", category: "Housing", transactionAt: "2026-08-01T12:00:00Z", amountMinor: 4500000, currency: "INR" },
      { id: "r2", merchant: "Croma", category: "Shopping", transactionAt: "2026-08-05T12:00:00Z", amountMinor: 1899000, currency: "INR" },
    ],
    rowIdKey: "id",
    rowActions: [
      { id: "edit", label: "Edit", action: "edit_saved_transaction", style: "secondary", resourceKey: "id", payloadKey: "transactionId", icon: null, capability: null },
    ],
    emptyMessage: "Nothing to show.",
  }),

  widget(widgetTypeIds.data_chart, {
    title: "Spend by month",
    chartType: "bar",
    rows: [
      { month: "2026-06", value: 5120000 },
      { month: "2026-07", value: 7364000 },
      { month: "2026-08", value: 6914000 },
    ],
    xAxis: { key: "month", label: "Month", type: "category" },
    yAxis: null,
    series: [{ key: "value", label: "Gross spend", valueType: "money", currency: "INR", groupKey: null }],
    labelKeys: [],
    emptyMessage: "No data to plot.",
    queryResult: null,
  }),

  widget(widgetTypeIds.data_visualization, {
    title: "Share of spending",
    datasets: {
      "dataset-0": [
        { label: "Housing", value: 4500000, basis_points: 6509 },
        { label: "Shopping", value: 1899000, basis_points: 2747 },
        { label: "Bills", value: 246000, basis_points: 356 },
        { label: "Health", value: 128000, basis_points: 185 },
      ],
    },
    views: [{
      id: "view-0",
      title: "Share of spending",
      dataset: "dataset-0",
      mark: "arc",
      height: 220,
      encoding: {
        x: null,
        y: null,
        color: { field: "label", type: "nominal", title: "Category", valueType: "category", sort: null },
        theta: { field: "value", type: "quantitative", title: "Gross spend", valueType: "money_minor", sort: null },
      },
    }],
    emptyMessage: "Nothing to plot.",
  }),

  widget(widgetTypeIds.analysis_table, {
    title: "Month on month",
    body: "Deterministic comparison transform.",
    currency: "INR",
    columns: [
      { key: "category", label: "Category", type: "text" },
      { key: "current", label: "This month", type: "money" },
      { key: "previous", label: "Last month", type: "money" },
    ],
    rows: [
      { category: "Housing", current: 4500000, previous: 4500000 },
      { category: "Food", current: 45000, previous: 545000 },
    ],
  }),

  widget(widgetTypeIds.reconciliation_review, {
    candidateId: "cand-1",
    title: "Are these the same payment?",
    score: 0.86,
    incoming: { merchant: "SWIGGY BANGALORE", amountMinor: 92000, transactionAt: "2026-08-11T20:02:00Z" },
    existing: { merchant: "Swiggy", amountMinor: 92000, transactionAt: "2026-08-11T20:00:00Z" },
    signals: { amount: "exact", merchant: "strong", time: "within 5 minutes" },
  }, [
    { id: "merge", label: "Same payment", action: "merge_reconciliation", style: "primary", payload: { candidateId: "cand-1" } },
    { id: "separate", label: "Keep separate", action: "separate_reconciliation", style: "secondary", payload: { candidateId: "cand-1" } },
  ]),

  widget(widgetTypeIds.import_review, {
    importId: "imp-1",
    status: "staged",
    total: 142,
    highConfidence: 128,
    needsReview: 11,
    duplicates: 3,
    idempotentReplay: false,
    title: "Statement ready to import",
  }, [{ id: "commit", label: "Import 128 rows", action: "commit_import", style: "primary", payload: { importId: "imp-1" } }]),

  widget(widgetTypeIds.agent_activity, {
    title: "Agno agent run",
    engine: "Agno harness",
    model: "gpt-5.6-luna → gpt-5.6-terra",
    totalMs: 13095.3,
    live: false,
    steps: [
      { id: "request", label: "Request received", status: "completed", durationMs: 0, cumulativeMs: 4.5 },
      { id: "router", label: "Produced a typed decision", status: "completed", durationMs: 8441.8, cumulativeMs: 8506.1 },
      { id: "validator", label: "Independent validation: approve", status: "completed", durationMs: 4504.4, cumulativeMs: 13010.6 },
    ],
  }),
];
