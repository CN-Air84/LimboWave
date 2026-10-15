"""只读预准备的复用、有效性校验及取消屏障。"""

import asyncio
import threading
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from limbowave.application.services.run_coordinator import RunCoordinator
from limbowave.domain.conversation import Branch, Conversation
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.unit.test_run_coordinator import _resume_context


@pytest.fixture
def coordinator():
    factory = in_memory_uow_factory(InMemoryStore())
    now = datetime.now(UTC)
    with factory() as uow:
        for suffix in ("1", "2"):
            uow.conversations.add(Conversation(f"c{suffix}", "会话", now))
            uow.branches.add(Branch(f"b{suffix}", f"c{suffix}", now))
        uow.commit()
    return RunCoordinator(None, factory, context=_resume_context)


async def test_preparation_is_read_only_and_reused(coordinator, monkeypatch):
    read = Mock(wraps=coordinator._prepare_switch)
    monkeypatch.setattr(coordinator, "_prepare_switch", read)
    preparation = await coordinator.prepare_conversation_switch("c1", "b1")
    assert coordinator.conversation_id is None
    assert coordinator.branch_id is None
    assert not coordinator.busy
    assert await coordinator.switch_conversation("c1", "b1", preparation=preparation)
    assert read.call_count == 1
    assert coordinator.conversation_id == "c1"


@pytest.mark.parametrize("change", ["revision", "location", "target", "owner"])
async def test_stale_or_mismatched_preparation_is_read_again(coordinator, monkeypatch, change):
    preparation = await coordinator.prepare_conversation_switch("c1", "b1")
    target = "c1", "b1"
    if change == "revision":
        coordinator.bump_revision()
    elif change == "location":
        assert await coordinator.switch_conversation("c2", "b2")
    elif change == "target":
        target = "c2", "b2"
    else:
        coordinator = RunCoordinator(None, coordinator._uow_factory, context=_resume_context)
    read = Mock(wraps=coordinator._prepare_switch)
    monkeypatch.setattr(coordinator, "_prepare_switch", read)
    assert await coordinator.switch_conversation(*target, preparation=preparation)
    assert read.call_count == 1
    assert coordinator.conversation_id == target[0]
    assert coordinator.branch_id == target[1]


async def test_invalid_target_does_not_change_location(coordinator):
    assert await coordinator.switch_conversation("c1", "b1")
    preparation = await coordinator.prepare_conversation_switch("c2", "b1")
    assert not await coordinator.switch_conversation("c2", "b1", preparation=preparation)
    assert (coordinator.conversation_id, coordinator.branch_id) == ("c1", "b1")


async def test_cancellation_drains_preparation_thread(coordinator, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = coordinator._prepare_switch

    def slow_read(*args):
        entered.set()
        assert release.wait(5)
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(coordinator, "_prepare_switch", slow_read)
    task = asyncio.create_task(coordinator.prepare_conversation_switch("c1", "b1"))
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert coordinator.conversation_id is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
