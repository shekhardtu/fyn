"""Per-message reasoning preference; it never changes financial authority."""
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_effort: ContextVar[str] = ContextVar("composer_effort", default="auto")
COMPOSER_EFFORTS = frozenset({"auto", "quick", "thorough"})


@contextmanager
def composer_effort(value: str = "auto") -> Iterator[None]:
    if value not in COMPOSER_EFFORTS:
        raise ValueError("Unknown composer effort")
    token = _effort.set(value)
    try:
        yield
    finally:
        _effort.reset(token)


def reasoning_effort(default: str) -> str:
    if default == "none":
        return default
    return {"quick": "low", "thorough": "high"}.get(_effort.get(), default)
