from app.services.agents import CopilotDecision
from app.services.capabilities import CAPABILITY_REGISTRY, CapabilityId, QUERY_CAPABILITIES, SAFE_READ_CAPABILITIES


def test_capability_registry_drives_the_typed_decision_contract():
    schema_values = set(CopilotDecision.model_json_schema()["$defs"]["CapabilityId"]["enum"])

    assert schema_values == {item.value for item in CapabilityId}
    assert {item.id for item in CAPABILITY_REGISTRY} == set(CapabilityId)
    assert QUERY_CAPABILITIES <= SAFE_READ_CAPABILITIES
