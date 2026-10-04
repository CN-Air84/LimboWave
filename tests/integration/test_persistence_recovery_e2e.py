"""持久化阶段的旗舰验收：**应用重启后**从持久化镜像恢复 Runtime。

与 Phase 1C 的恢复 e2e（内存仓库）不同，这里模拟的是真实产品路径：

    第一轮 → 第二轮（SQLite 落库，进程内对象全部丢弃）
    → 杀掉 Pi（模拟崩溃）→ **模拟应用重启**：全新 UoW 工厂、全新协调器，
      唯一的记忆来源是磁盘上的 limbowave.db
    → 从持久化的 Runtime 镜像重建恢复快照 → 恢复进新内核
    → 第三轮对话：mock 收到的请求包含重启前的全部上下文

同时断言重启后**历史消息、运行记录、双快照**都能从库里读回——
Phase 1B/1C 的权威数据不再随进程消亡。
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
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("node") is None,
    reason="需要 node 与已安装的 Pi",
)

CREDENTIAL_REF = "key-a"
SECRET = "sk-live-PERSIST-RECOVERY"
MARK1 = "TOKEN-GAMMA-PERSIST"
MARK2 = "TOKEN-DELTA-PERSIST"


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


def _snapshot_from_persisted(
    db_path: Path, key: VaultKey, conversation_id: str
) -> RuntimeStateSnapshot:
    """从**持久化**镜像重建恢复快照——重启后这是唯一可用的记忆来源。"""
    uow = sqlite_uow_factory(db_path, key)()
    try:
        mirrors = uow.runtime.list_for_conversation(conversation_id)
        assert mirrors, "重启后应能从数据库读回 Runtime 镜像"

        entries = []
        seen: set[str] = set()
        for mirror in sorted(mirrors, key=lambda m: m.captured_at):
            if mirror.entry_id in seen:
                continue
            seen.add(mirror.entry_id)
            entry = dict(mirror.payload)
            entry.setdefault("id", mirror.entry_id)
            entry.setdefault("type", mirror.entry_type)
            if mirror.parent_entry_id:
                entry.setdefault("parentId", mirror.parent_entry_id)
            entries.append(entry)

        fps = [fingerprint_from_entry(e, branch_position=i) for i, e in enumerate(entries)]
        return RuntimeStateSnapshot(
            conversation_id=conversation_id,
            entries=entries,
            leaf_entry_id=entries[-1].get("id"),
            fingerprint=fps,
        )
    finally:
        uow.close()


async def test_restart_then_recover_from_persisted_mirrors(
    tmp_path: Path, mock_provider: tuple[int, Path], vault_key: VaultKey
) -> None:
    from limbowave.composition import build_kernel

    port, mock_log = mock_provider
    paths = _paths(tmp_path)
    _seed(paths, f"http://127.0.0.1:{port}/v1", vault_key)
    db_path = paths.data_root / "limbowave.db"

    setup = build_kernel(paths, vault_key)
    assert isinstance(setup, KernelSetup)
    context = RunContext(
        logical_model_id=setup.logical_model_id,
        endpoint_id=setup.endpoint_id,
        routing_reason=setup.routing_reason,
        app_params=dict(setup.app_params),
    )

    # ================= 第一个"应用进程" =================
    factory_a = sqlite_uow_factory(db_path, vault_key)
    coord_a = RunCoordinator(setup.kernel, factory_a, context=lambda: context)
    conversation_id: str | None = None
    try:
        await coord_a.start()
        await _send_and_wait(coord_a, f"记住 {MARK1}")
        await _send_and_wait(coord_a, f"再记住 {MARK2}")
        await coord_a.wait_idle()
        conversation_id = coord_a.conversation_id
        assert conversation_id is not None

        # 杀掉 Pi（模拟崩溃）
        proc = setup.kernel._rpc._proc
        assert proc is not None
        proc.kill()
        await proc.wait()
    finally:
        # 进程退出：丢弃所有内存对象引用
        await coord_a.shutdown()
        del coord_a, factory_a, setup

    # ================= 第二个"应用进程"：只剩磁盘上的库 =================
    # 历史数据存活验证：全新的工厂，唯一来源是 limbowave.db
    factory_b = sqlite_uow_factory(db_path, vault_key)
    uow = factory_b()
    runs = uow.runs.list_for_conversation(conversation_id)
    assert len(runs) == 2, "重启后两轮运行记录都应可回读"
    assert all(r.status is RunStatus.COMPLETED for r in runs)
    branch_id = runs[0].branch_id
    messages = uow.messages.list_for_branch(branch_id)
    assert len(messages) == 4, "重启后两用户 + 两助手消息都应可回读"
    for run in runs:
        assert uow.snapshots.get_intent(run.id) is not None, "意图快照应持久化"
        assert uow.snapshots.list_transport(run.id), "传输快照应持久化"
    uow.close()

    # --- 从持久化镜像重建快照并恢复进新内核 ---
    snapshot = _snapshot_from_persisted(db_path, vault_key, conversation_id)
    assert len(snapshot.entries) >= 5

    setup_b = build_kernel(paths, vault_key)
    assert isinstance(setup_b, KernelSetup)
    await setup_b.kernel.start()

    recovery = RuntimeRecoveryCoordinator()
    recovery.attach_replacement_kernel(setup_b.kernel)
    result = await recovery.recover(snapshot)
    assert result.success, f"从持久化镜像恢复失败：{result.failure_reason} {result.error}"
    recovery.make_available()

    # --- 第三轮：新进程、新内核，上下文来自磁盘 ---
    context_b = RunContext(
        logical_model_id=setup_b.logical_model_id,
        endpoint_id=setup_b.endpoint_id,
        routing_reason=setup_b.routing_reason,
        app_params=dict(setup_b.app_params),
    )
    coord_b = RunCoordinator(setup_b.kernel, factory_b, context=lambda: context_b)
    try:
        await coord_b.start()
        # 重启后续接既有会话，而不是另开新会话
        resumed = await coord_b.resume(conversation_id, branch_id)
        assert resumed, "重启后应能续接既有会话"
        mock_log.write_text("", encoding="utf-8")
        await _send_and_wait(coord_b, "我们之前记住了哪两个标记？只回复标记本身")
        await coord_b.wait_idle()
    finally:
        await coord_b.shutdown()

    # --- 第三轮的请求里，重启前的上下文完整可见 ---
    entries = _read_jsonl(mock_log)
    assert len(entries) == 1, f"恢复本身不应触发 Provider 请求，实收 {len(entries)} 次"
    blob = json.dumps(entries[0]["body"].get("messages", []), ensure_ascii=False)
    assert MARK1 in blob, f"重启前第一轮上下文应存活：{blob[:200]}"
    assert MARK2 in blob, f"重启前第二轮上下文应存活：{blob[:200]}"

    # --- 第三轮的 run 也落进了同一个库 ---
    uow = factory_b()
    try:
        all_runs = uow.runs.list_for_conversation(conversation_id)
        assert len(all_runs) == 3, "重启后的新一轮应与前两轮落在同一会话"
    finally:
        uow.close()
