from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from limbowave.application.services.mcp_service import McpService
from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.run_origin import remote_origin
from limbowave.application.services.tool_gateway import ToolGateway
from limbowave.domain.conversation import Conversation
from limbowave.domain.mcp import McpConfiguration, McpServer
from limbowave.domain.permissions import Capability, Decision, PermissionPreset, evaluate
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory


@pytest.fixture
def stack(tmp_path):
    factory = in_memory_uow_factory()
    with factory() as uow:
        uow.conversations.add(Conversation(id="c", title="MCP", created_at=datetime.now(UTC)))
        uow.commit()
    permissions = PermissionService(factory)
    permissions.set_preset("c", PermissionPreset.FULL_ACCESS)
    operation = AsyncMock(return_value={"content": [{"type": "text", "text": "result"}],
                                       "isError": False})
    service = McpService(tmp_path / "mcp.json", operation=operation)
    service.save(McpConfiguration(servers=[McpServer(
        id="local", name="Local", command="python", enabled=True,
    )]))
    gateway = ToolGateway(permissions, tmp_path, mcp=service)
    return gateway, permissions, service, operation


PARAMS = {"server_id": "local", "tool": "echo", "arguments": {"text": "hello"}}


def test_custom_requires_per_call_confirmation(stack):
    gateway, permissions, _, operation = stack
    permissions.set_preset("c", PermissionPreset.CUSTOM)
    request = gateway.prepare_request("call_mcp_tool", PARAMS)
    assert evaluate(request, []).decision is Decision.CONFIRM
    assert permissions.authorize(request, "c").decision is Decision.CONFIRM
    denied = gateway.invoke("call_mcp_tool", PARAMS, "c")
    assert not denied.ok and denied.data["needs_confirmation"]
    operation.assert_not_called()
    result = gateway.invoke("call_mcp_tool", PARAMS, "c", user_confirmed=True)
    assert result.ok and "不可信" in result.notes[0]
    operation.assert_awaited_once()
    assert not gateway.invoke("call_mcp_tool", PARAMS, "c").ok


@pytest.mark.parametrize("preset", [PermissionPreset.CHAT_ONLY, PermissionPreset.READ_ONLY])
def test_restrictive_preset_cannot_be_bypassed_with_confirmation(stack, preset):
    gateway, permissions, _, operation = stack
    permissions.set_preset("c", preset)
    assert not gateway.invoke("call_mcp_tool", PARAMS, "c", user_confirmed=True).ok
    operation.assert_not_called()
    assert gateway.runtime_capabilities("c")["capabilities"]["mcp"]["status"] == "restricted"


def test_custom_per_call_and_web_hard_boundary(stack):
    gateway, permissions, _, operation = stack
    permissions.set_preset("c", PermissionPreset.CUSTOM)
    permissions.grant("c", Capability.MCP)
    assert not gateway.invoke("call_mcp_tool", PARAMS, "c").ok
    with remote_origin():
        assert not gateway.invoke("call_mcp_tool", PARAMS, "c", user_confirmed=True).ok
        assert not gateway.invoke("list_mcp_tools", {}, "c", user_confirmed=True).ok
    operation.assert_not_called()


def test_inventory_does_not_start_servers(stack):
    gateway, _, _, operation = stack
    result = gateway.invoke("list_mcp_tools", {}, "c", user_confirmed=True)
    assert result.ok and result.data["servers"][0]["id"] == "local"
    operation.assert_not_called()
    assert gateway.runtime_capabilities("c")["capabilities"]["mcp"] == {
        "status": "active", "permission": "allowed"}


def test_discovery_pagination_and_result_error(stack):
    gateway, _, _, operation = stack
    operation.return_value = {"tools": [{"name": f"tool{i}"} for i in range(7)]}
    result = gateway.invoke("list_mcp_tools", {"server_id": "local", "offset": 2, "limit": 3},
                            "c", user_confirmed=True)
    assert result.ok and result.data["total"] == 7 and result.data["next_offset"] == 5
    assert [t["name"] for t in result.data["tools"]] == ["tool2", "tool3", "tool4"]
    operation.return_value = {"isError": True, "content": [{"type": "text", "text": "failed"}]}
    result = gateway.invoke("call_mcp_tool", PARAMS, "c", user_confirmed=True)
    assert not result.ok and result.data["isError"]


def test_output_limit_and_disabled_servers_fail_closed(stack):
    gateway, _, service, operation = stack
    operation.return_value = {"content": [{"type": "text", "text": "x" * 70000}]}
    result = gateway.invoke("call_mcp_tool", PARAMS, "c", user_confirmed=True)
    assert not result.ok and result.truncated and result.data["code"] == "output_limit"
    operation.reset_mock()
    service.save(McpConfiguration())
    assert not gateway.invoke("call_mcp_tool", PARAMS, "c", user_confirmed=True).ok
    operation.assert_not_called()


def test_outbound_and_inbound_privacy_checks(stack):
    gateway, permissions, _, operation = stack
    permissions.set_preset("c", PermissionPreset.CUSTOM)
    policy = SimpleNamespace(blocks_structure=lambda value, **kw: "secret" in str(value))
    gateway.memory_service = SimpleNamespace(privacy=SimpleNamespace(policy=lambda _: policy))
    result = gateway.invoke("call_mcp_tool", {**PARAMS, "arguments": {"text": "secret"}},
                            "c", user_confirmed=True)
    assert not result.ok
    operation.assert_not_called()
    operation.return_value = {"content": [{"type": "text", "text": "secret"}]}
    result = gateway.invoke("call_mcp_tool", PARAMS, "c", user_confirmed=True)
    assert not result.ok and "secret" not in str(result)


@pytest.mark.parametrize("params", [{"offset": True}, {"limit": 0}, {"offset": -1}])
def test_invalid_discovery_parameters(stack, params):
    gateway, _, _, operation = stack
    assert not gateway.invoke("list_mcp_tools", params, "c", user_confirmed=True).ok
    operation.assert_not_called()


def test_full_access_automatically_allows_mcp(stack):
    gateway, _, _, operation = stack
    assert gateway.invoke("call_mcp_tool", PARAMS, "c").ok
    operation.assert_awaited_once()


async def test_desktop_confirmation_custom_and_full_access(stack, monkeypatch):
    from limbowave.app import _make_permission_handler

    gateway, permissions, _, operation = stack
    prompt = AsyncMock(return_value="once")
    monkeypatch.setattr("limbowave.app._ask_permission", prompt)
    callback = _make_permission_handler(None, permissions, lambda: "c", gateway)
    import json
    detail = json.dumps({"tool": "call_mcp_tool", "input": PARAMS})
    assert await callback("limbowave.gate:call_mcp_tool", detail)
    prompt.assert_not_called()
    permissions.set_preset("c", PermissionPreset.CUSTOM)
    assert await callback("limbowave.gate:call_mcp_tool", detail)
    prompt.assert_awaited_once()
    prompt.return_value = "deny"
    assert not await callback("limbowave.gate:call_mcp_tool", detail)
    operation.assert_not_called()
