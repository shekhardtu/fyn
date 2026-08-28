"""Print a content-free latency baseline from durable agent telemetry."""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select

from app.database import SessionLocal
from app.event_time import now_utc
from app.models import AgentEnrichment, AgentRun
from app.services.agent_latency_report import parse_iso_datetime, summarize_agent_latency


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize agent latency without printing conversation content")
    parser.add_argument("--days", type=int, default=30, help="rolling lookback window")
    parser.add_argument(
        "--since",
        type=parse_iso_datetime,
        help="exact ISO-8601 cohort start; overrides --days",
    )
    parser.add_argument(
        "--conversation-id",
        type=UUID,
        action="append",
        dest="conversation_ids",
        help="restrict the report to a benchmark conversation; repeat for multiple corpus threads",
    )
    args = parser.parse_args()
    cutoff = args.since or (now_utc() - timedelta(days=max(args.days, 1)))
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=now_utc().tzinfo)
    with SessionLocal() as db:
        run_query = select(AgentRun).where(AgentRun.created_at >= cutoff)
        enrichment_query = select(AgentEnrichment).where(AgentEnrichment.created_at >= cutoff)
        if args.conversation_ids:
            run_query = run_query.where(AgentRun.conversation_id.in_(args.conversation_ids))
            enrichment_query = enrichment_query.where(
                AgentEnrichment.conversation_id.in_(args.conversation_ids)
            )
        runs = list(db.scalars(run_query))
        enrichments = list(db.scalars(enrichment_query))
    print(json.dumps(summarize_agent_latency(runs, enrichments), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
