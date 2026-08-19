import ast
from pathlib import Path

from app.services.agent_policies import (
    AGENT_POLICIES,
    PROMPT_TEMPLATE_VERSION,
    AgentMode,
    policy_instructions,
)


AGENTS_SOURCE = Path(__file__).resolve().parents[1] / "app" / "services" / "agents.py"


def test_policy_modes_have_unique_one_word_semantic_names():
    names = [policy.name for policy in AGENT_POLICIES.values()]

    assert set(AGENT_POLICIES) == set(AgentMode)
    assert len(names) == len(set(names))
    assert all(name.isalpha() and " " not in name for name in names)
    assert names == ["Operator", "Reconciler", "Suggester"]


def test_shared_prompt_orders_policy_before_task_and_dynamic_context():
    instructions = policy_instructions(
        AgentMode.OPERATE,
        task_rules=["TASK RULE"],
        context=["DYNAMIC CONTEXT"],
        output_contract="Typed decision or grounded answer.",
    )

    assert instructions[0] == f"Prompt policy version: {PROMPT_TEMPLATE_VERSION}."
    assert instructions.index("TASK RULE") < instructions.index(
        "Output contract: Typed decision or grounded answer."
    )
    assert instructions[-1] == "DYNAMIC CONTEXT"
    assert instructions.index("TASK RULE") < instructions.index("DYNAMIC CONTEXT")


def test_policy_authority_can_be_narrowed_per_invocation():
    instructions = policy_instructions(
        AgentMode.OPERATE,
        authority="May write prose only; has no tools or mutation authority.",
    )

    assert "Authority: May write prose only; has no tools or mutation authority." in instructions


def test_runtime_wires_each_policy_mode_once():
    tree = ast.parse(AGENTS_SOURCE.read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    policy_calls = [
        node for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "policy_instructions"
    ]

    assert sorted(ast.unparse(call.args[0]) for call in policy_calls) == [
        "AgentMode.OPERATE",
        "AgentMode.RECONCILE",
        "AgentMode.SUGGEST",
    ]


def test_removed_role_builders_cannot_return():
    source = AGENTS_SOURCE.read_text(encoding="utf-8")

    assert "build_conversation_writer" not in source
    assert "build_transaction_intelligence" not in source
    assert "build_unified_read_agent" not in source
    assert "build_financial_copilot" not in source
    assert "build_analysis_tool_factory" not in source
