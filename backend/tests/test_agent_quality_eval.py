from __future__ import annotations

from app.evals.agent_quality import (
    LOCAL_LATENCY_GATES,
    REQUIRED_CATEGORIES,
    QualityJudgeItem,
    build_release_decision,
)


def judgement(scenario: str, score: int = 4, *, critical: bool = False) -> QualityJudgeItem:
    return QualityJudgeItem(
        scenario=scenario,
        correctness_grounding=score,
        relevance_completeness=score,
        clarity_naturalness=score,
        agentic_continuity=score,
        safety_trust=score,
        critical_failure=critical,
        rationale="Synthetic answer satisfies the scenario rubric.",
    )


def passing_inputs():
    categories = [*sorted(REQUIRED_CATEGORIES), "runtime_read", "context"]
    samples = [
        {
            "scenario": f"scenario_{index}",
            "category": category,
            "hardPassed": True,
        }
        for index, category in enumerate(categories)
    ]
    artifact = {"samples": samples, "infrastructureErrors": []}
    judges = [judgement(sample["scenario"]) for sample in samples]
    latency = {
        "gates": {
            path: {"passed": True, "actual": 1, "targetMax": 2}
            for path in LOCAL_LATENCY_GATES
        }
    }
    return artifact, judges, latency


def test_release_decision_passes_only_with_full_hybrid_coverage():
    artifact, judges, latency = passing_inputs()

    decision = build_release_decision(artifact, judges, latency)

    assert decision["passed"] is True
    assert decision["sampleCount"] == 11
    assert decision["hardCheckFailures"] == []
    assert decision["judgeFailures"] == []
    assert decision["failedLatencyGates"] == []
    assert decision["missingCategories"] == []


def test_release_decision_fails_closed_for_each_independent_layer():
    artifact, judges, latency = passing_inputs()
    artifact["samples"][0]["hardPassed"] = False
    artifact["infrastructureErrors"] = ["browser disconnected"]
    judges[1] = judgement(judges[1].scenario, score=3)
    latency["gates"][LOCAL_LATENCY_GATES[0]]["passed"] = False

    decision = build_release_decision(artifact, judges[:-1], latency)

    assert decision["passed"] is False
    assert decision["infrastructureErrors"] == ["browser disconnected"]
    assert decision["hardCheckFailures"] == ["scenario_0"]
    assert decision["judgeFailures"] == ["scenario_1"]
    assert decision["missingJudgeScenarios"] == ["scenario_10"]
    assert decision["failedLatencyGates"] == [LOCAL_LATENCY_GATES[0]]


def test_judge_requires_release_average_and_has_a_critical_failure_veto():
    assert judgement("good").passed is True
    assert judgement("weak", score=3).passed is False
    assert judgement("unsafe", critical=True).passed is False
