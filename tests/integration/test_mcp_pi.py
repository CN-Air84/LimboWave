"""Real Pi + local model fixture + authenticated gateway + real stdio MCP."""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from limbowave.app import _make_permission_handler
from limbowave.application.services.mcp_service import McpService
from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.tool_gateway import ToolGateway
from limbowave.domain.conversation import Conversation
from limbowave.domain.mcp import McpConfiguration, McpServer
from limbowave.domain.permissions import PermissionPreset
from limbowave.infrastructure.mcp_client import perform
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory
from limbowave.infrastructure.pi_adapter import PiKernelAdapter, build_spawn_spec
from limbowave.infrastructure.tools.ipc_server import IPC_ENV, ToolIpcServer
from tests.integration.test_memory_pi import _context
from tests.integration.test_pi_adapter import _wait_settled
from tests.integration.test_pi_adapter import mock_models as mock_models


@pytest.mark.parametrize("preset", [PermissionPreset.FULL_ACCESS, PermissionPreset.CUSTOM])
async def test_real_pi_calls_mcp_with_run_identity(
    mock_models, mock_provider, tmp_path, monkeypatch, preset,
):
    factory = in_memory_uow_factory()
    with factory() as uow:
        uow.conversations.add(Conversation(id="c", title="MCP", created_at=datetime.now(UTC)))
        uow.commit()
    permissions = PermissionService(factory)
    permissions.set_preset("c", preset)
    service = McpService(tmp_path / "mcp.json", operation=perform)
    service.save(McpConfiguration(servers=[McpServer(
        id="test", name="Test", enabled=True, command=sys.executable,
        args=[str(Path(__file__).parents[1] / "fixtures" / "mcp_server.py")],
    )]))
    gateway = ToolGateway(permissions, tmp_path, mcp=service)
    prompt = AsyncMock(return_value="once")
    monkeypatch.setattr("limbowave.app._ask_permission", prompt)
    handler = _make_permission_handler(None, permissions, lambda: "c", gateway)
    results, received = [], []
    async def dispatch(tool, params, conversation_id, confirmed):
        received.append((tool, dict(params), conversation_id))
        assert params.pop("run_id") == "r1"
        result = await asyncio.to_thread(gateway.invoke, tool, params, conversation_id,
                                         user_confirmed=confirmed)
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
    port, _ = mock_provider
    try:
        await adapter.start()
        await adapter.set_memory_context(_context(1))
        for tool, params in [
            ("list_mcp_tools", {"server_id": "test"}),
            ("call_mcp_tool", {"server_id": "test", "tool": "echo",
                               "arguments": {"text": "MCP_PI_ROUNDTRIP"}}),
        ]:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/control/toolcall", method="PUT",
                data=json.dumps({"name": tool, "id": "mcp_" + tool,
                                 "arguments": params}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5):
                pass
            await adapter.send_message("Please test " + tool)
            await _wait_settled(adapter)
    finally:
        await adapter.shutdown()
        await server.stop()
    assert len(results) == 2 and all(result.ok for result in results)
    assert results[1].data["content"][0]["text"] == "MCP_PI_ROUNDTRIP"
    assert all(params["run_id"] == "r1" for _, params, _ in received)
    assert prompt.await_count == (2 if preset is PermissionPreset.CUSTOM else 0)
