#!/usr/bin/env python3
"""Signal diff predicates — PostToolUse on Edit/Write under backend/app.

Reads the edit, not the database. Each predicate is decidable from the diff
plus the repo; none of them judge intent. Silence is the normal output: a
predicate that does not fire prints nothing at all.

Fails open — a crash here must never block an edit.
"""
from __future__ import annotations

import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

SCOPE = "backend/app/"
SKILL_DIR = Path(__file__).resolve().parent
# Anchored to the repo, never to the session cwd: hooks run wherever the user
# happens to be (frontend/, backend/, anywhere), and a relative grep from the
# wrong directory returns "not found" — a silently wrong answer, which is worse
# than a crash.
REPO = SKILL_DIR.parents[2]


def added_lines(tool_name: str, tool_input: dict) -> str:
    """The text this edit introduced, ignoring what it replaced."""
    if tool_name == "Write":
        return tool_input.get("content", "") or ""
    pairs = []
    if tool_name == "Edit":
        pairs = [(tool_input.get("old_string", ""), tool_input.get("new_string", ""))]
    elif tool_name == "MultiEdit":
        pairs = [(e.get("old_string", ""), e.get("new_string", "")) for e in tool_input.get("edits", [])]
    out = []
    for old, new in pairs:
        diff = difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), n=0)
        out.extend(line[1:] for line in diff if line.startswith("+") and not line.startswith("+++"))
    return "\n".join(out)


def repo_has(pattern: str, path: str) -> bool:
    """Literal grep over a repo-relative path. False on any error — never guess true."""
    target = REPO / path
    if not target.exists():
        return False
    try:
        return subprocess.run(
            ["grep", "-rqF", pattern, str(target)], capture_output=True, timeout=5
        ).returncode == 0
    except Exception:
        return False


def predicates(file_path: str, added: str) -> list[str]:
    found: list[str] = []

    # P1 — a new failure path while the terminal event still reports success
    # unconditionally. This is agui.py:1706, the defect of 2026-08-19.
    if re.search(r"""task_status\s*=\s*["'](failed|degraded)["']""", added):
        if repo_has('"value": "succeeded"', "backend/app/services/agui.py"):
            found.append(
                "P1 failure-invisible-at-transport: this edit adds a task_status "
                "failed/degraded path, but agui.py still hardcodes /fyn/phase="
                '"succeeded" in the terminal STATE_DELTA. The run will be recorded '
                "failed and streamed as success. — pattern: event-sourced-runs "
                "(temporalio/temporal; langchain-ai/langgraph)"
            )

    # P2 — a new failure class nothing asserts on.
    codes = set(re.findall(r"""error_code\s*=\s*["']([a-z_]{3,})["']""", added))
    codes |= set(re.findall(r"""["']code["']\s*:\s*["']([a-z_]{3,})["']""", added))
    untested = sorted(c for c in codes if not repo_has(c, "backend/tests"))
    if untested:
        found.append(
            f"P2 new-failure-class-untested: {', '.join(untested)} — no reference "
            "anywhere under backend/tests/. A failure class with no test is a "
            "failure class with no contract. — pattern: trace-evals-as-config "
            "(langfuse/langfuse; promptfoo/promptfoo)"
        )

    # P3 — a hardcoded user-facing question is a promise the binder must keep.
    if "question" in added.lower() and re.search(r"""["'][^"'\n]{12,}\?["']""", added):
        found.append(
            "P3 hardcoded-question-string: this edit adds a literal question to a "
            "suggestion/recovery list. Verify the template binder can bind it — "
            "agui.py:1380 shipped one that binding rejects (run 78333587). "
            "— pattern: grounded-follow-up-suggestions (ag-ui-protocol/ag-ui; "
            "ItzCrazyKns/Perplexica)"
        )

    # P4 — the seed pool moved; binder coverage is now a live question.
    if file_path.endswith("analysis_seeds.py"):
        found.append(
            "P4 seed-pool-changed: re-check binder coverage for the intents "
            "currently failing (period-over-period comparison: runs beb26337, "
            "53875a40, 78333587). — pattern: declarative-registries "
            "(langgenius/dify; n8n-io/n8n)"
        )

    return found


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    tool_name = payload.get("tool_name", "")
    if tool_name not in {"Edit", "Write", "MultiEdit"}:
        return 0
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path", "") or ""
    if SCOPE not in file_path:
        return 0

    # A misresolved repo root makes every predicate return False, which reads as
    # "nothing to report" — the one failure this observer must never fake. Say so.
    if not (REPO / "backend" / "app").is_dir():
        json.dump({"hookSpecificOutput": {"hookEventName": "PostToolUse",
            "additionalContext": f"Signal is misconfigured: repo root resolved to "
            f"{REPO}, which has no backend/app. Diff predicates are NOT running — "
            "this is not a quiet result. Tell the user."}}, sys.stdout)
        return 0

    try:
        found = predicates(file_path, added_lines(tool_name, tool_input))
    except Exception:
        return 0
    if not found:
        return 0

    body = "\n".join(f"  - {item}" for item in found)
    context = (
        f"Signal diff predicate fired on {file_path}:\n{body}\n"
        "Raise this with the user now, in one or two lines, citing the predicate "
        "id and the evidence above. Do not restate it as a full two-lane report — "
        "that form is for /signal and the session digest."
    )
    json.dump(
        {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}},
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
