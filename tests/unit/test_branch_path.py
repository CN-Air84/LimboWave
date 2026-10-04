"""分支路径：编辑消息分叉后，新旧分支各自的对话必须互不串台。

回归场景（用户报告）：两轮对话后编辑第二条用户消息 →
- 新分支应显示「第一轮 + 改后的第二条 + 新回复」；
- 旧分支必须仍是「第一轮 + 原第二条 + 原回复」，不能被新分支内容"覆盖"；
- 切回旧分支时，运行时上下文要恢复到旧分支的叶子。
"""

from __future__ import annotations

from typing import Any

from limbowave.application import branch_path
from limbowave.application.kernel import KernelCapabilities, KernelCapability
from limbowave.application.services.history_service import HistoryService
from limbowave.application.services.run_coordinator import RunCoordinator
from limbowave.domain.conversation import MessageRole
from limbowave.domain.runtime_state import RuntimeRestoreResult
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.unit.test_run_coordinator import FakeKernel, _resume_context


class TreeKernel(FakeKernel):
    """按 Pi 语义维护条目树：send 追加用户条目，say 追加助手条目，fork 回到用户条目之前。"""

    def __init__(self) -> None:
        super().__init__()
        self.path: list[dict[str, Any]] = []
        self.restored: list[str] = []
        self._counter = 0

    def capabilities(self) -> KernelCapabilities:
        return KernelCapabilities(
            capabilities=frozenset({KernelCapability.BRANCHING, KernelCapability.RUNTIME_RESTORE})
        )

    def _append(self, role: str, text: str) -> None:
        self._counter += 1
        self.path.append(
            {
                "id": f"e{self._counter}",
                "type": "message",
                "parentId": self.path[-1]["id"] if self.path else None,
                "message": {"role": role, "content": [{"type": "text", "text": text}]},
            }
        )

    async def send_message(self, text: str, *, images: list[dict[str, Any]] | None = None) -> None:
        await super().send_message(text, images=images)
        self._append("user", text)

    def say(self, text: str, *, stop: str = "stop") -> None:
        self._append("assistant", text)
        super().say(text, stop=stop)

    async def get_entries(self, *, since: str | None = None) -> list[dict[str, Any]]:
        return list(self.path)

    async def fork(self, entry_id: str) -> str:
        index = next(i for i, e in enumerate(self.path) if e["id"] == entry_id)
        text = self.path[index]["message"]["content"][0]["text"]
        self.path = self.path[:index]
        return str(text)

    async def restore_runtime_state(self, snapshot: Any) -> RuntimeRestoreResult:
        self.path = list(snapshot.entries)
        self.restored.append(str(snapshot.leaf_entry_id))
        return RuntimeRestoreResult(success=True, restored_entry_count=len(self.path))


async def _turn(kernel: TreeKernel, coord: RunCoordinator, text: str, reply: str) -> None:
    await coord.send(text)
    kernel.say(reply)
    kernel.emit("run.settled", {})
    await coord.wait_idle()


def _contents(messages: list[Any]) -> list[str]:
    return [m.content for m in messages]


async def _two_turns_then_edit() -> tuple[TreeKernel, RunCoordinator, InMemoryStore, str, str]:
    store = InMemoryStore()
    kernel = TreeKernel()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await _turn(kernel, coord, "问一", "答一")
    await _turn(kernel, coord, "问二", "答二")
    old_branch = coord.branch_id
    assert old_branch is not None
    with in_memory_uow_factory(store)() as uow:
        second = next(
            m
            for m in uow.messages.list_for_branch(old_branch)
            if m.role is MessageRole.USER and m.content == "问二"
        )

    await coord.edit_user_message(second.id, "问二（改）")
    kernel.say("答二（新）")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    new_branch = coord.branch_id
    assert new_branch is not None and new_branch != old_branch
    return kernel, coord, store, old_branch, new_branch


async def test_edited_branch_inherits_prefix_and_old_branch_is_untouched() -> None:
    _, _, store, old_branch, new_branch = await _two_turns_then_edit()
    history = HistoryService(in_memory_uow_factory(store))

    assert _contents(history.branch_messages(new_branch)) == [
        "问一",
        "答一",
        "问二（改）",
        "答二（新）",
    ]
    assert _contents(history.branch_messages(old_branch)) == ["问一", "答一", "问二", "答二"]


async def test_open_conversation_shows_current_branch_with_prefix() -> None:
    _, coord, store, _, new_branch = await _two_turns_then_edit()
    history = HistoryService(in_memory_uow_factory(store))

    opened = history.open_conversation(str(coord.conversation_id))
    assert opened is not None
    branch_id, messages = opened
    assert branch_id == new_branch
    assert _contents(messages) == ["问一", "答一", "问二（改）", "答二（新）"]


async def test_switch_back_restores_old_leaf_and_keeps_chatting_there() -> None:
    kernel, coord, store, old_branch, new_branch = await _two_turns_then_edit()
    factory = in_memory_uow_factory(store)
    with factory() as uow:
        old_leaf = branch_path.branch_leaf_entry(uow, old_branch)
        new_leaf = branch_path.branch_leaf_entry(uow, new_branch)
    assert old_leaf is not None and new_leaf is not None and old_leaf != new_leaf

    assert await coord.switch_branch(old_branch)
    assert kernel.restored == [old_leaf]
    # 恢复出来的上下文正是旧分支的对话
    texts = [e["message"]["content"][0]["text"] for e in kernel.path]
    assert texts == ["问一", "答一", "问二", "答二"]

    # 在旧分支上继续聊：写进旧分支，旧分支成为当前分支
    await _turn(kernel, coord, "问三", "答三")
    history = HistoryService(factory)
    assert _contents(history.branch_messages(old_branch))[-2:] == ["问三", "答三"]
    assert _contents(history.branch_messages(new_branch)) == [
        "问一",
        "答一",
        "问二（改）",
        "答二（新）",
    ]
    opened = history.open_conversation(str(coord.conversation_id))
    assert opened is not None and opened[0] == old_branch


async def test_edit_first_message_has_empty_prefix() -> None:
    store = InMemoryStore()
    kernel = TreeKernel()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=_resume_context)
    await _turn(kernel, coord, "问一", "答一")
    first_branch = coord.branch_id
    assert first_branch is not None
    with in_memory_uow_factory(store)() as uow:
        first = uow.messages.list_for_branch(first_branch)[0]

    await coord.edit_user_message(first.id, "问一（改）")
    kernel.say("答一（新）")
    kernel.emit("run.settled", {})
    await coord.wait_idle()

    history = HistoryService(in_memory_uow_factory(store))
    assert _contents(history.branch_messages(str(coord.branch_id))) == ["问一（改）", "答一（新）"]
    assert _contents(history.branch_messages(first_branch)) == ["问一", "答一"]


async def test_intent_snapshot_records_inherited_context() -> None:
    """意图快照记录的是实际上下文：分叉分支的首轮也要带上继承的前缀。"""
    _, coord, store, _, _ = await _two_turns_then_edit()
    with in_memory_uow_factory(store)() as uow:
        intent = uow.snapshots.get_intent(str(coord.run_id))
        assert intent is not None
        contents = [uow.messages.get(mid).content for mid in intent.message_ids]  # type: ignore[union-attr]
    assert contents == ["问一", "答一", "问二（改）"]
