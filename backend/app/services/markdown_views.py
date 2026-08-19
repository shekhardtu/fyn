"""Markdown rendering for chat responses.

Display results (tables, summaries, breakdowns, schedules) render as GitHub-
flavored markdown inside the assistant message body — the only widgets that
remain are the interactive HITL surfaces that carry actions. Everything here
is pure formatting over already-executed values: no arithmetic, no data reads,
no model involvement.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from .currency import format_money_minor


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    text = str(value)
    # Pipes and newlines are the only characters that break a GFM table cell.
    return text.replace("|", "\\|").replace("\n", " ").strip() or "—"


def markdown_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Render a GFM table; returns an empty string for an empty row set."""
    body = [[_cell(value) for value in row] for row in rows]
    if not body:
        return ""
    head = [_cell(value) for value in headers]
    lines = [
        "| " + " | ".join(head) + " |",
        "| " + " | ".join("---" for _ in head) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def money(value_minor: int | None, currency: str | None) -> str:
    if value_minor is None or not currency:
        return "—"
    return format_money_minor(int(value_minor), currency)


def markdown_section(title: str | None, *blocks: str) -> str:
    """Join non-empty blocks under an optional bold heading line."""
    parts = [part for part in ((f"**{title}**" if title else None), *blocks) if part]
    return "\n\n".join(parts)


def join_blocks(*blocks: str | None) -> str:
    return "\n\n".join(part.strip() for part in blocks if part and part.strip())
