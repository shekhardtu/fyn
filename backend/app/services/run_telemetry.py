"""Optional, content-free telemetry for durable agent runs.

This module is an observability adapter, not orchestration. It performs no I/O,
never raises into the caller, and can be removed or replaced without changing
agent decisions. Persistence piggybacks on commits the durable runtime already
has to make.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import time
from typing import Any


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _epoch_ms(value: datetime | float | int | None) -> float | None:
    if isinstance(value, (float, int)):
        return float(value)
    normalized = _utc(value)
    return normalized.timestamp() * 1000 if normalized is not None else None


def _elapsed_ms(
    start: datetime | float | int | None,
    end: datetime | float | int | None,
) -> float | None:
    left = _epoch_ms(start)
    right = _epoch_ms(end)
    if left is None or right is None:
        return None
    return round(max(0.0, right - left), 1)


class RunTelemetryObserver:
    """In-memory lifecycle observer with a deliberately tiny hot path."""

    def __init__(self) -> None:
        self._event_counts: Counter[str] = Counter()
        self._first_activity_at: float | None = None
        self._first_reasoning_at: float | None = None
        self._first_tool_call_at: float | None = None
        self._first_text_at: float | None = None

    def observe_event(self, payload: dict[str, Any]) -> None:
        """Observe one already-created event; malformed telemetry is ignored."""
        try:
            event_type = str(payload.get("type") or "")
            if not event_type:
                return
            self._event_counts[event_type] += 1
            raw_timestamp = payload.get("timestamp")
            observed_at = (
                float(raw_timestamp)
                if isinstance(raw_timestamp, (int, float))
                else time.time_ns() / 1_000_000
            )
            if event_type == "ACTIVITY_SNAPSHOT" and self._first_activity_at is None:
                self._first_activity_at = observed_at
            if event_type in {"REASONING_START", "REASONING_MESSAGE_CONTENT"} and self._first_reasoning_at is None:
                self._first_reasoning_at = observed_at
            if event_type == "TOOL_CALL_START" and self._first_tool_call_at is None:
                self._first_tool_call_at = observed_at
            if event_type == "TEXT_MESSAGE_CONTENT" and self._first_text_at is None:
                self._first_text_at = observed_at
        except Exception:
            return

    def terminal_metrics(
        self,
        metrics: dict[str, Any] | None,
        *,
        created_at: datetime | None,
        started_at: datetime | None,
        finished_at: datetime,
    ) -> dict[str, Any] | None:
        """Add server timings to a copy, returning the original on any fault."""
        if metrics is None:
            return None
        try:
            enriched = dict(metrics)
            enriched["server"] = {
                "queueWaitMs": _elapsed_ms(created_at, started_at),
                "startedToFirstActivityMs": _elapsed_ms(started_at, self._first_activity_at),
                "startedToFirstReasoningMs": _elapsed_ms(started_at, self._first_reasoning_at),
                "startedToFirstToolCallMs": _elapsed_ms(started_at, self._first_tool_call_at),
                "startedToFirstTextMs": _elapsed_ms(started_at, self._first_text_at),
                "acceptedToFirstTextMs": _elapsed_ms(created_at, self._first_text_at),
                "firstTextToFinishedMs": _elapsed_ms(self._first_text_at, finished_at),
                "acceptedToFinishedMs": _elapsed_ms(created_at, finished_at),
                "eventCounts": dict(self._event_counts),
            }
            return enriched
        except Exception:
            return metrics


def merge_client_telemetry(
    metrics: dict[str, Any] | None,
    client: dict[str, Any],
) -> dict[str, Any]:
    """Merge an independently reported browser observation, without raising."""
    try:
        merged = dict(metrics or {})
        merged["client"] = dict(client)
        return merged
    except Exception:
        return metrics or {}
