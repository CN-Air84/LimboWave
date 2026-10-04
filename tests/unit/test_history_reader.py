from __future__ import annotations

import asyncio
import threading

import pytest

from limbowave.application.services.history_reader import HistoryReader


async def test_reads_run_off_event_loop_and_close_rejects_new_work() -> None:
    reader = HistoryReader()
    try:
        assert await reader.read(threading.get_ident) != threading.get_ident()
        assert await reader.read(lambda *, value: value, value=42) == 42
        with pytest.raises(ValueError, match="failed"):
            await reader.read(lambda: (_ for _ in ()).throw(ValueError("failed")))
    finally:
        await reader.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        await reader.read(lambda: None)


async def test_cancel_waits_for_worker_without_blocking_event_loop() -> None:
    reader = HistoryReader()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def work() -> None:
        started.set()
        assert release.wait(5)
        finished.set()
        raise ValueError("cancelled read failed")

    task = asyncio.create_task(reader.read(work))
    try:
        async with asyncio.timeout(5):
            while not started.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert finished.is_set()
    finally:
        release.set()
        await reader.close()
