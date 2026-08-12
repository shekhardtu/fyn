from app.services.calculators import affordability, investment_projection, loan_amortization_schedule, loan_payment, loan_strategy_options, loan_with_prepayment


def test_zero_interest_loan_is_exact():
    result = loan_payment(1_200_000, 0, 12)
    assert result["emi_minor"] == 100_000
    assert result["total_interest_minor"] == 0


def test_prepayment_never_increases_interest():
    result = loan_with_prepayment(50_000_000, 8.5, 240, 5_000_000)
    assert result["interest_saved_minor"] > 0
    assert result["after_prepayment"]["total_interest_minor"] < result["baseline"]["total_interest_minor"]


def test_full_prepayment_has_one_canonical_zero_tenure_result():
    comparison = loan_with_prepayment(1_000_000, 8.5, 120, 1_000_000)
    strategy = loan_strategy_options(1_000_000, 8.5, 120, 1_000_000)

    assert comparison["after_prepayment"]["tenure_months"] == 0
    assert strategy["lower_emi"] == comparison["after_prepayment"] | {
        "interest_saved_minor": comparison["baseline"]["total_interest_minor"],
    }


def test_amortization_schedule_is_complete_typed_and_clears_principal():
    result = loan_amortization_schedule(520_000_000, 7.2, 240)

    assert result["kind"] == "computed_dataset"
    assert len(result["rows"]) == 240
    assert result["rows"][0]["installment"] == 1
    assert result["rows"][-1]["installment"] == 240
    assert result["rows"][-1]["remaining_principal_minor"] == 0
    assert sum(row["principal_payment_minor"] for row in result["rows"]) == 520_000_000
    assert result["default_dimension"] == "installment"
    assert result["default_measures"] == ["principal_payment_minor", "remaining_principal_minor"]


def test_investment_projection_uses_decimal_math():
    result = investment_projection(2_000_000, 0, 12, 10)
    assert result["projected_value_minor"] > result["invested_minor"] == 240_000_000


def test_affordability_preserves_emergency_fund():
    result = affordability(20_000_000, 30_000_000, 5_000_000, 2_000_000, 6)
    assert result["emergency_reserve_minor"] == 12_000_000
    assert result["affordable_now"] is False
    assert result["gap_minor"] == 2_000_000
