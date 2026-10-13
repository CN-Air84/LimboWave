from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from limbowave.bootstrap import AppPaths
from limbowave.server.lifecycle import ServerLifecycle, ServerState


@pytest.mark.asyncio
async def test_local_control_lists_and_revokes_devices(tmp_path: Path) -> None:
    revoked: list[str] = []
    web = SimpleNamespace(
        list_devices=lambda: [{"device_id": "device", "name": "browser"}],
        revoke=revoked.append,
        open_pairing=lambda: {"ticket": "secret", "code": "12345678"},
    )
    server = ServerLifecycle(AppPaths(tmp_path, tmp_path / "logs"))
    server._state = ServerState.UNLOCKED
    server._web = web

    devices = json.loads(await server._handle_control("devices", None))
    assert devices == [{"device_id": "device", "name": "browser"}]
    assert await server._handle_control("revoke", "device") == "ok"
    assert revoked == ["device"]


@pytest.mark.asyncio
async def test_local_control_refuses_pairing_while_locked(tmp_path: Path) -> None:
    server = ServerLifecycle(AppPaths(tmp_path, tmp_path / "logs"))
    server._state = ServerState.LOCKED
    assert await server._handle_control("pair", None) == "failed"
    assert await server._handle_control("devices", None) == "failed"
