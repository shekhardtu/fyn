"""Red-team suite for the Phase-3 uploaded-spreadsheet surface.

Every test is an attack through a public entry point — the FastAPI endpoints
via TestClient (auth override pattern from test_spreadsheet_flow) or
``query_source`` directly — and asserts the exact defined behavior: hostile
cells stay inert data, caps produce typed rejections, and no input crashes
the deterministic query engine.
"""
from __future__ import annotations

import io
import math
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect as sqla_inspect, select

from app.api import current_user, router
from app.database import get_db
from app.models import SourceRecord, Transaction
from app.seed import default_user
from app.services.spreadsheet import QUERY_ROW_CAP, query_source


def client_for(db, user) -> TestClient:
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[current_user] = lambda: user
    return TestClient(application)


def upload(client, body: str, name: str | None = None, filename: str = "attack.csv"):
    data = {"name": name} if name else {}
    return client.post(
        "/sources/spreadsheet",
        files={"file": (filename, io.BytesIO(body.encode()), "text/csv")},
        data=data,
    )


def stored_records(db, source_id) -> list[dict]:
    rows = db.scalars(
        select(SourceRecord)
        .where(SourceRecord.data_source_id == source_id)
        .order_by(SourceRecord.row_index)
    )
    return [row.record for row in rows]


# --- formula injection --------------------------------------------------------

FORMULA_CELLS = ["=SUM(A1:A2)", "+1234", "-2+3", "@cmd|' /C calc'!A0"]


def test_formula_prefixed_cells_are_stored_inert_and_returned_as_plain_strings(db):
    user = default_user(db)
    client = client_for(db, user)
    body = "Date,Narration,Amount\n" + "".join(
        f'2026-08-0{i + 1},"{cell}",{(i + 1) * 100}\n' for i, cell in enumerate(FORMULA_CELLS)
    )
    response = upload(client, body)
    assert response.status_code == 200, response.text
    source_id = UUID(response.json()["sourceId"])

    # Stored byte-for-byte, leading = + - @ intact — never normalized, never evaluated.
    assert [record["Narration"] for record in stored_records(db, source_id)] == FORMULA_CELLS

    result = query_source(
        db, user.id, source_id, metric="sum", value_field="Amount", group_by="Narration"
    )
    assert result["columns"] == ["Narration", "value_minor"]
    keys = {row["Narration"]: row["value_minor"] for row in result["rows"]}
    assert keys == {
        "@cmd|' /C calc'!A0": 40_000,
        "-2+3": 30_000,
        "+1234": 20_000,
        "=SUM(A1:A2)": 10_000,
    }
    assert all(isinstance(key, str) for key in keys)  # plain strings, no evaluation
    # "=SUM(A1:A2)" was not computed, "-2+3" did not become "1".
    assert "1" not in keys and "300" not in keys


# --- SQL in a cell ------------------------------------------------------------

SQL_PAYLOAD = "'); DROP TABLE transactions; --"


def test_sql_in_a_cell_is_stored_as_data_and_never_executed(db):
    user = default_user(db)
    client = client_for(db, user)
    transactions_before = db.scalar(select(func.count()).select_from(Transaction))

    body = f'Date,Narration,Amount\n2026-08-01,"{SQL_PAYLOAD}",100\n2026-08-02,other,200\n'
    response = upload(client, body)
    assert response.status_code == 200, response.text
    source_id = UUID(response.json()["sourceId"])

    # The payload round-trips verbatim through storage and an eq filter.
    assert stored_records(db, source_id)[0]["Narration"] == SQL_PAYLOAD
    result = query_source(
        db, user.id, source_id, metric="count",
        filters=[{"field": "Narration", "operator": "eq", "value": SQL_PAYLOAD}],
    )
    assert result["rows"] == [{"scope": "all", "value": 1}]

    # Provably not executed: the governed table still exists and is untouched.
    assert sqla_inspect(db.get_bind()).has_table("transactions")
    assert db.scalar(select(func.count()).select_from(Transaction)) == transactions_before


# --- header collisions with governed tables -----------------------------------

def test_headers_named_after_governed_tables_are_inert_labels(db):
    user = default_user(db)
    client = client_for(db, user)
    transactions_before = db.scalar(select(func.count()).select_from(Transaction))

    response = upload(client, "transactions,users,amount\nledger,alice,100\nledger,bob,250\n")
    assert response.status_code == 200, response.text
    source_id = UUID(response.json()["sourceId"])

    # The collision-named header is just a record key inside this source.
    result = query_source(
        db, user.id, source_id, metric="sum", value_field="amount", group_by="transactions"
    )
    assert result["rows"] == [{"transactions": "ledger", "value_minor": 35_000}]

    # Native queries against the real tables are unaffected.
    assert db.scalar(select(func.count()).select_from(Transaction)) == transactions_before


# --- oversize header and oversize cell ----------------------------------------

def test_oversize_header_and_100kb_cell_are_accepted_and_stored(db):
    # Defined behavior: accept-and-store. No header-length or cell-length cap
    # exists below the 10MB file cap, so the upload succeeds and round-trips.
    user = default_user(db)
    client = client_for(db, user)
    long_header = "H" * 150
    huge_cell = "x" * 100_000

    response = upload(client, f"Date,{long_header}\n2026-08-01,{huge_cell}\n")
    assert response.status_code == 200, response.text
    payload = response.json()
    source_id = UUID(payload["sourceId"])
    assert [column["name"] for column in payload["columns"]] == ["Date", long_header]
    assert stored_records(db, source_id)[0][long_header] == huge_cell

    result = query_source(
        db, user.id, source_id, metric="count",
        filters=[{"field": long_header, "operator": "contains", "value": "x" * 500}],
    )
    assert result["rows"] == [{"scope": "all", "value": 1}]

    # The annotation schema caps field names at 120 chars, so the oversize
    # header is rejected with a typed 422 at the annotation endpoint.
    rejected = client.post(
        f"/sources/spreadsheet/{source_id}/annotations",
        json={"annotations": [{"field": long_header, "statement": "too wide"}]},
    )
    assert rejected.status_code == 422


# --- ragged rows --------------------------------------------------------------

def test_ragged_rows_truncate_extras_and_blank_missing_cells(db):
    user = default_user(db)
    client = client_for(db, user)

    response = upload(client, "a,b,c\n1,2,3,4,5\nonly\n")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["rowCount"] == 2
    assert stored_records(db, UUID(payload["sourceId"])) == [
        {"a": "1", "b": "2", "c": "3"},  # cells beyond the headers are dropped
        {"a": "only", "b": "", "c": ""},  # missing cells become empty strings
    ]

    result = query_source(db, user.id, UUID(payload["sourceId"]), metric="count", group_by="b")
    assert sorted(result["rows"], key=lambda row: row["b"]) == [
        {"b": "", "value": 1},
        {"b": "2", "value": 1},
    ]


# --- filter flooding ----------------------------------------------------------

def test_one_thousand_filters_stay_bounded_and_deterministic(db):
    user = default_user(db)
    client = client_for(db, user)
    source_id = UUID(upload(
        client, "Date,Narration,Amount\n2026-08-01,alpha,100\n2026-08-02,beta,200\n"
    ).json()["sourceId"])

    flood = [{"field": "Narration", "operator": "contains", "value": "a"}] * 1000
    result = query_source(
        db, user.id, source_id, metric="count", filters=flood, limit=10**9
    )
    # Both narrations contain "a"; the absurd limit clamps to the row cap.
    assert result["rows"] == [{"scope": "all", "value": 2}]
    assert result["matched_records"] == 2
    assert result["row_count"] <= QUERY_ROW_CAP

    # One bad field among 1000 is rejected up front with the typed code.
    with pytest.raises(ValueError, match="unknown_field"):
        query_source(
            db, user.id, source_id, metric="count",
            filters=[*flood, {"field": "Ghost", "operator": "eq", "value": "x"}],
        )


# --- annotation flooding ------------------------------------------------------

def test_100kb_annotation_statement_is_a_typed_422(db):
    user = default_user(db)
    client = client_for(db, user)
    source_id = upload(
        client, "Date,Amount\n2026-08-01,100\n"
    ).json()["sourceId"]

    response = client.post(
        f"/sources/spreadsheet/{source_id}/annotations",
        json={"annotations": [{"field": "Amount", "statement": "x" * 100_000}]},
    )
    assert response.status_code == 422
    assert any("statement" in error["loc"] for error in response.json()["detail"])


# --- non-finite and overflowing numerics --------------------------------------

def test_nan_and_overflow_cells_have_defined_aggregation_results(db):
    user = default_user(db)
    client = client_for(db, user)
    source_id = UUID(upload(
        client,
        "Date,Narration,Amount\n"
        "2026-08-01,huge,1e309\n"
        "2026-08-02,nan,NaN\n"
        "2026-08-03,inf,Infinity\n",
    ).json()["sourceId"])

    # Money sum: NaN/Infinity cells are non-numeric and contribute nothing;
    # 1e309 is finite and sums exactly in integer minor units.
    result = query_source(db, user.id, source_id, metric="sum", value_field="Amount")
    assert result["rows"] == [{"scope": "all", "value_minor": 10**311}]
    assert result["matched_records"] == 3

    # Average skips the non-numeric cells too.
    average = query_source(db, user.id, source_id, metric="average", value_field="Amount")
    assert average["rows"] == [{"scope": "all", "value_minor": 10**311}]

    # A gte filter against "NaN" matches nothing instead of raising
    # decimal.InvalidOperation.
    nan_filter = query_source(
        db, user.id, source_id, metric="sum", value_field="Amount",
        filters=[{"field": "Amount", "operator": "gte", "value": "NaN"}],
    )
    assert nan_filter["rows"] == []
    assert nan_filter["matched_records"] == 0

    # A numeric gte filter treats the NaN/Infinity cells as non-numeric rows.
    finite_filter = query_source(
        db, user.id, source_id, metric="count",
        filters=[{"field": "Amount", "operator": "gte", "value": "0"}],
    )
    assert finite_filter["rows"] == [{"scope": "all", "value": 1}]


def test_overflowing_non_money_sum_yields_float_infinity_not_a_crash(db):
    user = default_user(db)
    client = client_for(db, user)
    source_id = UUID(upload(client, "Day,Score\nmon,1e309\ntue,2\n").json()["sourceId"])

    result = query_source(db, user.id, source_id, metric="sum", value_field="Score")
    assert result["columns"] == ["scope", "value"]
    value = result["rows"][0]["value"]
    assert isinstance(value, float) and math.isinf(value) and value > 0
    assert result["matched_records"] == 2
