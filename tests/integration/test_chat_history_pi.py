"""真实 Pi 模型调用 -> 桌面确认 -> IPC -> 权威历史查询。"""

import json
import urllib.request

import pytest

from limbowave.app import _make_permission_handler
from limbowave.domain.permissions import PermissionPreset
from limbowave.infrastructure.pi_adapter import PiKernelAdapter, build_spawn_spec
from limbowave.infrastructure.tools.ipc_server import IPC_ENV, ToolIpcServer
from limbowave.ui.permission_prompt import DENY, ONCE
from tests.integration.test_pi_adapter import _wait_settled
from tests.integration.test_pi_adapter import mock_models as mock_models
from tests.unit.test_chat_history import history_stack as history_stack


@pytest.mark.parametrize(
    "preset,answer",
    [
        (PermissionPreset.READ_ONLY, ONCE),
        (PermissionPreset.READ_ONLY, DENY),
        (PermissionPreset.CHAT_ONLY, ONCE),
        (PermissionPreset.FULL_ACCESS, DENY),
    ],
)
@pytest.mark.parametrize("history_params", [
    {"query": "Docker", "conversation_id": "other"},
    {"message_id": "m2", "conversation_id": "other", "before_turns": 2, "after_turns": 3},
])
async def test_actual_pi_history_query_respects_approval(
    mock_models, mock_provider, history_stack, tmp_path, monkeypatch, preset, answer, history_params
):
    port, _log = mock_provider
    _, permissions, gateway, _ = history_stack
    permissions.set_preset("current", preset)
    prompts, dispatched = [], []

    async def ask(window, request, reason, scope):
        prompts.append((request, scope))
        return answer

    monkeypatch.setattr("limbowave.app._ask_permission", ask)
    permission_handler = _make_permission_handler(None, permissions, lambda: "current", gateway)

    async def dispatch(tool, params, conversation_id, confirmed):
        result = gateway.invoke(tool, params, conversation_id, user_confirmed=confirmed)
        dispatched.append((tool, conversation_id, result))
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
        for turn in range(2):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/control/toolcall",
                method="PUT",
                data=json.dumps(
                    {
                        "name": "query_chat_history",
                        "id": f"history-{turn}",
                        "arguments": history_params,
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
            await adapter.send_message(f"查一下以前的部署说明 {turn}")
            await _wait_settled(adapter)
    finally:
        await adapter.shutdown()
        await server.stop()
    expected = preset is PermissionPreset.FULL_ACCESS or answer == ONCE
    assert len(prompts) == (0 if preset is PermissionPreset.FULL_ACCESS else 2)
    assert all(scope is None for _, scope in prompts)
    assert len(dispatched) == (2 if expected else 0)
    for tool, caller, result in dispatched:
        assert tool == "query_chat_history" and caller == "current"
        assert result.ok
        assert result.data["results"][0]["message_id"] == "m2"
        assert "PRIVATE_THINKING" not in json.dumps(result.data)

        if "message_id" in history_params:
            assert result.data["mode"] == "context"
            assert result.data["anchor_message_id"] == "m2"
            assert result.data["requested_after_turns"] == 3
