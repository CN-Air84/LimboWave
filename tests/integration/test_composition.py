"""组合根集成测试：**不设任何环境变量**，仅凭应用权威配置起内核。

这正对 Phase 1A 的验收条款：

- 不设置 ``LIMBOWAVE_PROVIDER`` / ``LIMBOWAVE_MODEL`` 也能启动聊天；
- API 密钥不出现在普通配置、Pi 的 ``auth.json``、日志或异常文本中；
- Pi 的 ``models.json`` 是运行时派生产物，不是权威数据源；
- 路由选择确定、可解释；未配置时保持优雅降级。

判定依据是 mock provider 记录的**真实请求头**——密钥有没有正确送达，由请求本身证明。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from limbowave.bootstrap import AppPaths
from limbowave.composition import build_kernel
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import VaultKey

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("node") is None,
    reason="需要 node 与已安装的 Pi",
)

CREDENTIAL_REF = "key-a"
SECRET = "sk-live-COMPOSITION-TEST-KEY"


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(data_root=tmp_path / "data", log_root=tmp_path / "logs")


def _seed_config(paths: AppPaths, base_url: str, key: VaultKey) -> None:
    """写入权威配置 + 密钥。配置里只有引用，密钥进加密库。"""
    paths.data_root.mkdir(parents=True, exist_ok=True)
    config = AppConfiguration(
        endpoints=[
            EndpointConfig(
                id="mock",
                name="本地 Mock",
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
    JsonConfigRepository(paths.data_root / "config.json").save(config)
    SecretStore(key, paths.data_root / "vault" / "secrets.json").set(CREDENTIAL_REF, SECRET)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.mark.parametrize("build_in_worker", [False, True])
async def test_kernel_from_config_without_env_vars(
    tmp_path: Path, mock_provider: tuple[int, Path], vault_key: VaultKey,
    build_in_worker: bool,
) -> None:
    """权威配置 → 路由 → 密钥 → 派生环境 → 真实一轮对话。"""
    import asyncio

    port, log = mock_provider
    paths = _paths(tmp_path)
    _seed_config(paths, f"http://127.0.0.1:{port}/v1", vault_key)

    # Startup constructs the runtime off-thread, then starts/uses it on the GUI loop.
    setup = (
        await asyncio.to_thread(build_kernel, paths, vault_key)
        if build_in_worker else build_kernel(paths, vault_key)
    )
    assert setup is not None, "配置齐全时应能构建内核（不依赖任何环境变量）"
    # 路由决策随装配结果一起给出，请求意图快照据此可解释
    assert setup.logical_model_id == "mock-model"
    assert setup.endpoint_id == "mock"
    assert setup.routing_reason
    kernel = setup.kernel
    try:
        await kernel.start()
        await kernel.send_message("你好")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        while loop.time() < deadline:
            await asyncio.sleep(0.1)
            state = await kernel.get_state()
            if not state.is_streaming and state.message_count > 0:
                await asyncio.sleep(0.4)
                break
    finally:
        await kernel.shutdown()

    entries = _read_jsonl(log)
    assert entries, "mock 侧应收到真实请求"

    # 密钥经环境变量注入、由 Pi 放进 Authorization 头
    auth = entries[0]["headers"].get("Authorization", "")
    assert SECRET in auth, f"密钥未正确送达：{auth!r}"


async def test_derived_models_json_is_runtime_artifact(
    tmp_path: Path, mock_provider: tuple[int, Path], vault_key: VaultKey
) -> None:
    """Pi 的 models.json 由运行时派生，权威仍是应用的 config.json。"""
    port, _ = mock_provider
    paths = _paths(tmp_path)
    _seed_config(paths, f"http://127.0.0.1:{port}/v1", vault_key)

    setup = build_kernel(paths, vault_key)
    assert setup is not None
    kernel = setup.kernel
    try:
        await kernel.start()
    finally:
        await kernel.shutdown()

    derived = paths.data_root / "runtime" / "pi-home" / ".pi" / "agent" / "models.json"
    assert derived.is_file(), "应派生 models.json"

    payload = json.loads(derived.read_text(encoding="utf-8"))
    provider = payload["providers"]["mock"]
    assert provider["apiKey"] == "$LIMBOWAVE_SECRET_KEY_A"  # 只有引用，没有明文
    assert SECRET not in derived.read_text(encoding="utf-8")

    # 权威配置里不得出现密钥明文
    authoritative = (paths.data_root / "config.json").read_text(encoding="utf-8")
    assert CREDENTIAL_REF in authoritative
    assert SECRET not in authoritative


def test_locked_vault_degrades_gracefully(tmp_path: Path) -> None:
    """资料库未解锁（无主密钥）→ 返回 None，不抛给 GUI。"""
    assert build_kernel(_paths(tmp_path), None) is None


def test_empty_config_degrades_gracefully(tmp_path: Path, vault_key: VaultKey) -> None:
    """未配置任何模型 → 返回 None，不抛给 GUI。"""
    assert build_kernel(_paths(tmp_path), vault_key) is None


def test_missing_secret_degrades_gracefully(
    tmp_path: Path, mock_provider: tuple[int, Path], vault_key: VaultKey
) -> None:
    """端点引用了不存在的凭据 → 同样降级为 None（配置问题，不是崩溃）。"""
    port, _ = mock_provider
    paths = _paths(tmp_path)
    _seed_config(paths, f"http://127.0.0.1:{port}/v1", vault_key)
    # 抹掉密钥库，保留引用
    (paths.data_root / "vault" / "secrets.json").unlink()

    assert build_kernel(paths, vault_key) is None
