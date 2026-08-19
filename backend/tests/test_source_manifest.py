from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.event_time import from_local_parts
from app.models import DataSource, SourceManifest, Transaction, User
from app.seed import default_user
from app.services import manifest
from app.services.manifest import (
    MAX_MERCHANT_VALUES,
    NATIVE_SOURCE_KIND,
    ensure_native_manifest,
    manifest_fingerprint,
    native_manifest_document,
    user_value_catalog,
)
from app.services.semantic_registry import semantic_schema_registry


def occurred(day: date):
    return from_local_parts(day, None, "Asia/Kolkata")


def expense(user_id, merchant: str, day: date = date(2026, 8, 5)) -> Transaction:
    return Transaction(
        user_id=user_id,
        transaction_type="expense",
        amount_minor=10_000,
        currency="INR",
        merchant_name=merchant,
        transaction_at=occurred(day),
    )


def test_native_manifest_covers_the_governed_registry():
    registry = semantic_schema_registry()
    document = native_manifest_document()

    assert document["kind"] == NATIVE_SOURCE_KIND
    assert document["semantics"]["provenance"] == "curated"
    assert document["semantics"]["version"] == registry.version
    assert document["physical"]["provenance"] == "profiled"

    profiled = document["physical"]["entities"]
    for entity in registry.entities:
        assert entity.name in profiled, entity.name
        governed = {
            name
            for column in profiled[entity.name]["columns"]
            for name in column["governed_fields"]
        }
        assert governed == {field.name for field in entity.fields}, entity.name
        sensitive = {field.name for field in entity.fields if field.sensitive}
        assert not sensitive & set(profiled[entity.name]["catalog_fields"])


def test_manifest_fingerprint_is_content_addressed():
    document = native_manifest_document()
    assert manifest_fingerprint(document) == manifest_fingerprint(native_manifest_document())
    altered = {**document, "name": "another ledger"}
    assert manifest_fingerprint(altered) != manifest_fingerprint(document)


def test_ensure_native_manifest_is_idempotent(db):
    first = ensure_native_manifest(db)
    second = ensure_native_manifest(db)

    assert first.id == second.id
    assert second.version == 1
    assert second.status == "active"
    source = db.scalar(select(DataSource).where(DataSource.kind == NATIVE_SOURCE_KIND))
    assert source.user_id is None
    assert db.scalar(select(SourceManifest).where(SourceManifest.data_source_id == source.id, SourceManifest.id != first.id)) is None


def test_registry_change_supersedes_the_active_manifest(db, monkeypatch):
    first = ensure_native_manifest(db)
    changed = {**native_manifest_document(), "name": "Canonical ledger, revised"}
    monkeypatch.setattr(manifest, "native_manifest_document", lambda: changed)

    second = ensure_native_manifest(db)

    assert second.id != first.id
    assert second.version == first.version + 1
    assert second.status == "active"
    assert second.manifest_hash != first.manifest_hash
    db.refresh(first)
    assert first.status == "superseded"


def test_template_pool_keys_on_the_stored_manifest(db):
    from app.models import AnalysisToolTemplate
    from app.services.analysis_seeds import seed_analysis_templates
    from app.services.manifest import native_manifest_fingerprint

    posted = ensure_native_manifest(db)
    assert posted.manifest_hash == native_manifest_fingerprint()

    seed_analysis_templates(db)
    template = db.scalar(select(AnalysisToolTemplate))
    assert template is not None
    assert template.source_manifest_hash == posted.manifest_hash


def test_value_catalog_is_tenant_scoped_and_never_sensitive(db):
    user = default_user(db)
    stranger = User(email="stranger@example.com", display_name="Stranger")
    db.add(stranger)
    db.flush()
    db.add(expense(user.id, "Blue Tokai"))
    db.add(expense(stranger.id, "Third Wave"))
    db.commit()

    catalog = user_value_catalog(db, user.id)

    merchants = catalog["transactions"]["merchant"]["values"]
    assert "Blue Tokai" in merchants
    assert "Third Wave" not in merchants
    assert "description" not in catalog["transactions"]
    assert "location" not in catalog["transactions"]
    assert catalog["transactions"]["transaction_type"]["values"] == ["expense"]


def test_value_catalog_marks_truncation_instead_of_hiding_it(db):
    user = default_user(db)
    for index in range(MAX_MERCHANT_VALUES + 1):
        db.add(expense(user.id, f"Merchant {index:02d}"))
    db.commit()

    entry = user_value_catalog(db, user.id)["transactions"]["merchant"]

    assert len(entry["values"]) == MAX_MERCHANT_VALUES
    assert entry["distinct"] == MAX_MERCHANT_VALUES + 1
    assert entry["truncated"] is True


def test_deleted_transactions_leave_the_catalog(db):
    from app.event_time import now_utc

    user = default_user(db)
    row = expense(user.id, "Gone Cafe")
    db.add(row)
    db.commit()
    assert "Gone Cafe" in user_value_catalog(db, user.id)["transactions"]["merchant"]["values"]

    row.deleted_at = now_utc()
    db.commit()

    assert "transactions" not in user_value_catalog(db, user.id) or "Gone Cafe" not in (
        user_value_catalog(db, user.id)["transactions"].get("merchant", {}).get("values", [])
    )
