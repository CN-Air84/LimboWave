"""工具通道的真实纵向验证（T7.1 / §九.1）。

三段证据缺一不可：

1. **跨语言**：用**真 Node 客户端**（与扩展同一套 socket 代码）打 Python 服务端，
   验证 JSONL 协议在两侧对得上——这是最容易出问题、也最难靠单侧测试发现的地方；
2. **工具注册被 Pi 接受**：真起内核，读扩展回报的 `tools.registered`；
   扩展加载失败时模型会静默地没有工具可用，必须有证据；
3. **全链路（到执行器）**：真 ToolGateway + 真 PowerShell 执行器经通道执行命令，
   返回结构化结果（stdout/退出码）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.tool_gateway import TerminalTools, ToolGateway
from limbowave.domain.permissions import Capability
from limbowave.infrastructure.crypto.vault import KdfParams, Vault, VaultKey
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.shell import create_executor, probe_shell
from limbowave.infrastructure.tools.ipc_server import IPC_ENV, ToolIpcServer

T0 = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
NODE = shutil.which("node")


@pytest.fixture
def stack(tmp_path: Path) -> tuple[ToolIpcServer, ToolGateway]:
    """真网关 + 真执行器，但不经内核（内核另测）。"""
    key: VaultKey = Vault(
        tmp_path / "v.json", params=KdfParams(time_cost=1, memory_cost=8, parallelism=1)
    ).create("p")
    factory = sqlite_uow_factory(tmp_path / "t.db", key)
    # permission_grants 有外键到 conversations：先把会话建出来
    from limbowave.domain.conversation import Conversation

    with factory() as uow:
        uow.conversations.add(Conversation(id="c1", title="t", created_at=T0))
        uow.commit()
    permissions = PermissionService(factory)
    permissions.grant("c1", Capability.TERMINAL)
    permissions.grant("c1", Capability.FILE_READ, allowed_paths=(str(tmp_path),))
    shell = probe_shell(tmp_path)
    terminal = TerminalTools(create_executor(shell)) if shell else None
    # 附件文档服务（read_document 的执行后端）：登记一份 3 行文档
    from limbowave.application.services.file_service import FileService

    files = FileService(factory)
    doc_path = tmp_path / "attachment.md"
    doc_path.write_text("附件行一\n附件行二\n附件行三\n", encoding="utf-8")
    document = files.index_path(doc_path)
    gateway = ToolGateway(permissions, tmp_path, terminal=terminal, documents=files)

    async def dispatch(tool: str, params: dict, conversation_id: str, confirmed: bool) -> dict:
        result = gateway.invoke(tool, params, conversation_id, user_confirmed=confirmed)
        return {
            "ok": result.ok,
            "data": result.data,
            "error": result.error,
            "truncated": result.truncated,
            "notes": list(result.notes),
        }

    server = ToolIpcServer(dispatch, conversation_provider=lambda: "c1")
    return server, gateway, document.id


async def test_python_client_round_trip(stack) -> None:
    """先证 Python 侧自洽（真执行器跑一条无害命令）。"""
    server, _, _ = stack
    await server.start()
    try:
        assert server.session is not None
        reader, writer = await asyncio.open_connection(server.session.host, server.session.port)
        writer.write(
            (
                json.dumps(
                    {
                        "token": server.session.token,
                        "tool": "run_command",
                        "params": {"command": "echo ipc-ok"},
                        "confirmed": True,
                    }
                )
                + "\n"
            ).encode()
        )
        await writer.drain()
        response = json.loads((await asyncio.wait_for(reader.readline(), timeout=30)).decode())
        writer.close()
        assert response["ok"] is True, response.get("error")
        assert "ipc-ok" in response["data"]["stdout"]
        assert response["data"]["exit_code"] == 0
    finally:
        await server.stop()


@pytest.mark.skipif(NODE is None, reason="需要 node")
async def test_node_client_matches_python_server(stack, tmp_path: Path) -> None:
    """**跨语言**：真 Node 客户端打 Python 服务端——协议两侧必须对得上。

    这段代码刻意与扩展里的 `callAppTool` 用同一套写法（JSONL + 回环 TCP + 令牌），
    因此协议漂移会被这里抓住。
    """
    server, _, _ = stack
    session = await server.start()
    script = tmp_path / "probe.mjs"
    script.write_text(
        """
import net from "node:net";
const session = JSON.parse(process.argv[2]);
const request = {
  token: session.token,
  tool: "run_command",
  params: { command: "echo node-ok" },
  confirmed: true,
};
const socket = net.createConnection({ host: session.host, port: session.port });
let buffer = "";
socket.setTimeout(30000, () => { console.error("timeout"); process.exit(3); });
socket.on("connect", () => socket.write(JSON.stringify(request) + "\\n"));
socket.on("data", (chunk) => {
  buffer += chunk.toString("utf8");
  const index = buffer.indexOf("\\n");
  if (index < 0) return;
  console.log(buffer.slice(0, index));
  socket.destroy();
  process.exit(0);
});
socket.on("error", (error) => { console.error(error.message); process.exit(1); });
""",
        encoding="utf-8",
    )
    payload = json.dumps({"host": session.host, "port": session.port, "token": session.token})
    try:
        completed = await asyncio.create_subprocess_exec(
            NODE,
            str(script),
            payload,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(completed.communicate(), timeout=90)
    finally:
        await server.stop()

    assert completed.returncode == 0, stderr.decode("utf-8", "replace")
    response = json.loads(stdout.decode("utf-8").strip().splitlines()[-1])
    assert response["ok"] is True, response.get("error")
    assert "node-ok" in response["data"]["stdout"]


@pytest.mark.skipif(NODE is None, reason="需要 node")
async def test_node_client_rejected_with_wrong_token(stack, tmp_path: Path) -> None:
    """跨语言的**拒绝路径**也要对：错令牌必须被明确拒绝。"""
    server, _, _ = stack
    session = await server.start()
    script = tmp_path / "probe_bad.mjs"
    script.write_text(
        """
import net from "node:net";
const session = JSON.parse(process.argv[2]);
const request = { token: "wrong", tool: "run_command", params: {}, confirmed: true };
const socket = net.createConnection({ host: session.host, port: session.port });
socket.on("connect", () => socket.write(JSON.stringify(request) + "\\n"));
socket.on("data", (chunk) => {
  console.log(chunk.toString("utf8").trim());
  socket.destroy();
  process.exit(0);
});
socket.on("error", () => process.exit(1));
""",
        encoding="utf-8",
    )
    payload = json.dumps({"host": session.host, "port": session.port, "token": session.token})
    try:
        completed = await asyncio.create_subprocess_exec(
            NODE,
            str(script),
            payload,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(completed.communicate(), timeout=60)
    finally:
        await server.stop()

    response = json.loads(stdout.decode("utf-8").strip().splitlines()[-1])
    assert response["ok"] is False
    assert response["error"] == "认证失败"


async def test_read_tool_through_channel(stack, tmp_path: Path) -> None:
    """文件读工具也能经通道走通（不只是命令执行）。"""
    server, _, _ = stack
    target = tmp_path / "notes.md"
    target.write_text("第一行\n第二行\n", encoding="utf-8")
    await server.start()
    try:
        assert server.session is not None
        reader, writer = await asyncio.open_connection(server.session.host, server.session.port)
        writer.write(
            (
                json.dumps(
                    {
                        "token": server.session.token,
                        "tool": "stat_file",
                        "params": {"path": "notes.md"},
                        "confirmed": True,
                    }
                )
                + "\n"
            ).encode()
        )
        await writer.drain()
        response = json.loads((await asyncio.wait_for(reader.readline(), timeout=30)).decode())
        writer.close()
        assert response["ok"] is True, response.get("error")
        assert response["data"]["type"] == "file"
    finally:
        await server.stop()


async def test_channel_refuses_when_not_confirmed(stack) -> None:
    """通道**不能自行铸造授权**：未确认且策略要求确认时拒绝执行。"""
    server, _gateway, _ = stack
    # 用一条没有授权的能力（写）来试：策略会判"需确认"
    await server.start()
    try:
        assert server.session is not None
        reader, writer = await asyncio.open_connection(server.session.host, server.session.port)
        writer.write(
            (
                json.dumps(
                    {
                        "token": server.session.token,
                        "tool": "create_file",
                        "params": {"path": "new.txt", "content": "x"},
                        "confirmed": False,  # 未确认
                    }
                )
                + "\n"
            ).encode()
        )
        await writer.drain()
        response = json.loads((await asyncio.wait_for(reader.readline(), timeout=30)).decode())
        writer.close()
        assert response["ok"] is False
        assert "确认" in (response["error"] or "")
    finally:
        await server.stop()


@pytest.mark.skipif(NODE is None, reason="需要 node")
async def test_kernel_reports_registered_tools(tmp_path: Path) -> None:
    """真起内核：扩展必须回报工具注册成功。

    扩展加载失败时模型会**静默地**没有任何应用侧工具——那是最难查的一类故障，
    所以这条要作为证据留下。
    """
    from limbowave.bootstrap import AppPaths
    from limbowave.composition import build_kernel
    from limbowave.domain.configuration import AppConfiguration
    from limbowave.domain.models import LogicalModel, ModelBinding
    from limbowave.domain.providers import EndpointConfig, ProviderProtocol
    from limbowave.infrastructure.configuration.json_config_repository import (
        JsonConfigRepository,
    )
    from limbowave.infrastructure.crypto.secret_store import SecretStore

    paths = AppPaths(data_root=tmp_path / "data", log_root=tmp_path / "logs")
    paths.data_root.mkdir(parents=True, exist_ok=True)
    key = Vault(
        tmp_path / "v.json", params=KdfParams(time_cost=1, memory_cost=8, parallelism=1)
    ).create("p")
    JsonConfigRepository(paths.data_root / "config.json").save(
        AppConfiguration(
            endpoints=[
                EndpointConfig(
                    id="mock",
                    name="Mock",
                    base_url="http://127.0.0.1:9/v1",
                    api=ProviderProtocol.OPENAI_COMPLETIONS,
                    credential_ref="key-a",
                )
            ],
            models=[
                LogicalModel(
                    id="mock-model",
                    name="Mock",
                    bindings=[ModelBinding(endpoint_id="mock", model_id="mock-model")],
                )
            ],
            default_model_id="mock-model",
        )
    )
    SecretStore(key, paths.data_root / "vault" / "secrets.json").set("key-a", "sk-test")

    # 通道：用真服务端，只为让扩展能读到凭据
    seen: list[dict] = []

    async def dispatch(tool, params, conversation_id, confirmed):
        return {"ok": True, "data": {"echo": tool}}

    server = ToolIpcServer(dispatch, conversation_provider=lambda: "c1")
    session = await server.start()
    setup = build_kernel(paths, key, extra_env={IPC_ENV: session.to_env_value()})
    assert setup is not None
    setup.kernel.set_observation_handler(lambda payload: seen.append(payload))
    try:
        await setup.kernel.start()
        await asyncio.sleep(2.0)  # 等扩展加载与回报
    finally:
        await setup.kernel.shutdown()
        await server.stop()

    registered = [p for p in seen if p.get("kind") == "tools.registered"]
    assert registered, f"扩展没有回报工具注册（收到的观测：{[p.get('kind') for p in seen]}）"
    request = registered[0]["request"]
    # 全集比对：扩展实际注册的清单与 Python 侧 APP_TOOLS 一致——
    # 只抽查几个名字会漏掉「新工具忘了同步」这种回归（read_document 曾漏过）。
    from limbowave.infrastructure.pi_runtime.environment_builder import APP_TOOLS

    assert set(request) == set(APP_TOOLS), f"注册清单不一致：{set(request) ^ set(APP_TOOLS)}"
    assert registered[0]["channel"] == "enabled"  # 通道凭据确实下发了


def test_python_tool_list_matches_extension() -> None:
    """契约守卫：Python 的工具清单与 TS 扩展里的清单必须一致。

    两侧任何一边改了工具名，这里必须失败——否则模型会拿到一个"注册了但服务端不认"
    （或反过来）的工具，症状是运行期才出现的"未知工具"。
    """
    from limbowave.infrastructure.pi_runtime.environment_builder import APP_TOOLS

    ts_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "limbowave"
        / "infrastructure"
        / "extensions"
        / "policy_enforcement.ts"
    )
    text = ts_path.read_text(encoding="utf-8")

    for tool in APP_TOOLS:
        assert f'name: "{tool}"' in text, f"扩展里没有注册 {tool}"

    # 反向：扩展里不得注册 Python 侧不认识的名字（除 Pi 内置工具外）
    import re

    registered = set(re.findall(r'name: "([a-z_]+)"', text))
    assert registered == set(APP_TOOLS), f"两侧清单不一致：{registered ^ set(APP_TOOLS)}"


def test_settings_json_lists_app_tools(tmp_path: Path) -> None:
    """派生 settings.json 的 defaultTools 必须是应用侧工具。"""
    import json

    from limbowave.composition import _app_params  # noqa: F401  (存在性检查)
    from limbowave.domain.configuration import AppConfiguration
    from limbowave.domain.models import LogicalModel, ModelBinding
    from limbowave.domain.providers import EndpointConfig, ProviderProtocol
    from limbowave.domain.routing import RoutingDecision
    from limbowave.infrastructure.pi_runtime.environment_builder import (
        APP_TOOLS,
        EnvironmentBuilder,
    )

    endpoint = EndpointConfig(
        id="mock",
        name="Mock",
        base_url="https://x.example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
    )
    model = LogicalModel(
        id="m", name="M", bindings=[ModelBinding(endpoint_id="mock", model_id="m")]
    )
    decision = RoutingDecision(
        model=model, binding=model.bindings[0], endpoint=endpoint, reason="test"
    )
    AppConfiguration()  # 触发导入路径

    builder = EnvironmentBuilder(tmp_path)
    runtime = builder.build(decision, None)
    settings = json.loads(
        (runtime.runtime_home / ".pi" / "agent" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings["defaultTools"] == list(APP_TOOLS)
    # Pi 自带的文件/命令工具被**有意排除**：它们会绕过应用侧路径守卫与审计
    for builtin in ("read", "write", "edit", "bash", "powershell"):
        assert builtin not in settings["defaultTools"]


def test_extra_env_reaches_spawn_environment(tmp_path: Path) -> None:
    """通道凭据确实进入派生环境（否则扩展读不到）。"""
    from limbowave.domain.models import LogicalModel, ModelBinding
    from limbowave.domain.providers import EndpointConfig, ProviderProtocol
    from limbowave.domain.routing import RoutingDecision
    from limbowave.infrastructure.pi_runtime.environment_builder import EnvironmentBuilder

    endpoint = EndpointConfig(
        id="mock",
        name="Mock",
        base_url="https://x.example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
    )
    model = LogicalModel(
        id="m", name="M", bindings=[ModelBinding(endpoint_id="mock", model_id="m")]
    )
    decision = RoutingDecision(
        model=model, binding=model.bindings[0], endpoint=endpoint, reason="test"
    )
    runtime = EnvironmentBuilder(tmp_path).build(
        decision, None, extra_env={IPC_ENV: '{"host":"127.0.0.1","port":1,"token":"t"}'}
    )
    assert IPC_ENV in runtime.env
    assert "127.0.0.1" in runtime.env[IPC_ENV]


async def test_read_document_through_channel(stack) -> None:
    """附件读取工具经真通道走通：node 客户端同款协议 → 网关 → FileService。

    这是「模型读附件」的最后一段纵向证据：工具注册（另测）+ 通道协议（另测）
    都对，还必须证明 read_document 在**生产同款装配**（网关持有文档服务）下
    能按 file_id 读回登记文档的指定行。
    """
    server, _gateway, file_id = stack
    await server.start()
    try:
        assert server.session is not None
        reader, writer = await asyncio.open_connection(server.session.host, server.session.port)
        writer.write(
            (
                json.dumps(
                    {
                        "token": server.session.token,
                        "tool": "read_document",
                        "params": {"file_id": file_id, "start_line": 2, "end_line": 3},
                        "confirmed": True,
                    }
                )
                + "\n"
            ).encode()
        )
        await writer.drain()
        response = json.loads((await asyncio.wait_for(reader.readline(), timeout=30)).decode())
        writer.close()
        assert response["ok"] is True, response.get("error")
        assert response["data"]["file_id"] == file_id
        assert response["data"]["actual_range"] == [2, 3]
        assert response["data"]["total_lines"] == 3
        assert response["data"]["text"] == "附件行二\n附件行三"
    finally:
        await server.stop()
