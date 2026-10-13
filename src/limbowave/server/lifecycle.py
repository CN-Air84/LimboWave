"""Lifecycle state machine for the locked-first Linux server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from limbowave.bootstrap import AppPaths
from limbowave.infrastructure.crypto.vault import InvalidPassword, Vault, VaultError, VaultKey
from limbowave.web.security import WebConfig
from limbowave.web.server import WebServer

from .auth import PersistentAuthStore
from .composition import ServerRuntime, build_server_runtime
from .control import ControlServer

_LOG = logging.getLogger(__name__)


class ServerState(StrEnum):
    LOCKED = "locked"
    UNLOCKED = "unlocked"
    STOPPED = "stopped"


@dataclass(slots=True)
class _LockedFacade:
    epoch: str

    async def state(self) -> dict[str, object]:
        return {
            "server_epoch": self.epoch,
            "revision": 0,
            "available": False,
            "busy": False,
            "conversation_id": None,
            "branch_id": None,
            "run_id": None,
            "seq": 0,
            "stream": None,
        }

    def rotate_epoch(self) -> str:
        self.epoch = uuid4().hex
        return self.epoch

    def invalidate(self) -> None:
        return None


class ServerLifecycle:
    """Owns the locked web listener, control socket, vault and unlocked runtime."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        web_config: WebConfig | None = None,
        control_socket: Path | None = None,
        runtime_builder: Callable[[AppPaths, VaultKey], ServerRuntime] = build_server_runtime,
    ) -> None:
        self.paths = paths
        self.vault = Vault(paths.data_root / "vault.json")
        self.web_config = web_config or WebConfig(host="127.0.0.1")
        self.control_socket = control_socket or paths.data_root / "server.sock"
        self._runtime_builder = runtime_builder
        self._runtime: ServerRuntime | None = None
        self._web: WebServer | None = None
        self._control: ControlServer | None = None
        self._state = ServerState.STOPPED
        self._lock = asyncio.Lock()
        self._epoch = uuid4().hex

    @property
    def state(self) -> ServerState:
        return self._state

    @property
    def locked(self) -> bool:
        return self._state is not ServerState.UNLOCKED

    @property
    def web(self) -> WebServer | None:
        return self._web

    async def start(self) -> None:
        async with self._lock:
            if self._state is not ServerState.STOPPED:
                return
            self.paths.data_root.mkdir(parents=True, exist_ok=True)
            if hasattr(asyncio, "start_unix_server"):
                self._control = ControlServer(self.control_socket, self._handle_control)
                await self._control.start()
            await self._start_locked_web()
            self._state = ServerState.LOCKED

    async def _start_locked_web(self) -> None:
        facade = _LockedFacade(self._epoch)
        web = WebServer(facade, self.web_config)
        # Locked mode must never expose pairing or an authenticated data route.
        web.app = None
        await web.start_with_lock_state(lambda: self.locked)
        self._web = web

    async def unlock(self, password: str) -> bool:
        async with self._lock:
            if self._state is not ServerState.LOCKED or not password:
                return False
            try:
                key = self.vault.unlock(password)
                runtime = self._runtime_builder(self.paths, key)
                await runtime.start()
            except (InvalidPassword, VaultError):
                self.vault.lock()
                return False
            except Exception:
                _LOG.exception("server.unlock_failed")
                if "runtime" in locals():
                    with contextlib.suppress(Exception):
                        await runtime.close()
                self.vault.lock()
                return False
            await self._stop_web()
            self._runtime = runtime
            self._epoch = runtime.facade.epoch
            auth = PersistentAuthStore(self.paths.data_root / "limbowave.db")
            web = WebServer(runtime.facade, self.web_config, auth=auth)
            await web.start()
            self._web = web
            self._state = ServerState.UNLOCKED
            return True

    async def lock(self) -> bool:
        async with self._lock:
            if self._state is not ServerState.UNLOCKED:
                return False
            runtime = self._runtime
            self._runtime = None
            await self._stop_web()
            if runtime is not None:
                with contextlib.suppress(Exception):
                    await runtime.close()
            self.vault.lock()
            self._epoch = uuid4().hex
            await self._start_locked_web()
            self._state = ServerState.LOCKED
            return True

    async def close(self) -> None:
        async with self._lock:
            if self._state is ServerState.STOPPED:
                return
            await self._stop_web()
            if self._runtime is not None:
                with contextlib.suppress(Exception):
                    await self._runtime.close()
                self._runtime = None
            self.vault.lock()
            if self._control is not None:
                await self._control.close()
                self._control = None
            self._state = ServerState.STOPPED

    async def _stop_web(self) -> None:
        if self._web is not None:
            await self._web.stop()
            self._web = None

    async def run_forever(self) -> None:
        await self.start()
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in ("SIGTERM", "SIGINT"):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(getattr(signal, name), stopped.set)
        await stopped.wait()
        await self.close()

    async def _handle_control(self, command: str, payload: str | None) -> str:
        if command == "status":
            return self.state.value
        if command in {"pair", "devices", "revoke"}:
            if self._state is not ServerState.UNLOCKED or self._web is None:
                return "failed"
            import json

            if command == "pair":
                return json.dumps(self._web.open_pairing(), ensure_ascii=False)
            if command == "devices":
                return json.dumps(self._web.list_devices(), ensure_ascii=False)
            if not payload:
                return "failed"
            self._web.revoke(payload)
            return "ok"
        if command == "unlock":
            return "ok" if await self.unlock(payload or "") else "failed"
        if command == "lock":
            return "ok" if await self.lock() else "failed"
        if command == "stop":
            await self.close()
            return "ok"
        return "failed"
