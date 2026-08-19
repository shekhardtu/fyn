from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from app.event_time import from_local_parts
from app.models import Transaction
from app.seed import default_user
from app.services import analysis_sandbox
from app.services import analysis_tools as analysis_tools_module
from app.services.analysis_sandbox import (
    ENTRYPOINT_NAME,
    PYTHON_TOOL_NAME,
    build_python_analysis_tool,
    check_analysis_code,
    execute_checked_source,
    record_dataset,
    run_sandboxed_analysis,
)
from app.services.analysis_tools import AnalysisToolContext, build_analysis_tools
from app.services.sql_analysis import build_sql_analysis_tool

PROJECTION = """
values = [row["amount_minor"] for row in datasets["spend"]]
slope = (values[-1] - values[0]) / (len(values) - 1)
return values[-1] + slope * 3
"""


def context_for(db, user, question: str = "Project my spending.") -> AnalysisToolContext:
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


# --- the sandbox itself -------------------------------------------------------

def test_a_projection_over_a_supplied_dataset_returns_the_right_number():
    outcome = run_sandboxed_analysis(
        PROJECTION,
        {"spend": [{"amount_minor": 10_000}, {"amount_minor": 12_000}, {"amount_minor": 14_000}]},
    )

    assert outcome == {"ok": True, "result": 20_000}


def test_the_allowlisted_imports_are_usable():
    outcome = run_sandboxed_analysis(
        "import statistics\n"
        "return statistics.mean([row['v'] for row in datasets['d']])",
        {"d": [{"v": 2}, {"v": 4}, {"v": 6}]},
    )

    assert outcome == {"ok": True, "result": 4}


def test_an_import_outside_the_allowlist_is_rejected_before_anything_runs():
    outcome = run_sandboxed_analysis("import os\nreturn os.getcwd()", {})

    assert outcome["ok"] is False
    assert outcome["error"]["code"] == "forbidden_import"
    assert "os" in outcome["error"]["detail"]


def test_a_relative_import_is_rejected():
    outcome = run_sandboxed_analysis("from . import anything\nreturn 1", {})

    assert outcome["error"]["code"] == "forbidden_import"


def test_dunder_traversal_out_of_the_object_graph_is_rejected():
    outcome = run_sandboxed_analysis(
        "return [c for c in ().__class__.__base__.__subclasses__()]", {}
    )

    assert outcome["error"]["code"] == "forbidden_attribute"
    assert "__subclasses__" in outcome["error"]["detail"]
    assert run_sandboxed_analysis("return datasets.__class__", {})["error"]["detail"] == (
        "attribute '__class__' reaches outside the computation"
    )


def test_opening_a_file_is_rejected():
    outcome = run_sandboxed_analysis("return open('/etc/passwd').read()", {})

    assert outcome["error"]["code"] == "forbidden_call"
    assert "open" in outcome["error"]["detail"]


def test_eval_is_rejected_even_when_only_rebound():
    called = run_sandboxed_analysis("return eval('1 + 1')", {})
    rebound = run_sandboxed_analysis("runner = eval\nreturn runner('1 + 1')", {})

    assert called["error"]["code"] == "forbidden_call"
    assert rebound["error"]["code"] == "forbidden_call"


def test_a_with_block_is_rejected():
    outcome = run_sandboxed_analysis("with datasets as d:\n    return d", {})

    assert outcome["error"]["code"] == "forbidden_call"


def test_unparseable_code_is_rejected_with_the_submitted_line_number():
    outcome = run_sandboxed_analysis("total = 1\nreturn total +", {})

    assert outcome["error"]["code"] == "syntax_error"
    assert outcome["error"]["detail"].startswith("line 2:")


def test_an_endless_loop_is_killed_and_coded_as_a_timeout():
    outcome = run_sandboxed_analysis("while True:\n    pass", {}, timeout_s=1)

    assert outcome["error"]["code"] == "timeout"
    assert "killed" in outcome["error"]["detail"]


def test_a_memory_hog_is_stopped_by_the_memory_ceiling():
    outcome = run_sandboxed_analysis("blob = 'x' * 10**9\nreturn len(blob)", {})

    assert outcome["error"]["code"] == "resource_limit"


def test_the_refused_names_are_absent_when_the_pre_check_is_bypassed():
    """The second layer: the namespace itself holds no file, import or compiler."""
    def run(body: str) -> dict:
        return execute_checked_source(f"def {ENTRYPOINT_NAME}(datasets):\n{body}\n", {})

    opened = run("    return open('escape.txt', 'w')")
    imported = run("    import socket\n    return 1")
    computed = run("    import statistics\n    return statistics.mean([2, 4])")

    assert opened["error"]["code"] == "execution_error"
    assert "'open' is not defined" in opened["error"]["detail"]
    assert imported["error"]["code"] == "execution_error"
    assert "not allowed" in imported["error"]["detail"]
    assert computed == {"ok": True, "result": 3}


def test_a_file_write_fails_even_when_the_pre_check_is_bypassed():
    """The third layer: a handle rebuilt through the type hierarchy still hits
    the kernel's write ceiling."""
    source = (
        f"def {ENTRYPOINT_NAME}(datasets):\n"
        "    base = [c for c in ().__class__.__base__.__subclasses__()"
        " if c.__name__ == '_IOBase'][0]\n"
        "    raw = [c for c in base.__subclasses__() if c.__name__ == '_RawIOBase'][0]\n"
        "    opener = [c for c in raw.__subclasses__() if c.__name__ == 'FileIO'][0]\n"
        "    handle = opener('escape.txt', 'w')\n"
        "    handle.write(b'x' * 5000)\n"
        "    return 'wrote'\n"
    )

    outcome = execute_checked_source(source, {})

    assert outcome["ok"] is False
    assert outcome["error"]["code"] == "resource_limit"


def test_a_child_that_prints_something_other_than_one_json_object_is_coded(monkeypatch):
    monkeypatch.setattr(
        analysis_sandbox,
        "CHILD_PROGRAM",
        "import sys\nsys.stdin.read()\nsys.stdout.write('not json at all')\n",
    )

    outcome = run_sandboxed_analysis("return 1", {})

    assert outcome["error"]["code"] == "invalid_output"
    assert "not json at all" in outcome["error"]["detail"]


def test_a_value_that_cannot_be_serialized_is_coded_invalid_output():
    outcome = run_sandboxed_analysis("return {1, 2, 3}", {})

    assert outcome["error"]["code"] == "invalid_output"


def test_a_raising_analysis_comes_back_as_an_execution_error():
    outcome = run_sandboxed_analysis("return datasets['missing']", {})

    assert outcome["error"]["code"] == "execution_error"
    assert "KeyError" in outcome["error"]["detail"]


def test_printing_does_not_corrupt_the_result():
    outcome = run_sandboxed_analysis("print('working')\nreturn 7", {})

    assert outcome == {"ok": True, "result": 7}


def test_a_spawn_failure_is_returned_rather_than_raised(monkeypatch):
    def explode(*args, **kwargs):
        raise OSError("no processes available")

    monkeypatch.setattr(analysis_sandbox.subprocess, "Popen", explode)

    outcome = run_sandboxed_analysis("return 1", {})

    assert outcome["error"]["code"] == "execution_error"
    assert "no processes available" in outcome["error"]["detail"]


def test_the_checked_source_is_exactly_what_the_child_compiles():
    source = check_analysis_code("return 1")

    assert source.startswith(f"def {ENTRYPOINT_NAME}(datasets):")
    assert "return 1" in source


# --- the agent tool -----------------------------------------------------------

def test_the_tool_computes_over_a_dataset_collected_this_turn(db):
    user = default_user(db)
    context = context_for(db, user)
    name = record_dataset(context, "sql_result", [{"amount_minor": 100}, {"amount_minor": 250}])
    tool = build_python_analysis_tool(context)

    payload = tool.entrypoint(
        purpose="total the collected rows",
        code="return sum(row['amount_minor'] for row in datasets['sql_result'])",
        dataset_names=[name],
    )

    assert payload["kind"] == "python_analysis"
    assert payload["result"] == 350
    assert payload["datasets_used"] == ["sql_result"]


def test_the_tool_refuses_a_dataset_that_was_not_collected_this_turn(db):
    user = default_user(db)
    context = context_for(db, user)
    record_dataset(context, "sql_result", [{"amount_minor": 100}])
    tool = build_python_analysis_tool(context)

    payload = tool.entrypoint(
        purpose="read something that was never fetched",
        code="return len(datasets['transactions'])",
        dataset_names=["transactions"],
    )

    assert payload["error"]["code"] == "unknown_dataset"
    assert payload["error"]["available"] == ["sql_result"]
    assert "retry" in payload["error"]["hint"].lower()


def test_the_tool_returns_typed_errors_and_never_raises(db):
    user = default_user(db)
    tool = build_python_analysis_tool(context_for(db, user))

    rejected = tool.entrypoint(purpose="escape", code="import socket\nreturn 1", dataset_names=[])
    broken = tool.entrypoint(purpose="crash", code="return 1/0", dataset_names=[])

    assert rejected["error"]["code"] == "forbidden_import"
    assert "retry at most twice" in rejected["error"]["hint"]
    assert broken["error"]["code"] == "execution_error"
    assert "retry at most twice" in broken["error"]["hint"]


def test_datasets_are_copies_so_the_sandbox_cannot_change_server_state(db):
    user = default_user(db)
    context = context_for(db, user)
    rows = [{"amount_minor": 100}]
    name = record_dataset(context, "sql_result", rows)
    tool = build_python_analysis_tool(context)

    payload = tool.entrypoint(
        purpose="mutate the rows it was handed",
        code=(
            "for row in datasets['sql_result']:\n"
            "    row['amount_minor'] = 999999\n"
            "datasets['sql_result'].append({'amount_minor': -1})\n"
            "return len(datasets['sql_result'])"
        ),
        dataset_names=[name],
    )

    assert payload["result"] == 2
    assert rows == [{"amount_minor": 100}]
    assert context.datasets["sql_result"] == [{"amount_minor": 100}]


def test_the_tool_description_states_its_boundary_and_this_turns_datasets(db):
    user = default_user(db)
    context = context_for(db, user)
    record_dataset(context, "sql_result", [{"merchant_name": "Blue Tokai", "total_minor": 40_000}])

    description = build_python_analysis_tool(context).description

    assert "math, statistics" in description or "statistics" in description
    assert "sql_result (1 rows): merchant_name, total_minor" in description
    assert "512MB" in description and "10s" in description
    assert "minor units" in description
    assert "JSON-serializable" in description


def test_recorded_names_stay_stable_and_never_collide(db):
    user = default_user(db)
    context = context_for(db, user)

    first = record_dataset(context, "sql_result", [{"a": 1}])
    second = record_dataset(context, "sql_result", [{"a": 2}])

    assert (first, second) == ("sql_result", "sql_result_2")
    assert context.datasets["sql_result"] == [{"a": 1}]


def test_the_sql_lane_records_its_rows_for_the_python_lane(db):
    user = default_user(db)
    db.add(expense(user.id, "Blue Tokai", 40_000))
    db.commit()
    context = context_for(db, user)

    sql_payload = build_sql_analysis_tool(context).entrypoint(
        purpose="total spend",
        sql="SELECT SUM(amount_minor) AS total_minor FROM transactions",
    )
    python_payload = build_python_analysis_tool(context).entrypoint(
        purpose="double the collected total",
        code="return datasets['sql_result'][0]['total_minor'] * 2",
        dataset_names=[sql_payload["dataset_name"]],
    )

    assert sql_payload["dataset_name"] == "sql_result"
    assert python_payload["result"] == 80_000


def test_the_lane_is_mounted_behind_its_flag(db, monkeypatch):
    user = default_user(db)
    context = context_for(db, user)
    lanes = {
        "sql_lane_enabled": False,
        "external_source_lane_enabled": False,
        "federation_lane_enabled": False,
    }

    monkeypatch.setattr(
        analysis_tools_module, "get_settings",
        lambda: SimpleNamespace(python_lane_enabled=True, **lanes),
    )
    assert PYTHON_TOOL_NAME in {tool.name for tool in build_analysis_tools(context)}

    monkeypatch.setattr(
        analysis_tools_module, "get_settings",
        lambda: SimpleNamespace(python_lane_enabled=False, **lanes),
    )
    assert PYTHON_TOOL_NAME not in {tool.name for tool in build_analysis_tools(context)}
