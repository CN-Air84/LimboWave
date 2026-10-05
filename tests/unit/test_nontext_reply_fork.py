"""Visible non-text replies must have persisted IDs usable by Fork/regenerate."""

import pytest

from limbowave.application.services.history_service import HistoryService
from limbowave.application.services.run_coordinator import RunCoordinator
from limbowave.domain.conversation import MessageRole, MessageStatus
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory
from tests.unit.test_branch_path import TreeKernel
from tests.unit.test_run_coordinator import _resume_context


@pytest.fixture(params=["memory", "sqlite"])
def factory(request, tmp_path, vault_key):
    result = (
        sqlite_uow_factory(tmp_path / "nontext.db", vault_key)
        if request.param == "sqlite"
        else in_memory_uow_factory()
    )
    yield result
    if hasattr(result, "close"):
        result.close()


def emit_nontext_reply(kernel, kind, *, stop="stop"):
    """Simulate the screenshot's five tools, or a thinking-only response."""
    if kind == "tools":
        kernel.say("", stop="toolUse")
        kernel.path[-1]["message"]["content"] = [
            {"type": "toolCall", "id": f"call-{i}", "name": "read_document", "arguments": {}}
            for i in range(5)
        ]
        for i in range(5):
            payload = {"toolCallId": f"call-{i}", "toolName": "read_document"}
            kernel.emit("tool.start", payload)
            kernel._append("toolResult", f"Document {i}")
            kernel.emit("tool.end", {**payload, "result": f"Document {i}", "isError": False})
        kernel.say("", stop=stop)
    else:
        kernel._append("assistant", "")
        kernel.path[-1]["message"]["content"] = [{"type": "thinking", "thinking": "分析文档"}]
        kernel.emit("message.start", {"message": {"role": "assistant"}})
        kernel.emit(
            "message.update",
            {"assistantMessageEvent": {"type": "thinking_delta", "delta": "分析文档"}},
        )
        kernel.emit("message.end", {"message": {
            **kernel.path[-1]["message"], "stopReason": stop,
        }})


@pytest.mark.parametrize("kind", ["tools", "thinking"])
async def test_nontext_reply_keeps_its_id_and_can_fork_and_regenerate(factory, kind):
    kernel = TreeKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    events = []
    coord.subscribe(events.append)
    await coord.send("读取文档")
    emit_nontext_reply(kernel, kind)
    target_id = [e.data["message_id"] for e in events if e.kind == "assistant_end"][-1]
    original_branch = coord.branch_id
    original_path = list(kernel.path)
    kernel.emit("run.settled", {})

    # Click immediately: Fork must wait for finalization, including non-text content.
    new_branch = await coord.fork_message(target_id)
    assert new_branch is not None
    assert new_branch != original_branch
    assert kernel.sent == ["读取文档"]
    assert kernel.path == original_path
    assert not [e for e in events if e.kind == "error"]
    with factory() as uow:
        target = uow.messages.get(target_id)
        assert target is not None
        assert target.role is MessageRole.ASSISTANT
        assert target.content == ""
        assert target.status is MessageStatus.COMPLETE
        assert uow.runs.get(target.run_id).assistant_message_id == target_id
        assert len(target.tool_steps) == (5 if kind == "tools" else 0)
        assert target.thinking == ("分析文档" if kind == "thinking" else "")
        assert uow.memories.get(f"snapshot:{target_id}") is not None

    history = HistoryService(factory)
    original = history.branch_messages(original_branch)
    assert len(original) == 2
    assert history.branch_messages(new_branch) == original
    assert await coord.switch_branch(original_branch)
    assert await coord.switch_branch(new_branch)
    assert kernel.path == original_path
    assert await coord.regenerate(target_id)
    kernel.say("重新生成的回答")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    assert kernel.sent == ["读取文档", "读取文档"]
    assert history.branch_messages(new_branch) == original


@pytest.mark.parametrize("kind", ["tools", "thinking"])
@pytest.mark.parametrize("stop", ["aborted", "error"])
async def test_incomplete_nontext_reply_is_saved_but_cannot_fork(factory, kind, stop):
    kernel = TreeKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await coord.send("读取文档")
    emit_nontext_reply(kernel, kind, stop=stop)
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    original_branch = coord.branch_id
    messages = HistoryService(factory).branch_messages(original_branch)
    assert len(messages) == 2
    target = messages[-1]
    expected = MessageStatus.PARTIAL if stop == "aborted" else MessageStatus.FAILED
    assert target.status is expected
    assert await coord.fork_message(target.id) is None
    assert coord.branch_id == original_branch
    assert kernel.sent == ["读取文档"]


async def test_truly_empty_reply_does_not_create_a_blank_message(factory):
    kernel = TreeKernel()
    coord = RunCoordinator(kernel, factory, context=_resume_context)
    await coord.send("读取文档")
    kernel.say("")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    messages = HistoryService(factory).branch_messages(coord.branch_id)
    assert len(messages) == 1
    assert messages[0].role is MessageRole.USER
