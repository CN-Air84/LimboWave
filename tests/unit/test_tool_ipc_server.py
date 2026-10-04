"""工具 IPC 通道验收（§九.1 的接线底座）。

锁住的不变量：
- **只绑回环**，端口由系统分配（不撞号、不对外）；
- **令牌认证**：错令牌/缺令牌一律拒绝并**断开**（不给探测者继续试的机会）；
- 认证失败**不区分原因**（不泄露"令牌对但工具错"这类信息）；
- 会话标识**以服务端为准**，扩展不能自报另一个会话（否则审计会串台）；
- 请求过大要拒绝，不能把内存吃满；
- 令牌随 `stop()` 作废（旧内核的连接不能继续用）；
- 派发异常不炸通道，转成结构化错误。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest

from limbowave.infrastructure.tools.ipc_server import (
    IPC_ENV,
    MAX_FRAME_BYTES,
    ToolIpcServer,
    parse_session,
)


class _Recorder:
    """假派发：记录调用并返回可预测的结果。"""

    def __init__(self, *, ok: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any], str, bool]] = []
        self.ok = ok
        self.raise_on: set[str] = set()

    async def __call__(
        self, tool: str, params: dict[str, Any], conversation_id: str, confirmed: bool
    ) -> dict[str, Any]:
        self.calls.append((tool, params, conversation_id, confirmed))
        if tool in self.raise_on:
            raise RuntimeError("派发内部炸了")
        return {"ok": self.ok, "data": {"tool": tool}, "error": None if self.ok else "拒绝"}


async def _connect(server: ToolIpcServer) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    session = server.session
    assert session is not None
    return await asyncio.open_connection(session.host, session.port)


async def _call(server: ToolIpcServer, payload: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await _connect(server)
    writer.write((json.dumps(payload) + "\n").encode("utf-8"))
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return json.loads(line.decode("utf-8"))


@pytest.fixture
async def server() -> Any:
    recorder = _Recorder()
    ipc = ToolIpcServer(recorder)
    await ipc.start()
    try:
        yield ipc, recorder
    finally:
        await ipc.stop()


# ---------- 绑定与端口 ----------


async def test_binds_loopback_only(server: Any) -> None:
    ipc, _ = server
    assert ipc.session is not None
    assert ipc.session.host == "127.0.0.1"  # **绝不** 0.0.0.0
    assert ipc.session.port > 0  # 系统分配
    assert ipc.session.token


async def test_token_is_long_and_random(server: Any) -> None:
    ipc, _ = server
    assert ipc.session is not None
    assert len(ipc.session.token) >= 32


async def test_stop_invalidates_token(server: Any) -> None:
    """内核关闭后旧令牌作废——旧连接不能继续调工具。"""
    ipc, _ = server
    session = ipc.session
    assert session is not None
    await ipc.stop()

    with pytest.raises(OSError):
        await asyncio.open_connection(session.host, session.port)


# ---------- 认证 ----------


async def test_valid_token_reaches_dispatch(server: Any) -> None:
    ipc, recorder = server
    assert ipc.session is not None
    response = await _call(
        ipc,
        {"token": ipc.session.token, "tool": "run_command", "params": {"command": "ls"}},
    )
    assert response["ok"] is True
    assert recorder.calls == [("run_command", {"command": "ls"}, "", False)]


async def test_wrong_token_rejected(server: Any) -> None:
    ipc, recorder = server
    response = await _call(ipc, {"token": "not-the-token", "tool": "run_command", "params": {}})
    assert response["ok"] is False
    assert response["error"] == "认证失败"
    assert recorder.calls == []  # 没有派发


async def test_missing_token_rejected(server: Any) -> None:
    ipc, recorder = server
    response = await _call(ipc, {"tool": "run_command", "params": {}})
    assert response["ok"] is False
    assert recorder.calls == []


async def test_auth_failure_does_not_leak_tool_validity(server: Any) -> None:
    """认证失败时**不区分**「工具有没有」——不给探测者额外信息。"""
    ipc, _ = server
    bad_tool = await _call(ipc, {"token": "x", "tool": "no_such_tool", "params": {}})
    good_tool = await _call(ipc, {"token": "x", "tool": "run_command", "params": {}})
    assert bad_tool["error"] == good_tool["error"] == "认证失败"


async def test_rejected_connection_is_closed(server: Any) -> None:
    """认证失败后断开：不给同一个连接反复试令牌的机会。"""
    ipc, _ = server
    session = ipc.session
    assert session is not None
    reader, writer = await asyncio.open_connection(session.host, session.port)
    writer.write((json.dumps({"token": "x", "tool": "t", "params": {}}) + "\n").encode())
    await writer.drain()
    await asyncio.wait_for(reader.readline(), timeout=5)
    # 服务端已关闭 → 下一次读得到 EOF
    writer.write((json.dumps({"token": "x", "tool": "t", "params": {}}) + "\n").encode())
    with contextlib.suppress(Exception):
        await writer.drain()
    rest = await asyncio.wait_for(reader.read(), timeout=5)
    assert rest == b""
    writer.close()


# ---------- 协议 ----------


async def test_malformed_json_rejected(server: Any) -> None:
    ipc, _ = server
    session = ipc.session
    assert session is not None
    reader, writer = await asyncio.open_connection(session.host, session.port)
    writer.write(b"{ not json\n")
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    assert json.loads(line.decode())["ok"] is False
    writer.close()


async def test_missing_tool_or_params_rejected(server: Any) -> None:
    ipc, recorder = server
    assert ipc.session is not None
    token = ipc.session.token
    assert (await _call(ipc, {"token": token, "params": {}}))["ok"] is False
    assert (await _call(ipc, {"token": token, "tool": "run_command"}))["ok"] is False
    assert recorder.calls == []


async def test_oversized_frame_rejected(server: Any) -> None:
    """超大请求要拒，不能把内存吃满。"""
    ipc, _ = server
    session = ipc.session
    assert session is not None
    reader, writer = await asyncio.open_connection(session.host, session.port)
    writer.write(b"x" * (MAX_FRAME_BYTES + 10) + b"\n")
    with contextlib.suppress(Exception):
        await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=10)
    assert json.loads(line.decode())["ok"] is False
    writer.close()


async def test_dispatch_exception_becomes_structured_error(server: Any) -> None:
    ipc, recorder = server
    recorder.raise_on.add("boom")
    assert ipc.session is not None
    response = await _call(ipc, {"token": ipc.session.token, "tool": "boom", "params": {}})
    assert response["ok"] is False
    assert "工具执行失败" in response["error"]
    assert ipc.stats.errors == 1


# ---------- 会话标识与确认标记 ----------


async def test_conversation_id_comes_from_server() -> None:
    """扩展自报的 conversation_id **不被采信**——只有服务端知道自己当前在哪个会话。"""
    recorder = _Recorder()
    ipc = ToolIpcServer(recorder, conversation_provider=lambda: "c-server")
    await ipc.start()
    try:
        assert ipc.session is not None
        await _call(
            ipc,
            {
                "token": ipc.session.token,
                "tool": "run_command",
                "params": {},
                "conversation_id": "attacker-supplied",
            },
        )
    finally:
        await ipc.stop()
    assert recorder.calls[0][2] == "c-server"


async def test_conversation_provider_reflects_switching() -> None:
    """用户在运行中切了会话 → 下一次调用必须落在新会话上（不能是启动时那个）。"""
    recorder = _Recorder()
    current = {"id": "c1"}
    ipc = ToolIpcServer(recorder, conversation_provider=lambda: current["id"])
    await ipc.start()
    try:
        assert ipc.session is not None
        token = ipc.session.token
        await _call(ipc, {"token": token, "tool": "read_url", "params": {}})
        current["id"] = "c2"  # 用户切会话
        await _call(ipc, {"token": token, "tool": "read_url", "params": {}})
    finally:
        await ipc.stop()
    assert [call[2] for call in recorder.calls] == ["c1", "c2"]


async def test_confirmed_flag_passes_through(server: Any) -> None:
    ipc, recorder = server
    assert ipc.session is not None
    await _call(
        ipc,
        {"token": ipc.session.token, "tool": "run_command", "params": {}, "confirmed": True},
    )
    assert recorder.calls[0][3] is True


async def test_stats_track_usage_without_params(server: Any) -> None:
    """统计只记工具名与次数——**不记参数内容**（参数可能含敏感文本）。"""
    ipc, _ = server
    assert ipc.session is not None
    await _call(
        ipc,
        {"token": ipc.session.token, "tool": "read_url", "params": {"url": "https://x/y"}},
    )
    assert ipc.stats.requests == 1
    assert ipc.stats.tools == {"read_url": 1}
    assert "https://x/y" not in json.dumps(ipc.stats.tools)


# ---------- 环境变量契约 ----------


def test_session_round_trips_through_env_value() -> None:
    from limbowave.infrastructure.tools.ipc_server import ToolIpcSession

    session = ToolIpcSession(host="127.0.0.1", port=54321, token="t" * 32)
    parsed = parse_session(session.to_env_value())
    assert parsed == session


def test_env_value_has_no_secrets_beyond_token() -> None:
    from limbowave.infrastructure.tools.ipc_server import ToolIpcSession

    payload = json.loads(ToolIpcSession(host="127.0.0.1", port=1, token="tok").to_env_value())
    assert set(payload) == {"host", "port", "token"}
    assert "conversation" not in json.dumps(payload)  # 会话标识不烘进通道


@pytest.mark.parametrize("value", [None, "", "not json", "[]", '{"host": 1}', '{"port": 1}'])
def test_parse_session_rejects_bad_input(value: str | None) -> None:
    assert parse_session(value) is None


def test_env_name_is_stable() -> None:
    """扩展侧按这个名字读——改名字要同步改 TS。"""
    assert IPC_ENV == "LIMBOWAVE_TOOL_IPC"
