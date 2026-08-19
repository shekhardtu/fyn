"""Identity resolution: one canonical spelling per counterparty, per user.

The same shop reaches a person's records under several names — ``BLUE TOKAI
COFFEE``, ``Blue Tokai``, ``blue tokai online`` — spread across the canonical
ledger, uploaded spreadsheets, and connected external databases. This module
collapses those spellings into ``EntityLink`` rows so downstream lanes can ask
about a merchant once instead of guessing at variants.

Three laws govern it:

* Tenancy: every sighting is read through an owner-scoped query, and links are
  written with the reading user's id. One person's spellings never inform
  another's resolution, and never become a shared dictionary.
* Derivation: links are a function of the data as it stands. A re-run upserts
  in place and removes links whose spelling no longer appears anywhere, so the
  table can never accumulate identities the evidence no longer supports.
* Loud failure: an unreachable external source aborts resolution with a stable
  code rather than returning a partial map that reads as complete. The code
  carries only the driver exception's class name — driver messages routinely
  echo the connection string, which the privacy law keeps out of every surface.

Confidence is inference, so it is bounded strictly below 1: only a user's own
statement is certain, and this module never produces one.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..domain import EntityLinkKind
from ..models import EntityLink, SourceRecord, Transaction, User
from .external_db import column_value_counts, owned_external_sources
from .external_db import _column_role as _external_column_role
from .extraction import normalize_merchant
from .spreadsheet import _column_role as _spreadsheet_column_role
from .spreadsheet import owned_spreadsheets
from .transactions import apply_canonical_transaction_scope

MERCHANT_ROLE = "merchant"
NAME_MAX = 160
# Bounded read per external column: resolution wants the spellings a source
# actually uses, not an unbounded dump of a remote table.
MAX_EXTERNAL_VALUES = 250

# A single sighting is a weak claim; repetition is what makes a spelling real.
# Never 1.0 — an inferred link is not a user statement.
MIN_LINK_CONFIDENCE = Decimal("0.500")
MAX_LINK_CONFIDENCE = Decimal("0.950")
LINK_CONFIDENCE_STEP = Decimal("0.050")


def link_confidence(support: int) -> Decimal:
    """Confidence for a spelling seen ``support`` times, bounded below 1."""
    steps = max(0, int(support) - 1)
    return min(MAX_LINK_CONFIDENCE, MIN_LINK_CONFIDENCE + LINK_CONFIDENCE_STEP * steps)


def _ledger_sightings(db: Session, user_id: UUID) -> list[tuple[str, int, UUID | None]]:
    """Merchant spellings from this user's active canonical transactions."""
    rows = db.execute(
        apply_canonical_transaction_scope(
            select(Transaction.merchant_name, func.count()), user_id
        )
        .where(Transaction.merchant_name.is_not(None))
        .group_by(Transaction.merchant_name)
    ).all()
    return [(str(name), int(count), None) for name, count in rows if str(name).strip()]


def _spreadsheet_sightings(db: Session, user_id: UUID) -> list[tuple[str, int, UUID | None]]:
    """Merchant spellings from columns whose *effective* role is merchant.

    Effective means the provenance law is applied: a user's stated role wins
    over the profiler's inference, so correcting a column immediately changes
    what resolution reads.
    """
    sightings: list[tuple[str, int, UUID | None]] = []
    for source, manifest in owned_spreadsheets(db, user_id):
        fields = [
            column["name"]
            for column in manifest.document["physical"]["columns"]
            if _spreadsheet_column_role(manifest, column["name"]) == MERCHANT_ROLE
        ]
        if not fields:
            continue
        counts: Counter[str] = Counter()
        for row in db.scalars(
            select(SourceRecord).where(
                SourceRecord.data_source_id == source.id,
                SourceRecord.user_id == user_id,
            )
        ):
            for field in fields:
                value = str(row.record.get(field, "")).strip()
                if value:
                    counts[value] += 1
        sightings.extend((value, count, source.id) for value, count in counts.items())
    return sightings


def _external_sightings(
    db: Session, user_id: UUID, truncated: set[UUID] | None = None
) -> list[tuple[str, int, UUID | None]]:
    """Merchant spellings from connected external databases, same role rule.

    A source whose value window filled to the cap is recorded in ``truncated``:
    the read saw only part of its vocabulary, so downstream code must not treat
    an absent spelling from that source as evidence of absence.
    """
    sightings: list[tuple[str, int, UUID | None]] = []
    for source, manifest in owned_external_sources(db, user_id):
        for table_name, section in manifest.document["physical"]["tables"].items():
            for column in section["columns"]:
                name = column["name"]
                if _external_column_role(manifest, table_name, name) != MERCHANT_ROLE:
                    continue
                try:
                    counted = column_value_counts(
                        source, manifest, table_name, name, limit=MAX_EXTERNAL_VALUES
                    )
                except SQLAlchemyError as error:
                    # `from None` and the class-name-only detail keep the
                    # driver's message — which can echo the connection url —
                    # out of the raised error and its traceback alike.
                    raise ValueError(
                        f"external_source_unavailable: {type(error).__name__}"
                    ) from None
                if truncated is not None and len(counted) >= MAX_EXTERNAL_VALUES:
                    truncated.add(source.id)
                sightings.extend(
                    (value, count, source.id) for value, count in counted if value.strip()
                )
    return sightings


def resolve_merchants(db: Session, user: User) -> list[EntityLink]:
    """Resolve this user's merchant spellings into canonical identities.

    Spellings are grouped by :func:`normalize_merchant` — the same normalizer
    the ledger's own merchant identity uses, so resolution cannot disagree with
    it. Within a group the canonical name is the most frequently written
    original spelling, ties broken alphabetically so repeated runs are stable.
    The canonical spelling gets a link to itself, so looking up any known
    spelling resolves.
    """
    truncated_sources: set[UUID] = set()
    sightings = [
        *_ledger_sightings(db, user.id),
        *_spreadsheet_sightings(db, user.id),
        *_external_sightings(db, user.id, truncated_sources),
    ]
    groups: dict[str, Counter[str]] = defaultdict(Counter)
    origins: dict[tuple[str, str], set[UUID | None]] = defaultdict(set)
    for text, occurrences, source_id in sightings:
        normalized = normalize_merchant(text)
        spelling = text.strip()[:NAME_MAX]
        if not normalized or not spelling:
            continue
        groups[normalized][spelling] += occurrences
        origins[(normalized, spelling)].add(source_id)

    resolved: dict[str, tuple[str, Decimal, UUID | None]] = {}
    for normalized, spellings in groups.items():
        canonical = sorted(spellings.items(), key=lambda item: (-item[1], item[0]))[0][0]
        for spelling, support in spellings.items():
            # A source is credited only when it is the sole origin of the
            # spelling; a spelling several places write belongs to none of them.
            contributors = origins[(normalized, spelling)]
            source_id = next(iter(contributors)) if len(contributors) == 1 else None
            resolved[spelling] = (canonical, link_confidence(support), source_id)

    kind = EntityLinkKind.MERCHANT.value
    # Rows are reconciled by alias, not by the four-column unique key: when the
    # canonical spelling of a group changes, the same alias must be *rewritten*,
    # never duplicated under a second canonical.
    reusable: dict[str, EntityLink] = {}
    residue: list[EntityLink] = []
    for row in db.scalars(
        select(EntityLink)
        .where(EntityLink.user_id == user.id, EntityLink.kind == kind)
        .order_by(EntityLink.alias, EntityLink.created_at, EntityLink.id)
    ):
        if row.alias in resolved and row.alias not in reusable:
            reusable[row.alias] = row
        elif row.alias in resolved:
            # A second row for the same alias: an earlier writer's duplicate.
            residue.append(row)
        elif truncated_sources and row.source_id in truncated_sources:
            # This alias came from a source whose value window was capped, so
            # its absence is unread evidence, not vanished evidence. Deleting
            # here would assert a fact the read never established.
            reusable.setdefault(row.alias, row)
        else:
            residue.append(row)
    for row in residue:
        db.delete(row)
    # The deletes are flushed first on purpose: a discarded duplicate has to
    # leave the unique index before the surviving row can take its canonical.
    db.flush()

    links: list[EntityLink] = []
    for alias, (canonical, confidence, source_id) in resolved.items():
        link = reusable.get(alias)
        if link is None:
            link = EntityLink(user_id=user.id, kind=kind, alias=alias)
            db.add(link)
        link.canonical = canonical
        link.confidence = confidence
        link.source_id = source_id
        links.append(link)
    db.flush()
    return sorted(links, key=lambda item: (item.canonical, item.alias))


def canonical_merchant(db: Session, user_id: UUID, spelling: str) -> str | None:
    """The canonical name for one spelling, or None when it is unresolved."""
    alias = (spelling or "").strip()[:NAME_MAX]
    if not alias:
        return None
    return db.scalar(
        select(EntityLink.canonical).where(
            EntityLink.user_id == user_id,
            EntityLink.kind == EntityLinkKind.MERCHANT.value,
            EntityLink.alias == alias,
        )
    )
