"""Active retrieval, explicit mutation consent, and branch-local working notes."""

import pytest

from limbowave.application.services.memory_service import capture_memory, fork_memory
from limbowave.domain.memory import MemoryRunContext
from tests.unit.test_memory_service import memory_stack as memory_stack


def test_search_scopes_overrides_and_ranking(memory_stack):
    service, _ = memory_stack
    first = service.save("小王\n下周讨论 Python 项目", "c", "b")
    service.save("Python unrelated", "c", "other")
    overridden = service.save("Python old")
    hidden = service.save("Python hidden")
    service.save_session_global(overridden.id, "Python new", "c")
    service.delete_session_global(hidden.id, "c")
    result = service.search("c", "b", "python")
    assert {r["id"] for r in result["results"]} == {first.id, overridden.id}
    assert "old" not in str(result) and "hidden" not in str(result)
    assert service.search("c", "b", "小王 python")["results"][0]["id"] == first.id
    assert service.search("c", "b", "Python", "session")["total"] == 1
    assert service.search("c", "b", "无结果")["results"] == []
    for query, scope, limit in [("", "all", 10), ("x", "other", 10),
                                 ("x", "all", True), ("x", "all", 21),
                                 ("x" * 501, "all", 10)]:
        with pytest.raises(ValueError):
            service.search("c", "b", query, scope, limit)


def test_search_bounded_excerpts(memory_stack):
    service, _ = memory_stack
    for _ in range(8):
        service.save("长" * 3990 + "关键词", "c", "b")
    result = service.search("c", "b", "关键词", limit=2)
    assert result["total"] == 8 and result["has_more"]
    assert len(result["results"]) == 2
    assert all(r["content_truncated"] and "关键词" in r["content"] for r in result["results"])
    full = service.search("c", "b", result["results"][0]["id"])["results"][0]
    assert not full["content_truncated"] and len(full["content"]) == 3993


def test_mutation_requires_bound_consent_and_is_idempotent(memory_stack):
    service, _ = memory_stack
    context = MemoryRunContext("r", "c", "b", "msg")
    item = service.save("old", "c", "b")
    change = service.prepare_mutation(context, "update_memory", item.id, "new")
    with pytest.raises(ValueError, match="授权"):
        service.mutate_from_model(context, "call", change, lambda _: True)
    service.approve_mutation(context, "call", change)
    result = service.mutate_from_model(context, "call", change, lambda _: True)
    assert result["id"] == item.id and service.list("c", "b")[0].content == "new"
    assert service.mutate_from_model(context, "call", change, lambda _: True) == result
    different = service.prepare_mutation(context, "delete_session_memory", item.id)
    with pytest.raises(ValueError):
        service.mutate_from_model(context, "call", different, lambda _: True)


def test_mutation_rejects_stale_original_and_other_branches(memory_stack):
    service, _ = memory_stack
    context = MemoryRunContext("r", "c", "b", "msg")
    item = service.save("old", "c", "b")
    change = service.prepare_mutation(context, "update_memory", item.id, "new")
    service.approve_mutation(context, "call", change)
    service.save("user changed it", "c", "b", item_id=item.id)
    with pytest.raises(ValueError, match="变化"):
        service.mutate_from_model(context, "call", change, lambda _: True)
    foreign = service.save("foreign", "c", "other")
    global_item = service.save("global")
    for memory_id in (foreign.id, global_item.id):
        with pytest.raises(ValueError):
            service.prepare_mutation(context, "delete_session_memory", memory_id)
    assert service.list("c", "b")[0].content == "user changed it"


@pytest.mark.parametrize("archive", [True, False])
def test_archive_or_delete_with_cancel_and_restore(memory_stack, archive):
    service, _ = memory_stack
    context = MemoryRunContext("r", "c", "b", "msg")
    item = service.save("done", "c", "b")
    change = service.prepare_mutation(context, "delete_session_memory", item.id, archive=archive)
    service.approve_mutation(context, "call", change)
    with pytest.raises(ValueError):
        service.mutate_from_model(context, "call", change, lambda _: False)
    assert service.list("c", "b") == [item]
    service.mutate_from_model(context, "call", change, lambda _: True)
    assert not service.list("c", "b")
    assert service.list_archived("c", "b") == ([item] if archive else [])
    assert service.search("c", "b", "done")["total"] == 0
    if archive:
        assert service.search("c", "b", "done", "archived")["total"] == 1
        service.restore_archived(item.id, "c", "b")
        assert service.list("c", "b") == [item]


def test_scratchpad_revision_isolation_and_historical_fork(memory_stack):
    service, factory = memory_stack
    assert service.read_scratchpad("c", "b").revision == 0
    first = service.write_scratchpad("c", "b", "待确认的会议时间", 0)
    with factory() as uow:
        capture_memory(uow, "msg", "c", "b")
        uow.commit()
    service.write_scratchpad("c", "b", "会议已结束", first.revision)
    with pytest.raises(ValueError, match="变化"):
        service.write_scratchpad("c", "b", "stale", first.revision)
    assert service.list("c", "b") == []
    with factory() as uow:
        fork_memory(uow, "msg", "c", "other")
        uow.commit()
    assert service.read_scratchpad("c", "other").content == first.content
    ctx = service.prompt_context(MemoryRunContext("r", "c", "b", "msg"), 2)
    assert ctx["scratchpad"]["content"] == "会议已结束"
    pad = service.read_scratchpad("c", "b")
    assert service.write_scratchpad("c", "b", "", pad.revision).content == ""
    for text, revision in [("x" * 4001, 3), ("x", True), ("x", -1)]:
        with pytest.raises(ValueError):
            service.write_scratchpad("c", "b", text, revision)
    with factory() as uow:
        fork_memory(uow, "legacy", "c", "other")
        uow.conversations.delete("c")
        uow.commit()
    with factory() as uow:
        assert uow.memories.get("scratchpad:b") is None
        assert uow.memories.get("scratchpad-snapshot:msg") is None

@pytest.mark.parametrize("tool,arguments", [
    ("update_memory", {"updated_content": "new"}),
    ("delete_session_memory", {}),
    ("delete_session_memory", {"archive": False}),
])
@pytest.mark.parametrize("accept", [True, False])
async def test_real_gate_mutations_always_ask(memory_stack, monkeypatch, tmp_path,
                                             tool, arguments, accept):
    import json

    from limbowave.app import _make_permission_handler
    from limbowave.application.services.permission_service import PermissionService
    from limbowave.application.services.tool_gateway import ToolGateway
    from limbowave.domain.memory import MemoryPolicy
    from limbowave.domain.permissions import PermissionPreset

    service, factory = memory_stack
    item = service.save("original", "c", "b")
    service.set_policy("c", MemoryPolicy.ALLOW)
    permissions = PermissionService(factory)
    permissions.set_preset("c", PermissionPreset.FULL_ACCESS)
    context = MemoryRunContext("r", "c", "b", "msg")
    gateway = ToolGateway(permissions, tmp_path)
    gateway.memory_context = lambda: context
    gateway.memory_service = service
    prompts = []

    async def answer(title, heading, detail):
        prompts.append((heading, detail))
        return accept

    monkeypatch.setattr("limbowave.app._ask_memory_user", answer)
    handler = _make_permission_handler(None, permissions, lambda: "c", gateway,
                                       memory_service=service, memory_context=lambda: context)
    params = {"memory_id": item.id, **arguments}
    wire = {**params, "call_id": "call", "run_id": "r"}
    assert not gateway.invoke(tool, wire, "c", user_confirmed=True).ok
    allowed = await handler("limbowave.gate:" + tool, json.dumps({
        "tool": tool, "input": params, "call_id": "call", "run_id": "r",
    }))
    assert allowed is accept and len(prompts) == 1
    assert "original" in prompts[0][1]
    result = gateway.invoke(tool, wire, "c", user_confirmed=True)
    assert result.ok is accept
    if accept:
        assert gateway.invoke(tool, wire, "c", user_confirmed=True).data == result.data
    else:
        assert service.list("c", "b") == [item]


def test_context_gateway_binds_scope_rejects_remote_and_stale(memory_stack, tmp_path):
    from limbowave.application.services.permission_service import PermissionService
    from limbowave.application.services.tool_gateway import ToolGateway

    service, factory = memory_stack
    context = MemoryRunContext("r", "c", "b", "msg")
    gateway = ToolGateway(PermissionService(factory), tmp_path)
    gateway.memory_service = service
    gateway.memory_context = lambda: context
    service.save("local", "c", "b")
    binding = {"call_id": "call", "run_id": "r"}
    assert gateway.invoke("search_memory", {**binding, "query": "local"}, "c").ok
    assert gateway.invoke("read_scratchpad", binding, "c").data["revision"] == 0
    wire = {**binding, "content": "work", "expected_revision": 0}
    assert gateway.invoke("write_scratchpad", wire, "c").ok
    assert not gateway.invoke("write_scratchpad", wire, "c").ok  # CAS, no silent repeat
    for tool, params in [("search_memory", {**binding, "query": "local"}),
                         ("read_scratchpad", binding), ("write_scratchpad", wire)]:
        assert not gateway.invoke(tool, params, "foreign", user_confirmed=True).ok
        assert not gateway.invoke(tool, {**params, "run_id": "old"}, "c").ok
        assert not gateway.invoke(tool, {**params, "branch_id": "other"}, "c").ok
        gateway.origin_provider = lambda: "web"
        assert not gateway.invoke(tool, params, "c", user_confirmed=True).ok
        gateway.origin_provider = None
    assert not gateway.invoke("write_scratchpad", {**wire, "expected_revision": True}, "c").ok


def test_archived_and_scratchpad_snapshots_are_encrypted(memory_stack, tmp_path):
    service, factory = memory_stack
    context = MemoryRunContext("r", "c", "b", "msg")
    item = service.save("UNIQUE_ARCHIVE_SECRET", "c", "b")
    change = service.prepare_mutation(context, "delete_session_memory", item.id)
    service.approve_mutation(context, "call", change)
    service.mutate_from_model(context, "call", change, lambda _: True)
    service.write_scratchpad("c", "b", "UNIQUE_SCRATCH_SECRET", 0)
    with factory() as uow:
        capture_memory(uow, "msg", "c", "b")
        uow.commit()
    service.restore_archived(item.id, "c", "b")
    with factory() as uow:
        fork_memory(uow, "msg", "c", "other")
        uow.commit()
    assert service.list_archived("c", "other") == [item]
    assert not service.list("c", "other")
    for file in tmp_path.glob("memory.db*"):
        assert b"UNIQUE_SCRATCH_SECRET" not in file.read_bytes()
        assert b"UNIQUE_ARCHIVE_SECRET" not in file.read_bytes()


def test_unicode_search_excerpt_uses_original_offsets(memory_stack):
    service, _ = memory_stack
    service.save("ß" * 2000 + "target", "c", "b")
    row = service.search("c", "b", "target")["results"][0]
    assert "target" in row["content"] and row["content_truncated"]


def test_receipt_cannot_be_reused_in_a_different_branch(memory_stack):
    service, _ = memory_stack
    first = MemoryRunContext("r", "c", "b", "msg")
    second = MemoryRunContext("r", "c", "other", "msg")
    item = service.save("original", "c", "b")
    change = service.prepare_mutation(first, "update_memory", item.id, "updated")
    service.approve_mutation(first, "call", change)
    service.mutate_from_model(first, "call", change, lambda _: True)
    with pytest.raises(ValueError, match="分支"):
        service.mutate_from_model(second, "call", change, lambda _: True)
    with pytest.raises(ValueError, match="分支"):
        service.mutation_for_call(second, "call", "update_memory", item.id, "updated")


def test_concurrent_scratchpad_edits_do_not_overwrite_each_other(memory_stack):
    from concurrent.futures import ThreadPoolExecutor

    service, _ = memory_stack

    def write(text):
        try:
            service.write_scratchpad("c", "b", text, 0)
            return text
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(write, ["first", "second", "third", "fourth"]))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert service.read_scratchpad("c", "b").content == winners[0]
    assert service.read_scratchpad("c", "b").revision == 1


def test_search_gateway_byte_limit_preserves_structured_results(memory_stack, tmp_path):
    import json

    from limbowave.application.services.permission_service import PermissionService
    from limbowave.application.services.tool_gateway import ToolGateway

    service, factory = memory_stack
    service.save("关键词" + "长" * 3990, "c", "b")
    gateway = ToolGateway(PermissionService(factory), tmp_path, max_output_bytes=500)
    gateway.memory_service = service
    gateway.memory_context = lambda: MemoryRunContext("r", "c", "b", "msg")
    result = gateway.invoke("search_memory", {
        "query": "关键词", "run_id": "r", "call_id": "call",
    }, "c")
    assert result.ok and result.truncated
    assert len(json.dumps(result.data, ensure_ascii=False).encode("utf-8")) <= 500
    assert result.data["output_truncated"] and result.data["has_more"]


@pytest.mark.parametrize("cancel", [False, True])
async def test_mutation_after_dialog_cannot_overwrite_new_state(
    memory_stack, tmp_path, monkeypatch, cancel,
):
    import json

    from limbowave.app import _make_permission_handler
    from limbowave.application.services.permission_service import PermissionService
    from limbowave.application.services.tool_gateway import ToolGateway

    service, factory = memory_stack
    item = service.save("original", "c", "b")
    holder = [MemoryRunContext("r", "c", "b", "msg")]
    gateway = ToolGateway(PermissionService(factory), tmp_path)
    gateway.memory_context = lambda: holder[0]
    gateway.memory_service = service

    async def answer(*_args):
        if cancel:
            holder[0] = None
        else:
            service.save("edited during dialog", "c", "b", item_id=item.id)
        return True

    monkeypatch.setattr("limbowave.app._ask_memory_user", answer)
    handler = _make_permission_handler(None, gateway=gateway, memory_service=service,
                                       memory_context=lambda: holder[0])
    params = {"memory_id": item.id, "updated_content": "model edit"}
    await handler("limbowave.gate:update_memory", json.dumps({
        "tool": "update_memory", "input": params, "run_id": "r", "call_id": "call",
    }))
    assert not gateway.invoke("update_memory", {**params, "run_id": "r", "call_id": "call"},
                              "c", user_confirmed=True).ok
    assert service.list("c", "b")[0].content == ("original" if cancel else "edited during dialog")


async def test_rejected_repeat_gate_revokes_unconsumed_mutation_approval(
    memory_stack, monkeypatch, tmp_path,
):
    import json

    from limbowave.app import _make_permission_handler
    from limbowave.application.services.permission_service import PermissionService
    from limbowave.application.services.tool_gateway import ToolGateway

    service, factory = memory_stack
    item = service.save("original", "c", "b")
    context = MemoryRunContext("r", "c", "b", "msg")
    gateway = ToolGateway(PermissionService(factory), tmp_path)
    gateway.memory_service = service
    gateway.memory_context = lambda: context
    answers = iter([True, False])

    async def answer(*_args):
        return next(answers)

    monkeypatch.setattr("limbowave.app._ask_memory_user", answer)
    handler = _make_permission_handler(None, gateway=gateway, memory_service=service,
                                       memory_context=lambda: context)
    params = {"memory_id": item.id, "updated_content": "updated"}
    detail = json.dumps({"tool": "update_memory", "input": params,
                         "run_id": "r", "call_id": "call"})
    assert await handler("limbowave.gate:update_memory", detail)
    assert not await handler("limbowave.gate:update_memory", detail)
    assert not gateway.invoke("update_memory", {**params, "run_id": "r", "call_id": "call"},
                              "c", user_confirmed=True).ok
    assert service.list("c", "b") == [item]


def test_search_explicitly_marks_model_writability(memory_stack):
    service, _ = memory_stack
    global_item = service.save("needle global")
    session_item = service.save("needle session", "c", "b")
    rows = {row["id"]: row for row in service.search("c", "b", "needle")["results"]}
    assert rows[global_item.id]["model_writable"] is True
    assert rows[global_item.id]["allowed_operations"] == ["update_memory"]
    assert rows[session_item.id]["allowed_operations"] == ["update_memory", "delete_session_memory"]
    assert rows[session_item.id]["model_writable"] is True
