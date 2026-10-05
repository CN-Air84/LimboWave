import asyncio
import threading

import pytest

from limbowave.application.background import run_blocking


async def test_cancellation_waits_for_submitted_transaction():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def work():
        entered.set()
        release.wait(3)
        finished.set()

    task = asyncio.create_task(run_blocking(work))
    while not entered.is_set():
        await asyncio.sleep(.001)
    task.cancel()
    await asyncio.sleep(.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_work_runs_off_thread_and_propagates_error():
    assert await run_blocking(threading.get_ident) != threading.get_ident()
    with pytest.raises(ValueError, match="failure"):
        await run_blocking(lambda: (_ for _ in ()).throw(ValueError("failure")))
