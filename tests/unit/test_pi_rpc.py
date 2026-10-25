from __future__ import annotations

import asyncio
import json
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


@pytest.fixture
def managed_install(tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    shim = agent / "bin" / "pi.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text('@ECHO off\nnode "%~dp0pi-launcher.js" %*', encoding="utf-8")
    install = agent / "install"
    install.mkdir()
    (install / "current-version").write_text("1.1.0\n", encoding="utf-8")
    package = install / "releases/1.1.0/node_modules/@earendil-works/pi-coding-agent"
    package.mkdir(parents=True)
    monkeypatch.setattr(
        pi_rpc.shutil, "which", lambda name: str(shim) if name == "pi.cmd" else None,
    )
    monkeypatch.setattr(pi_rpc.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return agent, package


@pytest.mark.parametrize("bin_value", ["dist/custom-cli.js", {"pi": "dist/custom-cli.js"}])
def test_cli_discovery_reads_active_managed_release(managed_install, bin_value):
    agent, package = managed_install
    (package / "package.json").write_text(json.dumps({"bin": bin_value}), encoding="utf-8")
    cli = package / "dist/custom-cli.js"
    cli.parent.mkdir()
    cli.touch()
    # A leftover global layout must not override the release selected by the launcher.
    stale = agent / "bin/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
    stale.parent.mkdir(parents=True)
    stale.touch()
    argv = pi_rpc.resolve_pi_argv(node="node", extra_args=["--mode", "rpc"])
    assert argv == ["node", str(cli), "--mode", "rpc"]


@pytest.mark.parametrize("version", ["", ".", "..", "../old", "old/release", "C:\\old", "/old"])
def test_cli_discovery_rejects_invalid_managed_version(managed_install, version):
    agent, _package = managed_install
    (agent / "install/current-version").write_text(version, encoding="utf-8")
    with pytest.raises(RuntimeError, match="Pi CLI"):
        pi_rpc._locate_cli()


@pytest.mark.parametrize("manifest", [
    "{broken", "[]", '{"bin": null}', '{"bin": {"pi": 123}}',
    '{"bin": "../../outside.js"}', '{"bin": "dist/missing.js"}',
])
def test_cli_discovery_rejects_invalid_managed_entry(managed_install, manifest):
    _agent, package = managed_install
    (package / "package.json").write_text(manifest, encoding="utf-8")
    (package.parent.parent / "outside.js").touch()
    with pytest.raises(RuntimeError, match="Pi CLI"):
        pi_rpc._locate_cli()


def test_cli_discovery_follows_changed_managed_release(managed_install):
    agent, package = managed_install
    (package / "package.json").write_text('{"bin": "cli.js"}', encoding="utf-8")
    (package / "cli.js").touch()
    assert pi_rpc._locate_cli() == package / "cli.js"
    newer = agent / "install/releases/1.2.0/node_modules/@earendil-works/pi-coding-agent"
    newer.mkdir(parents=True)
    (newer / "package.json").write_text('{"bin": {"pi": "cli.js"}}', encoding="utf-8")
    (newer / "cli.js").touch()
    (agent / "install/current-version").write_text("1.2.0", encoding="utf-8")
    assert pi_rpc._locate_cli() == newer / "cli.js"


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


@pytest.mark.asyncio
async def test_stdout_burst_yields_without_reordering_events() -> None:
    from types import SimpleNamespace

    reader = asyncio.StreamReader()
    count = 1_000
    reader.feed_data(b"".join(
        f'{{"type":"event","index":{index}}}\n'.encode() for index in range(count)
    ))
    reader.feed_eof()
    rpc = PiRpcProcess(SpawnSpec(argv=[]))
    rpc._proc = SimpleNamespace(stdout=reader, pid=0, returncode=0)
    rpc._expected_exit = True
    received = []
    opportunities = []
    rpc.on_event(lambda event: received.append(event["index"]) if "index" in event else None)

    async def input_task() -> None:
        while len(received) < count:
            await asyncio.sleep(0)
            if 0 < len(received) < count:
                opportunities.append(len(received))

    task = asyncio.create_task(input_task())
    await rpc._read_stdout()
    await task
    assert received == list(range(count))
    assert opportunities, "A buffered read/inline async decoder must not monopolize the UI loop"
    assert max(b - a for a, b in zip(
        [0, *opportunities], [*opportunities, count], strict=True
    )) <= 128


@pytest.mark.asyncio
async def test_fragmented_stdout_preserves_byte_framing_and_trailing_frame() -> None:
    import json
    from types import SimpleNamespace

    messages = [{"type": "event", "text": "🙂\u2028\u2029" + "x" * 150_000},
                {"type": "event", "text": "tail"}]
    data = b"\r\n" + b"\r\n".join(
        json.dumps(message, ensure_ascii=False).encode("utf-8") for message in messages
    )

    class Reader:
        offset = 0

        async def read(self, _size: int) -> bytes:
            chunk = data[self.offset:self.offset + 997]
            self.offset += len(chunk)
            return chunk

    rpc = PiRpcProcess(SpawnSpec(argv=[]))
    rpc._proc = SimpleNamespace(stdout=Reader(), pid=0, returncode=0)
    rpc._expected_exit = True
    received = []
    rpc.on_event(received.append)
    await rpc._read_stdout()
    assert received[:-1] == messages
    assert received[-1]["type"] == "process_exit"
