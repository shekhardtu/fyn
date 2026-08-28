"""Reset and seed the dedicated localhost browser-eval account.

This script is deliberately not imported by the application. It refuses to
touch a non-local database or any identity other than the fixed eval phone.
Ordinary resets preserve the identity and browser sessions while replacing
all application data; explicit cleanup still deletes the complete account.
"""

from __future__ import annotations

import argparse
from datetime import date, time, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.demo_finances import seed_demo_finances
from app.domain import IdentitySource, SpendNature, TransactionStatus, TransactionType
from app.event_time import from_local_parts, local_now
from app.models import (
    Category,
    OtpChallenge,
    Subcategory,
    Transaction,
    User,
    UserIdentity,
    UserSession,
)
from app.seed import seed_system_taxonomy
from app.services.user_data import DEPENDENT_USER_DATA, OWNED_USER_DATA, delete_user_data
from app.services.user_memory import clear_user_memories


EVAL_PHONE = "+919000000098"
EVAL_NAME = "FYN Quality Eval"
EVAL_MARKER = "fyn-quality-eval"
PRESERVED_AUTH_MODELS = frozenset({UserIdentity, UserSession, OtpChallenge})


def _assert_local_database() -> None:
    settings = get_settings()
    database_url = settings.database_url.casefold()
    if settings.environment != "development" or not any(
        marker in database_url for marker in ("localhost", "127.0.0.1", "@postgres:")
    ):
        raise SystemExit(
            "Refusing to reset the eval identity outside a development database."
        )


def _previous_month(value: date) -> date:
    return (value.replace(day=1) - timedelta(days=1)).replace(day=1)


def _reset_eval_application_data(db: Session, user: User) -> None:
    """Replace benchmark data without revoking the browser's eval session."""
    clear_user_memories(user.id)
    owned_by_model = {spec.model: spec for spec in OWNED_USER_DATA}
    for dependent in DEPENDENT_USER_DATA:
        parent = owned_by_model[dependent.parent_model]
        parent_ids = list(db.scalars(
            select(dependent.parent_model.id).where(
                getattr(dependent.parent_model, parent.owner_column) == user.id
            )
        ))
        if parent_ids:
            db.execute(delete(dependent.model).where(
                getattr(dependent.model, dependent.parent_column).in_(parent_ids)
            ))
    for owned in OWNED_USER_DATA:
        if owned.model in PRESERVED_AUTH_MODELS:
            continue
        db.execute(delete(owned.model).where(
            getattr(owned.model, owned.owner_column) == user.id
        ))
    user.display_name = EVAL_NAME
    user.currency = "INR"
    user.timezone = "Asia/Kolkata"
    db.commit()


def _seed_refunds(db, user: User, today: date) -> int:
    food = db.scalar(select(Category).where(Category.slug == "food"))
    if food is None:
        raise RuntimeError("System Food taxonomy is missing")
    dining = db.scalar(
        select(Subcategory).where(
            Subcategory.category_id == food.id,
            Subcategory.slug == "dining",
        )
    )
    if dining is None:
        raise RuntimeError("System Food/Dining taxonomy is missing")

    months = [today.replace(day=1)]
    months.append(_previous_month(months[-1]))
    months.append(_previous_month(months[-1]))
    for index, month in enumerate(months):
        amount_minor = (500 - index * 100) * 100
        occurred_at = from_local_parts(
            month.replace(day=min(8 + index, today.day if month == months[0] else 28)),
            time(hour=15, minute=index * 7),
            user.timezone,
        )
        db.add(
            Transaction(
                user_id=user.id,
                transaction_type=TransactionType.REFUND.value,
                amount_minor=amount_minor,
                currency=user.currency,
                merchant_name="Quality Cafe Refund",
                category_id=food.id,
                subcategory_id=dining.id,
                transaction_at=occurred_at,
                posted_at=occurred_at,
                spend_nature=SpendNature.DISCRETIONARY.value,
                status=TransactionStatus.CONFIRMED.value,
                notes=f"{EVAL_MARKER}:{month:%Y-%m}:refund:food:dining",
            )
        )
    db.commit()
    return len(months)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="delete the dedicated eval account instead of recreating it",
    )
    args = parser.parse_args()
    _assert_local_database()
    with SessionLocal() as db:
        existing = db.scalar(select(User).where(User.phone == EVAL_PHONE))
        if args.cleanup:
            if existing is not None:
                if existing.display_name != EVAL_NAME:
                    raise SystemExit("Refusing to delete a non-eval identity at the reserved phone.")
                delete_user_data(db, existing)
            print(f"cleaned {EVAL_NAME}")
            return

        seed_system_taxonomy(db)
        if existing is not None:
            if existing.display_name != EVAL_NAME:
                raise SystemExit("Refusing to reset a non-eval identity at the reserved phone.")
            user = existing
            _reset_eval_application_data(db, user)
        else:
            user = User(
                phone=EVAL_PHONE,
                display_name=EVAL_NAME,
                currency="INR",
                timezone="Asia/Kolkata",
            )
            db.add(user)
            db.flush()
            db.add(
                UserIdentity(
                    user_id=user.id,
                    provider="phone",
                    identifier=EVAL_PHONE,
                    source=IdentitySource.OTP.value,
                )
            )
            db.commit()
            db.refresh(user)

        today = local_now(user.timezone).date()
        seeded = seed_demo_finances(db, user, today=today)
        refunds = _seed_refunds(db, user, today)

        # Keep stdout content-free and shell-friendly. The exact fixture is
        # checked by the browser through authenticated product APIs.
        print(
            f"seeded {EVAL_NAME}: {seeded} demo rows + {refunds} refunds "
            f"for {today.isoformat()}"
        )


if __name__ == "__main__":
    main()
