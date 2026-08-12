from datetime import datetime, timezone

from app.services.adapters import CSVAdapter, MessageAdapter, classify_financial_message


def test_order_confirmation_is_not_a_transaction():
    kind, relevant, reason = classify_financial_message("Your Amazon order total is ₹2,000. It will ship tomorrow.")
    assert kind == "order_confirmation"
    assert relevant is False
    assert "not proof" in reason


def test_otp_is_never_ingested():
    result = MessageAdapter("sms").adapt_message("123456 is your OTP for a ₹2,000 purchase", "otp-1")
    assert result.classification == "otp"
    assert result.relevant is False
    assert result.observation is None


def test_bank_debit_message_becomes_observation():
    result = MessageAdapter("sms").adapt_message("₹2,000 debited at TOIT today", "sms-1")
    assert result.relevant is True
    assert result.observation.amount_minor == 200_000
    assert result.observation.transaction_type == "expense"
    assert result.observation.merchant == "TOIT"


def test_csv_adapter_normalizes_common_bank_columns():
    content = b"Transaction Date,Narration,Debit Amount,Credit Amount,Reference\n10/08/2026,TOIT POS,2000,,abc-1\n10/08/2026,SALARY,,300000,abc-2\n"
    rows = CSVAdapter().adapt(content)
    assert len(rows) == 2
    assert rows[0][1].amount_minor == 200_000
    assert rows[0][1].transaction_type == "expense"
    assert rows[1][1].amount_minor == 30_000_000
    assert rows[1][1].transaction_type == "income"
    assert rows[0][1].transaction_at == datetime(2026, 8, 9, 18, 30, tzinfo=timezone.utc)
