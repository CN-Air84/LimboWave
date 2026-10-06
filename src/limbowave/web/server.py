"""Controlled Uvicorn task on the desktop's existing asyncio/qasync loop."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import ipaddress
import socket
from collections.abc import Generator
from dataclasses import replace
from typing import Any

import uvicorn

from .app import create_app
from .auth import AuthStore
from .password_gate import PasswordGate
from .security import WebConfig


class WebStartupError(RuntimeError):
    """Safe desktop-facing classification, with no paths or raw OS diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code
        descriptions = {
            "port_in_use": "端口已被占用，请关闭占用程序或选择其他端口",
            "address_unavailable": "所选 IPv4 地址不属于当前可用网卡",
            "permission_denied": "系统拒绝监听此地址或端口",
            "startup_failed": "Web startup failed; 服务未能启动，请检查网络和证书配置",
        }
        super().__init__(descriptions.get(code, descriptions["startup_failed"]))


class EmbeddedServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        # The desktop owns process signals. Never register or replay them.
        yield


class WebServer:
    def __init__(
        self,
        facade: Any,
        config: WebConfig | None = None,
        *,
        password_gate: PasswordGate | None = None,
    ) -> None:
        self.facade = facade
        self.password_gate = password_gate
        self.config = config or WebConfig()
        self.auth = AuthStore()
        self.app: Any = None
        self._server: EmbeddedServer | None = None
        self._task: asyncio.Task[None] | None = None
        self._socket: socket.socket | None = None
        self._lock = asyncio.Lock()
        self.url = ""

    @property
    def running(self) -> bool:
        return bool(
            self._server
            and self._server.started
            and self._task
            and not self._task.done()
            and self.app.state.accepting
        )

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            await self._stop()
            self.config.validate()
            if (
                not ipaddress.ip_address(self.config.host).is_loopback
                and self.password_gate is None
            ):
                raise ValueError("局域网访问必须配置资料库密码核验")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket = sock
            try:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind((self.config.host, self.config.port))
                sock.listen(64)
                sock.setblocking(False)
                active = replace(self.config, port=sock.getsockname()[1])
                self.facade.rotate_epoch()
                self.app = create_app(self.facade, active, self.auth, self.password_gate)
                settings = uvicorn.Config(
                    self.app,
                    host=active.host,
                    port=active.port,
                    ssl_certfile=active.certfile,
                    ssl_keyfile=active.keyfile,
                    access_log=False,
                    log_config=None,
                    log_level="critical",
                    proxy_headers=False,
                    server_header=False,
                    ws="none",
                    lifespan="off",
                    timeout_keep_alive=5,
                    timeout_graceful_shutdown=2,
                    h11_max_incomplete_event_size=16384,
                )
                self._server = EmbeddedServer(settings)
                self._task = asyncio.create_task(
                    self._serve(self._server, sock), name="limbowave-web"
                )
                async with asyncio.timeout(5):
                    while not self._server.started:
                        if self._task.done():
                            await self._task
                            raise RuntimeError("Web startup failed")
                        await asyncio.sleep(0.01)
                self.url = active.origin
            except BaseException as exc:
                await self._stop()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                code = "startup_failed"
                if isinstance(exc, OSError):
                    number = exc.errno
                    if number in (errno.EADDRINUSE, 10048):
                        code = "port_in_use"
                    elif number in (errno.EADDRNOTAVAIL, 10049):
                        code = "address_unavailable"
                    elif number in (errno.EACCES, 10013):
                        code = "permission_denied"
                raise WebStartupError(code) from None

    async def _serve(self, server: EmbeddedServer, sock: socket.socket) -> None:
        try:
            await server.serve(sockets=[sock])
        except (Exception, SystemExit):
            # Never propagate Uvicorn bind/config diagnostics or SystemExit into Qt.
            pass
        finally:
            self.invalidate()

    def invalidate(self) -> None:
        if self.app is not None:
            self.app.state.accepting = False
        self.auth.invalidate()
        if self.password_gate is not None:
            self.password_gate.clear()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop()

    async def _stop(self) -> None:
        self.invalidate()
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=4)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            except Exception:
                pass
        if self._socket:
            self._socket.close()
        self._socket = None
        self._server = None
        self._task = None
        self.url = ""

    def open_pairing(self) -> dict[str, object]:
        if not self.running:
            raise RuntimeError("Web service is not running")
        result = self.auth.open_pairing()
        return {**result, "url": f"{self.url}/#ticket={result['ticket']}"}

    def list_devices(self) -> list[dict[str, str]]:
        return self.auth.list_devices()

    def list_pending(self) -> list[dict[str, str]]:
        return self.auth.list_pending()

    def revoke(self, device_id: str) -> None:
        self.auth.revoke(device_id)

    def approve(self, request_id: str) -> None:
        if not self.running:
            raise RuntimeError("Web service is not running")
        self.auth.approve(request_id)
