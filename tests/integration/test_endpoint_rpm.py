"""真 Pi + 本地模拟站点验证跨进程限流（不访问外部 provider）。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from limbowave.application.services.endpoint_rate_limiter import EndpointRateLimiter
from limbowave.infrastructure.pi_adapter import PiKernelAdapter, build_spawn_spec
from limbowave.infrastructure.tools.ipc_server import RATE_LIMIT_IPC_ENV, ToolIpcServer
from tests.integration.test_pi_adapter import _wait_settled
from tests.integration.test_pi_adapter import mock_models as rpm_models

__all__ = ["rpm_models"]


async def test_pi_requests_wait_for_shared_budget_and_cancel_without_sending(
    rpm_models: Path, mock_provider: tuple[int, Path], tmp_path: Path,
) -> None:
    _, log = mock_provider
    now = 0.0
    limiter = EndpointRateLimiter(clock=lambda: now)
    limiter.try_acquire("mock")  # 同站点的测活刚刚用过额度。

    async def no_tools(*_args: object) -> dict:
        raise AssertionError("Rate limit capability must not invoke tools")

    ipc = ToolIpcServer(no_tools, rate_limit=limiter.try_acquire)
    await ipc.start()
    assert ipc.rate_limit_session is not None
    spec = build_spawn_spec(
        provider="mock", model_id="mock-model", cwd=tmp_path,
        env={RATE_LIMIT_IPC_ENV: ipc.rate_limit_session.to_env_value()},
    )
    adapter = PiKernelAdapter(spec)
    observations: list[dict] = []
    adapter.set_observation_handler(observations.append)
    try:
        await adapter.start()
        await adapter.send_message("first")
        async with asyncio.timeout(15):
            while not any(o.get("kind") == "rate_limit.wait" for o in observations):
                await asyncio.sleep(0.05)
        assert not log.read_text(encoding="utf-8").strip()
        now = 12
        await _wait_settled(adapter)
        sent = log.read_text(encoding="utf-8")
        assert 'mock-model' in sent
        assert limiter.try_acquire("mock") == 12
        observations.clear()
        await adapter.send_message("cancel-before-send")
        async with asyncio.timeout(10):
            while not any(o.get("kind") == "rate_limit.wait" for o in observations):
                await asyncio.sleep(0.05)
        await asyncio.wait_for(adapter.abort(), timeout=3)
        assert log.read_text(encoding="utf-8") == sent
    finally:
        await adapter.shutdown()
        await ipc.stop()


async def test_isolated_kernel_keeps_only_rate_limit_capability(tmp_path: Path) -> None:
    from limbowave.infrastructure.tools.ipc_server import IPC_ENV

    adapter = PiKernelAdapter(build_spawn_spec(
        provider="mock", model_id="mock-model", cwd=tmp_path,
        env={IPC_ENV: "tool-secret", RATE_LIMIT_IPC_ENV: "rate-only-secret"},
    ))
    isolated = adapter.create_isolated()
    assert isinstance(isolated, PiKernelAdapter)
    assert IPC_ENV not in isolated._spec.env
    assert isolated._spec.env[RATE_LIMIT_IPC_ENV] == "rate-only-secret"

async def test_tool_continuation_is_a_separate_rate_limited_request(
    rpm_models: Path, mock_provider: tuple[int, Path], tmp_path: Path,
) -> None:
    import json

    import httpx

    port, log = mock_provider
    async with httpx.AsyncClient() as client:
        response = await client.put(
            f"http://127.0.0.1:{port}/control/toolcall",
            json={"name": "stat_file", "arguments": {"path": "not-used.txt"}},
        )
        response.raise_for_status()
    now = 0.0
    limiter = EndpointRateLimiter(clock=lambda: now)

    async def no_tools(*_args: object) -> dict:
        return {"ok": False, "error": "disabled"}

    ipc = ToolIpcServer(no_tools, rate_limit=limiter.try_acquire)
    await ipc.start()
    assert ipc.rate_limit_session is not None
    adapter = PiKernelAdapter(build_spawn_spec(
        provider="mock", model_id="mock-model", cwd=tmp_path,
        env={RATE_LIMIT_IPC_ENV: ipc.rate_limit_session.to_env_value()},
    ))
    observations: list[dict] = []
    adapter.set_observation_handler(observations.append)
    adapter.set_ui_handler(lambda _: False)
    try:
        await adapter.start()
        await adapter.send_message("tool round")
        async with asyncio.timeout(15):
            while not any(o.get("kind") == "rate_limit.wait" for o in observations):
                await asyncio.sleep(0.05)
        first = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert len(first) == 1  # 工具结果返回后，第二轮尚未发出。
        now = 12
        await _wait_settled(adapter)
        sent = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert len(sent) == 2
    finally:
        await adapter.shutdown()
        await ipc.stop()


async def test_rate_limit_failure_aborts_instead_of_sending_unlimited(
    rpm_models: Path, mock_provider: tuple[int, Path], tmp_path: Path,
) -> None:
    _, log = mock_provider
    limiter = EndpointRateLimiter()
    limiter.stop()

    async def no_tools(*_args: object) -> dict:
        return {"ok": False}

    ipc = ToolIpcServer(no_tools, rate_limit=limiter.try_acquire)
    await ipc.start()
    assert ipc.rate_limit_session is not None
    adapter = PiKernelAdapter(build_spawn_spec(
        provider="mock", model_id="mock-model", cwd=tmp_path,
        env={RATE_LIMIT_IPC_ENV: ipc.rate_limit_session.to_env_value()},
    ))
    try:
        await adapter.start()
        await adapter.send_message("must not send")
        await _wait_settled(adapter)
        assert not log.read_text(encoding="utf-8").strip()
    finally:
        await adapter.shutdown()
        await ipc.stop()
