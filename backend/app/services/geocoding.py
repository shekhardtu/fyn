"""Turning the coordinates a device reports into a place a person recognises.

Three rules shape everything below.

**Never in the save path.** Reverse geocoding is a call to a third party, and a
person pressing Save must not wait on one. A save takes whatever the cache
already knows; anything else is filled in afterwards.

**Once per cell, not once per transaction.** Names are cached by geohash at
~150m, which is roughly a building and its forecourt. Somebody's regular coffee
shop is looked up once, ever. This is also what the free provider's terms ask
for, and the reason a cache miss is cheap to be strict about.

**A failure is a missing label, never a failed save.** Every path here returns
None rather than raising. A transaction with coordinates and no name is a
complete record; the name is a convenience laid over it.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import LocationLabel, Transaction
from .recommendation import geohash_encode

# ~150m per cell. Fine enough that two shops on one street do not share a name,
# coarse enough that standing at either end of a forecourt is one lookup.
PLACE_PRECISION = 7

# Nominatim's zoom scale: 14 is "suburb", which is the level a person would
# name a spend by. Higher zooms return a building number, which is both more
# precise than the fix deserves and not what anyone calls the place.
NOMINATIM_ZOOM = 14
NOMINATIM_TIMEOUT_SECONDS = 8.0

# The keys OSM uses for "the settlement", most specific first. A spend in a
# named neighbourhood should say the neighbourhood.
_CITY_KEYS = ("suburb", "neighbourhood", "quarter", "city_district", "city", "town", "village", "municipality")
_STATE_KEYS = ("state", "province", "region")


def place_cell(latitude: float, longitude: float) -> str:
    """The cache key for a coordinate."""
    return geohash_encode(latitude, longitude, PLACE_PRECISION)


def _cell_row(db: Session, cell: str) -> LocationLabel | None:
    return db.scalar(select(LocationLabel).where(LocationLabel.geohash == cell))


def geocoding_provider(settings: Settings | None = None) -> str:
    """Which provider this installation will call, or "none".

    "none" is a working configuration, not a broken one: coordinates are still
    recorded, they just never get a name. Turning the feature off must not make
    location capture stop working.
    """
    settings = settings or get_settings()
    if not settings.location_enrichment_enabled:
        return "none"
    if settings.geocoding_provider != "auto":
        return settings.geocoding_provider
    return "nominatim"


def cached_label(db: Session, latitude: float, longitude: float) -> str | None:
    """The name already known for this cell, without any network call.

    Returns None both when the cell has never been looked up and when it was
    looked up and had no name. The caller cannot act differently on the two,
    and `needs_lookup` is what distinguishes them.
    """
    row = _cell_row(db, place_cell(latitude, longitude))
    return row.display if row else None


def needs_lookup(db: Session, latitude: float, longitude: float) -> bool:
    """Whether this cell has never been asked about.

    A cell the provider answered "nothing here" for is not asked again — that
    answer is as real as a name, and re-asking would spend the request budget
    on the same empty highway forever.
    """
    return _cell_row(db, place_cell(latitude, longitude)) is None


def _display(city: str | None, state: str | None, country: str | None) -> str | None:
    """The one line a transaction row shows.

    City and state, because that is how a person says where they were. Country
    only when nothing more specific came back, which is the case for a fix in
    open water or an unmapped area — better than showing nothing at all.
    """
    parts = [part for part in (city, state) if part]
    if not parts:
        parts = [part for part in (country,) if part]
    return ", ".join(parts)[:160] or None


def _from_nominatim(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    address = payload.get("address") or {}
    city = next((address[key] for key in _CITY_KEYS if address.get(key)), None)
    state = next((address[key] for key in _STATE_KEYS if address.get(key)), None)
    return city, state, address.get("country")


def _fetch_nominatim(settings: Settings, latitude: float, longitude: float) -> tuple[str | None, str | None, str | None] | None:
    """One reverse lookup, or None if the provider could not be reached.

    None is not "no name here" — it is "we do not know", and the caller must
    not cache it, or a momentary outage would permanently blank a real place.
    """
    try:
        response = httpx.get(
            f"{settings.nominatim_base_url.rstrip('/')}/reverse",
            params={
                "lat": f"{latitude:.6f}",
                "lon": f"{longitude:.6f}",
                "format": "jsonv2",
                "zoom": NOMINATIM_ZOOM,
                "addressdetails": 1,
            },
            # Nominatim's usage policy requires an identifying User-Agent and
            # blocks clients that omit one. This is a condition of the free
            # service, not a nicety.
            headers={"User-Agent": settings.geocoding_user_agent, "Accept": "application/json"},
            timeout=NOMINATIM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return _from_nominatim(response.json())
    except (httpx.HTTPError, ValueError):
        return None


def resolve_cell(db: Session, latitude: float, longitude: float, *, settings: Settings | None = None) -> str | None:
    """Look this cell up and remember the answer. Returns the name, if any.

    Safe to call for a cell already cached — it returns what is stored without
    calling out. The provider is only asked for a cell nobody has asked about.
    """
    settings = settings or get_settings()
    cell = place_cell(latitude, longitude)
    existing = _cell_row(db, cell)
    if existing:
        return existing.display

    provider = geocoding_provider(settings)
    if provider == "none":
        return None
    if provider != "nominatim":
        # An unknown provider name is a configuration mistake. Recording a miss
        # would bake that mistake into the cache, so nothing is written.
        return None

    found = _fetch_nominatim(settings, latitude, longitude)
    if found is None:
        return None

    city, state, country = found
    db.add(LocationLabel(
        geohash=cell,
        city=city,
        state=state,
        country=country,
        display=_display(city, state, country),
        provider=provider,
    ))
    db.flush()
    return _display(city, state, country)


def backfill_transaction_label(session_factory, transaction_id: UUID, user_id: UUID) -> None:
    """Name one saved transaction's location, long after it was saved.

    Runs on its own session because the request that created the transaction
    has already answered and closed its own. Overwrites nothing a person typed:
    a label they chose outranks any name a map service has for the place.
    """
    db = session_factory()
    try:
        # Scoped in the query rather than checked after loading: a background
        # worker holds no request context, so ownership has to be part of how
        # the row is found at all.
        transaction = db.scalar(select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.user_id == user_id,
        ))
        if not transaction:
            return
        if transaction.latitude is None or transaction.longitude is None:
            return
        if transaction.location_source != "device":
            return

        label = resolve_cell(db, float(transaction.latitude), float(transaction.longitude))
        if label and not transaction.location_label:
            transaction.location_label = label
        db.commit()
    except Exception:
        # A background name lookup must never be able to take anything down,
        # and there is nobody waiting on this to report to.
        db.rollback()
    finally:
        db.close()
