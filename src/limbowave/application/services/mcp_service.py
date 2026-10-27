"""Atomic server settings and synchronous facade for gateway/UI worker threads."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import Event, RLock
from typing import Any

from limbowave.domain.mcp import MAX_ARGUMENT_BYTES, McpConfiguration, McpError, McpServer

_CANCELLED: ContextVar[Event | None] = ContextVar("mcp_cancelled", default=None)


@contextmanager
def mcp_run_scope(cancelled: Event) -> Iterator[None]:
    token = _CANCELLED.set(cancelled)
    try:
        yield
    finally:
        _CANCELLED.reset(token)


def mcp_cancelled() -> bool:
    event = _CANCELLED.get()
    return event is not None and event.is_set()


Operation = Callable[[McpServer, str | None, dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


class McpService:
    def __init__(self, path: Path, *, operation: Operation | None = None) -> None:
        self._path = path
        self._lock = RLock()
        self._operation = operation

    def configuration(self) -> McpConfiguration:
        with self._lock:
            if not self._path.exists():
                return McpConfiguration()
            try:
                if self._path.stat().st_size > 1024 * 1024:
                    raise ValueError
                return McpConfiguration.model_validate_json(self._path.read_bytes())
            except (OSError, ValueError):
                raise McpError("MCP 配置无法读取；请修复配置，不会启动任何服务器。") from None

    def save(self, config: McpConfiguration) -> None:
        config = McpConfiguration.model_validate(config.model_dump())
        payload = config.model_dump_json(indent=2).encode("utf-8") + b"\n"
        if len(payload) > 1024 * 1024:
            raise McpError("MCP 配置过大，未保存。")
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".mcp-", suffix=".tmp", dir=self._path.parent)
            try:
                with os.fdopen(fd, "wb") as target:
                    target.write(payload)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, self._path)
            finally:
                Path(temporary).unlink(missing_ok=True)

    def resolve(self, server_id: str) -> McpServer:
        for server in self.configuration().servers:
            if server.id == server_id:
                if not server.enabled:
                    raise McpError("MCP 服务器已停用；请在设置中显式启用。")
                return server
        raise McpError("MCP 服务器不存在；请先在设置中添加。")

    def available(self) -> bool:
        try:
            return any(server.enabled for server in self.configuration().servers)
        except McpError:
            return False

    def servers(self) -> dict[str, Any]:
        # A safe inventory never launches processes or reveals paths/env/header refs.
        return {
            "servers": [
                {"id": s.id, "name": s.name, "transport": s.transport}
                for s in self.configuration().servers
                if s.enabled
            ]
        }

    def execute(
        self, server_id: str, tool: str | None = None, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if mcp_cancelled():
            raise McpError("MCP 调用所属运行已停止，未启动服务器。")
        server = self.resolve(server_id)
        if tool is not None and (
            not tool.strip() or len(tool) > 128 or any(ord(c) < 32 for c in tool)
        ):
            raise McpError("MCP 工具名无效。")
        arguments = {} if arguments is None else arguments
        try:
            encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES:
                raise ValueError
        except (TypeError, ValueError, RecursionError):
            raise McpError("MCP 参数不是有效 JSON 对象或超过大小上限。") from None
        if not isinstance(arguments, dict):
            raise McpError("MCP 参数必须是 JSON 对象。")
        operation = self._operation
        if operation is None:
            raise McpError("MCP 传输未装配，未启动服务器。")
        # Called only in background workers. No long-lived SDK tasks cross event loops.
        return asyncio.run(operation(server, tool, arguments))
