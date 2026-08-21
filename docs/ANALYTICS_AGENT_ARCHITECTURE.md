# Governed conversational analytics architecture

## Current SQL-first execution boundary

Native-ledger analysis is SQL-first. The Operator receives the full physical
schema for every table covered by the analyst tenant policy and can author one
query graph containing any number of CTEs, nested SELECTs, joins, unions,
intersections, windows, conditional aggregates, statistical functions, and
derived expressions. The final SELECT is shaped for the user-facing answer;
intermediate relations stay inside the query.

The model never supplies tenant identity. PostgreSQL row-level security reads
the authenticated id injected by the server, and the SQL gate refuses physical
table leaves outside that protected manifest. The analyst role remains
read-only, with a timeout and bounded returned rows. These are operational and
isolation boundaries, not a finite analytical grammar.

`ANALYSIS_QUERY_MODE=hybrid` retains the older AnalysisPlan/template workflow
described later in this document as a compatibility path.

## Historical motivation for the hybrid planner

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

## Coordinated multi-view turns

A user turn is not equivalent to one SQL query or one widget. Requests such as
`show that table again with an expense summary` are served by the single agent
loop: the record view arrives as a typed search operation (an interactive
table with its own lineage), and the aggregate summary is composed as grounded
markdown from governed analysis or read tools over the same scope. Every view
still executes against current canonical, non-deleted records, and scope
binding is deterministic — the model cannot independently alter the filters of
a scope it references.

Refresh and refinement have deliberately different semantics. `Show again`
rehydrates the last grounded query definition and drops prior entity IDs, so an
edit or deletion is reflected. `Only those shown records` binds
`activeDataScope` and keeps the exact IDs. This distinction prevents both stale
tables and accidental expansion beyond a user-selected result set.

## Source manifests

Every queryable origin is a `data_sources` row with content-addressed,
versioned `source_manifests` documents (platform blueprint, phase 1). A
manifest carries provenance-tagged sections: `curated` semantics, a
`profiled` physical scan, and later `user_stated` annotations that survive
re-scans. The native ledger's manifest is generated from the semantic
registry plus a deterministic schema scan and is posted idempotently at
startup; a registry change supersedes the active version. Per-user value
catalogs (the exact categorical values present in one user's rows, frequency
ordered, truncation marked, sensitive fields excluded) are computed inside
the canonical tenant scope at planning time and injected into the planner
prompt — they are user-scoped context and are never stored in the shared
manifest document.

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
