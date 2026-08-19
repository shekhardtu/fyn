from sqlalchemy import select

from app.models import Category
from app.services.category_prediction import score_static_signals, static_prior_distribution


def expense_categories(db):
    return list(db.scalars(select(Category).where(Category.slug.not_in(("income", "investment")))))


def ranked_slugs(scores):
    return [slug for slug, _ in sorted(scores.items(), key=lambda item: -item[1].score)]


def test_small_mealtime_amount_ranks_food_first_with_reasons(db):
    scores = score_static_signals(expense_categories(db), "₹200", 20_000, local_hour=13)

    assert ranked_slugs(scores)[0] == "food"
    assert "Typical meal time" in scores["food"].reasons
    # Nothing here has access to a location, so nothing may imply one.
    assert all("location" not in reason.lower() for entry in scores.values() for reason in entry.reasons)


def test_description_signal_outranks_time_prior(db):
    scores = score_static_signals(expense_categories(db), "₹800 for travelling", 80_000, local_hour=13)

    assert ranked_slugs(scores)[0] == "travel"
    assert scores["travel"].reasons[0] == "Matched your description"


def test_prior_distribution_is_normalised_and_carries_its_reasons(db):
    categories = expense_categories(db)

    weights, reasons = static_prior_distribution(categories, "₹200 lunch", 20_000, local_hour=13)

    assert set(weights) == {category.slug for category in categories}
    assert abs(sum(weights.values()) - 1.0) < 1e-9
    assert max(weights, key=weights.get) == "food"
    # The reasons must survive normalisation so a cold-start card can explain itself.
    assert reasons["food"]
