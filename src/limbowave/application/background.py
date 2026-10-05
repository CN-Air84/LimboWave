"""Cancellation-safe boundary for synchronous I/O; no Qt objects cross it."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

_pending: set[asyncio.Task[Any]] = set()


async def run_blocking[**P, T](work: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Cancellation waits for submitted work to release files/transactions."""
    pending = asyncio.create_task(asyncio.to_thread(work, *args, **kwargs))
    _pending.add(pending)
    pending.add_done_callback(_pending.discard)
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        await asyncio.gather(pending, return_exceptions=True)
        raise


async def drain_blocking() -> None:
    """Also drain IPC/permission work not owned by the window task set."""
    while _pending:
        await asyncio.gather(*tuple(_pending), return_exceptions=True)
