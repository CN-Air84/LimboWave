import asyncio
import signal
import socket
from types import SimpleNamespace

import httpx
import pytest

from limbowave.web.security import WebConfig
from limbowave.web.server import WebServer, WebStartupError


async def test_opt_in_same_loop_stop_restart_and_signals():
    facade = SimpleNamespace(epoch="test", rotate_epoch=lambda: None)
    web = WebServer(facade, WebConfig(port=0))
    assert not web.running and web.url == ""
    original = signal.getsignal(signal.SIGINT)
    model = asyncio.create_task(asyncio.sleep(30))
    try:
        await web.start()
        assert web.running
        assert signal.getsignal(signal.SIGINT) == original
        async with httpx.AsyncClient() as c:
            assert (await c.get(web.url + "/healthz")).status_code == 200
        pair = web.open_pairing()
        assert "#ticket=" in pair["url"]
        session = web.auth.pair(ticket=pair["ticket"])
        device = web.auth.authenticate(session["token"])
        await web.stop()
        assert not web.running and device.revoked.is_set()
        assert not model.done()
        await web.stop()
        await web.start()
        assert web.running and web.list_devices() == []
    finally:
        await web.stop()
        model.cancel()
        await asyncio.gather(model, return_exceptions=True)


async def test_occupied_port_cleans_up_and_retry():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    web = WebServer(
        SimpleNamespace(epoch="test", rotate_epoch=lambda: None),
        WebConfig(port=sock.getsockname()[1]),
    )
    try:
        with pytest.raises(WebStartupError) as error:
            await web.start()
        assert error.value.code == "port_in_use"
        assert not web.running and web._socket is None
    finally:
        sock.close()
    await web.start()
    assert web.running
    await web.stop()


async def test_real_sse_connection_limit_revoke_and_cleanup():
    from limbowave.application.services.event_broker import EventBroker

    broker = EventBroker()
    facade = SimpleNamespace(epoch=broker.epoch, events=broker)

    def rotate():
        facade.epoch = broker.rotate_epoch()

    async def state():
        return {"server_epoch": facade.epoch, "seq": broker.seq, "available": True, "stream": None}

    facade.rotate_epoch = rotate
    facade.state = state
    web = WebServer(facade, WebConfig(port=0, max_streams=1))
    try:
        await web.start()
        issued = web.auth.issue("phone")
        async with httpx.AsyncClient(base_url=web.url, timeout=2) as c:
            c.cookies.set("lw_session", issued["token"])
            async with c.stream("GET", "/api/v1/events") as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                assert (await anext(lines)).startswith("id:")
                assert (await anext(lines)) == "event: snapshot"
                assert (await anext(lines)).startswith("data:")
                assert (await anext(lines)) == ""
                assert (await c.get("/api/v1/events")).status_code == 429
                web.revoke(issued["device_id"])
                with pytest.raises(StopAsyncIteration):
                    await asyncio.wait_for(anext(lines), 0.5)
            for _ in range(10):
                if web.app.state.streams == 0:
                    break
                await asyncio.sleep(0.01)
            assert web.app.state.streams == 0
            assert not broker._subscriptions
    finally:
        await web.stop()


def test_qasync_loop_keeps_qt_timer_responsive(qapp):
    from PySide6.QtCore import QTimer
    from qasync import QEventLoop

    timer = QTimer()
    timer.setInterval(5)
    ticks = []
    timer.timeout.connect(lambda: ticks.append(True))
    original = signal.getsignal(signal.SIGINT)
    web = WebServer(SimpleNamespace(epoch="test", rotate_epoch=lambda: None), WebConfig(port=0))

    async def exercise(loop):
        timer.start()
        try:
            await web.start()
            assert web._task.get_loop() is loop
            async with httpx.AsyncClient(base_url=web.url) as c:
                assert (await c.get("/healthz")).status_code == 200
            await asyncio.sleep(0.03)
            assert ticks
            assert signal.getsignal(signal.SIGINT) == original
        finally:
            await web.stop()
            timer.stop()

    with QEventLoop(qapp) as loop:
        loop.run_until_complete(exercise(loop))
