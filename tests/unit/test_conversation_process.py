"""Real spawn-process coverage: no coordinator storage work on the caller's loop."""

import asyncio
import os

import pytest

from limbowave.application.services.history_service import HistoryService
from limbowave.application.services.run_coordinator import RunCoordinator
from limbowave.infrastructure.conversation_process import (
    ConversationProcess,
    ConversationProcessConfig,
)
from limbowave.infrastructure.database.sqlite_repositories import (
    open_connection,
    sqlite_uow_factory,
)
from tests.unit.test_branch_path import TreeKernel, _turn
from tests.unit.test_run_coordinator import _resume_context


@pytest.fixture
async def process_stack(tmp_path, vault_key):
    database = tmp_path / "test.db"
    factory = sqlite_uow_factory(database, vault_key)
    worker = ConversationProcess(
        ConversationProcessConfig(database, tmp_path, vault_key.key_bytes())
    )
    kernel = TreeKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    coord.storage_worker = worker
    from limbowave.application.services.configuration_service import ConfigurationService
    from limbowave.application.services.memory_service import MemoryService
    from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository

    coord.memory_service = MemoryService(
        factory, ConfigurationService(JsonConfigRepository(tmp_path / "config.json"))
    )
    yield coord, kernel, factory, worker, database
    await coord.wait_idle()
    await worker.close()
    factory.close()


async def test_real_child_does_preparation_commits_and_finalization(process_stack, monkeypatch):
    coord, kernel, factory, worker, _ = process_stack
    pid = await worker.warm()
    assert pid != os.getpid()
    assert await worker.warm() == pid  # warm process is reused on subsequent operations

    def forbidden(*args, **kwargs):
        raise AssertionError("storage business ran in caller process")

    for name in (
        "_prepare_regeneration", "_persist_branch", "_persist_run_start",
        "_commit_final", "_memory_prompt",
    ):
        monkeypatch.setattr(coord, name, forbidden)
    await _turn(kernel, coord, "question", "original")
    original_branch = coord.branch_id
    history = HistoryService(factory)
    original = history.branch_messages(original_branch)
    events = []
    coord.subscribe(events.append)
    assert await coord.regenerate(original[-1].id)
    assert coord.branch_id != original_branch
    assert kernel.sent == ["question", "question"]
    assert [e.kind for e in events][:2] == ["branched", "user"]
    assert events[0].data["history"] == []
    kernel.say("replacement")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    assert [m.content for m in history.branch_messages(coord.branch_id)] == [
        "question", "replacement"
    ]
    assert history.branch_messages(original_branch) == original


async def test_locked_worker_does_not_block_loop_or_allow_duplicate_send(process_stack):
    coord, kernel, factory, worker, database = process_stack
    await worker.warm()
    await _turn(kernel, coord, "question", "original")
    target = HistoryService(factory).branch_messages(coord.branch_id)[-1]
    lock = open_connection(database)
    lock.execute("BEGIN IMMEDIATE")
    task = asyncio.create_task(coord.regenerate(target.id))
    try:
        # The child can read and fork, then must wait at the branch commit.
        for _ in range(100):
            if not kernel.path:
                break
            await asyncio.sleep(0.01)
        assert not kernel.path
        beats = 0
        for _ in range(20):
            await asyncio.sleep(0.005)
            beats += 1
        assert beats == 20 and not task.done()
        assert coord.busy
        assert await coord.regenerate(target.id) is None
        assert await coord.send("duplicate") is None
        assert await coord.new_session() is False
    finally:
        lock.rollback()
        lock.close()
    assert await task
    assert kernel.sent == ["question", "question"]
    kernel.say("done")
    kernel.emit("run.settled", {})
    await coord.wait_idle()


async def test_broken_process_is_reported_without_fallback_to_ui(process_stack):
    coord, kernel, factory, worker, _ = process_stack
    await worker.warm()
    await _turn(kernel, coord, "question", "original")
    original_branch = coord.branch_id
    target = HistoryService(factory).branch_messages(original_branch)[-1]
    events = []
    coord.subscribe(events.append)
    process = next(iter(worker._executor._processes.values()))
    process.terminate()
    await asyncio.to_thread(process.join)
    assert await coord.regenerate(target.id) is None
    assert kernel.sent == ["question"]
    assert coord.branch_id == original_branch
    assert not coord.busy
    assert events[-1].kind == "error"


async def test_close_rejects_new_work(process_stack):
    _coord, _kernel, _factory, worker, _database = process_stack
    await worker.warm()
    await worker.close()
    with pytest.raises(RuntimeError, match="closed"):
        await worker.warm()


async def test_cancel_and_close_wait_for_inflight_transaction(process_stack):
    from datetime import UTC, datetime

    from limbowave.domain.conversation import Branch

    coord, kernel, factory, worker, database = process_stack
    await worker.warm()
    await _turn(kernel, coord, "question", "answer")
    anchor = HistoryService(factory).branch_messages(coord.branch_id)[0]
    lock = open_connection(database)
    lock.execute("BEGIN IMMEDIATE")
    branch = Branch("cancelled-branch", coord.conversation_id, datetime.now(UTC), coord.branch_id)
    pending = asyncio.create_task(worker.call(
        "_persist_branch", (coord.conversation_id, coord.branch_id), branch, anchor.id
    ))
    await asyncio.sleep(0.15)
    pending.cancel()
    closing = asyncio.create_task(worker.close())
    try:
        await asyncio.sleep(0.05)
        assert not closing.done()
        assert not pending.done()
    finally:
        lock.rollback()
        lock.close()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await closing
    with factory() as uow:
        assert uow.branches.get(branch.id) is not None  # commit finishes before shutdown


class _ProcessOnlyToolValue(str):
    def __new__(cls, value="prepared-in-child"):
        return super().__new__(cls, value)

    def __init__(self, value="prepared-in-child"):
        self.parent_pid = os.getpid()

    def __str__(self):
        assert os.getpid() != self.parent_pid, "tool formatting ran on GUI process"
        return "prepared-in-child"

    __repr__ = __str__


async def test_tool_normalization_and_display_are_ordered_in_child(process_stack):
    coord, kernel, factory, worker, _ = process_stack
    await worker.warm()
    events = []
    coord.subscribe(events.append)
    await coord.send("inspect file")
    kernel.emit("message.start", {"message": {"role": "assistant"}})
    kernel.emit("tool.start", {"toolCallId": "c1", "toolName": "read",
                               "args": {"path": _ProcessOnlyToolValue()}})
    await asyncio.sleep(0.02)
    kernel.emit("tool.end", {"toolCallId": "c1", "toolName": "read",
                             "result": _ProcessOnlyToolValue()})
    kernel.say("done")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    tool_events = [event for event in events if event.kind == "tool"]
    assert [e.data["phase"] for e in tool_events] == ["start", "end"]
    assert "prepared-in-child" in tool_events[0].data["step"].display_detail
    done = tool_events[1].data["step"]
    assert done.result_summary == "prepared-in-child"
    assert done.duration_ms >= 10
    assert not any(e.kind == "error" for e in events)
    stored = HistoryService(factory).branch_messages(coord.branch_id)[-1]
    assert stored.tool_steps[0].args["path"] == "prepared-in-child"
    assert stored.tool_steps[0].display_detail is None
    assert events.index(tool_events[-1]) < next(
        i for i, event in enumerate(events) if event.kind == "settled"
    )


async def test_tool_worker_failure_is_visible_without_gui_fallback(process_stack):
    coord, kernel, _factory, worker, _ = process_stack
    await coord.send("inspect")
    events = []
    coord.subscribe(events.append)
    await worker.close()
    kernel.emit("tool.start", {"toolCallId": "c", "toolName": "read"})
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    assert not coord.busy
    assert any(event.kind == "error" for event in events)
    assert not coord._pending_kernel_events


async def test_history_process_prepares_tools_and_rejects_wrong_branch(process_stack):
    coord, kernel, factory, worker, _ = process_stack
    await coord.send("inspect")
    kernel.emit("tool.start", {"toolCallId": "c", "toolName": "read",
                               "args": {"path": "test.txt"}})
    kernel.emit("tool.end", {"toolCallId": "c", "result": "file contents"})
    kernel.say("done")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    data = await worker.call("history_view", (None, None), coord.conversation_id, coord.branch_id)
    assert data.messages == HistoryService(factory).branch_messages(coord.branch_id)
    step = data.entries[-1].tool_steps[0]
    assert "test.txt" in step.display_detail
    assert "file contents" in step.display_detail
    assert data.messages[-1].tool_steps[0].display_detail is None
    assert data.attachment_ids == {data.messages[0].id: ()}
    assert await worker.call(
        "history_view", (None, None), "another-conversation", coord.branch_id
    ) is None


async def test_tool_queue_does_not_charge_ipc_delay_to_duration(process_stack, monkeypatch):
    coord, kernel, _factory, worker, _ = process_stack
    await coord.send("inspect")
    original = worker.call

    async def slow(operation, *args, **kwargs):
        if operation == "prepare_tool_event":
            await asyncio.sleep(0.15)
        return await original(operation, *args, **kwargs)

    monkeypatch.setattr(worker, "call", slow)
    events = []
    coord.subscribe(events.append)
    kernel.emit("tool.start", {"toolCallId": "c", "toolName": "read"})
    kernel.emit("tool.end", {"toolCallId": "c", "result": "done"})
    kernel.say("answer")
    kernel.emit("run.settled", {})
    waiting = asyncio.create_task(coord.wait_idle())
    for _ in range(10):
        await asyncio.sleep(0.005)
    assert not waiting.done()  # IPC is pending, but the caller's loop keeps responding.
    await waiting
    steps = [e.data["step"] for e in events if e.kind == "tool"]
    assert steps[-1].duration_ms < 100
    assert [e.data["phase"] for e in events if e.kind == "tool"] == ["start", "end"]


async def test_interruption_waits_for_queued_tool_audit(process_stack):
    coord, kernel, factory, _worker, _ = process_stack
    await coord.send("inspect")
    kernel.emit("tool.start", {"toolCallId": "c", "toolName": "read",
                               "args": {"path": "keep.txt"}})
    kernel.emit("tool.end", {"toolCallId": "c", "result": "kept"})
    await coord.mark_interrupted("Runtime exited")
    messages = HistoryService(factory).branch_messages(coord.branch_id)
    assert messages[-1].tool_steps[0].args == {"path": "keep.txt"}
    assert messages[-1].tool_steps[0].result_summary == "kept"
    assert not coord._pending_kernel_events


async def test_unused_worker_does_not_allocate_a_process_pool(tmp_path, vault_key, monkeypatch):
    from limbowave.infrastructure import conversation_process

    def unexpected_pool(*args, **kwargs):
        pytest.fail("no process-pool resources should be allocated before first use")

    monkeypatch.setattr(conversation_process, "ProcessPoolExecutor", unexpected_pool)
    worker = ConversationProcess(ConversationProcessConfig(
        tmp_path / "unused.db", tmp_path, vault_key.key_bytes()
    ))
    assert worker._executor is None
    worker.close_unstarted()
    await worker.close()
    assert not (tmp_path / "unused.db").exists()


async def test_first_call_lazily_starts_and_reuses_worker(process_stack):
    _, _, _, worker, _ = process_stack
    assert worker._executor is None
    child = await worker.call("pid", (None, None))
    assert child != os.getpid()
    assert await worker.call("pid", (None, None)) == child
