from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain import ACTIVE_STATUS, SpendNature
from ..models import Account, Budget, Goal, Loan, Transaction
from ..schemas import DataReference, Widget, WidgetType
from .analytics import category_breakdown, month_bounds, recurring_expenses, shift_month, spending_summary
from .calculators import loan_strategy_options
from .currency import format_money_minor, user_currency
from .semantic import BINARY_TRANSFORM_OPERATIONS, WINDOW_TRANSFORM_OPERATIONS, AnalysisPlan, AnalysisTransform, execute_finance_query
from .semantic_registry import TIME_GRAIN_SPECS
from .taxonomy import TaxonomyRepository
from .transactions import expense_transactions
from .widget_library import WidgetLibrary


@dataclass
class IntelligenceResult:
    message: str
    widgets: list[Widget]
    citations: list[DataReference]


def _active_loans(db: Session, user_id: UUID, currency: str) -> list[Loan]:
    return list(db.scalars(
        select(Loan).where(
            Loan.user_id == user_id,
            Loan.currency == currency,
            Loan.status == ACTIVE_STATUS,
        ).order_by(Loan.outstanding_principal_minor.desc())
    ))


def _apply_transform(results: list[dict], transform: AnalysisTransform) -> dict:
    source = next(result for result in results if result["name"] == transform.query_name)
    if transform.operation in BINARY_TRANSFORM_OPERATIONS:
        secondary = next(result for result in results if result["name"] == transform.secondary_query_name)
        primary_total = sum(int(row["value"]) for row in source["rows"])
        secondary_total = sum(int(row["value"]) for row in secondary["rows"])
        return {
            "name": transform.name,
            "operation": transform.operation,
            "queryName": transform.query_name,
            "secondaryQueryName": transform.secondary_query_name,
            "metric": source["metric"],
            "primaryValue": primary_total,
            "secondaryValue": secondary_total,
            "value": primary_total - secondary_total if transform.operation == "difference" else None,
            "ratioBasisPoints": round(primary_total * 10_000 / secondary_total) if transform.operation == "ratio" and secondary_total else None,
        }
    if transform.operation == "change_drivers":
        by_period: dict[str, dict[str, int]] = {}
        for row in source["rows"]:
            period = str(row.get(transform.period_dimension, "Unknown"))
            driver = str(row.get(transform.dimension, "Unknown"))
            values = by_period.setdefault(period, {})
            values[driver] = values.get(driver, 0) + int(row["value"])
        periods = sorted(by_period)
        output = {
            "name": transform.name,
            "operation": transform.operation,
            "queryName": transform.query_name,
            "dimension": transform.dimension,
            "periodDimension": transform.period_dimension,
            "metric": source["metric"],
            "periods": periods,
            "values": [],
        }
        if len(periods) >= 2:
            first, last = periods[0], periods[-1]
            drivers = set(by_period[first]) | set(by_period[last])
            changes = [
                {"label": driver, "fromValue": by_period[first].get(driver, 0), "toValue": by_period[last].get(driver, 0), "value": by_period[last].get(driver, 0) - by_period[first].get(driver, 0)}
                for driver in drivers
            ]
            changes.sort(key=lambda item: (item["value"], abs(item["value"])), reverse=True)
            output["values"] = changes[:transform.limit]
            output["from"] = first
            output["to"] = last
        return output
    grouped: dict[str, int] = {}
    for row in source["rows"]:
        key = str(row.get(transform.dimension, "Unknown"))
        grouped[key] = grouped.get(key, 0) + int(row["value"])
    ranked = sorted(grouped.items(), key=lambda item: item[1], reverse=True)
    output = {
        "name": transform.name,
        "operation": transform.operation,
        "queryName": transform.query_name,
        "dimension": transform.dimension,
        "metric": source["metric"],
    }
    if transform.operation in WINDOW_TRANSFORM_OPERATIONS:
        chronological = sorted(grouped.items())
        values = []
        running = 0
        for index, (label, value) in enumerate(chronological):
            if transform.operation == "cumulative_sum":
                running += value
                rendered = running
            else:
                window_values = [item[1] for item in chronological[max(0, index - transform.window + 1):index + 1]]
                rendered = round(sum(window_values) / len(window_values))
            values.append({"label": label, "value": rendered, "raw_value": value})
        output["values"] = values
        output["window"] = transform.window if transform.operation == "moving_average" else None
    elif transform.operation == "compare_totals":
        selected = ranked[:transform.limit]
        output["values"] = [{"label": label, "value": value} for label, value in selected]
        if len(selected) >= 2:
            output["leader"] = selected[0][0]
            output["difference"] = selected[0][1] - selected[1][1]
    elif transform.operation == "rank":
        output["values"] = [{"label": label, "value": value, "rank": index + 1} for index, (label, value) in enumerate(ranked[:transform.limit])]
    elif transform.operation == "share_of_total":
        total = sum(grouped.values())
        output["values"] = [
            {"label": label, "value": value, "basis_points": round(value * 10_000 / total) if total else 0}
            for label, value in ranked[:transform.limit]
        ]
        output["total"] = total
    else:
        chronological = sorted(grouped.items())
        output["values"] = [{"label": label, "value": value} for label, value in chronological]
        if len(chronological) >= 2:
            first, last = chronological[0], chronological[-1]
            difference = last[1] - first[1]
            output.update({
                "from": first[0],
                "to": last[0],
                "difference": difference,
                "changeBasisPoints": round(difference * 10_000 / first[1]) if first[1] else None,
            })
    return output


def _semantic_message(results: list[dict], transforms: list[dict]) -> str:
    """Render only values returned by the governed executor—no invented facts."""
    if not results:
        return "The analysis plan did not contain a query."
    currency = next((result.get("currency") for result in results if result.get("currency")), None)

    def render_money(value: int) -> str:
        if not currency:
            raise ValueError("A money analysis result must declare its currency")
        return format_money_minor(value, currency)

    if len(results) > 1:
        metric_labels = {
            "income": "recorded income",
            "gross_spend": "recorded spending",
            "net_spend": "net spending",
            "net_cash_flow": "net cash flow",
            "transaction_amount": "transaction activity",
            "transaction_count": "transaction count",
        }
        clauses = []
        seen_metrics: set[str] = set()
        for result in results:
            label = metric_labels.get(result["metric"])
            if not label or result["metric"] in seen_metrics:
                continue
            seen_metrics.add(result["metric"])
            total = sum(int(row.get("value", 0)) for row in result["rows"])
            rendered = str(total) if result["metric"] == "transaction_count" else render_money(total)
            clauses.append(f"{label} is {rendered}")
        if len(clauses) >= 2:
            start = date.fromisoformat(results[0]["start"])
            end = date.fromisoformat(results[0]["end"])
            period = (
                f"{start.day}–{end.day} {end.strftime('%b %Y')}"
                if start.year == end.year and start.month == end.month
                else f"{start.strftime('%-d %b %Y')}–{end.strftime('%-d %b %Y')}"
            )
            if len(clauses) == 2:
                summary = " and ".join(clauses)
            else:
                summary = f"{', '.join(clauses[:-1])}, and {clauses[-1]}"
            share = next((item for item in transforms if item.get("operation") == "share_of_total" and item.get("values")), None)
            if share:
                leader = share["values"][0]
                percentage = Decimal(leader["basis_points"]) / Decimal(100)
                summary += f". {leader['label']} is the largest recorded share at {percentage}% ({render_money(leader['value'])})"
            return f"For {period}, {summary}."
    if transforms:
        transform = transforms[0]
        values = transform.get("values", [])
        is_count = transform.get("metric") == "transaction_count"
        render = (lambda value: str(value)) if is_count else render_money
        if transform["operation"] == "compare_totals" and len(values) >= 2:
            leader = values[0]
            runner_up = values[1]
            return f"{leader['label']} is larger at {render(leader['value'])}, compared with {render(runner_up['value'])} for {runner_up['label']}; the difference is {render(transform['difference'])}."
        if transform["operation"] == "rank" and values:
            return f"The largest result is {values[0]['label']} at {render(values[0]['value'])}."
        if transform["operation"] == "share_of_total" and values:
            share = Decimal(values[0]["basis_points"]) / Decimal(100)
            return f"{values[0]['label']} is the largest share at {share}% ({render(values[0]['value'])})."
        if transform["operation"] == "period_change" and len(values) >= 2:
            direction = "increased" if transform["difference"] >= 0 else "decreased"
            return f"The result {direction} by {render(abs(transform['difference']))} from {transform['from']} to {transform['to']}."
        if transform["operation"] == "change_drivers" and values:
            driver = values[0]
            direction = "increase" if driver["value"] >= 0 else "decrease"
            return f"{driver['label']} is the largest recorded {direction}, changing by {render(abs(driver['value']))} from {transform['from']} to {transform['to']}."
        if transform["operation"] == "difference":
            direction = "above" if transform["value"] >= 0 else "below"
            return f"The first measure is {render(abs(transform['value']))} {direction} the comparison measure."
        if transform["operation"] == "ratio" and transform.get("ratioBasisPoints") is not None:
            return f"The first measure is {Decimal(transform['ratioBasisPoints']) / Decimal(100)}% of the comparison measure."
        if transform["operation"] in WINDOW_TRANSFORM_OPERATIONS and values:
            label = "cumulative total" if transform["operation"] == "cumulative_sum" else f"{transform['window']}-period moving average"
            return f"The latest {label} is {render(values[-1]['value'])}."
    if len(results) > 1:
        nonempty = sum(bool(result["rows"]) for result in results)
        return f"I ran {len(results)} validated analyses; {nonempty} returned recorded financial data."
    result = results[0]
    rows = result["rows"]
    if not rows:
        if result.get("requires_transaction_time"):
            grain = (result.get("time_grouping") or {}).get("grain", "sub-day")
            return (
                f"I can’t draw the requested {grain} analysis because none of the matching "
                "transactions has a recorded transaction time. I can group the same records by day instead."
            )
        return f"I found no recorded data for {result['name'].lower()} in that period."
    metric = result["metric"]
    grouping = result.get("time_grouping") or {}
    if grouping:
        grain = grouping.get("grain", "time")
        cadence = TIME_GRAIN_SPECS[grain].cadence if grain in TIME_GRAIN_SPECS else str(grain)
        if metric == "transaction_amount":
            return (
                f"I plotted {len(rows)} {cadence} transaction-amount series point"
                f"{'s' if len(rows) != 1 else ''} for {result['name'].lower()}, separated by transaction type. "
                "These are absolute amounts within each type, not net cash flow."
            )
        if metric == "transaction_count":
            count = sum(int(row.get("value", 0)) for row in rows)
            return f"I plotted {count} recorded transaction{'s' if count != 1 else ''} across {len(rows)} {cadence} bucket{'s' if len(rows) != 1 else ''}."
    if not result["dimensions"]:
        value = rows[0]["value"]
        rendered = str(value) if metric == "transaction_count" else render_money(value)
        return f"{result['name']}: {rendered}."
    first = rows[0]
    labels = [str(first.get(dimension, "Unknown")) for dimension in result["dimensions"]]
    rendered = str(first["value"]) if metric == "transaction_count" else render_money(first["value"])
    readable_name = result["name"].replace("_", " ").lower()
    if len(rows) == 1:
        return f"I found one grouped result for {readable_name}: {' · '.join(labels)} — {rendered}."
    return f"I found {len(rows)} grouped results for {readable_name}; the grounded values are shown below."


def _load_context(db: Session, user_id: UUID, currency: str, today: date, sources: list[str]) -> tuple[dict, list[DataReference]]:
    context: dict[str, list[dict]] = {}
    citations: list[DataReference] = []
    start, end = month_bounds(today)
    taxonomy = TaxonomyRepository(db, user_id)
    if "budgets" in sources:
        rows = []
        budgets = list(db.scalars(select(Budget).where(Budget.user_id == user_id, Budget.currency == currency).order_by(Budget.name)))
        for budget in budgets:
            category = taxonomy.category(budget.category_id)
            spent = spending_summary(db, user_id, start, min(today, end), category.slug if category else None)["total_minor"]
            rows.append({"id": str(budget.id), "name": budget.name, "category": category.name if category else None, "limitMinor": budget.amount_minor, "spentMinor": spent, "remainingMinor": budget.amount_minor - spent, "currency": budget.currency})
        context["budgets"] = rows
        citations.append(DataReference(label="Current budgets and month-to-date utilization", entity_type="budget", entity_ids=[row["id"] for row in rows]))
    if "goals" in sources:
        goals = list(db.scalars(select(Goal).where(Goal.user_id == user_id, Goal.currency == currency).order_by(Goal.target_date.nullslast(), Goal.name)))
        context["goals"] = [{"id": str(goal.id), "name": goal.name, "targetMinor": goal.target_minor, "currentMinor": goal.current_minor, "remainingMinor": max(0, goal.target_minor - goal.current_minor), "targetDate": goal.target_date.isoformat() if goal.target_date else None, "currency": goal.currency} for goal in goals]
        citations.append(DataReference(label="Saved financial goals", entity_type="goal", entity_ids=[str(goal.id) for goal in goals]))
    if "loans" in sources:
        loans = _active_loans(db, user_id, currency)
        context["loans"] = [{"id": str(loan.id), "name": loan.name, "lender": loan.lender, "principalMinor": loan.outstanding_principal_minor, "annualRatePercent": float(loan.annual_rate_percent), "remainingTenureMonths": loan.remaining_tenure_months, "emiMinor": loan.current_emi_minor, "prepaymentFeePercent": float(loan.prepayment_fee_percent), "currency": loan.currency} for loan in loans]
        citations.append(DataReference(label="Stored active loan terms", entity_type="loan", entity_ids=[str(loan.id) for loan in loans]))
    if "accounts" in sources:
        accounts = list(db.scalars(select(Account).where(Account.user_id == user_id, Account.currency == currency).order_by(Account.name)))
        context["accounts"] = [{"id": str(account.id), "name": account.name, "type": account.account_type, "balanceMinor": account.balance_minor, "currency": account.currency} for account in accounts]
        citations.append(DataReference(label="Saved account balances", entity_type="account", entity_ids=[str(account.id) for account in accounts]))
    if "recurring_expenses" in sources:
        recurring = recurring_expenses(db, user_id)
        context["recurring_expenses"] = recurring
        citations.append(DataReference(label="Detected recurring expense patterns", entity_type="transaction", entity_ids=[item["id"] for item in recurring]))
    return context, citations


def _month_periods(today: date, count: int = 3) -> list[tuple[date, date]]:
    current_start, current_end = month_bounds(today)
    periods = []
    for offset in reversed(range(count)):
        start = shift_month(current_start, -offset)
        _, end = month_bounds(start)
        periods.append((start, min(today, end) if offset == 0 else end))
    return periods


def three_month_allocation(db: Session, user_id: UUID, currency: str, today: date) -> IntelligenceResult:
    periods = _month_periods(today)
    series: dict[str, dict] = {}
    for start, end in periods:
        month = start.strftime("%b %Y")
        for row in category_breakdown(db, user_id, start, end):
            item = series.setdefault(row["id"], {"id": row["id"], "label": row["label"], "months": {}})
            item["months"][month] = row["amount_minor"]
    categories = sorted(series.values(), key=lambda item: sum(item["months"].values()), reverse=True)
    budgets = list(db.scalars(select(Budget).where(Budget.user_id == user_id, Budget.currency == currency)))
    budget_room = []
    current_month = periods[-1][0].strftime("%b %Y")
    taxonomy = TaxonomyRepository(db, user_id)
    for budget in budgets:
        category = taxonomy.category(budget.category_id)
        spent = series.get(category.slug, {}).get("months", {}).get(current_month, 0) if category else sum(item["months"].get(current_month, 0) for item in categories)
        if spent < budget.amount_minor:
            budget_room.append({"label": category.name if category else budget.name, "room_minor": budget.amount_minor - spent})
    if budget_room:
        message = "I compared the last three months. The highlighted categories are below limits you explicitly set; that is spending room, not a recommendation to spend it."
    else:
        message = "I can compare the last three months, but your transaction history alone cannot determine where you should spend more. Add budgets or priorities and I can evaluate allocation against them."
    widget = Widget(
        id=f"allocation-{today.isoformat()}",
        type=WidgetType.ANALYSIS_TABLE,
        data={
            "title": "Three-month spending allocation",
            "body": "Actual spending by category; no peer benchmark or invented target is used.",
            "columns": [start.strftime("%b %Y") for start, _ in periods],
            "rows": categories,
            "budgetRoom": budget_room,
            "currency": currency,
        },
    )
    citations = [DataReference(label="Canonical expenses across three months", entity_type="transaction", query={"periods": [[start.isoformat(), end.isoformat()] for start, end in periods]})]
    return IntelligenceResult(message, [widget], citations)


def avoidable_expense_candidates(db: Session, user_id: UUID, currency: str, today: date) -> IntelligenceResult:
    start = shift_month(today.replace(day=1), -2)
    transactions = list(db.scalars(expense_transactions(user_id, currency=currency).where(
        Transaction.transaction_date.between(start, today),
    ).order_by(Transaction.amount_minor.desc()).limit(500)))
    merchant_counts: dict[str, int] = {}
    for transaction in transactions:
        if transaction.merchant_name:
            merchant_counts[transaction.merchant_name.casefold()] = merchant_counts.get(transaction.merchant_name.casefold(), 0) + 1
    candidates = []
    taxonomy = TaxonomyRepository(db, user_id)
    fee_tokens = ("late fee", "penalty", "overdraft", "convenience fee", "interest charge")
    for transaction in transactions:
        text = f"{transaction.merchant_name or ''} {transaction.description or ''}".casefold()
        reasons = []
        score = Decimal("0")
        if transaction.spend_nature == SpendNature.POTENTIALLY_AVOIDABLE:
            reasons.append("Previously marked potentially avoidable")
            score += Decimal("0.8")
        if any(token in text for token in fee_tokens):
            reasons.append("Fee or penalty rather than a purchased service")
            score += Decimal("0.75")
        if transaction.spend_nature == SpendNature.DISCRETIONARY and transaction.amount_minor >= 100_000:
            reasons.append("Large transaction marked discretionary")
            score += Decimal("0.45")
        if transaction.merchant_name and merchant_counts.get(transaction.merchant_name.casefold(), 0) >= 3:
            reasons.append("Repeated merchant in the last three months")
            score += Decimal("0.2")
        if not reasons:
            continue
        category, subcategory = taxonomy.path(
            transaction.category_id,
            transaction.subcategory_id,
        )
        candidates.append({
            "id": str(transaction.id),
            "merchant": transaction.merchant_name or "Expense",
            "amountMinor": transaction.amount_minor,
            "currency": transaction.currency,
            "date": transaction.transaction_date.isoformat(),
            "category": category.name if category else None,
            "subcategory": subcategory.name if subcategory else None,
            "spendNature": transaction.spend_nature,
            "reasons": reasons,
            "confidence": float(min(score, Decimal("0.99"))),
        })
    candidates.sort(key=lambda item: (item["confidence"], item["amountMinor"]), reverse=True)
    candidates = candidates[:20]
    potential = sum(item["amountMinor"] for item in candidates)
    message = (
        f"I found {len(candidates)} expense{'s' if len(candidates) != 1 else ''} worth {format_money_minor(potential, currency)} that may be worth reviewing. Nothing is labelled avoidable until you decide."
        if candidates else
        "I didn’t find evidence strong enough to call any recorded expense avoidable. Mark discretionary items or connect subscription data to improve this analysis."
    )
    widget = Widget(
        id=f"avoidable-{today.isoformat()}",
        type=WidgetType.AVOIDABLE_EXPENSES,
        data={"title": "Potentially avoidable expenses", "body": "Review candidates—this is not an automatic judgement.", "transactions": candidates, "potentialMinor": potential, "currency": currency},
        actions=[],
    )
    citations = [DataReference(label="Candidate expense transactions", entity_type="transaction", entity_ids=[item["id"] for item in candidates], query={"start": start.isoformat(), "end": today.isoformat()})]
    return IntelligenceResult(message, [widget], citations)


def loan_strategy(db: Session, user_id: UUID, currency: str) -> IntelligenceResult:
    loans = _active_loans(db, user_id, currency)
    if not loans:
        widget = Widget(
            id="loan-setup",
            type=WidgetType.LOAN_CALCULATOR,
            data={"title": "Add your loan details", "body": "Enter outstanding principal, rate, remaining months and an optional prepayment. Saveable loan profiles are now supported by the backend.", "prepaymentMinor": 0},
        )
        return IntelligenceResult(
            "I need the loan principal, rate and remaining tenure before comparing reduction strategies.",
            [widget],
            [DataReference(label="No active saved loan profile was available", entity_type="loan")],
        )
    strategies = []
    for loan in loans:
        candidate_amounts = sorted({loan.current_emi_minor or 0, loan.outstanding_principal_minor // 20, loan.outstanding_principal_minor // 10})
        options = [loan_strategy_options(
            loan.outstanding_principal_minor,
            float(loan.annual_rate_percent),
            loan.remaining_tenure_months,
            amount,
            float(loan.prepayment_fee_percent),
        ) for amount in candidate_amounts if amount > 0]
        strategies.append({
            "loanId": str(loan.id), "name": loan.name, "lender": loan.lender,
            "principalMinor": loan.outstanding_principal_minor, "currency": loan.currency,
            "annualRatePercent": float(loan.annual_rate_percent), "tenureMonths": loan.remaining_tenure_months,
            "options": options,
        })
    widget = Widget(id="loan-strategy", type=WidgetType.LOAN_STRATEGY, data={"title": "Loan reduction strategies", "body": "Compare lower EMI with shorter tenure. Cash reserve and prepayment-fee constraints still apply.", "loans": strategies})
    citations = [DataReference(label="Stored active loan terms", entity_type="loan", entity_ids=[str(loan.id) for loan in loans])]
    return IntelligenceResult("I modelled prepayment options for your active loans. Shorter tenure generally saves more interest than lowering EMI, but choose only an amount that preserves your cash reserve.", [widget], citations)


def execute_analysis_plan(db: Session, user_id: UUID, today: date, plan: AnalysisPlan) -> IntelligenceResult:
    currency = user_currency(db, user_id)
    if plan.analysis_type == "three_month_allocation":
        return three_month_allocation(db, user_id, currency, today)
    if plan.analysis_type == "avoidable_expenses":
        return avoidable_expense_candidates(db, user_id, currency, today)
    if plan.analysis_type == "loan_strategy":
        return loan_strategy(db, user_id, currency)
    results = [execute_finance_query(db, user_id, query) for query in plan.queries]
    transforms = [_apply_transform(results, transform) for transform in plan.transforms]
    context, context_citations = _load_context(db, user_id, currency, today, plan.context_sources)
    widgets: list[Widget] = []
    if plan.visualizations:
        result_by_name = {result["name"]: result for result in results}
        transform_by_name = {transform["name"]: transform for transform in transforms}
        visualization_sources = {
            item.transform_name or item.query_name: (
                transform_by_name[item.transform_name]["values"]
                if item.transform_name else result_by_name[item.query_name]["rows"]
            )
            for item in plan.visualizations
        }
        widgets.append(WidgetLibrary.data_visualization(
            widget_id=f"visualization-{today.isoformat()}",
            title="Financial analysis",
            body="Composed from governed semantic query results.",
            datasets=visualization_sources,
            visualizations=plan.visualizations,
            query_results={item.query_name: result_by_name[item.query_name] for item in plan.visualizations},
            columns=2 if len(plan.visualizations) > 1 else 1,
        ))
    if not widgets:
        widgets = [Widget(id=f"semantic-{today.isoformat()}", type=WidgetType.ANALYSIS_TABLE, data={"title": "Financial analysis", "body": "Compiled from a governed semantic query plan and deterministic result transforms.", "queryResults": results, "transforms": transforms, "context": context, "currency": currency})]
    citations = [
        DataReference(
            label=result["metric_definition"],
            entity_type="semantic_query",
            query={
                **query.model_dump(mode="json"),
                "registry_version": result["registry_version"],
                "schema_hash": result["schema_hash"],
            },
        )
        for query, result in zip(plan.queries, results)
    ]
    return IntelligenceResult(_semantic_message(results, transforms), widgets, [*citations, *context_citations])
