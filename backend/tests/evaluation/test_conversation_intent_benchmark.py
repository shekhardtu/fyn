import pytest

from app.services.extraction import extract_transaction
from app.services.turn_signals import looks_like_financial_query


ENTRY_CASES = [
    ("₹200", "expense", 20_000, None, None),
    ("Rs 1,500 for lunch", "expense", 150_000, "food", "dining"),
    ("200 rupess for ice cream", "expense", 20_000, "food", "ice_cream"),
    ("Paid ₹850 at Swiggy for dinner", "expense", 85_000, "food", "delivery"),
    ("Salary ₹3 lakh credited today", "income", 30_000_000, "income", "salary"),
    ("Moved ₹30,000 from HDFC to SBI", "transfer", 3_000_000, None, None),
    ("Invested ₹20,000 in mutual funds", "investment", 2_000_000, "investment", "mutual_fund"),
    ("Received a ₹500 refund", "refund", 50_000, None, None),
    ("Spent ₹500 this morning", "expense", 50_000, None, None),
]

QUERY_CASES = [
    "How many rupees spend we last two days?",
    "How much did I spend on Travelling this month?",
    "How much did I spend on food this month?",
    "Why did I spend more this month?",
    "Compare this month with last month",
    "What was my biggest expense?",
    "Show my recurring expenses",
    "Show spending for the last 7 days",
    "List my transactions this month",
]


@pytest.mark.parametrize(("text", "transaction_type", "amount_minor", "category", "subcategory"), ENTRY_CASES)
def test_entry_extraction_benchmark(text, transaction_type, amount_minor, category, subcategory):
    assert not looks_like_financial_query(text)
    result = extract_transaction(text)
    assert (result.transaction_type, result.amount_minor, result.category_slug, result.subcategory_slug) == (
        transaction_type,
        amount_minor,
        category,
        subcategory,
    )


@pytest.mark.parametrize("text", QUERY_CASES)
def test_query_classification_benchmark(text):
    assert looks_like_financial_query(text)
