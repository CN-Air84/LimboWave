"""Trusted, invocation-scoped access; never populated from model tool arguments.

The gateway derives this value from the persisted local conversation preset.
ContextVar isolates concurrent calls and resetting restores enclosing scopes.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_FULL_ACCESS: ContextVar[bool] = ContextVar("limbowave_tool_full_access", default=False)


def has_full_access() -> bool:
    return _FULL_ACCESS.get()


@contextmanager
def tool_access_scope(full_access: bool) -> Iterator[None]:
    token = _FULL_ACCESS.set(full_access)
    try:
        yield
    finally:
        _FULL_ACCESS.reset(token)
