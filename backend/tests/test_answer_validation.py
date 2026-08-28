from __future__ import annotations

from app.services.agents import ToolGrounding
from app.services.answer_validation import (
    ObligationCode,
    compile_answer_contract,
    validate_coverage,
    validate_evidence,
)


def _sql_grounding(rows: list[dict], *, arguments: dict | None = None) -> ToolGrounding:
    return ToolGrounding(
        name="run_governed_sql",
        arguments=arguments or {},
        result={
            "tool": "run_governed_sql",
            "data": {
                "kind": "governed_sql",
                "columns": list(rows[0]) if rows else [],
                "rows": rows,
                "row_count": len(rows),
                "limit": 100,
            },
        },
    )


def test_typed_evidence_does_not_treat_arguments_or_row_counts_as_money():
    grounding = [_sql_grounding([], arguments={"sql": "SELECT 999", "limit": 999})]

    validation = validate_evidence("You spent ₹999 and ₹0.", grounding)

    assert not validation.passed
    assert {claim.value for claim in validation.unsupported} == {999, 0}


def test_typed_evidence_converts_minor_units_and_declared_percentage_rounding():
    grounding = [_sql_grounding([{
        "category": "Food",
        "august_minor": 1_244_000,
        "change_pct": 68.65,
    }])]

    validation = validate_evidence(
        "Food spending was ₹12,440, an increase of 68.7%.",
        grounding,
    )

    assert validation.passed


def test_dimension_scope_does_not_leak_between_tools():
    taxonomy = ToolGrounding(
        name="read_user_expense_taxonomy",
        arguments={},
        result={
            "tool": "read_user_expense_taxonomy",
            "data": [{"name": "Food", "subcategories": []}],
        },
    )
    sql = _sql_grounding([{"total_minor": 1_200_000}])

    assert validate_evidence("Total Food spending was ₹12,000.", [taxonomy, sql]).passed


def test_negative_currency_prefix_retains_its_sign():
    grounding = [_sql_grounding([{
        "day": "2026-08-20",
        "change_from_previous_day_minor": -894_500,
    }])]

    assert validate_evidence("| Aug 20 | -₹8,945 |", grounding).passed
    assert not validate_evidence("| Aug 20 | -₹8,944 |", grounding).passed


def test_count_parser_does_not_treat_accounted_as_an_account_unit():
    grounding = [_sql_grounding([{
        "day": "2026-08-24",
        "share_percent": 24.19,
    }])]

    validation = validate_evidence(
        "August 24 accounted for 24.19% of spending.",
        grounding,
    )

    assert validation.passed
    assert [claim.kind.value for claim in validation.claims] == ["percent"]


def test_complex_comparison_contract_requires_evidence_and_answer_coverage():
    question = (
        "Compare my Food and Travel spending from May through August 19, "
        "group it by month, identify the largest merchants, and compare it "
        "with the previous three-month average."
    )
    contract = compile_answer_contract(question)
    grounding = [_sql_grounding([{
        "category": "Food",
        "may_minor": 700_000,
        "june_minor": 720_000,
        "july_minor": 760_000,
        "august_minor": 1_244_000,
        "previous_average_minor": 726_667,
        "difference_minor": 517_333,
        "largest_merchant": "Fresh Foods",
        "merchant_amount_minor": 500_000,
    }, {
        "category": "Travel",
        "may_minor": 900_000,
        "june_minor": 950_000,
        "july_minor": 1_000_000,
        "august_minor": 1_754_000,
        "previous_average_minor": 950_000,
        "difference_minor": 804_000,
        "largest_merchant": "City Cabs",
        "merchant_amount_minor": 800_000,
    }])]
    evidence = validate_evidence("", grounding)
    complete = (
        "Travel has the larger increase.\n\n"
        "| Category | May | June | July | August 1–19 | Previous average | Difference | Top merchant |\n"
        "|---|---:|---:|---:|---:|---:|---:|---|\n"
        "| Food | ₹7,000 | ₹7,200 | ₹7,600 | ₹12,440 | ₹7,266.67 | ₹5,173.33 | Fresh Foods |\n"
        "| Travel | ₹9,000 | ₹9,500 | ₹10,000 | ₹17,540 | ₹9,500 | ₹8,040 | City Cabs |\n\n"
        "The previous average compares August through day 19 with the same elapsed days in May, June, and July."
    )

    coverage = validate_coverage(complete, contract, evidence.facts)

    assert coverage.passed
    assert {item.code for item in contract.obligations} == {
        ObligationCode.MONTHLY_BREAKDOWN,
        ObligationCode.MERCHANT_DRIVERS,
        ObligationCode.HISTORICAL_AVERAGE,
        ObligationCode.ABSOLUTE_COMPARISON,
        ObligationCode.RANKING,
        ObligationCode.PARTIAL_PERIOD_ALIGNMENT,
    }


def test_complex_comparison_contract_reports_query_gaps_before_prose_gaps():
    question = (
        "Compare Food and Travel from May through August 19, group by month, "
        "identify the largest merchants, and compare with the previous three-month average."
    )
    contract = compile_answer_contract(question)
    grounding = [_sql_grounding([{
        "month": "2026-08",
        "category": "Food",
        "total_minor": 1_244_000,
    }])]
    evidence = validate_evidence("August Food was ₹12,440.", grounding)

    coverage = validate_coverage("August Food was ₹12,440.", contract, evidence.facts)

    assert ObligationCode.MONTHLY_BREAKDOWN in {
        item.code for item in coverage.missing_evidence
    }
    assert ObligationCode.MERCHANT_DRIVERS in {
        item.code for item in coverage.missing_evidence
    }
    assert ObligationCode.HISTORICAL_AVERAGE in {
        item.code for item in coverage.missing_evidence
    }


def test_long_form_dimension_rows_prove_merchant_coverage():
    question = "How was Food spending distributed across days, merchants, and subcategories?"
    grounding = [_sql_grounding([
        {
            "dimension": "Day",
            "group_name": "2026-08-24",
            "spend_minor": 3_337_200,
        },
        {
            "dimension": "Merchant",
            "group_name": "Swiggy",
            "spend_minor": 5_205_000,
        },
        {
            "dimension": "Subcategory",
            "group_name": "Dining",
            "spend_minor": 7_369_200,
        },
    ])]
    answer = (
        "Food spending was ₹33,372 on August 24. "
        "Swiggy was the largest merchant at ₹52,050. Dining was ₹73,692."
    )
    evidence = validate_evidence(answer, grounding, question)

    assert evidence.passed
    assert validate_coverage(
        answer,
        compile_answer_contract(question),
        evidence.facts,
    ).passed


def test_long_form_sql_breakdown_proves_distinct_group_count():
    grounding = [_sql_grounding([
        {
            "breakdown": "overall",
            "label": "All Food",
            "spend_date": None,
            "merchant": None,
            "total_minor": 2_000,
        },
        {
            "breakdown": "day",
            "label": "2026-08-24",
            "spend_date": "2026-08-24",
            "merchant": None,
            "total_minor": 1_200,
        },
        {
            "breakdown": "day",
            "label": "2026-08-25",
            "spend_date": "2026-08-25",
            "merchant": None,
            "total_minor": 800,
        },
        {
            "breakdown": "merchant",
            "label": "Swiggy",
            "spend_date": None,
            "merchant": "Swiggy",
            "total_minor": 2_000,
        },
    ])]

    assert validate_evidence("Spending occurred on 2 days.", grounding).passed
    assert not validate_evidence("Spending occurred on 3 days.", grounding).passed


def test_result_level_rows_prove_merchant_coverage():
    question = "Which merchants contributed most to the net total?"
    grounding = [_sql_grounding([
        {
            "result_level": "overall",
            "label": "All categories",
            "net_spending_minor": 10_000,
        },
        {
            "result_level": "merchant",
            "label": "Swiggy",
            "net_spending_minor": 7_000,
        },
    ])]
    evidence = validate_evidence("Swiggy contributed ₹70.", grounding, question)

    assert evidence.passed
    assert validate_coverage(
        "Swiggy contributed ₹70.",
        compile_answer_contract(question),
        evidence.facts,
    ).passed


def test_matching_independent_breakdowns_prove_reconciled_total():
    grounding = [_sql_grounding([
        {"section": "category", "name": "Food", "net_spending_minor": 13_797_000},
        {"section": "category", "name": "Other", "net_spending_minor": 2_040_000},
        {"section": "merchant", "name": "Swiggy", "net_spending_minor": 5_407_800},
        {"section": "merchant", "name": "Unknown", "net_spending_minor": 4_411_400},
        {"section": "merchant", "name": "Toit", "net_spending_minor": 3_977_800},
        {"section": "merchant", "name": "Swiggy Online", "net_spending_minor": 2_040_000},
    ])]

    assert validate_evidence("Net spending totaled ₹1,58,370.", grounding).passed
    assert validate_evidence(
        "These four merchant groups account for the full ₹1,58,370 net total.",
        grounding,
    ).passed
    assert not validate_evidence("Net spending totaled ₹1,58,371.", grounding).passed


def test_taxonomy_list_cardinality_is_typed_as_categories_not_months():
    grounding = [ToolGrounding(
        name="read_user_expense_taxonomy",
        arguments={},
        result={
            "tool": "read_user_expense_taxonomy",
            "data": [
                {"slug": "food", "name": "Food", "subcategories": []},
                {"slug": "travel", "name": "Travel", "subcategories": []},
            ],
        },
    )]

    assert validate_evidence("You have 2 expense categories.", grounding).passed
    assert not validate_evidence("The loan takes 2 months.", grounding).passed


def test_empty_transaction_result_supports_zero_and_the_scoped_absence():
    grounding = [ToolGrounding(
        name="transaction_list",
        arguments={},
        result={
            "tool": "transaction_list",
            "data": {
                "rows": [],
                "returned": 0,
                "total_minor": 0,
                "total": "₹0",
                "currency": "INR",
            },
        },
    )]
    answer = "No matching expenses were recorded. The search found 0 transactions totaling ₹0."
    evidence = validate_evidence(answer, grounding)
    coverage = validate_coverage(
        answer,
        compile_answer_contract("Show expenses at the merchant Acme."),
        evidence.facts,
    )

    assert evidence.passed
    assert coverage.passed


def test_unfulfilled_requested_count_is_not_an_affirmative_count_claim():
    grounding = [_sql_grounding([])]
    grounding[0].result.data["empty_result"] = True
    question = "Show the three largest category drivers."
    answer = "No records were found, so I can’t show three category drivers."

    assert validate_evidence(answer, grounding, question).passed


def test_authoritative_empty_sql_result_fulfils_comparison_with_an_absence_answer():
    grounding = [_sql_grounding([])]
    grounding[0].result.data["empty_result"] = True
    answer = "No recorded expenses were available, so spending was ₹0 and the months cannot be compared."
    evidence = validate_evidence(answer, grounding)
    contract = compile_answer_contract(
        "Compare the last three full months and identify the highest category."
    )

    assert validate_coverage(answer, contract, evidence.facts).passed
    assert not validate_coverage(
        "August was higher than July.", contract, evidence.facts
    ).passed


def test_empty_ranked_subgroup_fulfils_only_the_ranking_obligation():
    grounding = [_sql_grounding([{
        "row_type": "month_summary",
        "month": "2026-07",
        "discretionary_minor": 0,
        "category": None,
        "category_reduction_minor": None,
        "rank": None,
    }])]
    answer = (
        "July discretionary spending was ₹0. There are no categories to rank, "
        "so no category reduction is available."
    )
    evidence = validate_evidence(answer, grounding)
    ranking_contract = compile_answer_contract(
        "Identify the categories with the largest potential reductions."
    )
    merchant_contract = compile_answer_contract(
        "Identify the merchants with the largest potential reductions."
    )

    assert evidence.passed
    assert validate_coverage(answer, ranking_contract, evidence.facts).passed
    assert not validate_coverage(answer, merchant_contract, evidence.facts).passed


def test_null_rank_without_a_ranked_entity_is_not_empty_ranking_proof():
    grounding = [_sql_grounding([{
        "month": "2026-07",
        "total_minor": 0,
        "rank": None,
    }])]
    answer = "There is nothing to rank."

    evidence = validate_evidence(answer, grounding)

    assert not validate_coverage(
        answer,
        compile_answer_contract("Identify the category with the largest spend."),
        evidence.facts,
    ).passed


def test_semantic_month_to_date_category_rank_satisfies_top_category_coverage():
    grounding = [ToolGrounding(
        name="analyze_month_to_date_spending",
        arguments={},
        result={
            "tool": "analyze_month_to_date_spending",
            "data": {
                "kind": "semantic_financial_analysis",
                "currency": "INR",
                "total_minor": 100_000,
                "categories": [{
                    "category": "Food",
                    "amount_minor": 100_000,
                    "category_rank": 1,
                }],
            },
        },
    )]
    answer = "Net month-to-date spending was ₹1,000. The top category was Food at ₹1,000."
    evidence = validate_evidence(answer, grounding)
    contract = compile_answer_contract(
        "How much did I spend this month? Give the exact net total and the top category."
    )

    assert evidence.passed
    assert validate_coverage(answer, contract, evidence.facts).passed


def test_summary_rows_prove_ranked_subgroup_empty_when_reduction_shape_is_explicit():
    grounding = [_sql_grounding([{
        "row_type": "MONTH",
        "label": "2026-07",
        "historical_average_minor": 0,
        "fixed_monthly_cap_minor": 0,
        "reduction_minor": None,
    }])]
    grounding[0].result.data["answer_contract"] = ["historical_average", "ranking"]
    answer = "The historical average and cap are ₹0. No categories are available to rank."
    evidence = validate_evidence(answer, grounding)

    assert evidence.passed
    assert validate_coverage(
        answer,
        compile_answer_contract(
            "Calculate a cap below my historical average and show the largest category reductions."
        ),
        evidence.facts,
    ).passed


def test_zero_valued_range_is_valid_absolute_comparison_evidence():
    grounding = [_sql_grounding([{
        "category": "Uncategorized",
        "may_spending_minor": 0,
        "june_spending_minor": 0,
        "range_minor": 0,
    }])]
    answer = "No recorded expenses were available, so May and June cannot be compared."
    evidence = validate_evidence(answer, grounding)

    assert validate_coverage(
        answer,
        compile_answer_contract("Compare May and June spending."),
        evidence.facts,
    ).passed


def test_a_loan_rate_parameter_does_not_require_percentage_comparison():
    contract = compile_answer_contract(
        "For a ₹12 lakh loan at 8% over 5 years, compare interest with a prepayment."
    )
    explicit = compile_answer_contract(
        "Calculate the percentage decrease in interest after the prepayment."
    )

    assert ObligationCode.PERCENTAGE_COMPARISON not in {
        item.code for item in contract.obligations
    }
    assert ObligationCode.PERCENTAGE_COMPARISON in {
        item.code for item in explicit.obligations
    }


def test_calculator_result_inputs_can_be_repeated_as_scenario_terms():
    grounding = [ToolGrounding(
        name="run_financial_calculator",
        arguments={},
        result={
            "tool": "run_financial_calculator",
            "data": {
                "kind": "deterministic_financial_calculation",
                "calculator": "loan_payment",
                "inputs": {
                    "principal_minor": 120_000_000,
                    "annual_rate_percent": 8,
                    "tenure_months": 60,
                },
                "result": {
                    "emi_minor": 2_433_167,
                    "total_interest_minor": 25_990_039,
                },
                "display": {
                    "emi_minor": "₹24,331.67",
                    "total_interest_minor": "₹2,59,900.39",
                },
            },
        },
    )]

    validation = validate_evidence(
        "On a ₹12,00,000 loan at 8% for 60 months, the EMI is ₹24,331.67 (about ₹24,332).",
        grounding,
    )

    assert validation.passed
    assert validate_evidence(
        "For a ₹12 lakh loan, the EMI is ₹24,331.67.",
        grounding,
    ).passed


def test_requested_percentage_cap_is_safe_to_repeat_as_an_input():
    grounding = [_sql_grounding([{
        "historical_average_minor": 0,
        "fixed_monthly_cap_minor": 0,
    }])]

    assert validate_evidence(
        "The fixed cap at 10% below the average is ₹0.",
        grounding,
        "Calculate a cap that is 10% below my historical average.",
    ).passed


def test_timed_prepayment_result_supports_scaled_input_and_savings_language():
    grounding = [ToolGrounding(
        name="run_financial_calculator",
        arguments={},
        result={
            "tool": "run_financial_calculator",
            "data": {
                "kind": "deterministic_financial_calculation",
                "calculator": "loan_with_timed_prepayment",
                "inputs": {
                    "principal_minor": 120_000_000,
                    "prepayment_minor": 10_000_000,
                    "annual_rate_percent": 8,
                    "tenure_months": 60,
                    "prepayment_after_months": 12,
                },
                "result": {
                    "emi_minor": 2_433_167,
                    "prepayment_minor": 10_000_000,
                    "baseline_total_interest_minor": 25_990_043,
                    "with_prepayment_total_interest_minor": 22_443_325,
                    "interest_saved_minor": 3_546_718,
                    "baseline_tenure_months": 60,
                    "with_prepayment_tenure_months": 55,
                    "months_saved": 5,
                },
            },
        },
    )]
    question = (
        "For a ₹12 lakh loan at 8% over 5 years, compare interest with and without "
        "a ₹1 lakh prepayment after 12 months."
    )
    answer = (
        "A ₹1 lakh prepayment saves ₹35,467.18: interest falls from ₹2,59,900.43 "
        "to ₹2,24,433.25 and the loan is 5 months shorter."
    )
    evidence = validate_evidence(answer, grounding, question)

    assert evidence.passed
    assert validate_coverage(
        answer,
        compile_answer_contract(question),
        evidence.facts,
    ).passed


def test_computed_field_labels_do_not_accidentally_scope_row_evidence():
    grounding = [ToolGrounding(
        name="run_financial_calculator",
        arguments={},
        result={
            "tool": "run_financial_calculator",
            "data": {
                "kind": "deterministic_financial_calculation",
                "calculator": "schedule",
                "result": {
                    "kind": "computed_dataset",
                    "fields": [{
                        "name": "payment_minor",
                        "label": "EMI",
                        "role": "measure",
                        "type": "quantitative",
                    }],
                    "rows": [{"payment_minor": 2_433_167}],
                    "summary": {"payment_minor": 2_433_167},
                },
            },
        },
    )]

    assert validate_evidence("The EMI is ₹24,331.67.", grounding).passed
