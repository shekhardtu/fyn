"""The bounded Python analysis lane: what SQL cannot express.

Forecasting, statistical tests and scenario modelling need arithmetic over
rows that were already retrieved, not another SELECT. This lane runs
model-authored Python against datasets the governed lanes produced earlier in
the same turn: an AST pre-check refuses the code shapes that reach outside the
computation, and whatever survives runs in a short-lived child interpreter
under POSIX resource limits, with no database handle, no connection, and no
Python object shared with the process that called it.

Honest limits — the AST pre-check and the resource limits are a *development*
boundary, not a security sandbox. Neither layer claims to stop a determined
escape from CPython bytecode: the pre-check can only refuse the shapes it can
name, and the child still runs with this server's OS credentials, so it can
read whatever this server can read. What is actually enforced today is narrow
and worth stating plainly:

* imports restricted to a pure-computation allowlist, with dunder names, dunder
  attribute traversal and the reflection builtins refused before anything
  executes;
* a separate process holding no inherited Python objects — datasets cross as
  JSON, so nothing the child mutates can reach server state;
* the analysis running against a builtins mapping stripped of the same names
  the pre-check refuses, with an ``__import__`` that honours the allowlist, so
  a code shape the pre-check fails to name still reaches no import machinery,
  no file handle and no compiler;
* CPU seconds, open files and written bytes capped by ``resource.setrlimit``,
  plus a wall-clock kill and a peak-RSS ceiling;
* ``RLIMIT_AS`` is unavailable on some platforms (macOS answers EINVAL), so
  there the memory ceiling degrades to the in-child RSS watchdog, which is a
  best-effort backstop and not a kernel guarantee.

Not enforced here at all: network egress and filesystem *reads*. Those need
OS-level isolation — a container with a dropped network namespace and a
read-only mount — which arrives with container deployment, not with this
module. Until then this lane is safe against model mistakes and careless
code, not against an adversary who controls the submitted source.
"""
from __future__ import annotations

import ast
import json
import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
import textwrap
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .agent_tools import bind_schema_tool

PYTHON_TOOL_NAME = "run_python_analysis"
ENTRYPOINT_NAME = "_fyn_analysis"

# Pure computation only: nothing here opens a file, a socket or a process.
ALLOWED_IMPORTS = frozenset({
    "math", "statistics", "datetime", "json", "itertools", "collections", "decimal",
})
# Reflection and code-loading builtins, refused by name rather than by call so
# that rebinding one (``f = eval``) is rejected just as plainly.
FORBIDDEN_NAMES = frozenset({
    "exec", "eval", "compile", "open", "__import__", "getattr", "setattr",
    "delattr", "globals", "locals", "vars", "input", "breakpoint",
})
# Non-dunder attributes that still walk out of the object graph into frames,
# code objects and the type hierarchy.
FORBIDDEN_ATTRIBUTES = frozenset({
    "mro", "gi_frame", "gi_code", "cr_frame", "f_globals", "f_locals",
    "f_builtins", "tb_frame", "func_globals", "func_code",
})

CPU_SECONDS = 5
MEMORY_BYTES = 512 * 1024 * 1024
MAX_OPEN_FILES = 32
DEFAULT_TIMEOUT_S = 10
# How long to wait for pipes to drain after the group is killed. Bounded on
# purpose: a surviving descendant must not convert a timeout into a hang.
_KILL_DRAIN_S = 2
MAX_DETAIL_CHARS = 400

# Stable codes. The four pre-check codes are decided before anything runs;
# the four execution codes are decided from the child's exit. ``with`` and
# ``async`` constructs report as forbidden_call — they are refused for the
# same reason the calls are, and the code set stays closed.
PRECHECK_CODES = frozenset({
    "forbidden_import", "forbidden_attribute", "forbidden_call", "syntax_error",
})
EXECUTION_CODES = frozenset({
    "invalid_output", "execution_error", "timeout", "resource_limit",
})

_CHILD_ENV = {"PATH": "/usr/bin:/bin"}
_LIMIT_SIGNALS = frozenset(
    number for number in (
        getattr(signal, "SIGKILL", None),
        getattr(signal, "SIGXCPU", None),
        getattr(signal, "SIGXFSZ", None),
    ) if number is not None
)

_CORRECTION_HINTS = {
    "forbidden_import": (
        "Only math, statistics, datetime, json, itertools, collections and decimal are "
        "importable. Rewrite without the rejected module and retry at most twice."
    ),
    "forbidden_attribute": (
        "Dunder names and dunder or frame attributes are rejected. Work with the dataset "
        "rows directly and retry at most twice."
    ),
    "forbidden_call": (
        "eval, exec, compile, open, getattr, globals and `with`/`async` blocks are rejected. "
        "Rewrite as plain computation and retry at most twice."
    ),
    "syntax_error": "Fix the reported line and call the tool again, at most twice.",
    "timeout": (
        "The computation ran past its wall-clock budget. Do less work — fewer iterations, "
        "closed-form arithmetic — and retry at most twice."
    ),
    "resource_limit": (
        "The computation ran past its CPU, memory or file budget. Compute over the supplied "
        "rows instead of building large intermediates, and retry at most twice."
    ),
    "invalid_output": (
        "Return a JSON-serializable value: numbers, strings, booleans, lists and dicts of "
        "those. Retry at most twice."
    ),
    "execution_error": (
        "The code raised. Read the detail, correct the code against the dataset columns and "
        "retry at most twice."
    ),
}
_DEFAULT_HINT = "Correct the code against this tool's description and retry at most twice."


class SandboxRejected(Exception):
    """An AST pre-check refusal, carrying the stable code that explains it."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


# --- dataset registry ---------------------------------------------------------

def _json_scalar(value: Any) -> Any:
    """One cell, as the JSON the child will receive.

    Money stays exact: an integral Decimal (every minor-unit sum) crosses as an
    int rather than a float, so the sandbox never inherits a rounding error the
    database did not have.
    """
    if isinstance(value, bool) or value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def json_safe_rows(rows: Any) -> list[dict[str, Any]]:
    """Result rows reduced to JSON scalars, copied away from their source."""
    return [
        {str(key): _json_scalar(value) for key, value in dict(row).items()}
        for row in rows or []
    ]


def record_dataset(context, base_name: str, rows: Any) -> str:
    """Register gate-approved result rows as a named dataset for this turn.

    The Python lane never opens a connection: every dataset it can read is a
    result some governed lane already returned in this same turn, recorded
    here under a stable name that lane hands back in its own tool payload.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", str(base_name).lower()).strip("_") or "dataset"
    name = slug
    suffix = 2
    while name in context.datasets:
        name = f"{slug}_{suffix}"
        suffix += 1
    context.datasets[name] = json_safe_rows(rows)
    return name


def dataset_catalog(datasets: dict[str, list[dict[str, Any]]]) -> str:
    """The prompt-facing listing of what this turn has collected so far."""
    if not datasets:
        return "- (none collected yet)"
    return "\n".join(
        f"- {name} ({len(rows)} rows): " + (", ".join(rows[0]) if rows else "no columns")
        for name, rows in datasets.items()
    )


# --- AST pre-check ------------------------------------------------------------

_STATIC_ALLOCATION_CAP = MEMORY_BYTES + 1


def _bounded_constant_int(node: ast.AST) -> int | None:
    """Evaluate a small non-negative integer expression without executing it."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return min(node.value, _STATIC_ALLOCATION_CAP) if node.value >= 0 else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd):
        return _bounded_constant_int(node.operand)
    if not isinstance(node, ast.BinOp):
        return None
    left = _bounded_constant_int(node.left)
    right = _bounded_constant_int(node.right)
    if left is None or right is None:
        return None
    if isinstance(node.op, ast.Add):
        return min(left + right, _STATIC_ALLOCATION_CAP)
    if isinstance(node.op, ast.Mult):
        if left == 0 or right == 0:
            return 0
        if left > _STATIC_ALLOCATION_CAP // right:
            return _STATIC_ALLOCATION_CAP
        return min(left * right, _STATIC_ALLOCATION_CAP)
    if isinstance(node.op, ast.Pow):
        if right == 0:
            return 1
        if left in {0, 1}:
            return left
        if right > 16:
            return _STATIC_ALLOCATION_CAP
        return min(left ** right, _STATIC_ALLOCATION_CAP)
    if isinstance(node.op, ast.LShift):
        if right > 30 or left > (_STATIC_ALLOCATION_CAP >> right):
            return _STATIC_ALLOCATION_CAP
        return min(left << right, _STATIC_ALLOCATION_CAP)
    return None


def _static_allocation_lower_bound(node: ast.AST) -> int | None:
    """Return bytes definitely requested by a literal allocation expression."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"bytearray", "bytes"} and node.args:
            return _bounded_constant_int(node.args[0])
        return None
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
        return None

    def unit_bytes(value: ast.AST) -> int | None:
        if isinstance(value, ast.Constant) and isinstance(value.value, (str, bytes)):
            return len(value.value)
        if isinstance(value, (ast.List, ast.Tuple)):
            # Every element reference consumes at least one pointer.
            return len(value.elts) * 8
        return None

    left_unit = unit_bytes(node.left)
    right_unit = unit_bytes(node.right)
    if left_unit is not None:
        repeats = _bounded_constant_int(node.right)
        return left_unit * repeats if repeats is not None else None
    if right_unit is not None:
        repeats = _bounded_constant_int(node.left)
        return right_unit * repeats if repeats is not None else None
    return None


def _check_node(node: ast.AST) -> None:
    static_allocation = _static_allocation_lower_bound(node)
    if static_allocation is not None and static_allocation >= MEMORY_BYTES:
        raise SandboxRejected(
            "resource_limit",
            "a literal allocation exceeds the analysis memory ceiling",
        )
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.split(".")[0] not in ALLOWED_IMPORTS:
                raise SandboxRejected(
                    "forbidden_import",
                    f"import of {alias.name!r} is not allowed; allowed modules are "
                    + ", ".join(sorted(ALLOWED_IMPORTS)),
                )
    elif isinstance(node, ast.ImportFrom):
        root = (node.module or "").split(".")[0]
        if node.level or root not in ALLOWED_IMPORTS:
            raise SandboxRejected(
                "forbidden_import",
                f"import from {node.module or '.'!r} is not allowed; allowed modules are "
                + ", ".join(sorted(ALLOWED_IMPORTS)),
            )
    elif isinstance(node, ast.Attribute):
        if node.attr.startswith("__") or node.attr in FORBIDDEN_ATTRIBUTES:
            raise SandboxRejected(
                "forbidden_attribute",
                f"attribute {node.attr!r} reaches outside the computation",
            )
    elif isinstance(node, ast.Name):
        if node.id in FORBIDDEN_NAMES:
            raise SandboxRejected(
                "forbidden_call", f"{node.id!r} is not available in the analysis sandbox"
            )
        # A dunder name is the same reach as a dunder attribute: an unqualified
        # __builtins__ resolves to the real builtins mapping and __loader__ to
        # the import machinery, both of which hand back what the allowlist and
        # the refused names exist to keep out.
        if node.id.startswith("__"):
            raise SandboxRejected(
                "forbidden_attribute", f"name {node.id!r} reaches outside the computation"
            )
    elif isinstance(node, (ast.With, ast.AsyncWith, ast.AsyncFunctionDef, ast.AsyncFor, ast.Await)):
        raise SandboxRejected(
            "forbidden_call",
            "`with` and `async` constructs are not available in the analysis sandbox",
        )


def check_analysis_code(code: str) -> str:
    """Refuse code that reaches outside the computation; return what will run.

    The submitted statements become the body of one function, so ``return`` is
    the natural way to hand a result back and the check reads exactly the
    source the child compiles — never a different string than the one executed.
    """
    source = "def {name}(datasets):\n{body}\n".format(
        name=ENTRYPOINT_NAME, body=textwrap.indent(code or "", "    ")
    )
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        # Line numbers are reported against the submitted code, not the wrapper.
        line = max(1, (error.lineno or 2) - 1)
        raise SandboxRejected("syntax_error", f"line {line}: {error.msg}") from error
    for node in ast.walk(tree):
        _check_node(node)
    return source


# --- execution ----------------------------------------------------------------

def _address_space_limit_supported() -> bool:
    """Whether this exact address-space ceiling can be applied to a child.

    A preexec_fn that raises aborts the spawn, so the answer has to be known
    before the first run — and it cannot be learned by trying the limit here,
    because a 512MB ceiling on the API process is not something to hold even
    for an instant. The probe forks, which is precisely the context preexec_fn
    runs in (macOS refuses the call outright, and refuses any ceiling under
    the forked image's reserved address space), and reports through the exit
    status. The answer is an exported constant, not a swallowed failure: it
    decides which of the two memory ceilings is the enforced one.
    """
    pid = os.fork()
    if pid == 0:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
        except (ValueError, OSError):
            os._exit(1)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


ADDRESS_SPACE_LIMIT_SUPPORTED = _address_space_limit_supported()


def _apply_resource_limits() -> None:
    """Run between fork and exec, so the child can never raise its own caps.

    Only plain setrlimit syscalls happen here: preexec_fn runs in a forked
    child and must stay this small.
    """
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_NOFILE, (MAX_OPEN_FILES, MAX_OPEN_FILES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    # No core dumps. A bounded analysis hitting its ceiling is an ordinary,
    # expected outcome — writing a crash image for it wastes disk and, on
    # macOS, raises a "Python quit unexpectedly" dialog at the developer.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if ADDRESS_SPACE_LIMIT_SUPPORTED:
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))


CHILD_PROGRAM = r'''
import errno
import io
import json
import os
import resource
import signal
import sys
import threading
import time


def _emit(payload):
    sys.__stdout__.write(json.dumps(payload))
    sys.__stdout__.flush()


def _exit_on_limit_signal(number, _frame):
    """Report the ceiling and leave quietly.

    A CPU or file-size ceiling arrives as a signal whose default action
    terminates the process abnormally — which the OS then reports as a crash
    (on macOS, a "Python quit unexpectedly" dialog). Hitting a bound is a
    normal outcome of this lane, so it is answered like one: a typed payload
    and a clean exit.
    """
    _emit({"error": {
        "code": "resource_limit",
        "detail": "the analysis passed a sandbox ceiling (signal %d)" % (number,),
    }})
    os._exit(3)


# SIGABRT joins them because an allocation the interpreter cannot satisfy
# aborts rather than raising: without this the OS files a crash report for
# what is simply a bounded analysis reaching its ceiling.
for _limit_signal in (signal.SIGXCPU, signal.SIGXFSZ, signal.SIGABRT):
    try:
        signal.signal(_limit_signal, _exit_on_limit_signal)
    except (ValueError, OSError, AttributeError):  # pragma: no cover - platform
        pass


def _watch_memory(ceiling):
    # RLIMIT_AS is unavailable on some platforms; peak RSS is the backstop.
    scale = 1 if sys.platform == "darwin" else 1024
    while True:
        if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale > ceiling:
            _emit({"error": {
                "code": "resource_limit",
                "detail": "peak memory passed the sandbox ceiling",
            }})
            os._exit(3)
        time.sleep(0.02)


class _SafeModule:
    """An allowlisted module with its re-exported modules sealed off.

    An import allowlist alone is not a boundary: ``collections`` binds the
    live ``sys`` module as ``_sys``, ``datetime`` as ``sys``, ``json`` binds
    ``codecs``, ``statistics`` binds ``random``. Any one of them reaches
    ``sys.modules`` — a plain dict, no attribute access to inspect — and from
    there the whole interpreter. So attribute access is answered here instead
    of by the module: underscore-prefixed names and anything that IS a module
    are refused, whatever their spelling.
    """

    __slots__ = ("_name", "_module")

    def __init__(self, name, module):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_module", module)

    def __getattr__(self, attribute):
        import types

        if attribute.startswith("_"):
            raise AttributeError(
                "%s.%s is not available in the analysis sandbox" % (self._name, attribute)
            )
        value = getattr(self._module, attribute)
        if isinstance(value, types.ModuleType):
            raise AttributeError(
                "%s.%s is a module and is not available in the analysis sandbox"
                % (self._name, attribute)
            )
        return value

    def __setattr__(self, attribute, value):
        raise AttributeError("the analysis sandbox does not allow assigning to a module")

    def __repr__(self):
        return "<sandboxed module %r>" % (self._name,)


def _analysis_builtins(request):
    # The pre-check refuses these names in the source; this removes them from
    # the namespace as well, so a shape the pre-check misses still finds no
    # importer, file or compiler to reach for.
    import builtins

    real_import = builtins.__import__
    allowed = frozenset(request["allowed_imports"])

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level or name.split(".")[0] not in allowed:
            raise ImportError("import of %r is not allowed in the analysis sandbox" % (name,))
        module = real_import(name, globals, locals, fromlist, level)
        # `import a.b` binds the root, `from a import b` returns the leaf;
        # both are wrapped, so no path hands back a raw module object.
        return _SafeModule(name.split(".")[0], module)

    # Dunder entries go too: __loader__ is the import machinery under another
    # name. __build_class__ stays because the interpreter looks it up itself
    # for every `class` statement, and __import__ returns guarded.
    forbidden = frozenset(request["forbidden_names"])
    exposed = {
        name: value for name, value in vars(builtins).items()
        if name not in forbidden and (not name.startswith("__") or name == "__build_class__")
    }
    exposed["__import__"] = guarded_import
    return exposed


def _main():
    request = json.loads(sys.stdin.read())
    threading.Thread(
        target=_watch_memory, args=(request["memory_bytes"],), daemon=True
    ).start()
    # __name__ is a plain label the interpreter reads for every `class` body;
    # the analysis itself cannot name it, the pre-check refuses that spelling.
    namespace = {"__builtins__": _analysis_builtins(request), "__name__": "<analysis>"}
    exec(compile(request["code"], "<analysis>", "exec"), namespace)
    entrypoint = namespace[request["entrypoint"]]
    # Anything the analysis prints must not corrupt the one JSON object.
    sys.stdout = io.StringIO()
    try:
        result = entrypoint(request["datasets"])
    finally:
        sys.stdout = sys.__stdout__
    try:
        # allow_nan=False: NaN and Infinity are not JSON tokens, and a
        # forecast dividing by a zero baseline produces them routinely. Left
        # permitted they reach a jsonb column and the provider's parser, and
        # kill the turn far from here; refused, they land on the correctable
        # invalid_output path with the rest.
        body = json.dumps({"result": result}, allow_nan=False)
    except (TypeError, ValueError) as error:
        _emit({"error": {
            "code": "invalid_output",
            "detail": "the returned value is not JSON-serializable: %s" % (error,),
        }})
        os._exit(4)
    sys.__stdout__.write(body)


try:
    _main()
except MemoryError:
    _emit({"error": {"code": "resource_limit", "detail": "the analysis exhausted memory"}})
    os._exit(3)
except OSError as error:
    _emit({"error": {
        "code": "resource_limit" if error.errno == errno.EFBIG else "execution_error",
        "detail": "%s: %s" % (type(error).__name__, error),
    }})
    os._exit(5)
except BaseException as error:
    _emit({"error": {
        "code": "execution_error",
        "detail": "%s: %s" % (type(error).__name__, error),
    }})
    os._exit(6)
'''


def _error(code: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "detail": detail[:MAX_DETAIL_CHARS]}}


def _tail(text: str) -> str:
    stripped = (text or "").strip()
    return stripped[-MAX_DETAIL_CHARS:] if stripped else "(no output)"


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def _interpret(returncode: int, stdout: str, stderr: str) -> dict[str, Any]:
    """Map one child exit onto the closed set of execution codes."""
    payload: Any = None
    if (stdout or "").strip():
        try:
            payload = json.loads(stdout)
        except ValueError:
            payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        reported = payload["error"]
        code = str(reported.get("code", "execution_error"))
        return _error(
            code if code in EXECUTION_CODES else "execution_error",
            str(reported.get("detail", "")),
        )
    if isinstance(payload, dict) and "result" in payload:
        return {"ok": True, "result": payload["result"]}
    if returncode < 0:
        killed_by = -returncode
        return _error(
            "resource_limit" if killed_by in _LIMIT_SIGNALS else "execution_error",
            f"the child interpreter was killed by {_signal_name(killed_by)}",
        )
    if returncode != 0:
        return _error(
            "execution_error", f"the child exited {returncode}: {_tail(stderr)}"
        )
    return _error("invalid_output", f"the child did not return one JSON object: {_tail(stdout)}")


def execute_checked_source(
    source: str, datasets: dict[str, Any], *, timeout_s: int = DEFAULT_TIMEOUT_S
) -> dict[str, Any]:
    """Run already-checked source in the limited child and read its one object.

    The datasets cross as JSON into a process that shares no object with this
    one, which is what makes mutation inside the analysis unable to touch
    server state — the isolation is the process boundary, not a defensive copy.
    """
    # Everything user-derived travels on stdin. The command line carries only
    # the fixed child program, so no row value is ever readable from ps.
    request = json.dumps({
        "code": source,
        "entrypoint": ENTRYPOINT_NAME,
        "datasets": datasets,
        "memory_bytes": MEMORY_BYTES,
        # One source of truth: the child strips exactly what the pre-check refuses.
        "allowed_imports": sorted(ALLOWED_IMPORTS),
        "forbidden_names": sorted(FORBIDDEN_NAMES),
    }, default=str)
    with tempfile.TemporaryDirectory(prefix="fyn-analysis-") as workdir:
        process = subprocess.Popen(
            # -I isolated, -S no site imports, -B no bytecode writes, -X utf8 so
            # stdio encoding never depends on the server's locale.
            [sys.executable, "-I", "-S", "-B", "-X", "utf8", "-c", CHILD_PROGRAM],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=dict(_CHILD_ENV),
            preexec_fn=_apply_resource_limits,
            encoding="utf-8",
            # Its own process group, so the timeout path can kill descendants
            # too. Killing only the direct child leaves any grandchild holding
            # the pipe write end, and the drain below then waits on an EOF that
            # never comes — the timeout silently becomes forever.
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(request, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            try:
                process.communicate(timeout=_KILL_DRAIN_S)
            except subprocess.TimeoutExpired:
                # A descendant still holds the pipe. The child is dead and the
                # budget is spent; report rather than wait on it.
                pass
            return _error(
                "timeout", f"the analysis did not finish within {timeout_s}s and was killed"
            )
    return _interpret(process.returncode, stdout, stderr)


def _kill_process_group(process: subprocess.Popen) -> None:
    """Kill the child and every descendant it started."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # The group is already gone, or the platform refused; the direct
        # child still has to die.
        pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


def run_sandboxed_analysis(
    code: str, datasets: dict[str, Any], *, timeout_s: int = DEFAULT_TIMEOUT_S
) -> dict[str, Any]:
    """Check, then run, model-authored analysis code. Never raises to callers."""
    try:
        source = check_analysis_code(code)
    except SandboxRejected as rejection:
        return _error(rejection.code, str(rejection))
    try:
        return execute_checked_source(source, datasets, timeout_s=timeout_s)
    except Exception as error:
        # A lane failure is a tool result the model can react to, never an
        # exception thrown into the agent loop.
        return _error("execution_error", f"{type(error).__name__}: {error}")


# --- agent tool ---------------------------------------------------------------

def build_python_analysis_tool(context) -> Any:
    """Mount the bounded Python lane over this turn's collected datasets."""

    def run_python_analysis(purpose: str, code: str, dataset_names: list) -> dict[str, Any]:
        requested = [str(name) for name in dataset_names or []]
        unknown = [name for name in requested if name not in context.datasets]
        if unknown:
            return {"error": {
                "code": "unknown_dataset",
                "detail": "not collected this turn: " + ", ".join(unknown),
                "available": sorted(context.datasets),
                "hint": (
                    "Run the analysis, SQL or uploaded-source tool that produces those rows "
                    "first, then pass the dataset_name it returned. Retry at most twice."
                ),
            }}
        outcome = run_sandboxed_analysis(
            code, {name: context.datasets[name] for name in requested}
        )
        if not outcome["ok"]:
            failure = outcome["error"]
            return {"error": {
                **failure,
                "hint": _CORRECTION_HINTS.get(failure["code"], _DEFAULT_HINT),
            }}
        return {
            "kind": "python_analysis",
            "purpose": purpose,
            "datasets_used": requested,
            "result": outcome["result"],
        }

    description = (
        "Compute in Python what SQL cannot express — forecasting, regression, statistical "
        "tests, scenario modelling — over rows the governed lanes already returned this turn. "
        "There is no database handle and no connection here: the only data is the datasets "
        "you name in dataset_names.\n"
        "Write the BODY of a function that receives `datasets` (a dict of name -> list of row "
        "dicts) and `return`s a JSON-serializable value (numbers, strings, booleans, lists, "
        "dicts of those). Imports are limited to: "
        + ", ".join(sorted(ALLOWED_IMPORTS))
        + ". Reflection (dunder attributes, eval, exec, compile, open, getattr, globals) and "
        "`with`/`async` blocks are rejected before the code runs. The call is killed after "
        f"{DEFAULT_TIMEOUT_S}s of wall clock, {CPU_SECONDS}s of CPU, or "
        f"{MEMORY_BYTES // (1024 * 1024)}MB of memory.\n"
        "Money arrives as integer minor units in columns suffixed _minor: keep results in "
        "minor units and never divide by 100 here. Dates arrive as ISO strings.\n"
        "Datasets collected so far this turn:\n"
        + dataset_catalog(context.datasets)
        + "\nEvery analysis, SQL and uploaded-source result also carries a `dataset_name`; "
        "pass those names here to compute over rows produced later in this same turn.\n"
        "A result with an `error` key names the exact rejected check — correct the code and "
        "retry at most twice."
    )
    return bind_schema_tool(
        run_python_analysis,
        name=PYTHON_TOOL_NAME,
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "purpose": {
                    "type": "string",
                    "description": "One short sentence: what this computation answers.",
                },
                "code": {
                    "type": "string",
                    "description": (
                        "The function body. Read `datasets` and `return` a JSON-serializable "
                        "value."
                    ),
                },
                "dataset_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of datasets collected this turn, from tool payloads.",
                },
            },
            "required": ["purpose", "code", "dataset_names"],
            "additionalProperties": False,
        },
        strict=True,
    )
