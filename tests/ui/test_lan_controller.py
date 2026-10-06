"""Optional LAN services stay absent until requested and obey desktop shutdown."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PySide6.QtWidgets import QWidget

from limbowave.ui import lan_controller
from limbowave.ui.lan_controller import LanAccessController


class Server:
    def __init__(self):
        self.facade = SimpleNamespace(
            close=AsyncMock(), invalidate=Mock(), revoke_device=Mock(),
        )
        self.running = False
        self.url = "http://127.0.0.1:8765"
        self.config = None
        self.stop_count = 0
        self.revoke = Mock()
        self.approve = Mock()
        self.invalidate = Mock()
        self.list_devices = Mock(return_value=[])
        self.list_pending = Mock(return_value=[])
        self.open_pairing = Mock(return_value={"url": self.url, "code": "ABC"})

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False
        self.stop_count += 1


@pytest.fixture
async def access(qtbot, monkeypatch):
    host = QWidget()
    qtbot.addWidget(host)
    created = []
    gui = threading.get_ident()

    def factory():
        assert threading.get_ident() == gui
        server = Server()
        created.append(server)
        return server

    monkeypatch.setattr(lan_controller, "_load_lan_dependencies", lambda: None)
    owner = LanAccessController(
        host, create_server=factory, allowed=lambda: True,
        spawn=lambda coro: asyncio.create_task(coro),
    )
    yield host, owner, created
    await owner.close()


async def test_unused_lan_has_no_panel_server_or_imports(access, monkeypatch):
    host, owner, created = access

    def forbidden():
        pytest.fail("disabled LAN must not load dependencies")

    monkeypatch.setattr(lan_controller, "_load_lan_dependencies", forbidden)
    assert host.findChild(QWidget, "lanAccessPanel") is None
    assert not owner._refresh_timer.isActive()
    await owner.close()
    assert created == []


async def test_imports_off_thread_widgets_and_services_on_owner_loop(access, monkeypatch):
    host, owner, created = access
    gui = threading.get_ident()
    imported = []
    monkeypatch.setattr(
        lan_controller, "_load_lan_dependencies", lambda: imported.append(threading.get_ident())
    )
    panel = owner.panel()
    assert owner.panel() is panel
    assert panel.thread() is host.thread()
    await owner.start({"host": "127.0.0.1", "port": 8765})
    assert len(created) == 1
    assert imported and imported[0] != gui
    assert created[0].running
    assert panel.stop_button.isEnabled()
    await owner.stop()
    assert not created[0].running
    created[0].facade.close.assert_awaited_once()
    assert owner._server is None


async def test_refresh_only_while_visible_pair_expiry_survives_hide(access, monkeypatch):
    host, owner, created = access
    host.show()
    panel = owner.panel()
    panel.show()
    await owner.start({"host": "127.0.0.1"})
    assert owner._refresh_timer.isActive()
    monkeypatch.setattr(panel, "set_invitation", Mock())
    owner._pair()
    assert owner._pair_timer.isActive()
    panel.hide()
    assert not owner._refresh_timer.isActive()
    assert owner._pair_timer.isActive()
    panel.show()
    assert owner._refresh_timer.isActive()
    owner._revoke("device")
    created[0].revoke.assert_called_once_with("device")
    created[0].facade.revoke_device.assert_called_once_with("device")
    owner._approve("request")
    created[0].approve.assert_called_once_with("request")
    await owner.stop()
    assert not owner._refresh_timer.isActive()
    assert not owner._pair_timer.isActive()


async def test_invalid_config_never_constructs_server(access):
    _, owner, created = access
    await owner.start({"host": "0.0.0.0"})
    assert created == []
    assert "无法开启" in owner.panel().status.text()
    assert owner.panel().start_button.isEnabled()


async def test_failed_start_disposes_subscription_and_hides_raw_error(access):
    _, owner, created = access
    server = Server()
    server.start = AsyncMock(side_effect=RuntimeError("private-path-and-secret"))
    owner._create_server = lambda: server
    await owner.start({"host": "127.0.0.1"})
    assert "private-path" not in owner.panel().status.text()
    assert owner.panel().start_button.isEnabled()
    assert owner._server is None
    assert server.stop_count == 1
    server.facade.close.assert_awaited_once()
    assert created == []


async def test_close_during_import_cannot_reopen_listener(access, monkeypatch):
    _, owner, created = access
    importing, release = threading.Event(), threading.Event()

    def slow_import():
        importing.set()
        assert release.wait(5)

    monkeypatch.setattr(lan_controller, "_load_lan_dependencies", slow_import)
    starting = asyncio.create_task(owner.start({"host": "127.0.0.1"}))
    try:
        assert await asyncio.to_thread(importing.wait, 5)
        closing = asyncio.create_task(owner.close())
        await asyncio.sleep(0)
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(starting, closing), timeout=5)
    assert created == []
    assert owner._server is None


async def test_invalidation_while_starting_disposes_listener(access):
    _, owner, created = access
    entered, release = asyncio.Event(), asyncio.Event()
    server = Server()

    async def slow_start():
        entered.set()
        await release.wait()
        server.running = True

    server.start = slow_start
    owner._create_server = lambda: server
    starting = asyncio.create_task(owner.start({"host": "127.0.0.1"}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        owner.invalidate()
    finally:
        release.set()
    await asyncio.wait_for(starting, timeout=5)
    assert not server.running
    server.facade.invalidate.assert_called_once()
    server.facade.close.assert_awaited_once()
    assert created == []


async def test_cancelled_start_releases_services(access):
    _, owner, _ = access
    entered = asyncio.Event()
    server = Server()

    async def waiting_start():
        entered.set()
        await asyncio.Event().wait()

    server.start = waiting_start
    owner._create_server = lambda: server
    starting = asyncio.create_task(owner.start({"host": "127.0.0.1"}))
    await asyncio.wait_for(entered.wait(), timeout=5)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    server.facade.close.assert_awaited_once()
    assert owner._server is None
    assert owner.panel().start_button.isEnabled()


async def test_panel_can_be_recreated_without_restarting_services(access):
    import shiboken6

    _, owner, created = access
    panel = owner.panel()
    await owner.start({"host": "127.0.0.1"})
    shiboken6.delete(panel)
    assert owner._panel is None
    assert owner.panel() is not panel
    assert len(created) == 1 and created[0].running


def test_real_lazy_server_runs_on_qasync_and_closes_cleanly(qtbot):
    import httpx
    from PySide6.QtWidgets import QApplication
    from qasync import QEventLoop

    host = QWidget()
    qtbot.addWidget(host)
    gui = threading.get_ident()
    loop = QEventLoop(QApplication.instance())
    asyncio.set_event_loop(loop)
    facade = SimpleNamespace(
        epoch="test", rotate_epoch=lambda: None, invalidate=Mock(), close=AsyncMock(),
    )

    def create_server():
        from limbowave.web.server import WebServer

        assert threading.get_ident() == gui
        assert asyncio.get_running_loop() is loop
        return WebServer(facade)

    owner = LanAccessController(
        host, create_server=create_server, allowed=lambda: True,
        spawn=lambda coro: loop.create_task(coro),
    )

    async def exercise():
        try:
            await owner.start({"host": "127.0.0.1", "port": 0})
            assert owner._server is not None and owner._server.running
            async with httpx.AsyncClient(trust_env=False) as client:
                reply = await client.get(owner._server.url + "/healthz")
                assert reply.status_code == 200
        finally:
            await owner.close()
        facade.close.assert_awaited_once()

    try:
        loop.run_until_complete(exercise())
    finally:
        loop.close()
        asyncio.set_event_loop(None)
