# Browser agent regression suite

This suite evaluates the live Fyn agent through the visible web interface. The
evaluator is an agent using the Browser tool, not a Playwright script and not a
backend test client. That makes it useful for model, prompt, routing, grounding,
HITL, and UI regressions that deterministic tests cannot observe together.

The suite contains 16 cases: four each at `easy`, `medium`, `hard`, and
`complex`. Every case has a 10-point rubric. A case passes at 8/10 only when all
critical assertions also pass. Every case runs three times in independent fresh
conversations so reproducibility is measured instead of assumed.

## What is covered

- Easy: exact single-scope totals and ranking.
- Medium: comparisons, grouped reconciliation, cash position, and combined filters.
- Hard: multi-turn corrections, row references, context retention, and clarification.
- Complex: analytical reconciliation, scenarios, calibrated abstention, and cancelled mutations.

All financial questions use the idempotent three-month fixture in
`backend/app/demo_finances.py`. The CLI recalculates its oracle from that source,
so fixture changes cannot silently leave stale expected answers in the suite.

## Prepare a local run

1. Start the PostgreSQL-backed app in LLM mode and open `http://localhost:3000`.
2. Bootstrap the pinned local-only evaluation account `+919000000098`. This
   uses the real OTP challenge and verification logic locally, consumes the
   generated code in memory, and deliberately skips SMS delivery:

   ```bash
   .venv/bin/python -m evals.browser.bootstrap_fixture
   .venv/bin/python -m evals.browser.cli fixture-status
   ```

   `fixture-status` must report `ready`. Seeding is idempotent but deliberately
   does not delete records the user entered, so any non-demo transaction marks
   this dedicated fixture as contaminated.

   The bootstrap refuses production, non-local databases, disabled development
   OTP echo, and contaminated accounts. OTP rows store only an HMAC, so there is
   no plaintext SMS code to retrieve from the database.

3. Validate the suite and create a result file:

   ```bash
   .venv/bin/python -m evals.browser.cli validate-suite
   .venv/bin/python -m evals.browser.cli new-report \
     evals/browser/results/local.json \
     --model "${OPERATOR_MODEL:-unknown}"
   ```

## Run it with an agent

Give the evaluator agent this instruction:

> Read `evals/browser/AGENT_RUNBOOK.md` completely. Use the Browser tool to run
> every case in `evals/browser/suite.yaml` against the local app. Record visible
> evidence in `evals/browser/results/local.json`, then finalize and summarize the
> report. Do not approve mutations and do not use API or database reads to judge
> the agent's answers.

For a smaller diagnostic run, list or render a tier/case:

```bash
.venv/bin/python -m evals.browser.cli list --tier hard
.venv/bin/python -m evals.browser.cli show hard.food_delivery_context_chain
```

## Finalize and validate

After filling each assertion's `passed` and `evidence` fields:

```bash
.venv/bin/python -m evals.browser.cli finalize \
  evals/browser/results/local.json --in-place
.venv/bin/python -m evals.browser.cli validate-report \
  evals/browser/results/local.json
```

`finalize` derives case status and per-tier/overall scores. It exits non-zero
for a failed, blocked, or incomplete run, which makes the report usable as a
release gate while keeping browser execution intentionally agent-driven.

## Measurements

Each prompt creates one durable `AgentRun`. The backend aggregates Agno's native
`RunOutput.metrics` across the Operator, Planner, Binder, Validator, repair, and
related-question passes and persists the aggregate on that run. The terminal
activity card shows the same token/model-time/cost summary. During an eval, copy
the newest fixture run into the report with:

```bash
.venv/bin/python -m evals.browser.cli latest-run-metrics
```

This command is telemetry-only. Correctness is always scored from the seeded
oracle against what the Browser-tool evaluator can see. `finalize` derives:

- correctness score and passing cases;
- median/p95 user-facing latency;
- repeated outcome, route, and latency consistency;
- model passes and input/output tokens, plus USD cost when every provider pass
  reports it.

Agno's OpenAI Responses integration may return tokens without cost. In that
case the report deliberately records `costUsd: null`, `costCoverage: 0`, and
`costBasis: unavailable`; it never inserts an unstated price estimate.

## Relationship to Playwright

Keep `frontend/e2e` as the deterministic browser CI lane. This suite adds a
separate behavioral lane in which a Browser-tool agent can interpret visible
responses, clarification cards, evidence, and uncertainty. The two suites have
different failure modes and should both remain.
