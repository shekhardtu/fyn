"""Stable, content-free percentage cohorts for independently reversible features."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID


SEMANTIC_FAST_TOOLS = "semantic_fast_tools"
ANALYSIS_DELEGATION = "analysis_delegation"
AGENT_ENRICHMENT = "agent_enrichment"


@dataclass(frozen=True)
class RolloutAssignment:
    feature: str
    enabled: bool
    percent: int
    bucket: int
    selected: bool

    @property
    def label(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.selected:
            return "control"
        return "all" if self.percent >= 100 else f"canary_{self.percent}"


def rollout_assignment(
    feature: str,
    subject_id: UUID | str | None,
    *,
    enabled: bool,
    percent: int,
) -> RolloutAssignment:
    """Assign one subject monotonically and stably without external state."""

    bounded_percent = max(0, min(100, int(percent)))
    material = f"fyn-rollout-v1:{feature}:{subject_id or 'anonymous'}".encode()
    bucket = int.from_bytes(sha256(material).digest()[:8], "big") % 100
    selected = bool(enabled and bounded_percent > 0 and bucket < bounded_percent)
    return RolloutAssignment(
        feature=feature,
        enabled=enabled,
        percent=bounded_percent,
        bucket=bucket,
        selected=selected,
    )


def rollout_metric_labels(subject_id: UUID | str, settings: Any) -> dict[str, str]:
    """Low-cardinality cohort labels safe for durable latency telemetry."""

    specifications = (
        (
            SEMANTIC_FAST_TOOLS,
            bool(getattr(settings, "semantic_fast_tools_enabled", True)),
            int(getattr(settings, "semantic_fast_tools_rollout_percent", 100)),
        ),
        (
            ANALYSIS_DELEGATION,
            bool(getattr(settings, "analysis_delegation_enabled", False)),
            int(getattr(settings, "analysis_delegation_rollout_percent", 0)),
        ),
        (
            AGENT_ENRICHMENT,
            bool(getattr(settings, "agent_enrichment_enabled", True)),
            int(getattr(settings, "agent_enrichment_rollout_percent", 100)),
        ),
    )
    return {
        feature: rollout_assignment(
            feature,
            subject_id,
            enabled=enabled,
            percent=percent,
        ).label
        for feature, enabled, percent in specifications
    }
