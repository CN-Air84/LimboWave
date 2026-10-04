"""历史读取的专用后台线程；退出前等待读取结束，避免提前关闭 SQLite。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import ParamSpec, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")


class HistoryReader:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="limbowave-history")
        self._closed = False

    async def read(self, work: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
        if self._closed:
            raise RuntimeError("历史读取器已关闭")
        pending = asyncio.get_running_loop().run_in_executor(
            self._executor, partial(work, *args, **kwargs)
        )
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # 取消等待不能杀死线程。等它释放事务，再允许应用关库；同时消费读取异常。
            await asyncio.gather(pending, return_exceptions=True)
            raise

    async def close(self) -> None:
        self._closed = True
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
