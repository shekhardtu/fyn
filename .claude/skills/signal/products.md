# Signal product library — reference products for gap analysis

The single source for every comparison claim the product agent makes. It exists
for exactly one reason: "product X already does Y" is the easiest sentence in
this system to fabricate, and a fabricated competitor feature is worse than no
comparison at all.

Same rule as `patterns.md`: **a claim may only cite an entry in this file.**
Entries come from model knowledge through Jan 2026 until pinned; treat
`pinned: no` as "verify the mechanism before quoting specifics to the user".
`pinned` upgrades to a docs URL or commit permalink once checked.

Maintenance rule: adding a comparison without a reference is not allowed. When
the product agent wants to claim something this file does not contain, it adds
a referenced entry first — or reframes the suggestion so it stands on our own
run-log evidence instead.

---

## envelope-and-rule budgeting
- **Feature**: Spending is governed by user-declared rules and envelopes, not by post-hoc categorisation; the ledger enforces the plan.
- **Refs**: `actualbudget/actual` — envelope budgeting with rollover; `firefly-iii/firefly-iii` — rule engine over transactions.
- **Our position**: we categorise after the fact and have no declared-plan surface.
- pinned: no

## open personal-finance data model
- **Feature**: Accounts, holdings, and transactions modelled openly so third parties can extend the product.
- **Refs**: `maybe-finance/maybe` — full personal finance app, open schema.
- **Our position**: schema is ours and closed; `AnalysisToolTemplate` is the extension point instead.
- pinned: no

## semantic layer as the query contract
- **Feature**: Metrics are defined once in a governed semantic layer; every question compiles against it rather than against raw SQL.
- **Refs**: `cube-js/cube` — semantic layer with metric definitions; `lightdash/lightdash` — dbt metrics as the query surface.
- **Our position**: `AnalysisToolTemplate` + binder is a de-facto semantic layer, but metrics are not user-declarable.
- pinned: no

## text-to-SQL with retrieval over verified queries
- **Feature**: Natural-language questions are answered by retrieving previously verified queries and adapting them, rather than generating SQL from scratch each time.
- **Refs**: `vanna-ai/vanna` — RAG over a store of validated question/SQL pairs.
- **Our position**: closest neighbour to our template pool + hybrid retrieval; compare retrieval quality and the bind→verify gate.
- pinned: no

## notebook-as-product analytics
- **Feature**: Analysis is a versioned, reviewable artifact — a notebook or a code-defined report — not an ephemeral chat answer.
- **Refs**: `evidence-dev/evidence` — markdown+SQL reports as code; `metabase/metabase` — saved questions and dashboards as first-class objects.
- **Our position**: insights/dashboards direction is exactly this; check whether an insight is durable and reviewable yet.
- pinned: no

## agent-authored charts as durable objects
- **Feature**: A chart produced in conversation becomes a saved, re-runnable object rather than a screenshot of one moment.
- **Refs**: `metabase/metabase` — question → saved card → dashboard.
- **Our position**: "every chart is an insight" is the stated model; verify the persistence path exists.
- pinned: no
