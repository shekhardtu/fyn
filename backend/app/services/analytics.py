from __future__ import annotations

from calendar import monthrange
from datetime import date
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..domain import TransactionType
from ..models import Category, Subcategory, Transaction
from .extraction import normalize_merchant
from .agent_tools import tool_contract
from .currency import user_currency, user_timezone
from ..event_time import local_date, utc_range_for_local_dates
from .tool_models import (
    BreakdownResult,
    CashPositionResult,
    ChangeDriversResult,
    DateRangeInput,
    EmptyInput,
    MonthlyComparisonResult,
    RecurringExpensesResult,
    SpendingSummaryInput,
    SpendingSummaryResult,
    SubcategoryBreakdownInput,
)
from .transactions import apply_canonical_transaction_scope, apply_expense_transaction_scope, expense_transactions


def month_bounds(day: date) -> tuple[date, date]:
    return day.replace(day=1), day.replace(day=monthrange(day.year, day.month)[1])


def shift_month(day: date, delta: int) -> date:
    index = day.year * 12 + day.month - 1 + delta
    return date(index // 12, index % 12 + 1, 1)


def _breakdown_rows(rows, currency: str) -> list[dict]:
    return [
        {
            "id": slug,
            "label": name,
            "amount_minor": int(total),
            "count": int(count),
            "currency": currency,
        }
        for slug, name, total, count in rows
    ]


@tool_contract(description=(
    "Return this user's expense total and transaction count for an inclusive date range, "
    "optionally filtered by an exact category slug. Money values are integer minor units."
), input_model=SpendingSummaryInput, output_model=SpendingSummaryResult)
def spending_summary(db: Session, user_id: UUID, start: date, end: date, category_slug: str | None = None) -> dict:
    currency = user_currency(db, user_id)
    start_at, end_at = utc_range_for_local_dates(start, end, user_timezone(db, user_id))
    stmt = apply_expense_transaction_scope(
        select(func.coalesce(func.sum(Transaction.amount_minor), 0), func.count(Transaction.id)),
        user_id,
        currency=currency,
    ).where(
        Transaction.transaction_at >= start_at,
        Transaction.transaction_at < end_at,
    )
    if category_slug:
        stmt = stmt.join(Category, Category.id == Transaction.category_id).where(Category.slug == category_slug)
    total, count = db.execute(stmt).one()
    return {"total_minor": int(total), "count": int(count), "currency": currency, "start": start.isoformat(), "end": end.isoformat(), "category": category_slug}


@tool_contract(description=(
    "Return this user's expense totals and counts grouped by category for an inclusive date range. "
    "Money values are integer minor units."
), input_model=DateRangeInput, output_model=BreakdownResult)
def category_breakdown(db: Session, user_id: UUID, start: date, end: date) -> list[dict]:
    currency = user_currency(db, user_id)
    start_at, end_at = utc_range_for_local_dates(start, end, user_timezone(db, user_id))
    statement = (
        apply_expense_transaction_scope(
            select(Category.slug, Category.name, func.sum(Transaction.amount_minor), func.count(Transaction.id))
            .join(Transaction, Transaction.category_id == Category.id),
            user_id,
            currency=currency,
        )
        .where(
            Transaction.transaction_at >= start_at,
            Transaction.transaction_at < end_at,
        )
        .group_by(Category.slug, Category.name)
        .order_by(func.sum(Transaction.amount_minor).desc())
    )
    return _breakdown_rows(db.execute(statement).all(), currency)


@tool_contract(description=(
    "Return this user's expense totals and counts grouped by subcategory for one exact category "
    "slug and inclusive date range. Money values are integer minor units."
), input_model=SubcategoryBreakdownInput, output_model=BreakdownResult)
def subcategory_breakdown(db: Session, user_id: UUID, start: date, end: date, category_slug: str) -> list[dict]:
    currency = user_currency(db, user_id)
    start_at, end_at = utc_range_for_local_dates(start, end, user_timezone(db, user_id))
    statement = (
        apply_expense_transaction_scope(
            select(Subcategory.slug, Subcategory.name, func.sum(Transaction.amount_minor), func.count(Transaction.id))
            .join(Transaction, Transaction.subcategory_id == Subcategory.id)
            .join(Category, Category.id == Transaction.category_id),
            user_id,
            currency=currency,
        )
        .where(
            Transaction.transaction_at >= start_at,
            Transaction.transaction_at < end_at,
            Category.slug == category_slug,
        )
        .group_by(Subcategory.slug, Subcategory.name)
        .order_by(func.sum(Transaction.amount_minor).desc())
    )
    return _breakdown_rows(db.execute(statement).all(), currency)


@tool_contract(description=(
    "Compare this user's month-to-date expense total with the same elapsed period in the previous "
    "month, using the authenticated user's current local date."
), input_model=EmptyInput, output_model=MonthlyComparisonResult)
def monthly_comparison(db: Session, user_id: UUID, today: date) -> dict:
    current_start, current_end = month_bounds(today)
    previous_start = shift_month(current_start, -1)
    previous_end = current_start.fromordinal(current_start.toordinal() - 1)
    current = spending_summary(db, user_id, current_start, min(today, current_end))
    # Compare equal elapsed days to avoid misleading partial-month claims.
    comparable_previous_end = min(previous_end, previous_start.replace(day=min(today.day, monthrange(previous_start.year, previous_start.month)[1])))
    previous = spending_summary(db, user_id, previous_start, comparable_previous_end)
    difference = current["total_minor"] - previous["total_minor"]
    pct = None if previous["total_minor"] == 0 else round(difference / previous["total_minor"] * 100, 1)
    return {"current": current, "previous": previous, "difference_minor": difference, "percent_change": pct}


@tool_contract(description=(
    "Return the categories driving the change between this user's month-to-date expenses and the "
    "same elapsed period in the previous month."
), input_model=EmptyInput, output_model=ChangeDriversResult)
def change_drivers(db: Session, user_id: UUID, today: date) -> dict:
    comparison = monthly_comparison(db, user_id, today)
    current_start = date.fromisoformat(comparison["current"]["start"])
    current_end = date.fromisoformat(comparison["current"]["end"])
    previous_start = date.fromisoformat(comparison["previous"]["start"])
    previous_end = date.fromisoformat(comparison["previous"]["end"])
    current = {row["id"]: row for row in category_breakdown(db, user_id, current_start, current_end)}
    previous = {row["id"]: row for row in category_breakdown(db, user_id, previous_start, previous_end)}
    drivers = []
    for slug in set(current) | set(previous):
        current_amount = current.get(slug, {}).get("amount_minor", 0)
        previous_amount = previous.get(slug, {}).get("amount_minor", 0)
        label = current.get(slug, previous.get(slug, {})).get("label", slug.title())
        drivers.append({"id": slug, "label": label, "current_minor": current_amount, "previous_minor": previous_amount, "change_minor": current_amount - previous_amount})
    drivers.sort(key=lambda item: item["change_minor"], reverse=True)
    return {**comparison, "drivers": drivers[:5]}


@tool_contract(description=(
    "Return this user's all-time recorded income, expenses, and net cash position in integer minor "
    "units, based only on canonical non-deleted transactions."
), input_model=EmptyInput, output_model=CashPositionResult)
def cash_position(db: Session, user_id: UUID) -> dict:
    currency = user_currency(db, user_id)
    statement = apply_canonical_transaction_scope(
        select(
            func.coalesce(func.sum(case((Transaction.transaction_type == TransactionType.INCOME, Transaction.amount_minor), else_=0)), 0),
            func.coalesce(func.sum(case((Transaction.transaction_type == TransactionType.EXPENSE, Transaction.amount_minor), else_=0)), 0),
        ),
        user_id,
        currency=currency,
    )
    income, expenses = db.execute(statement).one()
    return {"income_minor": int(income), "expenses_minor": int(expenses), "net_minor": int(income) - int(expenses), "currency": currency}


@tool_contract(description=(
    "Detect this user's recurring expense patterns from canonical non-deleted transactions and "
    "return merchant, amount, cadence, occurrence count, and last date."
), input_model=EmptyInput, output_model=RecurringExpensesResult)
def recurring_expenses(db: Session, user_id: UUID) -> list[dict]:
    currency = user_currency(db, user_id)
    timezone_name = user_timezone(db, user_id)
    transactions = list(db.scalars(expense_transactions(user_id, currency=currency).where(
        Transaction.merchant_name.is_not(None),
    ).order_by(Transaction.transaction_at)))
    groups: dict[tuple[str, int], list[Transaction]] = {}
    for transaction in transactions:
        key = (normalize_merchant(transaction.merchant_name) or "", transaction.amount_minor)
        groups.setdefault(key, []).append(transaction)
    recurring = []
    for (merchant, amount), items in groups.items():
        if len(items) < 2:
            continue
        days = [local_date(item.transaction_at, timezone_name) for item in items]
        gaps = [(right - left).days for left, right in zip(days, days[1:])]
        monthly = sum(20 <= gap <= 40 for gap in gaps) >= max(1, len(gaps) - 1)
        weekly = sum(5 <= gap <= 9 for gap in gaps) >= max(1, len(gaps) - 1)
        if not monthly and not weekly:
            continue
        recurring.append({
            "id": merchant,
            "merchant": items[-1].merchant_name,
            "amount_minor": amount,
            "currency": items[-1].currency,
            "cadence": "monthly" if monthly else "weekly",
            "occurrences": len(items),
            "last_date": days[-1].isoformat(),
        })
    return sorted(recurring, key=lambda item: item["amount_minor"], reverse=True)
