"""A tool-worker backlog must be just as cooperative as the live RPC reader."""
import asyncio
import time

from limbowave.application.kernel import KernelEvent
from limbowave.application.services.session_controller import SessionController


async def test_queued_deltas_yield_and_keep_every_event_in_order():
    coordinator = SessionController(None).coordinator()
    received = []
    coordinator.subscribe(lambda event: received.append(event.data["text"]))
    for index in range(1_000):
        event = KernelEvent("message.update", {
            "assistantMessageEvent": {"type": "text_delta", "delta": str(index)}
        })
        coordinator._pending_kernel_events.append((event, None, time.monotonic()))
    opportunities = []

    async def input_task():
        while len(received) < 1_000:
            await asyncio.sleep(0)
            if 0 < len(received) < 1_000:
                opportunities.append(len(received))

    task = asyncio.create_task(input_task())
    coordinator._kernel_event_task = asyncio.create_task(coordinator._drain_kernel_events())
    await coordinator._kernel_event_task
    await task
    assert received == [str(index) for index in range(1_000)]
    assert opportunities
    assert max(b - a for a, b in zip([0, *opportunities], [*opportunities, 1_000],
                                     strict=True)) <= 128
    assert coordinator._kernel_event_task is None


async def test_queue_rechecks_liveness_after_yield_and_accepts_new_events(monkeypatch):
    coordinator = SessionController(None).coordinator()
    coordinator.storage_worker = object()  # these synthetic events never call storage
    received = []
    monkeypatch.setattr(coordinator, "_handle_kernel_event",
                        lambda event, **kwargs: received.append(event.payload["index"]))
    for index in range(1_000):
        coordinator._pending_kernel_events.append(
            (KernelEvent("test", {"index": index}), None, time.monotonic())
        )

    async def switch():
        await asyncio.sleep(0)
        coordinator._live = object()
        coordinator._on_kernel_event(KernelEvent("test", {"index": "new run"}))

    task = asyncio.create_task(switch())
    coordinator._kernel_event_task = asyncio.create_task(coordinator._drain_kernel_events())
    await coordinator._kernel_event_task
    await task
    assert received[-1] == "new run"
    assert 0 < len(received) < 1_001
    assert not coordinator._pending_kernel_events
    assert coordinator._kernel_event_task is None
