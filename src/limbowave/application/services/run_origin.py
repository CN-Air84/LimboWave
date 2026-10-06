"""Trusted, task-local run provenance shared by runtime and tool permission gates.

Bind at the server-owned run entry point, before creating generation/retry tasks.
Never derive this value from model parameters or browser-provided tool arguments.
asyncio tasks and asyncio.to_thread inherit context; pre-existing IPC reader tasks
and plain executor threads do not. Those dispatchers must bind the active run's
trusted origin explicitly or consult the active-run origin provider before tools.
The provider must remain bound until the run and pending tool work have settled.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

RUN_ORIGIN: ContextVar[str] = ContextVar("run_origin", default="desktop")


@contextmanager
def remote_origin() -> Iterator[None]:
    """Deny remote tools for this scope and tasks spawned inside it; restore on exit."""
    token = RUN_ORIGIN.set("web")
    try:
        yield
    finally:
        RUN_ORIGIN.reset(token)


def remote_tools_denied(origin_provider: Callable[[], str] | None = None) -> bool:
    """Intersect task provenance with trusted active-run provenance; fail closed.

    Long-lived IPC / Pi callbacks must pass the coordinator's active-run getter.
    None preserves legacy desktop callers, not a substitute for runtime wiring.
    Neither a desktop task nor a desktop provider can override a web restriction.
    Unknown origins and unavailable providers are denied rather than downgraded.
    """
    if RUN_ORIGIN.get() != "desktop":
        return True
    if origin_provider is None:
        return False
    try:
        return origin_provider() != "desktop"
    except Exception:
        return True
