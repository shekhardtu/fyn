"""Judge one browser corpus and emit its combined quality/latency gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.database import SessionLocal
from app.evals.agent_quality import build_release_decision, judge_samples
from app.models import AgentEnrichment, AgentRun, Conversation, User
from app.services.agent_latency_report import summarize_agent_latency


EVAL_PHONE = "+919000000098"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-results", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    artifact = json.loads(args.browser_results.read_text(encoding="utf-8"))
    if artifact.get("kind") != "fyn_agent_quality_browser_eval":
        raise SystemExit("Unknown browser quality artifact")
    samples = [item for item in artifact.get("samples") or [] if isinstance(item, dict)]
    conversation_ids = {
        UUID(str(item["conversationId"]))
        for item in samples
        if item.get("conversationId")
    }

    with SessionLocal() as db:
        owner_ids = set(db.scalars(
            select(Conversation.user_id).where(Conversation.id.in_(conversation_ids))
        )) if conversation_ids else set()
        owners = list(db.scalars(select(User).where(User.id.in_(owner_ids)))) if owner_ids else []
        if len(owners) != 1 or owners[0].phone != EVAL_PHONE or owners[0].display_name != "FYN Quality Eval":
            raise SystemExit("Refusing to judge conversations outside the dedicated eval account")
        runs = list(db.scalars(
            select(AgentRun).where(AgentRun.conversation_id.in_(conversation_ids))
        ))
        enrichments = list(db.scalars(
            select(AgentEnrichment).where(AgentEnrichment.conversation_id.in_(conversation_ids))
        ))

    judge_model, judge_items = judge_samples(samples)
    latency = summarize_agent_latency(runs, enrichments)
    decision = build_release_decision(artifact, judge_items, latency)
    report = {
        "schemaVersion": 1,
        "kind": "fyn_agent_quality_release_gate",
        "browser": {
            "cohortStartedAt": artifact.get("cohortStartedAt"),
            "finishedAt": artifact.get("finishedAt"),
            "sampleCount": len(samples),
        },
        "judge": {
            "model": judge_model,
            "evaluations": [
                {
                    **item.model_dump(mode="json"),
                    "averageScore": item.average_score,
                    "passed": item.passed,
                }
                for item in judge_items
            ],
        },
        "latency": latency,
        "releaseDecision": decision,
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)
    raise SystemExit(0 if decision["passed"] else 1)


if __name__ == "__main__":
    main()
