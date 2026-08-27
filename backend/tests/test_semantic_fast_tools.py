from __future__ import annotations

from datetime import date
from uuid import uuid4

from sqlalchemy import select

from app.event_time import from_local_parts
from app.models import Category, Transaction
from app.seed import default_user
from app.services.analysis_tools import AnalysisToolContext, build_analysis_tools
from app.services.semantic_fast_tools import (
    CATEGORY_VOLATILITY_TOOL_NAME,
    DISCRETIONARY_CAP_TOOL_NAME,
    ELAPSED_MONTH_COMPARISON_TOOL_NAME,
    MONTH_TO_DATE_SPENDING_TOOL_NAME,
    build_semantic_fast_tools,
)
from app.services.sql_analysis import DESCRIBE_SQL_SCHEMA_TOOL_NAME, RUN_SQL_TOOL_NAME


def _context(db, user, question: str) -> AnalysisToolContext:
    return AnalysisToolContext(
        db=db,
        user_id=user.id,
        conversation_id=uuid4(),
        today=date(2026, 8, 17),
        timezone_name="Asia/Kolkata",
        question=question,
    )


def _expense(user, category, month: int, amount_minor: int) -> Transaction:
    return Transaction(
        user_id=user.id,
        transaction_type="expense",
        amount_minor=amount_minor,
        currency="INR",
        category_id=category.id,
        spend_nature="discretionary",
        transaction_at=from_local_parts(
            date(2026, month, 10), None, "Asia/Kolkata"
        ),
    )


def test_category_volatility_capability_returns_complete_full_month_matrix(db):
    user = default_user(db)
    category = db.scalar(select(Category).where(Category.slug == "food"))
    db.add_all([
        _expense(user, category, 5, 100_000),
        _expense(user, category, 6, 300_000),
        _expense(user, category, 7, 200_000),
    ])
    db.commit()
    context = _context(
        db,
        user,
        "Compare monthly spending by category and identify the most volatile category.",
    )

    tools = build_semantic_fast_tools(context)
    payload = tools[0].entrypoint()

    assert [tool.name for tool in tools] == [CATEGORY_VOLATILITY_TOOL_NAME]
    assert [period["month"] for period in payload["periods"]] == [
        "May 2026", "June 2026", "July 2026",
    ]
    assert payload["categories"][0] == {
        "category": category.name,
        "monthly_values_minor": [100_000, 300_000, 200_000],
        "lowest_month_minor": 100_000,
        "highest_month_minor": 300_000,
        "volatility_range_minor": 200_000,
        "volatility_rank": 1,
    }
    assert payload["empty_result"] is False


def test_discretionary_cap_capability_computes_average_cap_and_rank(db):
    user = default_user(db)
    category = db.scalar(select(Category).where(Category.slug == "food"))
    db.add_all([
        _expense(user, category, 5, 100_000),
        _expense(user, category, 6, 200_000),
        _expense(user, category, 7, 300_000),
    ])
    db.commit()
    context = _context(
        db,
        user,
        "Set a discretionary cap 10% below my historical average.",
    )

    tools = build_semantic_fast_tools(context)
    payload = tools[0].entrypoint(reduction_percent=10)

    assert [tool.name for tool in tools] == [DISCRETIONARY_CAP_TOOL_NAME]
    assert payload["historical_average_minor"] == 200_000
    assert payload["fixed_monthly_cap_minor"] == 180_000
    assert payload["required_monthly_reduction_minor"] == 20_000
    assert payload["categories"][0]["reduction_minor"] == 20_000
    assert payload["categories"][0]["reduction_rank"] == 1
    assert payload["empty_result"] is False


def test_exact_semantic_capability_keeps_sql_fallback_without_schema_turn(db):
    user = default_user(db)
    context = _context(
        db,
        user,
        "Using the last three full months, set a discretionary cap below my historical average.",
    )

    names = {tool.name for tool in build_analysis_tools(context)}

    assert DISCRETIONARY_CAP_TOOL_NAME in names
    assert RUN_SQL_TOOL_NAME in names
    assert DESCRIBE_SQL_SCHEMA_TOOL_NAME not in names


def test_elapsed_month_comparison_aligns_days_and_ranks_category_drivers(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    travel = db.scalar(select(Category).where(Category.slug == "travel"))
    db.add_all([
        _expense(user, food, 7, 100_000),
        _expense(user, food, 8, 300_000),
        _expense(user, travel, 7, 200_000),
        _expense(user, travel, 8, 150_000),
    ])
    db.commit()
    context = _context(
        db,
        user,
        "Compare my spending this month with the same elapsed days last month and show the three largest category drivers.",
    )

    tools = build_semantic_fast_tools(context)
    payload = tools[0].entrypoint()

    assert [tool.name for tool in tools] == [ELAPSED_MONTH_COMPARISON_TOOL_NAME]
    assert payload["current_period"] == {
        "start": "2026-08-01",
        "end": "2026-08-17",
        "elapsed_days": 17,
        "total_minor": 450_000,
    }
    assert payload["previous_period"] == {
        "start": "2026-07-01",
        "end": "2026-07-17",
        "elapsed_days": 17,
        "total_minor": 300_000,
    }
    assert payload["difference_minor"] == 150_000
    assert payload["categories"][0]["category"] == food.name
    assert payload["categories"][0]["difference_minor"] == 200_000
    assert payload["categories"][0]["driver_rank"] == 1


def test_month_to_date_capability_returns_net_total_and_period(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add(_expense(user, food, 8, 250_000))
    db.commit()
    context = _context(db, user, "How much did I spend this month?")

    tools = build_semantic_fast_tools(context)
    payload = tools[0].entrypoint()

    assert [tool.name for tool in tools] == [MONTH_TO_DATE_SPENDING_TOOL_NAME]
    assert payload["period"] == {
        "start": "2026-08-01",
        "end": "2026-08-17",
        "elapsed_days": 17,
    }
    assert payload["total_minor"] == 250_000
    assert payload["transaction_count"] == 1
    assert payload["empty_result"] is False
