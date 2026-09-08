# Provider usage accounting follow-up

Baseline: `ea61c76666d6181a49f9e8dd9b4dbe2adb3a1a0d` (PR #28).

## Cause confirmed against code and mocked native SDK responses

- Operator request events were held locally until a final Agno `RunOutput`.
  A later stream error prevented that pass from recording earlier usage.
- Agno rejects some native failed responses before its parser hook, so a
  final-output-only observer cannot retain usage present on those responses.
- Missing counters were normalized to zero in the logical-pass summary.
- Template embeddings were unobserved and used a hidden SDK retry.
- Enrichment completion/failure replaced `item.metrics`, losing previous
  attempts. A process restart could not establish complete usage either.

The fundamental issue was the accounting boundary: an agent pass is not a
provider request, and successful answer delivery is not a billing boundary.
This finding does not establish the source of any historical credential leak
or attribute account-wide provider spending to Fyn.

## Implemented in this batch

1. Capture whitelisted native usage before framework validation for synchronous,
   asynchronous, streaming and non-streaming Responses calls.
2. Preserve per-attempt identities, stages, models, status, nullable token
   counters and provider request/response IDs under `metrics.requestUsage`.
3. Track embeddings with explicit bounded transient retries, never retrying
   billing/auth failures. Responses SDK retries remain disabled centrally.
4. Accumulate enrichment attempts and deduplicate native observations across
   recovery snapshot merging. Retain existing financial commit boundaries.
5. Display reported subtotals and an incomplete-usage indicator. Failed provider
   run notices flag missing usage; old records are not presented as verified.
6. Mark hard-restart/expired-lease accounting as interrupted, not zero.

No schema migration, paid test calls, credential changes, historical financial
repairs or production deployment are part of this batch. Existing logical-pass
counters remain compatible; use the new envelope for request-level accounting.

## Verification and remaining limits

Verified on 2026-09-09: **1,089 backend tests passed** using Agno 2.9.0 and
OpenAI 2.54.0 (the production SDK pins) in an isolated Python 3.13 environment.
The Python 3.9/OpenAI 2.48.0 compatibility checks passed too. The frontend suite
passed **674 tests**; frontend lint, type checking and production build, backend
Ruff, mypy on the seven changed services and `git diff --check` passed. Existing
dependency deprecations and the frontend bundle-size warning remain. These are
local checks, not a production-host smoke test.

Regression tests exercise native failed/incomplete responses, a later failed
request, truncated streams, async calls, nested stages, embeddings retries,
durable failed runs and enrichment retry accumulation. Frontend tests cover
complete/partial/unavailable/interrupted/no-call states and legacy rendering.

- Hard crashes before a checkpoint still require provider-side reconciliation.
  A future independent durable attempt journal would reduce that loss window,
  at the cost of additional writes and a separate failure/retention policy.
- This is not account-wide spend reconciliation, cost estimation, budget
  enforcement, or leaked-key detection. Out-of-band use of a key cannot be
  reconstructed from Fyn's own request records.
- Background enrichment is measured on its own record, not folded into the
  already-completed answer. A combined operational spending view is separate.
- Existing malformed historical transactions still need reviewed repair.
- Older authority/postcondition incidents remain a separate RCA batch.

Provider field reference: [Responses usage](https://developers.openai.com/api/reference/cli/resources/responses/methods/retrieve)
and [embedding usage](https://developers.openai.com/api/reference/resources/embeddings).
