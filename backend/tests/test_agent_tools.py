from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from agno.models.response import ToolExecution
from pydantic import ValidationError
from sqlalchemy import select

from app.models import Category, TaxonomyScope, User
from app.seed import DEFAULT_USER_EMAIL
from app.services import agents
from app.services.agent_tools import bind_existing_tool
from app.services.agents import CopilotRouteDecision, PresentationIntent
from app.services.calculators import loan_amortization_schedule, loan_payment
from app.services.conversation import _agent_taxonomy, _user_runtime_tools
from app.services.runtime_tools import RUNTIME_TOOL_REGISTRY


def test_generic_binder_hides_dependencies_and_keeps_source_callable():
    dependency = {"prefix": "bound"}

    def existing(dependency: dict, value: int, on_date: date | None = None) -> dict:
        return {"value": f"{dependency['prefix']}-{value}", "on_date": on_date}

    bound = bind_existing_tool(
        existing,
        dependency,
        name="existing_read",
        description="Read through the existing function.",
    )

    assert set(bound.parameters["properties"]) == {"value", "on_date"}
    assert bound.parameters["properties"]["value"]["type"] == "integer"
    assert bound.parameters["properties"]["on_date"]["anyOf"][0] == {
        "format": "date",
        "type": "string",
    }
    assert bound.entrypoint(value=3, on_date="2026-08-12") == {
        "value": "bound-3",
        "on_date": "2026-08-12",
    }
    assert existing(dependency, 4, date(2026, 8, 13))["value"] == "bound-4"


def test_runtime_tool_catalog_is_complete_and_model_safe(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    tools = _user_runtime_tools(db, user, date(2026, 8, 12))

    assert {tool.name for tool in tools} == {spec.name for spec in RUNTIME_TOOL_REGISTRY}
    for tool in tools:
        assert "db" not in tool.parameters["properties"]
        assert "user_id" not in tool.parameters["properties"]
        assert "current_day" not in tool.parameters["properties"]

    calculator = next(tool for tool in tools if tool.name == "loan_payment")
    assert calculator.parameters["properties"]["principal_minor"]["exclusiveMinimum"] == 0
    assert calculator.parameters["properties"]["annual_rate_percent"]["maximum"] == 100
    assert calculator.parameters["properties"]["tenure_months"]["maximum"] == 600
    assert calculator.entrypoint(1_200_000, 0, 12) == loan_payment(1_200_000, 0, 12)
    with pytest.raises(ValidationError):
        calculator.entrypoint(1_200_000, 0, 601)
    schedule = next(tool for tool in tools if tool.name == "loan_amortization_schedule")
    assert schedule.entrypoint(1_200_000, 0, 12) == loan_amortization_schedule(1_200_000, 0, 12)


def test_taxonomy_tool_reads_only_the_authenticated_users_visible_categories(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    other = User(email="other@example.com", display_name="Other")
    db.add(other)
    db.flush()
    own_category = Category(
        slug="custom-own", name="Own category", icon="circle",
        scope=TaxonomyScope.USER.value, owner_user_id=user.id,
    )
    other_category = Category(
        slug="custom-other", name="Other category", icon="circle",
        scope=TaxonomyScope.USER.value, owner_user_id=other.id,
    )
    db.add_all([own_category, other_category])
    db.commit()

    taxonomy_tool = next(
        tool
        for tool in _user_runtime_tools(db, user, date(2026, 8, 12))
        if tool.name == "read_user_expense_taxonomy"
    )
    result = taxonomy_tool.entrypoint()

    # Eleven system expense categories plus the signed-in user's own row.
    assert len(result) == 12
    assert "Own category" in {item["name"] for item in result}
    assert "Other category" not in {item["name"] for item in result}
    assert result == _agent_taxonomy(db, user)


def test_router_preserves_only_successful_installed_tool_execution_as_grounding(monkeypatch):
    route = CopilotRouteDecision(
        route="conversation",
        reply="You have 11 expense categories.",
        confidence=0.99,
        reason="The authenticated taxonomy tool returned eleven categories.",
    )

    class StubAgent:
        def run(self, prompt):
            return SimpleNamespace(
                content=route,
                tools=[
                    ToolExecution(
                        tool_name="read_user_expense_taxonomy",
                        tool_args={},
                        result=(
                            "[{'slug': 'food', 'name': 'Food', 'subcategories': []}, "
                            "{'slug': 'other', 'name': 'Other', 'subcategories': []}]"
                        ),
                    ),
                    ToolExecution(tool_name="not_installed", tool_args={}, result="untrusted"),
                ],
            )

    def read_taxonomy() -> list[dict]:
        return []

    runtime_tool = bind_existing_tool(
        read_taxonomy,
        name="read_user_expense_taxonomy",
        description="Read taxonomy.",
    )
    monkeypatch.setattr(agents, "build_financial_copilot", lambda *args, **kwargs: StubAgent())

    decision = agents.interpret_with_financial_copilot(
        "How many categories are there?",
        [],
        date(2026, 8, 12),
        "Asia/Kolkata",
        [],
        runtime_tools=[runtime_tool],
    )

    assert decision.tool == "conversation"
    assert [item.name for item in decision.tool_grounding] == ["read_user_expense_taxonomy"]
    assert decision.tool_grounding[0].arguments == {}
    assert decision.tool_grounding[0].result.schema_name == "TaxonomyResult"
    assert len(decision.tool_grounding[0].result.data) == 2


def test_router_sends_authenticated_computed_dataset_to_generic_visualizer(monkeypatch):
    route = CopilotRouteDecision(
        route="analysis",
        query={"metric": "loan", "result_mode": "complex_analysis"},
        presentation=PresentationIntent(
            mode="chart",
            unit_of_analysis="installment",
            x_field="installment",
            y_fields=["principal_payment_minor"],
        ),
        confidence=0.99,
        reason="Chart the authenticated amortization dataset.",
    )
    computed = {
        "kind": "computed_dataset",
        "name": "loan_amortization_schedule",
        "title": "Loan amortization schedule",
        "description": "A deterministic test schedule.",
        "currency": "INR",
        "fields": [
            {
                "name": "installment",
                "label": "Installment",
                "type": "ordinal",
                "value_type": "number",
                "role": "dimension",
            },
            {
                "name": "principal_payment_minor",
                "label": "Principal paid",
                "type": "quantitative",
                "value_type": "money_minor",
                "role": "measure",
            },
        ],
        "default_dimension": "installment",
        "default_measures": ["principal_payment_minor"],
        "rows": [{"installment": 1, "principal_payment_minor": 100}],
        "summary": {"principal_minor": 1_200_000},
    }

    class StubAgent:
        def run(self, prompt):
            return SimpleNamespace(
                content=route,
                tools=[ToolExecution(
                    tool_name="loan_amortization_schedule",
                    tool_args={"principal_minor": 1_200_000, "annual_rate_percent": 0, "tenure_months": 12},
                    result=str(computed),
                )],
            )

    runtime_tool = bind_existing_tool(
        loan_amortization_schedule,
        name="loan_amortization_schedule",
        description="Return amortization rows.",
    )
    monkeypatch.setattr(agents, "build_financial_copilot", lambda *args, **kwargs: StubAgent())
    monkeypatch.setattr(
        agents,
        "build_analysis_tool_factory",
        lambda *args, **kwargs: pytest.fail("Computed datasets must not enter the database analysis factory"),
    )

    decision = agents.interpret_with_financial_copilot(
        "Chart principal paid after each EMI",
        [],
        date(2026, 8, 12),
        "Asia/Kolkata",
        [],
        runtime_tools=[runtime_tool],
    )

    assert decision.tool == "visualize_computation"
    assert decision.presentation.x_field == "installment"
    assert decision.presentation.y_fields == ["principal_payment_minor"]
    assert decision.tool_grounding[0].name == "loan_amortization_schedule"
