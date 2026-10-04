"""集成测试共用夹具：本地 mock provider。

mock 提供 OpenAI Chat Completions 兼容接口并**记录每次请求的完整请求体与请求头**，
因此"配置有没有正确落到请求上"由日志本身判定，不依赖任何真实密钥。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "pi-verify"
MOCK = TOOLS / "mock_provider.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def mock_provider(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    """起一个 mock provider，返回 (端口, 请求日志路径)。"""
    port = free_port()
    log = tmp_path / "mock.jsonl"
    log.write_text("", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(MOCK), "--port", str(port), "--log", str(log)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    time.sleep(1.5)
    try:
        yield port, log
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
