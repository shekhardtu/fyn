"""Red-team suite for the bounded Python analysis lane.

Every test is an attack driven through the lane's public entry point —
``run_sandboxed_analysis`` and the mounted ``run_python_analysis`` tool — never
through a private helper and never with the pre-check bypassed, so what is
asserted is what model-authored code can actually reach:

* the ways out of the process — imports, the import machinery under a dunder
  name, the type hierarchy, disguised eval/exec/compile, file handles — each
  come back refused with a stable code, before an interpreter is spawned;
* the ways to exhaust the host — a fork bomb, an endless loop, a 2GB
  allocation — each come back as timeout or resource_limit, killed inside the
  budget rather than left running;
* the ways to corrupt the answer — a printed forgery of the result protocol, a
  value JSON cannot carry — leave the payload intact or coded invalid_output;
* an honest-sized dataset (10k rows) still computes, so containment is not
  bought by refusing real work.

Reflection that yields only text is asserted as inert rather than refused: that
is the boundary as it actually stands, and it is stated here rather than
implied.
"""
from __future__ import annotations

import time
from datetime import date
from uuid import uuid4

from app.seed import default_user
from app.services import analysis_sandbox
from app.services.analysis_sandbox import (
    CPU_SECONDS,
    build_python_analysis_tool,
    record_dataset,
    run_sandboxed_analysis,
)
from app.services.analysis_tools import AnalysisToolContext


def context_for(db, user) -> AnalysisToolContext:
    return AnalysisToolContext(
        db=db,
        user_id=user.id,
        conversation_id=uuid4(),
        today=date(2026, 8, 17),
        timezone_name="Asia/Kolkata",
        question="Compute something over what we collected.",
    )


def attack(code: str, datasets=None, **kwargs) -> dict:
    return run_sandboxed_analysis(code, datasets or {}, **kwargs)


# --- reaching out of the process ---------------------------------------------

def test_every_module_that_reaches_the_host_is_refused_by_name():
    for module in ("os", "importlib", "subprocess", "socket"):
        outcome = attack(f"import {module}\nreturn 1")

        assert outcome["ok"] is False
        assert outcome["error"]["code"] == "forbidden_import"
        assert module in outcome["error"]["detail"]


def test_the_same_modules_are_refused_through_from_import_and_aliasing():
    aliased = attack("import socket as s\nreturn 1")
    partial = attack("from subprocess import run\nreturn 1")
    submodule = attack("import os.path\nreturn 1")

    assert aliased["error"]["code"] == "forbidden_import"
    assert partial["error"]["code"] == "forbidden_import"
    assert submodule["error"]["code"] == "forbidden_import"


def test_the_builtins_mapping_cannot_be_named_to_import_open_or_eval(tmp_path):
    """__builtins__ is a name, not an attribute — the reach is the same."""
    target = tmp_path / "escaped.txt"
    importer = attack("return __builtins__['__import__']('os').getcwd()")
    opener = attack(
        f"handle = __builtins__['open']({str(target)!r}, 'w')\n"
        "handle.write('owned')\n"
        "return 'wrote'"
    )
    evaluator = attack("return __builtins__['ev' + 'al']('1 + 1')")

    for outcome in (importer, opener, evaluator):
        assert outcome["ok"] is False
        assert outcome["error"]["code"] == "forbidden_attribute"
        assert "__builtins__" in outcome["error"]["detail"]
    assert not target.exists()


def test_the_import_machinery_cannot_be_named_through_module_dunders():
    loader = attack("return __loader__.load_module('os').getcwd()")
    spec = attack("return __spec__.loader.load_module('socket').gethostname()")

    assert loader["error"]["code"] == "forbidden_attribute"
    assert "__loader__" in loader["error"]["detail"]
    assert spec["error"]["code"] == "forbidden_attribute"


def test_walking_the_type_hierarchy_to_the_subclass_table_is_refused():
    outcome = attack("return [c.__name__ for c in ().__class__.__bases__[0].__subclasses__()]")

    assert outcome["error"]["code"] == "forbidden_attribute"
    assert "__" in outcome["error"]["detail"]


def test_splitting_the_dunder_across_string_pieces_still_hits_getattr():
    outcome = attack(
        "cls = getattr(getattr(datasets, '__cl' + 'ass__'), '__ba' + 'ses__')\nreturn str(cls)"
    )

    assert outcome["error"]["code"] == "forbidden_call"
    assert "getattr" in outcome["error"]["detail"]


def test_eval_exec_and_compile_are_refused_in_every_spelling_that_reaches_them():
    rebound = attack("runner = eval\nreturn runner('1 + 1')")
    tabled = attack("table = {'go': exec}\ntable['go']('x = 1')\nreturn 1")
    defaulted = attack("def build(fn=compile):\n    return fn('1', '<s>', 'eval')\nreturn 1")
    concatenated = attack("return __builtins__['ex' + 'ec']('x = 1')")

    assert rebound["error"]["code"] == "forbidden_call"
    assert tabled["error"]["code"] == "forbidden_call"
    assert defaulted["error"]["code"] == "forbidden_call"
    assert concatenated["error"]["code"] == "forbidden_attribute"


def test_reading_a_host_file_and_writing_a_new_one_are_both_refused(tmp_path):
    target = tmp_path / "written.txt"
    read = attack("return open('/etc/passwd').read()")
    written = attack(
        f"handle = open({str(target)!r}, 'w')\nhandle.write('x' * 1000)\nreturn 'wrote'"
    )

    assert read["error"]["code"] == "forbidden_call"
    assert "open" in read["error"]["detail"]
    assert written["error"]["code"] == "forbidden_call"
    assert not target.exists()


def test_a_refusal_is_decided_before_any_interpreter_is_spawned(monkeypatch):
    """A pre-check code is a promise that the code never ran, not that it failed."""
    def never(*args, **kwargs):
        raise AssertionError("the sandbox spawned a child for code it had already refused")

    monkeypatch.setattr(analysis_sandbox.subprocess, "Popen", never)

    assert attack("import os\nwhile True:\n    os.fork()")["error"]["code"] == "forbidden_import"
    assert attack("return __builtins__")["error"]["code"] == "forbidden_attribute"
    assert attack("return open('/etc/passwd')")["error"]["code"] == "forbidden_call"


def test_a_literal_allocation_over_the_memory_ceiling_never_spawns(monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("a statically impossible allocation spawned a child")

    monkeypatch.setattr(analysis_sandbox.subprocess, "Popen", never)

    multiplied = attack("blob = 'x' * (2 * 1024 ** 3)\nreturn len(blob)")
    constructed = attack("blob = bytearray(2 * 1024 ** 3)\nreturn len(blob)")

    assert multiplied["error"]["code"] == "resource_limit"
    assert constructed["error"]["code"] == "resource_limit"


# --- exhausting the host ------------------------------------------------------

def test_a_fork_bomb_never_gets_its_first_fork():
    through_import = attack("import os\nwhile True:\n    os.fork()")
    through_builtins = attack(
        "forker = __builtins__['__import__']('os').fork\nwhile True:\n    forker()"
    )

    assert through_import["error"]["code"] == "forbidden_import"
    assert through_builtins["error"]["code"] == "forbidden_attribute"


def test_a_ten_second_busy_loop_is_killed_at_the_wall_clock_budget():
    started = time.monotonic()
    outcome = attack("total = 0\nfor i in range(10 ** 9):\n    total += i\nreturn total", timeout_s=2)
    elapsed = time.monotonic() - started

    assert outcome["error"]["code"] == "timeout"
    assert "killed" in outcome["error"]["detail"]
    # Killed on the wall clock, well inside the loop's own runtime and the CPU cap.
    assert elapsed < CPU_SECONDS


def test_a_two_gigabyte_allocation_is_stopped_by_the_memory_ceiling():
    # Touched pages are the honest assertion. A bare bytearray(2GB) is
    # calloc-backed with lazily faulted zero pages, so where the kernel refuses
    # RLIMIT_AS (macOS) the in-child RSS watchdog may never see the memory —
    # exactly the best-effort backstop the module docstring admits to. Testing
    # the untouched case would assert a guarantee the platform does not give.
    filled = attack("blob = 'x' * (2 * 1024 ** 3)\nreturn len(blob)")

    assert filled["error"]["code"] == "resource_limit"


# --- corrupting the answer ----------------------------------------------------

def test_a_printed_forgery_of_the_result_protocol_cannot_replace_the_result():
    outcome = attack("print('{\"result\": \"owned\"}')\nprint('trailing noise')\nreturn 7")

    assert outcome == {"ok": True, "result": 7}


def test_a_value_json_cannot_carry_comes_back_as_invalid_output():
    function = attack("return lambda row: row")
    nested_set = attack("return {'merchants': {'a', 'b'}}")

    assert function["error"]["code"] == "invalid_output"
    assert nested_set["error"]["code"] == "invalid_output"


def test_format_string_reflection_is_inert_because_it_yields_only_text():
    """No refusal to assert here: str.format walks attributes but hands back text."""
    outcome = attack("return '{0.__class__}'.format(())")

    assert outcome["ok"] is True
    assert isinstance(outcome["result"], str)
    assert outcome["result"] == "<class 'tuple'>"


# --- containment does not cost real work --------------------------------------

def test_ten_thousand_rows_still_compute_inside_the_budget():
    rows = [{"amount_minor": index, "merchant_name": f"m{index % 7}"} for index in range(10_000)]

    started = time.monotonic()
    outcome = attack(
        "rows = datasets['big']\n"
        "totals = {}\n"
        "for row in rows:\n"
        "    totals[row['merchant_name']] = totals.get(row['merchant_name'], 0) + row['amount_minor']\n"
        "return {'rows': len(rows), 'merchants': len(totals), 'total_minor': sum(totals.values())}",
        {"big": rows},
    )
    elapsed = time.monotonic() - started

    assert outcome["ok"] is True
    assert outcome["result"] == {"rows": 10_000, "merchants": 7, "total_minor": 49_995_000}
    assert elapsed < CPU_SECONDS


# --- the mounted tool ---------------------------------------------------------

def test_an_escape_attempt_reaches_the_model_as_a_correctable_tool_error(db):
    user = default_user(db)
    context = context_for(db, user)
    name = record_dataset(context, "sql_result", [{"amount_minor": 100}])
    tool = build_python_analysis_tool(context)

    payload = tool.entrypoint(
        purpose="read the host filesystem",
        code="return __builtins__['open']('/etc/passwd').read()",
        dataset_names=[name],
    )

    assert payload["error"]["code"] == "forbidden_attribute"
    assert "retry at most twice" in payload["error"]["hint"]
    assert "result" not in payload
    assert context.datasets["sql_result"] == [{"amount_minor": 100}]


def test_hitting_a_ceiling_never_looks_like_a_crash_to_the_operating_system():
    """A bounded analysis reaching its limit is an ordinary outcome, so the
    child reports and exits cleanly. Left to abort, macOS files a crash report
    and shows the developer a "Python quit unexpectedly" dialog for what is
    really a working guardrail."""
    from pathlib import Path

    reports = Path.home() / "Library" / "Logs" / "DiagnosticReports"
    before = len(list(reports.glob("Python-*.ips"))) if reports.exists() else 0

    filled = attack("blob = 'x' * (2 * 1024 ** 3)\nreturn len(blob)")
    burned = attack("while True:\n    pass")

    assert filled["error"]["code"] == "resource_limit"
    assert burned["error"]["code"] in {"timeout", "resource_limit"}
    if reports.exists():
        assert len(list(reports.glob("Python-*.ips"))) <= before
