from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, getcontext

from .agent_tools import tool_contract
from .tool_models import (
    AffordabilityInput,
    AffordabilityResult,
    ComputedField,
    ComputedDatasetResult,
    FixedPaymentInput,
    FixedPaymentResult,
    InvestmentProjectionInput,
    InvestmentProjectionResult,
    LoanPaymentInput,
    LoanPaymentResult,
    LoanPrepaymentResult,
    LoanStrategyInput,
    LoanStrategyResult,
    LoanWithPrepaymentInput,
    TimedLoanPrepaymentInput,
    TimedLoanPrepaymentResult,
)

getcontext().prec = 28
CENT = Decimal("1")
MONTHLY_PERCENT_DIVISOR = Decimal("1200")

LOAN_AMORTIZATION_FIELDS = (
    ComputedField(name="installment", label="Installment", type="ordinal", value_type="number", role="dimension"),
    ComputedField(name="payment_minor", label="EMI", type="quantitative", value_type="money_minor", role="measure"),
    ComputedField(name="principal_payment_minor", label="Principal paid", type="quantitative", value_type="money_minor", role="measure"),
    ComputedField(name="interest_payment_minor", label="Interest paid", type="quantitative", value_type="money_minor", role="measure"),
    ComputedField(name="remaining_principal_minor", label="Outstanding principal", type="quantitative", value_type="money_minor", role="measure"),
)


def _minor(value: Decimal) -> int:
    return int(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _monthly_rate(annual_percent: float) -> Decimal:
    return Decimal(str(annual_percent)) / MONTHLY_PERCENT_DIVISOR


def _loan_payment_result(
    emi: Decimal | int,
    total_payment: Decimal | int,
    total_interest: Decimal | int,
    tenure_months: int,
) -> dict:
    return {
        "emi_minor": _minor(Decimal(emi)),
        "total_payment_minor": _minor(Decimal(total_payment)),
        "total_interest_minor": _minor(Decimal(total_interest)),
        "tenure_months": tenure_months,
    }


@tool_contract(description=(
    "Calculate a deterministic loan EMI, total payment, and total interest from principal in minor "
    "units, annual rate percent, and tenure in months."
), input_model=LoanPaymentInput, output_model=LoanPaymentResult)
def loan_payment(principal_minor: int, annual_rate_percent: float, tenure_months: int) -> dict:
    if principal_minor <= 0:
        raise ValueError("Principal must be greater than zero")
    if tenure_months <= 0:
        raise ValueError("Tenure must be greater than zero")
    if annual_rate_percent < 0:
        raise ValueError("Annual rate cannot be negative")
    principal = Decimal(principal_minor)
    monthly_rate = _monthly_rate(annual_rate_percent)
    if monthly_rate == 0:
        emi = principal / tenure_months
    else:
        factor = (Decimal(1) + monthly_rate) ** tenure_months
        emi = principal * monthly_rate * factor / (factor - Decimal(1))
    total = emi * tenure_months
    return _loan_payment_result(emi, total, total - principal, tenure_months)


def _prepayment_payment_results(
    principal_minor: int,
    annual_rate_percent: float,
    tenure_months: int,
    prepayment_minor: int,
) -> tuple[dict, int, dict]:
    """Return the canonical baseline and lower-balance payment scenarios."""
    baseline = loan_payment(principal_minor, annual_rate_percent, tenure_months)
    reduced_principal = max(0, principal_minor - prepayment_minor)
    after_prepayment = (
        loan_payment(reduced_principal, annual_rate_percent, tenure_months)
        if reduced_principal
        else _loan_payment_result(0, 0, 0, 0)
    )
    return baseline, reduced_principal, after_prepayment


def _amortization_steps(
    principal_minor: int,
    annual_rate_percent: float,
    payment_minor: int,
    max_months: int,
    *,
    fixed_installment_count: bool = False,
) -> tuple[list[tuple[int, Decimal, Decimal, Decimal, Decimal]], Decimal]:
    """Run the one canonical monthly-interest and principal-paydown engine."""
    balance = Decimal(principal_minor)
    payment = Decimal(payment_minor)
    monthly_rate = _monthly_rate(annual_rate_percent)
    total_interest = Decimal(0)
    steps: list[tuple[int, Decimal, Decimal, Decimal, Decimal]] = []

    for installment in range(1, max_months + 1):
        if balance <= 0 and not fixed_installment_count:
            break
        interest = balance * monthly_rate
        if payment <= interest and balance + interest > payment:
            raise ValueError("Payment does not cover monthly interest")
        if fixed_installment_count and installment == max_months:
            principal_payment = balance
            actual_payment = principal_payment + interest
        else:
            actual_payment = min(payment, balance + interest)
            principal_payment = max(Decimal(0), actual_payment - interest)
        balance = max(Decimal(0), balance - principal_payment)
        total_interest += interest
        steps.append((
            installment,
            actual_payment,
            principal_payment,
            interest,
            balance,
        ))

    if balance > 0:
        raise ValueError("Loan does not amortize within the supported range")
    return steps, total_interest


@tool_contract(description=(
    "Return a deterministic, renderer-neutral loan amortization dataset with one row per monthly "
    "installment and typed fields for EMI, principal paid, interest paid, and outstanding principal. "
    "Use when the user asks to chart, graph, table, compare, or export a loan repayment schedule."
), input_model=LoanPaymentInput, output_model=ComputedDatasetResult)
def loan_amortization_schedule(
    principal_minor: int,
    annual_rate_percent: float,
    tenure_months: int,
) -> dict:
    """Return a renderer-neutral, deterministic amortization dataset.

    Calculators describe their fields and rows; they do not choose a frontend
    chart. The presentation layer can therefore use the same contract for a
    chart, table, export, or a later analytical transform.
    """
    payment = loan_payment(principal_minor, annual_rate_percent, tenure_months)
    steps, total_interest = _amortization_steps(
        principal_minor,
        annual_rate_percent,
        payment["emi_minor"],
        tenure_months,
        fixed_installment_count=True,
    )
    rows = [
        {
            "installment": installment,
            "payment_minor": _minor(actual_payment),
            "principal_payment_minor": _minor(principal_payment),
            "interest_payment_minor": _minor(interest),
            "remaining_principal_minor": _minor(balance),
        }
        for installment, actual_payment, principal_payment, interest, balance in steps
    ]

    return {
        "kind": "computed_dataset",
        "name": "loan_amortization_schedule",
        "title": "Loan amortization schedule",
        "description": (
            f"{tenure_months} monthly installments at {annual_rate_percent:g}% annual interest."
        ),
        "fields": [field.model_dump(mode="json") for field in LOAN_AMORTIZATION_FIELDS],
        "default_dimension": "installment",
        "default_measures": ["principal_payment_minor", "remaining_principal_minor"],
        "rows": rows,
        "summary": {
            **payment,
            "principal_minor": principal_minor,
            "annual_rate_percent": annual_rate_percent,
            "calculated_total_interest_minor": _minor(total_interest),
        },
    }


@tool_contract(description=(
    "Return a deterministic loan amortization dataset for a customer-specified fixed monthly "
    "payment. Use when the payment, rather than a target tenure, controls the schedule."
), input_model=FixedPaymentInput, output_model=ComputedDatasetResult)
def fixed_payment_amortization_schedule(
    principal_minor: int,
    annual_rate_percent: float,
    payment_minor: int,
    max_months: int = 1200,
) -> dict:
    """Render the actual tenure produced by one fixed monthly payment."""
    steps, total_interest = _amortization_steps(
        principal_minor,
        annual_rate_percent,
        payment_minor,
        max_months,
    )
    rows = [
        {
            "installment": installment,
            "payment_minor": _minor(actual_payment),
            "principal_payment_minor": _minor(principal_payment),
            "interest_payment_minor": _minor(interest),
            "remaining_principal_minor": _minor(balance),
        }
        for installment, actual_payment, principal_payment, interest, balance in steps
    ]
    total_payment = sum(row["payment_minor"] for row in rows)
    return {
        "kind": "computed_dataset",
        "name": "fixed_payment_amortization_schedule",
        "title": "Fixed-payment loan schedule",
        "description": (
            f"{len(rows)} installments at {annual_rate_percent:g}% annual interest "
            "using the supplied monthly payment."
        ),
        "fields": [field.model_dump(mode="json") for field in LOAN_AMORTIZATION_FIELDS],
        "default_dimension": "installment",
        "default_measures": ["payment_minor", "remaining_principal_minor"],
        "rows": rows,
        "summary": {
            "payment_minor": payment_minor,
            "total_payment_minor": total_payment,
            "total_interest_minor": _minor(total_interest),
            "tenure_months": len(rows),
            "principal_minor": principal_minor,
            "annual_rate_percent": annual_rate_percent,
        },
    }


@tool_contract(description=(
    "Compare a deterministic loan baseline with an immediate principal prepayment. Principal and "
    "prepayment are integer minor units."
), input_model=LoanWithPrepaymentInput, output_model=LoanPrepaymentResult)
def loan_with_prepayment(principal_minor: int, annual_rate_percent: float, tenure_months: int, prepayment_minor: int) -> dict:
    baseline, _, reduced = _prepayment_payment_results(
        principal_minor,
        annual_rate_percent,
        tenure_months,
        prepayment_minor,
    )
    return {
        "baseline": baseline,
        "after_prepayment": reduced,
        "interest_saved_minor": baseline["total_interest_minor"] - reduced["total_interest_minor"],
        "emi_reduction_minor": baseline["emi_minor"] - reduced["emi_minor"],
    }


@tool_contract(description=(
    "Compare total interest and tenure with and without a one-time principal prepayment made after "
    "a specified number of monthly EMIs while keeping the original EMI fixed. Use this single "
    "calculator for a timed-prepayment comparison instead of reconstructing two schedules. Money "
    "inputs and outputs are integer minor units."
), input_model=TimedLoanPrepaymentInput, output_model=TimedLoanPrepaymentResult)
def loan_with_timed_prepayment(
    principal_minor: int,
    annual_rate_percent: float,
    tenure_months: int,
    prepayment_minor: int,
    prepayment_after_months: int,
) -> dict:
    """Apply one prepayment between installments and keep the original EMI."""

    baseline = loan_payment(principal_minor, annual_rate_percent, tenure_months)
    baseline_steps, baseline_interest = _amortization_steps(
        principal_minor,
        annual_rate_percent,
        baseline["emi_minor"],
        tenure_months,
        fixed_installment_count=True,
    )
    prefix = baseline_steps[:prepayment_after_months]
    balance_at_prepayment = (
        prefix[-1][4] if prefix else Decimal(principal_minor)
    )
    applied_prepayment = min(Decimal(prepayment_minor), balance_at_prepayment)
    remaining_principal = balance_at_prepayment - applied_prepayment
    remaining_steps: list[tuple[int, Decimal, Decimal, Decimal, Decimal]] = []
    remaining_interest = Decimal(0)
    if remaining_principal > 0:
        remaining_steps, remaining_interest = _amortization_steps(
            _minor(remaining_principal),
            annual_rate_percent,
            baseline["emi_minor"],
            tenure_months - prepayment_after_months,
        )
    prefix_interest = sum((step[3] for step in prefix), Decimal(0))
    prepayment_interest = _minor(prefix_interest + remaining_interest)
    baseline_interest_minor = _minor(baseline_interest)
    resulting_tenure = prepayment_after_months + len(remaining_steps)
    final_payment = _minor(remaining_steps[-1][1]) if remaining_steps else 0
    return {
        "principal_minor": principal_minor,
        "annual_rate_percent": annual_rate_percent,
        "emi_minor": baseline["emi_minor"],
        "prepayment_minor": prepayment_minor,
        "applied_prepayment_minor": _minor(applied_prepayment),
        "prepayment_after_months": prepayment_after_months,
        "baseline_total_interest_minor": baseline_interest_minor,
        "with_prepayment_total_interest_minor": prepayment_interest,
        "interest_saved_minor": max(0, baseline_interest_minor - prepayment_interest),
        "baseline_tenure_months": tenure_months,
        "with_prepayment_tenure_months": resulting_tenure,
        "months_saved": max(0, tenure_months - resulting_tenure),
        "final_payment_minor": final_payment,
    }


@tool_contract(description=(
    "Calculate deterministic loan tenure and interest for a fixed monthly payment in minor units. "
    "Use only when principal, annual rate, and payment are supplied."
), input_model=FixedPaymentInput, output_model=FixedPaymentResult)
def amortize_with_fixed_payment(principal_minor: int, annual_rate_percent: float, payment_minor: int, max_months: int = 1200) -> dict:
    """Deterministically amortize a balance; raises when the payment cannot cover interest."""
    steps, total_interest = _amortization_steps(
        principal_minor,
        annual_rate_percent,
        payment_minor,
        max_months,
    )
    return {"tenure_months": len(steps), "total_interest_minor": _minor(total_interest), "payment_minor": payment_minor}


@tool_contract(description=(
    "Compare lower-EMI and shorter-tenure strategies after a loan prepayment, including an optional "
    "fee percent. Money inputs and outputs are integer minor units."
), input_model=LoanStrategyInput, output_model=LoanStrategyResult)
def loan_strategy_options(principal_minor: int, annual_rate_percent: float, tenure_months: int, prepayment_minor: int, fee_percent: float = 0) -> dict:
    baseline, reduced_principal, lower_emi = _prepayment_payment_results(
        principal_minor,
        annual_rate_percent,
        tenure_months,
        prepayment_minor,
    )
    fee_minor = _minor(Decimal(prepayment_minor) * Decimal(str(fee_percent)) / Decimal(100))
    shorter_tenure = amortize_with_fixed_payment(reduced_principal, annual_rate_percent, baseline["emi_minor"]) if reduced_principal else {
        "tenure_months": 0, "total_interest_minor": 0, "payment_minor": 0,
    }
    return {
        "prepayment_minor": prepayment_minor,
        "fee_minor": fee_minor,
        "baseline": baseline,
        "lower_emi": {
            **lower_emi,
            "interest_saved_minor": baseline["total_interest_minor"] - lower_emi["total_interest_minor"] - fee_minor,
        },
        "shorter_tenure": {
            **shorter_tenure,
            "months_saved": tenure_months - shorter_tenure["tenure_months"],
            "interest_saved_minor": baseline["total_interest_minor"] - shorter_tenure["total_interest_minor"] - fee_minor,
        },
    }


@tool_contract(description=(
    "Project an investment deterministically from monthly contribution, current value, annual return "
    "percent, and years. Money inputs and outputs are integer minor units and returns are assumptions."
), input_model=InvestmentProjectionInput, output_model=InvestmentProjectionResult)
def investment_projection(monthly_contribution_minor: int, current_value_minor: int, annual_return_percent: float, years: int) -> dict:
    months = years * 12
    monthly_rate = _monthly_rate(annual_return_percent)
    current = Decimal(current_value_minor)
    contribution = Decimal(monthly_contribution_minor)
    if monthly_rate == 0:
        future = current + contribution * months
    else:
        future = current * ((Decimal(1) + monthly_rate) ** months)
        future += contribution * (((Decimal(1) + monthly_rate) ** months - Decimal(1)) / monthly_rate) * (Decimal(1) + monthly_rate)
    invested = current + contribution * months
    return {
        "projected_value_minor": _minor(future),
        "invested_minor": _minor(invested),
        "estimated_returns_minor": _minor(future - invested),
        "years": years,
        "assumed_annual_return_percent": annual_return_percent,
    }


@tool_contract(description=(
    "Evaluate a deterministic purchase-affordability scenario from purchase price, liquid savings, "
    "monthly income, monthly essential spend, and emergency-reserve months. Money uses minor units."
), input_model=AffordabilityInput, output_model=AffordabilityResult)
def affordability(purchase_minor: int, liquid_savings_minor: int, monthly_income_minor: int, monthly_essential_spend_minor: int, emergency_months: int = 6) -> dict:
    emergency_fund = monthly_essential_spend_minor * emergency_months
    available_after_reserve = max(0, liquid_savings_minor - emergency_fund)
    monthly_surplus = max(0, monthly_income_minor - monthly_essential_spend_minor)
    gap = max(0, purchase_minor - available_after_reserve)
    months_to_goal = (gap + monthly_surplus - 1) // monthly_surplus if gap and monthly_surplus else (0 if gap == 0 else None)
    affordable_now = purchase_minor <= available_after_reserve
    return {
        "affordable_now": affordable_now,
        "purchase_minor": purchase_minor,
        "emergency_reserve_minor": emergency_fund,
        "available_after_reserve_minor": available_after_reserve,
        "monthly_surplus_minor": monthly_surplus,
        "gap_minor": gap,
        "months_to_goal": months_to_goal,
        "rule": f"Preserves {emergency_months} months of essential expenses",
    }
