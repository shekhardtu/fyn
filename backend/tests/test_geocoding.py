"""Naming the place a transaction happened.

The provider is stubbed everywhere here. Reverse geocoding is a call to a third
party with usage limits and a terms-of-service that asks for caching, so the
suite must never make one — and the caching behaviour is most of what is worth
asserting anyway.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import current_user, router
from app.config import Settings
from app.database import get_db
from app.models import Category, LocationLabel, Transaction, User
from app.seed import DEFAULT_USER_EMAIL
from app.services import geocoding
from app.services.geocoding import cached_label, needs_lookup, place_cell, resolve_cell

BENGALURU = (12.971599, 77.594566)


def _settings(**overrides) -> Settings:
    return Settings(location_enrichment_enabled=True, **overrides)


def _nominatim_payload(**address) -> dict:
    return {"address": {"country": "India", **address}}


@pytest.fixture
def stub_provider(monkeypatch):
    """Replaces the network call and counts how often it was made."""
    calls: list[tuple[float, float]] = []

    def install(payload, fail=False):
        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append((float(params["lat"]), float(params["lon"])))
            if fail:
                raise httpx.ConnectError("provider unreachable")
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))
        monkeypatch.setattr(geocoding.httpx, "get", fake_get)
        return calls

    return install


def test_a_cell_is_looked_up_once_however_many_transactions_land_in_it(db, stub_provider):
    calls = stub_provider(_nominatim_payload(suburb="Indiranagar", state="Karnataka"))
    settings = _settings()

    assert resolve_cell(db, *BENGALURU, settings=settings) == "Indiranagar, Karnataka"
    # A few metres away is the same ~150m cell, and must not be a second call.
    assert resolve_cell(db, 12.971610, 77.594570, settings=settings) == "Indiranagar, Karnataka"
    assert resolve_cell(db, *BENGALURU, settings=settings) == "Indiranagar, Karnataka"
    assert len(calls) == 1


def test_a_cell_with_no_name_is_remembered_as_such_rather_than_re_asked(db, stub_provider):
    calls = stub_provider({"address": {}})
    settings = _settings()

    assert resolve_cell(db, *BENGALURU, settings=settings) is None
    assert resolve_cell(db, *BENGALURU, settings=settings) is None
    assert len(calls) == 1, "an answered 'nothing here' must not be asked again"
    assert not needs_lookup(db, *BENGALURU)


def test_an_unreachable_provider_is_not_cached_as_an_absent_name(db, stub_provider):
    calls = stub_provider(None, fail=True)
    settings = _settings()

    assert resolve_cell(db, *BENGALURU, settings=settings) is None
    assert needs_lookup(db, *BENGALURU), "an outage must not blank a real place forever"
    assert resolve_cell(db, *BENGALURU, settings=settings) is None
    assert len(calls) == 2


def test_enrichment_off_records_coordinates_and_never_calls_out(db, stub_provider):
    calls = stub_provider(_nominatim_payload(suburb="Indiranagar", state="Karnataka"))
    assert resolve_cell(db, *BENGALURU, settings=Settings(location_enrichment_enabled=False)) is None
    assert calls == []


def test_the_label_names_the_neighbourhood_over_the_city_when_both_are_known(db, stub_provider):
    stub_provider(_nominatim_payload(suburb="Indiranagar", city="Bengaluru", state="Karnataka"))
    assert resolve_cell(db, *BENGALURU, settings=_settings()) == "Indiranagar, Karnataka"


def test_a_fix_with_only_a_country_still_says_something(db, stub_provider):
    stub_provider({"address": {"country": "India"}})
    assert resolve_cell(db, *BENGALURU, settings=_settings()) == "India"


def _application(db, user):
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: (yield db)
    application.dependency_overrides[current_user] = lambda: user
    return application


def test_saving_takes_a_known_name_immediately_and_learns_an_unknown_one_after(db, stub_provider, monkeypatch):
    calls = stub_provider(_nominatim_payload(suburb="Indiranagar", state="Karnataka"))
    monkeypatch.setattr(geocoding, "get_settings", _settings)

    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    food = db.scalar(select(Category).where(Category.slug == "food"))
    entry = {
        "amountMinor": 42_000,
        "merchant": "Third Wave",
        "transactionAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "transactionType": "expense",
        "categoryId": str(food.id),
        "subcategoryId": None,
        "spendNature": "discretionary",
        "location": None,
        "latitude": BENGALURU[0],
        "longitude": BENGALURU[1],
        "locationAccuracy": 18,
    }

    with TestClient(_application(db, user)) as client:
        assert client.patch("/privacy/location", json={"enabled": True}).status_code == 200

        # First save: the cell is unknown, so the response carries no name and
        # the lookup happens behind it.
        first = client.post("/transactions", json=entry)
        assert first.status_code == 201
        assert first.json()["location"] is None
        saved = db.get(Transaction, UUID(first.json()["id"]))
        db.refresh(saved)
        assert saved.location_label == "Indiranagar, Karnataka"
        assert len(calls) == 1

        # Second save in the same cell: named in the response itself, no call.
        second = client.post("/transactions", json=entry)
        assert second.status_code == 201
        assert second.json()["location"] == "Indiranagar, Karnataka"
        assert len(calls) == 1

    assert db.scalar(select(LocationLabel).where(LocationLabel.geohash == place_cell(*BENGALURU))) is not None


def test_a_typed_label_is_never_replaced_by_a_map_service(db, stub_provider, monkeypatch):
    stub_provider(_nominatim_payload(suburb="Indiranagar", state="Karnataka"))
    monkeypatch.setattr(geocoding, "get_settings", _settings)
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    food = db.scalar(select(Category).where(Category.slug == "food"))

    with TestClient(_application(db, user)) as client:
        assert client.patch("/privacy/location", json={"enabled": True}).status_code == 200
        created = client.post("/transactions", json={
            "amountMinor": 1_000,
            "merchant": "Third Wave",
            "transactionAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "transactionType": "expense",
            "categoryId": str(food.id),
            "subcategoryId": None,
            "spendNature": "discretionary",
            "location": "The usual place",
            "latitude": BENGALURU[0],
            "longitude": BENGALURU[1],
            "locationAccuracy": 18,
        })
        assert created.status_code == 201

    saved = db.get(Transaction, UUID(created.json()["id"]))
    db.refresh(saved)
    assert saved.location_label == "The usual place"


def test_cached_label_never_calls_out(db, stub_provider):
    calls = stub_provider(_nominatim_payload(suburb="Indiranagar", state="Karnataka"))
    assert cached_label(db, *BENGALURU) is None
    assert calls == [], "the save path must not be able to make a network call"
