"""Real Pi + local provider: compaction must not invalidate attachment references."""

import json
import shutil
import urllib.request

import pytest

from limbowave.application.services.file_service import FileService
from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.tool_gateway import ToolGateway
from limbowave.domain.permissions import Capability
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from limbowave.infrastructure.pi_adapter import PiKernelAdapter, build_spawn_spec
from limbowave.infrastructure.tools.ipc_server import IPC_ENV, ToolIpcServer
from tests.integration.test_pi_adapter import _wait_settled
from tests.integration.test_pi_adapter import mock_models as mock_models

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")


def context(file_id="file_PERSISTENT_ATTACHMENT", **changes):
    return {
        "run_id": "attachment_run", "branch_id": "branch_a",
        "documents": [{"file_id": file_id, "name": "REFERENCE.md",
                       "path": None, "line_count": 59000}],
        **changes,
    }


def requests(log):
    return [row["body"] for line in log.read_text(encoding="utf-8").splitlines()
            if "body" in (row := json.loads(line))]


def next_tool(port, name, arguments):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/control/toolcall", method="PUT",
        data=json.dumps({"name": name, "id": "attachment_call", "arguments": arguments}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5):
        pass


async def test_compaction_keeps_reference_and_real_document_read_without_reupload(
    mock_models, mock_provider, tmp_path,
):
    port, log = mock_provider
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    document_path = tmp_path / "outside-workspace.md"
    document_path.write_text(
        "\n".join(f"line {n}" for n in range(1, 59001)), encoding="utf-8"
    )
    factory = in_memory_uow_factory(InMemoryStore())
    files = FileService(factory)
    document = files.index_path(document_path)
    permissions = PermissionService(factory)
    permissions.grant("c", Capability.FILE_READ, allowed_paths=(str(workspace),))
    gateway = ToolGateway(permissions, workspace, documents=files)
    dispatched, gates = [], []

    async def dispatch(tool, params, conversation_id, confirmed):
        result = gateway.invoke(tool, params, conversation_id, user_confirmed=confirmed)
        dispatched.append((tool, result))
        return {"ok": result.ok, "data": result.data, "error": result.error}

    async def confirm(title, detail):
        gate = json.loads(detail)
        allowed = permissions.is_allowed(gateway.prepare_request(gate["tool"], gate["input"]), "c")
        gates.append((gate["tool"], allowed))
        return allowed

    settings_file = mock_models / ".pi" / "agent" / "settings.json"
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    settings["compaction"] = {"enabled": True, "reserveTokens": 100, "keepRecentTokens": 200}
    settings_file.write_text(json.dumps(settings), encoding="utf-8")
    server = ToolIpcServer(dispatch, conversation_provider=lambda: "c")
    session = await server.start()
    adapter = PiKernelAdapter(build_spawn_spec(
        provider="mock", model_id="mock-model", cwd=workspace,
        env={IPC_ENV: session.to_env_value()},
    ))
    adapter.set_permission_handler(confirm)
    try:
        await adapter.start()
        await adapter.set_attachment_context(context(document.id))
        for n in range(3):
            await adapter.send_message(f"before compact {n} " + "history padding " * 300)
            await _wait_settled(adapter)
        entries = await adapter.get_entries()
        assert document.id not in json.dumps(entries)  # overlay is not persisted as user text
        await adapter.compact()
        assert any(e.get("type") == "compaction" for e in await adapter.get_entries())
        # Intentionally no rebind: same-run compact/retry must retain the installed context.
        next_tool(port, "read_document", {
            "file_id": document.id, "start_line": 58001, "end_line": 58200,
        })
        request_count = len(requests(log))
        await adapter.send_message("continue from line 58001")
        await _wait_settled(adapter)
        after_compact = requests(log)[request_count:]
        assert len(after_compact) >= 2  # tool request + follow-up request
        for body in after_compact:
            text = json.dumps(body["messages"], ensure_ascii=False)
            assert text.count("当前分支持久附件清单") == 1
            assert document.id in text
        assert dispatched[0][0] == "read_document"
        assert dispatched[0][1].ok
        assert dispatched[0][1].data["text"].splitlines() == [
            f"line {n}" for n in range(58001, 58201)
        ]
        assert dispatched[0][1].data["actual_range"] == [58001, 58200]
        next_tool(port, "run_command", {"command": "Get-ChildItem | Select-Object Name, Length"})
        await adapter.send_message("probe denied shell")
        await _wait_settled(adapter)
        assert ("run_command", False) in gates
        assert [tool for tool, _ in dispatched] == ["read_document"]
    finally:
        await adapter.shutdown()
        await server.stop()


async def test_manifest_replacement_empty_reset_and_control_auth(
    mock_models, mock_provider, tmp_path,
):
    _, log = mock_provider
    adapter = PiKernelAdapter(
        build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    )
    try:
        await adapter.start()
        await adapter.set_attachment_context(context())
        # Invalid control token must neither wipe the manifest nor appear as a user message.
        await adapter._rpc.request({
            "type": "prompt", "message": "/limbowave-attachments " + json.dumps({
                "token": "invalid", "context": context(documents=[]),
            }),
        })
        await adapter._rpc.request({
            "type": "prompt", "message": "/limbowave-attachments " + json.dumps({
                "token": adapter._control_token,
                "context": context(documents=[{"file_id": "malformed"}]),
            }),
        })
        await adapter.send_message("first")
        await _wait_settled(adapter)
        assert "file_PERSISTENT_ATTACHMENT" in json.dumps(requests(log)[-1])
        await adapter.set_attachment_context(context("file_CHILD", branch_id="branch_child"))
        await adapter.send_message("child")
        await _wait_settled(adapter)
        body = json.dumps(requests(log)[-1])
        assert "file_CHILD" in body and "file_PERSISTENT_ATTACHMENT" not in body
        await adapter.set_attachment_context(context(documents=[]))
        await adapter.send_message("empty")
        await _wait_settled(adapter)
        assert "当前分支持久附件清单" not in json.dumps(requests(log)[-1], ensure_ascii=False)
        await adapter.set_attachment_context(context())
        await adapter.new_session()
        await adapter.send_message("new session")
        await _wait_settled(adapter)
        assert "file_PERSISTENT_ATTACHMENT" not in json.dumps(requests(log)[-1])
    finally:
        await adapter.shutdown()
