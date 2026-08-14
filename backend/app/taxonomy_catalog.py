from __future__ import annotations

from .domain import TransactionType, ValueEnum


class DefaultCategorySlug(ValueEnum):
    FOOD = "food"
    TRANSPORT = "transport"
    SHOPPING = "shopping"
    ENTERTAINMENT = "entertainment"
    BILLS = "bills"
    HEALTH = "health"
    HOUSING = "housing"
    EDUCATION = "education"
    TRAVEL = "travel"
    PERSONAL_CARE = "personal_care"
    INCOME = "income"
    INVESTMENT = "investment"
    OTHER = "other"


OTHER_SUBCATEGORY = ("other", "Other")


# Category roots are part of transaction meaning, not decoration. Expenses may
# use any spending category, income and investments have dedicated roots, and
# the remaining directions do not use the category taxonomy at all. Keeping
# this compatibility rule beside the catalog gives extraction, persistence and
# repair code one definition to share.
TRANSACTION_CATEGORY_ROOTS = {
    TransactionType.INCOME: DefaultCategorySlug.INCOME,
    TransactionType.INVESTMENT: DefaultCategorySlug.INVESTMENT,
}
NON_EXPENSE_CATEGORY_SLUGS = frozenset(TRANSACTION_CATEGORY_ROOTS.values())


def category_slug_matches_transaction_type(
    transaction_type: TransactionType | str,
    category_slug: DefaultCategorySlug | str | None,
) -> bool:
    kind = TransactionType(str(transaction_type))
    normalized = str(category_slug) if category_slug is not None else None
    required = TRANSACTION_CATEGORY_ROOTS.get(kind)
    if required is not None:
        return normalized == required.value
    if kind is TransactionType.EXPENSE:
        return normalized is None or normalized not in {item.value for item in NON_EXPENSE_CATEGORY_SLUGS}
    return normalized is None


DEFAULT_TAXONOMY = {
    DefaultCategorySlug.FOOD: ("Food", "utensils", [("groceries", "Groceries"), ("dining", "Dining"), ("delivery", "Delivery"), ("coffee", "Coffee"), ("ice_cream", "Ice cream"), OTHER_SUBCATEGORY]),
    DefaultCategorySlug.TRANSPORT: ("Transport", "car", [("cab", "Cab"), ("fuel", "Fuel"), ("public_transit", "Public transit"), ("flights", "Flights"), ("parking", "Parking"), ("tolls", "Tolls"), OTHER_SUBCATEGORY]),
    DefaultCategorySlug.SHOPPING: ("Shopping", "shopping-bag", [("clothing", "Clothing"), ("electronics", "Electronics"), ("household", "Household"), ("gifts", "Gifts"), ("beauty", "Beauty"), ("books", "Books"), OTHER_SUBCATEGORY]),
    DefaultCategorySlug.ENTERTAINMENT: ("Entertainment", "sparkles", [("movies", "Movies"), ("events", "Events"), ("games", "Games"), ("music", "Music"), ("hobbies", "Hobbies"), ("streaming", "Streaming"), OTHER_SUBCATEGORY]),
    DefaultCategorySlug.BILLS: ("Bills", "receipt", [("utilities", "Utilities"), ("internet", "Internet"), ("phone", "Phone"), ("subscriptions", "Subscriptions"), ("insurance", "Insurance"), ("maintenance", "Maintenance"), OTHER_SUBCATEGORY]),
    DefaultCategorySlug.HEALTH: ("Health", "heart-pulse", [("doctor", "Doctor"), ("pharmacy", "Pharmacy"), ("fitness", "Fitness"), ("dental", "Dental"), ("therapy", "Therapy"), ("diagnostics", "Diagnostics"), OTHER_SUBCATEGORY]),
    DefaultCategorySlug.HOUSING: ("Housing", "house", [("rent", "Rent"), ("maintenance", "Maintenance"), ("repairs", "Repairs"), ("furnishings", "Furnishings"), ("domestic_help", "Domestic help"), ("appliances", "Appliances"), OTHER_SUBCATEGORY]),
    DefaultCategorySlug.EDUCATION: ("Education", "graduation-cap", [("courses", "Courses"), ("tuition", "Tuition"), ("books", "Books"), ("certifications", "Certifications"), ("school_fees", "School fees"), ("workshops", "Workshops"), OTHER_SUBCATEGORY]),
    DefaultCategorySlug.TRAVEL: ("Travel", "plane", [("accommodation", "Accommodation"), ("flights", "Flights"), ("trains", "Trains"), ("local_transport", "Local transport"), ("activities", "Activities"), ("visa", "Visa and documents"), OTHER_SUBCATEGORY]),
    DefaultCategorySlug.PERSONAL_CARE: ("Personal care", "sparkles", [("salon", "Salon"), ("grooming", "Grooming"), ("skincare", "Skincare"), ("wellness", "Wellness"), ("laundry", "Laundry"), OTHER_SUBCATEGORY]),
    DefaultCategorySlug.INCOME: ("Income", "wallet", [("salary", "Salary"), ("freelance", "Freelance"), ("interest", "Interest"), OTHER_SUBCATEGORY]),
    DefaultCategorySlug.INVESTMENT: ("Investments", "trending-up", [("mutual_fund", "Mutual fund"), ("stocks", "Stocks"), ("fixed_deposit", "Fixed deposit"), OTHER_SUBCATEGORY]),
    DefaultCategorySlug.OTHER: ("Other", "circle-ellipsis", [OTHER_SUBCATEGORY]),
}


def taxonomy_path(category: DefaultCategorySlug, subcategory: str) -> tuple[str, str]:
    """Return a validated canonical path for deterministic classification rules."""
    known_subcategories = {slug for slug, _name in DEFAULT_TAXONOMY[category][2]}
    if subcategory not in known_subcategories:
        raise ValueError(f"Unknown default taxonomy path: {category}/{subcategory}")
    return category.value, subcategory
