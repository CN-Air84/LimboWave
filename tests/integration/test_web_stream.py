import asyncio
from types import SimpleNamespace

import pytest

from limbowave.application.services.event_broker import EventBroker
from limbowave.web.auth import AuthStore
from limbowave.web.stream import event_stream


def setup_stream(*, idle_ttl=7200):
    broker = EventBroker(epoch="epoch")
    auth = AuthStore(idle_ttl=idle_ttl)
    issued = auth.issue("phone")
    d = auth.authenticate(issued["token"])
    facade = SimpleNamespace(events=broker, available=True)

    async def state():
        return {
            "server_epoch": "epoch",
            "seq": broker.seq,
            "available": facade.available,
            "stream": {"text": "live", "api_key": "SECRET"},
        }

    facade.state = state
    return facade, auth, d


async def test_snapshot_replay_projection_and_revoke():
    facade, auth, d = setup_stream()
    stream = event_stream(facade, auth, d, None, 65536)
    snapshot = await anext(stream)
    assert b'"kind":"snapshot"' in snapshot and b"live" in snapshot
    assert b"SECRET" not in snapshot
    facade.events.publish("text_delta", {"text": "hello", "api_key": "SECRET"})
    event = await anext(stream)
    assert b"hello" in event and b"SECRET" not in event
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    auth.revoke(d.id)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, 0.2)
    assert not facade.events._subscriptions


async def test_idle_expiry_closes_waiting_stream():
    facade, auth, d = setup_stream(idle_ttl=0.04)
    stream = event_stream(facade, auth, d, None, 65536)
    await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), 0.3)
    assert d.revoked.is_set()


async def test_invalidated_runtime_cannot_replay_buffered_secrets():
    facade, auth, d = setup_stream()
    facade.events.publish("text_delta", {"text": "OLD"})
    facade.available = False
    stream = event_stream(facade, auth, d, "epoch:0", 65536)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert d.revoked.is_set()


async def test_epoch_resync_and_event_byte_cap():
    facade, auth, d = setup_stream()
    stream = event_stream(facade, auth, d, "old:0", 65536)
    assert b"resync_required" in await anext(stream)
    await stream.aclose()
    stream = event_stream(facade, auth, d, "epoch:0", 200)
    facade.events.publish("text_delta", {"text": "a" * 1000})
    assert b"resync_required" in await anext(stream)
    await stream.aclose()


async def test_no_snapshot_subscribe_race():
    facade, auth, d = setup_stream()
    original = facade.state

    async def racing_state():
        state = await original()
        facade.events.publish("text_delta", {"text": "raced"})
        return state

    facade.state = racing_state
    stream = event_stream(facade, auth, d, None, 65536)
    await anext(stream)
    assert b"raced" in await anext(stream)
    await stream.aclose()


async def test_heartbeat_keeps_pending_event_and_does_not_refresh_idle():
    facade, auth, d = setup_stream()
    touched = d.touched
    stream = event_stream(facade, auth, d, None, 65536, heartbeat_seconds=0.01)
    await anext(stream)
    assert await asyncio.wait_for(anext(stream), 0.2) == b": keepalive\n\n"
    assert d.touched == touched
    facade.events.publish("assistant_delta", {"text": "not lost"})
    assert b"not lost" in await asyncio.wait_for(anext(stream), 0.2)
    assert d.touched == touched
    facade.available = False
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), 0.2)
    assert d.revoked.is_set()


async def test_slow_network_write_is_cancelled_on_revocation():
    from limbowave.web.stream import RevocableEventResponse

    facade, auth, d = setup_stream()
    blocked = asyncio.Event()
    forever = asyncio.Event()
    closed = []
    response = RevocableEventResponse(
        event_stream(facade, auth, d, None, 65536), auth, d, lambda: closed.append(True)
    )

    async def send(message):
        if message["type"] == "http.response.body" and message.get("more_body"):
            blocked.set()
            await forever.wait()

    async def receive():
        await forever.wait()

    task = asyncio.create_task(
        response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    )
    await blocked.wait()
    auth.revoke(d.id)
    await asyncio.wait_for(task, 0.2)
    assert closed == [True]
    assert not facade.events._subscriptions
