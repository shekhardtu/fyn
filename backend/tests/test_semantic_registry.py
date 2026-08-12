from datetime import date, datetime

import pytest
from sqlalchemy import select

from app.models import Account, AccountBalanceSnapshot, Budget, Category, FinancialObservation, Goal, GoalContribution, InvestmentHolding, InvestmentValuationSnapshot, Loan, Transaction, User
from app.seed import default_user
from app.services.semantic import (
    FinanceFilter,
    FinanceQueryPlan,
    TimeGrouping,
    TimePivot,
    SemanticValidationError,
    execute_finance_query,
    validate_runtime_registry_coverage,
    validate_finance_query_plan,
)
from app.services.semantic_registry import semantic_schema_registry
from app.services.analytics import cash_position, category_breakdown, spending_summary


def _query(metric: str, **changes) -> FinanceQueryPlan:
    values = {
        "name": metric,
        "metric": metric,
        "start_date": date(2026, 8, 1),
        "end_date": date(2026, 8, 11),
    }
    values.update(changes)
    return FinanceQueryPlan(**values)


def test_registry_has_versioned_entities_relationships_and_physical_drift_checks():
    registry = semantic_schema_registry()
    assert registry.version == "2026-08-12.2"
    assert len(registry.schema_hash) == 64
    assert {"transactions", "accounts", "budgets", "goals", "loans", "subscriptions"} <= {
        entity.name for entity in registry.entities
    }
    assert {"transaction_category", "transaction_subcategory", "loan_account"} <= {
        relationship.name for relationship in registry.relationships
    }
    assert {
        relationship.name for relationship in registry.relationships if not relationship.queryable
    } == {"loan_account", "subscription_recurring", "transaction_merchant", "transaction_sources"}
    validate_runtime_registry_coverage()


def test_validator_rejects_unknown_or_disconnected_semantics():
    with pytest.raises(SemanticValidationError, match="Unknown governed metric"):
        validate_finance_query_plan(_query("invented_revenue"))
    with pytest.raises(SemanticValidationError, match="must use base entity"):
        validate_finance_query_plan(_query("gross_spend", entity="accounts"))
    with pytest.raises(SemanticValidationError, match="not valid for transactions"):
        validate_finance_query_plan(_query("gross_spend", dimensions=["lender"]))
    with pytest.raises(SemanticValidationError, match="not an approved path"):
        validate_finance_query_plan(_query("gross_spend", relationships=["loan_account"]))


def test_validator_enforces_operator_money_and_relationship_rules():
    with pytest.raises(SemanticValidationError, match="Operator contains"):
        validate_finance_query_plan(_query(
            "gross_spend",
            filters=[FinanceFilter(field="amount", operator="contains", value="200")],
        ))
    with pytest.raises(SemanticValidationError, match="integer minor units"):
        validate_finance_query_plan(_query(
            "gross_spend",
            filters=[FinanceFilter(field="amount", operator="gte", value=2.5)],
        ))
    with pytest.raises(SemanticValidationError, match="not required"):
        validate_finance_query_plan(_query(
            "gross_spend",
            relationships=["transaction_category"],
        ))


def test_snapshot_query_builder_supports_multiple_finance_entities_and_tenant_scope(db):
    user = default_user(db)
    other = User(email="semantic-other@example.com", display_name="Other")
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add(other)
    db.flush()
    db.add_all([
        Account(user_id=user.id, name="HDFC", account_type="bank", currency="INR", balance_minor=250_000),
        Account(user_id=other.id, name="Hidden", account_type="bank", currency="INR", balance_minor=9_999_999),
        Budget(user_id=user.id, category_id=food.id, name="Food cap", amount_minor=40_000, currency="INR", period="monthly"),
        Goal(user_id=user.id, name="Vacation", target_minor=200_000, current_minor=75_000, currency="INR"),
        Loan(user_id=user.id, name="Home loan", loan_type="home", lender="HDFC", outstanding_principal_minor=5_000_000, currency="INR", annual_rate_percent=8.5, remaining_tenure_months=120, current_emi_minor=55_000, status="active"),
    ])
    db.flush()

    balances = execute_finance_query(db, user.id, _query("account_balance", dimensions=["account"]))
    budgets = execute_finance_query(db, user.id, _query("budget_limit", dimensions=["category"]))
    goals = execute_finance_query(db, user.id, _query("goal_saved", dimensions=["goal"]))
    loans = execute_finance_query(db, user.id, _query("loan_outstanding", dimensions=["lender"]))

    assert balances["rows"] == [{"account": "HDFC", "value": 250_000}]
    assert budgets["rows"] == [{"category": "Food", "value": 40_000}]
    assert goals["rows"] == [{"goal": "Vacation", "value": 75_000}]
    assert loans["rows"] == [{"lender": "HDFC", "value": 5_000_000}]
    assert all(result["registry_version"] == "2026-08-12.2" for result in (balances, budgets, goals, loans))
    assert all(result["time_semantics"] == "current_snapshot" for result in (balances, budgets, goals, loans))


def test_finance_ontology_keeps_cash_flow_investment_and_transfer_semantics_distinct(db):
    user = default_user(db)
    db.add_all([
        Transaction(user_id=user.id, transaction_type="income", amount_minor=100_000, currency="INR", transaction_date=date(2026, 8, 11)),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=30_000, currency="INR", transaction_date=date(2026, 8, 11)),
        Transaction(user_id=user.id, transaction_type="investment", amount_minor=20_000, currency="INR", transaction_date=date(2026, 8, 11)),
        Transaction(user_id=user.id, transaction_type="refund", amount_minor=5_000, currency="INR", transaction_date=date(2026, 8, 11)),
        Transaction(user_id=user.id, transaction_type="transfer", amount_minor=500_000, currency="INR", transaction_date=date(2026, 8, 11)),
    ])
    db.flush()

    cash_flow = execute_finance_query(db, user.id, _query("net_cash_flow"))
    investment = execute_finance_query(db, user.id, _query("investment_contributions"))
    transfer = execute_finance_query(db, user.id, _query("transfer_volume"))

    assert cash_flow["rows"] == [{"value": 55_000}]
    assert investment["rows"] == [{"value": 20_000}]
    assert transfer["rows"] == [{"value": 500_000}]


def test_money_analytics_never_sum_different_currencies(db):
    user = default_user(db)
    user.currency = "USD"
    food = db.scalar(select(Category).where(Category.slug == "food"))
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=1_000, currency="USD", category_id=food.id, transaction_date=date(2026, 8, 11)),
        Transaction(user_id=user.id, transaction_type="income", amount_minor=5_000, currency="USD", transaction_date=date(2026, 8, 11)),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=9_999_999, currency="INR", category_id=food.id, transaction_date=date(2026, 8, 11)),
    ])
    db.flush()

    summary = spending_summary(db, user.id, date(2026, 8, 1), date(2026, 8, 11))
    breakdown = category_breakdown(db, user.id, date(2026, 8, 1), date(2026, 8, 11))
    position = cash_position(db, user.id)
    semantic = execute_finance_query(db, user.id, _query("gross_spend"))

    assert summary["total_minor"] == 1_000
    assert summary["currency"] == "USD"
    assert breakdown == [{"id": "food", "label": "Food", "amount_minor": 1_000, "count": 1, "currency": "USD"}]
    assert position == {"income_minor": 5_000, "expenses_minor": 1_000, "net_minor": 4_000, "currency": "USD"}
    assert semantic["rows"] == [{"value": 1_000}]
    assert semantic["currency"] == "USD"


def test_observation_analytics_are_tenant_scoped_and_not_labelled_canonical(db):
    user = default_user(db)
    other = User(email="observation-other@example.com", display_name="Other")
    db.add(other)
    db.flush()
    db.add_all([
        FinancialObservation(user_id=user.id, source_type="sms", source_hash="a" * 64, transaction_type="expense", amount_minor=2_000, currency="INR", transaction_date=date(2026, 8, 11), processing_state="attached"),
        FinancialObservation(user_id=user.id, source_type="email", source_hash="b" * 64, transaction_type="expense", amount_minor=2_000, currency="INR", transaction_date=date(2026, 8, 11), processing_state="attached"),
        FinancialObservation(user_id=other.id, source_type="bank", source_hash="c" * 64, transaction_type="expense", amount_minor=99_999, currency="INR", transaction_date=date(2026, 8, 11), processing_state="attached"),
    ])
    db.flush()

    result = execute_finance_query(db, user.id, _query("observation_count", dimensions=["source_type"]))

    assert {row["source_type"]: row["value"] for row in result["rows"]} == {"sms": 1, "email": 1}
    assert "not a count of canonical transactions" in result["metric_definition"].lower()


def test_historical_balance_portfolio_and_goal_facts_are_queryable(db):
    user = default_user(db)
    account = Account(user_id=user.id, name="Broker", account_type="investment", currency="INR", balance_minor=10_000)
    goal = Goal(user_id=user.id, name="Emergency fund", target_minor=500_000, current_minor=50_000, currency="INR")
    db.add_all([account, goal])
    db.flush()
    holding = InvestmentHolding(user_id=user.id, account_id=account.id, name="Index fund", symbol="INDEX", asset_type="mutual_fund", quantity=10, cost_basis_minor=100_000, current_value_minor=120_000, currency="INR")
    db.add(holding)
    db.flush()
    db.add_all([
        AccountBalanceSnapshot(user_id=user.id, account_id=account.id, balance_minor=10_000, currency="INR"),
        InvestmentValuationSnapshot(user_id=user.id, holding_id=holding.id, market_value_minor=120_000, cost_basis_minor=100_000, currency="INR"),
        GoalContribution(user_id=user.id, goal_id=goal.id, amount_minor=50_000, currency="INR", contribution_date=date(2026, 8, 11)),
    ])
    db.flush()

    portfolio = execute_finance_query(db, user.id, _query("portfolio_value", dimensions=["asset_type"]))
    balances = execute_finance_query(db, user.id, _query("historical_account_balance", dimensions=["account"]))
    contributions = execute_finance_query(db, user.id, _query("goal_contribution_amount", dimensions=["goal"]))

    assert portfolio["rows"] == [{"asset_type": "mutual_fund", "value": 120_000}]
    assert balances["rows"] == [{"account": "Broker", "value": 10_000}]
    assert contributions["rows"] == [{"goal": "Emergency fund", "value": 50_000}]


def test_event_and_snapshot_time_semantics_are_explicit():
    event = validate_finance_query_plan(_query("gross_spend"))
    snapshot = validate_finance_query_plan(_query("loan_outstanding"))
    assert event["entity"] == "transactions"
    assert event["time_semantics"] == "event_window"
    assert snapshot["entity"] == "loans"
    assert snapshot["time_semantics"] == "current_snapshot"


def test_generic_time_grouping_buckets_hours_and_excludes_unknown_times(db):
    user = User(email="temporal-query@example.com", display_name="Temporal")
    db.add(user)
    db.flush()
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", transaction_date=date(2026, 8, 11), transaction_time="10:05:00", timezone="Asia/Kolkata"),
        Transaction(user_id=user.id, transaction_type="income", amount_minor=20_000, currency="INR", transaction_date=date(2026, 8, 11), transaction_time="10:50:00", timezone="Asia/Kolkata"),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=30_000, currency="INR", transaction_date=date(2026, 8, 11), transaction_time="11:15:00", timezone="Asia/Kolkata"),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=40_000, currency="INR", transaction_date=date(2026, 8, 11), transaction_time=None, timezone="Asia/Kolkata"),
    ])
    db.flush()

    hourly = execute_finance_query(db, user.id, _query(
        "transaction_count",
        start_datetime=datetime(2026, 8, 11, 9),
        end_datetime=datetime(2026, 8, 11, 12),
        time_grouping=TimeGrouping(grain="hour", timezone="Asia/Kolkata"),
        order="asc",
    ))
    daily = execute_finance_query(db, user.id, _query(
        "transaction_count",
        time_grouping=TimeGrouping(grain="day", timezone="Asia/Kolkata"),
        order="asc",
    ))
    pivot = execute_finance_query(db, user.id, _query(
        "transaction_amount",
        dimensions=["transaction_type"],
        time_pivot=TimePivot(row_grain="day", column_component="hour_of_day", timezone="Asia/Kolkata"),
        order="asc",
    ))

    assert hourly["dimensions"] == ["time_bucket"]
    assert hourly["rows"] == [
        {"time_bucket": "2026-08-11 10:00", "value": 2},
        {"time_bucket": "2026-08-11 11:00", "value": 1},
    ]
    assert hourly["requires_transaction_time"] is True
    assert daily["rows"] == [{"time_bucket": "2026-08-11", "value": 4}]
    assert daily["requires_transaction_time"] is False
    assert pivot["dimensions"] == ["transaction_type", "time_bucket", "time_segment"]
    assert pivot["time_pivot"]["column_component"] == "hour_of_day"
    assert {
        (row["transaction_type"], row["time_bucket"], row["time_segment"]): row["value"]
        for row in pivot["rows"]
    } == {
        ("expense", "2026-08-11", "10"): 10_000,
        ("income", "2026-08-11", "10"): 20_000,
        ("expense", "2026-08-11", "11"): 30_000,
    }


def test_governed_time_grouping_zero_fills_bounded_additive_series(db):
    user = User(email="gap-fill@example.com", display_name="Gap Fill")
    db.add(user)
    db.flush()
    db.add(Transaction(
        user_id=user.id,
        transaction_type="expense",
        amount_minor=12_300,
        currency="INR",
        transaction_date=date(2026, 8, 10),
    ))
    db.flush()

    result = execute_finance_query(db, user.id, _query(
        "gross_spend",
        start_date=date(2026, 8, 9),
        end_date=date(2026, 8, 11),
        time_grouping=TimeGrouping(grain="day", timezone="Asia/Kolkata", fill_gaps=True),
        order="asc",
        limit=10,
    ))

    assert result["rows"] == [
        {"time_bucket": "2026-08-09", "value": 0},
        {"time_bucket": "2026-08-10", "value": 12_300},
        {"time_bucket": "2026-08-11", "value": 0},
    ]
    with pytest.raises(SemanticValidationError, match="limited to additive metrics"):
        validate_finance_query_plan(_query(
            "average_expense",
            time_grouping=TimeGrouping(grain="day", timezone="Asia/Kolkata", fill_gaps=True),
        ))


def test_negative_category_filters_are_governed_and_preserve_uncategorized_rows(db):
    user = default_user(db)
    food = db.scalar(select(Category).where(Category.slug == "food"))
    transport = db.scalar(select(Category).where(Category.slug == "transport"))
    db.add_all([
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=30_000, currency="INR", category_id=food.id, transaction_date=date(2026, 8, 10)),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=20_000, currency="INR", category_id=transport.id, transaction_date=date(2026, 8, 10)),
        Transaction(user_id=user.id, transaction_type="expense", amount_minor=10_000, currency="INR", transaction_date=date(2026, 8, 10)),
    ])
    db.flush()

    result = execute_finance_query(db, user.id, _query(
        "gross_spend",
        dimensions=["category"],
        filters=[FinanceFilter(field="category", operator="neq", value="food")],
    ))
    assert result["rows"] == [
        {"category": "Transport", "value": 20_000},
        {"category": "Uncategorized", "value": 10_000},
    ]

    result = execute_finance_query(db, user.id, _query(
        "gross_spend",
        dimensions=["category"],
        filters=[FinanceFilter(field="category", operator="not_in", value=["food", "transport"])],
    ))
    assert result["rows"] == [{"category": "Uncategorized", "value": 10_000}]
