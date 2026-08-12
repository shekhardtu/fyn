from sqlalchemy import select

from app.models import Category
from app.services.category_prediction import rank_category_suggestions


def test_small_mealtime_amount_ranks_food_first_with_reasons(db):
    categories = list(db.scalars(select(Category).where(Category.slug.not_in(("income", "investment")))))

    suggestions = rank_category_suggestions(categories, "₹200", 20_000, local_hour=13)

    assert len(suggestions) == 3
    assert suggestions[0]["slug"] == "food"
    assert "Typical meal time" in suggestions[0]["reasons"]
    assert all("location" not in reason.lower() for item in suggestions for reason in item["reasons"])


def test_description_signal_outranks_time_prior(db):
    categories = list(db.scalars(select(Category).where(Category.slug.not_in(("income", "investment")))))

    suggestions = rank_category_suggestions(categories, "₹800 for travelling", 80_000, local_hour=13)

    assert suggestions[0]["slug"] == "transport"
    assert suggestions[0]["reasons"][0] == "Matched your description"
