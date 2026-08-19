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
