"""Lazy ownership of opt-in LAN UI, services and visibility-scoped refreshes.

The desktop's normal startup neither imports the web stack nor subscribes a
remote event broker. Qt-free imports run in a worker; loop-bound services and
all widget mutations stay on the owning Qt/asyncio thread.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QWidget

from limbowave.application.background import run_blocking

if TYPE_CHECKING:
    from limbowave.ui.lan_access_panel import LanAccessPanel
    from limbowave.web.server import WebServer


def _load_lan_dependencies() -> None:
    for module in (
        "limbowave.web.server",
        "limbowave.application.services.runtime_facade",
        "limbowave.runtime_composition",
    ):
        importlib.import_module(module)


class LanAccessController(QObject):
    def __init__(
        self, parent: QWidget, *, create_server: Callable[[], WebServer],
        allowed: Callable[[], bool], spawn: Callable[[Coroutine[Any, Any, object]], None],
    ) -> None:
        super().__init__(parent)
        self._host = parent
        self._create_server = create_server
        self._allowed = allowed
        self._spawn = spawn
        self._panel: LanAccessPanel | None = None
        self._server: WebServer | None = None
        self._closed = False
        self._busy = False
        self._generation = 0
        self._lock = asyncio.Lock()
        self._pair_timer = QTimer(self)
        self._pair_timer.setSingleShot(True)
        self._pair_timer.timeout.connect(self._clear_invitation)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self._refresh)

    def panel(self) -> LanAccessPanel:
        if self._panel is None:
            from limbowave.ui.lan_access_panel import LanAccessPanel

            panel = LanAccessPanel(self._host)
            self._panel = panel
            panel.installEventFilter(self)
            panel.destroyed.connect(self._panel_destroyed)
            panel.start_requested.connect(lambda values: self._spawn(self.start(values)))
            panel.stop_requested.connect(lambda: self._spawn(self.stop()))
            panel.pairing_requested.connect(self._pair)
            panel.revoke_requested.connect(self._revoke)
            panel.approve_requested.connect(self._approve)
            panel.refresh_requested.connect(self._refresh)
        return self._panel

    def _panel_destroyed(self) -> None:
        self._panel = None
        self._refresh_timer.stop()
        self._pair_timer.stop()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._panel:
            if event.type() == QEvent.Type.Show:
                self._refresh()
            elif event.type() == QEvent.Type.Hide:
                self._refresh_timer.stop()
        return super().eventFilter(watched, event)

    def _sync_refresh(self) -> None:
        if (
            not self._closed and not self._busy and self._panel is not None
            and self._panel.isVisible() and self._server is not None and self._server.running
        ):
            if not self._refresh_timer.isActive():
                self._refresh_timer.start()
        else:
            self._refresh_timer.stop()

    def _refresh(self) -> None:
        panel, server = self._panel, self._server
        if panel is not None and panel.isVisible() and not self._busy:
            running = server is not None and server.running
            panel.set_running(running, server.url if running and server is not None else "")
            if running and server is not None:
                panel.set_devices(server.list_devices(), server.list_pending())
        self._sync_refresh()

    async def start(self, values: dict[str, Any]) -> None:
        if self._closed or self._busy:
            return
        panel = self.panel()
        if not self._allowed():
            panel.set_running(False, error="请先解锁资料库并完成模型配置。")
            return
        async with self._lock:
            self._busy = True
            generation = self._generation
            panel.set_pending()
            self._refresh_timer.stop()
            try:
                if self._server is None:
                    await run_blocking(_load_lan_dependencies)
                # Reset/close may have happened while importing. Never reopen a
                # listener or construct a subscriber after that lifecycle boundary.
                if self._closed or generation != self._generation or not self._allowed():
                    panel.set_running(False)
                    return
                from limbowave.web.security import WebConfig

                config = WebConfig(**values)
                config.validate()
                if self._server is None:
                    self._server = self._create_server()
                self._server.config = config
                await self._server.start()
                if self._closed or generation != self._generation or not self._allowed():
                    await self._dispose_server()
                    panel.set_running(False)
                else:
                    panel.set_running(self._server.running, self._server.url)
            except asyncio.CancelledError:
                await self._dispose_server()
                panel.set_running(False)
                raise
            except ValueError as exc:
                await self._dispose_server()
                panel.set_running(False, error=f"无法开启：{exc}")
            except Exception:
                await self._dispose_server()
                panel.set_running(False, error="无法开启：请检查网卡、端口占用及证书/私钥。")
            finally:
                self._busy = False
                self._sync_refresh()

    async def _dispose_server(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            try:
                await server.stop()
            finally:
                await server.facade.close()

    async def stop(self) -> None:
        self._generation += 1
        self._refresh_timer.stop()
        self._pair_timer.stop()
        async with self._lock:
            self._busy = True
            try:
                await self._dispose_server()
            finally:
                self._busy = False
                if self._panel is not None:
                    self._panel.set_running(False)

    def invalidate(self) -> None:
        self._generation += 1
        self._refresh_timer.stop()
        self._pair_timer.stop()
        if self._server is not None:
            self._server.invalidate()
            self._server.facade.invalidate()
        self._clear_invitation()

    async def close(self) -> None:
        self._closed = True
        self.invalidate()
        await self.stop()

    def _clear_invitation(self) -> None:
        if self._panel is not None:
            self._panel.clear_invitation()

    def _pair(self) -> None:
        if self._server is None or not self._server.running or self._panel is None:
            return
        invitation = self._server.open_pairing()
        self._panel.set_invitation(str(invitation["url"]), str(invitation["code"]))
        self._pair_timer.start(120_000)

    def _revoke(self, device_id: str) -> None:
        if self._server is not None:
            self._server.revoke(device_id)
            self._server.facade.revoke_device(device_id)
            self._refresh()

    def _approve(self, request_id: str) -> None:
        if self._server is None or self._panel is None:
            return
        try:
            self._server.approve(request_id)
        except Exception:
            self._panel.status.setText("配对申请已失效，请在手机重新申请。")
            return
        self._refresh()
