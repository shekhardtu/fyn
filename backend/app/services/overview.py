from __future__ import annotations

from calendar import monthrange
from datetime import date
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..domain import TransactionType
from ..event_time import utc_range_for_local_dates
from ..models import Category, Subcategory, Transaction
from .analytics import shift_month
from .currency import user_currency, user_timezone
from .transactions import apply_canonical_transaction_scope, apply_expense_transaction_scope


def _month_end(month: date) -> date:
    return month.replace(day=monthrange(month.year, month.month)[1])


def _period_end(month: date, today: date) -> date:
    return today if (month.year, month.month) == (today.year, today.month) else _month_end(month)


def _money_totals(db: Session, user_id: UUID, start: date, end: date, currency: str) -> dict:
    start_at, end_at = utc_range_for_local_dates(start, end, user_timezone(db, user_id))
    statement = apply_canonical_transaction_scope(
        select(
            func.coalesce(func.sum(case((Transaction.transaction_type == TransactionType.INCOME, Transaction.amount_minor), else_=0)), 0),
            func.coalesce(func.sum(case((Transaction.transaction_type == TransactionType.EXPENSE, Transaction.amount_minor), else_=0)), 0),
            func.coalesce(func.sum(case((Transaction.transaction_type == TransactionType.EXPENSE, 1), else_=0)), 0),
        ),
        user_id,
        currency=currency,
    ).where(Transaction.transaction_at >= start_at, Transaction.transaction_at < end_at)
    income, spent, expense_count = db.execute(statement).one()
    return {
        "income_minor": int(income),
        "spent_minor": int(spent),
        "expense_count": int(expense_count),
    }


def _expense_hierarchy(db: Session, user_id: UUID, start: date, end: date, currency: str) -> list[dict]:
    start_at, end_at = utc_range_for_local_dates(start, end, user_timezone(db, user_id))
    statement = (
        apply_expense_transaction_scope(
            select(
                Category.slug,
                Category.name,
                Subcategory.slug,
                Subcategory.name,
                func.coalesce(func.sum(Transaction.amount_minor), 0),
                func.count(Transaction.id),
            )
            .select_from(Transaction)
            .outerjoin(Category, Category.id == Transaction.category_id)
            .outerjoin(Subcategory, Subcategory.id == Transaction.subcategory_id),
            user_id,
            currency=currency,
        )
        .where(Transaction.transaction_at >= start_at, Transaction.transaction_at < end_at)
        .group_by(Category.slug, Category.name, Subcategory.slug, Subcategory.name)
    )

    categories: dict[str, dict] = {}
    for category_slug, category_name, subcategory_slug, subcategory_name, amount, count in db.execute(statement):
        category_id = category_slug or "uncategorized"
        category = categories.setdefault(category_id, {
            "id": category_id,
            "label": category_name or "Uncategorized",
            "amount_minor": 0,
            "count": 0,
            "subcategories_by_id": {},
        })
        amount_minor = int(amount)
        category["amount_minor"] += amount_minor
        category["count"] += int(count)
        subcategory_id = subcategory_slug or "other"
        subcategory = category["subcategories_by_id"].setdefault(subcategory_id, {
            "id": subcategory_id,
            "label": subcategory_name or "Other",
            "amount_minor": 0,
            "count": 0,
        })
        subcategory["amount_minor"] += amount_minor
        subcategory["count"] += int(count)

    spent_minor = sum(category["amount_minor"] for category in categories.values())
    result = []
    for category in categories.values():
        category_total = category["amount_minor"]
        category["share_percent"] = round(category_total / spent_minor * 100, 1) if spent_minor else 0
        category["subcategories"] = list(category.pop("subcategories_by_id").values())
        category["subcategories"].sort(key=lambda item: (-item["amount_minor"], item["label"]))
        for subcategory in category["subcategories"]:
            subcategory["share_percent"] = round(subcategory["amount_minor"] / category_total * 100, 1) if category_total else 0
        result.append(category)
    return sorted(result, key=lambda item: (-item["amount_minor"], item["label"]))


def overview_snapshot(db: Session, user_id: UUID, month: date, today: date) -> dict:
    """One deterministic, user-scoped briefing for a calendar month.

    The current month stops at today and compares against the same number of
    elapsed days in the previous month. Completed months compare whole months.
    """
    month_start = month.replace(day=1)
    current_month = today.replace(day=1)
    if month_start > current_month:
        raise ValueError("Overview month cannot be in the future")

    currency = user_currency(db, user_id)
    end = _period_end(month_start, today)
    previous_start = shift_month(month_start, -1)
    elapsed_day = end.day
    previous_end = previous_start.replace(day=min(elapsed_day, monthrange(previous_start.year, previous_start.month)[1]))
    if end == _month_end(month_start):
        previous_end = _month_end(previous_start)

    current = _money_totals(db, user_id, month_start, end, currency)
    previous = _money_totals(db, user_id, previous_start, previous_end, currency)
    change_minor = current["spent_minor"] - previous["spent_minor"]
    change_percent = None if previous["spent_minor"] == 0 else round(change_minor / previous["spent_minor"] * 100, 1)

    return {
        "period": {
            "start": month_start,
            "end": end,
            "previous_start": previous_start,
            "previous_end": previous_end,
            "label": month_start.strftime("%B %Y"),
            "is_current": month_start == current_month,
        },
        "summary": {
            "currency": currency,
            "income_minor": current["income_minor"],
            "spent_minor": current["spent_minor"],
            "net_minor": current["income_minor"] - current["spent_minor"],
            "expense_count": current["expense_count"],
            "previous_spent_minor": previous["spent_minor"],
            "change_minor": change_minor,
            "change_percent": change_percent,
        },
        "categories": _expense_hierarchy(db, user_id, month_start, end, currency),
    }
