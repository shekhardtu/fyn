# Browser evaluator runbook

You are evaluating the live Fyn agent as a customer would experience it. Use
the Browser tool for every product interaction and every observation used for
scoring.

## Non-negotiable boundaries

- Use only the pinned local evaluation account `+919000000098` with the
  demo-finance fixture.
- Never run this suite against production or a personal financial account.
- Do not inspect cookies, local storage, session files, hidden application
  state, arbitrary database rows, or API responses.
- Do not use `curl`, a test client, or direct database queries to decide whether
  an answer is correct. The suite's oracle is the scorer; visible UI is the evidence.
- The only permitted database-backed read during execution is
  `latest-run-metrics`. It returns telemetry for the fixture's newest durable
  run and must never be used as correctness evidence.
- Do not approve any proposed mutation. The taxonomy case must be cancelled.
- Do not relax an assertion because the model returned a safe fallback. A safe
  fallback passes only the case that explicitly expects calibrated abstention.
- Do not reuse conversation context between cases.

## Before the first case

1. Read `evals/browser/suite.yaml` and validate it with:

   ```bash
   .venv/bin/python -m evals.browser.cli validate-suite
   ```

2. Confirm the application is reachable at the report's `run.baseUrl`, is
   signed in as `+919000000098`, and has just been seeded as described in
   `evals/browser/README.md`.
3. Run `.venv/bin/python -m evals.browser.cli fixture-status`. Continue only
   when it reports `ready`; this is a preflight check, not evidence for scoring.
4. Create a report with `new-report` if one was not supplied.
5. If the app, authentication, Browser tool, or model is unavailable, mark only
   affected cases `blocked`, provide a precise `blockedReason`, and do not
   reinterpret an environment failure as an agent-quality failure.

## Execute each case

For every case, in suite order, perform `defaults.repetitions` independent
attempts (three by default):

1. Start a fresh conversation through the visible UI for every attempt. Record
   its URL in the attempt notes so failures can be revisited. Never reuse the
   preceding attempt's context.
2. Perform each `steps` entry exactly:
   - `prompt`: enter the text verbatim in the composer and submit once.
   - `browser_action`: carry out only the described visible interaction.
3. Wait for a terminal agent state before scoring. An interrupt is terminal for
   the current step until its requested browser action is completed.
4. Inspect the final response, visible widgets, data-source controls, and the
   expandable agent-run trace. Do not infer success from a loading indicator.
5. When an assertion requires grounding, open the visible data-source control
   and confirm that it describes the same scope as the answer.
6. After every prompt reaches a terminal state, run:

   ```bash
   .venv/bin/python -m evals.browser.cli latest-run-metrics
   ```

   Copy its run ID, end-to-end latency, Agno model duration, pass count, token
   counts, cost fields, and route signature into the matching attempt turn.
   Confirm each prompt has a new run ID. `costBasis=unavailable` and a null
   `costUsd` are the correct record when Agno/provider coverage is zero.
7. Record each attempt's visible outcome class and exact response. Put all
   labeled attempts/turns in `observedResponse`; do not keep only the best run.
8. Set every assertion's `passed` field to `true` only when it holds in all
   repetitions; otherwise set it to `false`. Add concise,
   independently understandable evidence. Evidence should name visible text,
   values, controls, or trace stages and identify any failing attempt; “looks
   good” is not evidence.
9. Capture a screenshot for every failed critical assertion. Add its path to
   `artifacts`. Screenshots for important passes are encouraged but optional.
10. Leave `status` alone while editing. The CLI derives it from the assertions.

If a case proposes a write, inspect the proposal, cancel it, and use the next
visible prompt to verify no durable change occurred. Delete only conversations
created by this evaluation if cleanup is desired; never delete ledger or account
data as cleanup.

## Scoring

Each case has 10 weighted points. A case passes only when:

- earned score is at least `defaults.passThreshold`; and
- every critical assertion passes when `defaults.requireAllCritical` is true.

There is no partial credit within one assertion. Mark it false if any material
part of its description is absent or wrong. A clarification, tool error, or safe
fallback is a failure when the case expects a grounded answer.

The report derives four independent dimensions:

- correctness: weighted deterministic-oracle score and passing cases;
- latency: median and p95 time-to-first-visible-response and total turn duration;
- predictability: repeated outcome consistency, route consistency, and latency
  coefficient of variation;
- cost: Agno input/output tokens, model-pass count, and exact provider cost only
  when every pass reports it. A consistently wrong answer may be predictable,
  but remains incorrect.

## Finish the run

Run:

```bash
.venv/bin/python -m evals.browser.cli finalize REPORT.json --in-place
.venv/bin/python -m evals.browser.cli validate-report REPORT.json
```

Then report:

- overall status and score;
- the complete correctness, latency, predictability, and cost summaries;
- pass count and score for each tier;
- every failed or blocked case;
- the failed critical assertions and their visible evidence;
- model, commit, base URL, and report path.

Do not describe a pending or blocked suite as passing.
