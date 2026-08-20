# Fyn AG-UI runtime contract

Fyn uses AG-UI as its only interactive-agent transport.

## Client adapters

- Web keeps one official `@ag-ui/client` `HttpAgent` per thread. Its native message, state, pending-interrupt, lifecycle-verification, and capability-discovery behavior survives multiple runs and in-process reconnects.
- Fyn widgets remain the product's typed, versioned UI contract. Their validated result is carried in the namespaced `fyn.response.v1` custom event; protocol lifecycle and orchestration use standard AG-UI events.

## Run lifecycle

Every accepted command creates a persisted `agent_runs` row. Its ordered AG-UI events are appended to `agent_events`, and any human decision is represented by `agent_interrupts`.

The run keeps the complete reduced input command, links to the canonical final
assistant message, and records `started_at`, `first_response_at`, and
`finished_at`. `AgentRunOut` exposes total duration and time-to-first-response;
each persisted event retains its protocol timestamp, while activity snapshots
also carry stage duration and cumulative duration, plus the run-level
`failureSummary` and `modelPassCount` aggregates so the live card renders
server-authored values instead of re-deriving them. This makes the prompt,
complete reply, tool/stage sequence, and timing reconstructable without using
the client clock.

```text
POST /agent
  → RUN_STARTED
  → STATE_SNAPSHOT (safe Fyn projection only)
  → ACTIVITY_SNAPSHOT*
  → REASONING_* (observable-stage summary, never raw chain of thought)
  → TEXT_MESSAGE_*
  → CUSTOM fyn.response.v1
  → TOOL_CALL_* + STATE_SNAPSHOT + MESSAGES_SNAPSHOT + RUN_FINISHED(interrupt)
     or STATE_DELTA + RUN_FINISHED(success)
     or RUN_ERROR
```

The event sequence is durable and monotonic per run. An event is committed before it is delivered live, and terminal events commit atomically with terminal run state. Leaving a thread or losing the network detaches the listener; it does not cancel the server run. Both clients retain the last replay-safe SSE sequence and continue from that cursor. A fresh client replays from the beginning; a connected client receives a verifier-compatible continuation beginning with a synthetic, non-persisted `RUN_STARTED` boundary.

At startup, old work drains through leased database claims and a fixed-size
worker pool; backlog size never creates an equivalent number of tasks or model
requests. Queued runs are safe to start. A run that was already executing is
never replayed blindly because a governed action may have crossed its
side-effect boundary before the process exited, so recovery appends a durable
`RUN_ERROR(code: "server_restart")`. There is one narrower checkpoint: once a
successful canonical answer is committed, the run records
`postprocess_pending`. Recovery may resume only the idempotent activity and
related-question enrichment after that checkpoint, then continue the same
event stream through `fyn.response.v1` and `RUN_FINISHED`. Repeated process
exits are capped; after the cap, the committed answer finishes without the
optional suggestion pass.

## Human-in-the-loop actions

A pending Fyn widget action becomes a standard tool-bound AG-UI interrupt. The proposed action is emitted as the complete tool arguments; the response schema uses the standard `approved` / `editedArgs` shape, and the resumed run emits the actual governed execution response as `TOOL_CALL_RESULT`. While an interrupt is open, the composer pauses and the existing widget is the primary response surface. If that widget is unavailable, both clients render a small design-system fallback that can approve, deny, or cancel without deadlocking the thread.

Resumption must address every open interrupt and is routed through the same typed payload validation, widget-origin checks, deterministic domain handler, lifecycle receipt, and audit behavior as any other Fyn action. Replaying the same `(threadId, interruptId, status, payload)` under a new run id returns the previously committed response without executing the action again. Non-resume input while an interrupt is open is accepted as a run and terminates with `RUN_ERROR(code: "pending_interrupt")`.

Secondary widget actions that do not represent a suspended run start a new AG-UI run through `forwardedProps.fynAction`; they are still validated and governed on the server. Client-provided tool declarations are ignored and are explicitly advertised as non-authoritative.

## State and authority

AG-UI state events contain only a safe interaction projection: thread id, run id, phase, message id, and interrupt ids. The ledger, balances, drafts, permissions, and other canonical financial state remain server-side. Client-supplied state, history, context, and tools cannot overwrite or authorize financial data.

The server accepts only one of three reduced commands from a run input:

1. the newest user text message;
2. a validated namespaced Fyn widget action; or
3. a validated AG-UI interrupt resume.

## Conversation context and validation

The conversation table remains the authoritative thread history. A new model
turn receives the five most recent complete user/assistant turns with their
full message text, plus bounded grounding lineage for assistant answers: query
dates, filters, direction, grouping and result shape. Ledger rows and entity
IDs are not copied into general model context. The last complete structured
analysis/data scope remains available separately for authoritative result-set
refinement. The model does not receive an arbitrary character slice,
client-supplied history, or the current turn's reserved blank response.

Correction language marks the turn as reconciliation. The model must compare
the relevant recent answer scopes, state the mismatch, and preserve the
intended prior filters rather than simply continuing from the newest answer.
For a typed read correction, domain policy independently selects the most
relevant prior query by matching its specific filters and restores its period
when the user did not provide a new one.

One versioned prompt template supplies four server-owned policy modes:
Operator, Planner, Validator, and Reconciler. Operator owns each interactive
turn. It may answer ordinary conversation, call authenticated read/calculation
tools, or emit one terminal typed handoff. It has no mutation authority.
Ordinary conversation streams exact provider deltas. Personal-finance answers
are buffered until their numeric claims pass the tool-evidence postcondition.

The terminal `handoff_to_governed_workflow` tool carries the complete available
transaction, taxonomy, clarification, query, and presentation contract. A
complete handoff is compiled by deterministic policy and never sent through a
second interpretation pass. Planner runs only for unresolved complex analysis
and returns a declarative, read-only plan. Validator runs only for capabilities
whose effect policy requires independent review, plus bounded repair and
revalidation. Reconciler is outside chat and may only advise on a supplied pair
of ingestion candidates.

Governed read and analysis handlers derive final answer text from the exact
verified result and persist it beside the same widgets and citations. There is
no separate response Writer or transaction Extractor model. `PRIMARY_AGENT_ENABLED`
is the single operational switch for the model pipeline; disabling it leaves
the deterministic finance authority boundary in place.

Typed read and calculator routes use deterministic contract, scope, and final
evidence checks. An independent model validator is reserved for mutation
intent, taxonomy/planning workflows, coordinated query bundles, and generated
analysis. Runtime-grounded prose faces its postconditions once, at the reply
boundary: numbers are checked against authenticated tool arguments/results, and
an enumerated taxonomy answer must carry exactly the children the tool returned
for every category it names — coverage is intent, not grounding, so a correct
answer about one category is not rejected for omitting the rest.

Analytical reads have one path. Totals, breakdowns, comparisons, cash position
and recurring patterns execute through the template pool and the governed
harness, so validation, tenancy, template caching, chart grammar and the audit
trail apply to every one of them. The Operator's typed runtime tools are the
record listing the semantic layer cannot express, the taxonomy metadata read,
and the calculators — none of which is a query over ledger facts.

A reply that fails a postcondition is replaced by a rendering of the tool result
itself. For a governed analysis that rendering is the harness's own verified
markdown — the code that computed the figures also words them, and nothing
re-renders them downstream. The rendering reads the request the same way the postcondition does and
answers at the scope that was asked — a question about one category's
subcategories is answered with that category's subcategories, never with the
category list — because a true answer to a question nobody asked is not a
fallback. The run then reports `task_status="degraded"` with
`failure_stage="grounding"`, since an override the reader cannot see is how a
wrong answer survives as a clean success.

When the result has no faithful rendering, the turn says it could not verify an
answer rather than narrating a completion it cannot support. When the tool
reported its own failure, the run fails with that tool's stage and error code.
There is no separate prose-repair model pass: a direct answer that fails its
authority checks reroutes into the typed pipeline, which is the one retry lane.

## Cancellation and ordering

Runs in one conversation form a persisted predecessor chain, so concurrent clients cannot race replies or mutations. Cancellation is cooperative: a queued run stops immediately; an executing run stops only at a safe harness boundary. If a governed financial operation has already completed, its verified response is returned rather than pretending the change did not occur.

## Capability boundary

High-level capability metadata and Ops-authored workflows are loaded from the
filesystem operation catalog. Protected common instructions always precede
operation-specific instructions. The database stores normal run/interrupt
state only; it is not an operation registry and receives no YAML/JSON
definitions or compiled operation plans. Managed operations use one generic
form/approval/result widget contract and may compose only server-approved
primitives.

An operation approval is bound to its id, version and file checksum. Resume
re-reads the active in-memory catalog; a changed or removed file forces fresh
review and can never execute under the previous approval.

Capability access and business-data effects are separate policy dimensions.
`AccessMode` describes how a route is entered (read, compute, write, or guided
workflow); `maximum_effect` records the largest durable business-data change
reachable through that capability and its server-issued actions:

- `none` cannot change business data;
- `draft` may persist non-canonical workflow state;
- `mutation` may create, update, or remove canonical user data.

Every capability also declares whether confirmation is `never`, `conditional`,
or `required` before that maximum effect. Registry invariants prevent a safe
read from carrying a draft/mutation effect and prevent mutation-capable
workflows from omitting their confirmation policy. Generated analysis tools
always remain `none`; their read-only contract cannot be promoted to a write by
user or model-authored instructions.

Compound taxonomy creation is represented by the typed
`create_taxonomy_path` mutation. The plan carries one category and its requested
subcategories through routing and a single governed approval; approval executes
the path atomically and treats an identical replay as success without creating
duplicates. Categories and subcategories created by this action are stored with
`scope=user` and the authenticated user's owner id. They are visible only
through that user's `TaxonomyRepository`; a user-owned child added beneath a
visible system category remains private to its owner. Legacy prose
clarifications are upgraded to this typed plan when their names are explicit.
If a legacy resume reopens the same conflict fields, the task stops without a
write instead of emitting another equivalent interrupt.

The capabilities endpoint advertises what this implementation actually supports:

- streaming HTTP transport and event replay;
- structured output;
- state snapshots and JSON Patch deltas;
- persistent server memory/state;
- safe reasoning summaries (complete summaries, not token streaming);
- tool-call events and interrupt approvals with edits.

The clients query this declaration rather than assuming support. The server advertises sequence resumability because both clients consume replay-safe cursors, and advertises approve-with-edits because interrupt schemas accept the protocol's `editedArgs`. It does not advertise a maximum execution time because the underlying governed/model operations do not yet have a hard wall-clock kill boundary.

It deliberately reports unsupported capabilities as false: WebSocket, push notifications, binary HTTP, multimodal input/output, arbitrary code execution, and client-provided tool authority. Raw model chain of thought is never exposed. These can evolve independently without changing the core Fyn design system or weakening the finance authority boundary.
