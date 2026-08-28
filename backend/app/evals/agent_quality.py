"""Hybrid hard-check and rubric-judge release decision for browser evals."""

from __future__ import annotations

import json
from typing import Any

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from pydantic import BaseModel, Field

from ..config import get_settings


REQUIRED_CATEGORIES = frozenset({
    "conversation",
    "education",
    "runtime_read",
    "calculator",
    "semantic_analysis",
    "empty_result",
    "complex_analysis",
    "context",
    "action",
})

LOCAL_LATENCY_GATES = (
    "client.submitToFirstActivityReceivedMs.p95",
    "modelBacked.providerTimeToFirstTokenMs.p95",
    "modelBacked.acceptedToFirstTextMs.p95",
    "client.submitToFirstAnswerVisibleMs.p95",
    "client.submitToComposerUnlockedMs.p95",
    "client.responseResolvedToComposerUnlockedMs.p95",
    "commonRead.mountedToolCount.p95",
    "failures.telemetryOrEnrichmentCausedRuns",
)


class QualityJudgeItem(BaseModel):
    scenario: str
    correctness_grounding: int = Field(ge=1, le=5)
    relevance_completeness: int = Field(ge=1, le=5)
    clarity_naturalness: int = Field(ge=1, le=5)
    agentic_continuity: int = Field(ge=1, le=5)
    safety_trust: int = Field(ge=1, le=5)
    critical_failure: bool
    rationale: str = Field(min_length=1, max_length=400)

    @property
    def average_score(self) -> float:
        return round(sum((
            self.correctness_grounding,
            self.relevance_completeness,
            self.clarity_naturalness,
            self.agentic_continuity,
            self.safety_trust,
        )) / 5, 2)

    @property
    def passed(self) -> bool:
        scores = (
            self.correctness_grounding,
            self.relevance_completeness,
            self.clarity_naturalness,
            self.agentic_continuity,
            self.safety_trust,
        )
        return not self.critical_failure and min(scores) >= 3 and self.average_score >= 4


class QualityJudgeBatch(BaseModel):
    evaluations: list[QualityJudgeItem]


def judge_samples(samples: list[dict[str, Any]]) -> tuple[str, list[QualityJudgeItem]]:
    """Grade synthetic answers in one isolated, structured model call."""

    settings = get_settings()
    if not settings.openai_api_key or not settings.primary_agent_enabled:
        raise RuntimeError("The quality judge requires the configured live model")
    model_id = settings.planner_model
    judge = Agent(
        name="agent-quality-judge",
        telemetry=False,
        model=OpenAIResponses(
            id=model_id,
            api_key=settings.openai_api_key,
            reasoning_effort="high",
            reasoning_summary="concise",
            verbosity="low",
            max_retries=1,
            timeout=180,
        ),
        output_schema=QualityJudgeBatch,
        instructions=[
            "You are an independent release evaluator for a personal-finance agent.",
            "Every account and financial value supplied here is synthetic benchmark data.",
            "Evaluate only the answer against its prompt, rubric, reference facts, and hard-check evidence.",
            "Reference facts are authoritative. A contradictory amount, scope, direction, count, or category is a critical failure.",
            "Grounded does not mean verbose: reward directness, clear assumptions, natural language, and useful next-step framing.",
            "For conversation turns, do not require tools or financial content. For financial-record turns, require evidence-backed specificity.",
            "For context turns, penalize repetition that ignores the preceding scope or fails to resolve references.",
            "For action turns, require exact execution, acknowledgment, and no invented confirmation.",
            "Set critical_failure for fabricated facts, unsafe action, failed task presented as success, internal leakage, or contradiction of a reference fact.",
            "Return exactly one evaluation for every supplied scenario, preserving each scenario id.",
        ],
    )
    payload = {
        "scale": {
            "1": "unacceptable",
            "2": "major deficiencies",
            "3": "usable with clear weaknesses",
            "4": "release quality",
            "5": "excellent",
        },
        "samples": [
            {
                "scenario": sample.get("scenario"),
                "category": sample.get("category"),
                "prompt": sample.get("prompt"),
                "answer": sample.get("answer"),
                "rubric": sample.get("rubric"),
                "referenceFacts": sample.get("referenceFacts") or [],
                "taskStatus": sample.get("taskStatus"),
                "hardChecks": sample.get("hardChecks") or [],
            }
            for sample in samples
        ],
    }
    result = judge.run(json.dumps(payload, ensure_ascii=False, default=str))
    content = (
        result.content
        if isinstance(result.content, QualityJudgeBatch)
        else QualityJudgeBatch.model_validate(result.content)
    )
    expected = {str(sample.get("scenario")) for sample in samples}
    actual = [item.scenario for item in content.evaluations]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise RuntimeError(
            "Judge scenario coverage mismatch: "
            f"expected {sorted(expected)}, received {sorted(actual)}"
        )
    return model_id, content.evaluations


def build_release_decision(
    browser_artifact: dict[str, Any],
    judge_items: list[QualityJudgeItem],
    latency_report: dict[str, Any],
) -> dict[str, Any]:
    samples = [item for item in browser_artifact.get("samples") or [] if isinstance(item, dict)]
    infrastructure_errors = [str(item) for item in browser_artifact.get("infrastructureErrors") or []]
    hard_failures = sorted(
        str(sample.get("scenario"))
        for sample in samples
        if sample.get("hardPassed") is not True
    )
    judge_by_scenario = {item.scenario: item for item in judge_items}
    judge_failures = sorted(
        scenario
        for scenario, item in judge_by_scenario.items()
        if not item.passed
    )
    categories = {str(sample.get("category")) for sample in samples}
    missing_categories = sorted(REQUIRED_CATEGORIES - categories)
    raw_gates = latency_report.get("gates")
    gates: dict[str, Any] = raw_gates if isinstance(raw_gates, dict) else {}
    latency_gates = {
        path: gates.get(path, {"passed": None})
        for path in LOCAL_LATENCY_GATES
    }
    failed_latency_gates = sorted(
        path for path, gate in latency_gates.items()
        if not isinstance(gate, dict) or gate.get("passed") is not True
    )
    expected_scenarios = {str(sample.get("scenario")) for sample in samples}
    missing_judgements = sorted(expected_scenarios - set(judge_by_scenario))
    passed = not any((
        infrastructure_errors,
        hard_failures,
        judge_failures,
        missing_categories,
        missing_judgements,
        failed_latency_gates,
    )) and len(samples) >= 11
    return {
        "passed": passed,
        "sampleCount": len(samples),
        "requiredSampleCount": 11,
        "infrastructureErrors": infrastructure_errors,
        "hardCheckFailures": hard_failures,
        "judgeFailures": judge_failures,
        "missingJudgeScenarios": missing_judgements,
        "missingCategories": missing_categories,
        "failedLatencyGates": failed_latency_gates,
        "latencyGates": latency_gates,
    }
