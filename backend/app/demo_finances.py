from __future__ import annotations

from calendar import monthrange
from datetime import date, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .domain import SpendNature, TransactionStatus, TransactionType
from .event_time import from_local_parts, local_now
from .models import Category, Subcategory, Transaction, User


DEMO_MARKER = "fyn-demo"

# category, subcategory, merchant, amount in minor INR units
DEMO_EXPENSES = (
    ("food", "groceries", "BigBasket", 620_000),
    ("food", "dining", "Toit", 285_000),
    ("food", "delivery", "Swiggy", 145_000),
    ("food", "coffee", "Blue Tokai", 78_000),
    ("food", "ice_cream", "Naturals", 54_000),
    ("food", "other", "Office snacks", 62_000),
    ("transport", "cab", "Uber", 185_000),
    ("transport", "fuel", "IndianOil", 360_000),
    ("transport", "public_transit", "Namma Metro", 62_000),
    ("transport", "flights", "IndiGo", 980_000),
    ("transport", "parking", "Park+", 45_000),
    ("transport", "tolls", "FASTag", 70_000),
    ("transport", "other", "Auto rides", 52_000),
    ("shopping", "clothing", "Uniqlo", 349_000),
    ("shopping", "electronics", "Croma", 699_000),
    ("shopping", "household", "IKEA", 228_000),
    ("shopping", "gifts", "Ferns N Petals", 120_000),
    ("shopping", "beauty", "Nykaa", 165_000),
    ("shopping", "books", "Crossword", 89_000),
    ("shopping", "other", "Local market", 76_000),
    ("entertainment", "movies", "PVR Cinemas", 98_000),
    ("entertainment", "events", "BookMyShow", 180_000),
    ("entertainment", "games", "Steam", 120_000),
    ("entertainment", "music", "Spotify", 11_900),
    ("entertainment", "hobbies", "Itsy Bitsy", 65_000),
    ("entertainment", "streaming", "Prime Video", 29_900),
    ("entertainment", "other", "Bowling alley", 90_000),
    ("bills", "utilities", "BESCOM", 245_000),
    ("bills", "internet", "ACT Fibernet", 119_900),
    ("bills", "phone", "Airtel", 69_900),
    ("bills", "subscriptions", "Netflix", 64_900),
    ("bills", "insurance", "HDFC Ergo", 220_000),
    ("bills", "maintenance", "Appliance service", 90_000),
    ("bills", "other", "Cloud storage", 13_000),
    ("health", "doctor", "Apollo Clinic", 150_000),
    ("health", "pharmacy", "Apollo Pharmacy", 86_000),
    ("health", "fitness", "Cult.fit", 249_900),
    ("health", "dental", "Clove Dental", 180_000),
    ("health", "therapy", "Amaha", 120_000),
    ("health", "diagnostics", "Tata 1mg", 95_000),
    ("health", "other", "HealthKart", 70_000),
    ("housing", "rent", "Home rent", 3_200_000),
    ("housing", "maintenance", "Apartment association", 350_000),
    ("housing", "repairs", "Urban Company", 180_000),
    ("housing", "furnishings", "Home Centre", 220_000),
    ("housing", "domestic_help", "Home help", 300_000),
    ("housing", "appliances", "Livpure", 65_000),
    ("housing", "other", "Cleaning supplies", 48_000),
    ("education", "courses", "Coursera", 199_900),
    ("education", "tuition", "Language class", 250_000),
    ("education", "books", "Sapna Book House", 110_000),
    ("education", "certifications", "Certification exam", 320_000),
    ("education", "school_fees", "School fees", 450_000),
    ("education", "workshops", "Design workshop", 150_000),
    ("education", "other", "Stationery shop", 65_000),
    ("travel", "accommodation", "Airbnb", 780_000),
    ("travel", "flights", "Air India", 1_250_000),
    ("travel", "trains", "IRCTC", 145_000),
    ("travel", "local_transport", "Airport taxi", 110_000),
    ("travel", "activities", "Tripadvisor", 220_000),
    ("travel", "visa", "Travel documents", 85_000),
    ("travel", "other", "Forex fee", 48_000),
    ("personal_care", "salon", "Toni & Guy", 120_000),
    ("personal_care", "grooming", "Bombay Shaving Company", 65_000),
    ("personal_care", "skincare", "Health & Glow", 145_000),
    ("personal_care", "wellness", "Wellness spa", 180_000),
    ("personal_care", "laundry", "Tumbledry", 52_000),
    ("personal_care", "other", "Tailoring", 60_000),
)

DEMO_CATEGORY_SLUGS = frozenset(item[0] for item in DEMO_EXPENSES)
ESSENTIAL_CATEGORIES = frozenset({"food", "transport", "bills", "health", "housing", "education"})
PREVIOUS_MONTH_FACTORS = (86, 93, 101, 89, 96)


def _previous_month(month: date) -> date:
    return (month.replace(day=1) - timedelta(days=1)).replace(day=1)


def _at(month: date, index: int, timezone_name: str, *, through_day: int | None = None):
    last_day = monthrange(month.year, month.month)[1]
    available_days = min(last_day, through_day or last_day)
    day = month.replace(day=1 + index % available_days)
    clock = time(hour=8 + index % 13, minute=(index * 7) % 60)
    return from_local_parts(day, clock, timezone_name)


def seed_demo_finances(db: Session, user: User, *, today: date | None = None) -> int:
    """Add two realistic, repeatable months of demo finances for one user.

    The caller chooses the account explicitly. Each row carries a stable marker
    in ``notes``; rerunning fills missing rows and never duplicates existing
    demo data or changes finance records the user entered themselves.
    """
    today = today or local_now(user.timezone).date()
    current_month = today.replace(day=1)
    previous_month = _previous_month(current_month)
    category_slugs = DEMO_CATEGORY_SLUGS | {"income"}
    categories = {
        category.slug: category
        for category in db.scalars(select(Category).where(Category.slug.in_(category_slugs)))
    }
    subcategories = {
        (category.slug, subcategory.slug): subcategory
        for category, subcategory in db.execute(
            select(Category, Subcategory)
            .join(Subcategory, Subcategory.category_id == Category.id)
            .where(Category.slug.in_(category_slugs))
        )
    }
    expected_paths = {(category, subcategory) for category, subcategory, _merchant, _amount in DEMO_EXPENSES}
    expected_paths |= {("income", "salary"), ("income", "freelance")}
    missing_paths = sorted(expected_paths - set(subcategories))
    if missing_paths:
        rendered = ", ".join(f"{category}/{subcategory}" for category, subcategory in missing_paths)
        raise RuntimeError(f"Seed the system taxonomy before demo finances; missing: {rendered}")

    existing = {
        transaction.notes: transaction
        for transaction in db.scalars(
            select(Transaction).where(
                Transaction.user_id == user.id,
                Transaction.notes.like(f"{DEMO_MARKER}:%"),
            )
        )
    }
    added = 0

    def add_transaction(*, key: str, kind: TransactionType, amount_minor: int, occurred_at, category_slug: str, subcategory_slug: str, merchant: str, spend_nature: SpendNature) -> None:
        nonlocal added
        transaction = existing.get(key)
        if transaction:
            transaction.transaction_type = kind.value
            transaction.amount_minor = amount_minor
            transaction.currency = user.currency
            transaction.merchant_name = merchant
            transaction.category_id = categories[category_slug].id
            transaction.subcategory_id = subcategories[(category_slug, subcategory_slug)].id
            transaction.transaction_at = occurred_at
            transaction.posted_at = occurred_at
            transaction.spend_nature = spend_nature.value
            transaction.status = TransactionStatus.CONFIRMED.value
            return
        transaction = Transaction(
            user_id=user.id,
            transaction_type=kind.value,
            amount_minor=amount_minor,
            currency=user.currency,
            merchant_name=merchant,
            category_id=categories[category_slug].id,
            subcategory_id=subcategories[(category_slug, subcategory_slug)].id,
            transaction_at=occurred_at,
            posted_at=occurred_at,
            spend_nature=spend_nature.value,
            status=TransactionStatus.CONFIRMED.value,
            notes=key,
        )
        db.add(transaction)
        existing[key] = transaction
        added += 1

    for month, period, through_day in (
        (previous_month, "previous", today.day),
        (current_month, "current", today.day),
    ):
        for index, (category_slug, subcategory_slug, merchant, current_amount) in enumerate(DEMO_EXPENSES):
            amount_minor = current_amount if period == "current" else current_amount * PREVIOUS_MONTH_FACTORS[index % len(PREVIOUS_MONTH_FACTORS)] // 100
            add_transaction(
                key=f"{DEMO_MARKER}:{month:%Y-%m}:expense:{category_slug}:{subcategory_slug}",
                kind=TransactionType.EXPENSE,
                amount_minor=amount_minor,
                occurred_at=_at(month, index, user.timezone, through_day=through_day),
                category_slug=category_slug,
                subcategory_slug=subcategory_slug,
                merchant=merchant,
                spend_nature=SpendNature.ESSENTIAL if category_slug in ESSENTIAL_CATEGORIES else SpendNature.DISCRETIONARY,
            )

        incomes = (("salary", "Salary", 18_500_000), ("freelance", "Consulting", 2_500_000 if period == "current" else 1_800_000))
        for income_index, (subcategory_slug, merchant, amount_minor) in enumerate(incomes):
            add_transaction(
                key=f"{DEMO_MARKER}:{month:%Y-%m}:income:{subcategory_slug}",
                kind=TransactionType.INCOME,
                amount_minor=amount_minor,
                occurred_at=_at(month, len(DEMO_EXPENSES) + income_index, user.timezone, through_day=through_day),
                category_slug="income",
                subcategory_slug=subcategory_slug,
                merchant=merchant,
                spend_nature=SpendNature.UNKNOWN,
            )

    db.commit()
    return added
