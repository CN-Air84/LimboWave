"""Phase 1C 真实纵向集成：Runtime 崩溃后从镜像恢复，第三轮证明上下文完整。

这是 Phase 1C 最有分量的验收——比只验证 Pi 内部 entries 更接近产品真实语义：

    第一轮对话 → 第二轮对话 → 保存镜像 → 杀掉 Pi
    → 创建新 Pi Runtime → 从应用镜像恢复
    → 发送第三轮对话 → mock provider 收到的请求包含恢复前的全部上下文

mock 侧第三次请求应证明：
- 第一轮用户/助手消息存在
- 第二轮用户/助手消息存在
- 第三轮当前用户消息存在
- 消息顺序正确
- 恢复动作本身没有触发 Provider 请求
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from limbowave.application.kernel import KernelSetup
from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.application.services.runtime_recovery_coordinator import (
    RuntimeRecoveryCoordinator,
)
from limbowave.bootstrap import AppPaths
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.domain.run import RunStatus
from limbowave.domain.runtime_state import RuntimeStateSnapshot, fingerprint_from_entry
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("node") is None,
    reason="需要 node 与已安装的 Pi",
)

CREDENTIAL_REF = "key-a"
SECRET = "sk-live-RECOVERY-E2E"
MARK1 = "TOKEN-ALPHA-RECOVERY"
MARK2 = "TOKEN-BETA-RECOVERY"


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(data_root=tmp_path / "data", log_root=tmp_path / "logs")


def _seed(paths: AppPaths, base_url: str, key: VaultKey) -> None:
    paths.data_root.mkdir(parents=True, exist_ok=True)
    JsonConfigRepository(paths.data_root / "config.json").save(
        AppConfiguration(
            endpoints=[
                EndpointConfig(
                    id="mock",
                    name="Mock",
                    base_url=base_url,
                    api=ProviderProtocol.OPENAI_COMPLETIONS,
                    credential_ref=CREDENTIAL_REF,
                )
            ],
            models=[
                LogicalModel(
                    id="mock-model",
                    name="Mock",
                    bindings=[ModelBinding(endpoint_id="mock", model_id="mock-model")],
                    context_window=128000,
                    max_tokens=4096,
                )
            ],
            default_model_id="mock-model",
        )
    )
    SecretStore(key, paths.data_root / "vault" / "secrets.json").set(CREDENTIAL_REF, SECRET)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


async def _send_and_wait(coord: RunCoordinator, text: str, timeout: float = 60.0) -> str | None:
    """发送并等待安定。返回 run_id。"""
    run_id = await coord.send(text)
    assert run_id is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(0.1)
        state = await coord.state()
        if state is not None and not state.is_streaming and state.message_count > 0:
            await asyncio.sleep(0.5)
            return run_id
    raise TimeoutError(f"消息未在限时内安定：{text!r}")


def _snapshot_from_mirrors(store: InMemoryStore) -> RuntimeStateSnapshot | None:
    """从 RunCoordinator 的仓库镜像构建恢复快照（恢复时唯一可用路径）。"""
    entries = []
    seen: set[str] = set()
    mirrors = sorted(store.mirrors.values(), key=lambda m: m.captured_at)
    for mirror in mirrors:
        if mirror.entry_id in seen:
            continue
        seen.add(mirror.entry_id)
        entry = dict(mirror.payload)
        entry.setdefault("id", mirror.entry_id)
        entry.setdefault("type", mirror.entry_type)
        if mirror.parent_entry_id:
            entry.setdefault("parentId", mirror.parent_entry_id)
        entries.append(entry)

    if not entries:
        return None
    fps = [fingerprint_from_entry(e, branch_position=i) for i, e in enumerate(entries)]
    return RuntimeStateSnapshot(
        conversation_id="recovery-e2e",
        entries=entries,
        leaf_entry_id=entries[-1].get("id"),
        fingerprint=fps,
    )


async def test_full_recovery_flow_with_third_round_proof(
    tmp_path: Path, mock_provider: tuple[int, Path], vault_key: VaultKey
) -> None:
    """三轮真实请求证明恢复完整：前两轮上下文在第三轮请求中可见。"""
    from limbowave.composition import build_kernel

    port, mock_log = mock_provider
    paths = _paths(tmp_path)
    _seed(paths, f"http://127.0.0.1:{port}/v1", vault_key)

    setup = build_kernel(paths, vault_key)
    assert isinstance(setup, KernelSetup)
    context = RunContext(
        logical_model_id=setup.logical_model_id,
        endpoint_id=setup.endpoint_id,
        routing_reason=setup.routing_reason,
        app_params=dict(setup.app_params),
    )

    store = InMemoryStore()
    coord_a = RunCoordinator(setup.kernel, in_memory_uow_factory(store), context=lambda: context)

    try:
        # --- 第一轮和第二轮 ---
        await coord_a.start()
        await _send_and_wait(coord_a, f"记住 {MARK1}")
        await _send_and_wait(coord_a, f"再记住 {MARK2}")
        await coord_a.wait_idle()

        # 验证两轮都落了 Run
        assert len(store.runs) == 2
        assert all(r.status is RunStatus.COMPLETED for r in store.runs.values())

        # 验证镜像已保存
        assert store.mirrors, "RunCoordinator 应保存 Pi entries 镜像"
        mirror_count = len({m.entry_id for m in store.mirrors.values()})
        assert mirror_count >= 5  # system + 2×user + 2×assistant

        # --- 从镜像构建快照（崩溃前的最后操作）---
        snapshot = _snapshot_from_mirrors(store)
        assert snapshot is not None
        assert len(snapshot.entries) == mirror_count

        # --- 杀掉 Pi（模拟崩溃，不经 kill() 避免标 expected=True）---
        proc = setup.kernel._rpc._proc
        assert proc is not None
        proc.kill()
        await proc.wait()
        assert setup.kernel._rpc.returncode is not None

        # --- 创建替代内核并启动 ---
        setup_b = build_kernel(paths, vault_key)
        assert isinstance(setup_b, KernelSetup)
        await setup_b.kernel.start()

        # --- 恢复 ---
        recovery = RuntimeRecoveryCoordinator()
        recovery.attach_replacement_kernel(setup_b.kernel)
        result = await recovery.recover(snapshot)
        assert result.success, f"恢复失败：{result.failure_reason} {result.error}"
        assert result.state.value == "recovered"
        recovery.make_available()
        assert recovery.can_send

        # --- 第三轮：用替代内核继续对话 ---
        context_b = RunContext(
            logical_model_id=setup_b.logical_model_id,
            endpoint_id=setup_b.endpoint_id,
            routing_reason=setup_b.routing_reason,
            app_params=dict(setup_b.app_params),
        )
        coord_b = RunCoordinator(
            setup_b.kernel, in_memory_uow_factory(store), context=lambda: context_b
        )
        await coord_b.start()

        mock_log.write_text("", encoding="utf-8")  # 清空，只看第三轮的请求
        await _send_and_wait(coord_b, "我们之前记住了哪两个标记？只回复标记本身")
        await coord_b.wait_idle()
    finally:
        await coord_a.shutdown()

    # --- 验证第三轮请求包含恢复前的全部上下文 ---
    entries = _read_jsonl(mock_log)
    assert entries, "mock 应收到第三轮请求"
    body = entries[0]["body"]
    messages = body.get("messages", [])
    blob = json.dumps(messages, ensure_ascii=False)

    assert MARK1 in blob, f"第一轮用户消息应出现：{blob[:200]}"
    assert MARK2 in blob, f"第二轮用户消息应出现：{blob[:200]}"

    # 消息顺序：恢复后只应有第三轮的请求
    assert len(entries) == 1, f"恢复本身不应触发 Provider 请求，实收 {len(entries)} 次"

    # 助手消息也应存在（ACK turn=N）
    acks = [m for m in messages if m.get("role") == "assistant"]
    assert len(acks) >= 2, f"前两轮的助手回复应在上下文中，实得 {len(acks)} 条"
