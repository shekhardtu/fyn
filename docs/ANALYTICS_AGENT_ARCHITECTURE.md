# Governed conversational analytics architecture

## Why a single text-to-SQL agent is insufficient

Accurate conversational analytics is a state, semantics, compilation, and
verification problem. A larger model can improve planning, but it cannot repair
an execution contract that cannot express the user's condition. The production
failure `And other than Food?` illustrates this: routing inferred a category
ranking and the validator noticed the missing exclusion, but the low-latency
query contract had no negative category filter.

Real-world benchmarks remain difficult. Spider 2.0 reports low success on
enterprise workflows with large schemas, and BIRD-Interact reports much lower
success for interactive tasks than traditional single-turn benchmarks. The
product therefore optimizes for *verified correct answers and useful
abstention*, not maximum answer rate.

## Target data flow

```text
User message
    |
    v
Conversation resolver
  - last five messages
  - active analysis state
  - active row/result scope
  - active mutation workflow
    |
    v
Self-contained analytical objective + structured delta
    |
    v
Semantic linker
  - relevant entities and relationship paths
  - metrics, dimensions, types and allowed operators
  - retrieved verified questions/plans
    |
    v
Candidate plan generator
  - one fast plan for simple requests
  - multiple independent plans for complex/high-risk requests
    |
    v
Deterministic plan validator and compiler
  - tenant scope and soft deletion
  - grain/fanout and relationship validation
  - type/operator/date/currency validation
  - query cost and row limits
  - parameterized SQLAlchemy SELECT only
    |
    v
Execution sandbox
    |
    v
Result verifier
  - execution success and shape
  - requested filters/exclusions are observable
  - ranking, totals and comparison invariants
  - candidate result agreement where required
    |
    v
Grounded response + adaptive widget + lineage
```

## Conversation state contracts

`activeAnalysisState` stores the last grounded analytical intent: metric,
dimensions, period, filters, ordering, limit, result shape, and a short answer
summary. It supports semantic refinements such as `other than Food`, `make that
last quarter`, or `break it down by merchant`. It is persisted on the
conversation and changes only after a newer grounded query succeeds; retries,
clarifications, and the five-message language window do not erase it.

`activeDataScope` is separate and contains exact entity IDs from a displayed
row set. It supports references such as `the second one` or `only those five`.
Summary results must never become row scope.

The resolver should produce a typed state delta:

```json
{
  "inherit_previous_analysis": true,
  "filter_changes": [
    {"operation": "add", "field": "category", "operator": "neq", "value": "food"}
  ]
}
```

Applying the delta is deterministic. The model proposes the change; the domain
layer owns the resulting state.

## Coordinated multi-view queries

A user turn is not equivalent to one SQL query or one widget. Requests such as
`show that table again with an expense summary` require several result shapes
over one financial scope. They use a typed `QueryBundle`:

```text
QueryBundle
  base_query: filters, dates, direction, account and tenant scope
  views:
    - transaction rows
    - aggregate summary
    - optional breakdown or ranking
```

The domain compiler expands the views; the model cannot repeat or independently
alter their filters. Each view is executed against current canonical,
non-deleted records and the response persists lineage for every view.

Refresh and refinement have deliberately different semantics. `Show again`
rehydrates the last grounded query definition and drops prior entity IDs, so an
edit or deletion is reflected. `Only those shown records` binds
`activeDataScope` and keeps the exact IDs. This distinction prevents both stale
tables and accidental expansion beyond a user-selected result set.

## Two execution lanes

The fast lane handles plans fully expressible by the compact query contract.
It must remain semantic rather than phrase-matched.

The governed lane handles exclusions, compound conditions, joins, derived
metrics, multi-period diagnostics, recommendations, and scenarios through the
generic semantic plan. Unsupported conditions must promote a request to this
lane; they must never be discarded to stay on the fast path.

## Accuracy harness

Every grounded response records the resolved objective, semantic plan, schema
version/hash, compiled-query hash, parameters, result shape, source IDs, model
path, validation decisions, and timings. Financial payloads remain out of logs.

Evaluation compares result sets and semantic invariants rather than SQL text.
The suite needs single-turn and multi-turn cases, including:

- add, remove, replace, and negate a filter;
- retain or replace a period and grouping dimension;
- refer to exact rows from an earlier result;
- distinguish category, merchant, account, and location language;
- empty, null, duplicate, refund, transfer, and multi-currency behavior;
- metamorphic checks such as `all = Food + non-Food` for the same scope;
- calibrated abstention when the available schema cannot answer the question.

Primary references:

- https://spider2-sql.github.io/
- https://bird-bench.github.io/
- https://aclanthology.org/2022.findings-emnlp.150/
- https://research.google/pubs/sql-palm-improved-large-language-model-adaptation-for-text-to-sql/
- https://proceedings.mlr.press/v267/li25dt.html
- https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst
- https://docs.snowflake.com/en/user-guide/views-semantic/semantic-view-yaml-spec
- https://docs.cloud.google.com/gemini/data-agents/conversational-analytics-api/data-agent-system-instructions
