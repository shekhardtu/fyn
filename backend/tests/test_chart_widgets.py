"""The data_chart lane: deterministic builder checks and harness wiring."""
from __future__ import annotations

import json
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.event_time import from_local_parts
from app.models import Category, Transaction
from app.schemas import WIDGET_DATA_MODELS, Widget, WidgetType
from app.seed import default_user
from app.services import conversation as conversation_service
from app.services.analysis_harness import execute_analysis_template
from app.services.analysis_tools import (
    RUN_ANALYSIS_TOOL_NAME,
    AnalysisToolContext,
    build_analysis_tools,
)
from app.services.chart_widgets import CHART_ROW_CAP, ChartSpecError, build_chart_widget, dataset_id
from app.services.conversation import get_or_create_conversation
from app.services.manifest import native_manifest_fingerprint
from app.services.semantic import AnalysisPlan, AnalysisToolProposal
from app.visualization_contracts import (
    VisualEncodingContract,
    VisualFieldEncoding,
    VisualizationView,
)


def occurred(day: date):
    return from_local_parts(day, None, "Asia/Kolkata")


LINEAGE = {
    "origin": "analysis",
    "manifestHash": "a" * 64,
    "executedAt": "2026-08-18T00:00:00+00:00",
}

ROWS = [
    {"category": "Food", "value_minor": 30_000},
    {"category": "Transport", "value_minor": 10_000},
]


def _bar_view(**overrides) -> VisualizationView:
    values = {
        "id": "spend-by-category",
        "title": "Spending by category",
        "dataset": "category_spend",
        "mark": "bar",
        "encoding": VisualEncodingContract(
            x=VisualFieldEncoding(field="category", type="nominal", valueType="category"),
            y=VisualFieldEncoding(field="value_minor", type="quantitative", valueType="money_minor"),
        ),
        **overrides,
    }
    return VisualizationView(**values)


# ── Builder checks ───────────────────────────────────────────────────────────


def test_builder_produces_a_registered_widget_with_unscaled_minor_rows():
    widget = build_chart_widget(_bar_view(), ROWS, "INR", LINEAGE)

    assert widget.type is WidgetType.DATA_CHART
    assert widget.actions == []
    data = WIDGET_DATA_MODELS[WidgetType.DATA_CHART].model_validate(widget.data)
    # Minor units pass through untouched; the frontend divides for display.
    assert [row["value_minor"] for row in data.rows] == [30_000, 10_000]
    assert widget.data["currency"] == "INR"
    assert widget.data["lineage"] == LINEAGE


def test_builder_rejects_an_encoded_field_missing_from_the_rows():
    view = _bar_view(encoding=VisualEncodingContract(
        x=VisualFieldEncoding(field="category", type="nominal", valueType="category"),
        y=VisualFieldEncoding(field="value", type="quantitative", valueType="money_minor"),
    ))
    with pytest.raises(ChartSpecError) as failure:
        build_chart_widget(view, ROWS, "INR", LINEAGE)
    assert failure.value.code == "unknown_field"


def test_builder_rejects_a_tooltip_field_missing_from_the_rows():
    view = _bar_view(encoding=VisualEncodingContract(
        x=VisualFieldEncoding(field="category", type="nominal", valueType="category"),
        y=VisualFieldEncoding(field="value_minor", type="quantitative", valueType="money_minor"),
        tooltip=[VisualFieldEncoding(field="merchant", type="nominal", valueType="string")],
    ))
    with pytest.raises(ChartSpecError) as failure:
        build_chart_widget(view, ROWS, "INR", LINEAGE)
    assert failure.value.code == "unknown_field"


def test_builder_rejects_an_empty_result():
    with pytest.raises(ChartSpecError) as failure:
        build_chart_widget(_bar_view(), [], "INR", LINEAGE)
    assert failure.value.code == "empty_result"


def test_builder_rejects_an_arc_that_encodes_axes():
    view = _bar_view(mark="arc")
    with pytest.raises(ChartSpecError) as failure:
        build_chart_widget(view, ROWS, "INR", LINEAGE)
    assert failure.value.code == "mark_encoding_mismatch"


def test_builder_rejects_an_axis_mark_that_encodes_theta():
    view = _bar_view(encoding=VisualEncodingContract(
        theta=VisualFieldEncoding(field="value_minor", type="quantitative", valueType="money_minor"),
        color=VisualFieldEncoding(field="category", type="nominal", valueType="category"),
    ))
    with pytest.raises(ChartSpecError) as failure:
        build_chart_widget(view, ROWS, "INR", LINEAGE)
    assert failure.value.code == "mark_encoding_mismatch"


def test_builder_accepts_the_arc_theta_color_contract():
    view = _bar_view(mark="arc", encoding=VisualEncodingContract(
        theta=VisualFieldEncoding(field="value_minor", type="quantitative", valueType="money_minor"),
        color=VisualFieldEncoding(field="category", type="nominal", valueType="category"),
    ))
    widget = build_chart_widget(view, ROWS, "INR", LINEAGE)
    assert widget.data["view"]["mark"] == "arc"


def test_builder_enforces_the_row_cap():
    rows = [{"category": f"c{index}", "value_minor": index} for index in range(CHART_ROW_CAP + 1)]
    with pytest.raises(ChartSpecError) as failure:
        build_chart_widget(_bar_view(), rows, "INR", LINEAGE)
    assert failure.value.code == "row_cap_exceeded"


def test_dataset_id_normalizes_free_query_names():
    assert dataset_id("Category comparison") == "category_comparison"
    assert dataset_id("category_spend") == "category_spend"


# ── Harness wiring ───────────────────────────────────────────────────────────


def _seed_category_spending(db) -> None:
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    transport = db.scalar(select(Category).where(Category.slug == "transport"))
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=30_000, currency="INR", category_id=food.id, transaction_at=occurred(date.today())),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", category_id=transport.id, transaction_at=occurred(date.today())),
    ])
    db.flush()


def _chart_plan(today: date, *, encoding: dict | None = None, dataset: str = "category_spend") -> dict:
    return {
        "objective": "descriptive",
        "analysis_type": "semantic_query",
        "safe_reasoning_summary": ["Aggregate expenses by category", "Draw the requested chart"],
        "queries": [{
            "name": "category_spend",
            "metric": "gross_spend",
            "dimensions": ["category"],
            "start_date": today.replace(day=1).isoformat(),
            "end_date": today.isoformat(),
        }],
        "visualizations": [{
            "id": "spend-by-category",
            "title": "Spending by category",
            "dataset": dataset,
            "mark": "bar",
            "encoding": encoding or {
                "x": {"field": "category", "type": "nominal", "valueType": "category"},
                "y": {"field": "value_minor", "type": "quantitative", "valueType": "money_minor"},
            },
        }],
    }


def _run_tool(db, user, conversation, today):
    context = AnalysisToolContext(
        db=db,
        user_id=user.id,
        conversation_id=conversation.id,
        today=today,
        timezone_name=user.timezone,
        question="Chart my spending by category this month",
    )
    return next(
        tool for tool in build_analysis_tools(context)
        if tool.name == RUN_ANALYSIS_TOOL_NAME
    )


def test_plan_with_a_visualization_yields_exactly_one_valid_data_chart_widget(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    today = conversation_service._local_today(user)
    _seed_category_spending(db)
    run_tool = _run_tool(db, user, conversation, today)

    payload = run_tool.entrypoint(
        name="Spending by category chart",
        intent_signature="category spending chart",
        plan_json=json.dumps(_chart_plan(today)),
    )

    assert payload.get("kind") == "governed_analysis"
    assert "chart_errors" not in payload
    widgets = payload["widgets"]
    assert len(widgets) == 1
    raw_widget = widgets[0]
    assert raw_widget["type"] == "data_chart"
    # The widget's rows are exactly the executed query result rows.
    assert raw_widget["data"]["rows"] == payload["query_results"][0]["rows"]
    assert [row["value_minor"] for row in raw_widget["data"]["rows"]] == [30_000, 10_000]
    Widget.model_validate(raw_widget)
    data = WIDGET_DATA_MODELS[WidgetType.DATA_CHART].model_validate(raw_widget["data"])
    assert data.currency == "INR"
    assert data.lineage.origin == "analysis"
    assert data.lineage.manifest_hash == native_manifest_fingerprint()
    assert data.lineage.executed_at


def test_failed_chart_spec_degrades_loudly_without_failing_the_analysis(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    today = conversation_service._local_today(user)
    _seed_category_spending(db)
    run_tool = _run_tool(db, user, conversation, today)

    payload = run_tool.entrypoint(
        name="Spending by category chart",
        intent_signature="category spending chart",
        plan_json=json.dumps(_chart_plan(today, encoding={
            "x": {"field": "category", "type": "nominal", "valueType": "category"},
            "y": {"field": "not_a_row_key", "type": "quantitative", "valueType": "money_minor"},
        })),
    )

    # The analysis stands; the chart does not, and the refusal is recorded.
    assert payload.get("kind") == "governed_analysis"
    assert payload["query_results"][0]["rows"]
    assert "widgets" not in payload
    assert payload["chart_errors"] == [{
        "view": "spend-by-category",
        "dataset": "category_spend",
        "code": "unknown_field",
        "detail": payload["chart_errors"][0]["detail"],
    }]


def test_unknown_dataset_is_recorded_not_silently_passed(db):
    user = default_user(db)
    conversation = get_or_create_conversation(db, user)
    today = conversation_service._local_today(user)
    _seed_category_spending(db)
    run_tool = _run_tool(db, user, conversation, today)

    payload = run_tool.entrypoint(
        name="Spending by category chart",
        intent_signature="category spending chart",
        plan_json=json.dumps(_chart_plan(today, dataset="not_a_query")),
    )

    assert "widgets" not in payload
    assert payload["chart_errors"][0]["code"] == "unknown_dataset"


def test_direct_harness_execution_attaches_the_chart_to_the_result(db):
    """The conversation lane persists result.widgets, so the chart must ride
    the IntelligenceResult itself, not only the tool payload."""
    user = default_user(db)
    _seed_category_spending(db)
    today = date.today()
    proposal = AnalysisToolProposal(
        name="Spending by category chart",
        description="Chart recorded spending by category for the current month.",
        intent_signature="category spending chart",
        plan=AnalysisPlan.model_validate(_chart_plan(today)),
    )

    generated = execute_analysis_template(db, user.id, uuid4(), today, proposal)

    assert generated.result.chart_notes == []
    assert len(generated.result.widgets) == 1
    widget = generated.result.widgets[0]
    assert widget.type is WidgetType.DATA_CHART
    assert [row["value_minor"] for row in widget.data["rows"]] == [30_000, 10_000]
    # A view survives templatization only as this run's widget: the shared
    # template stores structure, never presentation text.
    assert "visualizations" not in generated.template.plan_template


def test_matching_dataset_by_normalized_query_name(db):
    user = default_user(db)
    _seed_category_spending(db)
    today = date.today()
    plan = _chart_plan(today)
    plan["queries"][0]["name"] = "Category spend"
    proposal = AnalysisToolProposal(
        name="Spending by category chart",
        description="Chart recorded spending by category for the current month.",
        intent_signature="category spending chart normalized",
        plan=AnalysisPlan.model_validate(plan),
    )

    generated = execute_analysis_template(db, user.id, uuid4(), today, proposal)

    assert generated.result.chart_notes == []
    assert len(generated.result.widgets) == 1
