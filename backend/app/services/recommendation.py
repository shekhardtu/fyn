"""Deterministic category and subcategory recommendation.

The engine scores every candidate as a weighted sum of Dirichlet-smoothed,
half-life-decayed conditionals over independent context channels (merchant,
description tokens, location, time of day, amount band), backed off to the
cold-start signals in :mod:`app.services.category_prediction`.

Design notes and the sources behind each choice:

* Exponential per-event decay follows the Firefox Places "frecency" model —
  ``weight * e^(-lambda * age)`` — so recency needs no separate term: a recent
  observation simply weighs more than an old one.
* Sparse counts are smoothed against a background distribution with the
  Dirichlet prior form from Zhai & Lafferty, ``(count + mu * P0) / (total +
  mu)``. ``mu`` is literally how many pseudo-counts of the background to trust
  before the user's own history takes over, which is what makes cold start a
  smooth ramp rather than a mode switch.
* Channels are combined by weighted sum rather than a naive-Bayes product.
  Merchant and location are near-duplicate signals for the same shop, and
  multiplying probabilities double-counts them while blowing up on tokens seen
  once or twice. A linear blend also yields a per-channel contribution, which
  is exactly what each user-facing reason string is rendered from.

Everything here is a pure function of rows already stored on ``transactions``.
There is no model file, no training step and no derived state to fall out of
sync when a user edits or deletes a transaction.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import DEFAULT_TIMEZONE
from ..models import Category, Subcategory, Transaction, TransactionDraft, TransactionFieldValue, User
from ..event_time import local_date, local_now, local_time, utc_range_for_local_dates
from .currency import user_timezone
from .category_prediction import static_prior_distribution
from .extraction import normalize_merchant

# Spending is cyclical: rent and bills repeat monthly, groceries weekly. A
# 90-day half-life spans roughly three monthly cycles, so a habit stays
# influential for a quarter while a one-off fades. Firefox uses 30 days for
# browsing; half-life decay work on ratings finds ~150 days optimal for slower
# preference drift. Spending sits between the two.
HALF_LIFE_DAYS = 90.0
DECAY_LAMBDA = math.log(2) / HALF_LIFE_DAYS

# Beyond a year the decayed weight is under 6% of a fresh observation, so the
# rows cost more to scan than they contribute.
EVIDENCE_WINDOW_DAYS = 365

# Pseudo-counts of the background distribution. MU_CHANNEL is small because a
# per-context slice holds only a handful of observations: at 3, a channel needs
# ~3 consistent observations before it outvotes the prior. MU_GLOBAL is larger
# because the global slice is the whole history.
MU_CHANNEL = 3.0
MU_GLOBAL = 8.0

MERCHANT = "merchant"
PLACE = "place"
TOKEN = "token"
TIME = "time"
AMOUNT = "amount"
AREA = "area"
PRIOR = "prior"

CHANNEL_WEIGHTS = {
    MERCHANT: 0.32,
    PLACE: 0.17,
    TOKEN: 0.17,
    TIME: 0.10,
    AMOUNT: 0.08,
    AREA: 0.06,
    PRIOR: 0.10,
}

# Auto-apply only when the leader is clear, well ahead of the runner-up, and
# supported by something specific to this transaction rather than by the
# background prior. Mirrors the calibration split reported for SME transaction
# categorisation, where overall accuracy was mediocre but the high-confidence
# subset was reliable.
AUTO_APPLY_CONFIDENCE = 0.62
AUTO_APPLY_MARGIN = 0.18
# Below MU_CHANNEL pseudo-counts the background prior is still outvoting the
# user's own evidence, so a single sighting must never auto-apply.
AUTO_APPLY_MIN_SUPPORT = 3

# Firefox weights a visit by how deliberate it was (typed beats followed-link
# beats redirect) before decaying it. The same applies here: a category the
# user typed or corrected by hand states an intent, while a category they
# merely accepted only failed to bother them. One correction is therefore
# worth several passive observations, and is enough to auto-apply on its own.
# Set to twice MU_CHANNEL deliberately: at exactly MU_CHANNEL a single
# correction only draws level with the background prior, leaving the user's
# stated intent to lose coin flips. At 2x it takes roughly two-thirds of the
# channel's smoothed mass — decisive now, still overturnable by a few contrary
# observations later, and still subject to decay.
CONFIRMED_WEIGHT = 6.0
OBSERVED_WEIGHT = 1.0

# geohash-7 cells are ~150m across (this shop); geohash-5 ~5km (this area).
PLACE_PRECISION = 7
AREA_PRECISION = 5
# A coarse fix cannot distinguish neighbouring shops, so it may only feed the
# area channel. Beyond this it cannot even place a neighbourhood.
PLACE_ACCURACY_LIMIT_M = 250
AREA_ACCURACY_LIMIT_M = 5_000

_GEOHASH_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_TOKEN_PATTERN = re.compile(r"[a-z][a-z0-9]{2,}")
_TOKEN_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "was", "were", "this", "that", "have",
    "has", "had", "paid", "pay", "spent", "spend", "bought", "buy", "got",
    "rupees", "rupee", "inr", "rs", "amount", "today", "yesterday", "morning",
    "afternoon", "evening", "night", "some", "few", "just", "about", "around",
    "expense", "txn", "transaction", "debited", "credited", "purchase",
})

_TIME_BUCKET_LABELS = {
    0: "late night",
    1: "early morning",
    2: "morning",
    3: "afternoon",
    4: "evening",
    5: "night",
}


def geohash_encode(latitude: float, longitude: float, precision: int) -> str:
    """Encode a coordinate as a geohash of the requested precision."""
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    bits = (16, 8, 4, 2, 1)
    bit = 0
    char = 0
    even = True
    out: list[str] = []
    while len(out) < precision:
        if even:
            mid = (lon_range[0] + lon_range[1]) / 2
            if longitude > mid:
                char |= bits[bit]
                lon_range[0] = mid
            else:
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if latitude > mid:
                char |= bits[bit]
                lat_range[0] = mid
            else:
                lat_range[1] = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(_GEOHASH_BASE32[char])
            bit = 0
            char = 0
    return "".join(out)


def tokenize(*parts: str | None) -> list[str]:
    """Split free text into de-duplicated, meaning-bearing lowercase tokens."""
    seen: list[str] = []
    for part in parts:
        if not part:
            continue
        for token in _TOKEN_PATTERN.findall(part.lower()):
            if token in _TOKEN_STOPWORDS or token in seen:
                continue
            seen.append(token)
    return seen


def amount_band(amount_minor: int | None) -> str | None:
    """Bucket an amount on a log2 scale, learned rather than hardcoded."""
    if amount_minor is None or amount_minor <= 0:
        return None
    return f"b{int(math.log2(max(amount_minor / 100, 1.0)))}"


def _band_range(band: str) -> tuple[int, int]:
    exponent = int(band[1:])
    return 2**exponent, 2 ** (exponent + 1)


def time_bucket(hour: int | None, day: date | None) -> str | None:
    """Bucket a timestamp into a 4-hour band crossed with weekday/weekend."""
    if hour is None:
        return None
    part_of_week = "we" if day is not None and day.weekday() >= 5 else "wd"
    return f"{hour // 4}:{part_of_week}"


def _parse_hour(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.split(":")[0])
    except (ValueError, IndexError):
        return None


@dataclass
class Observation:
    """Decayed votes from past transactions, kept alongside honest counts.

    ``weight`` drives ranking and is both decayed and weighted by how
    deliberate each choice was. ``count`` stays a plain tally of transactions
    so reason strings can quote a number the user can actually verify.
    """

    weight: float
    count: int = 1
    confirmed: int = 0


@dataclass
class Tally:
    """Decayed evidence for one context key, split by candidate."""

    by_candidate: dict[str, Observation] = field(default_factory=dict)
    total_weight: float = 0.0
    total_count: int = 0

    def add(self, candidate: str, weight: float, *, confirmed: bool = False) -> None:
        entry = self.by_candidate.get(candidate)
        if entry is None:
            self.by_candidate[candidate] = Observation(weight, confirmed=int(confirmed))
        else:
            entry.weight += weight
            entry.count += 1
            entry.confirmed += int(confirmed)
        self.total_weight += weight
        self.total_count += 1


@dataclass
class Context:
    """The context keys derivable from the transaction being categorised."""

    merchant: str | None = None
    tokens: list[str] = field(default_factory=list)
    place: str | None = None
    area: str | None = None
    time: str | None = None
    amount: str | None = None
    merchant_display: str | None = None
    amount_minor: int | None = None


@dataclass
class Contribution:
    """One channel's contribution to a candidate's score, kept for reasons."""

    channel: str
    weight: float
    probability: float
    hits: int
    total: int
    key: str | None = None
    confirmed: int = 0

    @property
    def value(self) -> float:
        return self.weight * self.probability


@dataclass
class Suggestion:
    """A ranked candidate with the evidence that produced it."""

    id: str
    slug: str
    label: str
    icon: str | None
    score: float
    reasons: list[str]
    evidence_backed: bool
    dominant_channel: str
    support: int = 0
    confirmed_support: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "label": self.label,
            "icon": self.icon,
            "score": round(self.score, 3),
            "reasons": self.reasons,
        }


class EvidenceLedger:
    """Decayed, per-channel evidence read straight off ``transactions``."""

    def __init__(self, reference: date, timezone_name: str = DEFAULT_TIMEZONE) -> None:
        self.reference = reference
        self.timezone_name = timezone_name
        self.category_channels: dict[str, dict[str, Tally]] = defaultdict(lambda: defaultdict(Tally))
        # Subcategory evidence is conditioned on its parent category, so the
        # same channel key can serve a different subcategory per category.
        self.subcategory_channels: dict[str, dict[tuple[str, str], Tally]] = defaultdict(lambda: defaultdict(Tally))
        self.category_totals = Tally()
        self.subcategory_totals: dict[str, Tally] = defaultdict(Tally)
        self.last_used: dict[str, date] = {}

    def decay(self, when: date) -> float:
        age = max((self.reference - when).days, 0)
        return math.exp(-DECAY_LAMBDA * age)

    def record(self, transaction: Transaction, *, confirmed: bool = False) -> None:
        if not transaction.category_id:
            return
        category = str(transaction.category_id)
        subcategory = str(transaction.subcategory_id) if transaction.subcategory_id else None
        day = local_date(transaction.transaction_at, self.timezone_name)
        weight = self.decay(day) * (CONFIRMED_WEIGHT if confirmed else OBSERVED_WEIGHT)

        self.category_totals.add(category, weight, confirmed=confirmed)
        if subcategory:
            self.subcategory_totals[category].add(subcategory, weight, confirmed=confirmed)
        previous = self.last_used.get(category)
        if previous is None or day > previous:
            self.last_used[category] = day

        for channel, key in self._context_keys(transaction):
            self.category_channels[channel][key].add(category, weight, confirmed=confirmed)
            if subcategory:
                self.subcategory_channels[channel][(category, key)].add(subcategory, weight, confirmed=confirmed)

    def _context_keys(self, transaction: Transaction) -> list[tuple[str, str]]:
        keys: list[tuple[str, str]] = []
        merchant = normalize_merchant(transaction.merchant_name)
        if merchant:
            keys.append((MERCHANT, merchant))
        for token in tokenize(transaction.description, transaction.merchant_name, transaction.notes):
            keys.append((TOKEN, token))
        place, area = _location_keys(
            transaction.latitude,
            transaction.longitude,
            transaction.location_accuracy,
        )
        if place:
            keys.append((PLACE, place))
        if area:
            keys.append((AREA, area))
        bucket = time_bucket(local_time(transaction.transaction_at, self.timezone_name).hour, local_date(transaction.transaction_at, self.timezone_name))
        if bucket:
            keys.append((TIME, bucket))
        band = amount_band(transaction.amount_minor)
        if band:
            keys.append((AMOUNT, band))
        return keys

    @property
    def is_empty(self) -> bool:
        return self.category_totals.total_count == 0


def _location_keys(latitude, longitude, accuracy: int | None) -> tuple[str | None, str | None]:
    if latitude is None or longitude is None:
        return None, None
    if accuracy is not None and accuracy > AREA_ACCURACY_LIMIT_M:
        return None, None
    lat = float(latitude)
    lon = float(longitude)
    area = geohash_encode(lat, lon, AREA_PRECISION)
    if accuracy is not None and accuracy > PLACE_ACCURACY_LIMIT_M:
        return None, area
    return geohash_encode(lat, lon, PLACE_PRECISION), area


def load_ledger(db: Session, user_id: UUID, *, reference: date) -> EvidenceLedger:
    """Load one user's decayed evidence within the lookback window."""
    timezone_name = user_timezone(db, user_id)
    ledger = EvidenceLedger(reference, timezone_name)
    window_start = date.fromordinal(max(reference.toordinal() - EVIDENCE_WINDOW_DAYS, 1))
    start_at, end_at = utc_range_for_local_dates(window_start, reference, timezone_name)
    rows = list(db.scalars(
        select(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.deleted_at.is_(None),
            Transaction.category_id.is_not(None),
            Transaction.transaction_at >= start_at,
            Transaction.transaction_at < end_at,
        )
    ))
    confirmed = set(db.scalars(
        select(TransactionFieldValue.transaction_id).where(
            TransactionFieldValue.transaction_id.in_([row.id for row in rows]),
            # The commit path records "category"; the saved-transaction edit
            # path records "category_id". Both mean the user chose it.
            TransactionFieldValue.field_name.in_(
                ("category", "subcategory", "category_id", "subcategory_id")
            ),
            TransactionFieldValue.user_confirmed.is_(True),
        )
    )) if rows else set()
    for row in rows:
        ledger.record(row, confirmed=row.id in confirmed)
    return ledger


def context_from_draft(draft: TransactionDraft, *, local_now: datetime) -> Context:
    """Derive the context keys for the draft being categorised."""
    merchant = normalize_merchant(draft.merchant_name)
    place, area = _location_keys(draft.latitude, draft.longitude, draft.location_accuracy)
    timezone_name = getattr(local_now.tzinfo, "key", None)
    day = local_date(draft.transaction_at, timezone_name)
    hour = local_time(draft.transaction_at, timezone_name).hour
    return Context(
        merchant=merchant,
        tokens=tokenize(draft.raw_text, draft.merchant_name, draft.description),
        place=place,
        area=area,
        time=time_bucket(hour, day),
        amount=amount_band(draft.amount_minor),
        merchant_display=draft.merchant_name,
        amount_minor=draft.amount_minor,
    )


def _smoothed(tally: Tally, candidate: str, background: float, mu: float) -> float:
    observed = tally.by_candidate.get(candidate)
    weight = observed.weight if observed else 0.0
    return (weight + mu * background) / (tally.total_weight + mu)


def _token_probability(
    channel: dict[str, Tally],
    tokens: list[str],
    candidate: str,
    background: float,
) -> tuple[float, Contribution | None]:
    """Blend per-token conditionals, weighting tokens by how often they're seen.

    A token the user has written 20 times carries more about their habits than
    one written once, so the blend is weighted by each token's own evidence.
    """
    numerator = 0.0
    denominator = 0.0
    best: Contribution | None = None
    for token in tokens:
        tally = channel.get(token)
        if tally is None or tally.total_weight <= 0:
            continue
        probability = _smoothed(tally, candidate, background, MU_CHANNEL)
        numerator += probability * tally.total_weight
        denominator += tally.total_weight
        observed = tally.by_candidate.get(candidate)
        hits = observed.count if observed else 0
        if best is None or probability > best.probability:
            best = Contribution(
                TOKEN, CHANNEL_WEIGHTS[TOKEN], probability, hits, tally.total_count, token,
                observed.confirmed if observed else 0,
            )
    if denominator <= 0:
        return 0.0, None
    blended = numerator / denominator
    if best is not None:
        best.probability = blended
    return blended, best


def _score_candidates(
    ledger_channels: dict[str, dict],
    totals: Tally,
    context: Context,
    candidates: dict[str, str],
    background: dict[str, float],
    *,
    key_of=lambda channel, key: key,
) -> dict[str, list[Contribution]]:
    """Score every candidate, returning each one's per-channel contributions."""
    single_keys = {
        MERCHANT: context.merchant,
        PLACE: context.place,
        AREA: context.area,
        TIME: context.time,
        AMOUNT: context.amount,
    }

    active: dict[str, Tally] = {}
    for channel, key in single_keys.items():
        if key is None:
            continue
        tally = ledger_channels.get(channel, {}).get(key_of(channel, key))
        if tally is not None and tally.total_weight > 0:
            active[channel] = tally

    token_channel = {
        token: tally
        for token in context.tokens
        if (tally := ledger_channels.get(TOKEN, {}).get(key_of(TOKEN, token))) is not None and tally.total_weight > 0
    }

    weights = {channel: CHANNEL_WEIGHTS[channel] for channel in active}
    if token_channel:
        weights[TOKEN] = CHANNEL_WEIGHTS[TOKEN]
    weights[PRIOR] = CHANNEL_WEIGHTS[PRIOR]
    # Renormalise over the channels that actually have evidence so an absent
    # GPS fix does not silently hand its weight to the background prior.
    scale = sum(weights.values())

    breakdown: dict[str, list[Contribution]] = {}
    for candidate_id in candidates:
        prior = background.get(candidate_id, 0.0)
        contributions: list[Contribution] = []
        for channel, tally in active.items():
            probability = _smoothed(tally, candidate_id, prior, MU_CHANNEL)
            observed = tally.by_candidate.get(candidate_id)
            contributions.append(Contribution(
                channel,
                weights[channel] / scale,
                probability,
                observed.count if observed else 0,
                tally.total_count,
                single_keys[channel],
                observed.confirmed if observed else 0,
            ))
        if token_channel:
            _, contribution = _token_probability(token_channel, context.tokens, candidate_id, prior)
            if contribution is not None:
                contribution.weight = weights[TOKEN] / scale
                contributions.append(contribution)
        observed = totals.by_candidate.get(candidate_id)
        contributions.append(Contribution(
            PRIOR,
            weights[PRIOR] / scale,
            prior,
            observed.count if observed else 0,
            totals.total_count,
        ))
        breakdown[candidate_id] = contributions
    return breakdown


def _global_background(totals: Tally, candidates: dict[str, str], static: dict[str, float]) -> dict[str, float]:
    """Blend the user's own decayed category share with the cold-start prior."""
    return {
        candidate_id: (
            (observed.weight if (observed := totals.by_candidate.get(candidate_id)) else 0.0)
            + MU_GLOBAL * static.get(candidate_id, 0.0)
        ) / (totals.total_weight + MU_GLOBAL)
        for candidate_id in candidates
    }


def _describe_place(hits: int, total: int, label: str) -> str:
    return f"{label} here {hits} of {total} times" if total > 1 else f"{label} here last time"


def _reasons(
    contributions: list[Contribution],
    label: str,
    context: Context,
    ledger: EvidenceLedger,
    candidate_id: str,
    prior_rank: int | None,
    static_reasons: list[str] | None = None,
) -> list[str]:
    """Render reason strings from the arithmetic that actually produced the score.

    Every string here is backed by a contribution that moved the score, with
    real observation counts, so the UI never claims evidence that does not exist.
    """
    ordered = sorted(contributions, key=lambda item: (-item.value, item.channel))
    reasons: list[str] = []
    for contribution in ordered:
        if len(reasons) >= 2:
            break
        if contribution.channel != PRIOR and contribution.hits == 0:
            continue
        hits, total = contribution.hits, contribution.total
        if contribution.channel == MERCHANT:
            name = context.merchant_display or contribution.key
            if contribution.confirmed and hits == 1:
                reasons.append(f"You set {name} → {label}")
            elif total > 1:
                reasons.append(f"{name} → {label} {hits} of {total} times")
            else:
                reasons.append(f"{name} → {label} last time")
        elif contribution.channel == PLACE:
            reasons.append(_describe_place(hits, total, label))
        elif contribution.channel == AREA:
            reasons.append(f"Usual around this area ({hits}×)")
        elif contribution.channel == TOKEN and contribution.key:
            # A token lifted straight out of the merchant name restates the
            # merchant reason, so it would spend the second line saying
            # nothing new. Let a different channel have the slot instead.
            if context.merchant and contribution.key in context.merchant.split():
                continue
            reasons.append(f"“{contribution.key}” → {label} {hits} of {total} times")
        elif contribution.channel == TIME and contribution.key:
            bucket = _TIME_BUCKET_LABELS.get(int(contribution.key.split(":")[0]), "this time")
            weekend = contribution.key.endswith("we")
            reasons.append(f"{hits} of {total} {'weekend' if weekend else 'weekday'} {bucket} entries")
        elif contribution.channel == AMOUNT and contribution.key:
            low, high = _band_range(contribution.key)
            reasons.append(f"{hits} of {total} entries between ₹{low:,} and ₹{high:,}")
        elif contribution.channel == PRIOR:
            # With no history the cold-start signals are the whole story, so
            # say which one fired instead of a placeholder.
            if ledger.is_empty:
                reasons.extend(static_reasons or ["Common starting point"])
            elif prior_rank:
                reasons.append(f"Your #{prior_rank} category overall")

    last = ledger.last_used.get(candidate_id)
    if last is not None and len(reasons) < 2:
        days = (ledger.reference - last).days
        if days <= 0:
            reasons.append("Last used today")
        elif days <= 7:
            reasons.append(f"Last used {days} day{'s' if days > 1 else ''} ago")
    return reasons[:2]


def _assemble(
    breakdown: dict[str, list[Contribution]],
    ledger: EvidenceLedger,
    context: Context,
    entities: dict[str, tuple[str, str, str | None]],
    prior_ranks: dict[str, int],
    static_reasons: dict[str, list[str]] | None = None,
) -> list[Suggestion]:
    static_reasons = static_reasons or {}
    suggestions: list[Suggestion] = []
    for candidate_id, contributions in breakdown.items():
        slug, label, icon = entities[candidate_id]
        score = sum(item.value for item in contributions)
        evidenced = [item for item in contributions if item.channel != PRIOR and item.hits > 0]
        dominant = max(contributions, key=lambda item: (item.value, item.channel))
        suggestions.append(Suggestion(
            id=candidate_id,
            slug=slug,
            label=label,
            icon=icon,
            score=score,
            reasons=_reasons(contributions, label, context, ledger, candidate_id, prior_ranks.get(candidate_id), static_reasons.get(candidate_id)),
            evidence_backed=bool(evidenced),
            dominant_channel=dominant.channel,
            support=max((item.hits for item in evidenced), default=0),
            confirmed_support=max((item.confirmed for item in evidenced), default=0),
        ))
    return suggestions


def _select_slots(suggestions: list[Suggestion], ledger: EvidenceLedger, limit: int = 3) -> list[Suggestion]:
    """Fill each slot from a different question rather than one ranking.

    Ranking purely by score fills slots 2 and 3 with near-ties from the same
    evidence, which reads as three guesses while carrying one guess worth of
    information. Instead: the leader, the best competing *explanation*, and
    what the user has been reaching for lately.
    """
    if not suggestions:
        return []
    ranked = sorted(suggestions, key=lambda item: (-item.score, item.label))

    # If anything is backed by real evidence, prior-only candidates are noise.
    backed = [item for item in ranked if item.evidence_backed]
    pool = backed or ranked

    chosen = [pool[0]]
    if len(pool) > 1:
        alternative = next(
            (item for item in pool[1:] if item.dominant_channel != chosen[0].dominant_channel),
            None,
        )
        if alternative is not None:
            chosen.append(alternative)

    if len(chosen) < limit:
        picked = {item.id for item in chosen}
        recent = [
            item for item in pool
            if item.id not in picked and item.evidence_backed and item.id in ledger.last_used
        ]
        recent.sort(key=lambda item: (ledger.last_used[item.id], item.score), reverse=True)
        if recent:
            chosen.append(recent[0])

    picked = {item.id for item in chosen}
    for item in pool:
        if len(chosen) >= limit:
            break
        if item.id not in picked:
            chosen.append(item)
            picked.add(item.id)
    return chosen[:limit]


def _local_now(user: User | None) -> datetime:
    return local_now(user.timezone if user else DEFAULT_TIMEZONE)


@dataclass
class Recommendation:
    """Ranked suggestions plus whether the leader is safe to apply unasked.

    ``suggestions`` holds the slots shown to the user; ``population`` holds
    every scored candidate. Confidence is always measured against the full
    population, because dropping weak candidates from the display must never
    make the survivor look more certain than the evidence says it is.
    """

    suggestions: list[Suggestion]
    population: list[Suggestion] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.population:
            self.population = list(self.suggestions)

    @property
    def top(self) -> Suggestion | None:
        return self.suggestions[0] if self.suggestions else None

    @property
    def confidence(self) -> float:
        total = sum(item.score for item in self.population)
        if total <= 0 or not self.population:
            return 0.0
        return max(item.score for item in self.population) / total

    @property
    def is_confident(self) -> bool:
        leader = self.top
        if leader is None or not leader.evidence_backed:
            return False
        # One deliberate correction is a stated intent and stands on its own;
        # passively accepted categorizations have to accumulate first.
        if leader.confirmed_support < 1 and leader.support < AUTO_APPLY_MIN_SUPPORT:
            return False
        total = sum(item.score for item in self.population)
        if total <= 0:
            return False
        ranked = sorted((item.score for item in self.population), reverse=True)
        margin = (ranked[0] - ranked[1]) / total if len(ranked) > 1 else ranked[0] / total
        return self.confidence >= AUTO_APPLY_CONFIDENCE and margin >= AUTO_APPLY_MARGIN

    def as_dicts(self) -> list[dict]:
        return [item.as_dict() for item in self.suggestions]


def recommend_categories(
    db: Session,
    user: User | None,
    draft: TransactionDraft,
    categories: list[Category],
    *,
    ledger: EvidenceLedger | None = None,
    limit: int = 3,
    now: datetime | None = None,
) -> Recommendation:
    """Rank categories for a draft from the user's own decayed history."""
    if not categories:
        return Recommendation([])
    now = now or _local_now(user)
    ledger = ledger if ledger is not None else load_ledger(db, draft.user_id, reference=now.date())
    context = context_from_draft(draft, local_now=now)

    entities = {str(category.id): (category.slug, category.name, category.icon) for category in categories}
    by_slug, reasons_by_slug = static_prior_distribution(categories, draft.raw_text, draft.amount_minor, now.hour)
    static = {str(category.id): by_slug.get(category.slug, 0.0) for category in categories}
    static_reasons = {str(category.id): reasons_by_slug.get(category.slug, []) for category in categories}
    background = _global_background(ledger.category_totals, entities, static)

    ranked_by_use = sorted(
        entities,
        key=lambda candidate_id: -(
            observed.weight if (observed := ledger.category_totals.by_candidate.get(candidate_id)) else 0.0
        ),
    )
    prior_ranks = {candidate_id: index + 1 for index, candidate_id in enumerate(ranked_by_use)}

    breakdown = _score_candidates(ledger.category_channels, ledger.category_totals, context, entities, background)
    suggestions = _assemble(breakdown, ledger, context, entities, prior_ranks, static_reasons)
    return Recommendation(_select_slots(suggestions, ledger, limit), suggestions)


def recommend_subcategories(
    db: Session,
    user: User | None,
    draft: TransactionDraft,
    category: Category,
    subcategories: list[Subcategory],
    *,
    ledger: EvidenceLedger | None = None,
    limit: int = 3,
    now: datetime | None = None,
) -> Recommendation:
    """Rank subcategories within an already-chosen category.

    Uses the same channels, conditioned on the parent category, and backs off
    to the category's own subcategory marginal before uniform — the
    Jelinek-Mercer style interpolation that keeps a rarely-used subcategory
    from being crowded out by a smoothing constant.
    """
    if not subcategories:
        return Recommendation([])
    now = now or _local_now(user)
    ledger = ledger if ledger is not None else load_ledger(db, draft.user_id, reference=now.date())
    context = context_from_draft(draft, local_now=now)

    category_id = str(category.id)
    entities = {str(item.id): (item.slug, item.name, None) for item in subcategories}
    totals = ledger.subcategory_totals.get(category_id, Tally())
    uniform = 1 / len(entities)
    background = {
        candidate_id: (
            (observed.weight if (observed := totals.by_candidate.get(candidate_id)) else 0.0)
            + MU_GLOBAL * uniform
        ) / (totals.total_weight + MU_GLOBAL)
        for candidate_id in entities
    }

    breakdown = _score_candidates(
        ledger.subcategory_channels,
        totals,
        context,
        entities,
        background,
        key_of=lambda channel, key: (category_id, key),
    )
    suggestions = _assemble(breakdown, ledger, context, entities, {})
    ranked = sorted(suggestions, key=lambda item: (-item.score, item.label))
    backed = [item for item in ranked if item.evidence_backed]
    return Recommendation((backed or ranked)[:limit], suggestions)
