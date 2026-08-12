"""Safe, reusable builders for agent-generated presentation contracts.

The library owns presentation inference, not business truth. Callers provide
already-authorized rows and capabilities; this module selects useful columns
and validates the final widget contract. It never accepts HTML, React, SQL, or
executable callbacks from a model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ..config import DEFAULT_CURRENCY
from ..schemas import (
    DataChartAxis,
    DataChartAxisType,
    DataChartData,
    DataChartSeries,
    DataChartType,
    DataChartValueType,
    DataTableColumn,
    DataTableData,
    DataTableRowAction,
    DataVisualizationData,
    TableColumnAlign,
    TableColumnPriority,
    TableColumnType,
    VisualizationLayout,
    VisualizationView,
    Widget,
    WidgetActionIcon,
    WidgetActionId,
    WidgetActionStyle,
    WidgetType,
)
from .semantic import VisualizationSpec


@dataclass(frozen=True)
class FieldPresentation:
    """Presentation metadata for one domain field.

    Field metadata comes from application code or a governed semantic schema,
    rather than being guessed from values such as a rupee-looking string.
    """

    key: str
    label: str
    type: TableColumnType = TableColumnType.TEXT
    priority: TableColumnPriority = TableColumnPriority.SECONDARY
    align: TableColumnAlign = TableColumnAlign.LEFT
    currency_key: str | None = None
    secondary_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class RowCapability:
    """A backend-authorized action that a renderer may expose per row."""

    id: str
    label: str
    action: WidgetActionId
    payload_key: str
    resource_key: str = "id"
    style: WidgetActionStyle = WidgetActionStyle.SECONDARY
    icon: WidgetActionIcon | None = None
    capability: str | None = None


@dataclass(frozen=True)
class TableBlueprint:
    """Declarative table recipe reusable across business entities."""

    fields: tuple[FieldPresentation, ...]
    row_actions: tuple[RowCapability, ...] = ()
    row_id_key: str = "id"
    capabilities_key: str = "_capabilities"
    empty_message: str = "No matching records."


class WidgetLibrary:
    """Creates registered, schema-validated widgets from trusted result data."""

    @staticmethod
    def data_table(
        *,
        widget_id: str,
        title: str,
        rows: Sequence[Mapping[str, Any]],
        blueprint: TableBlueprint,
        body: str | None = None,
    ) -> Widget:
        normalized_rows = [dict(row) for row in rows]
        visible_fields = [field for field in blueprint.fields if _has_display_value(normalized_rows, field.key)]
        if not visible_fields:
            # Empty result sets still need stable headings; otherwise retain the
            # primary field so the contract remains useful to screen readers.
            visible_fields = [field for field in blueprint.fields if field.priority == "primary"][:1]
        if not visible_fields and blueprint.fields:
            visible_fields = [blueprint.fields[0]]

        columns = [
            DataTableColumn(
                key=item.key,
                label=item.label,
                type=item.type,
                align=item.align,
                priority=item.priority,
                currencyKey=item.currency_key,
                secondaryKeys=list(item.secondary_keys),
            )
            for item in visible_fields
        ]
        actions = [
            DataTableRowAction(
                id=item.id,
                label=item.label,
                action=item.action,
                style=item.style,
                resourceKey=item.resource_key,
                payloadKey=item.payload_key,
                icon=item.icon,
                capability=item.capability,
            )
            for item in blueprint.row_actions
        ]
        data = DataTableData(
            title=title,
            body=body,
            columns=columns,
            rows=normalized_rows,
            rowIdKey=blueprint.row_id_key,
            rowActions=actions,
            capabilitiesKey=blueprint.capabilities_key,
            emptyMessage=blueprint.empty_message,
        )
        return Widget(id=widget_id, type=WidgetType.DATA_TABLE, data=data.model_dump(mode="json", by_alias=True))

    @staticmethod
    def generated_id(prefix: str) -> str:
        return f"{prefix}-{uuid4()}"

    @staticmethod
    def data_chart(
        *,
        widget_id: str,
        title: str,
        chart_type: DataChartType,
        rows: Sequence[Mapping[str, Any]],
        x_key: str,
        x_label: str,
        x_type: DataChartAxisType,
        y_key: str | None = None,
        y_label: str | None = None,
        y_type: DataChartAxisType = DataChartAxisType.CATEGORY,
        value_key: str = "value",
        value_label: str = "Amount",
        value_type: DataChartValueType = DataChartValueType.MONEY,
        currency: str | None = DEFAULT_CURRENCY,
        series_key: str | None = None,
        label_keys: Sequence[str] = (),
        body: str | None = None,
        query_result: Mapping[str, Any] | None = None,
    ) -> Widget:
        data = DataChartData(
            title=title,
            body=body,
            chartType=chart_type,
            rows=[dict(row) for row in rows],
            xAxis=DataChartAxis(key=x_key, label=x_label, type=x_type),
            yAxis=DataChartAxis(key=y_key, label=y_label or y_key.replace("_", " ").title(), type=y_type) if y_key else None,
            series=[DataChartSeries(
                key=value_key,
                label=value_label,
                valueType=value_type,
                currency=currency if value_type == DataChartValueType.MONEY else None,
                groupKey=series_key,
            )],
            labelKeys=list(label_keys),
            queryResult=dict(query_result) if query_result else None,
        )
        return Widget(id=widget_id, type=WidgetType.DATA_CHART, data=data.model_dump(mode="json", by_alias=True))

    @staticmethod
    def data_visualization(
        *,
        widget_id: str,
        title: str,
        datasets: Mapping[str, Sequence[Mapping[str, Any]]],
        visualizations: Sequence[VisualizationSpec],
        body: str | None = None,
        query_results: Mapping[str, Mapping[str, Any]] | None = None,
        columns: int = 1,
    ) -> Widget:
        """Build a validated, renderer-neutral BI visualization payload."""
        safe_dataset_names = {
            name: f"dataset-{index}"
            for index, name in enumerate(datasets)
        }
        views = [VisualizationView(
            id=f"view-{index}",
            title=visualization.title,
            description=visualization.rationale,
            dataset=safe_dataset_names[visualization.transform_name or visualization.query_name],
            mark=visualization.mark,
            encoding=visualization.encoding.model_dump(mode="json", by_alias=True),
        ) for index, visualization in enumerate(visualizations)]
        data = DataVisualizationData(
            title=title,
            body=body,
            datasets={
                safe_dataset_names[name]: [dict(row) for row in rows]
                for name, rows in datasets.items()
            },
            views=views,
            layout=VisualizationLayout(columns=min(max(columns, 1), 3)),
            queryResults={safe_dataset_names[name]: dict(result) for name, result in (query_results or {}).items() if name in safe_dataset_names},
        )
        return Widget(id=widget_id, type=WidgetType.DATA_VISUALIZATION, data=data.model_dump(mode="json", by_alias=True))


def fields_from_result(
    rows: Sequence[Mapping[str, Any]],
    *,
    labels: Mapping[str, str] | None = None,
    types: Mapping[str, ColumnType] | None = None,
    exclude: Iterable[str] = ("id", "_capabilities"),
) -> tuple[FieldPresentation, ...]:
    """Build a deterministic field catalog from a governed tabular result.

    This is useful for novel BI groupings. The query layer still supplies type
    hints so amounts, dates, and percentages are never inferred from strings.
    """

    labels = labels or {}
    types = types or {}
    excluded = set(exclude)
    ordered_keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in excluded and key not in ordered_keys:
                ordered_keys.append(key)
    return tuple(
        FieldPresentation(
            key=key,
            label=labels.get(key, key.replace("_", " ").replace("Minor", "").title()),
            type=types.get(key, "text"),
            align="right" if types.get(key) in {"money", "number", "percentage"} else "left",
            priority="primary" if index < 2 else "secondary",
        )
        for index, key in enumerate(ordered_keys)
    )


def _has_display_value(rows: Sequence[Mapping[str, Any]], key: str) -> bool:
    for row in rows:
        value = row.get(key)
        if value is not None and value != "" and value != []:
            return True
    return False
