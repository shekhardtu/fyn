from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from app.services.rollout import rollout_assignment, rollout_metric_labels


SUBJECT = UUID("7304abee-44c5-4c20-b9d7-3314751b8d10")


def test_rollout_assignment_is_stable_monotonic_and_has_hard_off_switches():
    first = rollout_assignment("delegate", SUBJECT, enabled=True, percent=5)
    repeated = rollout_assignment("delegate", SUBJECT, enabled=True, percent=5)
    expanded = rollout_assignment("delegate", SUBJECT, enabled=True, percent=25)

    assert first == repeated
    assert not first.selected or expanded.selected
    assert not rollout_assignment("delegate", SUBJECT, enabled=False, percent=100).selected
    assert not rollout_assignment("delegate", SUBJECT, enabled=True, percent=0).selected
    assert rollout_assignment("delegate", SUBJECT, enabled=True, percent=100).selected


def test_rollout_metrics_are_low_cardinality_and_contain_no_subject_identifier():
    labels = rollout_metric_labels(SUBJECT, SimpleNamespace(
        semantic_fast_tools_enabled=True,
        semantic_fast_tools_rollout_percent=100,
        analysis_delegation_enabled=True,
        analysis_delegation_rollout_percent=5,
        agent_enrichment_enabled=False,
        agent_enrichment_rollout_percent=100,
    ))

    assert labels["semantic_fast_tools"] == "all"
    assert labels["analysis_delegation"] in {"control", "canary_5"}
    assert labels["agent_enrichment"] == "disabled"
    assert str(SUBJECT) not in str(labels)
