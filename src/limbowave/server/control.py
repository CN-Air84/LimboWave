"""Owner-only local control socket for unlock/lock operations."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

Handler = Callable[[str, str | None], Awaitable[str]]


class ControlServer:
    def __init__(self, path: Path, handler: Handler) -> None:
        self.path = path
        self.handler = handler
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self.server = await asyncio.start_unix_server(  # type: ignore[attr-defined]
            self._client, path=str(self.path)
        )
        os.chmod(self.path, 0o600)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            command = (await reader.readline()).decode("utf-8").strip()
            payload: str | None = None
            if command == "unlock":
                raw_length = await reader.readline()
                length = int(raw_length.decode("ascii").strip())
                if not 0 < length <= 4096:
                    raise ValueError("invalid password length")
                payload = (await reader.readexactly(length)).decode("utf-8")
            result = await self.handler(command, payload)
            writer.write((result + "\n").encode("utf-8"))
            await writer.drain()
        except Exception:
            writer.write(b"failed\n")
            with contextlib.suppress(Exception):
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


async def send_control(path: Path, command: str, payload: str | None = None) -> str:
    reader, writer = await asyncio.open_unix_connection(str(path))  # type: ignore[attr-defined]
    try:
        writer.write((command + "\n").encode("utf-8"))
        if command == "unlock":
            value = (payload or "").encode("utf-8")
            writer.write((str(len(value)) + "\n").encode("ascii") + value)
        await writer.drain()
        result = await reader.readline()
        if not isinstance(result, bytes):
            raise RuntimeError("invalid control response")
        return result.decode("utf-8").strip()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
