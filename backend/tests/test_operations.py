from __future__ import annotations

from datetime import date
from pathlib import Path
import json
import shutil

import pytest
import yaml
from sqlalchemy import select

from app.models import Conversation, User
from app.operations.catalog import OperationCatalogError, OperationCatalogManager, compile_operation
from app.operations.execution import (
    OperationChangedError,
    execute_operation,
    execute_operation_steps,
    resolve_current_operation,
)
from app.operations.tools import (
    build_operation_proposal_tool,
    operation_tool_name,
    proposal_from_tool_execution,
)
from agno.models.response import ToolExecution
from app.services.taxonomy import TaxonomyRepository
from app.services import agents as agent_service
from app.services import conversation as conversation_service
from app.services.capabilities import CapabilityId, capability_invokes
from app.domain import WidgetActionId


CORE_ROOT = Path(__file__).resolve().parents[1] / "operations"


def operation_yaml(*, version: int = 1, effect: str = "mutation", extra: str = "") -> str:
    return f"""
apiVersion: fyn.ai/v1
kind: Operation
metadata:
  id: ops.taxonomy.create_path
  version: {version}
  title: Create category path
  namespace: finance
  owner: operations
  enabled: true
discovery:
  description: Create a user-owned category and its subcategories.
  aliases: [create category path]
  examples: [Create Pet Care with Vet underneath it]
  negativeExamples: [Show my categories]
eligibility:
  requiredPermissions: [taxonomy.write]
  expectedEffect: {effect}
  access: workflow
routing:
  strategy: managed
  bindings: {{}}
input:
  schema:
    type: object
    additionalProperties: false
    required: [category, subcategories]
    properties:
      category: {{type: string, title: Category, minLength: 1, maxLength: 80}}
      subcategories:
        type: array
        title: Subcategories
        minItems: 1
        maxItems: 10
        uniqueItems: true
        items: {{type: string, minLength: 1, maxLength: 80}}
clarification:
  requiredFields: [category, subcategories]
  prompt: Name the category and its subcategories.
instructions:
  selection: Use only when the user asks to create a category hierarchy.
  argumentCollection: Preserve the supplied names.
  response: Report success only after execution.
execution:
  label: Creating category hierarchy
  steps:
    - id: create_category
      uses: taxonomy.create_category@1
      with:
        name: ${{input.category}}
    - id: create_subcategories
      forEach: ${{input.subcategories}}
      uses: taxonomy.create_subcategory@1
      with:
        parentId: ${{steps.create_category.output.id}}
        name: ${{item}}
approval:
  minimum: required
  title: Create category?
  summary: Create “${{input.category}}” and its subcategories.
presentation:
  success:
    title: Category created
    message: '“${{input.category}}” was created.'
  failure: {{title: Category unchanged, message: No category was created.}}
tests:
  - name: matching request
    request: Create Pet Care with Vet underneath it
    expectedInputs: {{category: Pet Care, subcategories: [Vet]}}
    expectedEffect: mutation
    expectedApproval: required
  - name: read request
    request: Show my categories
    expectedMatch: false
{extra}
"""


def write_operation(directory: Path, **kwargs) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "create-path.yaml"
    path.write_text(operation_yaml(**kwargs), encoding="utf-8")
    return path


def test_managed_operation_compiles_effect_permissions_and_bounded_foreach(tmp_path):
    operation = compile_operation(write_operation(tmp_path), "managed")

    assert operation.id == "ops.taxonomy.create_path"
    assert operation.derived_effect == "mutation"
    assert operation.derived_confirmation == "required"
    assert operation.derived_permissions == ["taxonomy.write"]
    assert len(operation.definition.execution.steps) == 2


def test_catalog_uses_bounded_discovery_candidates(tmp_path):
    write_operation(tmp_path)
    manager = OperationCatalogManager(CORE_ROOT, tmp_path)
    snapshot = manager.load(initial=True)

    assert "ops.taxonomy.create_path" in snapshot.discovery_index["pet"]
    assert [item.id for item, _score in manager.candidates("Create Pet Care", limit=1)] == [
        "ops.taxonomy.create_path"
    ]
    assert manager.candidates("reconcile payroll", limit=1) == []


def test_small_catalog_discovery_does_not_depend_on_shared_verbs(tmp_path):
    write_operation(tmp_path)
    manager = OperationCatalogManager(CORE_ROOT, tmp_path)
    manager.load(initial=True)

    # The wording shares no discovery token with the operation file. For a
    # bounded catalog, the language model still receives the complete typed
    # operation tool and can compare its meaning instead of relying on a hidden
    # verb list in application code.
    candidates = manager.candidate_operations(
        "Establish a care hierarchy for animals",
        limit=12,
    )

    assert [item.id for item in candidates] == ["ops.taxonomy.create_path"]


def test_operation_file_compiles_to_a_closed_strict_proposal_tool(tmp_path):
    operation = compile_operation(write_operation(tmp_path), "managed")
    tool = build_operation_proposal_tool(operation)
    schema = tool.parameters

    assert tool.name == operation_tool_name(operation)
    assert tool.strict is True
    assert tool.stop_after_tool_call is True
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"category", "subcategories"}
    assert set(schema["properties"]) == {"category", "subcategories"}
    assert "operation_id" not in schema["properties"]
    assert "uniqueItems" not in schema["properties"]["subcategories"]["anyOf"][0]
    assert "uniqueItems" in operation.definition.input.schema_["properties"]["subcategories"]
    assert any(
        option.get("type") == "null"
        for option in schema["properties"]["category"]["anyOf"]
    )


def test_proposal_binds_server_owned_revision_and_preserves_only_supplied_inputs(tmp_path):
    operation = compile_operation(write_operation(tmp_path), "managed")
    tool = build_operation_proposal_tool(operation)
    execution = ToolExecution(
        tool_name=tool.name,
        tool_args={"category": "Pet Care", "subcategories": None},
        result="ignored model-visible result",
    )

    proposal = proposal_from_tool_execution(execution, [operation])

    assert proposal is not None
    assert proposal.operation_id == operation.id
    assert proposal.version == operation.version
    assert proposal.checksum == operation.checksum
    assert proposal.inputs == {"category": "Pet Care"}


def test_operator_operation_call_becomes_an_internal_revision_bound_decision(
    tmp_path,
    monkeypatch,
):
    from agno.run.agent import RunOutput

    write_operation(tmp_path)
    manager = OperationCatalogManager(CORE_ROOT, tmp_path)
    operation = manager.load(initial=True).operation("ops.taxonomy.create_path")
    tool_name = operation_tool_name(operation)
    execution = ToolExecution(
        tool_name=tool_name,
        tool_args={"category": "Pet Care", "subcategories": ["Vet"]},
        result="proposal accepted",
    )

    class StubOperator:
        def run(self, *_args, **_kwargs):
            return iter([RunOutput(content=None, tools=[execution])])

    monkeypatch.setattr(agent_service, "operation_catalog", lambda: manager)
    monkeypatch.setattr(agent_service, "build_operator", lambda *args, **kwargs: StubOperator())

    result = agent_service.run_operator(
        "Establish Pet Care with Vet beneath it",
        [],
        date(2026, 8, 16),
        "Asia/Kolkata",
        [],
    )

    proposal = result.operation
    assert result.reply is None
    assert proposal is not None
    assert proposal.operation_id == operation.id
    assert proposal.version == operation.version
    assert proposal.checksum == operation.checksum
    assert proposal.inputs == {
        "category": "Pet Care",
        "subcategories": ["Vet"],
    }

    decision = agent_service.filesystem_operation_decision(
        proposal.operation_id,
        proposal.inputs,
        confidence=1.0,
        reason="Selected a strictly typed filesystem operation proposal.",
        expected_version=proposal.version,
        expected_checksum=proposal.checksum,
    )

    assert decision is not None
    assert decision.operation_id == operation.id
    assert decision.operation_version == operation.version
    assert decision.operation_checksum == operation.checksum
    assert decision.operation_inputs == {
        "category": "Pet Care",
        "subcategories": ["Vet"],
    }


def test_every_core_operation_is_a_tested_step_workflow():
    snapshot = OperationCatalogManager(CORE_ROOT).load(initial=True)

    assert {operation.id for operation in snapshot.core_operations} == {
        capability.value for capability in CapabilityId
    }
    for operation in snapshot.core_operations:
        assert operation.definition.tests, operation.id
        assert operation.definition.execution.steps, operation.id
        assert "adapter:" not in Path(operation.source_path).read_text(encoding="utf-8")
        inputs = next(
            (
                case.expected_inputs
                for case in operation.definition.tests
                if case.expected_inputs is not None
            ),
            {},
        )
        outputs = execute_operation_steps(
            operation,
            inputs,
            lambda target, arguments: {"primitive": target.reference, "arguments": arguments},
        )
        final_step = operation.definition.execution.steps[-1]
        assert outputs[final_step.id]["primitive"] == final_step.uses


def test_application_source_has_no_hand_maintained_capability_members():
    app_root = Path(__file__).resolve().parents[1] / "app"
    sources = {
        path: path.read_text(encoding="utf-8")
        for path in app_root.rglob("*.py")
    }

    assert "class CapabilityId" not in sources[app_root / "services" / "capabilities.py"]
    assert not {
        str(path.relative_to(app_root)): line
        for path, source in sources.items()
        for line in source.splitlines()
        if "CapabilityId." in line
    }


def test_every_selectable_core_example_compiles_from_file_owned_inputs():
    snapshot = OperationCatalogManager(CORE_ROOT).load(initial=True)
    for operation in snapshot.core_operations:
        if (
            not operation.definition.discovery.model_selectable
            or operation.definition.routing.strategy != "decision"
        ):
            continue
        contract = operation.definition.tests[0]
        decision = agent_service.filesystem_operation_decision(
            operation.id,
            contract.expected_inputs or {},
            confidence=1,
            reason=f"Contract test for {operation.id}",
            expected_version=operation.version,
            expected_checksum=operation.checksum,
        )
        assert decision is not None, operation.id
        assert decision.tool.value == operation.id
        assert decision.operation_id == operation.id
        assert decision.operation_checksum == operation.checksum


@pytest.mark.parametrize(
    ("operation_id", "user_request"),
    [
        ("calculate_affordability", "Can I afford this purchase"),
        ("calculate_loan", "Calculate loan prepayment savings"),
        ("calculate_investment_projection", "Project my SIP growth"),
    ],
)
def test_calculator_operation_examples_execute_end_to_end(db, operation_id, user_request):
    user = db.scalar(select(User).order_by(User.created_at, User.id))
    conversation = Conversation(user_id=user.id, title=f"Operation {operation_id}")
    db.add(conversation)
    db.flush()

    response = conversation_service.handle_chat(db, user, conversation, user_request)

    assert response.task_status in {"succeeded", "needs_input"}
    # Calculators keep their HITL form widgets; affordability answers in markdown.
    assert response.widgets or "₹" in response.message


def test_json_operation_uses_the_same_contract(tmp_path):
    document = yaml.safe_load(operation_yaml())
    path = tmp_path / "create-path.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    operation = compile_operation(path, "managed")

    assert operation.id == "ops.taxonomy.create_path"
    assert operation.derived_permissions == ["taxonomy.write"]


def test_managed_operation_cannot_downgrade_primitive_effect(tmp_path):
    path = write_operation(tmp_path, effect="none")

    with pytest.raises(OperationCatalogError, match="Effect-free operations|does not match derived effect"):
        compile_operation(path, "managed")


def test_catalog_reload_is_atomic_and_retains_last_known_good(tmp_path):
    path = write_operation(tmp_path)
    manager = OperationCatalogManager(CORE_ROOT, tmp_path)
    first = manager.load(initial=True)

    path.write_text(operation_yaml(extra="unexpectedField: true"), encoding="utf-8")
    after_failure = manager.load()

    assert after_failure is first
    assert manager.health().status == "degraded"
    assert manager.health().last_error_code == "validation_error"


def test_managed_topology_hot_reloads_without_source_wiring(tmp_path):
    managed = tmp_path / "managed"
    managed.mkdir()
    manager = OperationCatalogManager(CORE_ROOT, managed)
    first = manager.load(initial=True)

    write_operation(managed)
    refreshed = manager.load()

    assert refreshed is not first
    assert refreshed.operation("ops.taxonomy.create_path") is not None
    assert refreshed.operation("ops.taxonomy.create_path").source == "managed"


def test_protected_capability_topology_change_requires_restart(tmp_path):
    protected_root = tmp_path / "operations"
    shutil.copytree(CORE_ROOT, protected_root)
    manager = OperationCatalogManager(protected_root)
    first = manager.load(initial=True)

    conversation_file = protected_root / "core" / "conversation.yaml"
    extra_file = protected_root / "core" / "extra_conversation.yaml"
    extra_file.write_text(
        conversation_file.read_text(encoding="utf-8").replace(
            "id: conversation,",
            "id: extra_conversation,",
        ),
        encoding="utf-8",
    )
    after_change = manager.load()

    assert after_change is first
    assert manager.health().status == "degraded"
    assert manager.health().last_error_code == "core_topology_requires_restart"


def test_configured_managed_directory_must_be_available(tmp_path):
    missing = tmp_path / "missing-managed-mount"
    manager = OperationCatalogManager(CORE_ROOT, missing)

    with pytest.raises(OperationCatalogError) as error:
        manager.load(initial=True)

    assert error.value.code == "managed_catalog_unavailable"
    assert manager.health().status == "uninitialized"


def test_core_operation_cannot_be_overridden_from_managed_directory(tmp_path):
    path = write_operation(tmp_path)
    path.write_text(operation_yaml().replace("ops.taxonomy.create_path", "conversation"), encoding="utf-8")

    with pytest.raises(OperationCatalogError) as error:
        OperationCatalogManager(CORE_ROOT, tmp_path).load(initial=True)

    assert error.value.code == "protected_operation_override"


def test_operation_executes_atomically_and_keeps_taxonomy_user_scoped(db, tmp_path):
    operation = compile_operation(write_operation(tmp_path), "managed")
    owner = db.scalar(select(User).order_by(User.created_at, User.id))
    other = User(display_name="Other", email="other-operations@example.test", currency="INR", timezone="Asia/Kolkata")
    db.add(other)
    db.flush()

    result = execute_operation(
        db,
        owner,
        operation,
        {"category": "Pet Care", "subcategories": ["Vet"]},
    )

    owner_names = {item.name for item in TaxonomyRepository(db, owner.id).expense_categories()}
    other_names = {item.name for item in TaxonomyRepository(db, other.id).expense_categories()}
    assert result.message == "“Pet Care” was created."
    assert "Pet Care" in owner_names
    assert "Pet Care" not in other_names


def test_pending_reference_requires_reapproval_after_file_change(tmp_path):
    path = write_operation(tmp_path)
    manager = OperationCatalogManager(CORE_ROOT, tmp_path)
    previous = manager.load(initial=True).operation("ops.taxonomy.create_path")
    path.write_text(operation_yaml(version=2), encoding="utf-8")
    manager.load()

    with pytest.raises(OperationChangedError, match="changed"):
        resolve_current_operation(manager, previous.id, previous.version, previous.checksum)


def test_managed_decision_uses_one_approval_then_executes(db, tmp_path, monkeypatch):
    write_operation(tmp_path)
    manager = OperationCatalogManager(CORE_ROOT, tmp_path)
    manager.load(initial=True)
    monkeypatch.setattr(agent_service, "operation_catalog", lambda: manager)
    monkeypatch.setattr(conversation_service, "operation_catalog", lambda: manager)
    user = db.scalar(select(User).order_by(User.created_at, User.id))
    conversation = db.scalar(select(Conversation).where(Conversation.user_id == user.id))
    if conversation is None:
        conversation = Conversation(user_id=user.id, title="Operations")
        db.add(conversation)
        db.flush()

    decision = agent_service.filesystem_operation_decision(
        "ops.taxonomy.create_path",
        {"category": "Pet Care", "subcategories": ["Vet"]},
        confidence=1,
        reason="Exact managed operation match",
    )
    approval = conversation_service._managed_operation_response(db, user, conversation, decision)

    assert approval.pending_action.action is WidgetActionId.APPROVE_OPERATION
    assert approval.widgets[0].type == "operation_approval"
    action = approval.widgets[0].actions[0]
    completed = conversation_service.handle_action(
        db,
        user,
        conversation,
        action.action,
        action.payload,
    )
    assert completed.pending_action is None
    assert completed.widgets == []
    assert "**" in completed.message
    assert "Pet Care" in {item.name for item in TaxonomyRepository(db, user.id).expense_categories()}


def test_prose_metric_label_never_fails_a_valid_search_proposal():
    # Production failure 2026-08-17 (run f916cc3e): the Operator proposed a
    # perfect search_transactions operation but wrote the advisory metric label
    # as prose ("grocery transactions"), and the typed contract rejected the
    # whole proposal. The label is normalized deterministically; the financial
    # fields must bind unchanged.
    decision = agent_service.filesystem_operation_decision(
        "search_transactions",
        {
            "limit": 100,
            "metric": "grocery transactions",
            "end_date": "2026-08-31",
            "operation": "total",
            "start_date": "2026-08-01",
            "result_mode": "summary",
            "category_slug": "food",
            "sort_direction": "asc",
            "subcategory_slug": "groceries",
            "transaction_type": "expense",
            "use_active_scope": False,
        },
        confidence=1.0,
        reason="Typed search proposal with a prose metric label",
    )

    assert decision is not None
    assert decision.tool.value == "search_transactions"
    assert decision.query.metric == "grocery_transactions"
    assert decision.query.category_slug == "food"
    assert decision.query.subcategory_slug == "groceries"
    assert decision.query.result_mode == "summary"

    unsalvageable = agent_service.filesystem_operation_decision(
        "search_transactions",
        {"metric": "££££", "result_mode": "summary", "operation": "total"},
        confidence=1.0,
        reason="Metric label with no identifier content",
    )
    assert unsalvageable is not None
    # An unusable label is dropped so the contract default applies.
    assert unsalvageable.query.metric == "transaction_summary"


def test_changed_operation_never_executes_under_old_approval(db, tmp_path, monkeypatch):
    path = write_operation(tmp_path)
    manager = OperationCatalogManager(CORE_ROOT, tmp_path)
    manager.load(initial=True)
    monkeypatch.setattr(agent_service, "operation_catalog", lambda: manager)
    monkeypatch.setattr(conversation_service, "operation_catalog", lambda: manager)
    user = db.scalar(select(User).order_by(User.created_at, User.id))
    conversation = db.scalar(select(Conversation).where(Conversation.user_id == user.id))
    if conversation is None:
        conversation = Conversation(user_id=user.id, title="Operations")
        db.add(conversation)
        db.flush()
    decision = agent_service.filesystem_operation_decision(
        "ops.taxonomy.create_path",
        {"category": "Pet Care", "subcategories": ["Vet"]},
        confidence=1,
        reason="Exact managed operation match",
    )
    approval = conversation_service._managed_operation_response(db, user, conversation, decision)
    old_action = approval.widgets[0].actions[0]

    path.write_text(operation_yaml(version=2), encoding="utf-8")
    manager.load()
    refreshed = conversation_service.handle_action(
        db,
        user,
        conversation,
        old_action.action,
        old_action.payload,
    )

    assert refreshed.pending_action.action is WidgetActionId.APPROVE_OPERATION
    assert "changed after the previous review" in refreshed.message
    assert refreshed.widgets[0].data["operationChecksum"] != old_action.payload["operationChecksum"]
    assert "Pet Care" not in {item.name for item in TaxonomyRepository(db, user.id).expense_categories()}
