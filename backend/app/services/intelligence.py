from __future__ import annotations

import calendar
import re
from collections.abc import Sequence
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import TypedDict, cast
from uuid import UUID, uuid4

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from ..domain import ACTIVE_STATUS, SpendNature, TransactionType, WidgetActionId
from ..event_time import as_utc, from_local_parts, local_date, now_utc, utc_range_for_local_dates
from ..models import Account, Budget, Category, Goal, Loan, Subcategory, Tag, Transaction, TransactionTag
from ..schemas import DataReference, Widget, WidgetAction, WidgetType
from .chart_widgets import ChartSpecError, build_chart_widget, dataset_id
from .finance_time import month_bounds, shift_month
from .calculators import affordability, loan_strategy_options
from .currency import format_money_minor, user_currency, user_timezone
from .extraction import normalize_merchant
from .manifest import native_manifest_fingerprint
from .markdown_views import join_blocks, markdown_section, markdown_table, money
from .semantic import BINARY_TRANSFORM_OPERATIONS, WINDOW_TRANSFORM_OPERATIONS, AnalysisPlan, AnalysisTransform, execute_finance_query
from .semantic_registry import TIME_GRAIN_SPECS
from .taxonomy import TaxonomyRepository
from .transactions import apply_canonical_transaction_scope, apply_expense_transaction_scope, canonical_transactions, expense_transactions


@dataclass
class IntelligenceResult:
    message: str
    widgets: list[Widget]
    citations: list[DataReference]
    # Raw executed query results, kept so the harness verifies rows against the
    # executor's own output — markdown rendering carries no machine-readable
    # payload the way widgets did.
    query_results: list[dict] = field(default_factory=list)
    # Chart specs that failed a deterministic check: the analysis stands, the
    # chart does not, and the refusal is recorded instead of silently passed.
    chart_notes: list[dict] = field(default_factory=list)


class LoanStrategyBranch(TypedDict, total=False):
    emi_minor: int
    interest_saved_minor: int
    months_saved: int


class LoanStrategyOption(TypedDict):
    prepayment_minor: int
    fee_minor: int
    lower_emi: LoanStrategyBranch
    shorter_tenure: LoanStrategyBranch


class LoanStrategySummary(TypedDict):
    loanId: str
    name: str
    lender: str | None
    principalMinor: int
    currency: str
    annualRatePercent: float
    tenureMonths: int
    options: list[LoanStrategyOption]


def _active_loans(db: Session, user_id: UUID, currency: str) -> list[Loan]:
    return list(db.scalars(
        select(Loan).where(
            Loan.user_id == user_id,
            Loan.currency == currency,
            Loan.status == ACTIVE_STATUS,
            or_(Loan.direction.is_(None), Loan.direction == "borrowed"),
        ).order_by(Loan.outstanding_principal_minor.desc())
    ))


# --- Deterministic domain reads -------------------------------------------------
# These are the canonical implementations of the shared finance aggregates. The
# dedicated analysis services below, the context loader, and the conversational
# grounding tools all read through them; money keys always end in `_minor`.


def expense_summary(db: Session, user_id: UUID, start: date, end: date, category_slug: str | None = None) -> dict:
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


def category_rows(db: Session, user_id: UUID, start: date, end: date) -> list[dict]:
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


def subcategory_rows(db: Session, user_id: UUID, start: date, end: date, category_slug: str) -> list[dict]:
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


def monthly_comparison_data(db: Session, user_id: UUID, today: date) -> dict:
    current_start, current_end = month_bounds(today)
    previous_start = shift_month(current_start, -1)
    previous_end = current_start.fromordinal(current_start.toordinal() - 1)
    current = expense_summary(db, user_id, current_start, min(today, current_end))
    # Compare equal elapsed days to avoid misleading partial-month claims.
    comparable_previous_end = min(previous_end, previous_start.replace(day=min(today.day, monthrange(previous_start.year, previous_start.month)[1])))
    previous = expense_summary(db, user_id, previous_start, comparable_previous_end)
    difference = current["total_minor"] - previous["total_minor"]
    pct = None if previous["total_minor"] == 0 else round(difference / previous["total_minor"] * 100, 1)
    return {"current": current, "previous": previous, "difference_minor": difference, "percent_change": pct}


def cash_totals(db: Session, user_id: UUID, start: date | None = None, end: date | None = None) -> dict:
    currency = user_currency(db, user_id)
    statement = apply_canonical_transaction_scope(
        select(
            func.coalesce(func.sum(case((Transaction.transaction_type == TransactionType.INCOME, Transaction.amount_minor), else_=0)), 0),
            func.coalesce(func.sum(case((Transaction.transaction_type == TransactionType.EXPENSE, Transaction.amount_minor), else_=0)), 0),
        ),
        user_id,
        currency=currency,
    )
    if start and end:
        start_at, end_at = utc_range_for_local_dates(start, end, user_timezone(db, user_id))
        statement = statement.where(
            Transaction.transaction_at >= start_at,
            Transaction.transaction_at < end_at,
        )
    income, expenses = db.execute(statement).one()
    return {
        "income_minor": int(income),
        "expenses_minor": int(expenses),
        "net_minor": int(income) - int(expenses),
        "currency": currency,
        "income_to_expense_ratio": round(int(income) / int(expenses), 2) if int(expenses) else None,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
    }


def transaction_rows(
    db: Session,
    user_id: UUID,
    *,
    transaction_type: str | None = None,
    merchant: str | None = None,
    category_slug: str | None = None,
    subcategory_slug: str | None = None,
    account: str | None = None,
    tag: str | None = None,
    min_amount_minor: int | None = None,
    max_amount_minor: int | None = None,
    start: date | None = None,
    end: date | None = None,
    sort_by: str = "transaction_at",
    sort_direction: str = "desc",
    limit: int = 50,
) -> dict:
    """Read one bounded, tenant-scoped page of canonical transactions.

    The total is summed over every matching record, not over the returned
    page, so a capped list can still state a correct period total. Presentation
    is not decided here: the caller receives rows and writes its own prose.
    """
    currency = user_currency(db, user_id)
    timezone_name = user_timezone(db, user_id)
    stmt = canonical_transactions(user_id, currency=currency)
    if transaction_type:
        stmt = stmt.where(Transaction.transaction_type == transaction_type)
    if merchant:
        stmt = stmt.where(Transaction.merchant_name.ilike(f"%{merchant.strip()}%"))
    if category_slug:
        stmt = stmt.join(Category, Category.id == Transaction.category_id).where(
            Category.slug == category_slug
        )
    if subcategory_slug:
        stmt = stmt.join(Subcategory, Subcategory.id == Transaction.subcategory_id).where(
            Subcategory.slug == subcategory_slug
        )
    if account:
        stmt = stmt.join(Account, Account.id == Transaction.account_id).where(
            Account.name.ilike(f"%{account.strip()}%")
        )
    if tag:
        stmt = (
            stmt.join(TransactionTag, TransactionTag.transaction_id == Transaction.id)
            .join(Tag, Tag.id == TransactionTag.tag_id)
            .where(Tag.normalized_name == tag.casefold().strip())
        )
    if min_amount_minor is not None:
        stmt = stmt.where(Transaction.amount_minor >= min_amount_minor)
    if max_amount_minor is not None:
        stmt = stmt.where(Transaction.amount_minor <= max_amount_minor)
    if start:
        stmt = stmt.where(Transaction.transaction_at >= from_local_parts(start, None, timezone_name))
    if end:
        _, end_at = utc_range_for_local_dates(end, end, timezone_name)
        stmt = stmt.where(Transaction.transaction_at < end_at)

    scoped_ids = stmt.with_only_columns(Transaction.id).order_by(None).subquery()
    total_minor, matched = db.execute(
        select(func.coalesce(func.sum(Transaction.amount_minor), 0), func.count(Transaction.id))
        .join(scoped_ids, scoped_ids.c.id == Transaction.id)
    ).one()

    ascending = sort_direction == "asc"
    primary = Transaction.amount_minor if sort_by == "amount" else Transaction.transaction_at
    stmt = stmt.order_by(
        primary.asc() if ascending else primary.desc(),
        Transaction.created_at.desc(),
    )
    records = list(db.scalars(stmt.limit(limit)))

    taxonomy = TaxonomyRepository(db, user_id)
    categories = {
        item_id: item.name
        for item_id, item in taxonomy.categories_by_id(
            {item.category_id for item in records if item.category_id}
        ).items()
    }
    subcategories = {
        item_id: item.name
        for item_id, item in taxonomy.subcategories_by_id(
            {item.subcategory_id for item in records if item.subcategory_id}
        ).items()
    }
    accounts = {
        item.id: item.name
        for item in db.scalars(
            select(Account).where(
                Account.user_id == user_id,
                Account.id.in_({item.account_id for item in records if item.account_id}),
            )
        )
    }
    tags_by_transaction: dict[UUID, list[str]] = {}
    if records:
        for transaction_id, tag_name in db.execute(
            select(TransactionTag.transaction_id, Tag.name)
            .join(Tag, Tag.id == TransactionTag.tag_id)
            .where(TransactionTag.transaction_id.in_([item.id for item in records]))
        ):
            tags_by_transaction.setdefault(transaction_id, []).append(tag_name)

    rows = [{
        "id": str(item.id),
        # A missing merchant stays a missing merchant.  Transaction type is a
        # separate dimension and must never be presented as a merchant name.
        "merchant": item.merchant_name or "Unknown merchant",
        "transaction_type": item.transaction_type,
        "category": categories.get(item.category_id) if item.category_id else None,
        "subcategory": subcategories.get(item.subcategory_id) if item.subcategory_id else None,
        "account": accounts.get(item.account_id) if item.account_id else None,
        "tags": tags_by_transaction.get(item.id, []),
        "transaction_at": as_utc(item.transaction_at).isoformat(),
        "transaction_date": local_date(item.transaction_at, timezone_name).isoformat(),
        "status": item.status,
        "amount_minor": item.amount_minor,
        # The display string is produced here so a reply can quote the user's
        # own currency formatting instead of re-deriving it from minor units.
        "amount": format_money_minor(item.amount_minor, item.currency),
        "currency": item.currency,
    } for item in records]

    return {
        "rows": rows,
        "returned": len(rows),
        "total_minor": int(total_minor),
        "total": format_money_minor(int(total_minor), currency),
        "currency": currency,
        "truncated": int(matched) > len(rows),
    }


def recurring_rows(db: Session, user_id: UUID) -> list[dict]:
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


def _apply_transform(
    results: list[dict],
    completed_transforms: list[dict],
    transform: AnalysisTransform,
) -> dict:
    source = next(result for result in results if result["name"] == transform.query_name)
    primary_total = sum(int(row["value"]) for row in source["rows"])
    if transform.operation == "prorate":
        target_start = transform.target_start_date
        target_end = transform.target_end_date
        if target_start is None or target_end is None:
            raise ValueError("prorate requires a target date range")
        source_start = date.fromisoformat(source["start"])
        source_end = date.fromisoformat(source["end"])
        source_days = (source_end - source_start).days + 1
        target_days = (target_end - target_start).days + 1
        value = int(
            (Decimal(primary_total) * Decimal(target_days) / Decimal(source_days)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        return {
            "name": transform.name,
            "operation": transform.operation,
            "queryName": transform.query_name,
            "metric": source["metric"],
            "sourceValue": primary_total,
            "sourceStartDate": source_start.isoformat(),
            "sourceEndDate": source_end.isoformat(),
            "sourceDays": source_days,
            "targetStartDate": target_start.isoformat(),
            "targetEndDate": target_end.isoformat(),
            "targetDays": target_days,
            "value": value,
            "values": [{"label": transform.name, "value": value}],
        }
    if transform.operation in BINARY_TRANSFORM_OPERATIONS:
        if transform.secondary_transform_name:
            secondary = next(
                item
                for item in completed_transforms
                if item["name"] == transform.secondary_transform_name
            )
            secondary_total = int(secondary["value"])
            secondary_name = transform.secondary_transform_name
        else:
            if transform.secondary_query_name is None:
                raise ValueError(f"{transform.operation} requires a secondary query")
            secondary = next(
                result for result in results if result["name"] == transform.secondary_query_name
            )
            secondary_total = sum(int(row["value"]) for row in secondary["rows"])
            secondary_name = transform.secondary_query_name
        return {
            "name": transform.name,
            "operation": transform.operation,
            "queryName": transform.query_name,
            "secondaryQueryName": transform.secondary_query_name,
            "secondaryTransformName": transform.secondary_transform_name,
            "metric": source["metric"],
            "primaryValue": primary_total,
            "secondaryValue": secondary_total,
            "value": primary_total - secondary_total if transform.operation == "difference" else None,
            "ratioBasisPoints": round(primary_total * 10_000 / secondary_total) if transform.operation == "ratio" and secondary_total else None,
            "values": [
                {"label": transform.query_name, "value": primary_total},
                {"label": secondary_name, "value": secondary_total},
            ],
        }
    if transform.operation == "change_drivers":
        by_period: dict[str, dict[str, int]] = {}
        for row in source["rows"]:
            period = str(row.get(transform.period_dimension, "Unknown"))
            driver = str(row.get(transform.dimension, "Unknown"))
            period_values = by_period.setdefault(period, {})
            period_values[driver] = period_values.get(driver, 0) + int(row["value"])
        periods = sorted(by_period)
        output = {
            "name": transform.name,
            "operation": transform.operation,
            "queryName": transform.query_name,
            "dimension": transform.dimension,
            "periodDimension": transform.period_dimension,
            "metric": source["metric"],
            "periods": periods,
            "values": [],
        }
        if len(periods) >= 2:
            first_period, last_period = periods[0], periods[-1]
            drivers = set(by_period[first_period]) | set(by_period[last_period])
            changes = [
                {"label": driver, "fromValue": by_period[first_period].get(driver, 0), "toValue": by_period[last_period].get(driver, 0), "value": by_period[last_period].get(driver, 0) - by_period[first_period].get(driver, 0)}
                for driver in drivers
            ]
            changes.sort(
                key=lambda item: (
                    cast(int, item["value"]),
                    abs(cast(int, item["value"])),
                ),
                reverse=True,
            )
            output["values"] = changes[:transform.limit]
            output["from"] = first_period
            output["to"] = last_period
        return output
    grouped: dict[str, int] = {}
    display: dict[str, str] = {}
    for row in source["rows"]:
        key = str(row.get(transform.dimension, "Unknown"))
        grouped[key] = grouped.get(key, 0) + int(row["value"])
        display.setdefault(key, _readable_key(key, row))
    # A grouping key is a join key, not a caption. An identifier one names a
    # record the reader has never seen, so the row's own descriptive columns
    # stand in for it and a bare id never reaches prose.
    if any(label != key for key, label in display.items()):
        grouped = {display[key]: value for key, value in grouped.items()}
    ranked = sorted(grouped.items(), key=lambda item: item[1], reverse=True)
    output = {
        "name": transform.name,
        "operation": transform.operation,
        "queryName": transform.query_name,
        "dimension": transform.dimension,
        "metric": source["metric"],
    }
    if transform.operation in WINDOW_TRANSFORM_OPERATIONS:
        chronological = sorted(grouped.items())
        transform_values = []
        running = 0
        for index, (label, value) in enumerate(chronological):
            if transform.operation == "cumulative_sum":
                running += value
                rendered = running
            else:
                window_values = [item[1] for item in chronological[max(0, index - transform.window + 1):index + 1]]
                rendered = round(sum(window_values) / len(window_values))
            transform_values.append({"label": label, "value": rendered, "raw_value": value})
        output["values"] = transform_values
        output["window"] = transform.window if transform.operation == "moving_average" else None
    elif transform.operation == "compare_totals":
        selected = ranked[:transform.limit]
        output["values"] = [{"label": label, "value": value} for label, value in selected]
        if len(selected) >= 2:
            output["leader"] = selected[0][0]
            output["difference"] = selected[0][1] - selected[1][1]
    elif transform.operation == "rank":
        output["values"] = [{"label": label, "value": value, "rank": index + 1} for index, (label, value) in enumerate(ranked[:transform.limit])]
    elif transform.operation == "share_of_total":
        total = sum(grouped.values())
        output["values"] = [
            {"label": label, "value": value, "basis_points": round(value * 10_000 / total) if total else 0}
            for label, value in ranked[:transform.limit]
        ]
        output["total"] = total
    else:
        chronological = sorted(grouped.items())
        output["values"] = [{"label": label, "value": value} for label, value in chronological]
        if len(chronological) >= 2:
            first_entry, last_entry = chronological[0], chronological[-1]
            difference = last_entry[1] - first_entry[1]
            output.update({
                "from": first_entry[0],
                "to": last_entry[0],
                "difference": difference,
                "changeBasisPoints": round(difference * 10_000 / first_entry[1]) if first_entry[1] else None,
            })
    return output


_MONTH_TOKENS = {name.lower() for name in (*calendar.month_name, *calendar.month_abbr) if name}
_LABEL_EXPANSIONS = {"mtd": "month-to-date", "ytd": "year-to-date"}


_UUID_KEY = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# The columns that describe a record, in the order a person would name it by.
_DESCRIPTIVE_COLUMNS = ("merchant", "category", "subcategory", "account", "transaction_date", "time_bucket")


def _readable_key(key: str, row: dict) -> str:
    """A caption for one grouped row: never a raw identifier."""
    if not _UUID_KEY.match(key):
        return key
    parts = [str(row[name]) for name in _DESCRIPTIVE_COLUMNS if row.get(name)]
    return " · ".join(parts) if parts else f"Record {key[:8]}"


def _humanize_label(name: str, *, sentence_start: bool = True) -> str:
    """Render a plan-authored identifier as prose without inventing words.

    Underscores become spaces, month tokens regain their capital, and the
    to-date abbreviations are expanded; every other word is preserved
    exactly, because query and transform names may contain user vocabulary.
    """
    words: list[str] = []
    for word in name.replace("_", " ").split():
        lowered = word.lower()
        if lowered in _LABEL_EXPANSIONS:
            words.append(_LABEL_EXPANSIONS[lowered])
        elif lowered in _MONTH_TOKENS:
            words.append(lowered.capitalize())
        else:
            words.append(word)
    if not words:
        return name
    text = " ".join(words)
    return text[0].upper() + text[1:] if sentence_start else text


def _format_period(start: date, end: date) -> str:
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {end.strftime('%b %Y')}"
    return f"{start.strftime('%-d %b %Y')}–{end.strftime('%-d %b %Y')}"


def _render_prorate(transform: dict, render) -> str:
    period = _format_period(
        date.fromisoformat(transform["sourceStartDate"]),
        date.fromisoformat(transform["sourceEndDate"]),
    )
    return (
        f"{_humanize_label(transform['name'])}: {render(transform['value'])} — "
        f"{render(transform['sourceValue'])} recorded over {period} "
        f"({transform['sourceDays']} days), projected across all {transform['targetDays']} days "
        "at the same daily pace."
    )


def _render_difference(transform: dict, results: list[dict], transforms: list[dict], render) -> str:
    label = _humanize_label(transform["name"])
    primary_label = _humanize_label(transform["queryName"], sentence_start=False)
    secondary_name = transform.get("secondaryTransformName") or transform.get("secondaryQueryName") or ""
    projection = next(
        (
            item for item in transforms
            if item["name"] == secondary_name and item["operation"] == "prorate"
        ),
        None,
    )
    if projection:
        # The one difference shape that needs its reasoning spelled out: an
        # actual measured against a projection. Every figure and date below
        # comes from the executed transform records, never from arithmetic
        # done here.
        source_period = _format_period(
            date.fromisoformat(projection["sourceStartDate"]),
            date.fromisoformat(projection["sourceEndDate"]),
        )
        primary_result = next(
            (result for result in results if result["name"] == transform["queryName"]),
            None,
        )
        primary_clause = f"{render(transform['primaryValue'])} of {primary_label}"
        if primary_result:
            primary_period = _format_period(
                date.fromisoformat(primary_result["start"]),
                date.fromisoformat(primary_result["end"]),
            )
            primary_clause += f" for {primary_period}"
        return (
            f"{label}: {render(transform['value'])}. "
            f"Over {source_period} ({projection['sourceDays']} days), "
            f"{_humanize_label(projection['queryName'], sentence_start=False)} came to "
            f"{render(projection['sourceValue'])} — a pace that projects to "
            f"{render(projection['value'])} across all {projection['targetDays']} days. "
            f"Set against {primary_clause} (actual, not projected), "
            f"that leaves {render(transform['value'])}."
        )
    secondary_label = (
        _humanize_label(secondary_name, sentence_start=False)
        if secondary_name
        else "the comparison value"
    )
    return (
        f"{label}: {render(transform['value'])} — {primary_label} "
        f"({render(transform['primaryValue'])}) minus {secondary_label} "
        f"({render(transform['secondaryValue'])})."
    )


def _presentation_labels(results: list[dict], transforms: list[dict]) -> tuple[list[dict], list[dict]]:
    """Attach display labels beside contract names for the widget payload.

    Names stay the machine keys — template references and verification match
    on them — so the human rendering travels as a separate ``displayLabel``.
    A transform value's label is humanized only when it references a query or
    transform by name; dimension values such as dates and category names pass
    through untouched for the client's own formatters.
    """
    known = {result["name"] for result in results} | {transform["name"] for transform in transforms}
    labeled_results = [
        {**result, "displayLabel": _humanize_label(result["name"])} for result in results
    ]
    labeled_transforms = [
        {
            **transform,
            "displayLabel": _humanize_label(transform["name"]),
            "values": [
                {**value, "displayLabel": _humanize_label(str(value["label"]))}
                if str(value.get("label")) in known
                else value
                for value in transform.get("values", [])
            ],
        }
        for transform in transforms
    ]
    return labeled_results, labeled_transforms


_MARKDOWN_ROW_CAP = 50


def _is_count_metric(metric: str) -> bool:
    return "count" in metric


def _capped_table(headers: list[str], rows: list[list], total: int) -> str:
    table = markdown_table(headers, rows[:_MARKDOWN_ROW_CAP])
    if total > _MARKDOWN_ROW_CAP:
        return join_blocks(table, f"_Showing {_MARKDOWN_ROW_CAP} of {total} rows._")
    return table


def _result_markdown(result: dict) -> str:
    """One executed query result as a markdown table; scalars render inline
    through the message instead."""
    rows = result.get("rows") or []
    columns = list(result.get("dimensions") or [])
    if result.get("time_grouping"):
        columns.append("time_bucket")
    if result.get("time_pivot"):
        columns.extend(("time_bucket", "time_segment"))
    if not rows or not columns:
        return ""
    currency = result.get("currency")
    is_count = _is_count_metric(result.get("metric", ""))
    headers = [*(_humanize_label(column) for column in columns), "Value"]
    body = [
        [
            *(row.get(column, "—") for column in columns),
            str(row.get("value", 0)) if is_count else money(row.get("value", 0), currency),
        ]
        for row in rows
    ]
    title = result.get("displayLabel") or _humanize_label(result.get("name", "Result"))
    return markdown_section(title, _capped_table(headers, body, len(rows)))


def _transform_markdown(transform: dict, currency: str | None) -> str:
    values = transform.get("values") or []
    if not values:
        return ""
    is_count = _is_count_metric(transform.get("metric", ""))
    extra_columns = []
    if any("basis_points" in value for value in values):
        extra_columns.append(("Share", lambda value: f"{Decimal(value['basis_points']) / Decimal(100)}%" if value.get("basis_points") is not None else "—"))
    if any("rank" in value for value in values):
        extra_columns.append(("Rank", lambda value: value.get("rank", "—")))
    headers = ["", "Value", *(name for name, _render in extra_columns)]
    body = [
        [
            value.get("displayLabel") or value.get("label", "—"),
            str(value.get("value", 0)) if is_count else money(value.get("value", 0), currency),
            *(render(value) for _name, render in extra_columns),
        ]
        for value in values
    ]
    title = transform.get("displayLabel") or _humanize_label(transform.get("name", "Result"))
    return markdown_section(title, _capped_table(headers, body, len(values)))


def _context_markdown(context: dict, currency: str) -> str:
    blocks: list[str] = []
    if context.get("budgets"):
        blocks.append(markdown_section("Budgets", markdown_table(
            ["Budget", "Category", "Limit", "Spent", "Remaining"],
            [[row["name"], row.get("category") or "Overall", money(row["limitMinor"], row["currency"]), money(row["spentMinor"], row["currency"]), money(row["remainingMinor"], row["currency"])] for row in context["budgets"]],
        )))
    if context.get("goals"):
        blocks.append(markdown_section("Goals", markdown_table(
            ["Goal", "Target", "Saved", "Remaining", "Target date"],
            [[row["name"], money(row["targetMinor"], row["currency"]), money(row["currentMinor"], row["currency"]), money(row["remainingMinor"], row["currency"]), row.get("targetDate") or "—"] for row in context["goals"]],
        )))
    if context.get("loans"):
        blocks.append(markdown_section("Loans", markdown_table(
            ["Loan", "Lender", "Outstanding", "Rate", "EMI", "Months left"],
            [[row["name"], row.get("lender") or "—", money(row["principalMinor"], row["currency"]), f"{row['annualRatePercent']}%", money(row.get("emiMinor"), row["currency"]), row.get("remainingTenureMonths", "—")] for row in context["loans"]],
        )))
    if context.get("accounts"):
        blocks.append(markdown_section("Accounts", markdown_table(
            ["Account", "Type", "Balance"],
            [[row["name"], row.get("type") or "—", money(row["balanceMinor"], row["currency"])] for row in context["accounts"]],
        )))
    if context.get("recurring_expenses"):
        blocks.append(_recurring_markdown(context["recurring_expenses"]))
    return join_blocks(*blocks)


def _recurring_markdown(recurring: list[dict]) -> str:
    if not recurring:
        return ""
    return markdown_section("Recurring expenses", markdown_table(
        ["Merchant", "Cadence", "Occurrences", "Last seen", "Typical amount"],
        [[row["merchant"], row["cadence"], row["occurrences"], row["last_date"], money(row["amount_minor"], row["currency"])] for row in recurring],
    ))


def _semantic_message(results: list[dict], transforms: list[dict]) -> str:
    """Render only values returned by the governed executor—no invented facts."""
    if not results:
        return "The analysis plan did not contain a query."
    currency = next((result.get("currency") for result in results if result.get("currency")), None)

    def render_money(value: int) -> str:
        if not currency:
            raise ValueError("A money analysis result must declare its currency")
        return format_money_minor(value, currency)

    if transforms:
        final_transform = transforms[-1]
        if final_transform["operation"] in ("difference", "prorate"):
            render = (
                (lambda value: str(value))
                if final_transform.get("metric") == "transaction_count"
                else render_money
            )
            if final_transform["operation"] == "difference":
                return _render_difference(final_transform, results, transforms, render)
            return _render_prorate(final_transform, render)

    if len(results) > 1:
        metric_labels = {
            "income": "recorded income",
            "gross_spend": "recorded spending",
            "net_spend": "net spending",
            "net_cash_flow": "net cash flow",
            "transaction_amount": "transaction activity",
            "transaction_count": "transaction count",
        }
        clauses = []
        spans: list[tuple[str, str]] = []
        seen_metrics: set[str] = set()
        for result in results:
            label = metric_labels.get(result["metric"])
            if not label or result["metric"] in seen_metrics:
                continue
            seen_metrics.add(result["metric"])
            total = sum(int(row.get("value", 0)) for row in result["rows"])
            rendered = str(total) if result["metric"] == "transaction_count" else render_money(total)
            clauses.append(f"{label} is {rendered}")
            spans.append((result["start"], result["end"]))
        if len(clauses) >= 2:
            # A comparison plan runs several queries over deliberately different
            # windows. Reading the period off one of them and printing it once
            # in front of every figure would date the other figures to a period
            # they were never computed over — a false statement about recorded
            # money. One prefix is used only when every contributing query
            # really shares the window; otherwise each figure carries its own.
            shared = spans[0] if len(set(spans)) == 1 else None
            if shared is None:
                clauses = [
                    f"{clause} for {_format_period(date.fromisoformat(start), date.fromisoformat(end))}"
                    for clause, (start, end) in zip(clauses, spans)
                ]
            if len(clauses) == 2:
                summary = " and ".join(clauses)
            else:
                summary = f"{', '.join(clauses[:-1])}, and {clauses[-1]}"
            share = next((item for item in transforms if item.get("operation") == "share_of_total" and item.get("values")), None)
            if share:
                leader = share["values"][0]
                percentage = Decimal(leader["basis_points"]) / Decimal(100)
                summary += f". {leader['label']} is the largest recorded share at {percentage}% ({render_money(leader['value'])})"
            if shared is None:
                return f"{summary[0].upper()}{summary[1:]}."
            period = _format_period(
                date.fromisoformat(shared[0]),
                date.fromisoformat(shared[1]),
            )
            return f"For {period}, {summary}."
    if transforms:
        transform = transforms[-1]
        values = transform.get("values", [])
        is_count = transform.get("metric") == "transaction_count"
        render = (lambda value: str(value)) if is_count else render_money
        if transform["operation"] == "compare_totals" and len(values) >= 2:
            leader = values[0]
            runner_up = values[1]
            return f"{leader['label']} is larger at {render(leader['value'])}, compared with {render(runner_up['value'])} for {runner_up['label']}; the difference is {render(transform['difference'])}."
        if transform["operation"] == "rank" and values:
            return f"The largest result is {values[0]['label']} at {render(values[0]['value'])}."
        if transform["operation"] == "share_of_total" and values:
            share_percentage = Decimal(values[0]["basis_points"]) / Decimal(100)
            return f"{values[0]['label']} is the largest share at {share_percentage}% ({render(values[0]['value'])})."
        if transform["operation"] == "period_change" and len(values) >= 2:
            direction = "increased" if transform["difference"] >= 0 else "decreased"
            return f"The result {direction} by {render(abs(transform['difference']))} from {transform['from']} to {transform['to']}."
        if transform["operation"] == "change_drivers" and values:
            driver = values[0]
            direction = "increase" if driver["value"] >= 0 else "decrease"
            return f"{driver['label']} is the largest recorded {direction}, changing by {render(abs(driver['value']))} from {transform['from']} to {transform['to']}."
        if transform["operation"] == "ratio" and transform.get("ratioBasisPoints") is not None:
            return f"The first measure is {Decimal(transform['ratioBasisPoints']) / Decimal(100)}% of the comparison measure."
        if transform["operation"] in WINDOW_TRANSFORM_OPERATIONS and values:
            label = "cumulative total" if transform["operation"] == "cumulative_sum" else f"{transform['window']}-period moving average"
            return f"The latest {label} is {render(values[-1]['value'])}."
    if len(results) > 1:
        # One metric measured as a scalar plus grouped context (the summary
        # template shape): quote the scalar so the answer leads with the total.
        metrics = {result["metric"] for result in results}
        scalar = next(
            (result for result in results if not result["dimensions"] and not result.get("time_grouping") and result["rows"]),
            None,
        )
        if len(metrics) == 1 and scalar:
            value = int(scalar["rows"][0]["value"])
            rendered = str(value) if scalar["metric"] == "transaction_count" else render_money(value)
            period = _format_period(
                date.fromisoformat(scalar["start"]),
                date.fromisoformat(scalar["end"]),
            )
            return f"{_humanize_label(scalar['name'])}: {rendered} for {period}."
        nonempty = sum(bool(result["rows"]) for result in results)
        return f"I ran {len(results)} validated analyses; {nonempty} returned recorded financial data."
    result = results[0]
    rows = result["rows"]
    if not rows:
        if result.get("requires_transaction_time"):
            grain = (result.get("time_grouping") or {}).get("grain", "sub-day")
            return (
                f"I can’t draw the requested {grain} analysis because none of the matching "
                "transactions has a recorded transaction time. I can group the same records by day instead."
            )
        return f"I found no recorded data for {result['name'].lower()} in that period."
    metric = result["metric"]
    grouping = result.get("time_grouping") or {}
    if grouping:
        grain = grouping.get("grain", "time")
        cadence = TIME_GRAIN_SPECS[grain].cadence if grain in TIME_GRAIN_SPECS else str(grain)
        if metric == "transaction_amount":
            return (
                f"I plotted {len(rows)} {cadence} transaction-amount series point"
                f"{'s' if len(rows) != 1 else ''} for {result['name'].lower()}, separated by transaction type. "
                "These are absolute amounts within each type, not net cash flow."
            )
        if metric == "transaction_count":
            count = sum(int(row.get("value", 0)) for row in rows)
            return f"I plotted {count} recorded transaction{'s' if count != 1 else ''} across {len(rows)} {cadence} bucket{'s' if len(rows) != 1 else ''}."
    if not result["dimensions"]:
        value = rows[0]["value"]
        rendered = str(value) if metric == "transaction_count" else render_money(value)
        return f"{_humanize_label(result['name'])}: {rendered}."
    first = rows[0]
    labels = [str(first.get(dimension, "Unknown")) for dimension in result["dimensions"]]
    rendered = str(first["value"]) if metric == "transaction_count" else render_money(first["value"])
    readable_name = result["name"].replace("_", " ").lower()
    if len(rows) == 1:
        return f"I found one grouped result for {readable_name}: {' · '.join(labels)} — {rendered}."
    return f"I found {len(rows)} grouped results for {readable_name}; the grounded values are shown below."


def _load_context(db: Session, user_id: UUID, currency: str, today: date, sources: Sequence[str]) -> tuple[dict, list[DataReference]]:
    context: dict[str, list[dict]] = {}
    citations: list[DataReference] = []
    start, end = month_bounds(today)
    taxonomy = TaxonomyRepository(db, user_id)
    if "budgets" in sources:
        rows = []
        budgets = list(db.scalars(select(Budget).where(Budget.user_id == user_id, Budget.currency == currency).order_by(Budget.name)))
        for budget in budgets:
            category = taxonomy.category(budget.category_id)
            spent = expense_summary(db, user_id, start, min(today, end), category.slug if category else None)["total_minor"]
            rows.append({"id": str(budget.id), "name": budget.name, "category": category.name if category else None, "limitMinor": budget.amount_minor, "spentMinor": spent, "remainingMinor": budget.amount_minor - spent, "currency": budget.currency})
        context["budgets"] = rows
        citations.append(DataReference(label="Current budgets and month-to-date utilization", entity_type="budget", entity_ids=[row["id"] for row in rows]))
    if "goals" in sources:
        goals = list(db.scalars(select(Goal).where(Goal.user_id == user_id, Goal.currency == currency).order_by(Goal.target_date.nullslast(), Goal.name)))
        context["goals"] = [{"id": str(goal.id), "name": goal.name, "targetMinor": goal.target_minor, "currentMinor": goal.current_minor, "remainingMinor": max(0, goal.target_minor - goal.current_minor), "targetDate": goal.target_date.isoformat() if goal.target_date else None, "currency": goal.currency} for goal in goals]
        citations.append(DataReference(label="Saved financial goals", entity_type="goal", entity_ids=[str(goal.id) for goal in goals]))
    if "loans" in sources:
        loans = _active_loans(db, user_id, currency)
        context["loans"] = [{"id": str(loan.id), "name": loan.name, "lender": loan.lender, "principalMinor": loan.outstanding_principal_minor, "annualRatePercent": float(loan.annual_rate_percent), "remainingTenureMonths": loan.remaining_tenure_months, "emiMinor": loan.current_emi_minor, "prepaymentFeePercent": float(loan.prepayment_fee_percent), "currency": loan.currency} for loan in loans]
        citations.append(DataReference(label="Stored active loan terms", entity_type="loan", entity_ids=[str(loan.id) for loan in loans]))
    if "accounts" in sources:
        accounts = list(db.scalars(select(Account).where(Account.user_id == user_id, Account.currency == currency).order_by(Account.name)))
        context["accounts"] = [{"id": str(account.id), "name": account.name, "type": account.account_type, "balanceMinor": account.balance_minor, "currency": account.currency} for account in accounts]
        citations.append(DataReference(label="Saved account balances", entity_type="account", entity_ids=[str(account.id) for account in accounts]))
    if "recurring_expenses" in sources:
        recurring = recurring_rows(db, user_id)
        context["recurring_expenses"] = recurring
        citations.append(DataReference(label="Detected recurring expense patterns", entity_type="transaction", entity_ids=[item["id"] for item in recurring]))
    return context, citations


def _month_periods(today: date, count: int = 3) -> list[tuple[date, date]]:
    current_start, current_end = month_bounds(today)
    periods = []
    for offset in reversed(range(count)):
        start = shift_month(current_start, -offset)
        _, end = month_bounds(start)
        periods.append((start, min(today, end) if offset == 0 else end))
    return periods


def three_month_allocation(db: Session, user_id: UUID, currency: str, today: date) -> IntelligenceResult:
    periods = _month_periods(today)
    series: dict[str, dict] = {}
    for start, end in periods:
        month = start.strftime("%b %Y")
        for row in category_rows(db, user_id, start, end):
            item = series.setdefault(row["id"], {"id": row["id"], "label": row["label"], "months": {}})
            item["months"][month] = row["amount_minor"]
    categories = sorted(series.values(), key=lambda item: sum(item["months"].values()), reverse=True)
    budgets = list(db.scalars(select(Budget).where(Budget.user_id == user_id, Budget.currency == currency)))
    budget_room = []
    current_month = periods[-1][0].strftime("%b %Y")
    taxonomy = TaxonomyRepository(db, user_id)
    for budget in budgets:
        category = taxonomy.category(budget.category_id)
        spent = series.get(category.slug, {}).get("months", {}).get(current_month, 0) if category else sum(item["months"].get(current_month, 0) for item in categories)
        if spent < budget.amount_minor:
            budget_room.append({"label": category.name if category else budget.name, "room_minor": budget.amount_minor - spent})
    if budget_room:
        room = ", ".join(
            f"{item['label']} ({format_money_minor(item['room_minor'], currency)} below its limit)"
            for item in budget_room[:3]
        )
        message = (
            "I reviewed your recorded expenses across the last three months. "
            f"Your current budgets still have room in {room}. That is available budget, not evidence that you should spend it; "
            "redirecting some of that room toward a saved goal would be the clearest savings lever."
        )
    else:
        if categories:
            leaders = ", ".join(
                f"{item['label']} ({format_money_minor(sum(item['months'].values()), currency)})"
                for item in categories[:3]
            )
            message = (
                "I reviewed your recorded expenses across the last three months. "
                f"The largest category totals were {leaders}. These totals show where money went, but without a saved budget or goal they do not prove which spending is unnecessary. "
                "Start with the largest flexible category, set a realistic cap, and move the difference to a named savings goal."
            )
        else:
            message = (
                "I found no recorded expenses in the last three months, so there is not enough evidence to identify a savings pattern yet. "
                "Once expenses are recorded, I can compare categories against budgets and goals."
            )
    month_labels = [start.strftime("%b %Y") for start, _ in periods]
    allocation_table = markdown_section(
        "Three-month spending allocation",
        markdown_table(
            ["Category", *month_labels],
            [
                [item["label"], *(money(item["months"].get(label), currency) if item["months"].get(label) is not None else "—" for label in month_labels)]
                for item in categories
            ],
        ),
    )
    room_lines = "\n".join(
        f"- {item['label']}: {money(item['room_minor'], currency)} below its limit"
        for item in budget_room
    )
    citations = [DataReference(label="Canonical expenses across three months", entity_type="transaction", query={"periods": [[start.isoformat(), end.isoformat()] for start, end in periods]})]
    return IntelligenceResult(join_blocks(message, allocation_table, room_lines), [], citations)


def avoidable_expense_candidates(db: Session, user_id: UUID, currency: str, today: date) -> IntelligenceResult:
    start = shift_month(today.replace(day=1), -2)
    start_at, end_at = utc_range_for_local_dates(start, today, user_timezone(db, user_id))
    transactions = list(db.scalars(expense_transactions(user_id, currency=currency).where(
        Transaction.transaction_at >= start_at,
        Transaction.transaction_at < end_at,
    ).order_by(Transaction.amount_minor.desc()).limit(500)))
    merchant_counts: dict[str, int] = {}
    for transaction in transactions:
        if transaction.merchant_name:
            merchant_counts[transaction.merchant_name.casefold()] = merchant_counts.get(transaction.merchant_name.casefold(), 0) + 1
    candidates = []
    taxonomy = TaxonomyRepository(db, user_id)
    fee_tokens = ("late fee", "penalty", "overdraft", "convenience fee", "interest charge")
    for transaction in transactions:
        text = f"{transaction.merchant_name or ''} {transaction.description or ''}".casefold()
        reasons = []
        score = Decimal("0")
        if transaction.spend_nature == SpendNature.POTENTIALLY_AVOIDABLE:
            reasons.append("Previously marked potentially avoidable")
            score += Decimal("0.8")
        if any(token in text for token in fee_tokens):
            reasons.append("Fee or penalty rather than a purchased service")
            score += Decimal("0.75")
        if transaction.spend_nature == SpendNature.DISCRETIONARY and transaction.amount_minor >= 100_000:
            reasons.append("Large transaction marked discretionary")
            score += Decimal("0.45")
        if transaction.merchant_name and merchant_counts.get(transaction.merchant_name.casefold(), 0) >= 3:
            reasons.append("Repeated merchant in the last three months")
            score += Decimal("0.2")
        if not reasons:
            continue
        category, subcategory = taxonomy.path(
            transaction.category_id,
            transaction.subcategory_id,
        )
        candidates.append({
            "id": str(transaction.id),
            "merchant": transaction.merchant_name or "Expense",
            "amountMinor": transaction.amount_minor,
            "currency": transaction.currency,
            "transactionAt": as_utc(transaction.transaction_at).isoformat(),
            "category": category.name if category else None,
            "subcategory": subcategory.name if subcategory else None,
            "spendNature": transaction.spend_nature,
            "reasons": reasons,
            "confidence": float(min(score, Decimal("0.99"))),
        })
    candidates.sort(key=lambda item: (item["confidence"], item["amountMinor"]), reverse=True)
    candidates = candidates[:20]
    potential = sum(cast(int, item["amountMinor"]) for item in candidates)
    message = (
        f"I found {len(candidates)} expense{'s' if len(candidates) != 1 else ''} worth {format_money_minor(potential, currency)} that may be worth reviewing. Nothing is labelled avoidable until you decide."
        if candidates else
        "I didn’t find evidence strong enough to call any recorded expense avoidable. Mark discretionary items or connect subscription data to improve this analysis."
    )
    widget = Widget(
        id=f"avoidable-{today.isoformat()}-{uuid4()}",
        type=WidgetType.AVOIDABLE_EXPENSES,
        data={"title": "Potentially avoidable expenses", "body": "Review candidates—this is not an automatic judgement.", "transactions": candidates, "potentialMinor": potential, "currency": currency},
        actions=[
            WidgetAction(
                id=f"nature-{item['id']}-{nature.value}",
                label=nature.value.replace("_", " ").title(),
                action=WidgetActionId.SET_SPEND_NATURE,
                style="secondary",
                payload={"transactionId": item["id"], "spendNature": nature.value},
            )
            for item in candidates
            for nature in (
                SpendNature.ESSENTIAL,
                SpendNature.DISCRETIONARY,
                SpendNature.POTENTIALLY_AVOIDABLE,
            )
        ],
    )
    citations = [DataReference(label="Candidate expense transactions", entity_type="transaction", entity_ids=[item["id"] for item in candidates], query={"start": start.isoformat(), "end": today.isoformat()})]
    return IntelligenceResult(message, [widget], citations)


def loan_strategy(db: Session, user_id: UUID, currency: str) -> IntelligenceResult:
    loans = _active_loans(db, user_id, currency)
    if not loans:
        widget = Widget(
            id=f"loan-setup-{uuid4()}",
            type=WidgetType.LOAN_CALCULATOR,
            data={"title": "Add your loan details", "body": "Enter outstanding principal, rate, remaining months and an optional prepayment. Saveable loan profiles are now supported by the backend.", "prepaymentMinor": 0},
            actions=[WidgetAction(
                id="calculate",
                label="Calculate",
                action=WidgetActionId.CALCULATE_LOAN_SCENARIO,
                style="primary",
                payload={},
            )],
        )
        return IntelligenceResult(
            "I need the loan principal, rate and remaining tenure before comparing reduction strategies.",
            [widget],
            [DataReference(label="No active saved loan profile was available", entity_type="loan")],
        )
    strategies: list[LoanStrategySummary] = []
    for loan in loans:
        candidate_amounts = sorted({loan.current_emi_minor or 0, loan.outstanding_principal_minor // 20, loan.outstanding_principal_minor // 10})
        options = [
            cast(
                LoanStrategyOption,
                loan_strategy_options(
                    loan.outstanding_principal_minor,
                    float(loan.annual_rate_percent),
                    loan.remaining_tenure_months,
                    amount,
                    float(loan.prepayment_fee_percent),
                ),
            )
            for amount in candidate_amounts
            if amount > 0
        ]
        strategies.append({
            "loanId": str(loan.id), "name": loan.name, "lender": loan.lender,
            "principalMinor": loan.outstanding_principal_minor, "currency": loan.currency,
            "annualRatePercent": float(loan.annual_rate_percent), "tenureMonths": loan.remaining_tenure_months,
            "options": options,
        })
    strategy_blocks = []
    for strategy in strategies:
        strategy_blocks.append(markdown_section(
            f"{strategy['name']} — {money(strategy['principalMinor'], strategy['currency'])} at {strategy['annualRatePercent']}% for {strategy['tenureMonths']} months",
            markdown_table(
                ["Prepayment", "Fee", "Lower EMI", "Interest saved (lower EMI)", "Months saved (shorter tenure)", "Interest saved (shorter tenure)"],
                [
                    [
                        money(option["prepayment_minor"], strategy["currency"]),
                        money(option["fee_minor"], strategy["currency"]),
                        money(option["lower_emi"].get("emi_minor"), strategy["currency"]),
                        money(option["lower_emi"].get("interest_saved_minor"), strategy["currency"]),
                        option["shorter_tenure"].get("months_saved", "—"),
                        money(option["shorter_tenure"].get("interest_saved_minor"), strategy["currency"]),
                    ]
                    for option in strategy["options"]
                ],
            ),
        ))
    citations = [DataReference(label="Stored active loan terms", entity_type="loan", entity_ids=[str(loan.id) for loan in loans])]
    return IntelligenceResult(
        join_blocks(
            "I modelled prepayment options for your active loans. Shorter tenure generally saves more interest than lowering EMI, but choose only an amount that preserves your cash reserve.",
            *strategy_blocks,
        ),
        [],
        citations,
    )


def recurring_expense_patterns(db: Session, user_id: UUID, currency: str, today: date) -> IntelligenceResult:
    recurring = recurring_rows(db, user_id)
    message = (
        f"I found {len(recurring)} recurring expense pattern{'s' if len(recurring) != 1 else ''}."
        if recurring
        else "I don’t have enough repeated transactions to identify a recurring expense yet."
    )
    citations = [DataReference(label="Repeated merchant transactions", entity_type="transaction", query={"patterns": len(recurring)})]
    return IntelligenceResult(join_blocks(message, _recurring_markdown(recurring)), [], citations)


def _comparison_markdown(result: dict, *, title: str) -> str:
    def period_row(label: str, period: dict) -> list:
        return [
            label,
            f"{period['start']} – {period['end']}",
            money(period["total_minor"], period["currency"]),
            period["count"],
        ]

    return markdown_section(
        title,
        markdown_table(
            ["Period", "Window", "Spent", "Transactions"],
            [period_row("Previous period", result["previous"]), period_row("Current period", result["current"])],
        ),
        "_The periods use the same elapsed-day window for a fair comparison._",
    )


def monthly_spending_comparison(db: Session, user_id: UUID, currency: str, today: date) -> IntelligenceResult:
    result = monthly_comparison_data(db, user_id, today)
    diff = result["difference_minor"]
    message = (
        f"This month is {format_money_minor(abs(diff), result['current']['currency'])} "
        f"{'higher' if diff > 0 else 'lower' if diff < 0 else 'different'} than the same point last month."
    )
    citations = [DataReference(label="Transactions included", entity_type="transaction", query={"current": result["current"], "previous": result["previous"]})]
    return IntelligenceResult(
        join_blocks(message, _comparison_markdown(result, title="Monthly spending")),
        [],
        citations,
    )


def affordability_scenario(db: Session, user_id: UUID, currency: str, today: date, purchase_minor: int) -> IntelligenceResult:
    position = cash_totals(db, user_id)
    start, end = month_bounds(today)
    current_month = expense_summary(db, user_id, start, min(today, end))
    result = affordability(purchase_minor, max(position["net_minor"], 0), position["income_minor"], current_month["total_minor"], 6)
    if result["affordable_now"]:
        message = (
            f"Based on the money recorded here, {format_money_minor(purchase_minor, currency)} is affordable "
            "while preserving a six-month expense reserve."
        )
    else:
        months = result["months_to_goal"]
        message = (
            f"Not safely yet based on the records I have. You’re {format_money_minor(result['gap_minor'], currency)} "
            "short after keeping a six-month expense reserve."
        )
        if months:
            message += f" At your recorded surplus, that’s about {months} month{'s' if months != 1 else ''}."
    scenario = markdown_section(
        f"Can I afford {format_money_minor(purchase_minor, currency)}?",
        markdown_table(
            ["", "Amount"],
            [
                ["Purchase", money(result["purchase_minor"], currency)],
                ["Reserve required", money(result["emergency_reserve_minor"], currency)],
                ["Available after reserve", money(result["available_after_reserve_minor"], currency)],
                ["Gap", money(result["gap_minor"], currency)],
                ["Monthly surplus", money(result["monthly_surplus_minor"], currency)],
                ["Months to goal", result["months_to_goal"] if result["months_to_goal"] is not None else "—"],
            ],
        ),
        f"_{result['rule']} Based only on recorded transactions._",
    )
    citations = [DataReference(label="Recorded income and expenses", entity_type="transaction", query={"position": position, "month": current_month})]
    return IntelligenceResult(join_blocks(message, scenario), [], citations)


def tool_facing_rows(result: dict) -> list[dict]:
    """Tool- and chart-facing rows: money values carry the ``_minor`` suffix so
    the evidence postcondition admits their major-unit rendering (value/100).
    Values themselves are never scaled here."""
    is_count = "count" in str(result.get("metric", ""))
    rows = []
    for row in result.get("rows") or []:
        shaped = dict(row)
        if "value" in shaped and not is_count:
            shaped["value_minor"] = shaped.pop("value")
        rows.append(shaped)
    return rows


def _chart_widgets_for_plan(
    plan: AnalysisPlan,
    results: list[dict],
    currency: str,
) -> tuple[list[Widget], list[dict]]:
    """Bind each declared view to its executed dataset, degrading loudly.

    A failed chart never fails the analysis: the widget is withheld and the
    stable refusal code is recorded as a note instead of a silent pass.
    """
    if not plan.visualizations:
        return [], []
    lineage = {
        "origin": "analysis",
        "manifestHash": native_manifest_fingerprint(),
        "executedAt": now_utc().isoformat(),
    }
    by_dataset: dict[str, dict] = {}
    for result in results:
        name = str(result.get("name") or "")
        by_dataset.setdefault(name, result)
        by_dataset.setdefault(dataset_id(name), result)
    widgets: list[Widget] = []
    notes: list[dict] = []
    for view in plan.visualizations:
        try:
            selected_result = by_dataset.get(view.dataset)
            if selected_result is None:
                raise ChartSpecError(
                    f"View {view.id} references dataset {view.dataset}, which matches no plan query",
                    code="unknown_dataset",
                )
            widgets.append(build_chart_widget(
                view,
                tool_facing_rows(selected_result),
                selected_result.get("currency") or currency,
                lineage,
            ))
        except ChartSpecError as error:
            notes.append({
                "view": view.id,
                "dataset": view.dataset,
                "code": error.code,
                "detail": str(error),
            })
    return widgets, notes


def execute_analysis_plan(db: Session, user_id: UUID, today: date, plan: AnalysisPlan) -> IntelligenceResult:
    currency = user_currency(db, user_id)
    if plan.analysis_type == "three_month_allocation":
        return three_month_allocation(db, user_id, currency, today)
    if plan.analysis_type == "avoidable_expenses":
        return avoidable_expense_candidates(db, user_id, currency, today)
    if plan.analysis_type == "loan_strategy":
        return loan_strategy(db, user_id, currency)
    if plan.analysis_type == "recurring_expenses":
        return recurring_expense_patterns(db, user_id, currency, today)
    if plan.analysis_type == "monthly_comparison":
        return monthly_spending_comparison(db, user_id, currency, today)
    if plan.analysis_type == "affordability":
        return affordability_scenario(db, user_id, currency, today, plan.service_inputs["purchase_minor"])
    results = [execute_finance_query(db, user_id, query) for query in plan.queries]
    transforms: list[dict] = []
    for transform in plan.transforms:
        transforms.append(_apply_transform(results, transforms, transform))
    context, context_citations = _load_context(db, user_id, currency, today, plan.context_sources)
    chart_widgets, chart_notes = _chart_widgets_for_plan(plan, results, currency)
    labeled_results, labeled_transforms = _presentation_labels(results, transforms)
    blocks = [
        _semantic_message(results, transforms),
        *(_result_markdown(result) for result in labeled_results),
        *(
            _transform_markdown(transform, currency)
            for transform in labeled_transforms
            if transform.get("values")
        ),
        _context_markdown(context, currency),
    ]
    citations = [
        DataReference(
            label=result["metric_definition"],
            entity_type="semantic_query",
            query={
                **query.model_dump(mode="json"),
                "registry_version": result["registry_version"],
                "schema_hash": result["schema_hash"],
            },
        )
        for query, result in zip(plan.queries, results)
    ]
    return IntelligenceResult(
        join_blocks(*blocks),
        chart_widgets,
        [*citations, *context_citations],
        query_results=results,
        chart_notes=chart_notes,
    )
