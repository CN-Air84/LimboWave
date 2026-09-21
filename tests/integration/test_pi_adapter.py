"""PiKernelAdapter + PolicyEnforcementExtension 的集成测试（真实 Pi 子进程 + mock provider）。

这些测试需要本机已安装 Pi（`npm i -g @earendil-works/pi-coding-agent`）与 Node。
缺少时自动 skip。它们复用 tools/pi-verify 的 mock provider 作为模型端点。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from limbowave.application.kernel import KernelCapability
from limbowave.infrastructure.pi_adapter import PiKernelAdapter, build_spawn_spec

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "pi-verify"
MOCK = TOOLS / "mock_provider.py"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not MOCK.is_file(),
    reason="需要 node 与 tools/pi-verify/mock_provider.py",
)


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def mock_provider(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    """起一个 mock provider，返回 (端口, 请求日志路径)。"""
    port = _free_port()
    log = tmp_path / "mock.jsonl"
    log.write_text("", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(MOCK), "--port", str(port), "--log", str(log)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    time.sleep(1.5)
    try:
        yield port, log
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def mock_models(
    tmp_path: Path, mock_provider: tuple[int, Path], monkeypatch: pytest.MonkeyPatch
) -> Path:
    """写一个指向 mock 的 models.json 到隔离的 ~/.pi/agent（monkeypatch home）。"""
    port, _ = mock_provider
    home = tmp_path / "home"
    agent = home / ".pi" / "agent"
    agent.mkdir(parents=True)
    (agent / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "mock": {
                        "baseUrl": f"http://127.0.0.1:{port}/v1",
                        "api": "openai-completions",
                        "apiKey": "mock-key",
                        "models": [
                            {
                                "id": "mock-model",
                                "name": "Mock",
                                "contextWindow": 128000,
                                "maxTokens": 4096,
                                "input": ["text"],
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (agent / "settings.json").write_text(
        json.dumps({"enableInstallTelemetry": False, "defaultProjectTrust": "never"}),
        encoding="utf-8",
    )
    # 让 Pi 的 ~/.pi 指向隔离目录（Windows 用 USERPROFILE）
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    return home


async def _wait_settled(adapter: PiKernelAdapter, timeout: float = 60.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(0.1)
        state = await adapter.get_state()
        if not state.is_streaming and state.pending_message_count == 0 and state.message_count > 0:
            await asyncio.sleep(0.4)
            return
    raise TimeoutError("agent 未在限时内安定")


async def test_adapter_lifecycle_and_capabilities(mock_models: Path, tmp_path: Path) -> None:
    spec = build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    adapter = PiKernelAdapter(spec)
    try:
        await adapter.start()
        assert adapter.capabilities().has(KernelCapability.FINAL_REQUEST_HOOK)
        state = await adapter.get_state()
        assert state.model_id == "mock-model"
        assert state.session_id
    finally:
        await adapter.shutdown()
    assert adapter._rpc.returncode == 0


async def test_send_message_streams_events(
    mock_models: Path, mock_provider: tuple[int, Path], tmp_path: Path
) -> None:
    _, log = mock_provider
    events: list[str] = []
    observations: list[dict] = []
    spec = build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    adapter = PiKernelAdapter(spec)
    adapter.subscribe(lambda e: events.append(e.kind))
    adapter.set_observation_handler(observations.append)
    try:
        await adapter.start()
        await adapter.send_message("你好")
        await _wait_settled(adapter)
    finally:
        await adapter.shutdown()

    assert "run.start" in events
    assert any(k.startswith("message.") for k in events)
    # 观测通道应有 provider 请求（扩展已加载的证据）
    assert any(o.get("kind") == "provider.request" for o in observations)
    # mock 侧确实收到了请求
    assert '"model": "mock-model"' in log.read_text(
        encoding="utf-8"
    ) or "mock-model" in log.read_text(encoding="utf-8")


async def test_policy_extension_blocks_tool_by_default(
    mock_models: Path, mock_provider: tuple[int, Path], tmp_path: Path
) -> None:
    """模型发起工具调用，应用默认拒绝 → 机密不得进入上下文。"""
    port, log = mock_provider
    secret = "CANARY-SECRET-INTEGRATION"
    (tmp_path / "secret.txt").write_text(secret, encoding="utf-8")

    observations: list[dict] = []
    spec = build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    adapter = PiKernelAdapter(spec)
    adapter.set_observation_handler(observations.append)
    try:
        await adapter.start()
        # 注册：模型下一次响应发出 read(secret.txt)
        urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{port}/control/toolcall",
                data=json.dumps({"name": "read", "arguments": {"path": "secret.txt"}}).encode(),
                headers={"Content-Type": "application/json"},
                method="PUT",
            ),
            timeout=10,
        ).read()
        await adapter.send_message("读文件")
        await _wait_settled(adapter)
    finally:
        await adapter.shutdown()

    # 钩子触发且默认拒绝
    assert any(o.get("kind") == "tool_call" for o in observations)
    assert any(o.get("kind") == "tool_call.denied" for o in observations)
    # 机密未泄露
    assert secret not in log.read_text(encoding="utf-8")


async def test_session_controller_end_to_end(
    mock_models: Path, mock_provider: tuple[int, Path], tmp_path: Path
) -> None:
    """controller → adapter → rpc → 扩展 → mock 全链：发送并收到完整的 assistant 回复。"""
    from limbowave.application.services.session_controller import SessionController

    events: list[tuple[str, dict]] = []
    spec = build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    controller = SessionController(PiKernelAdapter(spec))
    controller.subscribe(lambda e: events.append((e.kind, e.data)))

    async def wait_idle(timeout: float = 60.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(0.1)
            if any(k == "settled" for k, _ in events):
                return
        raise TimeoutError("控制器未在限时内安定")

    try:
        await controller.start()
        assert controller.available
        await controller.send("你好")
        await wait_idle()
    finally:
        await controller.shutdown()

    kinds = [k for k, _ in events]
    assert "user" in kinds
    assert "assistant_start" in kinds
    assert "assistant_delta" in kinds
    assert "assistant_end" in kinds
    assert "settled" in kinds
    # mock 的回复是 "ACK turn=1 messages=2"
    end = next(d for k, d in events if k == "assistant_end")
    assert "ACK" in end["text"]
    assert not controller.busy
