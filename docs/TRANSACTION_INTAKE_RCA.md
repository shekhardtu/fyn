# Transaction intake RCA follow-up

Scope: transaction-intake fixes prepared from deployed `main` at
`1797da3d8934913bbf38ebaf6de2cc21dd155eb3`. API deployment and production smoke
verification remain separate release steps.
Provider error handling and planning-intent fixes were already merged in PRs 25
and 26. This batch does not change credentials or repair historical records.

## Causes and invariants

| Defect reproduced | Fundamental issue | New boundary |
| --- | --- | --- |
| Paid salary classified as income; an external recipient treated as an owned account | Keyword priority mixed transaction purpose with direction and ownership | Explicit incoming/outgoing evidence precedes purpose; unresolved salary/recipient direction enters the type selector. Model output cannot override explicit direction or populate accounts while direction is unresolved. |
| An explicit calendar date became today; a leading date became the amount | Date extraction handled only a few relative phrases, while amount extraction treated every number as money; model merging could overwrite explicit dates | One event-date resolver separates absent, resolved, invalid and ambiguous dates. Date spans are excluded from amount parsing. Explicit dates win during model merging; unresolved dates cannot become timestamps. |
| “Similarly add…” ignored the preceding entry or requested write authority again | Context recognition, command authorization and entity binding disagreed | Continuation prefixes are recognized consistently. A new write command and amount are still required; only one preceding server-issued, user-owned active transaction can supply direction/taxonomy. |
| Same account appeared on both sides, or a custom spelling reached a late validation error | The UI candidates and text-input path did not share the canonical distinct-account invariant | Both selectors exclude the opposite account. IDs and normalized names are checked before creating an account or changing draft endpoints; the same question remains correctable. The final ledger guard remains in force. |
| A retry failed when the next question was already open | Resume validation only considered currently open interrupts; emitting an old response could create another interrupt | Exact prior payloads are verified against durable resolutions. Replays do not execute again, and advertise the current pending question without reopening an old one. Conflicting payloads still fail. |

Initial regression reproduction: 13 failures in 17 cases before application
changes. Coverage in `backend/tests/test_transaction_intake.py` now includes real
AG-UI runs and resumes, gapless persisted/live event parity, one terminal event,
retry deduplication, explicit-over-model precedence, invalid-date correction,
and bounded follow-up provenance. Existing conversation/runtime tests also run.
Tests use a blank provider key and make no paid LLM requests.

Verification on 2026-09-09: the isolated PR checkout passed the full backend
suite (1,069 tests), including 38 new intake regressions. This excludes unrelated
account-deletion and composer changes in the shared workspace. The earlier
shared-workspace suite passed 1,072 tests, with a focused rerun of 183 tests
after the final account-name grounding guard.
Ruff, mypy on the changed services, and `git diff --check` passed. Existing
Python 3.9/deprecated HTTP-status warnings remain. Browser and production smoke
checks are still release steps, not claimed as completed here.

## Client behavior and trade-offs

- Ambiguous direction asks for the type before saving. “Paid salary” is an
  expense; “received salary” is income; “maid salary” alone is not enough.
- A transfer to a named recipient without an owned-account pair asks for type.
  An explicitly stated expense remains an expense even when it says
  “transferred.” Existing explicit `from … to …` transfer intake is retained;
  this is not a general natural-language ownership classifier.
- Named dates such as `28 July` use the most recent occurrence, including the
  previous year when appropriate. ISO dates retain their supplied year.
  Ambiguous numeric dates offer both interpretations; invalid or multiple event
  dates ask for one `YYYY-MM-DD` value. Date clarification resumes through a
  typed continuation, without another model call.
- “Similarly”/“Likewise” may inherit only direction and compatible taxonomy from
  the immediately preceding saved-transaction card. New explicit fields win.
  Amount, date, currency, merchant and account names are **not** copied from the
  previous transaction. Inherited fields record their source message in draft
  provenance. Missing context may require another question.
- Account-name lookup now collapses whitespace and case-folds consistently.
  It scans the user's account inventory; a stored normalized-name index would
  be appropriate if inventories become large. Existing accounts are not merged.
- No new frontend component, database migration, retry loop or provider call is
  introduced. This does not promise to interpret every natural-language date
  expression or every payer/payee construction.

## Release and rollback plan

1. Review the scoped backend changes and regression suite in a PR, leaving
   unrelated workspace changes out of that PR.
2. Run the full backend suite, Ruff and mypy with the supported dependency set;
   check both deterministic and model-merge boundaries.
3. Deploy after approval using the existing deployment procedure. Smoke-test a
   paid-salary draft, an explicit historical date, a date clarification, a
   distinct-account transfer and a “Similarly add” continuation on test data.
4. Watch invalid-resume, clarification and transaction-save outcomes; verify
   single terminal events and no duplicate ledger effects. Ambiguity prompting
   may increase while silent misclassification should decrease.
5. No schema rollback is needed. Older code cannot read the new
   `transaction_date` continuation kind: resolve or cancel those open questions
   before rollback, or retain its compatible reader. Never replay financial
   writes to recover an interrupted deployment.

## Remaining work (separate batches)

Request-level usage accounting remains incomplete. Completed-run aggregates are
not an authoritative provider billing ledger, especially when a later request
fails. Implement this separately:

1. Add a durable per-attempt usage record keyed by provider/request identity and
   run/stage/model lineage. Store input, output, cached and reasoning usage when
   returned; represent unavailable usage as unknown, not zero.
2. Capture each completed request before a later failure can discard its usage;
   include failed attempts with reported usage, enrichment retries and embedding
   requests. Deduplicate streaming and nested-agent reports of the same request.
3. Derive aggregates from those records, with explicit coverage indicators in
   the client and operational views. Do not claim exact historical billing when
   raw request usage was never captured.
4. Add tests for partial streams, quota/auth errors, retries, nested calls,
   replay, embeddings and aggregation; roll out storage before exposing totals.

Historical incorrect transactions require a separately reviewed, auditable
repair. Neither this batch nor a future usage backfill should silently rewrite
the customer's ledger. The older authority/postcondition incidents also remain
outside this transaction-intake batch.
