---
name: signal
description: Ambient two-lane observer over the durable agent run log and the working diff. Reads deterministic signals from agent_runs/agent_events/ai_actions, plus diff predicates on every edit under backend/app, then produces exactly two evidence-anchored suggestions — one product lane, one architecture lane — each citing named evidence plus a pinned open-source foundation reference from patterns.md. Use at session start (the SessionStart hook injects its digest), when a diff or turn predicate fires, when the user asks "what should we improve", after any incident, or via /signal.
---

# Signal: two-lane ambient observation

Signal is the productized version of the RCA loop: it watches the durable
record and the working diff, detects known failure classes deterministically,
and proposes the next product move and the next architecture move — each with
conviction, meaning named evidence plus an inspectable open-source reference.

Named `signal`, not `copilot`, because this codebase already spends that word
on the product's own agent (`CopilotDecision`, `finance-copilot.v1`,
`workspace.tsx:368`). Signal observes that copilot; it is not one.

It never speaks on a schedule — only when a predicate fires. A turn with no
firing predicate produces silence, and silence is the correct output.

## Authority boundary

Read the record; write nothing into product state. Signal may only
append to its own ledger (`ledger.jsonl` here until the `signal_observations`
table lands in Phase 1 of the backend consumer). Never mutate finance data,
never re-run failed commands as "verification" in a user's real thread without
saying so, and never present a claim without its evidence.

## Trigger surfaces

Detection is free (shell, no model). Observation runs in two sub-agents, in
their own contexts — none of their queries or file reads land in the user's
window. Narration is relayed by the main agent only after its current response
finishes.

| Lane | Agent | Role | Library |
|---|---|---|---|
| Product | `.claude/agents/signal-product.md` | PM who knows this codebase; finds the gap between what users needed and what fyn does | `products.md` |
| Architecture | `.claude/agents/signal-principal.md` | Principal engineer for agent systems; finds the structural weakness, never the workaround | `patterns.md` |

Both are dispatched concurrently and both may report quiet. Each cites only its
own pinned library — `products.md` exists because "product X already does Y" is
the single easiest sentence in this system to fabricate.

| Hook | Fires on | Does |
|---|---|---|
| `SessionStart` | startup, resume, clear, compact | `digest.sh` — run-log state as **background context only**. No report: at session start nothing has happened yet, so there is no turn to judge. |
| `PostToolUse` | Edit/Write under `backend/app/**` | `predicates.py` — P1–P4 on the edit, inline and instant |
| `Stop` | end of every response | `turn_check.py` — cheap change check (new run-log rows, per-file diff deltas). If anything moved, returns `decision: block` carrying a dispatch instruction. |

**Why dispatch at `Stop` and not at the prompt.** An observer spawned at
`UserPromptSubmit` sees the session *before* the turn's work exists — it would
judge the problem without seeing the solution. Firing after the response is
finished hands both observers the complete turn: what was asked, what was built,
which files moved.

Hooks cannot spawn sub-agents; only the main agent can. `decision: block` is the
one Stop verdict that returns control, so the hook uses it to buy a continuation
exactly two Agent calls long. `stop_hook_active` guards it — the continuation's
own Stop exits immediately, so it fires once per turn, never twice.

The diff check is per-file `numstat`, not a whole-tree hash: this branch carries
60+ uncommitted files, and a `--stat` dump would inject thousands of tokens per
turn while claiming everything changed. Only files whose add/delete counts moved
are reported, and it covers edits made outside Claude.

## Step 1 — Run the deterministic detectors

The database is the log (see the `rca` skill for the full schema). Run the
seed detectors — each is a pure query; same input, same finding:

```bash
DSN="docker exec expen-postgres psql -U finance -d finance"
```

1. **Failed/degraded tasks (24h)** — the headline:
```sql
select task_status, failure_stage, error_code, count(*)
from agent_runs where created_at > now() - interval '24 hours'
and task_status in ('failed','degraded') group by 1,2,3 order by 4 desc;
```
2. **Typed-contract parse crashes** (model returned unparseable structured output):
```sql
select payload_redacted->>'stage', count(*), max(created_at)
from ai_actions where action_type = 'typed_contract_validation'
and created_at > now() - interval '7 days' group by 1;
```
3. **Repeated question after a reply** (product-lane gold — the reply did not satisfy):
```sql
select m1.conversation_id, left(m1.content, 80), count(*)
from messages m1 join messages m2 on m2.conversation_id = m1.conversation_id
and m2.role = 'user' and m1.role = 'user' and m2.id <> m1.id
and lower(trim(m2.content)) = lower(trim(m1.content))
and m2.created_at > m1.created_at and m2.created_at < m1.created_at + interval '2 hours'
where m1.created_at > now() - interval '24 hours'
group by 1, 2 having count(*) >= 1;
```
4. **Suggester failures** (`ai_actions.action_type = 'suggester'`, status failed).
5. **Deterministic-repair activations** (`agent_events` where stage `tool_repair`
   completed — each one is a model mistake the machine absorbed; rising counts
   mean the planner prompt or contract needs attention).
6. **Fabrication guard trips** (`agent_events` stage `operator` detail
   "Rejected an ungrounded" — count and inspect).

Add detectors as new failure classes appear; a detector belongs here only if
it is a deterministic predicate with named evidence fields.

### Diff predicates (`predicates.py`, PostToolUse)

These read the edit, not the database, so they fire before a failure ever
reaches production. Each one exists because it would have caught a defect
this system actually shipped:

- **P1 — failure invisible at the transport.** An edit adds a
  `task_status="failed"|"degraded"` path while `agui.py` still hardcodes
  `"value": "succeeded"` in the terminal `STATE_DELTA`. Caught nothing on
  2026-08-19 because it did not exist; it would have caught `agui.py:1706`.
- **P2 — new failure class with no test.** A new `error_code` / `"code"`
  literal appears in the diff with no reference anywhere under
  `backend/tests/`.
- **P3 — hardcoded user-facing question.** An edit adds a literal question
  string to a suggestion or recovery list. Every such string is a promise the
  template binder must be able to keep — verify it binds before shipping.
- **P4 — seed pool changed.** An edit touches `analysis_seeds.py`; re-check
  binder coverage for the intents currently failing.

A predicate belongs here only if it is decidable by reading the diff and the
repo — never by judging intent.

## Step 2 — Compose exactly two suggestions

One per lane. Non-negotiable form for each:

- **Claim** — one sentence, concrete.
- **Evidence** — run ids / conversation ids / counts from Step 1, quoted.
- **Foundation reference** — one entry from `patterns.md` in this directory,
  cited as `repo — mechanism`. A suggestion with no matching pattern entry
  must either get a new (referenced) entry first or be reframed. Never cite
  from recall; only from `patterns.md`.
- **Smallest next step** — what to change, where (file:line when known).

If no detector fired: the two slots report the verified-quiet state — which
detectors ran, over what window, with zero findings. Verified quiet is
evidence; filler is not. Never invent a suggestion to fill a slot, and never
produce scores of any kind — this system deleted its fabricated quality
metrics deliberately (see the agent-honesty project memory).

## Step 3 — Record the outcome

Append one JSON line per suggestion to `ledger.jsonl` in this directory:
`{"date", "lane", "claim", "evidence", "pattern", "status"}` with status
`proposed`, then update to `adopted` or `rejected` when the user decides.
The adopted/rejected history is the Co-pilot's only quality metric.

## Escalation

A finding that warrants a full investigation hands off to the `rca` skill —
this skill detects and proposes; `rca` diagnoses to file:line and fixes.
