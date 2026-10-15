import asyncio
from unittest.mock import AsyncMock

from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.domain.run import RunStatus
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.unit.test_run_coordinator import FakeKernel


def coordinator():
    store = InMemoryStore()
    kernel = FakeKernel()
    return (
        RunCoordinator(
            kernel, in_memory_uow_factory(store), context=lambda: RunContext("m", "e", "test")
        ),
        kernel,
        store,
    )


async def test_shutdown_aborts_running_model_then_commits_before_kernel_close():
    coord, kernel, store = coordinator()
    run = await coord.send("do not lose this")

    async def close():
        assert kernel.aborted
        assert store.runs[run].status is RunStatus.INTERRUPTED

    kernel.shutdown = close
    await coord.shutdown()
    assert not coord.available
    assert len(store.messages) == 1


async def test_shutdown_waits_for_slow_finalizer_without_cancelling_it():
    coord, kernel, _ = coordinator()
    await coord.send("hi")
    gate = asyncio.Event()
    original = coord._finalize

    async def slow(*args):
        await gate.wait()
        await original(*args)

    coord._finalize = slow
    kernel.shutdown = AsyncMock()
    task = asyncio.create_task(coord.shutdown())
    await asyncio.sleep(0)
    assert not task.done()
    kernel.shutdown.assert_not_awaited()
    gate.set()
    await task
    kernel.shutdown.assert_awaited_once()


async def test_idle_crash_replaces_kernel_once_without_replay():
    coord, old, _ = coordinator()
    replacement = FakeKernel()
    factory = AsyncMock(return_value=replacement)
    coord.replacement_factory = factory
    old.emit("runtime.exited", {})
    old.emit("runtime.exited", {})
    assert not coord.available
    await coord._recovery_task
    assert coord.available and coord.kernel is replacement
    assert replacement.sent == []
    factory.assert_awaited_once()
    # A retired kernel can no longer deliver late exit or output events.
    old.emit("runtime.exited", {})
    assert coord.available
    await coord.shutdown()


async def test_replacement_start_failure_stays_visibly_unavailable():
    coord, old, _ = coordinator()
    replacement = FakeKernel()
    replacement.start = AsyncMock(side_effect=RuntimeError("failed to start"))
    replacement.shutdown = AsyncMock()
    coord.replacement_factory = AsyncMock(return_value=replacement)
    events = []
    coord.subscribe(events.append)
    old.emit("runtime.exited", {})
    await coord._recovery_task
    assert not coord.available
    assert any(event.kind == "runtime_recovery_failed" for event in events)
    replacement.shutdown.assert_awaited_once()


async def test_crash_recovers_committed_snapshot_without_replaying_interrupted_run():
    from types import SimpleNamespace

    from limbowave.domain.runtime_state import (
        RuntimeRestoreResult,
        RuntimeStateSnapshot,
        RuntimeValidationResult,
    )

    coord, old, store = coordinator()
    run = await coord.send("never replay me")
    snapshot = RuntimeStateSnapshot("c", [{"id": "committed"}], "committed", [])
    coord._prepare_switch = lambda *args: SimpleNamespace(snapshot=snapshot, has_messages=True)
    replacement = FakeKernel()
    replacement.restore_runtime_state = AsyncMock(return_value=RuntimeRestoreResult(success=True))
    replacement.validate_runtime_state = AsyncMock(return_value=RuntimeValidationResult(valid=True))
    coord.replacement_factory = AsyncMock(return_value=replacement)
    old.emit("runtime.exited", {})
    await coord._recovery_task
    assert store.runs[run].status is RunStatus.INTERRUPTED
    replacement.restore_runtime_state.assert_awaited_once_with(snapshot)
    replacement.validate_runtime_state.assert_awaited_once_with(snapshot)
    assert replacement.sent == [] and coord.available
    await coord.shutdown()


async def test_missing_mirror_keeps_send_disabled_but_explicit_new_session_can_repair():
    coord, old, store = coordinator()
    run = await coord.send("interrupted before any stable mirror")
    replacement = FakeKernel()
    coord.replacement_factory = AsyncMock(return_value=replacement)
    old.emit("runtime.exited", {})
    await coord._recovery_task
    assert not coord.available and replacement.sent == []
    assert await coord.new_session()
    assert coord.available
    assert store.runs[run].status is RunStatus.INTERRUPTED
    await coord.shutdown()


async def test_shutdown_cancels_recovery_and_closes_its_replacement():
    coord, old, _ = coordinator()
    entered = asyncio.Event()
    replacement = FakeKernel()

    async def starting():
        entered.set()
        await asyncio.Event().wait()

    replacement.start = starting
    replacement.shutdown = AsyncMock()
    coord.replacement_factory = AsyncMock(return_value=replacement)
    old.emit("runtime.exited", {})
    await entered.wait()
    await coord.shutdown()
    replacement.shutdown.assert_awaited_once()
    assert not coord.available
