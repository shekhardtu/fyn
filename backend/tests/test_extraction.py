from datetime import date

import pytest

from app.services.extraction import extract_transaction, looks_like_financial_query, parse_amount_minor, parse_spending_period


@pytest.mark.parametrize(("text", "minor"), [
    ("₹2,000", 200_000),
    ("Salary of ₹3 lakh", 30_000_000),
    ("received Rs. 50,000", 5_000_000),
    ("invested 20k in mutual funds", 2_000_000),
    ("200 rupees for ice cream", 20_000),
    ("200 rupess for ice cream", 20_000),
])
def test_amounts_use_minor_units(text, minor):
    assert parse_amount_minor(text) == minor


def test_transaction_currency_inherits_user_setting_and_explicit_code_wins():
    assert extract_transaction("Spent 10 on coffee", default_currency="USD").currency == "USD"
    assert extract_transaction("Spent ₹10 on coffee", default_currency="USD").currency == "INR"


@pytest.mark.parametrize(("text", "transaction_type", "merchant", "category", "subcategory"), [
    ("Spent ₹2,000 at Toit last night", "expense", "Toit", "food", "dining"),
    ("Paid ₹850 at Swiggy for dinner", "expense", "Swiggy", "food", "delivery"),
    ("Got my salary of ₹3 lakh today", "income", None, "income", "salary"),
    ("Received ₹50,000 from a freelance project", "income", None, "income", "freelance"),
    ("Invested ₹20,000 in mutual funds", "investment", None, "investment", "mutual_fund"),
    ("Paid ₹45,000 toward my home loan", "loan_payment", None, None, None),
    ("200 rupees for ice cream", "expense", None, "food", "ice_cream"),
    ("200 rupess for ice cream", "expense", None, "food", "ice_cream"),
])
def test_structured_extraction(text, transaction_type, merchant, category, subcategory):
    result = extract_transaction(text, today=date(2026, 8, 10))
    assert result.transaction_type == transaction_type
    assert result.merchant == merchant
    assert result.category_slug == category
    assert result.subcategory_slug == subcategory


def test_relative_date_and_inference_provenance():
    result = extract_transaction("Spent ₹2,000 at Toit yesterday", today=date(2026, 8, 10))
    assert result.transaction_date == date(2026, 8, 9)
    assert "transaction_date" not in result.inferred_fields
    assert "category" in result.inferred_fields


def test_bare_amount_minimizes_questions():
    result = extract_transaction("₹2,000", today=date(2026, 8, 10))
    assert result.transaction_type == "expense"
    assert result.missing_fields == ["category"]
    assert "transaction_type" in result.inferred_fields


def test_payment_success_phrase_is_not_part_of_merchant():
    result = extract_transaction("Your payment of ₹2,000 at Toit was successful today")
    assert result.merchant == "Toit"
    assert result.amount_minor == 200_000


@pytest.mark.parametrize("text", [
    "How many rupees spend we last two days?",
    "How much did I spend today?",
    "What were my expenses yesterday?",
    "Show spending for the last 7 days",
    "List my transactions this month",
])
def test_financial_questions_are_classified_as_queries(text):
    assert looks_like_financial_query(text)


@pytest.mark.parametrize("text", [
    "₹200",
    "200 rupees for ice cream",
    "Spent ₹500 this morning",
    "I paid ₹900 at Toit",
])
def test_transaction_statements_are_not_classified_as_queries(text):
    assert not looks_like_financial_query(text)


@pytest.mark.parametrize(("text", "start", "end", "label"), [
    ("last two days", date(2026, 8, 9), date(2026, 8, 10), "Last 2 days"),
    ("past 7 days", date(2026, 8, 4), date(2026, 8, 10), "Last 7 days"),
    ("yesterday", date(2026, 8, 9), date(2026, 8, 9), "Yesterday"),
    ("last month", date(2026, 7, 1), date(2026, 7, 31), "Last month"),
    ("this year", date(2026, 1, 1), date(2026, 8, 10), "This year"),
])
def test_spending_periods_are_deterministic(text, start, end, label):
    assert parse_spending_period(text, date(2026, 8, 10)) == (start, end, label)
