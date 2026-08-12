from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Category, Subcategory, User
from .taxonomy_catalog import DEFAULT_TAXONOMY


DEFAULT_USER_EMAIL = "demo@fynai.local"


def default_user(db: Session) -> User | None:
    return db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))


def seed_defaults(db: Session) -> User | None:
    """Install the shared taxonomy, and the local account on an empty database.

    The seeded user predates authentication and is adopted by the first real
    sign-in, which replaces its placeholder address. It is created only while no
    account exists at all, so a database that already has real users never grows
    an extra unowned one — and never re-seeds one for a later stranger to claim.
    """
    user = default_user(db)
    if not user and not db.scalar(select(func.count()).select_from(User)):
        user = User(email=DEFAULT_USER_EMAIL, display_name="Hari")
        db.add(user)
        db.flush()

    for slug, (name, icon, subs) in DEFAULT_TAXONOMY.items():
        category = db.scalar(select(Category).where(Category.slug == slug))
        if not category:
            category = Category(slug=slug, name=name, icon=icon)
            db.add(category)
            db.flush()
        existing = {s.slug for s in db.scalars(select(Subcategory).where(Subcategory.category_id == category.id))}
        for sub_slug, sub_name in subs:
            if sub_slug not in existing:
                db.add(Subcategory(category_id=category.id, slug=sub_slug, name=sub_name))
    db.commit()
    if user is not None:
        db.refresh(user)
    return user
