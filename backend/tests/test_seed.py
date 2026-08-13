from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.demo_finances import DEMO_CATEGORY_SLUGS, DEMO_EXPENSES, seed_demo_finances
from app.domain import TaxonomyScope
from app.event_time import utc_range_for_local_dates
from app.models import Category, Subcategory, Transaction, User
from app.seed import DEFAULT_USER_EMAIL, seed_demo_user, seed_system_taxonomy
from app.taxonomy_catalog import DEFAULT_TAXONOMY


def empty_db() -> tuple[object, Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine, Session(engine)


def test_system_taxonomy_seeds_without_creating_a_user_and_is_idempotent():
    engine, db = empty_db()
    try:
        seed_system_taxonomy(db)
        seed_system_taxonomy(db)

        categories = list(db.scalars(select(Category)))
        subcategories = list(db.scalars(select(Subcategory)))
        expected_subcategories = sum(len(item[2]) for item in DEFAULT_TAXONOMY.values())

        assert db.scalar(select(func.count()).select_from(User)) == 0
        assert len(categories) == len(DEFAULT_TAXONOMY)
        assert len(subcategories) == expected_subcategories
        assert all(item.scope == TaxonomyScope.SYSTEM.value and item.owner_user_id is None for item in categories)
        assert all(item.scope == TaxonomyScope.SYSTEM.value and item.owner_user_id is None for item in subcategories)
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_demo_user_seeding_does_not_install_taxonomy():
    engine, db = empty_db()
    try:
        user = seed_demo_user(db)

        assert user is not None
        assert user.email == DEFAULT_USER_EMAIL
        assert db.scalar(select(func.count()).select_from(Category)) == 0
        assert db.scalar(select(func.count()).select_from(Subcategory)) == 0
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_demo_finances_cover_ten_categories_and_are_idempotent():
    engine, db = empty_db()
    try:
        user = seed_demo_user(db)
        seed_system_taxonomy(db)
        assert user is not None

        expected = len(DEMO_EXPENSES) * 2 + 4
        assert seed_demo_finances(db, user, today=date(2026, 8, 13)) == expected
        assert seed_demo_finances(db, user, today=date(2026, 8, 13)) == 0
        assert db.scalar(select(func.count()).select_from(Transaction).where(Transaction.user_id == user.id)) == expected

        start_at, end_at = utc_range_for_local_dates(date(2026, 8, 1), date(2026, 8, 13), user.timezone)
        current_category_ids = set(db.scalars(
            select(Transaction.category_id).where(
                Transaction.user_id == user.id,
                Transaction.transaction_type == "expense",
                Transaction.transaction_at >= start_at,
                Transaction.transaction_at < end_at,
            )
        ))
        assert len(current_category_ids) == 10

        for slug in DEMO_CATEGORY_SLUGS:
            category = db.scalar(select(Category).where(Category.slug == slug))
            subcategory_count = db.scalar(select(func.count()).select_from(Subcategory).where(Subcategory.category_id == category.id))
            assert 4 <= subcategory_count <= 8
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()
