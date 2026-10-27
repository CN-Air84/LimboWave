"""Bounded MCP SDK sessions; no shell, implicit retry, logging or persistent secrets."""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import timedelta
from typing import Any

import anyio
import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool

from limbowave.application.services.mcp_service import mcp_cancelled
from limbowave.application.services.network_privacy import check_network_destination
from limbowave.domain.mcp import MAX_CATALOG_BYTES, MAX_TOOLS, McpError, McpServer

_LOG_SCOPE: ContextVar[bool] = ContextVar("mcp_transport_log_scope", default=False)


class _PrivateTransportLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # SDK diagnostics can contain raw responses, full URLs and session credentials.
        # Filters are context-local: unrelated HTTP/application logging stays intact.
        return not _LOG_SCOPE.get()


_LOG_FILTER = _PrivateTransportLogs()
for _logger in ("", "client", "mcp.client.stdio", "mcp.client.streamable_http", "httpx",
                "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy"):
    logging.getLogger(_logger).addFilter(_LOG_FILTER)


async def _check_request(request: httpx.Request) -> None:
    check_network_destination(str(request.url))


async def _reject_redirect(response: httpx.Response) -> None:
    # The SDK follows same-origin 307/308 itself, even with follow_redirects=False.
    # Reject before its manual redirect code can replay a mutating POST.
    if 300 <= response.status_code < 400:
        await response.aclose()
        raise McpError("MCP 地址发生重定向，已停止；请在设置中填写最终地址。")


def _environment_refs(refs: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, ref in refs.items():
        value = os.environ.get(ref)
        if value is None or not value or any(c in value for c in "\r\n\x00"):
            raise McpError("MCP 所需环境变量缺失或无效；请配置后重新启动应用。")
        values[key] = value
    return values


def _validate_schema(schema: dict[str, Any]) -> None:
    # Never resolve remote $refs (or permit validation to do unexpected network I/O).
    todo: list[Any] = [schema]
    count = 0
    while todo:
        node = todo.pop()
        count += 1
        if count > 10000:
            raise McpError("MCP 参数模式过于复杂。")
        if isinstance(node, dict):
            for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                if key in node and (
                    not isinstance(node[key], str) or not node[key].startswith("#")
                ):
                    raise McpError("MCP 参数模式包含外部引用，已拒绝。")
            # Nested resource IDs can change the resolution base of local refs.
            if "$id" in node and node is not schema:
                raise McpError("MCP 参数模式包含嵌套资源标识，已拒绝。")
            todo.extend(node.values())
        elif isinstance(node, list):
            todo.extend(node)
    try:
        Draft202012Validator.check_schema(schema)
    except (SchemaError, RecursionError):
        raise McpError("MCP 工具参数模式无效。") from None


async def _catalog(session: ClientSession) -> list[Tool]:
    tools: list[Tool] = []
    cursors: set[str] = set()
    names: set[str] = set()
    cursor: str | None = None
    size = 0
    for _ in range(20):
        page = await session.list_tools(cursor=cursor)
        size += len(page.model_dump_json().encode("utf-8"))
        if size > MAX_CATALOG_BYTES or len(tools) + len(page.tools) > MAX_TOOLS:
            raise McpError("MCP 工具目录超过上限；请减少服务器暴露的工具。")
        for tool in page.tools:
            if (
                not tool.name.strip()
                or len(tool.name) > 128
                or tool.name in names
                or any(ord(c) < 32 for c in tool.name)
            ):
                raise McpError("MCP 工具目录含重复或无效名称。")
            names.add(tool.name)
            tools.append(tool)
        cursor = page.nextCursor
        if cursor is None:
            return tools
        if cursor in cursors:
            raise McpError("MCP 工具目录分页循环，已停止。")
        cursors.add(cursor)
    raise McpError("MCP 工具目录分页超过上限。")


async def _exchange(
    session: ClientSession, tool_name: str | None, arguments: dict[str, Any]
) -> dict[str, Any]:
    await session.initialize()
    tools = await _catalog(session)
    if tool_name is None:
        return {
            "tools": [
                {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema}
                for t in tools
            ]
        }
    tool = next((item for item in tools if item.name == tool_name), None)
    if tool is None:
        raise McpError("MCP 工具不存在；请重新获取工具目录。")
    _validate_schema(tool.inputSchema)
    try:
        Draft202012Validator(tool.inputSchema).validate(arguments)
    except (ValidationError, RecursionError):
        raise McpError("MCP 参数不符合服务器声明的 inputSchema，未执行工具。") from None
    result = await session.call_tool(tool_name, arguments)
    # No automatic dereference of resource links or downloads of binary content.
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


async def perform(server: McpServer, tool: str | None, arguments: dict[str, Any]) -> dict[str, Any]:
    if mcp_cancelled():
        raise McpError("MCP 调用所属运行已停止，未启动服务器。")
    result: dict[str, Any] | None = None
    failure: McpError | None = None
    async with anyio.create_task_group() as group:
        async def watch() -> None:
            while not mcp_cancelled():
                await anyio.sleep(0.05)
            group.cancel_scope.cancel()
        group.start_soon(watch)
        try:
            result = await _perform_session(server, tool, arguments)
        except McpError as exc:
            failure = exc
        finally:
            group.cancel_scope.cancel()
    if failure is not None:
        raise failure
    if result is None or mcp_cancelled():
        raise McpError("MCP 调用已取消，连接已关闭；外部操作可能已发生，请先核对状态。")
    return result


async def _perform_session(
    server: McpServer, tool: str | None, arguments: dict[str, Any],
) -> dict[str, Any]:
    log_token = _LOG_SCOPE.set(True)
    try:
        # anyio scopes surround SDK task groups, and exit in the same task/order.
        with anyio.fail_after(server.timeout_seconds):
            async with AsyncExitStack() as stack:
                if server.transport == "stdio":
                    environment = _environment_refs(server.env_refs)
                    error_sink = stack.enter_context(open(os.devnull, "w"))  # noqa: SIM115
                    streams = await stack.enter_async_context(
                        stdio_client(
                            StdioServerParameters(
                                command=server.command,
                                args=server.args,
                                cwd=server.cwd,
                                env=environment,
                            ),
                            errlog=error_sink,
                        )
                    )
                    read, write = streams
                else:
                    check_network_destination(server.url)
                    headers = _environment_refs(server.header_refs)
                    client = await stack.enter_async_context(
                        httpx.AsyncClient(
                            headers=headers,
                            timeout=httpx.Timeout(server.timeout_seconds),
                            follow_redirects=False,
                            trust_env=False,
                            event_hooks={"request": [_check_request],
                                         "response": [_reject_redirect]},
                        )
                    )
                    read, write, _ = await stack.enter_async_context(
                        streamable_http_client(
                            server.url,
                            http_client=client,
                        )
                    )
                session = await stack.enter_async_context(
                    ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timedelta(seconds=server.timeout_seconds),
                    )
                )
                result = await _exchange(session, tool, arguments)
                return {"server_id": server.id, **result}
    except McpError:
        raise
    except TimeoutError:
        raise McpError(
            "MCP 操作超时，连接已关闭；调用可能已生效，请核对后再决定是否重试。"
        ) from None
    except Exception:
        # ExceptionGroups may include headers, subprocess args or remote response bodies.
        raise McpError(
            "MCP 连接或调用失败；请检查服务器、认证环境变量与参数。未自动重试。"
        ) from None
    finally:
        _LOG_SCOPE.reset(log_token)
