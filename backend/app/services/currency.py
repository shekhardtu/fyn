from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import User


def normalize_currency(value: str) -> str:
    """Return the canonical ISO-style currency code used at every boundary."""
    currency = value.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("Currency must be a three-letter code")
    return currency


def user_currency(db: Session, user_id: UUID) -> str:
    """Resolve currency from the authenticated user's single persisted setting."""
    value = db.scalar(select(User.currency).where(User.id == user_id))
    if not value:
        raise ValueError("Authenticated user has no currency setting")
    return normalize_currency(value)


def user_timezone(db: Session, user_id: UUID) -> str:
    value = db.scalar(select(User.timezone).where(User.id == user_id))
    if not value:
        raise ValueError("Authenticated user has no timezone setting")
    return value


def format_money_minor(amount_minor: int, currency: str) -> str:
    sign = "-" if amount_minor < 0 else ""
    amount = Decimal(abs(amount_minor)) / Decimal(100)
    code = normalize_currency(currency)
    decimals = 0 if amount == amount.to_integral() else 2
    if code == "INR":
        rendered = f"{amount:.{decimals}f}"
        whole, dot, fraction = rendered.partition(".")
        if len(whole) > 3:
            head, tail = whole[:-3], whole[-3:]
            pairs: list[str] = []
            while head:
                pairs.insert(0, head[-2:])
                head = head[:-2]
            whole = ",".join([*pairs, tail])
        return f"{sign}₹{whole}{dot}{fraction}"
    return f"{sign}{code} {amount:,.{decimals}f}"
