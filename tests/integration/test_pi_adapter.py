"""PiKernelAdapter + PolicyEnforcementExtension 的集成测试（真实 Pi 子进程 + mock provider）。

这些测试需要本机已安装 Pi（`npm i -g @earendil-works/pi-coding-agent`）与 Node。
缺少时自动 skip。它们复用 tools/pi-verify 的 mock provider 作为模型端点。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import urllib.request
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


async def test_large_provider_request_does_not_block_stderr_observer(
    mock_models: Path, mock_provider: tuple[int, Path], tmp_path: Path
) -> None:
    """超过 asyncio 默认 64 KiB 行限制的最终请求仍能真正到达 provider。"""
    _, log = mock_provider
    observations: list[dict] = []
    spec = build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    adapter = PiKernelAdapter(spec)
    adapter.set_observation_handler(observations.append)
    prompt = "长上下文" + "x" * 200_000
    try:
        await adapter.start()
        await adapter.send_message(prompt)
        await _wait_settled(adapter)
    finally:
        await adapter.shutdown()

    request = next(o for o in observations if o.get("kind") == "provider.request")
    assert len(json.dumps(request, ensure_ascii=False).encode("utf-8")) > 65_536
    assert "mock-model" in log.read_text(encoding="utf-8")


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


async def test_fork_after_restore_and_new_turn(mock_models: Path, tmp_path: Path) -> None:
    """回归：恢复后再聊一轮再分叉，报 "Session file is not a valid pi session"。

    switch_session 让 Pi 进入持久化模式、往物化文件里追加；提前删掉那个文件，
    Pi 追加时会重建一个没有 session 头的文件，fork 重新读它就失败。
    """
    spec = build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    adapter = PiKernelAdapter(spec)
    try:
        await adapter.start()
        await adapter.send_message("第一轮")
        await _wait_settled(adapter)
        snapshot = await adapter.export_runtime_state()
        result = await adapter.restore_runtime_state(snapshot)
        assert result.success, result.error

        await adapter.send_message("第二轮")
        await _wait_settled(adapter)
        entries = await adapter.get_entries()
        users = [
            e
            for e in entries
            if e.get("type") == "message" and (e.get("message") or {}).get("role") == "user"
        ]
        assert len(users) == 2
        text = await adapter.fork(str(users[-1]["id"]))
        assert "第二轮" in text
    finally:
        await adapter.shutdown()
    # 会话文件随适配器关闭清理
    assert not (tmp_path / "runtime" / "pi-sessions").exists() or not any(
        (tmp_path / "runtime" / "pi-sessions").iterdir()
    )


async def test_reload_models_registers_new_provider(
    mock_models: Path, mock_provider: tuple[int, Path], tmp_path: Path
) -> None:
    """热更新：运行中新增站点（密钥经 $ENV 引用），无需重启即可切过去并真正发请求。"""
    port, log = mock_provider
    models_json = mock_models / ".pi" / "agent" / "models.json"
    spec = build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    adapter = PiKernelAdapter(spec)
    try:
        await adapter.start()
        with pytest.raises(RuntimeError, match="Model not found"):
            await adapter.set_model("relay-new", "new-model")

        payload = json.loads(models_json.read_text(encoding="utf-8"))
        payload["providers"]["relay-new"] = {
            "baseUrl": f"http://127.0.0.1:{port}/v1",
            "api": "openai-completions",
            "apiKey": "$LIMBOWAVE_SECRET_NEW",
            "authHeader": True,
            "models": [{"id": "new-model", "name": "New"}],
        }
        models_json.write_text(json.dumps(payload), encoding="utf-8")

        await adapter.reload_models(
            {"LIMBOWAVE_SECRET_NEW": "sk-new", "LIMBOWAVE_PARAM_RULES": ""},
            (("mock", "mock-model"), ("relay-new", "new-model")),
        )
        await adapter.set_model("relay-new", "new-model")
        assert (await adapter.get_state()).model_id == "new-model"

        await adapter.send_message("你好")
        await _wait_settled(adapter)
    finally:
        await adapter.shutdown()
    assert "new-model" in log.read_text(encoding="utf-8")


async def test_reload_models_rejects_wrong_token(mock_models: Path, tmp_path: Path) -> None:
    """输入框里敲同名斜杠命令（没有口令）不能改动模型目录。"""
    models_json = mock_models / ".pi" / "agent" / "models.json"
    spec = build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    adapter = PiKernelAdapter(spec)
    try:
        await adapter.start()
        # 先等 Pi 读完启动目录（start 只拉起进程），再改文件
        await adapter.get_state()
        payload = json.loads(models_json.read_text(encoding="utf-8"))
        payload["providers"]["mock"]["apiKey"] = "$LIMBOWAVE_SECRET_MOCK"
        models_json.write_text(json.dumps(payload), encoding="utf-8")

        forged = json.dumps({"token": "guess", "env": {"LIMBOWAVE_SECRET_MOCK": "x"}})
        await adapter._rpc.request(
            {"type": "prompt", "message": f"/limbowave-reload-models {forged}"}
        )
        listing = await adapter._rpc.request({"type": "get_available_models"})
        models = listing["data"]["models"]
        # 没被刷新：仍是启动时的目录（字面密钥），而非改写后要求的 $ENV
        assert any(m["provider"] == "mock" for m in models)
    finally:
        await adapter.shutdown()


async def test_new_and_deleted_sessions_never_reach_next_provider_request(
    mock_models: Path, mock_provider: tuple[int, Path], tmp_path: Path
) -> None:
    """真实 Pi：内存会话→持久化恢复→删除/新建，旧用户消息和记忆都不能串进新请求。"""
    from limbowave.application.services.history_service import HistoryService
    from limbowave.application.services.session_controller import SessionController
    from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory

    _, log = mock_provider
    store = InMemoryStore()
    factory = in_memory_uow_factory(store)
    spec = build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    adapter = PiKernelAdapter(spec)
    controller = SessionController(adapter, uow_factory=factory)
    observations: list[dict] = []
    original_observer = controller.coordinator()._on_observation

    def observe(payload):
        observations.append(payload)
        original_observer(payload)

    adapter.set_observation_handler(observe)
    try:
        await controller.start()
        await adapter.set_memory_context({
            "run_id": "old-run", "branch_id": "old-branch", "round": 1,
            "global": ["GLOBAL-OLD-CONTEXT"], "session": ["SESSION-OLD-CONTEXT"],
            "global_interval": 1, "session_interval": 1,
        })
        await controller.send("OLD-SCREENSHOT-CANARY")
        await _wait_settled(adapter)
        await controller.wait_idle()
        old_id, old_branch = controller.conversation_id, controller.branch_id
        assert old_id and old_branch
        old_entries = {e["id"] for e in await adapter.get_entries()}
        requests_before = len([o for o in observations if o.get("kind") == "provider.request"])
        assert await controller.new_session()
        # reset/restore 本身不得访问 provider；恢复使 Pi 进入持久化会话模式。
        assert await controller.switch_conversation(old_id, old_branch)
        assert "OLD-SCREENSHOT-CANARY" in json.dumps(await adapter.get_entries())
        assert await controller.new_session()
        assert HistoryService(factory).delete(old_id)
        assert requests_before == sum(
            o.get("kind") == "provider.request" for o in observations
        )
        await controller.send("NEW-SESSION-CANARY")
        await _wait_settled(adapter)
        await controller.wait_idle()
        new_id = controller.conversation_id
        assert new_id and new_id != old_id
        entries = await adapter.get_entries()
        assert old_entries.isdisjoint({e["id"] for e in entries})
        with factory() as uow:
            mirrors = uow.runtime.list_for_conversation(new_id)
            assert old_entries.isdisjoint({m.entry_id for m in mirrors})
    finally:
        await controller.shutdown()

    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    last = json.dumps(records[-1], ensure_ascii=False)
    assert "NEW-SESSION-CANARY" in last
    assert "OLD-SCREENSHOT-CANARY" not in last
    assert "GLOBAL-OLD-CONTEXT" not in last
    assert "SESSION-OLD-CONTEXT" not in last


async def test_contaminated_mirror_is_filtered_before_real_pi_restore(
    mock_models: Path, mock_provider: tuple[int, Path], tmp_path: Path
) -> None:
    """模拟已落库的旧前缀：恢复副本清理后，下一次真实请求不含旧用户消息。"""
    from datetime import UTC, datetime

    from limbowave.application.services.runtime_state_service import RuntimeStateService
    from limbowave.domain.conversation import Conversation
    from limbowave.domain.runtime_mirror import RuntimeEntryMirror
    from limbowave.infrastructure.memory_repositories import in_memory_uow_factory

    _, log = mock_provider
    spec = build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    adapter = PiKernelAdapter(spec)
    try:
        await adapter.start()
        await adapter.send_message("DELETED-PREFIX-CANARY")
        await _wait_settled(adapter)
        old_entries = await adapter.get_entries()
        await adapter.new_session()
        created = datetime.now(UTC)
        await adapter.send_message("CURRENT-HISTORY-CANARY")
        await _wait_settled(adapter)
        current = await adapter.get_entries()
        # 模拟旧版没有真正 new_session：新会话的首条 entry 接在已删除的旧会话后。
        current[0] = {**current[0], "parentId": old_entries[-1]["id"]}
        combined = old_entries + current
        factory = in_memory_uow_factory()
        with factory() as uow:
            uow.conversations.add(Conversation("contaminated", "test", created))
            uow.runtime.add_many([
                RuntimeEntryMirror(
                    id=f"mirror-{i}", conversation_id="contaminated", entry_id=e["id"],
                    entry_type=e["type"], captured_at=datetime.now(UTC),
                    parent_entry_id=e.get("parentId"), payload=e,
                ) for i, e in enumerate(combined)
            ])
            uow.commit()
        snapshot = RuntimeStateService(factory).build_branch_snapshot(
            "contaminated", current[-1]["id"]
        )
        assert snapshot is not None
        assert "DELETED-PREFIX-CANARY" not in json.dumps(snapshot.entries)
        assert "CURRENT-HISTORY-CANARY" in json.dumps(snapshot.entries)
        restored = await adapter.restore_runtime_state(snapshot)
        assert restored.success, (
            [(e["id"], e["type"]) for e in snapshot.entries],
            [(e["id"], e["type"]) for e in await adapter.get_entries()],
        )
        await adapter.send_message("CONTINUE-CLEAN-CANARY")
        await _wait_settled(adapter)
        with factory() as uow:
            assert len(uow.runtime.list_for_conversation("contaminated")) == len(combined)
    finally:
        await adapter.shutdown()
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    last = json.dumps(records[-1], ensure_ascii=False)
    assert "CONTINUE-CLEAN-CANARY" in last
    assert "CURRENT-HISTORY-CANARY" in last
    assert "DELETED-PREFIX-CANARY" not in last
