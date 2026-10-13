from __future__ import annotations

from limbowave.application.kernel import KernelEvent
from limbowave.infrastructure.pi_adapter import PiKernelAdapter
from limbowave.infrastructure.pi_rpc import SpawnSpec


def test_unconsumed_event_queue_stays_bounded() -> None:
    adapter = PiKernelAdapter(SpawnSpec(argv=[]))
    for index in range(2_000):
        adapter._publish(KernelEvent(kind="message.update", payload={"i": index}))

    assert adapter._event_queue.qsize() <= 1_024
