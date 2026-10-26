import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from limbowave.application.services.reminder_scheduler import ReminderScheduler
from limbowave.application.services.reminder_service import ReminderService
from limbowave.domain.conversation import Branch, Conversation
from limbowave.domain.reminder import ReminderContext
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory


@pytest.fixture
def service():
    factory = in_memory_uow_factory()
    now = datetime.now(UTC)
    with factory() as uow:
        uow.conversations.add(Conversation("c", "c", now))
        uow.branches.add(Branch("b", "c", now))
        uow.commit()
    service = ReminderService(factory, clock=lambda: now - timedelta(seconds=10))
    service.set_reminder(ReminderContext("c", "b"), reason="timer", delay_seconds=1)
    service._clock = lambda: now
    return service


async def test_startup_scan_delivery_and_cancellation(service):
    delivered = []
    scheduler = ReminderScheduler(service, delivered.append, interval=0.01)
    task = asyncio.create_task(scheduler.run())
    try:
        async with asyncio.timeout(2):
            while not delivered:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.03)
        assert len(delivered) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_disabled_timer_waits_for_unlock(service):
    delivered = []
    enabled = False
    scheduler = ReminderScheduler(service, delivered.append, enabled=lambda: enabled)
    await scheduler.tick()
    assert not delivered and len(service.list_reminders("c")) == 1
    enabled = True
    await scheduler.tick()
    assert len(delivered) == 1


async def test_transient_failure_retries_without_private_logs(service, monkeypatch, caplog):
    original = service.deliver_due
    attempts = 0
    def flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("private-reminder-canary")
        return original()
    monkeypatch.setattr(service, "deliver_due", flaky)
    delivered = []
    task = asyncio.create_task(ReminderScheduler(service, delivered.append, interval=0.01).run())
    try:
        async with asyncio.timeout(2):
            while not delivered:
                await asyncio.sleep(0.01)
        assert attempts >= 2
        assert "private-reminder-canary" not in caplog.text
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_shutdown_drains_in_flight_disk_work(service, monkeypatch):
    entered, released = Event(), Event()
    def blocked():
        entered.set()
        released.wait(timeout=3)
        return []
    monkeypatch.setattr(service, "deliver_due", blocked)
    task = asyncio.create_task(ReminderScheduler(service, lambda _: None).run())
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done()
    finally:
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
