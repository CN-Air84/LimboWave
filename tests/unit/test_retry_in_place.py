"""失败/停止重试留在当前分支，只有正常回复的 Fork 才创建分支。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.domain.conversation import MessageRole, MessageStatus
from limbowave.domain.retry import RetryPolicy
from limbowave.domain.run import RunStatus
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.unit.test_run_coordinator import BranchingKernel, FakeKernel, _seed_one_turn


def _coordinator(kernel: FakeKernel, store: InMemoryStore) -> RunCoordinator:
    return RunCoordinator(
        kernel,
        in_memory_uow_factory(store),
        context=lambda: RunContext(
            logical_model_id="fake",
            endpoint_id="fake",
            routing_reason="test",
            retry_policy=RetryPolicy(max_attempts=1),
        ),
    )


@pytest.mark.parametrize("with_entries", [False, True])
@pytest.mark.parametrize("stop", ["error", "aborted", "interrupted", "send_error"])
@pytest.mark.parametrize("text", ["", "部分输出"])
async def test_retry_keeps_branch_and_prior_attempts(
    with_entries: bool, stop: str, text: str
) -> None:
    store = InMemoryStore()
    kernel = BranchingKernel()
    if with_entries:
        _seed_one_turn(kernel)
    coord = _coordinator(kernel, store)
    events = []
    coord.subscribe(events.append)
    if stop == "send_error":
        kernel.send_error = RuntimeError("发送失败")
    original_run = await coord.send("第一轮")
    original_branch = coord.branch_id
    conversation = coord.conversation_id
    user_id = store.runs[original_run].user_message_id
    if stop == "interrupted":
        kernel.say(text)
        await coord.mark_interrupted()
    elif stop != "send_error":
        if stop == "aborted":
            await coord.abort()
        kernel.say(text, stop=stop)
        kernel.emit("run.settled", {})
    # 故意不 wait_idle：点击重试可能先于旧轮异步收尾。
    kernel.send_error = None
    retry_run = await coord.retry_user_message(user_id)
    assert retry_run is not None and retry_run != original_run
    assert coord.conversation_id == conversation
    assert coord.branch_id == original_branch
    assert len(store.branches) == 1
    assert kernel.forked == []
    assert not any(event.kind == "branched" for event in events)
    assert kernel.sent[-1] == "第一轮"
    assert store.runs[retry_run].branch_id == original_branch
    assert store.runs[retry_run].retry_of_message_id == user_id
    retry_event = [event for event in events if event.kind == "user"][-1]
    assert retry_event.data["retry_of_message_id"] == user_id
    assert retry_event.data["message_id"] == store.runs[retry_run].user_message_id
    assert store.runs[original_run].status is {
        "error": RunStatus.FAILED,
        "aborted": RunStatus.ABORTED,
        "interrupted": RunStatus.INTERRUPTED,
        "send_error": RunStatus.FAILED,
    }[stop]
    if text and stop != "send_error":
        assert any(m.content == text and m.run_id == original_run for m in store.messages.values())
    if stop == "aborted":
        end = next(event for event in events if event.kind == "assistant_end")
        assert end.data["user_message_id"] == user_id
        assert end.data["stop_reason"] == "aborted"
    kernel.say("重试成功")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    assert store.runs[retry_run].status is RunStatus.COMPLETED


@pytest.mark.parametrize("stop", ["error", "aborted", "interrupted"])
async def test_regenerate_incomplete_reply_does_not_fork(stop: str) -> None:
    store = InMemoryStore()
    kernel = BranchingKernel()
    _seed_one_turn(kernel)
    coord = _coordinator(kernel, store)
    first_run = await coord.send("第一轮")
    first_branch = coord.branch_id
    kernel.say("部分输出", stop=stop if stop != "interrupted" else "stop")
    if stop == "interrupted":
        await coord.mark_interrupted()
    else:
        kernel.emit("run.settled", {})
    await coord.wait_idle()
    assistant_id = store.runs[first_run].assistant_message_id
    assert assistant_id is not None
    retry_run = await coord.regenerate(assistant_id)
    assert retry_run is not None
    assert coord.branch_id == first_branch
    assert len(store.branches) == 1
    assert kernel.forked == []
    assert kernel.sent == ["第一轮", "第一轮"]
    kernel.say("完成")
    kernel.emit("run.settled", {})
    await coord.wait_idle()


async def test_retry_without_branching_capability_and_repeated_failures() -> None:
    store = InMemoryStore()
    kernel = FakeKernel()
    coord = _coordinator(kernel, store)
    run_id = await coord.send("原文")
    branch_id = coord.branch_id
    for _ in range(3):
        kernel.say("", stop="error")
        kernel.emit("run.settled", {})
        user_id = store.runs[run_id].user_message_id
        run_id = await coord.retry_user_message(user_id)
        assert run_id is not None
        assert coord.branch_id == branch_id
    assert kernel.sent == ["原文"] * 4
    assert len(store.branches) == 1
    kernel.say("成功")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    assert len(store.runs) == 4


async def test_retry_rejects_invalid_foreign_messages_and_busy_state() -> None:
    store = InMemoryStore()
    kernel = BranchingKernel()
    _seed_one_turn(kernel)
    coord = _coordinator(kernel, store)
    run_id = await coord.send("第一轮")
    user_id = store.runs[run_id].user_message_id
    assert await coord.retry_user_message(user_id) is None  # busy
    kernel.say("部分输出", stop="aborted")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    assistant_id = store.runs[run_id].assistant_message_id
    assert await coord.retry_user_message("missing") is None
    assert await coord.retry_user_message(assistant_id) is None

    assert await coord.new_session()
    second_run = await coord.send("另一个会话")
    kernel.say("完成")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    location = (coord.conversation_id, coord.branch_id)
    assert await coord.retry_user_message(user_id) is None
    assert await coord.regenerate(assistant_id) is None
    assert (coord.conversation_id, coord.branch_id) == location
    assert len(store.runs) == 2
    assert store.runs[second_run].status is RunStatus.COMPLETED
    assert kernel.forked == []


async def test_retry_rejects_message_from_sibling_branch() -> None:
    store = InMemoryStore()
    kernel = BranchingKernel()
    _seed_one_turn(kernel)
    coord = _coordinator(kernel, store)
    first_run = await coord.send("第一轮")
    kernel.say("完成")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    first_user = store.runs[first_run].user_message_id
    await coord.edit_user_message(first_user, "改过的输入")
    kernel.say("完成")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    # 分叉点的原用户消息不在新分支继承前缀里。
    assert await coord.retry_user_message(first_user) is None
    assert len(store.runs) == 2


async def test_regenerate_uses_failed_run_status_even_if_message_is_complete() -> None:
    store = InMemoryStore()
    kernel = BranchingKernel()
    coord = _coordinator(kernel, store)
    run_id = await coord.send("第一轮")
    kernel.say("部分输出", stop="error")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    assistant = next(m for m in store.messages.values() if m.role is MessageRole.ASSISTANT)
    with in_memory_uow_factory(store)() as uow:
        uow.messages.update(replace(assistant, status=MessageStatus.COMPLETE))
        uow.commit()
    assert store.runs[run_id].status is RunStatus.FAILED
    assert await coord.regenerate(assistant.id) is not None
    assert kernel.forked == []
    assert len(store.branches) == 1
    kernel.say("成功")
    kernel.emit("run.settled", {})
    await coord.wait_idle()


async def test_retry_reserves_location_while_waiting_for_finalize() -> None:
    class SlowEntriesKernel(BranchingKernel):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def get_entries(self, *, since: str | None = None) -> list[dict]:
            self.entered.set()
            await self.release.wait()
            return await super().get_entries(since=since)

    store = InMemoryStore()
    kernel = SlowEntriesKernel()
    coord = _coordinator(kernel, store)
    first_run = await coord.send("第一轮")
    user_id = store.runs[first_run].user_message_id
    kernel.say("部分输出", stop="aborted")
    kernel.emit("run.settled", {})
    await kernel.entered.wait()
    retry = asyncio.create_task(coord.retry_user_message(user_id))
    await asyncio.sleep(0)
    assert kernel.sent == ["第一轮"]
    switching = asyncio.create_task(coord.new_session())
    await asyncio.sleep(0)
    assert switching.done()
    assert await switching is False
    kernel.release.set()
    assert await retry is not None
    assert kernel.sent == ["第一轮", "第一轮"]
    assert kernel.forked == []
    assert store.runs[first_run].status is RunStatus.ABORTED


@pytest.mark.parametrize("action", ["regenerate", "edit"])
@pytest.mark.parametrize("attempts", [1, 3])
async def test_regenerating_successful_retry_does_not_revive_aborted_turn(
    action: str, attempts: int,
) -> None:
    from limbowave.application import branch_path
    from limbowave.application.history_payload import history_payload
    from tests.unit.test_branch_path import TreeKernel

    store = InMemoryStore()
    factory = in_memory_uow_factory(store)
    kernel = TreeKernel()
    coord = _coordinator(kernel, store)
    events = []
    coord.subscribe(events.append)
    # An independent identical send must not be deduplicated by text.
    await coord.send("n")
    kernel.say("earlier")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    first = await coord.send("n")
    second = first
    for _ in range(attempts):
        kernel.say("x", stop="aborted")
        kernel.emit("run.settled", {})
        await coord.wait_idle()
        second = await coord.retry_user_message(store.runs[second].user_message_id)
        assert second is not None
    kernel.say("y")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    old_branch = coord.branch_id

    def contents(branch):
        with factory() as uow:
            messages = branch_path.branch_messages(uow, branch)
        return [entry.content for entry in history_payload(
            messages, uow_factory=factory, branch_id=branch,
        )]

    assert contents(old_branch) == ["n", "earlier", "n", "y"]
    prepared = coord._prepare_regeneration(store.runs[second].assistant_message_id)
    assert [entry.content for entry in prepared.history] == ["n", "earlier"]
    if action == "regenerate":
        third = await coord.regenerate(store.runs[second].assistant_message_id)
    else:
        third = await coord.edit_user_message(store.runs[second].user_message_id, "edited")
    assert third is not None
    assert [entry.content for entry in next(
        event for event in reversed(events) if event.kind == "branched"
    ).data["history"]] == ["n", "earlier"]
    kernel.say("z")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    expected_user = "n" if action == "regenerate" else "edited"
    assert contents(coord.branch_id) == ["n", "earlier", expected_user, "z"]
    assert contents(old_branch) == ["n", "earlier", "n", "y"]
    # Reopen through a fresh projection and regenerate on a descendant branch.
    fourth = await coord.regenerate(store.runs[third].assistant_message_id)
    assert fourth is not None
    kernel.say("final")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    assert contents(coord.branch_id) == ["n", "earlier", expected_user, "final"]
    assert any(message.content == "x" for message in store.messages.values())
