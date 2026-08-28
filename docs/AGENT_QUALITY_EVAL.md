# Agent quality release gate

`make eval-agent-quality` is the repeatable localhost release gate for the
agent as a product, not only as a model call. It drives Chrome through the real
UI and AG-UI stream, grades durable answers, reads the detached latency
telemetry, and exits non-zero when the candidate is not ready.

## What it evaluates

The corpus covers conversation quality, financial education, authenticated
taxonomy and transaction reads, deterministic calculation, semantic analysis,
empty-result honesty, complex refund reconciliation, multi-turn continuity,
and one governed write. Every financial scenario carries independently read
reference facts from the synthetic ledger.

The gate has three independent layers:

1. Browser hard checks: completed/successful run, useful answer, no internal
   diagnostics or generic failure, required authenticated citations, exact
   oracle terms and figures, three late related questions, and exact mutation
   verification.
2. Structured rubric judge: correctness/grounding, relevance/completeness,
   clarity/naturalness, agentic continuity, and safety/trust, each scored 1–5
   by the configured stronger model. Every dimension must be at least 3, the
   average must be at least 4, and no critical failure may be present.
3. Durable latency/safety gates: first activity, provider TTFT, first visible
   answer, composer unlock, tool mounting, and zero telemetry/enrichment-caused
   failures.

## Isolation and lifecycle

The runner refuses non-local URLs. It resets only the fixed
`FYN Quality Eval` identity (`+919000000098`) in a development database,
installs deterministic synthetic finances, and uses a distinct Playwright
session. Reruns preserve that identity and session while replacing all of its
application data, so an eval never inherits prior conversations or mutations
and does not consume a fresh OTP for every run. The populated local fixture is
retained for inspection by default; set `AGENT_QUALITY_KEEP_FIXTURE=0` to
delete the complete account after reporting.

For a diagnostic rerun, `AGENT_QUALITY_SCENARIOS` accepts comma-separated
scenario ids. Contextual follow-ups automatically execute their setup turn,
but only requested samples are written to the artifact. A filtered corpus is
intentionally not release-eligible; the complete 11-scenario run remains the
release gate.

No test shortcut is enabled in production. Browser authentication still uses
the ordinary OTP route; localhost supplies the debug code because production
configuration refuses `OTP_DEBUG_ECHO`.

## Run

```bash
make eval-agent-quality
```

Existing localhost servers are reused. Missing backend/frontend processes are
started for the run and stopped afterward. Results are written to:

- `frontend/test-results/agent-quality/browser.json`
- `frontend/test-results/agent-quality/release.json`
- backend/frontend logs in the same directory when the runner starts them

The browser artifact intentionally contains only synthetic prompts, answers,
and reference facts. Customer conversation content never enters this harness.
