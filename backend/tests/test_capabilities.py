from app.services.agents import CopilotDecision
from app.services.capabilities import CAPABILITY_REGISTRY, CapabilityId, QUERY_CAPABILITIES, SAFE_READ_CAPABILITIES, ValidationMode, capability_spec


def test_capability_registry_drives_the_typed_decision_contract():
    schema_values = set(CopilotDecision.model_json_schema()["$defs"]["CapabilityId"]["enum"])

    assert schema_values == {item.value for item in CapabilityId}
    assert {item.id for item in CAPABILITY_REGISTRY} == set(CapabilityId)
    assert QUERY_CAPABILITIES <= SAFE_READ_CAPABILITIES


def test_model_validation_is_reserved_for_mutation_and_complex_workflows():
    assert capability_spec(CapabilityId.SEARCH_TRANSACTIONS).validation is ValidationMode.DETERMINISTIC
    assert capability_spec(CapabilityId.GET_SPENDING_SUMMARY).validation is ValidationMode.DETERMINISTIC
    assert capability_spec(CapabilityId.CREATE_TRANSACTION_DRAFT).validation is ValidationMode.MODEL
    assert capability_spec(CapabilityId.MANAGE_TAXONOMY).validation is ValidationMode.MODEL
    assert capability_spec(CapabilityId.RUN_ANALYSIS_HARNESS).validation is ValidationMode.MODEL
    assert capability_spec(CapabilityId.RUN_QUERY_BUNDLE).validation is ValidationMode.MODEL
