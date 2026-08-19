"""Customer traits: deterministic summaries of one user's canonical data.

A trait is a small, explainable number the rest of the product can lean on
without recomputing it — how regularly income arrives, what a normal month of
spending looks like per category, how much of a month is already committed to
recurring charges. Every trait here is arithmetic over canonical transactions;
nothing is modelled, guessed, or asked of a language model.

Two rules make traits safe to surface:

* The stamp travels with the value. ``computed_at`` and ``freshness_note`` are
  columns, not decoration, and :func:`traits_context_line` prints the stamp
  beside every value. A trait that appears without its stamp would read as
  current forever, which is the precise way a stale number becomes a lie.
* Absence beats invention. A trait whose evidence is missing is not stored, and
  a trait whose evidence disappears is deleted on the next computation. There
  is no placeholder value for "we don't know yet".

Writes here only flush; the caller's transaction decides when they commit, so
refreshing traits inside a conversation turn cannot commit that turn early.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from uuid import UUID

from ..domain import TransactionType
from ..event_time import as_utc, local_date, now_utc, utc_range_for_local_dates
from ..models import Category, Transaction, User, UserTrait
from .currency import user_currency, user_timezone
from .finance_time import month_bounds, shift_month
from .intelligence import recurring_rows
from .transactions import apply_canonical_transaction_scope, apply_expense_transaction_scope

TRAIT_INCOME_CADENCE = "income_cadence"
TRAIT_CATEGORY_BASELINES = "category_baselines"
TRAIT_RECURRING_LOAD = "recurring_load"

# Baselines describe *complete* months only: the month in progress would drag
# every category down for no reason other than the calendar.
BASELINE_MONTHS = 3
# The same monthly band the recurring-expense detector uses, so "monthly" means
# one thing across the product.
MONTHLY_GAP_DAYS = (20, 40)
WEEKS_PER_MONTH = Decimal(52) / Decimal(12)
DEFAULT_TRAIT_MAX_AGE_HOURS = 24
# How many categories the compact context line prints before saying how many
# it left out. Truncation is stated, never silent.
CONTEXT_CATEGORY_LIMIT = 6


def _minor(value: Decimal) -> int:
    """Round a money/day quantity to a whole unit, half away from zero."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _income_cadence(db: Session, user_id: UUID, currency: str, timezone_name: str) -> dict | None:
    """How regularly income lands, by the median gap between income records."""
    days = sorted(
        local_date(value, timezone_name)
        for value in db.scalars(
            apply_canonical_transaction_scope(
                select(Transaction.transaction_at), user_id, currency=currency
            ).where(Transaction.transaction_type == TransactionType.INCOME)
        )
    )
    gaps = [(right - left).days for left, right in zip(days, days[1:])]
    if not gaps:
        # One income record (or none) describes no cadence at all.
        return None
    median_gap = _minor(Decimal(str(median(gaps))))
    monthly = MONTHLY_GAP_DAYS[0] <= median_gap <= MONTHLY_GAP_DAYS[1]
    return {
        "cadence": "monthly" if monthly else "irregular",
        "median_gap_days": median_gap,
        "observations": len(days),
    }


def _category_baselines(
    db: Session, user_id: UUID, currency: str, timezone_name: str, today: date
) -> dict | None:
    """Mean monthly spend per category over the last three complete months."""
    current_month = today.replace(day=1)
    months = [shift_month(current_month, -offset) for offset in range(BASELINE_MONTHS, 0, -1)]
    start_at, end_at = utc_range_for_local_dates(
        months[0], month_bounds(months[-1])[1], timezone_name
    )
    window = (Transaction.transaction_at >= start_at, Transaction.transaction_at < end_at)
    # select_from is explicit: the first selected column belongs to Category, and
    # letting SQLAlchemy infer the join's left side from it would join categories
    # to itself.
    by_category = (
        select(Category.slug, Category.name, func.sum(Transaction.amount_minor))
        .select_from(Transaction)
        .join(Category, Category.id == Transaction.category_id)
    )
    rows = db.execute(
        apply_expense_transaction_scope(by_category, user_id, currency=currency)
        .where(*window)
        .group_by(Category.slug, Category.name)
    ).all()
    # Spend with no category is real spend. It is reported separately rather
    # than dropped, so the baselines never quietly understate the month.
    uncategorized = db.scalar(
        apply_expense_transaction_scope(
            select(func.coalesce(func.sum(Transaction.amount_minor), 0)),
            user_id,
            currency=currency,
        ).where(*window, Transaction.category_id.is_(None))
    ) or 0
    if not rows and not uncategorized:
        return None
    months_divisor = Decimal(BASELINE_MONTHS)
    categories = sorted(
        (
            {
                "slug": slug,
                "name": name,
                "mean_minor": _minor(Decimal(int(total)) / months_divisor),
            }
            for slug, name, total in rows
        ),
        key=lambda item: (-item["mean_minor"], item["slug"]),
    )
    return {
        "months": [item.strftime("%Y-%m") for item in months],
        "currency": currency,
        "categories": categories,
        "uncategorized_mean_minor": _minor(Decimal(int(uncategorized)) / months_divisor),
    }


def _recurring_load(db: Session, user_id: UUID, currency: str) -> dict | None:
    """Monthly cost of the recurring expenses the shared detector found.

    Weekly patterns are converted at 52/12 weeks per month rather than being
    counted once, so a weekly charge is not understated by a factor of four.
    """
    patterns = recurring_rows(db, user_id)
    if not patterns:
        return None
    monthly = Decimal(0)
    cadences: dict[str, int] = {}
    for item in patterns:
        amount = Decimal(int(item["amount_minor"]))
        cadence = str(item["cadence"])
        cadences[cadence] = cadences.get(cadence, 0) + 1
        monthly += amount * WEEKS_PER_MONTH if cadence == "weekly" else amount
    return {
        "monthly_minor": _minor(monthly),
        "currency": currency,
        "pattern_count": len(patterns),
        "cadences": dict(sorted(cadences.items())),
    }


def compute_traits(db: Session, user: User, today: date) -> list[UserTrait]:
    """Recompute every trait from canonical data and upsert the results.

    Idempotent by construction: rows are keyed by ``(user_id, name)`` and
    rewritten in place, so a second run in the same state changes nothing but
    the stamp. A trait whose evidence has gone is deleted, never left behind.
    """
    currency = user_currency(db, user.id)
    timezone_name = user_timezone(db, user.id)
    computed_at = now_utc()
    freshness_note = f"computed from data through {today.isoformat()}"
    computed: dict[str, dict | None] = {
        TRAIT_INCOME_CADENCE: _income_cadence(db, user.id, currency, timezone_name),
        TRAIT_CATEGORY_BASELINES: _category_baselines(
            db, user.id, currency, timezone_name, today
        ),
        TRAIT_RECURRING_LOAD: _recurring_load(db, user.id, currency),
    }
    existing = {
        row.name: row
        for row in db.scalars(select(UserTrait).where(UserTrait.user_id == user.id))
    }
    stored: list[UserTrait] = []
    for name, value in computed.items():
        row = existing.get(name)
        if value is None:
            if row is not None:
                db.delete(row)
            continue
        if row is None:
            row = UserTrait(user_id=user.id, name=name)
            db.add(row)
        row.value = value
        row.computed_at = computed_at
        row.freshness_note = freshness_note
        stored.append(row)
    db.flush()
    return sorted(stored, key=lambda item: item.name)


def get_traits(
    db: Session,
    user: User,
    *,
    today: date | None = None,
    max_age_hours: int = DEFAULT_TRAIT_MAX_AGE_HOURS,
) -> list[UserTrait]:
    """This user's traits, recomputed when any stored one has gone stale.

    Traits are computed together from one snapshot, so one stale row makes the
    whole set stale: serving a fresh trait beside an old one would present a
    single moment that never existed. Every returned row carries its own
    ``computed_at``, because the caller has no other way to know how old the
    number it is about to use really is.

    A user whose data supports no trait at all has nothing to go stale, so this
    recomputes on every call for them. That is deliberate — the alternative is a
    stored marker row asserting "still nothing", which is a value we would then
    have to keep honest — and it is cheap precisely because their data is thin.
    """
    stored = list(db.scalars(
        select(UserTrait).where(UserTrait.user_id == user.id).order_by(UserTrait.name)
    ))
    horizon = now_utc() - timedelta(hours=max_age_hours)
    if stored and all(as_utc(row.computed_at) >= horizon for row in stored):
        return stored
    resolved_today = today or local_date(now_utc(), user_timezone(db, user.id))
    return compute_traits(db, user, resolved_today)


def _stamp(value: datetime) -> str:
    return as_utc(value).isoformat()


def _summarize(trait: UserTrait) -> str:
    value: Any = trait.value or {}
    if trait.name == TRAIT_INCOME_CADENCE:
        return (
            f"{value.get('cadence')}, median gap {value.get('median_gap_days')}d "
            f"across {value.get('observations')} income records"
        )
    if trait.name == TRAIT_CATEGORY_BASELINES:
        categories = list(value.get("categories") or [])
        shown = ", ".join(
            f"{item['slug']} {item['mean_minor']}"
            for item in categories[:CONTEXT_CATEGORY_LIMIT]
        )
        remainder = len(categories) - CONTEXT_CATEGORY_LIMIT
        parts = [part for part in (shown, f"+{remainder} more" if remainder > 0 else "") if part]
        uncategorized = value.get("uncategorized_mean_minor") or 0
        if uncategorized:
            parts.append(f"uncategorized {uncategorized}")
        months = value.get("months") or []
        window = f"{months[0]}..{months[-1]}" if months else "no complete months"
        return f"mean minor units/month over {window}: " + ("; ".join(parts) or "none")
    if trait.name == TRAIT_RECURRING_LOAD:
        return (
            f"{value.get('monthly_minor')} minor units/month across "
            f"{value.get('pattern_count')} recurring patterns"
        )
    return str(value)


def traits_context_line(traits: list[UserTrait]) -> str:
    """One compact line of traits, each value carrying its own stamp.

    The stamp is not optional formatting. A reader that sees only the value has
    no way to tell yesterday's number from last month's, so the two are printed
    together or not at all.
    """
    return " | ".join(
        f"{trait.name}: {_summarize(trait)} (computed_at {_stamp(trait.computed_at)}; "
        f"{trait.freshness_note})"
        for trait in sorted(traits, key=lambda item: item.name)
    )
