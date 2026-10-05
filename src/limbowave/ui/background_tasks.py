"""Run service work off-thread; poll and deliver results exclusively on Qt.

Workers never call QObject methods, including during widget/interpreter teardown.
Capture widget values before submit; drain before closing/resetting the vault.
"""
from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from PySide6.QtCore import QObject, QTimer

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="limbowave-ui-work")
_pending: set[Future[Any]] = set()
_receivers: weakref.WeakSet[BackgroundTasks] = weakref.WeakSet()


class BackgroundTasks(QObject):
    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._closing = False
        self._callbacks: dict[Future[Any], tuple[Callable[..., object], Callable[..., object]]] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._poll)
        _receivers.add(self)

    def submit[T](
        self, work: Callable[[], T], success: Callable[[T], object],
        error: Callable[[Exception], object],
    ) -> Future[T]:
        if self._closing:
            raise RuntimeError("后台任务接收器已关闭")
        future = _pool.submit(work)
        _pending.add(future)
        self._callbacks[future] = success, error
        self._timer.start()
        return future

    def _poll(self) -> None:
        for future in tuple(self._callbacks):
            if not future.done():
                continue
            success, error = self._callbacks.pop(future)
            _pending.discard(future)
            if self._closing:
                continue
            try:
                value = future.result()
            except Exception as exc:
                error(exc)
            else:
                success(value)
        if not self._callbacks:
            self._timer.stop()


async def drain_background_tasks() -> None:
    """Drain even work whose receiver has already been destroyed."""
    for receiver in tuple(_receivers):
        receiver._closing = True
    while _pending:
        current = tuple(_pending)
        await asyncio.gather(
            *(asyncio.wrap_future(future) for future in current), return_exceptions=True
        )
        _pending.difference_update(current)
