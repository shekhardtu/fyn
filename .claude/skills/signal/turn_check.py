#!/usr/bin/env python3
"""Signal turn check — Stop hook. Dispatches the observers once, after the
response is finished.

Timing is the whole point. Dispatching at UserPromptSubmit would hand the
observers a session that does not yet contain the turn's work — they would judge
the problem without seeing the solution. Firing here, at the Stop boundary,
they get the finished turn: what the user asked, what was built, what changed.

Hooks cannot spawn sub-agents; only the main agent can. So this returns the one
Stop verdict that hands control back — `decision: block` — carrying a dispatch
instruction and nothing else. The continuation it buys is two Agent calls long.

Loop safety is `stop_hook_active`: on the continuation's own Stop, this exits
immediately. It fires once per turn, never twice.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
REPO = SKILL_DIR.parents[2]
STATE = SKILL_DIR / "state.json"
QUERY = """
select substr(id::text,1,8)||' '||task_status||' '||coalesce(failure_stage,'?')
       ||'/'||coalesce(error_code,'?')
from agent_runs
where created_at > now() - interval '24 hours'
  and task_status in ('failed','degraded')
order by created_at desc limit 50;
"""


def run_log() -> list[str] | None:
    """Rows, or None when the log is unreachable. None is not 'quiet'."""
    try:
        proc = subprocess.run(
            ["docker", "exec", "expen-postgres", "psql", "-U", "finance",
             "-d", "finance", "-t", "-A", "-c", QUERY],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def diff_state() -> dict[str, str] | None:
    """Per-file {path: "added,deleted"} for the working diff.

    Per-file, not a whole-tree hash: the uncommitted branch here is 60+ files
    deep, so a --stat dump would inject thousands of tokens per turn and would
    say "everything changed" every time. Comparing numstat entries yields the
    handful of files that actually moved since the last look, which is what the
    observers need to judge the turn.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO), "diff", "--numstat", "--",
             "backend/app", "frontend/src"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            out[parts[2]] = f"{parts[0]},{parts[1]}"
    return out


def moved_files(previous: dict, current: dict) -> list[str]:
    """Paths whose add/delete counts differ, plus paths that appeared or left."""
    changed = []
    for path, counts in current.items():
        if previous.get(path) != counts:
            added, deleted = counts.split(",")
            changed.append(f"{path} (+{added}/-{deleted})")
    for path in previous:
        if path not in current:
            changed.append(f"{path} (no longer differs from HEAD)")
    return sorted(changed)


def load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save(state: dict) -> None:
    try:
        STATE.write_text(json.dumps(state, indent=1))
    except Exception:
        pass


def main() -> int:
    payload = {}
    try:
        payload = json.load(sys.stdin)
    except Exception:
        pass
    # The continuation we just asked for is finishing. Let it stop.
    if payload.get("stop_hook_active"):
        return 0

    if not (REPO / "backend" / "app").is_dir():
        print(f"Signal is misconfigured: repo root resolved to {REPO}, which has "
              "no backend/app. Observers were NOT dispatched — this is not a quiet "
              "result.", file=sys.stderr)
        return 0

    state = load()
    reasons: list[str] = []
    fresh: list[str] = []

    rows = run_log()
    if rows is not None:
        seen = set(state.get("seen_runs", []))
        fresh = [row for row in rows if row.split()[0] not in seen]
        if fresh:
            if len(rows) >= 50:
                fresh.append("(50-row query cap reached — more runs exist than listed)")
            reasons.append(f"{len(fresh)} new failed/degraded run(s)")
            state["seen_runs"] = sorted(seen | {row.split()[0] for row in rows})[-200:]

    moved: list[str] = []
    current = diff_state()
    if current is not None:
        previous = state.get("diff_files")
        if previous is not None:
            moved = moved_files(previous, current)
            if moved:
                reasons.append(f"{len(moved)} file(s) changed")
        state["diff_files"] = current

    if not reasons:
        save(state)
        return 0

    state["last_dispatch"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save(state)

    runs = "\n".join(f"  - {row}" for row in fresh[-12:])
    reason = (
        f"Signal observers are due ({', '.join(reasons)}).\n"
        + (f"New failed/degraded runs:\n{runs}\n" if runs else "")
        + (f"Files changed since last check:\n"
           + "\n".join(f"  - {f}" for f in moved[:15])
           + (f"\n  - (+{len(moved) - 15} more)" if len(moved) > 15 else "") + "\n"
           if moved else "")
        + "\nDo exactly this, then stop — no other work:\n"
        "Dispatch BOTH observers in ONE message so they run concurrently and in "
        'the background: Agent with subagent_type "signal-product", and Agent with '
        'subagent_type "signal-principal". Give each the same context block: what '
        "the user asked for this turn, what you actually built or changed, which "
        "files you touched, and any decision the user made. They observe the "
        "finished turn — the problem AND the solution — so this context is the "
        "point of firing here rather than at the prompt.\n"
        "Then end your turn with one short line telling the user the observers "
        "are running. Do NOT do their analysis yourself, do NOT read the run log, "
        "do NOT summarise the turn at length. Their reports arrive as task "
        "notifications; relay them at the end of a later response, never mid-task."
    )
    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
