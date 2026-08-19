from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import SourceAnnotation, SourceRecord, User
from app.seed import default_user
from app.services.spreadsheet import (
    annotate_source_field,
    ensure_spreadsheet_manifest,
    match_fingerprint,
    profile_rows,
    query_source,
    semantic_draft,
)

HEADERS = ["Date", "Narration", "Amount", "Category"]
ROWS = [
    ["2026-08-01", "Blue Tokai", "450.00", "Food"],
    ["2026-08-02", "Metro card", "100", "Travel"],
    ["2026-08-03", "Blue Tokai", "450.00", "Food"],
]


def upload(db, user, name="expenses.csv", rows=ROWS, headers=HEADERS):
    return ensure_spreadsheet_manifest(db, user, name, headers, [list(row) for row in rows])


# --- profiling ---------------------------------------------------------------

def test_profile_infers_types_nulls_and_catalogs():
    profile = profile_rows(HEADERS, [*ROWS, ["", "", "", ""]])
    by_name = {column["name"]: column for column in profile["columns"]}
    assert by_name["Date"]["type"] == "date"
    assert by_name["Amount"]["type"] == "decimal"
    assert by_name["Amount"]["money_hint"] is True
    assert by_name["Category"]["catalog"]["values"] == ["Food", "Travel"]
    assert by_name["Date"]["null_count"] == 1


@pytest.mark.parametrize("headers,code", [
    ([], "upload_has_no_headers"),
    (["a", "A"], "duplicate_headers"),
    ([f"c{i}" for i in range(61)], "too_many_columns"),
])
def test_malformed_uploads_are_rejected_with_stable_codes(headers, code):
    with pytest.raises(ValueError) as excinfo:
        profile_rows(headers, [])
    assert code in str(excinfo.value)


def test_row_cap_is_enforced():
    with pytest.raises(ValueError) as excinfo:
        profile_rows(["a"], [["x"]] * 5001)
    assert "too_many_rows" in str(excinfo.value)


def test_fingerprint_prefills_known_export_shapes():
    fingerprint = match_fingerprint(["Date", "Account", "Amount", "Narration"])
    assert fingerprint["name"] == "generic_bookkeeping"
    draft = semantic_draft(profile_rows(HEADERS, ROWS), fingerprint)
    assert draft["Narration"] == {"role": "description", "confidence": 0.9}
    assert match_fingerprint(["Alpha", "Beta"]) is None


# --- manifest lifecycle ------------------------------------------------------

def test_upload_posts_v1_and_identical_reupload_is_a_noop(db):
    user = default_user(db)
    source, manifest = upload(db, user)
    assert manifest.version == 1
    _, again = upload(db, user)
    assert again.id == manifest.id
    assert db.scalar(select(SourceRecord).where(SourceRecord.data_source_id == source.id)) is not None


def test_changed_reupload_replaces_rows_and_bumps_version(db):
    user = default_user(db)
    source, first = upload(db, user)
    _, second = upload(db, user, rows=[*ROWS, ["2026-08-04", "Big Basket", "900", "Groceries"]])
    assert second.version == first.version + 1
    count = len(list(db.scalars(select(SourceRecord).where(SourceRecord.data_source_id == source.id))))
    assert count == 4  # replaced, not appended


def test_annotation_wins_and_survives_reupload(db):
    user = default_user(db)
    source, _ = upload(db, user)
    annotated = annotate_source_field(db, user, source.id, "Amount", "Amount is in INR including GST")
    assert annotated.document["annotations"]["fields"]["Amount"]["statement"] == "Amount is in INR including GST"

    _, after = upload(db, user, rows=[*ROWS, ["2026-08-05", "Chai", "20", "Food"]])
    assert after.document["annotations"]["fields"]["Amount"]["statement"] == "Amount is in INR including GST"
    assert after.document["semantics"]["provenance"] == "inferred"

    replaced = annotate_source_field(db, user, source.id, "Amount", "Amounts are gross")
    assert replaced.document["annotations"]["fields"]["Amount"]["statement"] == "Amounts are gross"
    rows = list(db.scalars(select(SourceAnnotation).where(SourceAnnotation.data_source_id == source.id)))
    assert len(rows) == 1


def test_annotating_an_unknown_field_or_foreign_source_is_rejected(db):
    user = default_user(db)
    stranger = User(email="stranger@example.com", display_name="Stranger")
    db.add(stranger)
    db.flush()
    source, _ = upload(db, user)
    with pytest.raises(ValueError, match="unknown_field"):
        annotate_source_field(db, user, source.id, "Nope", "whatever")
    with pytest.raises(ValueError, match="unknown_source"):
        annotate_source_field(db, stranger, source.id, "Amount", "mine now")
    with pytest.raises(ValueError, match="unknown_source"):
        annotate_source_field(db, user, uuid4(), "Amount", "ghost")


# --- querying ----------------------------------------------------------------

def test_query_sums_money_in_minor_units_grouped(db):
    user = default_user(db)
    source, _ = upload(db, user)
    result = query_source(
        db, user.id, source.id,
        metric="sum", value_field="Amount", group_by="Category",
    )
    assert result["columns"] == ["Category", "value_minor"]
    assert result["rows"][0] == {"Category": "Food", "value_minor": 90_000}
    assert result["rows"][1] == {"Category": "Travel", "value_minor": 10_000}
    assert result["matched_records"] == 3


def test_query_filters_and_counts(db):
    user = default_user(db)
    source, _ = upload(db, user)
    result = query_source(
        db, user.id, source.id,
        metric="count",
        filters=[{"field": "Narration", "operator": "contains", "value": "blue"}],
    )
    assert result["rows"] == [{"scope": "all", "value": 2}]


def test_query_is_tenant_scoped_and_field_validated(db):
    user = default_user(db)
    stranger = User(email="stranger2@example.com", display_name="Stranger")
    db.add(stranger)
    db.flush()
    source, _ = upload(db, user)
    with pytest.raises(ValueError, match="unknown_source"):
        query_source(db, stranger.id, source.id, metric="count")
    with pytest.raises(ValueError, match="unknown_field"):
        query_source(db, user.id, source.id, metric="sum", value_field="Ghost")
    with pytest.raises(ValueError, match="value_field_required"):
        query_source(db, user.id, source.id, metric="sum")


# --- alignment with the migration -------------------------------------------

def test_baseline_secures_both_spreadsheet_tables():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0001_baseline.py"
    spec = importlib.util.spec_from_file_location("migration_baseline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert {"source_records", "source_annotations"} <= set(module.EXTENDED_USER_TABLES)


# --- verification findings, pinned -------------------------------------------

def test_cell_edit_with_identical_profile_still_posts_a_new_version(db):
    """A changed cell must never be silently judged 'unchanged' (HIGH finding):
    the manifest digest covers content, so the row replacement commits."""
    user = default_user(db)
    source, first = upload(db, user)
    corrected = [list(row) for row in ROWS]
    corrected[1][2] = "475.00"  # same type/distinct/null profile, new content

    _, second = upload(db, user, rows=corrected)

    assert second.version == first.version + 1
    result = query_source(db, user.id, source.id, metric="sum", value_field="Amount")
    assert result["rows"] == [{"scope": "all", "value_minor": 137_500}]


def test_stated_role_overrides_inferred_money_semantics(db):
    """user_stated must win where it counts: a 'Credit' points column stops
    being rendered as money once the user says so (MEDIUM finding)."""
    user = default_user(db)
    headers = ["Date", "Description", "Debit", "Credit", "Balance"]
    rows = [
        ["2026-08-01", "coffee", "450", "120", "10000"],
        ["2026-08-02", "metro", "100", "80", "9900"],
    ]
    source, _ = upload(db, user, name="statement.csv", headers=headers, rows=rows)

    before = query_source(db, user.id, source.id, metric="sum", value_field="Credit")
    assert before["columns"][-1] == "value_minor"  # fingerprint says money

    annotate_source_field(
        db, user, source.id, "Credit", "Credit is loyalty points, not money", role="number"
    )
    after = query_source(db, user.id, source.id, metric="sum", value_field="Credit")
    assert after["columns"][-1] == "value"
    assert after["rows"] == [{"scope": "all", "value": 200.0}]

    with pytest.raises(ValueError, match="unknown_role"):
        annotate_source_field(db, user, source.id, "Credit", "points", role="astrology")


def test_version_conflict_is_retried_and_the_annotation_survives(db, monkeypatch):
    """The losing writer must redo its work, not adopt a win it didn't have
    (MEDIUM finding). One simulated conflict, then success with the statement
    present in the reposted manifest."""
    from app.services import spreadsheet as spreadsheet_module
    from app.services.manifest import ManifestVersionConflict

    user = default_user(db)
    source, _ = upload(db, user)
    real = spreadsheet_module.post_manifest_version
    calls = {"count": 0}

    def flaky(session, target, document, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            session.rollback()
            raise ManifestVersionConflict("simulated concurrent writer")
        return real(session, target, document, **kwargs)

    monkeypatch.setattr(spreadsheet_module, "post_manifest_version", flaky)
    manifest = annotate_source_field(db, user, source.id, "Amount", "GST inclusive")

    assert calls["count"] == 2
    assert manifest.document["annotations"]["fields"]["Amount"]["statement"] == "GST inclusive"
    row = db.scalar(select(SourceAnnotation).where(SourceAnnotation.data_source_id == source.id))
    assert row is not None and row.statement == "GST inclusive"
