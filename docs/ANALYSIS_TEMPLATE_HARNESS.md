# Analysis template harness

> Legacy/hybrid-mode reference. The default `ANALYSIS_QUERY_MODE=sql` bypasses
> this finite AnalysisPlan/transform grammar and grammar-template replay. The
> Operator instead authors an arbitrary read-only PostgreSQL query graph using
> the complete tenant-governed schema exposed by `services/sql_analysis.py`.
> Set `ANALYSIS_QUERY_MODE=hybrid` only to restore the workflow below.

The finance copilot builds customer-defined analyses from shared, read-only
system primitives. It does not add a Python function or a bespoke prompt for
each question.

## Runtime flow

1. Hash the normalized question and look for an exact successful run in the
   authenticated user's scope. If its relative dates can be safely rebound,
   reuse the validated template before any model routing or embedding lookup.
2. For a financial ask, ONE agentic loop (the Operator turn) receives a
   dynamic toolset assembled per turn (`services/analysis_tools.py`): the top
   retrieved pool templates compile to `bind_template__*` tools that fill and
   run a validated stored analysis (retrieval is in-process hybrid ranking,
   `services/template_retrieval.py`: Okapi BM25 fused with cosine similarity
   over lazily backfilled embeddings), and `run_financial_analysis` lets the
   model author a novel `AnalysisPlan` that the harness validates, executes,
   and templatizes back into the pool. A failed governed check returns its
   details as the tool result so the model corrects the plan in the same loop.
   The model composes the final answer as free-form grounded markdown; every
   figure is verified against the tool results before it ships. Curated seed
   templates (`services/analysis_seeds.py`) cover the common metrics from
   first startup.
3. Produce one fully bound declarative plan and resolve its dates from the
   central finance-time policy.
5. Validate the plan against the semantic registry and read-only policy.
6. Replace dates, filter values, limits, timezone, service inputs, and
   presentation text with typed parameters to derive a value-free template.
7. Compare the complete template hash with the retrieved candidate. An exact
   structural match is reused; a mismatch is never executed and creates a new
   shared template.
8. Save or reuse the customer's association with that template.
9. Execute the bound plan only against the authenticated customer's records.
10. Verify the evidence/result contract and persist a readable user-scoped run
    trace.

## Sources of truth

| Concern | Authoritative source |
| --- | --- |
| Local date, timezone, relative periods, ambiguity | `services/finance_time.py` |
| Metrics, dimensions, filters, relationships | `services/semantic_registry.py` |
| Transform grammar and composition | `services/semantic.py` |
| Hybrid template retrieval and ranking | `services/template_retrieval.py` |
| Bind-tool compilation, fill materialization, tenancy guard | `services/template_binding.py` |
| Curated seed templates | `services/analysis_seeds.py` |
| Deterministic domain reads and dedicated analysis services | `services/intelligence.py` |
| Template parameterization, identity, matching, trace stages | `services/analysis_harness.py` |
| Shared value-free definition | `analysis_tool_templates` |
| Customer ownership and display metadata | `user_analysis_tools` |
| Bound values, outcome, and RCA trace | `analysis_tool_runs` |
| Customer question text | `messages` (runs store only a one-way replay hash) |

No template stores customer dates, categories, merchants, amounts, timezone,
or prose. No execution reads a user ID from model-authored input; the server
injects authenticated scope.

## What is system-defined

The maintained code surface is the small grammar, not every possible customer
analysis. Its current deterministic transforms are:

- `compare_totals`
- `period_change`
- `change_drivers`
- `share_of_total`
- `rank`
- `difference`
- `ratio`
- `prorate`
- `cumulative_sum`
- `moving_average`

These primitives and the harness require unit, policy, isolation, and contract
tests. A customer-created combination does not require a new coded function or
one-off test case.

## Acceptance prompts

Run these in Play against two users with different transaction data:

1. `Using my income and expenses from this month so far, project expenses to month-end at the same daily pace and tell me the projected savings. Keep recorded income unchanged.`
   - Expected: a `prorate` followed by `difference`; plain-language result;
     no capability refusal.
2. Repeat prompt 1 on the following local day.
   - Expected: same template ID, fresh date bindings and a new run ID.
3. Run prompt 1 as another user.
   - Expected: same template ID; different user-tool/run IDs and figures from
     only that user's records.
4. `Compare this month's food and transport spending and rank the larger one.`
   - Expected: the projection candidate is rejected as a mismatch and an
     appropriate comparison/rank template is selected or created.
5. `Show expenses from 04/05/2026 to 06/07/2026.`
   - Expected: ask whether the dates are day/month or month/day before reading
     records.
6. Near a UTC day boundary, ask `What did I spend yesterday?` from users in
   `Asia/Kolkata` and `America/New_York`.
   - Expected: each user's local yesterday.
7. Remove all recorded income for the period and run prompt 1.
   - Expected: a clear evidence-based answer about missing recorded income,
     never internal terms such as catalog, transform, semantic plan, or
     executor.

For prompts 1–4, inspect the run row. Its trace must contain
`intent_resolution`, `date_resolution`, `template_candidates`,
`template_match`, `parameter_binding`, `template_validation`,
`tool_execution`, and `result_verification`, each with a timestamp and readable
detail. `parameter_binding.values` is the DB source for RCA; the shared template
must contain parameter references only.
