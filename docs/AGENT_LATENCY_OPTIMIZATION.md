# Agent latency optimization

This is the living delivery plan for reducing actual and perceived agent
latency without replacing the universal Operator with deterministic
conversation routes.

## Product invariants

- Every user message enters the Operator.
- The model decides when deeper reasoning or capability discovery is useful.
- Deterministic code remains authoritative for tenancy, validation,
  confirmation, idempotency, grounding, and recovery.
- Optional telemetry and enrichment never own run state and never determine a
  run outcome.
- The UI may show truthful execution stages, but never fabricated or raw chain
  of thought.

## Targets

| Metric | Target |
|---|---:|
| Submit to visible activity p95 | 250 ms |
| Provider first model token p50 / p95 | 1,500 ms / 3,000 ms |
| Model-backed accepted to first text p50 | 3,000 ms |
| Model-backed accepted to first text p95 | 8,000 ms |
| 11–25 character prompt first text p50 | 2,500 ms |
| 11–25 character prompt first text p95 | 5,000 ms |
| Verified response resolved to composer unlocked p95 | 500 ms |
| Related questions attached after answer p95 | 3,000 ms |
| Queue wait p95 | 50 ms |
| Median Operator input reduction | 25% |
| Common read mounted-tool count | ≤ 8 |
| Eligible prompt-cache read share | ≥ 40% |
| Safety and authority invariant pass rate | 100% |
| Telemetry/enrichment-caused run failures | 0 |

## Work board

| Priority | Workstream | Status | Definition of done |
|---:|---|---|---|
| P0 | Content-free telemetry and regression report | Implemented; release-size sample count pending | Detached browser report, fail-contained persistence, server/provider-request/token/cache breakdown, scenario percentiles, zero customer-data fields |
| P0 | Redundant model/tool-loop removal | Implemented for common semantic reads; long-tail corpus remains | Exact semantic tools collapse schema/query repair loops; successful empty evidence is terminal; generic SQL remains agent-chosen fallback |
| P0 | Prompt/tool payload and cache optimization | Implemented; release-size sample count pending | Common read ≤8 tools, isolated prompts, stable cache keys, ≥40% cache-read share after the first request, ambiguous turns retain capability escape hatches |
| P0 | Agent-chosen model delegation | Implemented; quality/latency evaluation pending | Fast Operator remains universal; it may delegate genuinely complex work to a stronger model; routine turns add no serial router pass |
| P1 | Related questions outside critical path | Implemented; canary pending | Answer and composer finish first; durable independent worker; exactly three differentiated lanes; live late chips; failure invisible to run |
| P1 | Perceived-latency response UX | Implemented; localhost browser verified | Truthful morphing state mark, same-row expandable activity, smooth reveal, click-only user-message metadata, no layout jump |
| P1 | Persistence and render batching | Implemented; localhost browser verified | First activity commits immediately; subsequent events batch without losing exact replay; React renders are bounded per animation frame |
| P1 | Capability preflight removal | Implemented | Ordinary send is not serialized behind a preflight; resume/interrupt authority remains checked |
| P2 | Production rollout and rollback | Pending full verification and sample threshold | Feature flags, 5%/25%/100% canary, SLO and safety gates, one-command rollback, post-deploy report |

## Measured localhost checkpoints

All figures below came from durable provider/server metrics, not browser-control
wall time. The local browser extension can pause independently and is not used
as the latency clock.

| Scenario | Current browser E2E | Provider/tool shape |
|---|---:|---|
| “Hi” | 1.90 s | One universal Operator request; no finance tools |
| “How are you doing?” | 1.98 s | One universal Operator request; no finance tools |
| Ordinary explanation without records | 4.37 s | One universal Operator request; no finance tools |
| Taxonomy / five recent records | 4.38 s / 5.42 s | One selected runtime tool; two provider requests |
| ₹12 lakh EMI / timed prepayment | 4.90 s / 7.88 s | One calculator gateway action; two provider requests |
| Month-to-date total | 3.41 s | One exact semantic action; two provider requests |
| Same-elapsed-day category comparison | 5.39 s | One exact semantic action; two provider requests; full validation passed |
| Empty historical merchant search | 5.40 s | One runtime read; authenticated absence |
| Three-full-month volatility / discretionary cap | 4.95 s / 4.92 s | One exact semantic action; two provider requests; full validation passed |
| Explicit analysis refinement flow | 6.43 s setup / 4.92 s follow-up | Separate contextual corpus; both turns fully grounded |

The values above are the clean, isolated localhost cohort from 27 August 2026,
not release percentiles. All 12 tasks succeeded. The corpus creates one
conversation per standalone sample; true multi-turn growth is measured by
`latency-context-corpus.spec.ts` instead. Its content-free report measured
first visible activity at 56 ms p50 / 121 ms p95, browser first answer at
4.62 s p50 / 6.18 s p95, provider first token at 781 ms p50 / 905 ms p95,
queue wait at 25 ms p95, local tool execution at 22 ms p95, and a 45% aggregate
prompt-cache read share. Telemetry or enrichment caused zero run failures.

These checkpoints are directional, not the release gate. The 12-sample cohort
passes every latency and safety p95 gate, but it intentionally remains below
the 30-run and per-scenario release thresholds. Its aggregate model-backed
first-text median is 4.55 s against the 3.00 s target, so provider/model work is
the next actual-latency priority before rollout.

The checkpoints also exposed why aggregate pass duration must be
non-overlapping: the outer Operator waits synchronously for a delegate, so its
native duration already contains the delegate duration. Tokens and cost still
sum across both passes; `modelDurationMs` now excludes that nested double count
while retaining each pass's own duration in the trace.

Provider-request telemetry showed the current hard boundary clearly. A
grounded read normally has two requests inside one Operator pass: select/call
the authenticated capability, then compose from its result. Local semantic
tool execution is 5–20 ms; the remaining 4–8 seconds is provider time. We do
not bypass that first request with a deterministic answer router. Instead,
bounded semantic capability retrieval removes schema discovery and generated
SQL repair for known shapes while leaving governed SQL and delegation mounted
as agent-chosen fallbacks.

## Ordered implementation objectives

### 1. Close the common-read critical path

Objective: keep the universal Operator agentic while removing capabilities and
instructions an explicitly read-only turn cannot use.

Done means common reads mount at most eight tools, no serial routing model is
added, identical or evidence-only retries do not occur, and the read/correction/
mutation test suites remain green. Localhost verification covers a spending
total, empty result, transaction list, calculator, comparison, and correction;
the activity trace must show the selected tool and exact call count.

### 2. Add agent-chosen complexity escalation

Objective: let the fast Operator solve routine turns and decide when a stronger
analysis model is worth the added latency. This is a tool/delegation decision by
the agent, not a deterministic intent answer or a mandatory router pass.

Done means greetings, ordinary conversation, reads, and calculators never pay a
delegation pass; a complex projection/join/optimization corpus can delegate;
quality is no worse than the current high-effort baseline; delegation reason,
model, tokens, and duration are content-free telemetry. Localhost verification
compares one simple read and three complex questions with the delegation flag on
and off.

### 3. Bound transport, persistence, and rendering work

Objective: make useful progress visible immediately without letting telemetry,
event durability, or React updates extend the critical path.

Done means the first activity event is independently durable, later events are
batched, replay is byte-equivalent in sequence and meaning, the browser paints
at most once per animation frame during text reveal, and telemetry/enrichment
exceptions cannot alter run state. Localhost verification uses stop, reconnect,
reload/replay, throttled CPU/network, and an injected telemetry-store failure.

### 4. Finish continuity and response-state UX

Objective: make waiting legible and completion satisfying without pretending
that work happened earlier than it did.

Done means the fyn mark visibly distinguishes turn/routing/reasoning/tool/
responding/prepared/failed and stops animating when terminal; activity is on the
same header row and keyboard-expandable; user delivery metadata toggles only on
the bubble; smoothed text trails the raw stream by no more than 500 ms; related
questions use contextual, behavioral, and strategic generation rules without
showing those lane titles in the UI. Verify on
desktop, Pixel 7, reduced motion, keyboard-only navigation, and screen-reader
roles in localhost.

### 5. Canary with hard rollback gates

Objective: prove the gains on production traffic without trading away safety or
answer quality.

Done means 5%, 25%, and 100% cohorts each meet the latency, task-success,
authority, grounding, and optional-worker failure gates for a full observation
window. Any authority regression, telemetry-caused failure, or p95 breach rolls
back the affected feature independently.

## Baseline command

After migrations, print the rolling content-free report with:

```bash
cd backend
../.venv/bin/python scripts/report_agent_latency.py --days 30
```

For a release candidate, isolate current code from historical and test traffic
through the opt-in real-browser corpus. It is excluded from ordinary tests
because it intentionally makes live model calls:

```bash
cd frontend
AGENT_LATENCY_CORPUS_REPETITIONS=1 yarn test:e2e:latency
```

Run genuine multi-turn latency separately so unrelated standalone scenarios
cannot contaminate one another:

```bash
cd frontend
yarn test:e2e:latency:context
```

The final `agent_latency_cohort` JSON line prints the exact `--since` and
repeatable `--conversation-id` arguments for the report. Use them to isolate
the current-code cohort, for example:

```bash
cd backend
../.venv/bin/python scripts/report_agent_latency.py \
  --since 2026-08-27T00:00:00+05:30 \
  --conversation-id 00000000-0000-0000-0000-000000000000 \
  --conversation-id 11111111-1111-1111-1111-111111111111
```

The report contains first-activity, first-text, unlock, provider, token, cache,
mounted-tool, tool-call, failure, enrichment, prompt-length, and content-free
execution-scenario aggregates. It never prints prompts, answers, tool
arguments/results, financial values, or reasoning content. `releaseReadiness`
is false when any SLO fails, a gate has no data, or a scenario lacks enough
samples for a meaningful percentile.

## Verification required for every latency PR

```bash
make check

cd frontend
yarn typecheck
yarn lint
yarn test
yarn build
yarn test:e2e
```

Start the real stack with `make dev`, then verify `http://localhost:3000` in the
in-app Browser on desktop and a Pixel 7 viewport. The minimum interaction set
is: a fresh-load “Hi”, ordinary conversation, a simple financial read, a
three-month comparison, a calculator scenario, a contextual follow-up, one HITL
mutation, stop, reload, and replay. Also verify the three related-question types
from their content rather than visible labels,
prompt-metadata toggle, activity expansion, state-mark transitions, reduced
motion, and late enrichment. Each PR records request ordering, first visible
activity, first painted answer, composer unlock, mounted tools, provider cache
reads, tool-call count, late enrichment, screenshots, and a Playwright trace.
