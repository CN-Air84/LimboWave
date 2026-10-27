from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from limbowave.domain.mcp import McpError, McpServer
from limbowave.infrastructure.mcp_client import perform

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mcp_server.py"


def stdio(**kwargs):
    return McpServer(
        id="test", name="Test", enabled=True, command=sys.executable, args=[str(FIXTURE)], **kwargs
    )


async def test_real_stdio_discovery_call_and_error():
    config = stdio()
    catalog = await perform(config, None, {})
    assert {t["name"] for t in catalog["tools"]} == {"echo", "fail", "slow"}
    result = await perform(config, "echo", {"text": "hello MCP 中文"})
    assert result["content"][0]["text"] == "hello MCP 中文"
    assert not result["isError"]
    failed = await perform(config, "fail", {})
    assert failed["isError"]


async def test_transport_failure_is_sanitized():
    with pytest.raises(McpError) as error:
        await perform(
            McpServer(id="bad", name="Bad", command="missing-secret-executable"), None, {}
        )
    assert "missing-secret-executable" not in str(error.value)


async def test_timeout_is_bounded_and_does_not_retry():
    start = time.monotonic()
    with pytest.raises(McpError):
        await perform(stdio(timeout_seconds=3), "slow", {})
    assert time.monotonic() - start < 12


@pytest.fixture
def http_server():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "MCP_TEST_PORT": str(port)}
    process = subprocess.Popen(
        [sys.executable, str(FIXTURE), "--http"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("fixture server stopped")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("fixture server did not start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def test_real_streamable_http(http_server):
    server = McpServer(
        id="http", name="HTTP", enabled=True, transport="streamable_http", url=http_server
    )
    result = await perform(server, "echo", {"text": "http roundtrip"})
    assert result["content"][0]["text"] == "http roundtrip"
