---
name: rca
description: Root-cause a failed or misbehaving agent thread. Takes a thread URL or thread ID, reconstructs the full run timeline from the database (agent_runs, agent_events, agent_interrupts, conversation history), correlates errors, pinpoints the exact defect, and proposes a robust structural fix — never a workaround patch. Use when a chat thread failed, an agent run errored or hung, a reply was wrong or ungrounded, a widget/interrupt misbehaved, or the user pastes a thread URL asking "what went wrong".
---

# RCA: Agent-thread root-cause analysis

The end goal of every RCA is not just to explain one bad thread — it is to leave the
system safer, more predictable, and more effective than before. Every investigation
must end in a pinpointed defect (file:line where possible) and a robust fix that
removes the failure class, not the single symptom.

## Step 0 — Resolve the thread

Accept either form:

- Thread URL: `http://localhost:3000/c/<uuid>` — the thread ID is the last path
  segment, URL-decoded (route pattern `/c/:conversationId`,
  `frontend/src/routing/paths.ts`).
- Bare thread ID (UUID).

The thread ID **is** `conversations.id` and is identical to the AG-UI `threadId`
used by `agent_runs.conversation_id`. The shared e2e thread ID lives in
`frontend/e2e/.auth/thread.json` if the user says "the test thread".

## Step 1 — Reconstruct the timeline (evidence first, hypotheses later)

Pull ALL of the following before forming any hypothesis. Do not stop at the first
error you see; the first error is usually a symptom.

### Where the evidence lives

There are **no log files, no Sentry, no APM**. The backend logs to the uvicorn
terminal only. **The database is the log**: `agent_runs` + `agent_events` +
`ai_actions` + `analysis_tool_runs` are the durable record.

DB: PostgreSQL 16 in the `expen-postgres` container
(DSN `postgresql+psycopg://finance:finance@localhost:5432/finance`). `psql` is not
on the host — go through docker:

```bash
docker exec expen-postgres psql -U finance -d finance -c "<SQL>"
```

| Table | What it holds |
|---|---|
| `conversations` | thread row: `title`, `archived`, `active_analysis_state`, `active_data_scope` |
| `messages` | canonical turns: `role`, `content`, `widgets` JSON, `citations` JSON |
| `agent_runs` | one row per accepted command: `status`, `task_status`, `failure_stage`, `error_code`, `input_payload`, `delivery_mode`, `last_sequence`, `final_message_id`, `parent_run_id`, `blocked_by_run_id`, `client_message_id`, `started_at`, `first_response_at`, `finished_at` |
| `agent_events` | the durable AG-UI stream: `(run_id, sequence)` unique, `event_type`, `payload` |
| `agent_interrupts` | HITL: `status`, `tool_call_id`, `widget_id`, `reason`, `response_schema`, `metadata`, `response_payload`, `resolved_by_run_id`, `expires_at` |
| `ai_actions` | router/validator decision log (`action_type`, `status`, `payload_redacted`) |
| `analysis_tool_runs` | generated-analysis executions: `status`, `error_code`, `trace` JSON, `duration_ms` |
| `audit_logs` | governed mutation audit trail |

**Critical status distinction** (`backend/app/domain.py`): `agent_runs.status` is
the *transport* outcome (`queued/running/interrupted/succeeded/cancelled/failed`);
`task_status` is the *domain* outcome (`pending/needs_input/succeeded/degraded/failed/cancelled`);
`failure_stage` names where it died (`transport`, `decision_validation`,
`intent_resolution`, a model stage, …). A run can be `status='succeeded'` with
`task_status='failed'` — a thread that "looks fine" at the transport layer but
failed the user's actual task. **Always read both columns.**

There is no tool-calls table: tool calls exist only as `TOOL_CALL_START/ARGS/END/RESULT`
events and as steps inside the persisted `agent_activity` widget on the final message
(`title`, `steps[]` with `stageId/status/tool/detail/input/output/durationMs`, `totalMs`).
Stage vocabulary: `request`, `classification`, `router`, `validator`,
`contract_repair`, `reroute`, `model_pass_<name>`, `execution`, `grounding`,
`response_synthesis`, `tool_validation/repair/execution/verification`.

### Ready-made queries

```sql
-- runs for a thread (start here)
select id, status, task_status, failure_stage, error_code, delivery_mode,
       created_at, started_at, first_response_at, finished_at,
       last_sequence, final_message_id, parent_run_id, blocked_by_run_id, input_payload
from agent_runs where conversation_id = '<thread_id>' order by created_at;

-- full event stream for a run
select sequence, event_type, created_at, payload
from agent_events where run_id = '<run_id>' order by sequence;

-- canonical messages
select id, role, created_at, left(content, 400),
       jsonb_array_length(widgets::jsonb) widgets, jsonb_array_length(citations::jsonb) cites
from messages where conversation_id = '<thread_id>' order by created_at, id;

-- interrupts for the thread
select i.* from agent_interrupts i join agent_runs r on r.id = i.run_id
where r.conversation_id = '<thread_id>' order by i.created_at;

-- router / validator decisions
select created_at, action_type, status, payload_redacted
from ai_actions where conversation_id = '<thread_id>' order by created_at;

-- generated-analysis traces
select created_at, status, error_code, duration_ms, trace
from analysis_tool_runs where conversation_id = '<thread_id>' order by created_at;

-- persisted activity trace on the final message
select jsonb_path_query(widgets::jsonb, '$[*] ? (@.type == "agent_activity")')
from messages where id = '<final_message_id>';
```

### APIs (when the server view matters)

Authenticate with the stored e2e session — **never trigger an OTP send; codes are
rate-limited to 5/hour and lock testing out**. Read the token at use time:

```bash
TOKEN=$(jq -r '.cookies[] | select(.name=="fyn_session") | .value' frontend/e2e/.auth/session.json)
curl -s -H "Cookie: fyn_session=$TOKEN" http://localhost:8000/api/agent/threads/<thread_id>
```

- `GET /api/agent/threads/{id}` — active run, latest run, open interrupts. (The
  old `/metrics` endpoint and its `agent_observability` heuristic scores were
  removed deliberately — run outcome truth lives on `agent_runs.task_status` /
  `failure_stage` / `error_code`; do not reintroduce derived quality scores.)
- `GET /api/conversations/{id}` — messages with widgets + citations.
- `GET /api/agent/runs/{run_id}/events?after=N` — SSE replay of persisted events.
- `GET /api/diagnostics/agent` — mode, models, last 20 router decisions.
- There is **no JSON endpoint for raw `agent_events`** — use SQL for that.

### Known observability blind spots (check, and close if they blocked you)

- The catch-all in `execute_run` (`backend/app/services/agui.py`) persists the
  exception class name as `error_code` and, since 2026-08-16, the actual
  `"ClassName: message"` detail in the terminal `RUN_ERROR` event's `detail`
  field — read that event first when a run shows a bare class name. Runs failed
  before that date have no durable detail.
- `error_code='server_restart'` means startup recovery terminated a run that was
  mid-execution when the process died (`recover_agent_runs`) — the defect is
  whatever killed/restarted the server, not the recovery itself.

### Build the narrative

Assemble one chronological story: user input → reduced command (`input_payload.kind`
∈ `message/action/resume/protocol_error`) → run created → event sequence → tool
calls/results → interrupts → terminal event → persisted reply → task_status verdict.
Note every timestamp gap larger than expected (`durationMs`, `timeToFirstResponseMs`
are exposed on `AgentRunOut`).

## Step 2 — Localize the defect

Work from the timeline to the exact code path:

- Diff the observed event sequence against the contract in `docs/AG_UI_RUNTIME.md`.
  Invariants to check mechanically:
  - `sequence` is gapless and monotonic per run; events commit **before** live delivery.
  - Exactly one terminal event (`RUN_FINISHED` or `RUN_ERROR`), atomic with terminal
    run status and `finished_at`. A run with no terminal event is itself a finding.
  - Non-resume input while an interrupt is open must end `RUN_ERROR(code="pending_interrupt")`.
  - Replaying the same `(threadId, interruptId, status, payload)` under a new run id
    must return the committed response without re-executing.
  - Runs in a thread form a predecessor chain (`blocked_by_run_id`) — concurrent
    clients must not race replies or mutations.
  - Financial prose must pass the tool-evidence postcondition; an unsupported number
    is replaced by a deterministic grounded summary, never shipped.
- The **divergence point, not the crash point, is where you dig.** Code map for the
  usual suspects: `services/agui.py` (`DurableEventPublisher._commit`, `execute_run`,
  `_emit_response`, `normalize_run_input`, `recover_agent_runs`),
  `services/conversation.py` (routing/validation; sets `failure_stage` such as
  `decision_validation`, `intent_resolution`), `services/continuations.py`
  (clarification/resume envelopes — a `legacy_prompt` kind in a trace is deliberate
  evidence of a non-typed resume).
- Reproduce when feasible: `backend/tests/test_agui_runtime.py` has the harness —
  `_execute(db, user, conversation, payload, client_message_id)` drives a real run
  with a real `DurableEventPublisher` and returns `(run, live_events)`. Model new
  reproductions on `test_transport_success_does_not_hide_a_failed_financial_task`,
  `test_pending_interrupt_rejects_new_input_with_run_error_event`,
  `test_live_events_are_committed_before_delivery_and_terminal_status_is_atomic`.
  Run with `cd backend && ../.venv/bin/pytest tests/test_agui_runtime.py -k <expr> -x`.

Ask "why" until the answer is a design property, a missing invariant, or a wrong
assumption — not "the exception was thrown here". Distinguish:

- **Proximate cause**: the line that raised / the event that broke the stream.
- **Root cause**: the missing validation, race, unmodeled state, ambiguous contract,
  or absent invariant that allowed that line to be reached with bad state.

An RCA that stops at the proximate cause is incomplete.

## Step 3 — Classify the failure

Tag the root cause with one or more classes — the class drives the shape of the fix:

| Class | Typical signature | Robust fix shape |
|---|---|---|
| Contract violation | event stream diverges from documented lifecycle | enforce the invariant in code (validator/state machine), not in the caller |
| Unmodeled state | run/interrupt in a state the code never expects | make the state explicit in the model + exhaustive handling |
| Race / ordering | concurrent runs, replay, reconnect, restart | serialize via the predecessor chain / idempotency key, add a test that interleaves |
| Grounding failure | reply numbers not supported by tool evidence | strengthen the postcondition check, never patch the prose |
| Silent swallow | error caught and dropped, or only a bare class name persisted | guarantee a durable terminal `RUN_ERROR` with actionable detail on every exit path |
| Bad input handling | malformed/adversarial client payload reached domain logic | validate at the boundary with typed schemas |
| Timeout / hang | no `finished_at`, no terminal event | add an explicit boundary + durable error, not a client-side retry |

## Step 4 — Propose the robust fix (no workaround patches)

A fix is **robust** only if all of these hold:

1. It removes the *class* of failure, not the instance. ("Retry on this error" and
   "special-case this thread" are workarounds — reject them.)
2. It makes the invalid state unrepresentable or loudly rejected at the boundary,
   rather than tolerated downstream.
3. It keeps the durable-event guarantees: every run still ends in exactly one
   terminal event, committed atomically with run state.
4. It is verified by a new automated test that fails on the old code and passes on
   the new code — reproducing the original thread's shape via the
   `test_agui_runtime.py` harness where possible.
5. It does not weaken the finance authority boundary or widen what clients can
   assert (client state, tools, and history remain non-authoritative).

If a temporary mitigation is genuinely needed to unblock users, label it explicitly
as a mitigation, keep it separate from the fix, and include its removal in the fix.

## Step 5 — Report

Deliver the RCA in this structure (in the final message; write it to a file only if asked):

```
## RCA: <one-line defect statement>
**Thread**: <id> · **Run(s)**: <ids> · **When**: <timestamps>

### Timeline
<chronological narrative with event sequence numbers and timestamps>

### Root cause
<exact defect, file:line, and the why-chain from symptom to root>

### Evidence
<the specific rows/events/log lines that prove it — quoted, not paraphrased>

### Blast radius
<what other threads/flows the same defect can hit; query agent_runs by
task_status/failure_stage/error_code to check whether it already has>

### Fix
<the robust fix: what invariant is added/enforced, where, and the test that pins it>

### Prevention
<what detection would have caught this earlier: assertion, metric, evaluation
signal, or the observability blind spot to close>
```

Severity honesty: if the data is insufficient to pinpoint the cause, say exactly
what is missing and what instrumentation would capture it next time — do not guess
and present a plausible story as a conclusion.
