from __future__ import annotations

from datetime import date
from uuid import uuid4

from sqlalchemy import select

from app.domain import AnalysisToolStatus
from app.models import AnalysisToolTemplate
from app.seed import default_user
from app.services.analysis_harness import execute_analysis_template
from app.services.analysis_seeds import seed_analysis_templates
from app.services.semantic import AnalysisPlan, AnalysisToolProposal, FinanceFilter, FinanceQueryPlan
from app.services.manifest import native_manifest_fingerprint
from app.services.semantic_registry import semantic_schema_registry
from app.services.template_binding import (
    ABSTAIN_TOOL_NAME,
    bind_tool_name,
    compile_bind_tools,
    materialize_binding,
    tenancy_safe_parameters,
)
from app.services.template_retrieval import ANALYSIS_TEMPLATE_VERSION


TODAY = date(2026, 8, 17)


def _food_proposal(today: date) -> AnalysisToolProposal:
    return AnalysisToolProposal(
        name="Food spending",
        description="Summarize recorded food expenses for a period.",
        intent_signature="food spending period",
        plan=AnalysisPlan(
            objective="descriptive",
            analysis_type="semantic_query",
            safe_reasoning_summary=["Filter recorded expenses to food", "Aggregate the validated period"],
            queries=[FinanceQueryPlan(
                name="Food spending",
                metric="gross_spend",
                filters=[FinanceFilter(field="category", value="food")],
                start_date=today.replace(day=1),
                end_date=today,
            )],
        ),
    )


def _food_template(db) -> AnalysisToolTemplate:
    user = default_user(db)
    outcome = execute_analysis_template(db, user.id, uuid4(), TODAY, _food_proposal(TODAY))
    return outcome.template


def _fill_for(template: AnalysisToolTemplate, **overrides):
    values = {
        "query_1.filter_1.value": "food",
        "query_1.start_date": "2026-08-01",
        "query_1.end_date": "2026-08-17",
        "query_1.limit": 25,
    }
    values.update(overrides)
    return {name.replace(".", "__"): value for name, value in values.items()}


def test_compiled_bind_tools_are_strict_closed_forms(db):
    seed_analysis_templates(db, today=TODAY)
    templates = list(db.scalars(select(AnalysisToolTemplate)))

    tools, mapping = compile_bind_tools(templates)

    assert len(mapping) == len(templates)
    named = {tool.name: tool for tool in tools}
    assert ABSTAIN_TOOL_NAME in named
    for tool in tools:
        assert tool.strict is True
        assert tool.stop_after_tool_call is True
        schema = tool.parameters
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert all("." not in name for name in schema["properties"])
    affordability_tool = named[bind_tool_name(mapping[next(
        name for name, template in mapping.items()
        if template.plan_template.get("analysis_type") == "affordability"
    )])]
    assert list(affordability_tool.parameters["properties"]) == ["service_inputs__purchase_minor"]


def test_round_trip_binding_reuses_the_original_template(db):
    user = default_user(db)
    template = _food_template(db)

    proposal = materialize_binding(template, _fill_for(template), today=TODAY, timezone_name=user.timezone)

    assert proposal is not None
    assert proposal.plan.queries[0].filters[0].value == "food"
    replay = execute_analysis_template(db, user.id, uuid4(), TODAY, proposal, template.id)
    assert replay.reused is True
    assert replay.template.id == template.id
    assert len(list(db.scalars(select(AnalysisToolTemplate)))) == 1


def test_binding_rejects_missing_values_and_wrong_types(db):
    user = default_user(db)
    template = _food_template(db)

    missing = dict(_fill_for(template))
    missing["query_1__start_date"] = None
    assert materialize_binding(template, missing, today=TODAY, timezone_name=user.timezone) is None

    wrong_type = _fill_for(template, **{"query_1.limit": "25"})
    assert materialize_binding(template, wrong_type, today=TODAY, timezone_name=user.timezone) is None


def test_future_query_windows_are_clamped_to_today(db):
    user = default_user(db)
    template = _food_template(db)

    proposal = materialize_binding(
        template,
        _fill_for(template, **{"query_1.end_date": "2026-12-31"}),
        today=TODAY,
        timezone_name=user.timezone,
    )

    assert proposal is not None
    assert proposal.plan.queries[0].end_date == TODAY


def test_parameterless_dedicated_templates_bind_trivially(db):
    user = default_user(db)
    seed_analysis_templates(db, today=TODAY)
    template = next(
        item for item in db.scalars(select(AnalysisToolTemplate))
        if item.plan_template.get("analysis_type") == "monthly_comparison"
    )

    proposal = materialize_binding(template, {}, today=TODAY, timezone_name=user.timezone)

    assert proposal is not None
    assert proposal.plan.analysis_type == "monthly_comparison"


def test_identity_shaped_parameters_are_refused_structurally(db):
    registry = semantic_schema_registry()
    hostile = AnalysisToolTemplate(
        capability_name="Hostile",
        capability_description="Template carrying a trusted runtime binding as a parameter.",
        capability_signature="hostile",
        template_version=ANALYSIS_TEMPLATE_VERSION,
        status=AnalysisToolStatus.ACTIVE,
        semantic_registry_version=registry.version,
        source_manifest_hash=native_manifest_fingerprint(),
        parameter_schema=[{"name": "user_id", "type": "string", "required": True}],
        plan_template={"objective": "descriptive", "analysis_type": "monthly_comparison"},
        template_hash=uuid4().hex + uuid4().hex[:32],
        validation_report={"passed": True},
    )
    db.add(hostile)
    db.flush()

    assert tenancy_safe_parameters(hostile) is False
    tools, mapping = compile_bind_tools([hostile])
    assert mapping == {}
    assert [tool.name for tool in tools] == [ABSTAIN_TOOL_NAME]
    assert materialize_binding(hostile, {"user_id": "x"}, today=TODAY, timezone_name="Asia/Kolkata") is None
