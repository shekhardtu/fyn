from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .domain import TaxonomyScope
from .models import Category, Subcategory, User
from .taxonomy_catalog import DEFAULT_TAXONOMY


DEFAULT_USER_EMAIL = "demo@fynai.local"


def default_user(db: Session) -> User | None:
    return db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))


def seed_demo_user(db: Session) -> User | None:
    """Install the legacy local account on an otherwise empty database.

    The seeded user predates authentication and is adopted by the first real
    sign-in, which replaces its placeholder address. It is created only while no
    account exists at all, so a database that already has real users never grows
    an extra unowned one — and never re-seeds one for a later stranger to claim.

    Application startup deliberately does not call this helper. It exists for
    isolated tests that still exercise the legacy account-adoption path.
    """
    user = default_user(db)
    if not user and not db.scalar(select(func.count()).select_from(User)):
        user = User(email=DEFAULT_USER_EMAIL, display_name="Hari")
        db.add(user)
        db.flush()

    db.commit()
    if user is not None:
        db.refresh(user)
    return user


def seed_system_taxonomy(db: Session) -> None:
    """Install the shared category catalog without creating or owning a user.

    System taxonomy is application reference data: it exists before the first
    sign-in and is visible to every user. The operation is idempotent so startup
    can safely repair a partially populated catalog after a clean local reset.
    """

    for slug, (name, icon, subs) in DEFAULT_TAXONOMY.items():
        category = db.scalar(select(Category).where(Category.slug == slug))
        if not category:
            category = Category(
                slug=slug,
                name=name,
                icon=icon,
                scope=TaxonomyScope.SYSTEM.value,
                owner_user_id=None,
            )
            db.add(category)
            db.flush()
        existing = {s.slug for s in db.scalars(select(Subcategory).where(Subcategory.category_id == category.id))}
        for sub_slug, sub_name in subs:
            if sub_slug not in existing:
                db.add(Subcategory(
                    category_id=category.id,
                    slug=sub_slug,
                    name=sub_name,
                    scope=TaxonomyScope.SYSTEM.value,
                    owner_user_id=None,
                ))
    db.commit()
