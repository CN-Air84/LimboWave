from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from mcp.types import ListToolsResult, Tool
from pydantic import ValidationError

from limbowave.application.services.mcp_service import McpService
from limbowave.domain.mcp import McpConfiguration, McpError, McpServer
from limbowave.infrastructure.mcp_client import (
    _catalog,
    _environment_refs,
    _exchange,
    _validate_schema,
)


def server(**overrides):
    return McpServer(id="local", name="Local", command="python", **overrides)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "../bad"},
        {"command": ""},
        {"timeout_seconds": 0},
        {"timeout_seconds": 46},
        {"enabled": "yes"},
        {"args": ["line\nbreak"]},
        {"env_refs": {"TOKEN": "actual secret"}},
        {"env_refs": {"BAD=KEY": "TOKEN"}},
        {"url": "https://example.org/mcp"},
        {"header_refs": {"Authorization": "TOKEN"}},
        {"unknown": "value"},
    ],
)
def test_invalid_stdio(changes):
    data = server().model_dump()
    data.update(changes)
    with pytest.raises(ValidationError):
        McpServer.model_validate(data)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/mcp",
        "https://user:password@example.org/mcp",
        "file:///tmp/x",
        "https://example.org/mcp?token=secret",
        "https://example.org/mcp#frag",
        "https://example.org:bad/mcp",
        "https://example.org/with space",
        "https://",
    ],
)
def test_invalid_remote_urls(url):
    with pytest.raises(ValidationError):
        McpServer(id="remote", name="Remote", transport="streamable_http", url=url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:123/mcp",
        "http://127.0.0.1/mcp",
        "http://[::1]/mcp",
        "https://example.org/mcp",
    ],
)
def test_http_urls(url):
    assert McpServer(id="remote", name="Remote", transport="streamable_http", url=url)


def test_default_disabled_and_configuration_roundtrip(tmp_path):
    service = McpService(tmp_path / "mcp.json")
    assert service.configuration().servers == []
    service.save(McpConfiguration(servers=[server()]))
    assert not service.available()
    assert service.servers() == {"servers": []}
    with pytest.raises(McpError, match="停用"):
        service.execute("local")
    service.save(McpConfiguration(servers=[server(enabled=True)]))
    assert service.available()
    assert service.configuration().servers[0].enabled
    assert not list(tmp_path.glob("*.tmp"))
    assert "command" not in json.dumps(service.servers())


def test_corrupt_configuration_is_not_replaced(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("invalid-secret", encoding="utf8")
    service = McpService(path)
    with pytest.raises(McpError) as error:
        service.configuration()
    assert "invalid-secret" not in str(error.value)
    assert not service.available()
    assert path.read_text() == "invalid-secret"


def test_atomic_save_preserves_old_file_on_failure(tmp_path, monkeypatch):
    service = McpService(tmp_path / "mcp.json")
    service.save(McpConfiguration(servers=[server()]))

    def fail(*args):
        raise OSError("cannot replace")

    monkeypatch.setattr("limbowave.application.services.mcp_service.os.replace", fail)
    with pytest.raises(OSError):
        service.save(McpConfiguration())
    assert len(service.configuration().servers) == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_duplicate_ids_and_headers_rejected():
    with pytest.raises(ValidationError):
        McpConfiguration(servers=[server(), server()])
    with pytest.raises(ValidationError):
        McpServer(
            id="r",
            name="R",
            transport="streamable_http",
            url="https://example.org",
            header_refs={"Authorization": "A", "authorization": "B"},
        )


def test_environment_references_are_resolved_without_leaking(monkeypatch):
    monkeypatch.setenv("MCP_TEST_SECRET", "Bearer secret-value")
    assert _environment_refs({"Authorization": "MCP_TEST_SECRET"}) == {
        "Authorization": "Bearer secret-value"
    }
    monkeypatch.setenv("MCP_TEST_SECRET", "secret\r\nInjected: yes")
    with pytest.raises(McpError) as error:
        _environment_refs({"Authorization": "MCP_TEST_SECRET"})
    assert "secret" not in str(error.value)
    monkeypatch.delenv("MCP_TEST_SECRET")
    with pytest.raises(McpError):
        _environment_refs({"Authorization": "MCP_TEST_SECRET"})


def test_service_validates_arguments_before_transport(tmp_path):
    operation = AsyncMock(return_value={"tools": []})
    service = McpService(tmp_path / "mcp.json", operation=operation)
    service.save(McpConfiguration(servers=[server(enabled=True)]))
    for args in ([], {"n": float("nan")}, {"s": "x" * 70000}):
        with pytest.raises(McpError):
            service.execute("local", "echo", args)
    operation.assert_not_called()
    assert service.execute("local") == {"tools": []}
    operation.assert_awaited_once()


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://attacker.invalid/schema"},
        {"type": "not-a-type"},
        {"properties": {"x": {"$id": "https://attacker.invalid"}}},
    ],
)
def test_unsafe_schema_rejected(schema):
    with pytest.raises(McpError):
        _validate_schema(schema)


async def test_catalog_paginates_and_rejects_loops():
    client = AsyncMock()
    client.list_tools.side_effect = [
        ListToolsResult(tools=[Tool(name="a", inputSchema={"type": "object"})], nextCursor="next"),
        ListToolsResult(tools=[Tool(name="b", inputSchema={"type": "object"})]),
    ]
    assert [t.name for t in await _catalog(client)] == ["a", "b"]
    client.list_tools.assert_any_await(cursor="next")
    client.list_tools.side_effect = None
    client.list_tools.return_value = ListToolsResult(tools=[], nextCursor="loop")
    with pytest.raises(McpError, match="循环"):
        await _catalog(client)


async def test_catalog_rejects_duplicate_names():
    client = AsyncMock()
    tool = Tool(name="x", inputSchema={"type": "object"})
    client.list_tools.return_value = ListToolsResult(tools=[tool, tool])
    with pytest.raises(McpError):
        await _catalog(client)


async def test_schema_error_and_missing_tool_never_execute():
    client = AsyncMock()
    client.list_tools.return_value = ListToolsResult(
        tools=[
            Tool(
                name="echo",
                inputSchema={
                    "type": "object",
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}},
                },
            )
        ]
    )
    for name, args in [("echo", {"text": 10}), ("missing", {})]:
        with pytest.raises(McpError):
            await _exchange(client, name, args)
    client.call_tool.assert_not_awaited()


async def test_redirect_rejected_before_sdk_can_replay_post():
    import httpx

    from limbowave.infrastructure.mcp_client import _reject_redirect

    response = httpx.Response(307, headers={"location": "https://example.org/secret"},
                              request=httpx.Request("POST", "https://example.org/mcp"))
    with pytest.raises(McpError) as error:
        await _reject_redirect(response)
    assert "secret" not in str(error.value)
    assert response.is_closed


def test_transport_log_filter_is_scoped_and_never_formats_payload():
    import logging

    from limbowave.infrastructure.mcp_client import _LOG_FILTER, _LOG_SCOPE

    record = logging.LogRecord("httpx", logging.INFO, "", 1, "token=secret", (), None)
    assert _LOG_FILTER.filter(record)
    token = _LOG_SCOPE.set(True)
    try:
        assert not _LOG_FILTER.filter(record)
    finally:
        _LOG_SCOPE.reset(token)
    assert _LOG_FILTER.filter(record)
