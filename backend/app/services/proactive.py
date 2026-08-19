"""Proactive insights: dated claims about one user's money, each recheckable.

An insight is not a summary. It is a *claim* — "food is running 1.8x its usual
month", "Netflix takes 499 on the 25th" — and a claim that was true when it was
written is not thereby true when it is read. So every insight here is built to
be replayed:

* **Deterministic detectors only.** Three of them, each pure arithmetic over
  canonical transactions and the CDP traits. No model is asked to notice
  anything, so nothing here can hallucinate a pattern that is not in the data.
* **One arithmetic, two callers.** A detector's ``evaluate`` both *finds* the
  claim and *rechecks* it: :func:`generate_insights` runs it over candidate
  keys, :func:`verify_insight` runs the same function over a stored key and
  compares. A drift between "how we found it" and "how we check it" is
  therefore not expressible.
* **The recompute key is the whole input.** Everything ``evaluate`` needs —
  the as-of date, the thresholds, the baseline it was compared against — is in
  the key, in JSON-native types. A key that needed something from outside
  itself would make the claim unreproducible, which is the same as unfalsifiable.
* **Verification on read.** A stored insight is shown only when its claim
  reproduces *now*. One that does not is stamped ``stale_at`` and excluded.

Unlike a CDP trait, a falsified insight is not deleted. A trait is a current
value, so a value with no evidence should not exist; an insight is a dated
claim, and the fact that it stopped holding is itself a record worth keeping —
marked, and never shown as current.

Writes here only flush; the caller's transaction decides when they commit, so
refreshing insights inside a conversation turn cannot commit that turn early.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..domain import TransactionType
from ..event_time import as_utc, local_date, now_utc, utc_range_for_local_dates
from ..models import Category, FinancialInsight, Transaction, User, UserTrait
from .cdp import TRAIT_CATEGORY_BASELINES, TRAIT_INCOME_CADENCE, get_traits
from .currency import format_money_minor, user_currency, user_timezone
from .finance_time import shift_month
from .intelligence import recurring_rows
from .manifest import native_manifest_fingerprint
from .transactions import apply_canonical_transaction_scope, apply_expense_transaction_scope

KIND_CATEGORY_ANOMALY = "category_anomaly"
KIND_UPCOMING_OBLIGATION = "upcoming_obligation"
KIND_INCOME_GAP = "income_gap"

# --- stated thresholds --------------------------------------------------------
# Every number below is a product decision, written once here so an insight can
# never be "past the threshold" without the reader being able to see which one.

# A category has to be half again its own normal month before it is worth a
# word. Ratio alone is not enough: a category whose baseline is small enough to
# be noise doubles constantly, so an absolute floor keeps the trivial quiet.
ANOMALY_RATIO_THRESHOLD = Decimal("1.5")
ANOMALY_MIN_DELTA_MINOR = 50_000
# How far ahead a recurring charge counts as "upcoming". Two weeks is one pay
# cycle's worth of warning for a monthly charge and two for a weekly one.
OBLIGATION_HORIZON_DAYS = 14
# Income rarely lands on the exact median gap. Fire only once the gap has been
# missed by more than this, so an employer paying two days late is not an alarm.
INCOME_GAP_GRACE_DAYS = 3

# `title` is String(180) and merchant names run to 160 on their own, so the
# quoted part of a headline is bounded here — visibly, with an ellipsis, rather
# than by a silent slice of the finished sentence.
HEADLINE_SUBJECT_MAX = 60
INSIGHT_SUBJECT_MAX = 160
# How many insights the compact context line prints before saying how many it
# left out. Truncation is stated, never silent.
CONTEXT_INSIGHT_LIMIT = 5


@dataclass(frozen=True)
class Insight:
    """One generated claim, before it is stored.

    ``evidence`` is the claim itself: ``rows`` are the exact numbers in minor
    units, ``labels`` the display strings the headline quotes. Both are
    verified, because a headline naming last month's merchant spelling is as
    wrong as one naming last month's amount.
    """

    kind: str
    subject: str
    headline: str
    evidence: dict
    lineage: dict
    recompute_key: dict


@dataclass(frozen=True)
class Detector:
    """A deterministic claim finder and its own recheck.

    ``candidates`` proposes recompute keys from the user's current data;
    ``evaluate`` turns one key into a claim, or ``None`` when the claim does not
    hold; ``headline`` renders a claim as one sentence.
    """

    kind: str
    candidates: Callable[[Session, User, date, dict[str, UserTrait]], list[dict]]
    evaluate: Callable[[Session, User, dict], dict | None]
    headline: Callable[[dict, dict], str]


def _row(label: str, value: int, unit: str, *, currency: str | None = None, on: str | None = None) -> dict:
    """One evidence number, exact, with the unit that makes it readable."""
    return {"label": label, "value": int(value), "unit": unit, "currency": currency, "on": on}


def _clip(text: str) -> str:
    """Bound a quoted name for a headline, showing that it was cut."""
    value = (text or "").strip()
    return value if len(value) <= HEADLINE_SUBJECT_MAX else value[: HEADLINE_SUBJECT_MAX - 1] + "…"


# --- category_anomaly ---------------------------------------------------------

def _baseline_means_by_slug(trait: UserTrait | None) -> dict[str, int]:
    """Baseline monthly means keyed by category slug.

    Traits group by ``(slug, name)``, so a renamed category can appear twice
    under one slug. The means are summed rather than picked between: the mean of
    a sum is the sum of the means, and the observed side below sums by slug too,
    so both halves of the comparison cover exactly the same transactions.
    """
    if trait is None:
        return {}
    totals: dict[str, int] = {}
    for entry in trait.value.get("categories") or []:
        slug = str(entry["slug"])
        totals[slug] = totals.get(slug, 0) + int(entry["mean_minor"])
    return totals


def _category_anomaly_candidates(
    db: Session, user: User, today: date, traits: dict[str, UserTrait]
) -> list[dict]:
    """One candidate per category that has a baseline to be measured against.

    Spend in a category with no baseline — a brand-new category, or spend with
    no category at all — is deliberately not a candidate: there is no "usual
    month" to exceed, so any claim about it would be invented rather than
    computed.
    """
    currency = user_currency(db, user.id)
    return [
        {
            "detector": KIND_CATEGORY_ANOMALY,
            "subject": slug,
            "asOf": today.isoformat(),
            "currency": currency,
            "baselineMeanMinor": mean_minor,
            "ratioThreshold": str(ANOMALY_RATIO_THRESHOLD),
            "minDeltaMinor": ANOMALY_MIN_DELTA_MINOR,
        }
        for slug, mean_minor in sorted(
            _baseline_means_by_slug(traits.get(TRAIT_CATEGORY_BASELINES)).items()
        )
    ]


def _category_anomaly_evaluate(db: Session, user: User, key: dict) -> dict | None:
    slug = str(key["subject"])
    currency = str(key["currency"])
    as_of = date.fromisoformat(str(key["asOf"]))
    baseline_minor = int(key["baselineMeanMinor"])
    ratio_threshold = Decimal(str(key["ratioThreshold"]))
    min_delta_minor = int(key["minDeltaMinor"])
    if baseline_minor <= 0:
        # No usual month to exceed; a ratio against zero is not a measurement.
        return None
    start_at, end_at = utc_range_for_local_dates(
        as_of.replace(day=1), as_of, user_timezone(db, user.id)
    )
    # Month-to-date against a *complete*-month mean, with no proration. The
    # comparison is deliberately conservative: a partial month can only fall
    # short of the full-month baseline, so this under-reports early in a month
    # and never over-reports. A prorated version would raise alarms on the 2nd.
    observed_minor = int(
        db.scalar(
            apply_expense_transaction_scope(
                select(func.coalesce(func.sum(Transaction.amount_minor), 0))
                .select_from(Transaction)
                .join(Category, Category.id == Transaction.category_id),
                user.id,
                currency=currency,
            ).where(
                Category.slug == slug,
                Transaction.transaction_at >= start_at,
                Transaction.transaction_at < end_at,
            )
        )
        or 0
    )
    delta_minor = observed_minor - baseline_minor
    over_ratio = Decimal(observed_minor) >= Decimal(baseline_minor) * ratio_threshold
    if not over_ratio or delta_minor < min_delta_minor:
        return None
    return {
        "rows": [
            _row(f"{slug} month to date", observed_minor, "minor", currency=currency, on=as_of.isoformat()),
            _row(f"{slug} usual month", baseline_minor, "minor", currency=currency),
            _row(f"{slug} above usual", delta_minor, "minor", currency=currency, on=as_of.isoformat()),
        ],
        "labels": {"category": slug.replace("-", " ")},
    }


def _category_anomaly_headline(key: dict, claim: dict) -> str:
    observed, baseline, _ = (int(row["value"]) for row in claim["rows"])
    currency = str(key["currency"])
    ratio = (Decimal(observed) / Decimal(baseline)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    label = _clip(str(claim["labels"]["category"]))
    return (
        f"{label.capitalize()} is running {ratio}x its usual month: "
        f"{format_money_minor(observed, currency)} so far against "
        f"{format_money_minor(baseline, currency)}"
    )


# --- upcoming_obligation ------------------------------------------------------

def _obligation_due_date(last_seen: date, cadence: str) -> date:
    """When the next charge of a detected pattern is expected."""
    if cadence == "monthly":
        return shift_month(last_seen, 1)
    if cadence == "weekly":
        return last_seen + timedelta(days=7)
    raise ValueError(f"unknown_recurring_cadence: {cadence}")


def _obligation_candidates(
    db: Session, user: User, today: date, traits: dict[str, UserTrait]
) -> list[dict]:
    """One candidate per recurring pattern the shared detector already found.

    The patterns come from :func:`recurring_rows` — the single recurring
    detector in this codebase, the same one the ``recurring_load`` trait reads —
    so an obligation this module announces can never disagree with the recurring
    list the rest of the product shows.
    """
    return [
        {
            "detector": KIND_UPCOMING_OBLIGATION,
            # Patterns are keyed by merchant *and* amount, so two charges from
            # one shop stay two obligations rather than overwriting each other.
            "subject": f"{pattern['id']}:{int(pattern['amount_minor'])}"[:INSIGHT_SUBJECT_MAX],
            "merchantKey": str(pattern["id"]),
            "amountMinor": int(pattern["amount_minor"]),
            "cadence": str(pattern["cadence"]),
            "currency": str(pattern["currency"]),
            "asOf": today.isoformat(),
            "horizonDays": OBLIGATION_HORIZON_DAYS,
        }
        for pattern in recurring_rows(db, user.id)
    ]


def _obligation_evaluate(db: Session, user: User, key: dict) -> dict | None:
    as_of = date.fromisoformat(str(key["asOf"]))
    horizon_days = int(key["horizonDays"])
    pattern = next(
        (
            item
            for item in recurring_rows(db, user.id)
            if str(item["id"]) == str(key["merchantKey"])
            and int(item["amount_minor"]) == int(key["amountMinor"])
            and str(item["cadence"]) == str(key["cadence"])
        ),
        None,
    )
    if pattern is None:
        # The pattern stopped being recurring — a charge was removed, or the
        # gaps drifted out of band. There is nothing to be due.
        return None
    last_seen = date.fromisoformat(str(pattern["last_date"]))
    due_on = _obligation_due_date(last_seen, str(pattern["cadence"]))
    days_until = (due_on - as_of).days
    if days_until < 0 or days_until > horizon_days:
        return None
    currency = str(pattern["currency"])
    return {
        "rows": [
            _row("expected charge", int(pattern["amount_minor"]), "minor", currency=currency, on=due_on.isoformat()),
            _row("days until due", days_until, "days", on=due_on.isoformat()),
            _row("charges observed", int(pattern["occurrences"]), "count", on=last_seen.isoformat()),
        ],
        "labels": {
            "merchant": str(pattern["merchant"]),
            "cadence": str(pattern["cadence"]),
            "dueOn": due_on.isoformat(),
        },
    }


def _obligation_headline(key: dict, claim: dict) -> str:
    amount, days_until, _ = (int(row["value"]) for row in claim["rows"])
    currency = str(key["currency"])
    merchant = _clip(str(claim["labels"]["merchant"]))
    when = "today" if days_until == 0 else f"in {days_until} day{'' if days_until == 1 else 's'}"
    return (
        f"{merchant} takes {format_money_minor(amount, currency)} "
        f"on {claim['labels']['dueOn']} ({when})"
    )


# --- income_gap ---------------------------------------------------------------

def _income_gap_candidates(
    db: Session, user: User, today: date, traits: dict[str, UserTrait]
) -> list[dict]:
    """A single candidate, and only when the cadence trait says monthly.

    An irregular earner has no expected window to miss, so there is no claim to
    make about them — silence, rather than an alarm calibrated on nothing.
    """
    trait = traits.get(TRAIT_INCOME_CADENCE)
    if trait is None or trait.value.get("cadence") != "monthly":
        return []
    return [
        {
            "detector": KIND_INCOME_GAP,
            "subject": "income",
            "asOf": today.isoformat(),
            "currency": user_currency(db, user.id),
            "medianGapDays": int(trait.value["median_gap_days"]),
            "graceDays": INCOME_GAP_GRACE_DAYS,
        }
    ]


def _income_gap_evaluate(db: Session, user: User, key: dict) -> dict | None:
    as_of = date.fromisoformat(str(key["asOf"]))
    currency = str(key["currency"])
    median_gap_days = int(key["medianGapDays"])
    grace_days = int(key["graceDays"])
    latest = db.execute(
        apply_canonical_transaction_scope(
            select(Transaction.transaction_at, Transaction.amount_minor),
            user.id,
            currency=currency,
        )
        .where(Transaction.transaction_type == TransactionType.INCOME)
        .order_by(Transaction.transaction_at.desc())
        .limit(1)
    ).first()
    if latest is None:
        # No income at all is a different statement from late income, and this
        # detector is not entitled to make it.
        return None
    last_on = local_date(latest[0], user_timezone(db, user.id))
    expected_by = last_on + timedelta(days=median_gap_days)
    days_overdue = (as_of - expected_by).days
    if days_overdue <= grace_days:
        return None
    return {
        "rows": [
            _row("days overdue", days_overdue, "days", on=expected_by.isoformat()),
            _row("usual gap", median_gap_days, "days", on=last_on.isoformat()),
            _row("last income", int(latest[1]), "minor", currency=currency, on=last_on.isoformat()),
        ],
        "labels": {"lastOn": last_on.isoformat(), "expectedBy": expected_by.isoformat()},
    }


def _income_gap_headline(key: dict, claim: dict) -> str:
    days_overdue, _, amount = (int(row["value"]) for row in claim["rows"])
    currency = str(key["currency"])
    return (
        f"Income is {days_overdue} day{'' if days_overdue == 1 else 's'} late: the last "
        f"{format_money_minor(amount, currency)} landed on {claim['labels']['lastOn']}, "
        f"and the monthly cadence expected the next by {claim['labels']['expectedBy']}"
    )


DETECTORS: tuple[Detector, ...] = (
    Detector(
        kind=KIND_CATEGORY_ANOMALY,
        candidates=_category_anomaly_candidates,
        evaluate=_category_anomaly_evaluate,
        headline=_category_anomaly_headline,
    ),
    Detector(
        kind=KIND_UPCOMING_OBLIGATION,
        candidates=_obligation_candidates,
        evaluate=_obligation_evaluate,
        headline=_obligation_headline,
    ),
    Detector(
        kind=KIND_INCOME_GAP,
        candidates=_income_gap_candidates,
        evaluate=_income_gap_evaluate,
        headline=_income_gap_headline,
    ),
)

DETECTORS_BY_KIND: dict[str, Detector] = {item.kind: item for item in DETECTORS}


def _lineage(traits: dict[str, UserTrait]) -> dict:
    """The stamps that say what this insight was derived from, and when.

    ``traitsComputedAt`` is the one snapshot the whole trait set was computed
    from, so it describes every trait-derived detector at once. It is ``None``
    only when the user has no traits at all — an honest "not computed", never a
    borrowed timestamp.
    """
    stamps = [as_utc(trait.computed_at) for trait in traits.values()]
    return {
        "manifestHash": native_manifest_fingerprint(),
        "traitsComputedAt": max(stamps).isoformat() if stamps else None,
        "computedAt": now_utc().isoformat(),
    }


def generate_insights(db: Session, user: User, today: date) -> list[Insight]:
    """Every claim this user's data supports right now, from deterministic detectors.

    Traits are read through :func:`get_traits`, so a stale trait set is
    recomputed before anything is derived from it rather than being quoted as
    current.
    """
    traits = {trait.name: trait for trait in get_traits(db, user, today=today)}
    lineage = _lineage(traits)
    insights: list[Insight] = []
    for detector in DETECTORS:
        for key in detector.candidates(db, user, today, traits):
            claim = detector.evaluate(db, user, key)
            if claim is None:
                continue
            insights.append(
                Insight(
                    kind=detector.kind,
                    subject=str(key["subject"])[:INSIGHT_SUBJECT_MAX],
                    headline=detector.headline(key, claim),
                    evidence=claim,
                    lineage=lineage,
                    recompute_key=key,
                )
            )
    return sorted(insights, key=lambda item: (item.kind, item.subject))


def verify_insight(
    db: Session,
    user: User,
    insight: FinancialInsight | Insight,
    *,
    today: date | None = None,
) -> bool:
    """Whether a claim still reproduces from its own recompute key, today.

    The same ``evaluate`` that found the claim recomputes it and the two are
    compared whole — numbers *and* the strings the headline quotes — so an
    insight passes only when the exact sentence it carries is still true.
    Passing ``today`` additionally requires the claim to be about today: a
    stored key replays its own historical window perfectly, so without that
    check "verified" would only ever mean "was true when written".
    """
    if isinstance(insight, FinancialInsight):
        if insight.user_id != user.id:
            raise ValueError("insight_tenant_mismatch: the insight belongs to another user")
        kind, key, claim = insight.insight_type, insight.recompute_key, insight.evidence
    else:
        kind, key, claim = insight.kind, insight.recompute_key, insight.evidence
    detector = DETECTORS_BY_KIND.get(kind)
    if detector is None:
        raise ValueError(f"unknown_insight_kind: {kind}")
    if str((key or {}).get("detector")) != kind:
        raise ValueError(f"insight_key_mismatch: {kind}")
    if today is not None and str((key or {}).get("asOf") or "") != today.isoformat():
        # Historical data does not change, so replaying a stored key always
        # reproduces its own claim. "Verified" has to mean the claim holds
        # TODAY — otherwise a cancelled subscription's July obligation is
        # still announced as five days away in August.
        return False
    return detector.evaluate(db, user, key) == claim


def store_insights(db: Session, user: User, insights: list[Insight]) -> list[FinancialInsight]:
    """Upsert generated claims, one row per ``(kind, subject)``.

    A restated claim rewrites its row instead of appending: "food is running
    hot" is one insight that gets restamped, not a new one every read.
    """
    existing = {
        (row.insight_type, row.subject): row
        for row in db.scalars(
            select(FinancialInsight).where(FinancialInsight.user_id == user.id)
        )
    }
    stored: list[FinancialInsight] = []
    for insight in insights:
        row = existing.get((insight.kind, insight.subject))
        if row is None:
            row = FinancialInsight(
                user_id=user.id, insight_type=insight.kind, subject=insight.subject
            )
            db.add(row)
        row.title = insight.headline
        row.evidence = insight.evidence
        row.lineage = insight.lineage
        row.recompute_key = insight.recompute_key
        # A claim just recomputed is not stale; a row that was stale and fires
        # again is current once more.
        row.stale_at = None
        stored.append(row)
    db.flush()
    return stored


def current_insights(
    db: Session,
    user: User,
    today: date,
    *,
    checked_at: datetime | None = None,
) -> list[FinancialInsight]:
    """This user's verified insights: regenerate, store, then recheck every row.

    Every returned row was replayed in this call — including the ones just
    written, which costs a second deterministic evaluation and buys the rule
    that nothing is returned unverified, with no exceptions to remember.

    Rows that fail are stamped ``stale_at`` and left in place as history.
    Dismissed rows are skipped without rechecking: the user has said they do not
    want them, so whether they still hold is not a question worth asking.

    ``checked_at`` lets a caller that reports the moment of the pass stamp the
    rows with the very instant it reports, rather than one a hair apart from it.
    """
    store_insights(db, user, generate_insights(db, user, today))
    checked_at = checked_at or now_utc()
    verified: list[FinancialInsight] = []
    for row in db.scalars(
        select(FinancialInsight)
        .where(FinancialInsight.user_id == user.id)
        .order_by(FinancialInsight.insight_type, FinancialInsight.subject)
    ):
        if row.dismissed_at is not None:
            continue
        try:
            holds = verify_insight(db, user, row, today=today)
        except ValueError:
            # An unrecognizable row — a kind that no longer exists, or a key
            # a migration backfilled empty — is unverifiable, which is exactly
            # what stale means. One such row must never take down the page or
            # every chat turn that reads insights.
            holds = False
        if holds:
            row.verified_at = checked_at
            row.stale_at = None
            verified.append(row)
        else:
            row.stale_at = checked_at
    db.flush()
    return verified


def insights_context_line(insights: list[FinancialInsight]) -> str:
    """One compact line of verified insights, each carrying its own stamps.

    Same law as the traits line: the value and the moment it was computed travel
    together, because a claim printed without its date reads as true now.
    """
    shown = insights[:CONTEXT_INSIGHT_LIMIT]
    entries = [
        f"{row.insight_type}: {row.title} "
        f"(computed_at {row.lineage.get('computedAt')}; "
        f"verified_at {as_utc(row.verified_at).isoformat() if row.verified_at else 'never'})"
        for row in shown
    ]
    remainder = len(insights) - len(shown)
    if remainder > 0:
        entries.append(f"+{remainder} more")
    return " | ".join(entries)
