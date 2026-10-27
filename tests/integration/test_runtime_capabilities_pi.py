"""CAP-01: real Pi -> authenticated IPC -> gateway -> actual local provider body."""

import json
import shutil
from datetime import UTC, datetime

import pytest

from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.reminder_service import ReminderService
from limbowave.application.services.tool_gateway import NetworkTools, ToolGateway
from limbowave.domain.conversation import Conversation
from limbowave.domain.reminder import ReminderContext
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory
from limbowave.infrastructure.pi_adapter import PiKernelAdapter, build_spawn_spec
from limbowave.infrastructure.tools.ipc_server import IPC_ENV, ToolIpcServer
from tests.integration.test_attachment_context_pi import next_tool, requests
from tests.integration.test_pi_adapter import _wait_settled
from tests.integration.test_pi_adapter import mock_models as mock_models

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="requires node")


async def test_actual_requests_refresh_on_each_tool_turn_and_clear_on_session_switch(
    mock_models,
    mock_provider,
    tmp_path,
):
    port, log = mock_provider
    factory = in_memory_uow_factory()
    with factory() as uow:
        uow.conversations.add(Conversation("c", "test", datetime.now(UTC)))
        uow.commit()
    gateway = ToolGateway(PermissionService(factory), tmp_path)
    gateway.reminder_service = ReminderService(factory)
    gateway.reminder_context = lambda: ReminderContext("c", "b", "m")
    current_run = "r1"
    calls = []

    async def snapshot(conversation_id, run_id):
        assert conversation_id == "c" and run_id == current_run
        calls.append(run_id)
        return {**gateway.runtime_capabilities(conversation_id), "run_id": run_id}

    async def dispatch(tool, params, conversation_id, confirmed):
        assert tool == "get_current_datetime"
        result = gateway.invoke(tool, params, conversation_id, user_confirmed=confirmed)
        gateway._network = None  # Configuration changes between tool and continuation.
        return {"ok": result.ok, "data": result.data, "error": result.error}

    async def confirm(_title, _detail):
        return True

    server = ToolIpcServer(
        dispatch, conversation_provider=lambda: "c", capability_snapshot=snapshot
    )
    session = await server.start()
    adapter = PiKernelAdapter(
        build_spawn_spec(
            provider="mock",
            model_id="mock-model",
            cwd=tmp_path,
            env={IPC_ENV: session.to_env_value()},
        )
    )
    adapter.set_permission_handler(confirm)
    try:
        await adapter.start()
        await adapter.set_attachment_context(
            {"run_id": current_run, "branch_id": "b", "documents": []}
        )
        await adapter.send_message("first request")
        await _wait_settled(adapter)
        text = json.dumps(requests(log)[-1]["messages"], ensure_ascii=False)
        assert text.count("运行时能力快照 CAP-01") == 1
        assert "web_search: unconfigured" in text
        assert "reminders: active(local_only)" in text

        current_run = "r2"
        gateway._network = NetworkTools(searcher=lambda _: pytest.fail("not a real search"))
        await adapter.set_attachment_context(
            {"run_id": current_run, "branch_id": "b", "documents": []}
        )
        next_tool(port, "get_current_datetime", {})
        count = len(requests(log))
        await adapter.send_message("read clock")
        await _wait_settled(adapter)
        bodies = requests(log)[count:]
        assert len(bodies) == 2
        texts = [json.dumps(body["messages"], ensure_ascii=False) for body in bodies]
        assert all(text.count("运行时能力快照 CAP-01") == 1 for text in texts)
        assert "web_search: active" in texts[0]
        assert "web_search: unconfigured" in texts[1]
        assert calls == ["r1", "r2", "r2"]
        assert "CAP-01" not in json.dumps(await adapter.get_entries(), ensure_ascii=False)

        await adapter.new_session()
        await adapter.send_message("unbound session")
        await _wait_settled(adapter)
        text = json.dumps(requests(log)[-1]["messages"], ensure_ascii=False)
        assert "web_search: unknown" in text
        assert calls == ["r1", "r2", "r2"]
    finally:
        await adapter.shutdown()
        await server.stop()
