"""真实 Pi 子进程 + 本地 provider：记忆不污染原文，独立周期及工具桥接。"""

import json
import urllib.request

from limbowave.infrastructure.pi_adapter import PiKernelAdapter, build_spawn_spec
from limbowave.infrastructure.tools.ipc_server import IPC_ENV, ToolIpcServer
from tests.integration.test_pi_adapter import _wait_settled
from tests.integration.test_pi_adapter import mock_models as mock_models


def _context(n, **changes):
    return {
        "run_id": f"r{n}",
        "branch_id": "b",
        "round": n,
        "global": ["GLOBAL_MEMORY_TEST"],
        "session": ["BRANCH_MEMORY_TEST"],
        "global_interval": 2,
        "session_interval": 3,
        **changes,
    }


async def test_actual_pi_memory_context_intervals(mock_models, mock_provider, tmp_path):
    _, log = mock_provider
    adapter = PiKernelAdapter(
        build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    )
    try:
        await adapter.start()
        for n in range(1, 5):
            await adapter.set_memory_context(_context(n))
            await adapter.send_message(f"USER_TURN_{n}")
            await _wait_settled(adapter)
        await adapter.set_memory_context(_context(5, **{"global": [], "session": ["NEW_BRANCH"]}))
        await adapter.send_message("USER_TURN_5")
        await _wait_settled(adapter)
    finally:
        await adapter.shutdown()
    requests = [
        json.loads(line)["body"]
        for line in log.read_text(encoding="utf-8").splitlines()
        if "body" in json.loads(line)
    ]
    assert len(requests) == 5
    for index, body in enumerate(requests[:4]):
        text = json.dumps(body["messages"], ensure_ascii=False)
        assert text.count("GLOBAL_MEMORY_TEST") == 1
        assert text.count("BRANCH_MEMORY_TEST") == 1
        assert any(
            m.get("content") == [{"type": "text", "text": f"USER_TURN_{index + 1}"}]
            for m in body["messages"]
        )
    third = json.dumps(requests[2]["messages"])
    assert third.index("BRANCH_MEMORY_TEST") < third.index("USER_TURN_2")
    assert third.index("GLOBAL_MEMORY_TEST") > third.index("USER_TURN_3")
    fourth = json.dumps(requests[3]["messages"])
    assert fourth.index("GLOBAL_MEMORY_TEST") < fourth.index("USER_TURN_4")
    assert fourth.index("BRANCH_MEMORY_TEST") > fourth.index("USER_TURN_4")
    last = json.dumps(requests[4]["messages"])
    assert "GLOBAL_MEMORY_TEST" not in last and "BRANCH_MEMORY_TEST" not in last
    assert "NEW_BRANCH" in last


async def test_actual_pi_memory_tool_binding(mock_models, mock_provider, tmp_path):
    port, _log = mock_provider
    dispatched, gates = [], []

    async def dispatch(tool, params, conversation_id, confirmed):
        dispatched.append((tool, params, conversation_id))
        return {"ok": True, "data": {"saved": True}}

    async def confirm(title, detail):
        gates.append(json.loads(detail))
        return True

    server = ToolIpcServer(dispatch, conversation_provider=lambda: "c")
    session = await server.start()
    spec = build_spawn_spec(
        provider="mock", model_id="mock-model", cwd=tmp_path, env={IPC_ENV: session.to_env_value()}
    )
    adapter = PiKernelAdapter(spec)
    adapter.set_permission_handler(confirm)
    try:
        await adapter.start()
        await adapter.set_memory_context(_context(1))
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/control/toolcall",
            method="PUT",
            data=json.dumps(
                {
                    "name": "add_session_memory",
                    "id": "memory_call",
                    "arguments": {"content": "remember this"},
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
        await adapter.send_message("remember")
        await _wait_settled(adapter)
    finally:
        await adapter.shutdown()
        await server.stop()
    assert gates[0]["run_id"] == "r1"
    assert gates[0]["call_id"] == "memory_call"
    assert dispatched == [
        (
            "add_session_memory",
            {"content": "remember this", "call_id": "memory_call", "run_id": "r1"},
            "c",
        )
    ]


async def test_memory_branch_switch_and_compaction(mock_models, mock_provider, tmp_path):
    _, log = mock_provider
    settings_file = mock_models / ".pi" / "agent" / "settings.json"
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    settings["compaction"] = {"enabled": True, "reserveTokens": 100, "keepRecentTokens": 200}
    settings_file.write_text(json.dumps(settings), encoding="utf-8")
    adapter = PiKernelAdapter(
        build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    )
    try:
        await adapter.start()
        for n in range(1, 4):
            await adapter.set_memory_context(_context(n))
            await adapter.send_message("before compact " + "history padding " * 300)
            await _wait_settled(adapter)
        await adapter.compact()
        await adapter.set_memory_context(_context(2, branch_id="child", session=["CHILD_ONLY"]))
        await adapter.send_message("after compact")
        await _wait_settled(adapter)
    finally:
        await adapter.shutdown()
    requests = [
        json.loads(line)["body"]
        for line in log.read_text(encoding="utf-8").splitlines()
        if "body" in json.loads(line)
    ]
    last = json.dumps(requests[-1]["messages"])
    assert "CHILD_ONLY" in last
    assert "GLOBAL_MEMORY_TEST" in last
    assert "BRANCH_MEMORY_TEST" not in last
