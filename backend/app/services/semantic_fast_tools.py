"""Small authenticated analysis capabilities for common, failure-prone shapes.

These are tools the universal Operator may select, not deterministic response
routes. Arbitrary governed SQL remains mounted as the long-tail fallback. The
capabilities keep common period/category arithmetic out of generated SQL so a
correct financial answer needs one tool action instead of schema discovery and
query-repair turns.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Any

from sqlalchemy import case, func, select

from ..domain import SpendNature, TransactionType
from ..event_time import utc_range_for_local_dates
from ..models import Category, Transaction
from .agent_tools import bind_schema_tool
from .currency import format_money_minor, user_currency
from .finance_time import month_bounds, shift_month
from .transactions import apply_canonical_transaction_scope


CATEGORY_VOLATILITY_TOOL_NAME = "analyze_category_volatility"
DISCRETIONARY_CAP_TOOL_NAME = "analyze_discretionary_spending_cap"
ELAPSED_MONTH_COMPARISON_TOOL_NAME = "analyze_elapsed_month_category_comparison"
MONTH_TO_DATE_SPENDING_TOOL_NAME = "analyze_month_to_date_spending"
THREE_MONTH_RECONCILIATION_TOOL_NAME = "analyze_three_month_spending_reconciliation"
SEMANTIC_FAST_TOOL_NAMES = frozenset({
    CATEGORY_VOLATILITY_TOOL_NAME,
    DISCRETIONARY_CAP_TOOL_NAME,
    ELAPSED_MONTH_COMPARISON_TOOL_NAME,
    MONTH_TO_DATE_SPENDING_TOOL_NAME,
    THREE_MONTH_RECONCILIATION_TOOL_NAME,
})


def _last_full_months(today: date, count: int = 3) -> list[tuple[date, date]]:
    current_month = today.replace(day=1)
    periods: list[tuple[date, date]] = []
    for offset in reversed(range(1, count + 1)):
        start = shift_month(current_month, -offset)
        _, end = month_bounds(start)
        periods.append((start, end))
    return periods


def _signed_amount():
    return case(
        (Transaction.transaction_type == TransactionType.REFUND.value, -Transaction.amount_minor),
        else_=Transaction.amount_minor,
    )


def _category_totals(
    context: Any,
    start: date,
    end: date,
    *,
    discretionary_only: bool,
) -> list[dict[str, Any]]:
    currency = user_currency(context.db, context.user_id)
    start_at, end_at = utc_range_for_local_dates(
        start,
        end,
        context.timezone_name,
    )
    statement = (
        select(
            Category.name,
            func.coalesce(func.sum(_signed_amount()), 0),
            func.count(Transaction.id),
        )
        .select_from(Transaction)
        .outerjoin(Category, Category.id == Transaction.category_id)
    )
    statement = apply_canonical_transaction_scope(
        statement,
        context.user_id,
        currency=currency,
    ).where(
        Transaction.transaction_at >= start_at,
        Transaction.transaction_at < end_at,
        Transaction.transaction_type.in_([
            TransactionType.EXPENSE.value,
            TransactionType.REFUND.value,
        ]),
    )
    if discretionary_only:
        statement = statement.where(
            Transaction.spend_nature == SpendNature.DISCRETIONARY.value
        )
    statement = statement.group_by(Category.name).order_by(Category.name)
    return [
        {
            "category": category_name or "Uncategorized",
            "amount_minor": int(amount_minor),
            "transaction_count": int(transaction_count),
        }
        for category_name, amount_minor, transaction_count
        in context.db.execute(statement).all()
    ]


def _gross_refund_category_rows(
    context: Any,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    currency = user_currency(context.db, context.user_id)
    start_at, end_at = utc_range_for_local_dates(start, end, context.timezone_name)
    statement = (
        select(
            Category.name,
            func.coalesce(func.sum(case(
                (Transaction.transaction_type == TransactionType.EXPENSE.value, Transaction.amount_minor),
                else_=0,
            )), 0),
            func.coalesce(func.sum(case(
                (Transaction.transaction_type == TransactionType.REFUND.value, Transaction.amount_minor),
                else_=0,
            )), 0),
            func.count(Transaction.id),
        )
        .select_from(Transaction)
        .outerjoin(Category, Category.id == Transaction.category_id)
    )
    statement = apply_canonical_transaction_scope(
        statement,
        context.user_id,
        currency=currency,
    ).where(
        Transaction.transaction_at >= start_at,
        Transaction.transaction_at < end_at,
        Transaction.transaction_type.in_([
            TransactionType.EXPENSE.value,
            TransactionType.REFUND.value,
        ]),
    )
    statement = statement.group_by(Category.name).order_by(Category.name)
    return [
        {
            "category": category_name or "Uncategorized",
            "gross_expenses_minor": int(gross_minor),
            "refunds_minor": int(refunds_minor),
            "net_spending_minor": int(gross_minor) - int(refunds_minor),
            "transaction_count": int(transaction_count),
        }
        for category_name, gross_minor, refunds_minor, transaction_count
        in context.db.execute(statement).all()
    ]


def _period_category_matrix(
    context: Any,
    *,
    discretionary_only: bool,
) -> tuple[list[dict[str, Any]], dict[str, list[int]], int, str]:
    periods: list[dict[str, Any]] = []
    category_values: dict[str, list[int]] = {}
    transaction_count = 0
    full_months = _last_full_months(context.today)
    for index, (start, end) in enumerate(full_months):
        category_rows = _category_totals(
            context,
            start,
            end,
            discretionary_only=discretionary_only,
        )
        month_total = sum(row["amount_minor"] for row in category_rows)
        month_count = sum(row["transaction_count"] for row in category_rows)
        transaction_count += month_count
        periods.append({
            "month": start.strftime("%B %Y"),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "total_minor": month_total,
            "transaction_count": month_count,
        })
        present = {row["category"]: row["amount_minor"] for row in category_rows}
        for category in set(category_values).union(present):
            category_values.setdefault(category, [0] * len(full_months))[index] = present.get(category, 0)
    return periods, category_values, transaction_count, user_currency(
        context.db, context.user_id
    )


def _empty_guidance(transaction_count: int) -> str | None:
    if transaction_count:
        return None
    return (
        "The authenticated three-full-month scope contains no matching records. "
        "State that absence directly; do not invent a ranking or a positive baseline."
    )


def _build_category_volatility_tool(context: Any):
    def analyze_category_volatility() -> dict[str, Any]:
        periods, values, transaction_count, currency = _period_category_matrix(
            context,
            discretionary_only=False,
        )
        categories = []
        for category, monthly_values in values.items():
            categories.append({
                "category": category,
                "monthly_values_minor": monthly_values,
                "lowest_month_minor": min(monthly_values),
                "highest_month_minor": max(monthly_values),
                "volatility_range_minor": max(monthly_values) - min(monthly_values),
            })
        categories.sort(
            key=lambda row: (row["volatility_range_minor"], row["highest_month_minor"]),
            reverse=True,
        )
        for rank, row in enumerate(categories, 1):
            row["volatility_rank"] = rank
        return {
            "kind": "semantic_financial_analysis",
            "analysis": "three_full_month_category_volatility",
            "currency": currency,
            "periods": periods,
            "categories": categories,
            "transaction_count": transaction_count,
            "empty_result": transaction_count == 0,
            "empty_result_guidance": _empty_guidance(transaction_count),
        }

    return bind_schema_tool(
        analyze_category_volatility,
        name=CATEGORY_VOLATILITY_TOOL_NAME,
        description=(
            "Run the exact authenticated analysis for comparing spending by category across "
            "the last three full calendar months and ranking category volatility by the "
            "highest-minus-lowest monthly net spend. Expense amounts add and refunds subtract; "
            "transfers are excluded. Prefer this over authoring SQL when the request matches."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        strict=True,
    )


def _build_discretionary_cap_tool(context: Any):
    def analyze_discretionary_spending_cap(
        reduction_percent: float,
    ) -> dict[str, Any]:
        percent = Decimal(str(reduction_percent))
        if percent <= 0 or percent > 100:
            return {"error": {
                "code": "invalid_reduction_percent",
                "detail": "reduction_percent must be greater than 0 and no more than 100.",
            }}
        periods, values, transaction_count, currency = _period_category_matrix(
            context,
            discretionary_only=True,
        )
        month_count = Decimal(len(periods))
        historical_average = (
            Decimal(sum(period["total_minor"] for period in periods)) / month_count
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        cap = (
            historical_average * (Decimal("100") - percent) / Decimal("100")
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        required_reduction = max(Decimal("0"), historical_average - cap)
        categories = []
        for category, monthly_values in values.items():
            monthly_average = (
                Decimal(sum(monthly_values)) / month_count
            ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            reduction = max(
                Decimal("0"),
                (monthly_average * percent / Decimal("100")).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                ),
            )
            categories.append({
                "category": category,
                "monthly_average_minor": int(monthly_average),
                "reduction_minor": int(reduction),
            })
        categories.sort(
            key=lambda row: (row["reduction_minor"], row["monthly_average_minor"]),
            reverse=True,
        )
        for rank, row in enumerate(categories, 1):
            row["reduction_rank"] = rank
        return {
            "kind": "semantic_financial_analysis",
            "analysis": "three_full_month_discretionary_cap",
            "currency": currency,
            "reduction_percent": float(percent),
            "periods": periods,
            "historical_average_minor": int(historical_average),
            "fixed_monthly_cap_minor": int(cap),
            "required_monthly_reduction_minor": int(required_reduction),
            "categories": categories,
            "transaction_count": transaction_count,
            "empty_result": transaction_count == 0,
            "empty_result_guidance": _empty_guidance(transaction_count),
            "display": {
                "historical_average_minor": format_money_minor(int(historical_average), currency),
                "fixed_monthly_cap_minor": format_money_minor(int(cap), currency),
                "required_monthly_reduction_minor": format_money_minor(int(required_reduction), currency),
            },
        }

    return bind_schema_tool(
        analyze_discretionary_spending_cap,
        name=DISCRETIONARY_CAP_TOOL_NAME,
        description=(
            "Run the exact authenticated analysis for a fixed monthly discretionary-spending "
            "cap below the historical average of the last three full calendar months, including "
            "the category reductions ranked by size. Expense amounts add and refunds subtract; "
            "transfers and non-discretionary records are excluded. Prefer this over authoring SQL "
            "when the request matches."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reduction_percent": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 100,
                    "description": "Requested percentage below the historical monthly average.",
                },
            },
            "required": ["reduction_percent"],
            "additionalProperties": False,
        },
        strict=True,
    )


def _build_elapsed_month_comparison_tool(context: Any):
    def analyze_elapsed_month_category_comparison() -> dict[str, Any]:
        current_start = context.today.replace(day=1)
        previous_start = shift_month(current_start, -1)
        _, previous_month_end = month_bounds(previous_start)
        previous_end = min(
            previous_month_end,
            previous_start + timedelta(days=context.today.day - 1),
        )
        current_rows = _category_totals(
            context,
            current_start,
            context.today,
            discretionary_only=False,
        )
        previous_rows = _category_totals(
            context,
            previous_start,
            previous_end,
            discretionary_only=False,
        )
        current_by_category = {
            row["category"]: row["amount_minor"] for row in current_rows
        }
        previous_by_category = {
            row["category"]: row["amount_minor"] for row in previous_rows
        }
        categories = []
        for category in set(current_by_category).union(previous_by_category):
            current_minor = current_by_category.get(category, 0)
            previous_minor = previous_by_category.get(category, 0)
            categories.append({
                "category": category,
                "current_period_minor": current_minor,
                "previous_period_minor": previous_minor,
                "difference_minor": current_minor - previous_minor,
                "absolute_driver_minor": abs(current_minor - previous_minor),
            })
        categories.sort(
            key=lambda row: row["absolute_driver_minor"],
            reverse=True,
        )
        for rank, row in enumerate(categories, 1):
            row["driver_rank"] = rank
        current_total = sum(current_by_category.values())
        previous_total = sum(previous_by_category.values())
        transaction_count = sum(
            row["transaction_count"] for row in [*current_rows, *previous_rows]
        )
        currency = user_currency(context.db, context.user_id)
        return {
            "kind": "semantic_financial_analysis",
            "analysis": "same_elapsed_days_month_category_comparison",
            "currency": currency,
            "current_period": {
                "start": current_start.isoformat(),
                "end": context.today.isoformat(),
                "elapsed_days": context.today.day,
                "total_minor": current_total,
            },
            "previous_period": {
                "start": previous_start.isoformat(),
                "end": previous_end.isoformat(),
                "elapsed_days": previous_end.day,
                "total_minor": previous_total,
            },
            "difference_minor": current_total - previous_total,
            "categories": categories,
            "transaction_count": transaction_count,
            "empty_result": transaction_count == 0,
            "empty_result_guidance": _empty_guidance(transaction_count),
            "display": {
                "current_total_minor": format_money_minor(current_total, currency),
                "previous_total_minor": format_money_minor(previous_total, currency),
                "difference_minor": format_money_minor(
                    current_total - previous_total,
                    currency,
                ),
            },
        }

    return bind_schema_tool(
        analyze_elapsed_month_category_comparison,
        name=ELAPSED_MONTH_COMPARISON_TOOL_NAME,
        description=(
            "Run the exact authenticated like-for-like comparison of this month through today's "
            "elapsed day against the same number of days last month, including category totals, "
            "absolute difference, and category drivers ranked by absolute change. Expense amounts "
            "add and refunds subtract; transfers are excluded. Prefer this over authoring SQL when "
            "the request matches."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        strict=True,
    )


def _build_month_to_date_spending_tool(context: Any):
    def analyze_month_to_date_spending() -> dict[str, Any]:
        start = context.today.replace(day=1)
        category_rows = _category_totals(
            context,
            start,
            context.today,
            discretionary_only=False,
        )
        category_rows.sort(
            key=lambda row: row["amount_minor"],
            reverse=True,
        )
        for rank, row in enumerate(category_rows, 1):
            row["category_rank"] = rank
        total_minor = sum(row["amount_minor"] for row in category_rows)
        transaction_count = sum(
            row["transaction_count"] for row in category_rows
        )
        currency = user_currency(context.db, context.user_id)
        return {
            "kind": "semantic_financial_analysis",
            "analysis": "month_to_date_spending",
            "currency": currency,
            "period": {
                "start": start.isoformat(),
                "end": context.today.isoformat(),
                "elapsed_days": context.today.day,
            },
            "total_minor": total_minor,
            "transaction_count": transaction_count,
            "categories": category_rows,
            "empty_result": transaction_count == 0,
            "empty_result_guidance": _empty_guidance(transaction_count),
            "display": {
                "total_minor": format_money_minor(total_minor, currency),
            },
        }

    return bind_schema_tool(
        analyze_month_to_date_spending,
        name=MONTH_TO_DATE_SPENDING_TOOL_NAME,
        description=(
            "Return the authenticated month-to-date net spending total from the first day of "
            "the current month through today, plus its category evidence. Expense amounts add "
            "and refunds subtract; transfers are excluded. Prefer this over authoring SQL for "
            "a simple current-month spending-total question."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        strict=True,
    )


def _build_three_month_reconciliation_tool(context: Any):
    def analyze_three_month_spending_reconciliation() -> dict[str, Any]:
        current_start = context.today.replace(day=1)
        previous_start = shift_month(current_start, -1)
        two_months_ago_start = shift_month(current_start, -2)
        _, previous_end = month_bounds(previous_start)
        _, two_months_ago_end = month_bounds(two_months_ago_start)
        period_bounds = [
            (two_months_ago_start, two_months_ago_end),
            (previous_start, previous_end),
            (current_start, context.today),
        ]
        periods: list[dict[str, Any]] = []
        category_totals: dict[str, dict[str, Any]] = {}
        transaction_count = 0
        for start, end in period_bounds:
            rows = _gross_refund_category_rows(context, start, end)
            gross_minor = sum(row["gross_expenses_minor"] for row in rows)
            refunds_minor = sum(row["refunds_minor"] for row in rows)
            period_count = sum(row["transaction_count"] for row in rows)
            transaction_count += period_count
            periods.append({
                "period": start.strftime("%B %Y"),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "gross_expenses_minor": gross_minor,
                "refunds_minor": refunds_minor,
                "net_spending_minor": gross_minor - refunds_minor,
                "transaction_count": period_count,
            })
            for row in rows:
                aggregate = category_totals.setdefault(row["category"], {
                    "category": row["category"],
                    "gross_expenses_minor": 0,
                    "refunds_minor": 0,
                    "net_spending_minor": 0,
                    "transaction_count": 0,
                })
                for field in (
                    "gross_expenses_minor",
                    "refunds_minor",
                    "net_spending_minor",
                    "transaction_count",
                ):
                    aggregate[field] += row[field]
        categories = sorted(
            category_totals.values(),
            key=lambda row: (row["net_spending_minor"], row["gross_expenses_minor"]),
            reverse=True,
        )
        for rank, row in enumerate(categories, 1):
            row["category_rank"] = rank
        gross_minor = sum(period["gross_expenses_minor"] for period in periods)
        refunds_minor = sum(period["refunds_minor"] for period in periods)
        currency = user_currency(context.db, context.user_id)
        return {
            "kind": "semantic_financial_analysis",
            "analysis": "current_month_to_date_plus_two_preceding_months_reconciliation",
            "currency": currency,
            "scope": {
                "start": two_months_ago_start.isoformat(),
                "end": context.today.isoformat(),
                "month_count": 3,
            },
            "gross_expenses_minor": gross_minor,
            "refunds_minor": refunds_minor,
            "net_spending_minor": gross_minor - refunds_minor,
            "periods": periods,
            "categories": categories,
            "transaction_count": transaction_count,
            "empty_result": transaction_count == 0,
            "empty_result_guidance": (
                None
                if transaction_count
                else "The authenticated three-month scope contains no expenses or refunds."
            ),
            "display": {
                "gross_expenses_minor": format_money_minor(gross_minor, currency),
                "refunds_minor": format_money_minor(refunds_minor, currency),
                "net_spending_minor": format_money_minor(gross_minor - refunds_minor, currency),
            },
        }

    return bind_schema_tool(
        analyze_three_month_spending_reconciliation,
        name=THREE_MONTH_RECONCILIATION_TOOL_NAME,
        description=(
            "Reconcile authenticated gross expenses, refunds, and net spending for exactly "
            "the current month-to-date plus the two preceding calendar months. Return explicit "
            "aggregate totals, all three period rows, and categories ranked by net spending. "
            "Transfers are excluded and each canonical transaction is counted once. Prefer this "
            "over authoring SQL when the request matches."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        strict=True,
    )


def build_semantic_fast_tools(context: Any) -> list[Any]:
    """Retrieve exact semantic capabilities; generic SQL always remains fallback."""

    question = context.question.casefold()
    tools: list[Any] = []
    if "categor" in question and re.search(r"\bvolatil(?:e|ity)\b", question):
        tools.append(_build_category_volatility_tool(context))
    if (
        "discretionary" in question
        and re.search(r"\b(?:cap|limit|ceiling)\b", question)
        and re.search(r"\b(?:average|baseline|historical)\b", question)
    ):
        tools.append(_build_discretionary_cap_tool(context))
    if (
        re.search(r"\bcompare\b", question)
        and re.search(r"\bthis month\b|\bcurrent month\b|\bmonth[- ]to[- ]date\b|\bmtd\b", question)
        and re.search(r"\blast month\b|\bprevious month\b", question)
        and re.search(r"\bcategor(?:y|ies)\b", question)
        and re.search(r"\bsame\b.{0,20}\b(?:day|elapsed)|\blike[- ]for[- ]like\b", question)
    ):
        tools.append(_build_elapsed_month_comparison_tool(context))
    if (
        re.search(r"\b(?:reconcile|reconciliation)\b", question)
        and re.search(r"\bgross\b", question)
        and re.search(r"\brefunds?\b", question)
        and re.search(r"\bnet\b", question)
        and re.search(r"\b(?:previous|preceding)\s+two\s+months?\b", question)
    ):
        tools.append(_build_three_month_reconciliation_tool(context))
    if (
        not re.search(r"\bcompare\b|\bversus\b|\bvs\.?\b", question)
        and re.search(r"\b(?:spend|spent|spending|expenses?)\b", question)
        and re.search(r"\b(?:this|current)\s+month\b|\bmonth[- ]to[- ]date\b|\bmtd\b", question)
        and re.search(r"\b(?:how much|total|sum)\b", question)
    ):
        tools.append(_build_month_to_date_spending_tool(context))
    return tools
