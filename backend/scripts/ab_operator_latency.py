"""Run a content-free, counterbalanced Operator latency A/B.

This is intentionally outside the application request path. It exercises the
same agent, authenticated tools, evidence validator, and answer-contract
validator, but emits only scenario ids, timings, token counts, tool names, and
pass/fail booleans. It never persists prompts or model prose.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import json
from time import perf_counter
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Conversation, User
from app.operation_types import ContextRelationship
from app.services.agent_run_metrics import (
    agent_metric_snapshot,
    begin_agent_metric_collection,
    end_agent_metric_collection,
)
from app.services.agents import build_analysis_delegate_tool, run_operator
from app.services.analysis_sandbox import PYTHON_TOOL_NAME
from app.services.analysis_tools import AnalysisToolContext, build_analysis_tools
from app.services.answer_presentation import answer_presentation
from app.services.answer_validation import (
    compile_answer_contract,
    validate_coverage,
    validate_evidence,
)
from app.services.preferences import AnswerStyle
from app.services.runtime_tools import build_runtime_tools
from app.services.turn_policy import resolve_turn_intent


SCENARIOS = {
    "month_total": "How much did I spend this month?",
    "same_elapsed_comparison": (
        "Compare my spending this month with the same elapsed days last month "
        "and show the three largest category drivers."
    ),
    "food_distribution": (
        "How was my August 1–27, 2026 Food spending distributed across days, "
        "merchants, and subcategories?"
    ),
    "discretionary_optimization": (
        "Using the last three full months, calculate a fixed monthly "
        "discretionary-spending cap that is 10% below my historical average "
        "and show which categories require the largest reductions."
    ),
    "cashflow_projection": (
        "Project my cash flow for the next 90 days from recorded recurring income, "
        "recurring expenses, and current account balances. Show the lowest projected "
        "balance, its date, and the assumptions that drive the result."
    ),
    "concentration_optimization": (
        "Using the last six full months, find the smallest set of merchants and "
        "subcategories whose spending reductions could lower my monthly expense "
        "baseline by 15% while preserving categories marked essential."
    ),
}


def _sample(
    db: Session,
    conversation: Conversation,
    scenario_id: str,
    variant: str,
    experiment: str,
) -> dict:
    user = db.get(User, conversation.user_id)
    if user is None:
        raise RuntimeError("Conversation owner not found")
    question = SCENARIOS[scenario_id]
    today = datetime.now(ZoneInfo(user.timezone)).date()
    presentation = answer_presentation(AnswerStyle.EXPLAINED)
    if experiment == "verbosity" and variant == "low_verbosity":
        presentation = replace(presentation, provider_verbosity="low")

    runtime_tools = build_runtime_tools(db, user, today)
    analysis_tools = build_analysis_tools(AnalysisToolContext(
        db=db,
        user_id=user.id,
        conversation_id=uuid4(),
        today=today,
        timezone_name=user.timezone,
        question=question,
        currency=user.currency,
    ))
    if experiment != "delegation" or variant == "delegation_on":
        delegate_tool = build_analysis_delegate_tool(
            question,
            today,
            user.timezone,
            [],
            user_id=user.id,
            read_tools=[*runtime_tools, *analysis_tools],
            presentation=presentation,
        )
        if delegate_tool is not None:
            analysis_tools = [
                tool
                for tool in analysis_tools
                if getattr(tool, "name", None) != PYTHON_TOOL_NAME
            ]
            analysis_tools.append(delegate_tool)
    intent = resolve_turn_intent(question, ContextRelationship.STANDALONE)
    workflow_context = {
        "kind": "none",
        "contextRelationship": ContextRelationship.STANDALONE.value,
        "intentContract": intent.model_dump(mode="json"),
        "correctionRequested": False,
    }

    metric_token = begin_agent_metric_collection()
    started_at = perf_counter()
    try:
        result = run_operator(
            question,
            [],
            today,
            user.timezone,
            [],
            workflow_context=workflow_context,
            user_id=user.id,
            runtime_tools=runtime_tools,
            analysis_tools=analysis_tools,
            presentation=presentation,
        )
        elapsed_ms = round((perf_counter() - started_at) * 1000, 1)
        metrics = agent_metric_snapshot()
    finally:
        end_agent_metric_collection(metric_token)

    reply = result.reply if result and result.reply else ""
    grounding = result.tool_grounding if result else []
    evidence = validate_evidence(reply, grounding, question)
    coverage = validate_coverage(
        reply,
        compile_answer_contract(question),
        evidence.facts,
    )
    first_pass = (metrics.get("passes") or [{}])[0]
    return {
        "type": "operator_latency_ab_sample",
        "scenario": scenario_id,
        "variant": variant,
        "elapsedMs": elapsed_ms,
        "modelDurationMs": metrics.get("modelDurationMs"),
        "providerRequestCount": metrics.get("providerRequestCount"),
        "firstModelTimeToFirstTokenMs": metrics.get("firstModelTimeToFirstTokenMs"),
        "inputTokens": metrics.get("inputTokens"),
        "outputTokens": metrics.get("outputTokens"),
        "cacheReadTokens": metrics.get("cacheReadTokens"),
        "promptComponents": first_pass.get("promptComponents"),
        "mountedTools": first_pass.get("mountedTools"),
        "toolCalls": [item.get("name") for item in first_pass.get("toolCalls") or []],
        "groundedTools": [item.name for item in grounding],
        "evidencePassed": evidence.passed,
        "coveragePassed": coverage.passed,
        "replyProduced": bool(reply),
        "qualityPassed": bool(reply and grounding and evidence.passed and coverage.passed),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversation-id", type=UUID, required=True)
    parser.add_argument(
        "--scenario",
        action="append",
        choices=sorted(SCENARIOS),
        dest="scenarios",
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument(
        "--experiment",
        choices=("verbosity", "delegation"),
        default="verbosity",
    )
    parser.add_argument(
        "--variant",
        action="append",
        dest="variants",
        help="restrict to a named experiment variant; repeat to select more than one",
    )
    args = parser.parse_args()
    scenario_ids = args.scenarios or list(SCENARIOS)

    with SessionLocal() as db:
        conversation = db.get(Conversation, args.conversation_id)
        if conversation is None:
            raise SystemExit("Conversation not found")
        for repetition in range(1, max(1, args.repetitions) + 1):
            pair = (
                ("explained_medium", "low_verbosity")
                if args.experiment == "verbosity"
                else ("delegation_on", "delegation_off")
            )
            variants = pair if repetition % 2 else tuple(reversed(pair))
            if args.variants:
                requested = set(args.variants)
                variants = tuple(item for item in variants if item in requested)
                if not variants:
                    raise SystemExit(
                        f"No requested variant belongs to {args.experiment}: {', '.join(pair)}"
                    )
            for scenario_id in scenario_ids:
                for variant in variants:
                    sample = _sample(
                        db,
                        conversation,
                        scenario_id,
                        variant,
                        args.experiment,
                    )
                    sample["repetition"] = repetition
                    print(json.dumps(sample, sort_keys=True), flush=True)
                    # Governed SQL may stage a reusable template. Benchmarks
                    # must not let the first variant change the second one's
                    # available tool description or leave durable test data.
                    db.rollback()


if __name__ == "__main__":
    main()
