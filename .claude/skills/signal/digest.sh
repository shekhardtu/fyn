#!/bin/bash
# Signal session-start digest: deterministic signals from the durable run
# log, injected into context by the SessionStart hook. Read-only; fails quiet.
set -u
PSQL="docker exec expen-postgres psql -U finance -d finance -t -A -F' | '"

if ! docker exec expen-postgres pg_isready -U finance -q 2>/dev/null; then
  echo "Signal: run-log database unreachable (expen-postgres down?) — no digest this session; run /signal after starting it."
  exit 0
fi

echo "=== Signal digest (last 24h of the agent run log) ==="
FOUND=0

FAILED=$($PSQL -c "select task_status||' at '||coalesce(failure_stage,'?')||' ('||coalesce(error_code,'?')||'): '||count(*) from agent_runs where created_at > now() - interval '24 hours' and task_status in ('failed','degraded') group by task_status, failure_stage, error_code order by count(*) desc limit 8;" 2>/dev/null)
FAILED_RC=$?
if [ $FAILED_RC -ne 0 ]; then
  echo "Failed/degraded tasks: detector errored (rc=$FAILED_RC) — not quiet, investigate"
  FOUND=1
elif [ -n "$FAILED" ]; then
  FOUND=1
  echo "Failed/degraded tasks:"
  echo "$FAILED" | sed 's/^/  - /'
  CLASSES=$($PSQL -c "select count(*) from (select 1 from agent_runs where created_at > now() - interval '24 hours' and task_status in ('failed','degraded') group by task_status, failure_stage, error_code) g;" 2>/dev/null)
  [ -n "$CLASSES" ] && [ "$CLASSES" -gt 8 ] 2>/dev/null && echo "  - (showing 8 of $CLASSES failure classes — run /signal for the full set)"
else
  echo "Failed/degraded tasks: none (verified quiet)"
fi

PARSE=$($PSQL -c "select coalesce(payload_redacted->>'stage','?')||': '||count(*) from ai_actions where action_type = 'typed_contract_validation' and created_at > now() - interval '7 days' group by payload_redacted->>'stage';" 2>/dev/null)
[ -n "$PARSE" ] && { FOUND=1; echo "Typed-contract parse crashes (7d):"; echo "$PARSE" | sed 's/^/  - /'; }

SUGGEST=$($PSQL -c "select count(*) from ai_actions where action_type = 'suggester' and status = 'failed' and created_at > now() - interval '24 hours';" 2>/dev/null)
[ -n "$SUGGEST" ] && [ "$SUGGEST" != "0" ] && { FOUND=1; echo "Suggester failures (24h): $SUGGEST"; }

REPAIRS=$($PSQL -c "select count(distinct run_id) from agent_events where event_type='ACTIVITY_SNAPSHOT' and payload->'content'->>'stageId'='tool_repair' and payload->'content'->>'status'='completed' and created_at > now() - interval '24 hours';" 2>/dev/null)
[ -n "$REPAIRS" ] && [ "$REPAIRS" != "0" ] && { FOUND=1; echo "Deterministic repairs absorbed (24h): $REPAIRS run(s) — each is a model contract mistake the machine fixed"; }

RECENT=$($PSQL -c "select substr(id::text,1,8)||' thread '||substr(conversation_id::text,1,8)||' — '||coalesce(failure_stage,'?')||'/'||coalesce(error_code,'?') from agent_runs where task_status='failed' and created_at > now() - interval '24 hours' order by created_at desc limit 3;" 2>/dev/null)
[ -n "$RECENT" ] && { echo "Most recent failed runs:"; echo "$RECENT" | sed 's/^/  - /'; }

# Context only. At session start nothing has happened yet, so there is nothing
# to observe about the session — firing a report here would judge the problem
# without seeing any solution. The observers run at the Stop boundary instead,
# where they get the finished turn. This block is background, not an instruction.
if [ "$FOUND" = "1" ]; then
  cat <<'EOF'
The above is BACKGROUND CONTEXT ONLY — do not act on it, do not produce a Signal
report, do not mention it unless the user asks. It exists so you know the state
of the run log while you work.
The observers (signal-product, signal-principal) are dispatched by the Stop hook
once a response is finished, when they can see the whole turn. If the user asks
what to improve, invoke the signal skill or wait for their reports.
EOF
else
  echo "All detectors verified quiet — background context only, nothing owed."
fi
