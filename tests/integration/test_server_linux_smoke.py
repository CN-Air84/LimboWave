"""Linux/headless server lifecycle smoke tests."""

from __future__ import annotations

import asyncio
import runpy
from pathlib import Path

import httpx
import pytest

from limbowave.bootstrap import AppPaths
from limbowave.infrastructure.crypto.vault import KdfParams, Vault
from limbowave.server.composition import ServerRuntime, build_server_runtime
from limbowave.server.control import send_control
from limbowave.server.lifecycle import ServerLifecycle, ServerState
from limbowave.web.security import WebConfig


@pytest.fixture
def fake_kernel():
    path = Path(__file__).parents[1] / "unit" / "test_run_coordinator.py"
    return runpy.run_path(str(path))["FakeKernel"]()


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(tmp_path, tmp_path / "logs")


@pytest.mark.asyncio
async def test_locked_server_health_does_not_open_pairing(tmp_path: Path, fake_kernel) -> None:
    paths = _paths(tmp_path)
    Vault(paths.data_root / "vault.json", params=KdfParams(1, 8192, 1)).create("password")

    def builder(p: AppPaths, key):
        return build_server_runtime(p, key, kernel=fake_kernel)

    server = ServerLifecycle(
        paths,
        web_config=WebConfig(host="127.0.0.1", port=0),
        control_socket=tmp_path / "server.sock",
        runtime_builder=builder,
    )
    try:
        await server.start()
        assert server.state is ServerState.LOCKED
        assert server.web is not None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.web.app), base_url=server.web.url
        ) as client:
            assert (await client.get("/healthz")).json() == {"ok": True, "locked": True}
            assert (await client.get("/api/v1/pair/status")).json() == {"status": "locked"}
            assert (await client.get("/api/v1/state")).status_code == 503
    finally:
        await server.close()


@pytest.mark.skipif(
    not hasattr(asyncio, "start_unix_server"), reason="Unix control socket requires Linux"
)
@pytest.mark.asyncio
async def test_unlock_lock_and_control_socket(tmp_path: Path, fake_kernel) -> None:
    paths = _paths(tmp_path)
    Vault(paths.data_root / "vault.json", params=KdfParams(1, 8192, 1)).create("password")

    def builder(p: AppPaths, key):
        return build_server_runtime(p, key, kernel=fake_kernel)

    server = ServerLifecycle(
        paths,
        web_config=WebConfig(host="127.0.0.1", port=0),
        control_socket=tmp_path / "server.sock",
        runtime_builder=builder,
    )
    try:
        await server.start()
        assert await send_control(tmp_path / "server.sock", "status") == "locked"
        assert await send_control(tmp_path / "server.sock", "unlock", "bad") == "failed"
        assert server.state is ServerState.LOCKED
        assert await send_control(tmp_path / "server.sock", "unlock", "password") == "ok"
        assert server.state is ServerState.UNLOCKED
        assert server.web is not None
        pairing = server.web.open_pairing()
        issued = server.web.auth.pair(ticket=str(pairing["ticket"]))
        assert issued["status"] == "paired"
        assert await send_control(tmp_path / "server.sock", "lock") == "ok"
        assert server.state is ServerState.LOCKED
        assert server.web is not None
        assert server.web.auth.list_devices() == []
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_server_runtime_uses_process_isolated_storage(tmp_path: Path, fake_kernel) -> None:
    paths = _paths(tmp_path)
    Vault(paths.data_root / "vault.json", params=KdfParams(1, 8192, 1)).create("password")
    runtime = build_server_runtime(
        paths, Vault(paths.data_root / "vault.json").unlock("password"), kernel=fake_kernel
    )
    assert runtime.controller.coordinator().storage_worker is runtime.storage_process
    await runtime.close()


@pytest.mark.asyncio
async def test_headless_runtime_can_complete_one_streaming_turn(
    tmp_path: Path, fake_kernel
) -> None:
    paths = _paths(tmp_path)
    Vault(paths.data_root / "vault.json", params=KdfParams(1, 8192, 1)).create("password")
    runtime: ServerRuntime = build_server_runtime(
        paths, Vault(paths.data_root / "vault.json").unlock("password"), kernel=fake_kernel
    )
    await runtime.start()
    try:
        run_id = await runtime.controller.send("hello from linux")
        assert run_id
        fake_kernel.say("hello back")
        fake_kernel.emit("run.settled", {})
        await runtime.controller.wait_idle()
        state = await runtime.facade.state()
        assert state["available"] is True
        assert state["busy"] is False
    finally:
        await runtime.close()
