# Overview information hierarchy

Reviewed 8 September 2026. This is a review of the interface against UX guidance, informed by the user's explicit requirement that **Your month in motion must be above the fold on mobile**. It is not a study of end users or an analysis of product usage.

## User tasks and order

The working assumption is that people open this expense overview to check spending, understand its direction, and record a transaction. The chart's first-screen placement is an explicit user requirement.

| Priority | User question or task | Interface placement |
| --- | --- | --- |
| Context | Which month am I looking at? | Persistent header with the selected month; chart defaults to that month's full period. |
| First screen | How much have I spent, and am I within my budget? | Compact spending summary, actual budget remaining or exceeded, and a separately labelled pace projection when relevant. Income and income minus expenses are supporting figures. |
| First screen | How is my month progressing? | Your month in motion immediately follows the summary on phones and sits alongside it on wide screens. The complete chart must clear the fixed action bar. |
| Persistent action | Record an entry or ask a question. | Add transaction is the primary action; Ask fyn is secondary. Both remain reachable on mobile. |
| Next | Which transactions explain this, and where did the money go? | Recent activity followed by spending categories. |
| Details | What are the limits and daily spending calculations? | Expandable Budget details. Actual overruns and important pace warnings remain visible in the summary. |
| Less frequent setup | Add an account, set a budget, or set a savings goal. | Accounts and planning below the financial information, with direct full-screen forms. |

## Findings and changes

**Emphasis must reflect task importance.** The large welcome sentence and three setup shortcuts occupied the first screen without answering a financial question. Spending now receives the strongest numeric emphasis and the chart stays immediately visible. Size, position, and grouping are tools for communicating importance. [NN/G: visual-design principles](https://www.nngroup.com/articles/principles-visual-design/)

**Defer details without hiding frequently needed information.** The full budget editor, category limits, and daily calculations sit behind a labelled disclosure. The trend chart, remaining budget, actual overruns, and relevant pace warnings stay outside it. NN/G stresses that frequently needed features must remain on the initial display and that the path to additional detail must be obvious. [NN/G: progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/)

**Make state and financial meaning explicit.** The month selector remains visible while scrolling. An empty month retains its zero totals and chart, with a notice and paths to the previous month and all records. Income minus expenses is not labelled as an available bank balance. Recorded account balances remain separate. The previous financial-health score was removed because an income-to-expense ratio alone does not establish financial health. These choices apply clear system status and familiar, accurate language. [NN/G: usability heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/)

**Reduce competing actions.** The same Add transaction flow serves desktop and mobile. Ask fyn is secondary, and setup actions no longer compete with daily information near the top. Contextual chat actions prepare an editable prompt; the user decides when to send it. This follows the same principle of keeping the primary display focused on important options. [NN/G: progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/)

**Make routine writes direct.** Budget, goal, contribution, and account actions now open forms with explicit validation and save results. They require no chat thread or model response. Ask fyn remains the optional advice action with an editable prompt. Budget forms prefill existing limits and explain that they recur monthly. Goal cards expose editing and savings entry. Account forms distinguish available balances from money owed and explain that a manually recorded account does not connect to a bank.

**Use the currency the user chose.** New transaction and account forms have searchable currency selectors. The transaction API previously always used the profile currency; it now accepts the entry's currency, defaulting to the profile only when omitted. Saved transactions display their stored currency during editing. Selecting a different entry currency does not change the profile or perform conversion, and the form explains its exclusion from default-currency overview totals. Budgets and new goals use the profile currency; existing goals retain their recorded currency.

**Use comfortable touch targets.** Main actions are 48 CSS pixels high; chart range controls are at least 44 by 44. WCAG's enhanced target-size criterion uses 44 by 44 CSS pixels; its AA minimum is 24 by 24 with exceptions. These are target-size decisions, not a claim that the whole application has been audited for WCAG conformance. [W3C: enhanced target size](https://www.w3.org/WAI/WCAG22/Understanding/target-size-enhanced.html), [W3C: minimum target size](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)

## Verification and remaining questions

The full-screen entry applies the same hierarchy to a focused task: select a type, enter the amount, then add context. Expense and Income are directly selectable; the other supported types remain in More. The amount has the strongest emphasis, category and subcategory are adjacent, and Location and Spend nature sit under More details. Required and optional labels make the minimum effort clear. Precise device coordinates are available within location details instead of occupying the primary form. These choices apply the visual hierarchy and progressive disclosure guidance above.

The entry header and save bar remain outside the scrolling fields. Field validation brings focus to the invalid input, and failed-save or discard feedback scrolls into view. Collapsing optional details preserves their values; changing to a non-expense type clears expense-only classification. The full-screen variant shares the existing form state and submission contract with the ledger editor and conversation form.

The browser regression checks cover the complete chart fitting between the persistent header and action bar at 360 × 740, 375 × 667, and 390 × 844, plus the desktop layout. The plot height adapts to shorter phone screens. Checks also cover an empty month, month switching and refresh, full-screen entry, and contextual chat prompts. Component checks distinguish actual budget overruns, projected overruns, completed months, and category-only budgets.

Entry checks include light and dark layouts, touch input sizes, optional fields, transaction-type changes, and a 390 × 420 viewport with the save action and error feedback still visible. A reduced viewport tests layout constraints; native keyboard and safe-area behaviour still require an actual-device smoke test.

Direct-form checks exercise budget creation and editing, goals and savings contributions, account balances, reloadable form URLs, and saved values appearing on the overview at phone and desktop widths. Backend checks verify persistence, validation, tenant and taxonomy ownership, duplicate protection, retry-safe savings contributions, and currency isolation in overview totals. Savings contributions track progress; they neither move funds nor create expenses. Accounts are manual records with an initial balance snapshot.

Automated checks establish layout and interaction behavior. They do not establish whether users understand or prefer the hierarchy. A later usability check should ask representative users to find the selected month, spending total, budget status, and trend; add an entry; and recover from an empty month. Observe whether people confuse income minus expenses or budget remaining with money available in their bank account.
