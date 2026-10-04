from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

import pytest

from limbowave.infrastructure import pi_rpc
from limbowave.infrastructure.pi_rpc import (
    PiRpcProcess,
    SpawnSpec,
    _subprocess_creation_kwargs,
)


def test_pi_process_is_headless_on_windows_only() -> None:
    assert _subprocess_creation_kwargs("win32") == {"creationflags": 0x08000000}
    assert _subprocess_creation_kwargs("linux") == {}
    assert _subprocess_creation_kwargs("darwin") == {}


@pytest.mark.asyncio
async def test_large_rpc_json_decode_does_not_block_event_loop(monkeypatch) -> None:
    gui_thread = threading.get_ident()
    decode_threads: list[int] = []

    def slow_decode(_raw: bytes) -> dict[str, object]:
        decode_threads.append(threading.get_ident())
        time.sleep(0.08)
        return {"type": "event"}

    monkeypatch.setattr(pi_rpc, "_decode_frame", slow_decode)
    rpc = PiRpcProcess(SpawnSpec(argv=["unused"]))
    received: list[dict[str, object]] = []
    rpc.on_event(received.append)
    ticks = 0
    running = True

    async def heartbeat() -> None:
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0.005)

    pulse = asyncio.create_task(heartbeat())
    try:
        await rpc._dispatch_async(b"x" * pi_rpc._BACKGROUND_JSON_THRESHOLD)
    finally:
        running = False
        await pulse

    assert ticks >= 5
    assert decode_threads and decode_threads[0] != gui_thread
    assert received == [{"type": "event"}]


@pytest.mark.asyncio
async def test_stderr_reader_accepts_frame_larger_than_asyncio_line_limit() -> None:
    """最终请求日志超过 64 KiB 时仍必须持续排空 stderr。"""
    payload = "x" * 200_000
    received: list[str] = []
    code = (
        "import sys; "
        f"sys.stderr.write('x' * {len(payload)} + '\\n'); "
        "sys.stderr.flush()"
    )
    rpc = PiRpcProcess(
        SpawnSpec(
            argv=[sys.executable, "-c", code],
            stderr_handler=received.append,
        )
    )

    try:
        await rpc.start()
        assert rpc._proc is not None
        await asyncio.wait_for(rpc._proc.wait(), timeout=10)
        assert rpc._stderr_task is not None
        await asyncio.wait_for(rpc._stderr_task, timeout=10)
    finally:
        await rpc.shutdown()

    assert received == [payload + os.linesep]
    assert rpc.stderr_text == payload + os.linesep


def test_cli_discovery_uses_selected_node_prefix(tmp_path, monkeypatch) -> None:
    prefix = tmp_path / "nvm" / "versions" / "node" / "v24.0.0"
    node = prefix / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.touch()
    cli = prefix / "lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
    cli.parent.mkdir(parents=True)
    cli.touch()
    monkeypatch.setattr(pi_rpc.shutil, "which", lambda name: None)
    assert pi_rpc._locate_cli(str(node)) == cli


@pytest.mark.skipif(os.name == "nt", reason="native POSIX symlink installation layout")
def test_cli_discovery_resolves_npm_symlink(tmp_path, monkeypatch) -> None:
    cli = tmp_path / "custom/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
    cli.parent.mkdir(parents=True)
    cli.touch()
    shim = tmp_path / "bin/pi"
    shim.parent.mkdir()
    shim.symlink_to(cli)
    monkeypatch.setattr(pi_rpc.shutil, "which", lambda name: str(shim) if name == "pi" else None)
    assert pi_rpc._locate_cli() == cli
