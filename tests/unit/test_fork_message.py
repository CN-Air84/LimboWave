"""Fork keeps its selected reply, runtime context and history without sending a prompt."""

import pytest

from limbowave.application import branch_path
from limbowave.application.services.history_service import HistoryService
from limbowave.application.services.run_coordinator import RunCoordinator
from limbowave.domain.runtime_state import RuntimeRestoreResult
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory
from tests.unit.test_branch_path import TreeKernel, _contents, _turn
from tests.unit.test_run_coordinator import _resume_context


@pytest.fixture(params=["memory", "sqlite"])
def factory(request, tmp_path, vault_key):
    result = (
        sqlite_uow_factory(tmp_path / "fork.db", vault_key)
        if request.param == "sqlite"
        else in_memory_uow_factory()
    )
    yield result
    if hasattr(result, "close"):
        result.close()


async def test_fork_keeps_anchor_without_send_and_restores_after_reopen(factory):
    kernel = TreeKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await _turn(kernel, coord, "问一", "答一")
    await _turn(kernel, coord, "问二", "答二")
    old_branch = coord.branch_id
    conversation = coord.conversation_id
    history = HistoryService(factory)
    original = history.branch_messages(old_branch)
    target = original[1]
    events = []
    coord.subscribe(events.append)

    new_branch = await coord.fork_message(target.id)

    assert new_branch and new_branch != old_branch
    assert coord.branch_id == new_branch
    assert kernel.sent == ["问一", "问二"]
    assert [e.kind for e in events] == ["branched"]
    assert not coord.busy
    assert history.branch_messages(old_branch) == original
    assert history.branch_messages(new_branch) == original[:2]
    assert [e["id"] for e in kernel.path] == ["e1", "e2"]
    with factory() as uow:
        assert uow.branches.get(new_branch).include_fork_message
        assert uow.branches.get(new_branch).forked_from_message_id == target.id
        assert not uow.messages.list_for_branch(new_branch)
        assert len(uow.runs.list_for_conversation(conversation)) == 2
        assert branch_path.branch_leaf_entry(uow, new_branch) == "e2"
    assert history.open_conversation(conversation)[0] == new_branch

    assert await coord.switch_branch(old_branch)
    assert kernel.restored[-1] == "e4"
    # A fresh coordinator must recover even before any new message exists on the fork.
    reopened = RunCoordinator(kernel, factory, context=_resume_context)
    assert await reopened.resume(conversation, new_branch)
    assert kernel.restored[-1] == "e2"
    await _turn(kernel, reopened, "沿着答一继续", "新回答")
    assert _contents(history.branch_messages(new_branch)) == [
        "问一", "答一", "沿着答一继续", "新回答"
    ]
    assert history.branch_messages(old_branch) == original
    with factory() as uow:
        intent = uow.snapshots.get_intent(reopened.run_id)
        assert [uow.messages.get(mid).content for mid in intent.message_ids] == [
            "问一", "答一", "沿着答一继续"
        ]


async def test_fork_of_inherited_reply_and_regenerate_remain_distinct(factory):
    kernel = TreeKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    history = HistoryService(factory)
    await _turn(kernel, coord, "问题", "原回答")
    old_branch = coord.branch_id
    target = history.branch_messages(old_branch)[-1]
    first = await coord.fork_message(target.id)
    second = await coord.fork_message(target.id)
    assert first and second and first != second
    assert history.branch_messages(first) == history.branch_messages(second)
    assert kernel.sent == ["问题"]
    assert await coord.regenerate(target.id)
    kernel.say("新回答")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    assert _contents(history.branch_messages(coord.branch_id)) == ["问题", "新回答"]
    assert _contents(history.branch_messages(second)) == ["问题", "原回答"]


async def test_fork_uses_whole_run_leaf_when_aggregate_reply_has_no_mirror_link(factory):
    kernel = TreeKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await coord.send("调用工具")
    kernel.say("先检查")
    kernel._append("toolResult", "检查结果")
    kernel.say("最终回答")
    kernel.path[-1]["message"]["content"][0]["text"] = "最终回答（运行时格式）"
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    history = HistoryService(factory)
    original = history.branch_messages(coord.branch_id)
    target = original[-1]
    with factory() as uow:
        assert not any(m.message_id == target.id for m in uow.runtime.list_for_conversation(
            coord.conversation_id
        ))
    assert await coord.fork_message(target.id)
    assert history.branch_messages(coord.branch_id) == original
    assert [e["id"] for e in kernel.path] == ["e1", "e2", "e3", "e4"]
    with factory() as uow:
        assert branch_path.branch_leaf_entry(uow, coord.branch_id) == "e4"


async def test_fork_rejects_busy_missing_user_incomplete_and_foreign_targets(factory):
    kernel = TreeKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await _turn(kernel, coord, "问题", "答案")
    history = HistoryService(factory)
    original = history.branch_messages(coord.branch_id)
    target = original[-1]
    assert await coord.fork_message("missing") is None
    assert await coord.fork_message(original[0].id) is None
    await coord.send("未完成")
    assert await coord.fork_message(target.id) is None
    kernel.say("半截", stop="aborted")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    incomplete = history.branch_messages(coord.branch_id)[-1]
    assert await coord.fork_message(incomplete.id) is None
    await coord.edit_user_message(original[0].id, "兄弟分支")
    kernel.say("兄弟回答")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    assert await coord.fork_message(target.id) is None
    assert await coord.new_session()
    assert await coord.fork_message(target.id) is None
    assert kernel.sent == ["问题", "未完成", "兄弟分支"]


async def test_fork_restore_failure_does_not_create_branch_or_send(factory):
    class FailingKernel(TreeKernel):
        fail = False

        async def restore_runtime_state(self, snapshot):
            if self.fail:
                self.fail = False
                return RuntimeRestoreResult(success=False, error="restore failed")
            return await super().restore_runtime_state(snapshot)

    kernel = FailingKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await _turn(kernel, coord, "问题", "回答")
    old_branch = coord.branch_id
    target = HistoryService(factory).branch_messages(old_branch)[-1]
    kernel.fail = True
    assert await coord.fork_message(target.id) is None
    assert coord.branch_id == old_branch
    assert kernel.sent == ["问题"]
    assert not coord.busy
    with factory() as uow:
        assert len(uow.branches.list_for_conversation(coord.conversation_id)) == 1
    assert coord._runtime_valid


async def test_fork_waits_for_finalize_and_blocks_other_runtime_operations(factory):
    import asyncio

    class SlowKernel(TreeKernel):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def get_entries(self, *, since=None):
            self.entered.set()
            await self.release.wait()
            return await super().get_entries(since=since)

    kernel = SlowKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await coord.send("问题")
    target_id = coord._live.assistant_message_id
    kernel.say("回答")
    kernel.emit("run.settled", {})
    await kernel.entered.wait()
    task = asyncio.create_task(coord.fork_message(target_id))
    await asyncio.sleep(0)
    assert coord.busy
    assert not task.done()
    assert await coord.new_session() is False
    assert await coord.send("不应发送") is None
    kernel.release.set()
    assert await task
    assert kernel.sent == ["问题"]
    assert _contents(HistoryService(factory).branch_messages(coord.branch_id)) == ["问题", "回答"]


async def test_fork_copies_completion_memory_not_later_edits(factory, tmp_path):
    from limbowave.application.services.configuration_service import ConfigurationService
    from limbowave.application.services.memory_service import MemoryService
    from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository

    kernel = TreeKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    memory = MemoryService(
        factory, ConfigurationService(JsonConfigRepository(tmp_path / "config.json"))
    )
    await coord.send("问题")
    original_branch = coord.branch_id
    item = memory.save("回复完成时的记忆", coord.conversation_id, original_branch)
    kernel.say("回答")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    target = HistoryService(factory).branch_messages(original_branch)[-1]
    memory.save("后来的修改", coord.conversation_id, original_branch, item_id=item.id)
    assert await coord.fork_message(target.id)
    assert memory.list(coord.conversation_id, coord.branch_id) == [item]
    assert memory.list(coord.conversation_id, original_branch)[0].content == "后来的修改"


async def test_fork_save_failure_restores_original_runtime(factory, monkeypatch):
    from limbowave.application.services import run_coordinator

    kernel = TreeKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await _turn(kernel, coord, "问一", "答一")
    await _turn(kernel, coord, "问二", "答二")
    original_branch = coord.branch_id
    target = HistoryService(factory).branch_messages(original_branch)[1]

    def fail(*args):
        raise RuntimeError("save failed")

    monkeypatch.setattr(run_coordinator, "fork_memory", fail)
    assert await coord.fork_message(target.id) is None
    assert coord.branch_id == original_branch
    assert coord._runtime_valid
    assert [e["id"] for e in kernel.path] == ["e1", "e2", "e3", "e4"]
    assert kernel.sent == ["问一", "问二"]
    with factory() as uow:
        assert len(uow.branches.list_for_conversation(coord.conversation_id)) == 1
