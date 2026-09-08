from datetime import date, datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import router as money_router
from app.api_finance import router
from app.database import get_db
from app.models import Account, AccountBalanceSnapshot, Budget, Category, Conversation, Goal, GoalContribution, InvestmentHolding, Loan, Transaction, TransactionDraft, TransactionRevision, User
from app.security import current_user
from app.seed import DEFAULT_USER_EMAIL
from app.services.overview import overview_snapshot


@pytest.fixture()
def client(db):
    application = FastAPI()
    application.include_router(router)
    application.include_router(money_router)
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[current_user] = lambda: user
    with TestClient(application) as client:
        yield client


def test_budget_create_update_and_scope_do_not_need_a_conversation(client, db):
    payload = {"name": "Monthly limit", "amountMinor": 500_000, "categoryId": None}
    created = client.post("/budgets", json=payload)
    assert created.status_code == 201, created.text
    budget = created.json()
    assert budget["currency"] == "INR"
    assert budget["period"] == "monthly"
    assert client.post("/budgets", json=payload).status_code == 409
    edited = client.patch(f"/budgets/{budget['id']}", json={**payload, "amountMinor": 600_000})
    assert edited.status_code == 200
    assert edited.json()["amountMinor"] == 600_000
    category = db.scalar(select(Category).where(Category.slug == "food"))
    created_category = client.post("/budgets", json={**payload, "categoryId": str(category.id), "name": "Food"})
    assert created_category.status_code == 201
    assert len(client.get("/budgets").json()) == 2
    assert db.get(Budget, UUID(budget["id"])).amount_minor == 600_000
    assert client.post("/budgets", json={**payload, "categoryId": str(uuid4())}).status_code == 422


def test_goal_save_edit_and_retried_contribution_are_persisted_once(client, db):
    payload = {"name": "Emergency fund", "targetMinor": 1_000_000, "targetDate": "2027-01-01"}
    created = client.post("/goals", json=payload)
    assert created.status_code == 201, created.text
    goal_id = created.json()["id"]
    assert client.post("/goals", json=payload).status_code == 409
    entry = {"amountMinor": 12_500, "requestId": str(uuid4())}
    assert client.post(f"/goals/{goal_id}/contributions", json=entry).json()["currentMinor"] == 12_500
    assert client.post(f"/goals/{goal_id}/contributions", json=entry).json()["currentMinor"] == 12_500
    assert client.post(f"/goals/{goal_id}/contributions", json={**entry, "amountMinor": 25_000}).status_code == 409
    edited = client.patch(f"/goals/{goal_id}", json={**payload, "targetMinor": 2_000_000})
    assert edited.status_code == 200
    assert edited.json()["currentMinor"] == 12_500
    assert edited.json()["targetDate"] == "2027-01-01"
    assert client.get("/goals").json()[0]["targetMinor"] == 2_000_000
    assert db.scalar(select(func.count()).select_from(GoalContribution)) == 1
    assert db.scalar(select(func.count()).select_from(Transaction)) == 0


@pytest.mark.parametrize("balance", [0, 125_000, -45_000])
def test_account_records_currency_signed_balance_and_snapshot(client, db, balance):
    payload = {"name": " Travel card ", "accountType": "credit_card", "currency": " usd ", "balanceMinor": balance, "institution": "Bank", "mask": "1234"}
    response = client.post("/accounts", json=payload)
    assert response.status_code == 201, response.text
    account = response.json()
    assert account["name"] == "Travel card"
    assert account["currency"] == "USD"
    assert account["balanceMinor"] == balance
    assert client.post("/accounts", json=payload).status_code == 409
    assert len(client.get("/accounts").json()) == 1
    snapshot = db.scalar(select(AccountBalanceSnapshot))
    assert snapshot.balance_minor == balance
    assert snapshot.currency == "USD"
    assert db.get(Account, UUID(account["id"])).balance_minor == balance


def test_delete_account_removes_balance_history_and_allows_name_reuse(client, db):
    db.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
    payload = {"name": "ICICI", "accountType": "bank", "currency": "INR", "balanceMinor": 125_000}
    account = client.post("/accounts", json=payload).json()
    response = client.delete(f"/accounts/{account['id']}")
    assert response.status_code == 204, response.text
    assert response.content == b""
    assert client.get("/accounts").json() == []
    assert db.scalar(select(func.count()).select_from(AccountBalanceSnapshot)) == 0
    assert client.delete(f"/accounts/{account['id']}").status_code == 404
    assert client.post("/accounts", json=payload).status_code == 201


def test_delete_account_preserves_ledger_transfers_drafts_and_other_financial_records(client, db):
    db.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    account = Account(user_id=user.id, name="ICICI", currency="INR", balance_minor=125_000)
    remaining = Account(user_id=user.id, name="Savings", currency="INR", balance_minor=40_000)
    conversation = Conversation(user_id=user.id, title="Account deletion")
    db.add_all([account, remaining, conversation])
    db.flush()
    transactions = [
        Transaction(user_id=user.id, account_id=account.id, transaction_type="expense", amount_minor=1_000, currency="INR", transaction_at=datetime(2026, 9, 8, tzinfo=timezone.utc)),
        Transaction(user_id=user.id, account_id=account.id, destination_account_id=remaining.id, transaction_type="transfer", amount_minor=2_000, currency="INR"),
        Transaction(user_id=user.id, account_id=remaining.id, destination_account_id=account.id, transaction_type="transfer", amount_minor=3_000, currency="INR"),
        Transaction(user_id=user.id, account_id=account.id, transaction_type="expense", amount_minor=500, currency="INR", deleted_at=datetime(2026, 9, 8, tzinfo=timezone.utc)),
    ]
    drafts = [
        TransactionDraft(user_id=user.id, conversation_id=conversation.id, raw_text="Transfer", account_id=account.id, source_account_name=account.name, destination_account_id=remaining.id, destination_account_name=remaining.name),
        TransactionDraft(user_id=user.id, conversation_id=conversation.id, raw_text="Transfer back", account_id=remaining.id, source_account_name=remaining.name, destination_account_id=account.id, destination_account_name=account.name),
    ]
    holding = InvestmentHolding(user_id=user.id, account_id=account.id, name="Fund", current_value_minor=50_000)
    loan = Loan(user_id=user.id, account_id=account.id, name="Loan", outstanding_principal_minor=100_000, annual_rate_percent=5, remaining_tenure_months=12)
    snapshot = AccountBalanceSnapshot(user_id=user.id, account_id=account.id, balance_minor=125_000)
    db.add_all([*transactions, *drafts, holding, loan, snapshot])
    db.commit()
    before = overview_snapshot(db, user.id, date(2026, 9, 1), date(2026, 9, 8))["summary"]

    response = client.delete(f"/accounts/{account.id}")

    assert response.status_code == 204, response.text
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Transaction)) == 4
    assert [item.amount_minor for item in transactions] == [1_000, 2_000, 3_000, 500]
    assert all(item.row_version == 2 for item in transactions)
    assert transactions[0].account_id is None
    assert transactions[1].account_id is None and transactions[1].destination_account_id == remaining.id
    assert transactions[2].account_id == remaining.id and transactions[2].destination_account_id is None
    assert transactions[3].account_id is None and transactions[3].deleted_at is not None
    revisions = list(db.scalars(select(TransactionRevision).where(TransactionRevision.source == "account_deleted")))
    assert len(revisions) == 4
    assert all(item.actor_user_id == user.id for item in revisions)
    assert drafts[0].account_id is None and drafts[0].source_account_name is None
    assert drafts[0].destination_account_id == remaining.id and drafts[0].destination_account_name == remaining.name
    assert drafts[1].destination_account_id is None and drafts[1].destination_account_name is None
    assert drafts[1].account_id == remaining.id and drafts[1].source_account_name == remaining.name
    assert holding.account_id is None and holding.current_value_minor == 50_000
    assert loan.account_id is None and loan.outstanding_principal_minor == 100_000
    assert db.scalar(select(func.count()).select_from(AccountBalanceSnapshot)) == 0
    assert overview_snapshot(db, user.id, date(2026, 9, 1), date(2026, 9, 8))["summary"] == before
    assert [item["id"] for item in client.get("/accounts").json()] == [str(remaining.id)]


def test_delete_account_rejects_unknown_and_other_owners_accounts(client, db):
    other = User(email="account-owner@example.test", display_name="Other", currency="INR", timezone="UTC")
    db.add(other)
    db.flush()
    account = Account(user_id=other.id, name="Private", balance_minor=42)
    db.add(account)
    db.commit()
    assert client.delete(f"/accounts/{account.id}").status_code == 404
    assert client.delete(f"/accounts/{uuid4()}").status_code == 404
    assert db.get(Account, account.id).balance_minor == 42


def test_finance_forms_enforce_owner_and_private_category_boundaries(client, db):
    other = User(email="finance-other@example.test", display_name="Other", currency="USD", timezone="UTC")
    db.add(other)
    db.flush()
    budget = Budget(user_id=other.id, name="Private", amount_minor=12_000, currency="USD")
    goal = Goal(user_id=other.id, name="Private", target_minor=100_000, current_minor=0, currency="USD")
    category = Category(name="Private category", slug="private", scope="user", owner_user_id=other.id, icon="tag")
    db.add_all([budget, goal, category, Account(user_id=other.id, name="Private", currency="USD", balance_minor=42)])
    db.commit()
    assert client.get("/budgets").json() == []
    assert client.get("/goals").json() == []
    assert client.get("/accounts").json() == []
    assert client.patch(f"/budgets/{budget.id}", json={"name": "Mine", "amountMinor": 100}).status_code == 404
    assert client.patch(f"/goals/{goal.id}", json={"name": "Mine", "targetMinor": 100}).status_code == 404
    assert client.post(f"/goals/{goal.id}/contributions", json={"amountMinor": 100, "requestId": str(uuid4())}).status_code == 404
    assert client.post("/budgets", json={"name": "Mine", "amountMinor": 100, "categoryId": str(category.id)}).status_code == 422
    assert db.get(Goal, goal.id).current_minor == 0


@pytest.mark.parametrize("path,payload", [
    ("/budgets", {"name": "Limit", "amountMinor": 0}),
    ("/budgets", {"name": "Limit", "amountMinor": 2_147_483_648}),
    ("/goals", {"name": "  ", "targetMinor": 100}),
    ("/goals", {"name": "Goal", "targetMinor": -100}),
    ("/accounts", {"name": "Bank", "accountType": "bank", "currency": "US", "balanceMinor": 0}),
    ("/accounts", {"name": "Bank", "accountType": "bank", "currency": "INR", "balanceMinor": -2_147_483_649}),
])
def test_invalid_finance_forms_do_not_write(client, db, path, payload):
    assert client.post(path, json=payload).status_code == 422
    assert db.scalar(select(func.count()).select_from(Budget)) == 0
    assert db.scalar(select(func.count()).select_from(Goal)) == 0
    assert db.scalar(select(func.count()).select_from(Account)) == 0


def test_goal_contribution_rejects_total_overflow_before_writing(client, db):
    response = client.post("/goals", json={"name": "Savings", "targetMinor": 2_147_483_647})
    goal_id = response.json()["id"]
    goal = db.get(Goal, UUID(goal_id))
    goal.current_minor = 2_147_483_640
    db.commit()
    response = client.post(f"/goals/{goal_id}/contributions", json={"amountMinor": 100, "requestId": str(uuid4())})
    assert response.status_code == 422
    assert goal.current_minor == 2_147_483_640
    assert db.scalar(select(func.count()).select_from(GoalContribution)) == 0


def test_goal_contribution_key_collision_never_changes_either_owners_savings(client, db):
    other = User(email="savings-other@example.test", display_name="Other", currency="USD", timezone="UTC")
    db.add(other)
    db.flush()
    other_goal = Goal(user_id=other.id, name="Private", target_minor=100_000, current_minor=500, currency="USD")
    db.add(other_goal)
    db.flush()
    request_id = uuid4()
    db.add(GoalContribution(id=request_id, user_id=other.id, goal_id=other_goal.id, amount_minor=500, currency="USD", contribution_at=date(2026, 9, 8)))
    db.commit()
    own_goal = client.post("/goals", json={"name": "Mine", "targetMinor": 100_000}).json()
    response = client.post(f"/goals/{own_goal['id']}/contributions", json={"amountMinor": 100, "requestId": str(request_id)})
    assert response.status_code == 409
    assert client.get("/goals").json()[0]["currentMinor"] == 0
    assert db.get(Goal, other_goal.id).current_minor == 500
    assert db.scalar(select(func.count()).select_from(GoalContribution)) == 1
    # The failed insert leaves the session usable for a new entry.
    response = client.post(f"/goals/{own_goal['id']}/contributions", json={"amountMinor": 100, "requestId": str(uuid4())})
    assert response.status_code == 201
    assert response.json()["currentMinor"] == 100


def test_transaction_currency_is_saved_retained_on_edit_and_not_mixed_into_overview(client, db):
    payload = {"amountMinor": 1_250, "merchant": "Coffee", "transactionAt": "2026-09-08T10:00:00Z", "transactionType": "expense", "spendNature": "unknown", "currency": " usd "}
    response = client.post("/transactions", json=payload)
    assert response.status_code == 201, response.text
    transaction = response.json()
    assert transaction["currency"] == "USD"
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    assert user.currency == "INR"
    overview = overview_snapshot(db, user.id, date(2026, 9, 1), date(2026, 9, 8))
    assert overview["summary"]["spent_minor"] == 0
    assert client.get("/transactions").json()[0]["currency"] == "USD"
    changes = {**payload, "expectedVersion": transaction["rowVersion"], "currency": None, "amountMinor": 2_000}
    updated = client.patch(f"/transactions/{transaction['id']}", json=changes)
    assert updated.status_code == 200, updated.text
    assert updated.json()["currency"] == "USD"
    assert client.patch(f"/transactions/{transaction['id']}", json={**changes, "expectedVersion": updated.json()["rowVersion"], "currency": "INR"}).status_code == 422
    assert client.post("/transactions", json={**payload, "currency": "12!"}).status_code == 422
    assert client.post("/transactions", json={**payload, "currency": None}).json()["currency"] == "INR"
