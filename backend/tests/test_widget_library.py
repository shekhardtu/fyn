from pydantic import ValidationError
import pytest

from app.services.widget_library import FieldPresentation, RowCapability, TableBlueprint, WidgetLibrary, fields_from_result
from app.services.semantic import VisualEncoding, VisualEncodingSet, VisualizationSpec


def test_widget_library_discovers_only_populated_business_fields():
    widget = WidgetLibrary.data_table(
        widget_id="invoice-table",
        title="Open invoices",
        rows=[{
            "id": "invoice-1",
            "customer": "Acme",
            "amountMinor": 125_000,
            "currency": "INR",
            "dueDate": "2026-08-30",
            "notes": None,
            "_capabilities": ["invoice.view"],
        }],
        blueprint=TableBlueprint(
            fields=(
                FieldPresentation("customer", "Customer", "entity", "primary"),
                FieldPresentation("dueDate", "Due date", "date"),
                FieldPresentation("notes", "Notes", "text", "detail"),
                FieldPresentation("amountMinor", "Amount", "money", "primary", "right", "currency"),
            ),
            row_actions=(RowCapability("view", "View", "edit_saved_transaction", "invoiceId", icon="view", capability="invoice.view"),),
        ),
    )

    assert widget.type == "data_table"
    assert [column["key"] for column in widget.data["columns"]] == ["customer", "dueDate", "amountMinor"]
    assert widget.data["rowActions"][0]["action"] == "edit_saved_transaction"


def test_widget_library_rejects_capability_actions_without_row_authority():
    with pytest.raises(ValidationError, match="requires row capabilities"):
        WidgetLibrary.data_table(
            widget_id="unsafe",
            title="Unsafe",
            rows=[{"id": "1", "name": "Record"}],
            blueprint=TableBlueprint(
                fields=(FieldPresentation("name", "Name", "entity", "primary"),),
                row_actions=(RowCapability("remove", "Remove", "request_remove_transaction", "recordId", capability="record.remove"),),
            ),
        )


def test_fields_from_result_preserves_novel_dimension_order_and_types():
    fields = fields_from_result(
        [{"month": "2026-08", "category": "Food", "value": 123_400}],
        labels={"value": "Spend"},
        types={"value": "money"},
    )

    assert [field.key for field in fields] == ["month", "category", "value"]
    assert fields[-1].label == "Spend"
    assert fields[-1].type == "money"
    assert fields[-1].align == "right"


def test_widget_library_builds_and_validates_generic_chart_data():
    widget = WidgetLibrary.data_chart(
        widget_id="amount-chart",
        title="Transactions by amount",
        chart_type="bar",
        rows=[{"transaction": "tx-1", "merchant": "Toit", "value": 77_700}],
        x_key="transaction",
        x_label="Transaction",
        x_type="category",
        value_label="Amount",
        value_type="money",
        label_keys=["merchant"],
    )

    assert widget.type == "data_chart"
    assert widget.data["series"][0]["valueType"] == "money"
    assert widget.data["labelKeys"] == ["merchant"]


def test_widget_library_rejects_chart_fields_missing_from_rows():
    with pytest.raises(ValidationError, match="missing referenced fields"):
        WidgetLibrary.data_chart(
            widget_id="invalid-chart",
            title="Invalid chart",
            chart_type="bar",
            rows=[{"merchant": "Toit", "value": 77_700}],
            x_key="transaction",
            x_label="Transaction",
            x_type="category",
        )


def test_widget_library_composes_multiple_renderer_neutral_bi_views():
    views = [
        VisualizationSpec(
            name="Monthly trend", query_name="monthly", mark="line",
            encoding=VisualEncodingSet(
                x=VisualEncoding(field="time_bucket", type="temporal", value_type="datetime"),
                y=VisualEncoding(field="value", type="quantitative", value_type="money_minor"),
            ),
            title="Monthly cash flow", rationale="Show the ordered trend.",
        ),
        VisualizationSpec(
            name="Category composition", query_name="categories", mark="arc",
            encoding=VisualEncodingSet(
                theta=VisualEncoding(field="value", type="quantitative", value_type="money_minor"),
                color=VisualEncoding(field="category", type="nominal", value_type="category"),
            ),
            title="Spending composition", rationale="Show each category share.",
        ),
    ]

    widget = WidgetLibrary.data_visualization(
        widget_id="dashboard",
        title="Cash-flow dashboard",
        datasets={
            "monthly": [{"time_bucket": "2026-08", "value": 50_000}],
            "categories": [{"category": "Food", "value": 30_000}],
        },
        visualizations=views,
        columns=2,
    )

    assert widget.type == "data_visualization"
    assert widget.data["layout"]["columns"] == 2
    assert [view["mark"] for view in widget.data["views"]] == ["line", "arc"]
