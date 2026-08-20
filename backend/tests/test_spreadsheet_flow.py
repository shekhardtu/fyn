from __future__ import annotations

import io

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import current_user, router
from app.database import get_db
from app.models import SourceRecord, User
from app.seed import default_user


CSV_ONE = "Date,Narration,Amount,Category\n2026-08-01,Blue Tokai,450.00,Food\n2026-08-02,Metro card,100,Travel\n"
CSV_TWO = CSV_ONE + "2026-08-03,Big Basket,900,Groceries\n"


def client_for(db, user) -> TestClient:
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[current_user] = lambda: user
    return TestClient(application)


def upload(client, body: str, name: str | None = None, filename: str = "expenses.csv"):
    data = {"name": name} if name else {}
    return client.post(
        "/sources/spreadsheet",
        files={"file": (filename, io.BytesIO(body.encode()), "text/csv")},
        data=data,
    )


def test_upload_reupload_and_annotate_walk_the_manifest_versions(db):
    user = default_user(db)
    client = client_for(db, user)

    first = upload(client, CSV_ONE)
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["manifestVersion"] == 1
    assert payload["needsConfirmation"] is True
    amount = next(column for column in payload["columns"] if column["name"] == "Amount")
    assert amount["role"] == "money"
    source_id = payload["sourceId"]
    assert len(list(db.scalars(select(SourceRecord)))) == 2

    second = upload(client, CSV_TWO)
    assert second.json()["manifestVersion"] == 2
    assert len(list(db.scalars(select(SourceRecord)))) == 3

    annotated = client.post(
        f"/sources/spreadsheet/{source_id}/annotations",
        json={"annotations": [{"field": "Amount", "statement": "INR including GST"}]},
    )
    assert annotated.status_code == 200, annotated.text
    assert annotated.json()["manifestVersion"] == 3

    confirmed = upload(client, CSV_TWO)
    stated = next(column for column in confirmed.json()["columns"] if column["name"] == "Amount")
    assert stated["userStated"] == "INR including GST"


def test_foreign_sources_return_404_and_bad_uploads_are_typed(db):
    user = default_user(db)
    stranger = User(email="stranger@example.com", display_name="Stranger")
    db.add(stranger)
    db.flush()
    owner_client = client_for(db, user)
    source_id = upload(owner_client, CSV_ONE).json()["sourceId"]

    stranger_client = client_for(db, stranger)
    forbidden = stranger_client.post(
        f"/sources/spreadsheet/{source_id}/annotations",
        json={"annotations": [{"field": "Amount", "statement": "mine"}]},
    )
    assert forbidden.status_code == 404

    assert upload(owner_client, CSV_ONE, filename="data.xlsx").status_code == 415
    assert upload(owner_client, "").status_code == 422
    huge = "a,b\n" + "\n".join("1,2" for _ in range(5001))
    assert upload(owner_client, huge).status_code == 422
    duplicate_headers = upload(owner_client, "a,a\n1,2\n")
    assert duplicate_headers.status_code == 422
    assert "duplicate_headers" in duplicate_headers.json()["detail"]
