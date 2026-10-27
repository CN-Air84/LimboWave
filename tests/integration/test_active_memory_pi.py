"""Real Pi context hooks, tool registration, consent and encrypted memory gateway."""

import json
import urllib.request

import pytest

from limbowave.app import _make_permission_handler
from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.tool_gateway import ToolGateway
from limbowave.domain.memory import MemoryRunContext
from limbowave.infrastructure.pi_adapter import PiKernelAdapter, build_spawn_spec
from limbowave.infrastructure.tools.ipc_server import IPC_ENV, ToolIpcServer
from tests.integration.test_memory_pi import _context
from tests.integration.test_pi_adapter import _wait_settled
from tests.integration.test_pi_adapter import mock_models as mock_models
from tests.unit.test_memory_service import memory_stack as memory_stack


async def test_scratchpad_overlay_replaced_cleared_and_branch_scoped(
    mock_models, mock_provider, tmp_path,
):
    _, log = mock_provider
    adapter = PiKernelAdapter(
        build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    )
    try:
        await adapter.start()
        for n, content, branch in [
            (1, "PAD_FIRST_UNIQUE", "b"), (2, "PAD_SECOND_UNIQUE", "b"),
            (3, "", "b"), (4, "PAD_CHILD_UNIQUE", "child"),
        ]:
            await adapter.set_memory_context(_context(
                n, branch_id=branch, scratchpad={"content": content, "revision": n},
            ))
            await adapter.send_message(f"PAD_USER_{n}")
            await _wait_settled(adapter)
    finally:
        await adapter.shutdown()
    bodies = [json.loads(line)["body"] for line in log.read_text(encoding="utf-8").splitlines()
              if "body" in json.loads(line)]
    assert len(bodies) == 4
    for index, marker in enumerate([
        "PAD_FIRST_UNIQUE", "PAD_SECOND_UNIQUE", "", "PAD_CHILD_UNIQUE",
    ]):
        text = json.dumps(bodies[index]["messages"])
        for candidate in ("PAD_FIRST_UNIQUE", "PAD_SECOND_UNIQUE", "PAD_CHILD_UNIQUE"):
            assert text.count(candidate) == int(candidate == marker)
        assert any(m.get("content") == [{"type": "text", "text": f"PAD_USER_{index + 1}"}]
                   for m in bodies[index]["messages"])


async def test_active_memory_real_toolchain(
    mock_models, mock_provider, tmp_path, memory_stack, monkeypatch,
):
    service, factory = memory_stack
    permissions = PermissionService(factory)
    context = MemoryRunContext("r1", "c", "b", "msg")
    gateway = ToolGateway(permissions, tmp_path)
    gateway.memory_service = service
    gateway.memory_context = lambda: context
    original = service.save("PROJECT_ORIGINAL_UNIQUE", "c", "b")
    prompts, dispatched, results, observations = [], [], [], []

    async def answer(_window, title, detail):
        prompts.append((title, detail))
        return True

    monkeypatch.setattr("limbowave.app._ask_memory_user", answer)
    handler = _make_permission_handler(None, permissions, lambda: "c", gateway,
                                       memory_service=service, memory_context=lambda: context)

    async def dispatch(tool, params, conversation_id, confirmed):
        dispatched.append((tool, params, conversation_id))
        result = gateway.invoke(tool, params, conversation_id, user_confirmed=confirmed)
        results.append(result)
        return {"ok": result.ok, "data": result.data, "error": result.error,
                "notes": list(result.notes), "truncated": result.truncated}

    server = ToolIpcServer(dispatch, conversation_provider=lambda: "c")
    session = await server.start()
    adapter = PiKernelAdapter(build_spawn_spec(
        provider="mock", model_id="mock-model", cwd=tmp_path,
        env={IPC_ENV: session.to_env_value()},
    ))
    adapter.set_permission_handler(handler)
    adapter.set_observation_handler(observations.append)
    port, _ = mock_provider
    calls = [
        ("search_memory", {"query": "PROJECT"}),
        ("update_memory", {"memory_id": original.id, "updated_content": "PROJECT_UPDATED_UNIQUE"}),
        ("delete_session_memory", {"memory_id": original.id}),
        ("read_scratchpad", {}),
        ("write_scratchpad", {"content": "PAD_WRITE_UNIQUE", "expected_revision": 0}),
        ("read_scratchpad", {}),
    ]
    try:
        await adapter.start()
        for n, (tool, arguments) in enumerate(calls, 1):
            await adapter.set_memory_context(service.prompt_context(context, n))
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/control/toolcall", method="PUT",
                data=json.dumps({"name": tool, "id": f"call{n}", "arguments": arguments}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5):
                pass
            await adapter.send_message(f"MEMORY_REQUEST_{n}")
            await _wait_settled(adapter)
    finally:
        await adapter.shutdown()
        await server.stop()
    expected = []
    for n, (tool, _params) in enumerate(calls, 1):
        if tool in {"update_memory", "delete_session_memory"}:
            expected.append(("search_memory", f"call{n}"))  # Read-only scope preflight.
        expected.append((tool, f"call{n}"))
    assert len(dispatched) == len(expected)
    assert all(r.ok for r in results), [r.error for r in results]
    for (tool, params, conversation), (expected_tool, expected_call) in zip(
        dispatched, expected, strict=True,
    ):
        assert params["run_id"] == "r1" and params["call_id"] == expected_call
        assert tool == expected_tool and conversation == "c"
    assert len(prompts) == 2  # Updating and archiving always ask; context tools do not.
    assert "PROJECT_ORIGINAL_UNIQUE" in prompts[0][1]
    assert "PROJECT_UPDATED_UNIQUE" in prompts[0][1]
    assert results[0].data["results"][0]["id"] == original.id
    assert not service.list("c", "b")
    assert service.list_archived("c", "b")[0].content == "PROJECT_UPDATED_UNIQUE"
    assert service.read_scratchpad("c", "b").content == "PAD_WRITE_UNIQUE"
    assert results[-1].data["revision"] == 1
    tool_logs = [p for p in observations if p.get("kind") == "tool_call"]
    assert len(tool_logs) == len(calls)
    assert all(p["input"] == {"redacted": True} for p in tool_logs)


async def test_scratchpad_survives_compaction_without_duplicate_overlay(
    mock_models, mock_provider, tmp_path,
):
    settings_file = mock_models / ".pi" / "agent" / "settings.json"
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    settings["compaction"] = {"enabled": True, "reserveTokens": 100, "keepRecentTokens": 200}
    settings_file.write_text(json.dumps(settings), encoding="utf-8")
    _, log = mock_provider
    adapter = PiKernelAdapter(
        build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    )
    try:
        await adapter.start()
        for n in range(1, 4):
            await adapter.set_memory_context(_context(
                n, scratchpad={"content": "PAD_COMPACT_UNIQUE", "revision": 1},
            ))
            await adapter.send_message("before compact " + "history padding " * 300)
            await _wait_settled(adapter)
        await adapter.compact()
        await adapter.send_message("after compact")
        await _wait_settled(adapter)
    finally:
        await adapter.shutdown()
    bodies = [json.loads(line)["body"] for line in log.read_text(encoding="utf-8").splitlines()
              if "body" in json.loads(line)]
    assert json.dumps(bodies[-1]["messages"]).count("PAD_COMPACT_UNIQUE") == 1


async def test_global_delete_reports_scope_error_not_missing_permission(
    mock_models, mock_provider, tmp_path, memory_stack,
):
    service, factory = memory_stack
    item = service.save("GLOBAL_TARGET_DO_NOT_EDIT")
    context = MemoryRunContext("r1", "c", "b", "msg")
    gateway = ToolGateway(PermissionService(factory), tmp_path)
    gateway.memory_service = service
    gateway.memory_context = lambda: context
    gates, dispatched, events = [], [], []
    handler = _make_permission_handler(None, gateway=gateway, memory_service=service,
                                       memory_context=lambda: context)

    async def confirm(title, detail):
        gates.append(json.loads(detail)["tool"])
        return await handler(title, detail)

    async def dispatch(tool, params, conversation_id, confirmed):
        dispatched.append(tool)
        result = gateway.invoke(tool, params, conversation_id, user_confirmed=confirmed)
        return {"ok": result.ok, "data": result.data, "error": result.error}

    server = ToolIpcServer(dispatch, conversation_provider=lambda: "c")
    session = await server.start()
    adapter = PiKernelAdapter(build_spawn_spec(
        provider="mock", model_id="mock-model", cwd=tmp_path,
        env={IPC_ENV: session.to_env_value()},
    ))
    adapter.set_permission_handler(confirm)
    adapter.subscribe(events.append)
    port, _ = mock_provider
    try:
        await adapter.start()
        await adapter.set_memory_context(service.prompt_context(context, 1))
        for n, (tool, extra) in enumerate([
            ("delete_session_memory", {"archive": True}),
            ("delete_session_memory", {"archive": False}),
        ]):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/control/toolcall", method="PUT",
                data=json.dumps({"name": tool, "id": f"readonly{n}",
                                 "arguments": {"memory_id": item.id, **extra}}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5):
                pass
            await adapter.send_message(f"READ_ONLY_TEST_{n}")
            await _wait_settled(adapter)
    finally:
        await adapter.shutdown()
        await server.stop()
    assert gates == []  # Unsupported scope is not a user permission decision.
    assert dispatched == ["search_memory", "search_memory"]
    errors = [event for event in events if event.kind == "tool.end"]
    # The adapter's visible tool step must carry the actionable reason, not just a log.
    text = json.dumps([e.payload for e in errors], ensure_ascii=False)
    assert "全局记忆" in text and "未获授权" not in text
    assert service.list() == [item] and not service.list("c", "b")


@pytest.mark.parametrize("accept", [True, False])
@pytest.mark.parametrize("full_access", [True, False])
async def test_global_update_respects_preset_and_changes_only_global_original(
    mock_models, mock_provider, tmp_path, memory_stack, monkeypatch, accept, full_access,
):
    from limbowave.domain.permissions import PermissionPreset
    from tests.unit.test_memory_service import _second_conversation

    service, factory = memory_stack
    _second_conversation(factory)
    original = service.save("GLOBAL_ORIGINAL_TEST")
    service.save_session_global(original.id, "LOCAL_OVERRIDE_TEST", "c")
    permissions = PermissionService(factory)
    permissions.set_preset("c", PermissionPreset.FULL_ACCESS if full_access
                           else PermissionPreset.READ_ONLY)
    context = MemoryRunContext("global-run", "c", "b", "msg")
    gateway = ToolGateway(permissions, tmp_path)
    gateway.memory_service = service
    gateway.memory_context = lambda: context
    prompts, results, events = [], [], []

    async def answer(_window, title, detail):
        prompts.append((title, detail))
        assert service.list() == [original]  # A preflight is not an approval.
        return accept

    monkeypatch.setattr("limbowave.app._ask_memory_user", answer)
    handler = _make_permission_handler(None, permissions, lambda: "c", gateway,
                                       memory_service=service, memory_context=lambda: context)

    async def dispatch(tool, params, conversation_id, confirmed):
        result = gateway.invoke(tool, params, conversation_id, user_confirmed=confirmed)
        results.append((tool, result))
        return {"ok": result.ok, "data": result.data, "error": result.error,
                "notes": list(result.notes)}

    server = ToolIpcServer(dispatch, conversation_provider=lambda: "c")
    session = await server.start()
    adapter = PiKernelAdapter(build_spawn_spec(
        provider="mock", model_id="mock-model", cwd=tmp_path,
        env={IPC_ENV: session.to_env_value()},
    ))
    adapter.set_permission_handler(handler)
    adapter.subscribe(events.append)
    port, _ = mock_provider
    try:
        await adapter.start()
        await adapter.set_memory_context(service.prompt_context(context, 1))
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/control/toolcall", method="PUT",
            data=json.dumps({"name": "update_memory", "id": "global-call", "arguments": {
                "memory_id": original.id, "updated_content": "GLOBAL_UPDATED_TEST",
            }}).encode(), headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5):
            pass
        await adapter.send_message("GLOBAL_UPDATE_REQUEST")
        await _wait_settled(adapter)
    finally:
        await adapter.shutdown()
        await server.stop()
    expected = full_access or accept
    if full_access:
        assert prompts == []
        assert all(not audit.user_confirmed for audit in permissions.list_audit("c"))
    else:
        assert len(prompts) == 1 and prompts[0][0] == "更新全局记忆？"
        assert "GLOBAL_ORIGINAL_TEST" in prompts[0][1] and "GLOBAL_UPDATED_TEST" in prompts[0][1]
        assert "所有继承" in prompts[0][1] and "自定义覆盖" in prompts[0][1]
    assert service.list()[0].content == ("GLOBAL_UPDATED_TEST" if expected else original.content)
    assert service.effective_global("second")[0] == service.list()[0]
    assert service.effective_global("c")[0].content == "LOCAL_OVERRIDE_TEST"
    assert not service.list("c", "b")
    if expected:
        assert results[-1][0] == "update_memory" and results[-1][1].ok
        assert results[-1][1].data["scope"] == "global"
    else:
        assert [tool for tool, _result in results] == ["search_memory"]
        error = next(e for e in events if e.kind == "tool.end")
        assert "未执行" in json.dumps(error.payload, ensure_ascii=False)
