"""新建/删除/恢复会话必须隔离内核、落库目标和异步收尾。"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter
from typing import Any

import pytest

from limbowave.application.services.history_service import HistoryService
from limbowave.application.services.run_coordinator import RunCoordinator
from limbowave.application.services.session_controller import SessionController
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.domain.runtime_state import RuntimeRestoreResult
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.unit.test_branch_path import TreeKernel, _turn
from tests.unit.test_run_coordinator import _resume_context
from tests.unit.test_runtime_state_service import T0


class SessionKernel(TreeKernel):
    def __init__(self) -> None:
        super().__init__()
        self.resets = 0
        self.reset_error: Exception | None = None
        self.restore_error = False
        self.restoring: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def new_session(self) -> None:
        self.resets += 1
        if self.reset_error is not None:
            raise self.reset_error
        self.path = []

    async def restore_runtime_state(self, snapshot: Any) -> RuntimeRestoreResult:
        if self.restoring is not None and self.release is not None:
            self.restoring.set()
            await self.release.wait()
        await super().restore_runtime_state(snapshot)
        return RuntimeRestoreResult(success=not self.restore_error, error="failed")


def _stack():
    store = InMemoryStore()
    factory = in_memory_uow_factory(store)
    kernel = SessionKernel()
    coordinator = RunCoordinator(kernel, factory, context=_resume_context)
    return store, kernel, coordinator, factory


@pytest.mark.parametrize("delete_old", [False, True])
async def test_new_session_never_copies_old_context_or_mirrors(delete_old: bool) -> None:
    store, kernel, coord, factory = _stack()
    await _turn(kernel, coord, "旧会话截图标记", "旧回复")
    old_conversation = coord.conversation_id
    old_entries = {e["id"] for e in kernel.path}
    assert await coord.new_session()
    assert kernel.path == []
    assert coord.run_id is None
    if delete_old:
        assert HistoryService(factory).delete(old_conversation)
    await _turn(kernel, coord, "新会话", "新回复")
    assert coord.conversation_id != old_conversation
    assert all(e["id"] not in old_entries for e in kernel.path)
    with factory() as uow:
        mirrors = uow.runtime.list_for_conversation(coord.conversation_id)
        assert {m.entry_id for m in mirrors}.isdisjoint(old_entries)
    assert len(store.conversations) == (1 if delete_old else 2)


async def test_new_session_waits_for_old_finalize_before_reset() -> None:
    class SlowKernel(SessionKernel):
        entered = asyncio.Event()
        release_entries = asyncio.Event()

        async def get_entries(self, *, since=None):
            self.entered.set()
            await self.release_entries.wait()
            return await super().get_entries(since=since)

    kernel = SlowKernel()
    store = InMemoryStore()
    factory = in_memory_uow_factory(store)
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await coord.send("old")
    old_id = coord.conversation_id
    kernel.say("reply")
    kernel.emit("run.settled", {})
    await kernel.entered.wait()
    reset = asyncio.create_task(coord.new_session())
    await asyncio.sleep(0)
    assert kernel.resets == 0
    assert coord.busy
    assert await coord.send("must not send") is None
    kernel.release_entries.set()
    assert await reset
    assert kernel.resets == 1
    with factory() as uow:
        assert len(uow.runtime.list_for_conversation(old_id)) == 2


async def test_failed_reset_preserves_location_and_blocks_send_until_recovery() -> None:
    store, kernel, coord, _ = _stack()
    await _turn(kernel, coord, "old", "reply")
    old_id = coord.conversation_id
    kernel.reset_error = RuntimeError("reset refused")
    assert not await coord.new_session()
    assert coord.conversation_id == old_id
    assert await coord.send("must not send") is None
    assert len(store.runs) == 1
    kernel.reset_error = None
    assert await coord.new_session()
    await _turn(kernel, coord, "new", "reply")
    assert len(store.runs) == 2


async def test_empty_target_resets_instead_of_reusing_current_history() -> None:
    _, kernel, coord, factory = _stack()
    await _turn(kernel, coord, "old", "reply")
    with factory() as uow:
        uow.conversations.add(Conversation("empty", "empty", T0))
        uow.branches.add(Branch("empty-branch", "empty", T0))
        uow.commit()
    assert await coord.switch_conversation("empty", "empty-branch")
    assert kernel.path == []
    assert coord.conversation_id == "empty"


async def test_missing_history_is_not_a_successful_switch() -> None:
    _, kernel, coord, factory = _stack()
    await _turn(kernel, coord, "old", "reply")
    old_id = coord.conversation_id
    with factory() as uow:
        uow.conversations.add(Conversation("missing", "missing", T0))
        uow.branches.add(Branch("missing-branch", "missing", T0))
        uow.messages.add(Message("m", "missing", "missing-branch", MessageRole.USER, "lost", T0))
        uow.commit()
    assert not await coord.switch_conversation("missing", "missing-branch")
    assert coord.conversation_id == old_id
    assert kernel.resets == 0


async def test_restore_failure_cannot_commit_target_or_send_on_uncertain_context() -> None:
    store, kernel, coord, factory = _stack()
    await _turn(kernel, coord, "A", "reply A")
    a, branch_a = coord.conversation_id, coord.branch_id
    assert await coord.new_session()
    await _turn(kernel, coord, "B", "reply B")
    b = coord.conversation_id
    kernel.restore_error = True
    controller = SessionController(kernel, coord, uow_factory=factory)
    assert not await controller.switch_conversation(a, branch_a)
    assert coord.conversation_id == b
    assert await coord.send("not in A or B") is None
    assert len(store.runs) == 2
    kernel.restore_error = False
    assert await controller.switch_conversation(a, branch_a)
    assert coord.conversation_id == a
    await _turn(kernel, coord, "continue A", "reply")
    assert "B" not in str(kernel.path)


async def test_restore_is_atomic_against_send_new_and_another_switch() -> None:
    _, kernel, coord, _ = _stack()
    await _turn(kernel, coord, "A", "reply")
    a, branch_a = coord.conversation_id, coord.branch_id
    assert await coord.new_session()
    await _turn(kernel, coord, "B", "reply")
    b = coord.conversation_id
    kernel.restoring = asyncio.Event()
    kernel.release = asyncio.Event()
    task = asyncio.create_task(coord.switch_conversation(a, branch_a))
    await kernel.restoring.wait()
    assert coord.conversation_id == b
    assert await coord.send("no") is None
    assert not await coord.new_session()
    assert not await coord.switch_conversation(a, branch_a)
    kernel.release.set()
    assert await task
    assert coord.conversation_id == a


async def test_switch_preparation_keeps_event_loop_alive_and_reuses_reads() -> None:
    store = InMemoryStore()
    base_factory = in_memory_uow_factory(store)
    calls: Counter[str] = Counter()
    worker_threads: list[int] = []
    slow = [False]

    def factory():
        if slow[0]:
            worker_threads.append(threading.get_ident())
            time.sleep(0.08)
        uow = base_factory()
        original_runtime = uow.runtime.list_for_conversation
        original_messages = uow.messages.list_for_branch

        def runtime(conversation_id: str):
            calls["runtime"] += 1
            return original_runtime(conversation_id)

        def messages(branch_id: str):
            calls["messages"] += 1
            return original_messages(branch_id)

        uow.runtime.list_for_conversation = runtime
        uow.messages.list_for_branch = messages
        return uow

    kernel = SessionKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await _turn(kernel, coord, "A", "reply A")
    target = (coord.conversation_id, coord.branch_id)
    assert all(target)
    assert await coord.new_session()
    await _turn(kernel, coord, "B", "reply B")

    calls.clear()
    slow[0] = True
    ticks = 0
    running = True

    async def heartbeat() -> None:
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0.005)

    pulse = asyncio.create_task(heartbeat())
    try:
        assert await coord.switch_conversation(
            target[0],
            target[1],
            known_has_messages=True,
        )
    finally:
        running = False
        await pulse

    assert ticks >= 5, "同步解密若回到事件循环会吞掉动画心跳"
    assert worker_threads and set(worker_threads) == {worker_threads[0]}
    assert worker_threads[0] != threading.get_ident()
    assert calls["runtime"] == 1
    assert calls["messages"] == 0
