"""门禁 10：真实纵向集成——TransportSnapshot 必须对上 mock 实际收到的请求。

这是 Phase 1B 唯一一条需要真实 Pi 进程的验收：前面 9 条用 FakeKernel 证明语义，
这一条证明**观测链路真的连通**——扩展经 stderr 报上来的最终请求，
与应用落库的传输快照逐字段一致。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from limbowave.application.kernel import KernelSetup
from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.bootstrap import AppPaths
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.domain.run import RunStatus
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("node") is None,
    reason="需要 node 与已安装的 Pi",
)

CREDENTIAL_REF = "key-a"
SECRET = "sk-live-E2E-TRANSPORT-KEY"


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(data_root=tmp_path / "data", log_root=tmp_path / "logs")


def _seed(paths: AppPaths, base_url: str, key: VaultKey) -> None:
    paths.data_root.mkdir(parents=True, exist_ok=True)
    JsonConfigRepository(paths.data_root / "config.json").save(
        AppConfiguration(
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
    )
    SecretStore(key, paths.data_root / "vault" / "secrets.json").set(CREDENTIAL_REF, SECRET)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


async def test_transport_snapshot_matches_mock_received_request(
    tmp_path: Path, mock_provider: tuple[int, Path], vault_key: VaultKey
) -> None:
    from limbowave.composition import build_kernel

    port, mock_log = mock_provider
    paths = _paths(tmp_path)
    _seed(paths, f"http://127.0.0.1:{port}/v1", vault_key)

    setup = build_kernel(paths, vault_key)
    assert isinstance(setup, KernelSetup), "配置齐全时应能装配内核"

    store = InMemoryStore()
    coordinator = RunCoordinator(
        setup.kernel,
        in_memory_uow_factory(store),
        context=lambda: RunContext(
            logical_model_id=setup.logical_model_id,
            endpoint_id=setup.endpoint_id,
            routing_reason=setup.routing_reason,
            app_params=dict(setup.app_params),
        ),
    )

    try:
        await coordinator.start()
        run_id = await coordinator.send("你好")
        assert run_id is not None

        import asyncio

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        while loop.time() < deadline:
            await asyncio.sleep(0.1)
            state = await coordinator.state()
            if state is not None and not state.is_streaming and state.message_count > 0:
                await asyncio.sleep(0.5)
                break
        await coordinator.wait_idle()
    finally:
        await coordinator.shutdown()

    # --- 应用侧落库 ---
    run = next(iter(store.runs.values()))
    assert run.status is RunStatus.COMPLETED
    transport = next(iter(store.transports.values()))
    assert transport.run_id == run_id

    # --- mock 侧实际收到 ---
    received = _read_jsonl(mock_log)
    assert received, "mock 应收到真实请求"
    actual_body = received[0]["body"]
    actual_headers = received[0]["headers"]

    # 传输快照的请求体与 mock 实收一致（关键字段逐条对）
    for key in ("model", "stream", "max_completion_tokens"):
        assert transport.body.get(key) == actual_body.get(key), f"字段 {key} 不一致"

    # 密钥确实送达，但快照里只有脱敏视图
    assert SECRET in actual_headers.get("Authorization", "")
    assert transport.headers["Authorization"] == {
        "present": True,
        "scheme": "Bearer",
        "value": "[REDACTED]",
    }
    assert SECRET not in json.dumps(transport.headers)

    # 意图与传输的差异被如实记录：Pi 注入了应用没写的字段
    assert "messages" in transport.param_diff["only_in_transport"]
    assert "model" in transport.param_diff["shared"]

    # --- §十三.1「原始响应」与「流式事件」：真实 Pi + 真实 HTTP 流 ---
    # 这是解析后的响应对象（Pi 不给扩展 HTTP 正文），但字段都来自协议标准位置
    body = transport.response_body
    assert body, "真实一轮应留下响应对象"
    assert body.get("stopReason") == "stop"
    assert body.get("model") == actual_body.get("model")
    assert body.get("usage"), f"usage 应被记录：{body}"
    assert any(
        block.get("type") == "text" for block in body.get("content", []) if isinstance(block, dict)
    ), f"应含正文块：{body.get('content')}"

    # 流式磁带：mock 是逐块推的，所以应能看到 http → start → text… → end 的形状
    tape = transport.stream_tape
    assert tape, "真实一轮应留下流式磁带"
    events = [e["e"] for e in tape["events"]]
    assert "http" in events, f"响应头到达应有记录：{events}"
    assert "text" in events, f"文本增量应有记录：{events}"
    assert events[-1] == "end", f"最后一条应是结束：{events}"
    assert tape["text_chars"] > 0
    assert tape["dropped"] == 0

    # Runtime 镜像已落库，且 Pi entry ID 没被当作应用主键
    assert store.mirrors, "应镜像 Pi entries"
    assert all(m.id != m.entry_id for m in store.mirrors.values())
    assert all(m.run_id == run_id for m in store.mirrors.values())

    # 应用消息与快照同属这一轮
    assert all(m.run_id == run_id for m in store.messages.values())
    assert run.assistant_message_id is not None
