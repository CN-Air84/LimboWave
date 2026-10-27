"""记忆专用审批、运行身份和轮次接入。"""

import json

import pytest

from limbowave.app import _make_permission_handler
from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.application.services.tool_gateway import ToolGateway
from limbowave.domain.memory import MemoryPolicy, MemoryRunContext, MemorySettings
from limbowave.domain.permissions import PermissionPreset
from tests.unit.test_memory_service import memory_stack as memory_stack
from tests.unit.test_run_coordinator import FakeKernel


@pytest.mark.parametrize("preset", list(PermissionPreset))
@pytest.mark.parametrize("override", list(MemoryPolicy))
@pytest.mark.parametrize("default", ["ask", "allow"])
async def test_memory_policy_respects_full_access_override(
    memory_stack, monkeypatch, tmp_path, preset, override, default
):
    service, factory = memory_stack
    service.save_settings(MemorySettings(default_policy=default))
    service.set_policy("c", override)
    permissions = PermissionService(factory)
    permissions.set_preset("c", preset)
    context = MemoryRunContext("r", "c", "b", "msg")
    gateway = ToolGateway(permissions, tmp_path)
    gateway.memory_service = service
    gateway.memory_context = lambda: context
    asked = []

    async def answer(*args):
        asked.append(args)
        return False

    monkeypatch.setattr("limbowave.app._ask_memory_user", answer)
    handler = _make_permission_handler(
        None,
        permissions,
        lambda: "c",
        gateway,
        memory_service=service,
        memory_context=lambda: context,
    )
    detail = json.dumps(
        {
            "tool": "add_session_memory",
            "input": {"content": "note"},
            "run_id": "r",
            "call_id": "call",
        }
    )
    allowed = await handler("limbowave.gate:add_session_memory", detail)
    expected = (preset is PermissionPreset.FULL_ACCESS
                or service.effective_policy("c") is MemoryPolicy.ALLOW)
    assert allowed is expected
    assert bool(asked) is not expected
    params = {"content": "note", "call_id": "call", "run_id": "r"}
    # 客户端即使声称 confirmed，也不能铸造审批。
    result = gateway.invoke("add_session_memory", params, "c", user_confirmed=True)
    assert result.ok is expected
    assert len(service.list("c", "b")) == int(expected)
    assert not service.list()
    assert len(permissions.list_audit("c")) >= 1


async def test_late_approval_refused(memory_stack, monkeypatch):
    service, _ = memory_stack
    holder = [MemoryRunContext("r", "c", "b", "msg")]

    async def answer(*_):
        holder[0] = None
        return True

    monkeypatch.setattr("limbowave.app._ask_memory_user", answer)
    handler = _make_permission_handler(
        None, memory_service=service, memory_context=lambda: holder[0]
    )
    detail = json.dumps(
        {
            "tool": "add_session_memory",
            "input": {"content": "note"},
            "run_id": "r",
            "call_id": "call",
        }
    )
    assert not await handler("limbowave.gate:add_session_memory", detail)
    assert not service.list("c", "b")


async def test_user_round_count_and_abort(memory_stack):
    service, factory = memory_stack

    class MemoryKernel(FakeKernel):
        def __init__(self):
            super().__init__()
            self.contexts = []

        async def set_memory_context(self, context):
            self.contexts.append(context)

    kernel = MemoryKernel()
    coordinator = RunCoordinator(kernel, factory, context=lambda: RunContext("m", "e", "test"))
    coordinator.memory_service = service
    service.save("global")
    for _ in range(3):
        await coordinator.send("original")
        kernel.say("reply")
        kernel.emit("run.settled", {})
        await coordinator.wait_idle()
    assert [c["round"] for c in kernel.contexts] == [1, 2, 3]
    assert kernel.sent == ["original"] * 3
    await coordinator.send("pending")
    context = coordinator.memory_context
    assert context is not None
    service.approve(context, "call", "note")
    await coordinator.abort()
    assert coordinator.memory_context is None
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()


async def test_coordinator_fork_restores_memory_at_original_user_message(memory_stack):
    from tests.unit.test_run_coordinator import BranchingKernel, _one_turn, _seed_one_turn

    service, factory = memory_stack

    class MemoryBranchKernel(BranchingKernel):
        async def set_memory_context(self, context):
            self.last_context = context

    kernel = MemoryBranchKernel()
    coordinator = RunCoordinator(kernel, factory, context=lambda: RunContext("m", "e", "test"))
    coordinator.memory_service = service
    await coordinator.resume("c", "b")
    original = service.save("before turn", "c", "b")
    global_item = service.save("global before turn")
    _seed_one_turn(kernel)
    await _one_turn(kernel, coordinator)
    with factory() as uow:
        source = next(m for m in uow.messages.list_for_branch("b") if m.role.value == "user")
    service.save("after turn", "c", "b", item_id=original.id)
    service.save_session_global(global_item.id, "conversation-wide override", "c")
    kernel.entries = []
    assert await coordinator.edit_user_message(source.id, "edited") is not None
    assert service.list("c", coordinator.branch_id) == [original]
    assert kernel.last_context["session"] == ["before turn"]
    assert kernel.last_context["global"] == ["conversation-wide override"]
    assert kernel.last_context["round"] == 1
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()



async def test_coordinator_session_global_snapshot_and_conversation_switch(memory_stack):
    from tests.unit.test_memory_service import _second_conversation

    service, factory = memory_stack
    _second_conversation(factory)

    class MemoryKernel(FakeKernel):
        def __init__(self):
            super().__init__()
            self.contexts = []

        async def set_memory_context(self, context):
            self.contexts.append(context)

    kernel = MemoryKernel()
    coordinator = RunCoordinator(kernel, factory, context=lambda: RunContext("m", "e", "test"))
    coordinator.memory_service = service
    first = service.save("first global")
    second = service.save("second global")
    local = service.save("branch only", "c", "b")
    await coordinator.resume("c", "b")
    await coordinator.send("first run")
    before = kernel.contexts[-1]
    assert before["global"] == [first.content, second.content]
    service.save_session_global(first.id, "only c", "c")
    service.delete_session_global(second.id, "c")
    # 更改持久化配置不能修改已交给运行内核的快照。
    assert before["global"] == [first.content, second.content]
    kernel.say("reply")
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()
    await coordinator.send("next run")
    assert kernel.contexts[-1]["global"] == ["only c"]
    assert kernel.contexts[-1]["session"] == [local.content]
    kernel.say("reply")
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()
    await coordinator.resume("second", "second-branch")
    await coordinator.send("another conversation")
    assert kernel.contexts[-1]["global"] == [first.content, second.content]
    assert kernel.contexts[-1]["session"] == []
    kernel.say("reply")
    kernel.emit("run.settled", {})
    await coordinator.wait_idle()
    assert service.list() == [first, second]
