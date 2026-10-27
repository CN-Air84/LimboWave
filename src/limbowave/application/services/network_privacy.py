"""Invocation-scoped outbound URL checks, without sessions, storage, or network I/O.

Install inside the synchronous tool invocation, on the same execution context as
HTTP requests. ContextVar state is isolated between async tasks and threads;
asyncio.to_thread copies context, a raw executor submission does not. Never cache
an invocation's checker on a shared HTTP client or capture it in a DNS worker.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import cast

from limbowave.domain.network import WebAccessError

_URL_CHECKS: ContextVar[tuple[Callable[[str], None], ...]] = ContextVar(
    "limbowave_network_destination_checks",
    default=(),
)


@contextmanager
def guard_network_request(check_url: Callable[[str], None]) -> Iterator[None]:
    """Add a synchronous URL guard for this scope; reset even on cancellation.

    A checker returns None to allow, or raises to deny. Do not pass policy.blocks
    directly: boolean results fail closed, rather than reversing allow/deny.
    Nested scopes add checks and cannot weaken an enclosing scope's protection.
    Exiting restores the exact prior scope, not a global empty value.
    """
    if not callable(check_url):
        raise WebAccessError("privacy_denied", "联网隐私检查不可用，未发送请求")
    token = _URL_CHECKS.set((*_URL_CHECKS.get(), check_url))
    try:
        yield
    finally:
        _URL_CHECKS.reset(token)


def check_network_destination(url: str) -> None:
    """Check a candidate URL without I/O; absent invocation guards are a no-op.

    The transport passes the canonical URL, including encoded path/query and no
    fragment. Policy owns bounded decoding/normalization and must not log the
    URL: search-provider URLs may contain credentials. All checker failures are
    converted to a constant safe error, without URL, policy text, or exception.
    """
    try:
        for check_url in _URL_CHECKS.get():
            result = cast(Callable[[str], object], check_url)(url)
            if result is not None:
                raise ValueError
    except Exception:
        raise WebAccessError("privacy_denied", "联网目标未通过隐私检查，未发送请求") from None
