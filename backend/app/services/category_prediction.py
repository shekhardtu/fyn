from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Category
from ..taxonomy_catalog import DefaultCategorySlug
from .extraction import infer_expense_category


@dataclass
class CategoryScore:
    score: float
    reasons: list[str] = field(default_factory=list)


BASE_PRIORS = {
    DefaultCategorySlug.FOOD: 0.30,
    DefaultCategorySlug.TRAVEL: 0.24,
    DefaultCategorySlug.SHOPPING: 0.18,
    DefaultCategorySlug.BILLS: 0.14,
    DefaultCategorySlug.ENTERTAINMENT: 0.12,
    DefaultCategorySlug.HEALTH: 0.10,
    DefaultCategorySlug.OTHER: 0.04,
}


def score_static_signals(
    categories: list[Category],
    raw_text: str,
    amount_minor: int | None,
    local_hour: int,
) -> dict[str, CategoryScore]:
    """Score categories from signals available without any user history.

    This is the cold-start layer. :func:`rank_category_suggestions` turns it
    into a ranked list; :mod:`app.services.recommendation` normalises it into
    the background distribution that learned evidence is smoothed against, so
    a user with no transactions sees exactly these signals and nothing invented.
    """
    scores = {category.slug: CategoryScore(BASE_PRIORS.get(category.slug, 0.08), ["Common starting point"]) for category in categories}
    text_category, _ = infer_expense_category(raw_text)
    if text_category in scores:
        scores[text_category].score += 0.70
        scores[text_category].reasons.insert(0, "Matched your description")

    if 7 <= local_hour <= 10 or 12 <= local_hour <= 14 or 19 <= local_hour <= 22:
        if DefaultCategorySlug.FOOD in scores:
            scores[DefaultCategorySlug.FOOD].score += 0.22
            scores[DefaultCategorySlug.FOOD].reasons.insert(0, "Typical meal time")
    if 7 <= local_hour <= 10 or 17 <= local_hour <= 20:
        if DefaultCategorySlug.TRAVEL in scores:
            scores[DefaultCategorySlug.TRAVEL].score += 0.18
            scores[DefaultCategorySlug.TRAVEL].reasons.insert(0, "Typical commute time")
    if 20 <= local_hour <= 23 and DefaultCategorySlug.ENTERTAINMENT in scores:
        scores[DefaultCategorySlug.ENTERTAINMENT].score += 0.10
        scores[DefaultCategorySlug.ENTERTAINMENT].reasons.insert(0, "Evening purchase time")

    if amount_minor is not None:
        rupees = amount_minor / 100
        if rupees <= 500:
            for slug, boost in ((DefaultCategorySlug.FOOD, 0.16), (DefaultCategorySlug.TRAVEL, 0.12)):
                if slug in scores:
                    scores[slug].score += boost
                    scores[slug].reasons.insert(0, "Common for this amount")
        elif rupees >= 5_000:
            for slug, boost in ((DefaultCategorySlug.SHOPPING, 0.18), (DefaultCategorySlug.BILLS, 0.12)):
                if slug in scores:
                    scores[slug].score += boost
                    scores[slug].reasons.insert(0, "Plausible for this amount")

    return scores


def static_prior_distribution(
    categories: list[Category],
    raw_text: str,
    amount_minor: int | None,
    local_hour: int,
) -> tuple[dict[str, float], dict[str, list[str]]]:
    """Normalise the cold-start signals into a distribution and its reasons.

    The reasons travel with the weights so a cold-start guess can say what
    actually drove it ("Typical meal time") rather than a generic placeholder.
    """
    scores = score_static_signals(categories, raw_text, amount_minor, local_hour)
    reasons = {slug: entry.reasons[:2] for slug, entry in scores.items()}
    total = sum(entry.score for entry in scores.values())
    if total <= 0:
        uniform = 1 / len(scores) if scores else 0.0
        return {slug: uniform for slug in scores}, reasons
    return {slug: entry.score / total for slug, entry in scores.items()}, reasons
