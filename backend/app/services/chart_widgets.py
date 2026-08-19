"""Deterministic construction of ``data_chart`` widgets.

The chart builder is a validator, not a renderer: it binds one governed
``VisualizationView`` to the exact rows a governed query executor returned and
refuses — with a stable machine code — any spec a client could not draw
faithfully. Money values stay in minor units (``money_minor`` encodings);
scaling for display belongs to the renderer alone.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from ..schemas import DataChartData, Widget, WidgetType
from ..visualization_contracts import VisualizationView


CHART_ROW_CAP = 250

# Every non-arc mark plots on axes; an arc plots slice size on theta and slice
# identity on color. This mirrors the generated frontend channel reader.
_AXIS_MARKS = frozenset({"bar", "line", "area", "point", "rect", "tick"})
_CHANNEL_NAMES = ("x", "y", "color", "size", "theta", "row", "column")


class ChartSpecError(ValueError):
    """A chart specification failed a deterministic check.

    ``code`` is the stable machine label consumers key on:
    ``empty_result``, ``row_cap_exceeded``, ``mark_encoding_mismatch``,
    ``unknown_field``, ``unknown_dataset``.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def dataset_id(name: str) -> str:
    """The stable ``DataResourceId`` form of a plan query name."""
    normalized = re.sub(r"[^a-z0-9]+", "_", str(name).casefold()).strip("_")
    return normalized[:64] or "dataset"


def build_chart_widget(
    view: VisualizationView,
    rows: Sequence[Mapping[str, Any]],
    currency: str | None,
    lineage: Mapping[str, Any],
) -> Widget:
    """Validate one view against its executed rows and return the widget."""
    materialized = [dict(row) for row in rows]
    if not materialized:
        raise ChartSpecError(
            f"View {view.id} has no result rows to draw",
            code="empty_result",
        )
    if len(materialized) > CHART_ROW_CAP:
        raise ChartSpecError(
            f"View {view.id} binds {len(materialized)} rows; charts are capped at {CHART_ROW_CAP}",
            code="row_cap_exceeded",
        )

    encoding = view.encoding
    bound = {
        name: channel
        for name in _CHANNEL_NAMES
        if (channel := getattr(encoding, name)) is not None
    }
    if view.mark == "arc":
        if "theta" not in bound or "color" not in bound or "x" in bound or "y" in bound:
            raise ChartSpecError(
                f"View {view.id}: an arc mark encodes theta and color, never x/y",
                code="mark_encoding_mismatch",
            )
    elif view.mark in _AXIS_MARKS:
        if "x" not in bound or "y" not in bound or "theta" in bound:
            raise ChartSpecError(
                f"View {view.id}: a {view.mark} mark encodes x and y, never theta",
                code="mark_encoding_mismatch",
            )
    else:  # pragma: no cover - VisualMark is a closed literal
        raise ChartSpecError(
            f"View {view.id} uses unsupported mark {view.mark}",
            code="mark_encoding_mismatch",
        )

    encoded_fields = [channel.field for channel in bound.values()]
    encoded_fields.extend(item.field for item in encoding.tooltip)
    for field_name in dict.fromkeys(encoded_fields):
        if any(field_name not in row for row in materialized):
            raise ChartSpecError(
                f"View {view.id} encodes field {field_name}, which is not a key of the result rows",
                code="unknown_field",
            )

    data = DataChartData(
        view=view,
        # Minor units pass through untouched; the renderer divides money.
        rows=materialized,
        currency=currency,
        lineage=dict(lineage),
    )
    return Widget(
        id=f"data-chart-{uuid4()}",
        type=WidgetType.DATA_CHART,
        data=data.model_dump(mode="json", by_alias=True),
        actions=[],
    )
