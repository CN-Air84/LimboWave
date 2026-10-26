"""真实 Pi 模型工具调用 → 桌面联网权限档位 → IPC → 纯 JSON 环境数据。"""

import json
import urllib.request

import pytest

from limbowave.app import _make_permission_handler
from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.tool_gateway import ToolGateway
from limbowave.domain.conversation import Conversation
from limbowave.domain.permissions import Capability, PermissionPreset
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from limbowave.infrastructure.pi_adapter import PiKernelAdapter, build_spawn_spec
from limbowave.infrastructure.tools.ipc_server import IPC_ENV, ToolIpcServer
from tests.integration.test_pi_adapter import _wait_settled
from tests.integration.test_pi_adapter import mock_models as mock_models
from tests.unit.test_environment_awareness import COORDS, NOW, provider, service


@pytest.mark.parametrize(
    "preset,granted,expected",
    [
        (PermissionPreset.READ_ONLY, False, True),
        (PermissionPreset.CHAT_ONLY, True, False),
        (PermissionPreset.FULL_ACCESS, False, True),
        (PermissionPreset.CUSTOM, False, False),
        (PermissionPreset.CUSTOM, True, True),
    ],
)
async def test_actual_pi_environment_respects_network_preset(
    mock_models,
    mock_provider,
    tmp_path,
    monkeypatch,
    preset,
    granted,
    expected,
):
    port, _log = mock_provider
    backend, requests = provider()
    factory = in_memory_uow_factory(InMemoryStore())
    with factory() as uow:
        uow.conversations.add(Conversation("current", "当前会话", NOW))
        uow.commit()
    permissions = PermissionService(factory)
    permissions.set_preset("current", preset)
    if granted:
        permissions.grant("current", Capability.NETWORK, allowed_domains=("api.open-meteo.com",))
    gateway = ToolGateway(permissions, tmp_path, environment=service(backend))
    prompts, dispatched = [], []

    async def ask(window, request, reason, scope):
        prompts.append(request)
        raise AssertionError("环境工具应沿用已选权限档位，不擅自新增审批流程")

    monkeypatch.setattr("limbowave.app._ask_permission", ask)
    permission_handler = _make_permission_handler(None, permissions, lambda: "current", gateway)

    async def dispatch(tool, params, conversation_id, confirmed):
        result = gateway.invoke(tool, params, conversation_id, user_confirmed=confirmed)
        dispatched.append((tool, result))
        return {
            "ok": result.ok,
            "data": result.data,
            "error": result.error,
            "truncated": result.truncated,
            "notes": list(result.notes),
        }

    server = ToolIpcServer(dispatch, conversation_provider=lambda: "current")
    session = await server.start()
    adapter = PiKernelAdapter(
        build_spawn_spec(
            provider="mock",
            model_id="mock-model",
            cwd=tmp_path,
            env={IPC_ENV: session.to_env_value()},
        )
    )
    adapter.set_permission_handler(permission_handler)
    try:
        await adapter.start()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/control/toolcall",
            method="PUT",
            data=json.dumps(
                {"name": "get_environment", "id": "environment-1", "arguments": COORDS}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5):
            pass
        await adapter.send_message("确认这个已提供地点今天的天气与日落，不搜索网页")
        await _wait_settled(adapter)
    finally:
        await adapter.shutdown()
        await server.stop()
    assert not prompts
    assert len(requests) == (1 if expected else 0)
    assert len(dispatched) == (1 if expected else 0)
    if dispatched:
        tool, result = dispatched[0]
        assert tool == "get_environment" and result.ok
        assert result.data["provider"] == "open-meteo"
        assert result.data["current"]["temperature_c"] == 18.5
        assert result.data["time"]["timezone"] == "Asia/Shanghai"
