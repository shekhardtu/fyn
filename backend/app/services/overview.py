from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from ..domain import TransactionType
from ..event_time import local_date, utc_range_for_local_dates
from ..models import Account, Budget, Category, Subcategory, Transaction
from .finance_time import shift_month
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


def _daily_trend(
    db: Session,
    user_id: UUID,
    start: date,
    end: date,
    previous_start: date,
    previous_end: date,
    currency: str,
) -> list[dict]:
    """Align this month and the comparison month by calendar day.

    Aggregating in Python keeps the grouping timezone-correct on both SQLite
    tests and PostgreSQL without introducing dialect-specific date functions.
    A month is a deliberately small, bounded result set.
    """
    timezone_name = user_timezone(db, user_id)
    range_start, range_end = utc_range_for_local_dates(previous_start, end, timezone_name)
    transactions = db.execute(
        apply_canonical_transaction_scope(
            select(Transaction.transaction_type, Transaction.amount_minor, Transaction.transaction_at),
            user_id,
            currency=currency,
        ).where(
            Transaction.transaction_at >= range_start,
            Transaction.transaction_at < range_end,
            Transaction.transaction_type.in_((TransactionType.INCOME, TransactionType.EXPENSE)),
        )
    )
    current_by_day: dict[int, dict[str, int]] = {}
    previous_by_day: dict[int, dict[str, int]] = {}
    for transaction_type, amount_minor, occurred_at in transactions:
        occurred_on = local_date(occurred_at, timezone_name)
        if start <= occurred_on <= end:
            bucket = current_by_day.setdefault(occurred_on.day, {"income": 0, "spent": 0})
        elif previous_start <= occurred_on <= previous_end:
            # A completed February is compared with all of January. Fold the
            # comparison month's extra calendar days into the last plotted
            # point so the line still lands on the same total as the summary.
            comparison_day = min(occurred_on.day, end.day)
            bucket = previous_by_day.setdefault(comparison_day, {"income": 0, "spent": 0})
        else:
            continue
        key = "income" if transaction_type == TransactionType.INCOME else "spent"
        bucket[key] += int(amount_minor)

    return [
        {
            "day": day,
            "date": start + timedelta(days=day - 1),
            "income_minor": current_by_day.get(day, {}).get("income", 0),
            "spent_minor": current_by_day.get(day, {}).get("spent", 0),
            "previous_income_minor": previous_by_day.get(day, {}).get("income", 0),
            "previous_spent_minor": previous_by_day.get(day, {}).get("spent", 0),
        }
        for day in range(1, end.day + 1)
    ]


def _recent_transactions(
    db: Session,
    user_id: UUID,
    start: date,
    end: date,
    currency: str,
    *,
    limit: int = 7,
) -> list[dict]:
    start_at, end_at = utc_range_for_local_dates(start, end, user_timezone(db, user_id))
    statement = (
        apply_canonical_transaction_scope(
            select(Transaction, Category.name, Account.name)
            .outerjoin(Category, Category.id == Transaction.category_id)
            .outerjoin(Account, and_(Account.id == Transaction.account_id, Account.user_id == user_id)),
            user_id,
            currency=currency,
        )
        .where(Transaction.transaction_at >= start_at, Transaction.transaction_at < end_at)
        .order_by(Transaction.transaction_at.desc(), Transaction.id.desc())
        .limit(limit)
    )
    return [
        {
            "id": transaction.id,
            "transaction_type": transaction.transaction_type,
            "amount_minor": transaction.amount_minor,
            "currency": transaction.currency,
            "merchant": transaction.merchant_name,
            "transaction_at": transaction.transaction_at,
            "category": category_name,
            "account": account_name,
        }
        for transaction, category_name, account_name in db.execute(statement)
    ]


def _linked_accounts(db: Session, user_id: UUID) -> list[dict]:
    accounts = db.scalars(
        select(Account)
        .where(Account.user_id == user_id)
        .order_by(Account.balance_minor.desc(), Account.name)
    )
    return [
        {
            "id": account.id,
            "name": account.name,
            "account_type": account.account_type,
            "institution": account.institution,
            "mask": account.mask,
            "balance_minor": account.balance_minor,
            "currency": account.currency,
        }
        for account in accounts
    ]


def _monthly_budgets(
    db: Session,
    user_id: UUID,
    currency: str,
    spent_minor: int,
    categories: list[dict],
) -> list[dict]:
    """Project the canonical budget records against this overview's spend.

    A category-less monthly budget is the overall spending limit. Categorized
    records are independent limits for that category; they are deliberately
    not summed into a second synthetic overall limit.
    """
    spent_by_category = {item["id"]: item["amount_minor"] for item in categories}
    rows = list(db.execute(
        select(Budget, Category.slug, Category.name)
        .outerjoin(Category, Category.id == Budget.category_id)
        .where(
            Budget.user_id == user_id,
            Budget.currency == currency,
            Budget.period == "monthly",
        )
    ))
    result = []
    for budget, category_slug, category_name in rows:
        scoped_spend = spent_minor if budget.category_id is None else spent_by_category.get(category_slug, 0)
        variance = budget.amount_minor - scoped_spend
        result.append({
            "id": budget.id,
            "name": budget.name,
            "category_id": budget.category_id,
            "category_slug": category_slug,
            "category": category_name,
            "amount_minor": budget.amount_minor,
            "spent_minor": scoped_spend,
            "remaining_minor": max(variance, 0),
            "over_minor": max(-variance, 0),
            "percent_used": round(scoped_spend / budget.amount_minor * 100, 1) if budget.amount_minor else 0,
            "currency": budget.currency,
            "period": budget.period,
        })
    return sorted(
        result,
        key=lambda item: (
            item["category_id"] is not None,
            str(item["category"] or item["name"]).casefold(),
        ),
    )


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

    categories = _expense_hierarchy(db, user_id, month_start, end, currency)

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
        "categories": categories,
        "budgets": _monthly_budgets(db, user_id, currency, current["spent_minor"], categories),
        "trend": _daily_trend(db, user_id, month_start, end, previous_start, previous_end, currency),
        "recent_transactions": _recent_transactions(db, user_id, month_start, end, currency),
        "accounts": _linked_accounts(db, user_id),
    }
