from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import current_user, router
from app.database import get_db
from app.event_time import from_local_parts
from app.models import Category, DashboardTile, Transaction, User
from app.seed import default_user
from app.services.semantic import AnalysisPlan, AnalysisToolProposal, FinanceFilter, FinanceQueryPlan
from app.visualization_contracts import VisualEncodingContract, VisualFieldEncoding, VisualizationView


def client_for(db, user) -> TestClient:
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[current_user] = lambda: user
    return TestClient(application)


def occurred(day: date):
    return from_local_parts(day, None, "Asia/Kolkata")


def spend(user, category, amount_minor: int) -> Transaction:
    return Transaction(
        user_id=user.id,
        transaction_type="expense",
        amount_minor=amount_minor,
        currency="INR",
        category_id=category.id,
        transaction_at=occurred(date.today()),
    )


def proposal_payload(today: date, *, with_view: bool = False) -> dict:
    visualizations = []
    if with_view:
        visualizations = [VisualizationView(
            id="food_by_category",
            title="Food by category",
            dataset="food_spending_this_month",
            mark="bar",
            encoding=VisualEncodingContract(
                x=VisualFieldEncoding(field="category", type="nominal", value_type="category"),
                y=VisualFieldEncoding(field="value_minor", type="quantitative", value_type="money_minor"),
            ),
        )]
    return AnalysisToolProposal(
        name="Food spending by category",
        description="Summarize recorded food expenses for the current month.",
        intent_signature="monthly food spending",
        plan=AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Group recorded food expenses by category for the month"],
            visualizations=visualizations,
            queries=[FinanceQueryPlan(
                name="Food spending this month",
                metric="gross_spend",
                dimensions=["category"],
                filters=[FinanceFilter(field="category", value="food")],
                start_date=today.replace(day=1),
                end_date=today,
            )],
        ),
    ).model_dump(mode="json")


def test_dashboard_lifecycle_reflects_live_ledger_changes(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add(spend(user, food, 20_000))
    db.flush()
    client = client_for(db, user)

    created = client.post("/dashboards", json={"name": "Spending"})
    assert created.status_code == 200, created.text
    dashboard_id = created.json()["id"]
    assert created.json() == {"id": dashboard_id, "name": "Spending"}

    assert client.get("/dashboards").json() == {
        "dashboards": [{"id": dashboard_id, "name": "Spending", "tileCount": 0}],
    }

    added = client.post(
        f"/dashboards/{dashboard_id}/tiles",
        json={"title": "Food this month", "proposal": proposal_payload(date.today())},
    )
    assert added.status_code == 200, added.text
    tile_id = added.json()["id"]
    assert added.json() == {
        "id": tile_id,
        "dashboardId": dashboard_id,
        "title": "Food this month",
        "position": 0,
    }
    assert client.get("/dashboards").json()["dashboards"][0]["tileCount"] == 1

    first = client.get(f"/dashboards/{dashboard_id}")
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["id"] == dashboard_id
    assert page["name"] == "Spending"
    tile = page["tiles"][0]
    assert tile["id"] == tile_id
    assert tile["error"] is None
    assert tile["executedAt"]
    chart = tile["chart"]
    # No view was declared, so the synthesized bar spans the grouped dimension.
    assert chart["view"]["mark"] == "bar"
    assert chart["view"]["encoding"]["x"]["field"] == "category"
    assert chart["currency"] == "INR"
    assert sum(row["value_minor"] for row in chart["rows"]) == 20_000

    # The page is live: a new transaction changes the very next read.
    db.add(spend(user, food, 5_000))
    db.flush()
    second = client.get(f"/dashboards/{dashboard_id}").json()
    assert sum(row["value_minor"] for row in second["tiles"][0]["chart"]["rows"]) == 25_000

    assert client.delete(f"/dashboards/{dashboard_id}/tiles/{tile_id}").status_code == 204
    assert client.get(f"/dashboards/{dashboard_id}").json()["tiles"] == []
    assert client.get("/dashboards").json()["dashboards"][0]["tileCount"] == 0
    assert client.delete(f"/dashboards/{dashboard_id}/tiles/{tile_id}").status_code == 404


def test_plan_declared_view_drives_the_tile_chart(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add(spend(user, food, 20_000))
    db.flush()
    client = client_for(db, user)

    dashboard_id = client.post("/dashboards", json={"name": "Declared"}).json()["id"]
    added = client.post(
        f"/dashboards/{dashboard_id}/tiles",
        json={"title": "Food", "proposal": proposal_payload(date.today(), with_view=True)},
    )
    assert added.status_code == 200, added.text

    tile = client.get(f"/dashboards/{dashboard_id}").json()["tiles"][0]
    assert tile["error"] is None
    assert tile["chart"]["view"]["id"] == "food_by_category"
    assert tile["chart"]["lineage"]["origin"] == "dashboard"


def test_broken_tiles_degrade_without_breaking_the_page(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add(spend(user, food, 20_000))
    db.flush()
    client = client_for(db, user)

    dashboard_id = client.post("/dashboards", json={"name": "Mixed"}).json()["id"]
    good = client.post(
        f"/dashboards/{dashboard_id}/tiles",
        json={"title": "Good", "proposal": proposal_payload(date.today())},
    )
    assert good.status_code == 200, good.text

    # A proposal that can never render is refused at the door now: declared
    # missing_information would fail validation on every single view.
    refused = proposal_payload(date.today())
    refused["plan"]["missing_information"] = ["the loan interest rate"]
    refusal = client.post(
        f"/dashboards/{dashboard_id}/tiles",
        json={"title": "Refused", "proposal": refused},
    )
    assert refusal.status_code == 422
    assert "not_chartable" in refusal.json()["detail"]

    # A spec that rotted after storage; the API could never have accepted it.
    db.add(DashboardTile(
        user_id=user.id,
        dashboard_id=UUID(dashboard_id),
        title="Broken",
        position=2,
        spec={"kind": "plan", "proposal": {"name": "x"}},
    ))
    db.flush()

    page = client.get(f"/dashboards/{dashboard_id}").json()
    by_title = {tile["title"]: tile for tile in page["tiles"]}
    assert by_title["Good"]["error"] is None
    assert by_title["Good"]["chart"] is not None
    assert by_title["Broken"]["chart"] is None
    assert by_title["Broken"]["error"]["code"] == "invalid_analysis_plan"
    assert by_title["Broken"]["error"]["detail"]


def test_tile_proposal_is_validated_at_creation(db):
    user = default_user(db)
    client = client_for(db, user)
    dashboard_id = client.post("/dashboards", json={"name": "Strict"}).json()["id"]

    bad = client.post(
        f"/dashboards/{dashboard_id}/tiles",
        json={"title": "Bad", "proposal": {"name": "x"}},
    )
    assert bad.status_code == 422
    assert "invalid_analysis_plan" in bad.json()["detail"]
    assert client.get("/dashboards").json()["dashboards"][0]["tileCount"] == 0


def test_foreign_dashboards_and_tiles_return_404(db):
    user = default_user(db)
    stranger = User(email="stranger@example.com", display_name="Stranger")
    db.add(stranger)
    db.flush()
    owner = client_for(db, user)
    dashboard_id = owner.post("/dashboards", json={"name": "Private"}).json()["id"]
    tile_id = owner.post(
        f"/dashboards/{dashboard_id}/tiles",
        json={"title": "Mine", "proposal": proposal_payload(date.today())},
    ).json()["id"]

    other = client_for(db, stranger)
    assert other.get(f"/dashboards/{dashboard_id}").status_code == 404
    assert other.post(
        f"/dashboards/{dashboard_id}/tiles",
        json={"title": "Hijack", "proposal": proposal_payload(date.today())},
    ).status_code == 404
    assert other.delete(f"/dashboards/{dashboard_id}/tiles/{tile_id}").status_code == 404
    assert other.get("/dashboards").json() == {"dashboards": []}

    # A foreign tile id under the caller's own dashboard is equally invisible.
    own_dashboard = other.post("/dashboards", json={"name": "Own"}).json()["id"]
    assert other.delete(f"/dashboards/{own_dashboard}/tiles/{tile_id}").status_code == 404


# --- verification findings, pinned -------------------------------------------

def test_poisoned_tile_reports_per_tile_and_never_500s_the_page(db):
    """The reviewer's executed repro: a gap-filled daily window larger than its
    limit passed creation and 500'd the page at view time. It must now surface
    as that tile's error while sibling tiles still render."""
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add(spend(user, food, 20_000))
    db.flush()
    client = client_for(db, user)
    today = date.today()
    dashboard_id = client.post("/dashboards", json={"name": "Ops"}).json()["id"]

    healthy = client.post(
        f"/dashboards/{dashboard_id}/tiles",
        json={"title": "Healthy", "proposal": proposal_payload(today)},
    )
    assert healthy.status_code == 200

    poisoned = dict(proposal_payload(today))
    poisoned["plan"] = dict(poisoned["plan"])
    poisoned["plan"]["queries"] = [dict(
        poisoned["plan"]["queries"][0],
        start_date=(today - __import__("datetime").timedelta(days=180)).isoformat(),
        end_date=today.isoformat(),
        time_grouping={"field": "event_time", "grain": "day", "fill_gaps": True},
        limit=100,
    )]
    stored = client.post(
        f"/dashboards/{dashboard_id}/tiles",
        json={"title": "Poisoned", "proposal": poisoned},
    )
    assert stored.status_code == 200  # rot-after-storage belongs to the page error object

    page = client.get(f"/dashboards/{dashboard_id}")
    assert page.status_code == 200
    tiles = {tile["title"]: tile for tile in page.json()["tiles"]}
    assert tiles["Healthy"]["error"] is None
    assert tiles["Healthy"]["chart"] is not None
    assert tiles["Poisoned"]["chart"] is None
    assert tiles["Poisoned"]["error"]["code"] == "analysis_plan_rejected"


def test_unexpected_execution_errors_stay_inside_their_tile(db, monkeypatch):
    user = default_user(db)
    client = client_for(db, user)
    today = date.today()
    dashboard_id = client.post("/dashboards", json={"name": "Ops"}).json()["id"]
    client.post(
        f"/dashboards/{dashboard_id}/tiles",
        json={"title": "Any", "proposal": proposal_payload(today)},
    )

    from app import api as api_module

    def explode(*args, **kwargs):
        raise RuntimeError("backend went sideways")

    monkeypatch.setattr(api_module, "execute_analysis_template", explode)
    page = client.get(f"/dashboards/{dashboard_id}")
    assert page.status_code == 200
    tile = page.json()["tiles"][0]
    assert tile["error"]["code"] == "tile_execution_error"
    assert "backend went sideways" in tile["error"]["detail"]


def test_unchartable_proposals_are_refused_at_the_door(db):
    user = default_user(db)
    client = client_for(db, user)
    today = date.today()
    dashboard_id = client.post("/dashboards", json={"name": "Ops"}).json()["id"]

    dedicated = dict(proposal_payload(today))
    dedicated["plan"] = dict(dedicated["plan"], analysis_type="monthly_comparison", queries=[], transforms=[])
    response = client.post(
        f"/dashboards/{dashboard_id}/tiles",
        json={"title": "Dedicated", "proposal": dedicated},
    )
    assert response.status_code == 422
    assert "not_chartable" in response.json()["detail"]

    missing = dict(proposal_payload(today))
    missing["plan"] = dict(missing["plan"], missing_information=["the interest rate"])
    response = client.post(
        f"/dashboards/{dashboard_id}/tiles",
        json={"title": "Missing", "proposal": missing},
    )
    assert response.status_code == 422
    assert "not_chartable" in response.json()["detail"]


def test_colliding_query_names_are_rejected_by_the_plan_contract(db):
    user = default_user(db)
    client = client_for(db, user)
    today = date.today()
    dashboard_id = client.post("/dashboards", json={"name": "Ops"}).json()["id"]
    payload = dict(proposal_payload(today))
    payload["plan"] = dict(payload["plan"])
    first = dict(payload["plan"]["queries"][0])
    second = dict(first, name=first["name"].replace("_", " ").title())
    payload["plan"]["queries"] = [first, second]

    response = client.post(
        f"/dashboards/{dashboard_id}/tiles",
        json={"title": "Colliding", "proposal": payload},
    )
    assert response.status_code == 422


def test_dashboards_have_a_hard_tile_cap(db):
    from app.api import MAX_TILES_PER_DASHBOARD

    user = default_user(db)
    client = client_for(db, user)
    today = date.today()
    dashboard_id = client.post("/dashboards", json={"name": "Ops"}).json()["id"]
    for index in range(MAX_TILES_PER_DASHBOARD):
        assert client.post(
            f"/dashboards/{dashboard_id}/tiles",
            json={"title": f"T{index}", "proposal": proposal_payload(today)},
        ).status_code == 200
    overflow = client.post(
        f"/dashboards/{dashboard_id}/tiles",
        json={"title": "Overflow", "proposal": proposal_payload(today)},
    )
    assert overflow.status_code == 422
    assert "dashboard_full" in overflow.json()["detail"]
