from pathlib import Path
import shutil

from app.operation_types import ConfirmationPolicy, DataEffect, ValidationMode
from app.operations.catalog import OperationCatalogManager
from app.services.agents import CopilotDecision
from app.services import capabilities as capability_service
from app.services.capabilities import (
    CapabilityId,
    capabilities_for_primitives,
    capability_for_metric,
    capability_for_primitive,
    capability_spec,
    capability_specs,
    draft_capabilities,
    mutation_capabilities,
    query_capabilities,
    safe_read_capabilities,
)


def test_protected_files_drive_the_typed_decision_contract():
    schema_values = set(CopilotDecision.model_json_schema()["$defs"]["CapabilityId"]["enum"])
    live_specs = capability_specs()

    assert schema_values == {item.value for item in CapabilityId}
    assert {item.id for item in live_specs} == set(CapabilityId)
    assert query_capabilities() <= safe_read_capabilities()


def test_validation_modes_come_from_live_file_workflows():
    assert capability_spec(capability_for_primitive("transaction.search@1")).validation is ValidationMode.DETERMINISTIC
    assert capability_spec(capability_for_metric("biggest_expenses")).validation is ValidationMode.DETERMINISTIC
    assert capability_spec(capability_for_primitive("transaction.record@1")).validation is ValidationMode.MODEL
    assert capability_spec(capability_for_primitive("taxonomy.change@1")).validation is ValidationMode.MODEL
    assert capability_spec(capability_for_primitive("analysis.run@1")).validation is ValidationMode.MODEL
    assert capability_spec(capability_for_primitive("analysis.bundle@1")).validation is ValidationMode.MODEL


def test_every_capability_declares_its_maximum_business_data_effect():
    assert draft_capabilities() == capabilities_for_primitives("agent.clarify@1")
    assert mutation_capabilities() == capabilities_for_primitives(
        "transaction.record@1",
        "transaction.find_removal@1",
        "taxonomy.change@1",
        "planning.run@1",
        "managed.dispatch@1",
    )
    assert all(
        capability_spec(capability).maximum_effect is DataEffect.NONE
        for capability in safe_read_capabilities()
    )


def test_mutation_capabilities_cannot_hide_their_confirmation_policy():
    conditional = capability_for_primitive("transaction.record@1")
    assert capability_spec(conditional).confirmation is ConfirmationPolicy.CONDITIONAL
    for capability in mutation_capabilities() - {conditional}:
        assert capability_spec(capability).confirmation is ConfirmationPolicy.REQUIRED
    assert all(
        capability_spec(capability).confirmation is ConfirmationPolicy.NEVER
        for capability in set(CapabilityId) - mutation_capabilities()
    )


def test_capability_policy_reads_the_successfully_reloaded_file_snapshot(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "operations"
    protected_root = tmp_path / "operations"
    shutil.copytree(source, protected_root)
    manager = OperationCatalogManager(protected_root)
    manager.load(initial=True)
    monkeypatch.setattr(capability_service, "operation_catalog", lambda: manager)

    review_file = protected_root / "core" / "show_reconciliation_review.yaml"
    review_file.write_text(
        review_file.read_text(encoding="utf-8").replace(
            "validation: deterministic",
            "validation: model",
        ),
        encoding="utf-8",
    )
    manager.load()

    review = capability_service.capability_for_metric("reconciliation_review")
    assert capability_service.capability_spec(review).validation is ValidationMode.MODEL
