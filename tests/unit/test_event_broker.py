import pytest

from limbowave.application.services.event_broker import EventBroker


async def test_replay_order_scope_and_defensive_copy():
    broker = EventBroker("epoch")
    broker.publish("delta", {"text": "one"}, conversation_id="a")
    broker.publish("delta", {"text": "other"}, conversation_id="b")
    sub = broker.subscribe(after_seq=0, server_epoch="epoch", conversation_id="a")
    broker.publish("delta", {"text": "two"}, conversation_id="a")
    first = await anext(sub)
    first["payload"]["text"] = "tampered"
    assert (await anext(sub))["seq"] == 3
    again = broker.subscribe(after_seq=0, conversation_id="a")
    assert (await anext(again))["payload"]["text"] == "one"
    sub.close()
    again.close()


@pytest.mark.parametrize("cursor,epoch", [(0, "epoch"), (2, "old"), (99, "epoch"), (-1, "epoch")])
async def test_evicted_or_invalid_cursor_requires_resync(cursor, epoch):
    broker = EventBroker("epoch", max_events=1)
    broker.publish("delta", {})
    broker.publish("delta", {})
    sub = broker.subscribe(after_seq=cursor, server_epoch=epoch)
    assert (await anext(sub))["kind"] == "resync_required"
    with pytest.raises(StopAsyncIteration):
        await anext(sub)


async def test_slow_consumer_is_disconnected_without_blocking_producer():
    broker = EventBroker("epoch", queue_size=1)
    sub = broker.subscribe()
    broker.publish("delta", {})
    broker.publish("delta", {})
    assert (await anext(sub))["kind"] == "resync_required"
    assert sub.closed
    broker.publish("delta", {})
    assert broker.seq == 3


async def test_byte_limit_and_invalidation_erase_sensitive_replay():
    broker = EventBroker("epoch", max_bytes=400)
    sub = broker.subscribe()
    broker.publish("delta", {"text": "x" * 500})
    assert (await anext(sub))["kind"] == "resync_required"
    fresh = broker.subscribe()
    broker.publish("delta", {"text": "secret"})
    broker.invalidate()
    assert (await anext(fresh))["kind"] == "runtime_unavailable"
    with pytest.raises(StopAsyncIteration):
        await anext(fresh)
    reconnect = broker.subscribe(after_seq=0)
    assert (await anext(reconnect))["kind"] == "resync_required"


async def test_subscription_limit_and_close_wakes_reader():
    broker = EventBroker(max_subscriptions=1)
    sub = broker.subscribe()
    with pytest.raises(RuntimeError, match="subscription_capacity"):
        broker.subscribe()
    broker.close()
    with pytest.raises(StopAsyncIteration):
        await anext(sub)
