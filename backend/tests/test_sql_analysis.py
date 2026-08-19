from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from app.event_time import from_local_parts
from app.models import AnalysisToolTemplate, Transaction, User
from app.seed import default_user
from app.services import analysis_tools as analysis_tools_module
from app.services import sql_analysis
from app.services.analysis_tools import AnalysisToolContext, build_analysis_tools
from app.services.sql_analysis import (
    RUN_SQL_TOOL_NAME,
    SQL_TEMPLATE_VERSION,
    build_sql_analysis_tool,
    memorize_sql_template,
    sql_examples,
)
from app.services.sql_gate import SqlCompilationError
from app.services.template_retrieval import retrieve_templates


def context_for(db, user, question: str = "How much did I spend?") -> AnalysisToolContext:
    return AnalysisToolContext(
        db=db,
        user_id=user.id,
        conversation_id=uuid4(),
        today=date(2026, 8, 17),
        timezone_name="Asia/Kolkata",
        question=question,
    )


def expense(user_id, merchant: str, amount: int) -> Transaction:
    return Transaction(
        user_id=user_id,
        transaction_type="expense",
        amount_minor=amount,
        currency="INR",
        merchant_name=merchant,
        transaction_at=from_local_parts(date(2026, 8, 5), None, "Asia/Kolkata"),
    )


def test_gate_rejections_come_back_as_correctable_tool_errors(db):
    user = default_user(db)
    tool = build_sql_analysis_tool(context_for(db, user))

    payload = tool.entrypoint(purpose="steal", sql="SELECT email FROM users")

    assert payload["error"]["code"] == "unknown_table"
    assert "retry" in payload["error"]["hint"]


def test_successful_sql_returns_named_rows_and_saves_a_template(db):
    user = default_user(db)
    db.add(expense(user.id, "Blue Tokai", 40_000))
    db.commit()
    tool = build_sql_analysis_tool(context_for(db, user))

    payload = tool.entrypoint(
        purpose="total spend at one merchant",
        sql="SELECT SUM(amount_minor) AS total_minor FROM transactions WHERE merchant_name = 'Blue Tokai'",
    )

    assert payload["kind"] == "governed_sql"
    assert payload["rows"] == [{"total_minor": 40_000}]
    assert payload["template_saved"] is True

    template = db.scalar(select(AnalysisToolTemplate).where(
        AnalysisToolTemplate.template_version == SQL_TEMPLATE_VERSION
    ))
    assert template is not None
    stored_sql = template.plan_template["sql"]
    assert "Blue Tokai" not in stored_sql
    assert "%(p1)s" in stored_sql
    assert template.parameter_schema[0]["column"] == "merchant_name"
    assert template.parameter_schema[0]["type"] == "string"
    assert "Blue Tokai" not in template.capability_name
    assert "Blue Tokai" not in template.capability_description
    assert "Blue Tokai" not in template.capability_signature


def test_identical_structure_dedupes_instead_of_duplicating(db):
    user = default_user(db)
    first = memorize_sql_template(
        db, user.id, "SELECT name FROM accounts WHERE account_type = 'bank'"
    )
    second = memorize_sql_template(
        db, user.id, "SELECT name FROM accounts WHERE account_type = 'cash'"
    )
    templates = list(db.scalars(select(AnalysisToolTemplate).where(
        AnalysisToolTemplate.template_version == SQL_TEMPLATE_VERSION
    )))
    assert first is True and second is False
    assert len(templates) == 1
    assert templates[0].success_count == 2


def test_date_literals_become_typed_date_parameters(db):
    user = default_user(db)
    memorize_sql_template(
        db, user.id,
        "SELECT SUM(amount_minor) AS total_minor FROM transactions "
        "WHERE transaction_at BETWEEN '2026-08-01' AND '2026-08-17'",
    )
    template = db.scalar(select(AnalysisToolTemplate).where(
        AnalysisToolTemplate.template_version == SQL_TEMPLATE_VERSION
    ))
    assert [item["type"] for item in template.parameter_schema] == ["date", "date"]
    assert "2026-08-01" not in template.plan_template["sql"]


def test_sql_examples_are_scoped_to_the_current_manifest(db, monkeypatch):
    user = default_user(db)
    memorize_sql_template(db, user.id, "SELECT name FROM accounts WHERE account_type = 'bank'")
    db.flush()

    assert len(sql_examples(db, "accounts")) == 1

    monkeypatch.setattr(sql_analysis, "native_manifest_fingerprint", lambda: "0" * 64)
    assert sql_examples(db, "accounts") == []


def test_sql_templates_never_leak_into_the_grammar_lane(db):
    user = default_user(db)
    memorize_sql_template(db, user.id, "SELECT name FROM accounts WHERE account_type = 'bank'")
    db.flush()
    assert all(
        item.template.template_version != SQL_TEMPLATE_VERSION
        for item in retrieve_templates(db, user.id, "bank accounts")
    )


def test_the_lane_is_mounted_behind_its_flag(db, monkeypatch):
    user = default_user(db)
    context = context_for(db, user)

    monkeypatch.setattr(
        analysis_tools_module, "get_settings",
        lambda: SimpleNamespace(sql_lane_enabled=True, external_source_lane_enabled=True, federation_lane_enabled=True, python_lane_enabled=True),
    )
    names = {tool.name for tool in build_analysis_tools(context)}
    assert RUN_SQL_TOOL_NAME in names

    monkeypatch.setattr(
        analysis_tools_module, "get_settings",
        lambda: SimpleNamespace(sql_lane_enabled=False, external_source_lane_enabled=True, federation_lane_enabled=True, python_lane_enabled=True),
    )
    names = {tool.name for tool in build_analysis_tools(context)}
    assert RUN_SQL_TOOL_NAME not in names


def test_sql_mode_exposes_one_unrestricted_native_analysis_author(db, monkeypatch):
    user = default_user(db)
    context = context_for(db, user)
    monkeypatch.setattr(
        analysis_tools_module,
        "get_settings",
        lambda: SimpleNamespace(
            primary_agent_enabled=True,
            analysis_query_mode="sql",
            sql_lane_enabled=True,
            external_source_lane_enabled=False,
            federation_lane_enabled=False,
            python_lane_enabled=False,
        ),
    )

    tools = build_analysis_tools(context)
    names = {tool.name for tool in tools}

    assert RUN_SQL_TOOL_NAME in names
    assert "run_financial_analysis" not in names
    assert not any(name.startswith("bind_template__") for name in names)


def test_the_tool_description_carries_only_this_users_values(db):
    user = default_user(db)
    stranger = User(email="stranger@example.com", display_name="Stranger")
    db.add(stranger)
    db.flush()
    db.add(expense(user.id, "Blue Tokai", 40_000))
    db.add(expense(stranger.id, "Third Wave", 90_000))
    db.commit()

    tool = build_sql_analysis_tool(context_for(db, user))

    assert "transactions" in tool.description
    assert "Blue Tokai" in tool.description
    assert "Third Wave" not in tool.description
    assert "description" in tool.description
    assert "location_label" in tool.description
    assert "Forbidden sensitive columns" not in tool.description


def test_execution_errors_are_fed_back_not_raised(db, monkeypatch):
    user = default_user(db)
    tool = build_sql_analysis_tool(context_for(db, user))

    def explode(*args, **kwargs):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(sql_analysis, "execute_governed_sql", explode)
    payload = tool.entrypoint(purpose="anything", sql="SELECT name FROM accounts")
    assert payload["error"]["code"] == "execution_error"
    assert "connection lost" in payload["error"]["detail"]


def test_semantic_compilation_errors_are_distinct_from_execution_errors(db, monkeypatch):
    user = default_user(db)
    tool = build_sql_analysis_tool(context_for(db, user))

    def reject(*args, **kwargs):
        raise SqlCompilationError("UNION types text and bigint cannot be matched")

    monkeypatch.setattr(sql_analysis, "execute_governed_sql", reject)
    payload = tool.entrypoint(purpose="compare periods", sql="SELECT name FROM accounts")

    assert payload["error"] == {
        "code": "query_compilation_error",
        "stage": "semantic_compilation",
        "detail": "UNION types text and bigint cannot be matched",
        "hint": (
            "PostgreSQL rejected the statement during deterministic semantic "
            "compilation; correct its result shape or expression types."
        ),
    }
