"""Bounded, ordered application events. All methods run on the runtime event loop."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from copy import deepcopy
from typing import Any
from uuid import uuid4


class EventSubscription:
    def __init__(
        self, broker: EventBroker, conversation_id: str | None, branch_id: str | None
    ) -> None:
        self.broker = broker
        self.conversation_id = conversation_id
        self.branch_id = branch_id
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(broker.queue_size)
        self.closed = False
        self.bytes = 0

    def __aiter__(self) -> EventSubscription:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self.closed and self.queue.empty():
            raise StopAsyncIteration
        value = await self.queue.get()
        if value is None:
            raise StopAsyncIteration
        self.bytes -= self.broker.size(value)
        return value

    def close(self) -> None:
        self.closed = True
        self.broker._subscriptions.discard(self)
        while not self.queue.empty():
            self.queue.get_nowait()
        self.bytes = 0
        self.queue.put_nowait(None)

    def resync(self) -> None:
        self.close()
        self.queue.get_nowait()
        event = self.broker.envelope("resync_required", {})
        self.bytes = self.broker.size(event)
        self.queue.put_nowait(event)

    def push(self, event: dict[str, Any]) -> None:
        if self.closed:
            return
        if self.conversation_id is not None and event["conversation_id"] != self.conversation_id:
            return
        if self.branch_id is not None and event["branch_id"] != self.branch_id:
            return
        size = self.broker.size(event)
        if self.queue.full() or self.bytes + size > self.broker.max_bytes:
            self.resync()
            return
        self.bytes += size
        self.queue.put_nowait(deepcopy(event))


class EventBroker:
    def __init__(
        self,
        epoch: str | None = None,
        *,
        max_events: int = 2048,
        max_bytes: int = 8 * 1024 * 1024,
        queue_size: int = 256,
        max_subscriptions: int = 64,
    ) -> None:
        if min(max_events, max_bytes, queue_size, max_subscriptions) < 1:
            raise ValueError("limits must be positive")
        self.epoch = epoch or uuid4().hex
        self.max_events, self.max_bytes = max_events, max_bytes
        self.queue_size, self.max_subscriptions = queue_size, max_subscriptions
        self.seq = 0
        self._events: deque[tuple[dict[str, Any], int]] = deque()
        self._bytes = 0
        self._subscriptions: set[EventSubscription] = set()
        self._closed = False

    @staticmethod
    def size(event: dict[str, Any]) -> int:
        return len(json.dumps(event, ensure_ascii=False).encode("utf-8"))

    def envelope(self, kind: str, payload: dict[str, Any], **scope: Any) -> dict[str, Any]:
        return {
            "protocol_version": 1,
            "server_epoch": self.epoch,
            "seq": self.seq,
            "kind": kind,
            "conversation_id": scope.get("conversation_id"),
            "branch_id": scope.get("branch_id"),
            "run_id": scope.get("run_id"),
            "message_id": scope.get("message_id"),
            "run_origin": scope.get("run_origin"),
            "payload": deepcopy(payload),
        }

    def publish(self, kind: str, payload: dict[str, Any], **scope: Any) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("broker_closed")
        self.seq += 1
        event = self.envelope(kind, payload, **scope)
        size = self.size(event)
        self._events.append((event, size))
        self._bytes += size
        while self._events and (
            len(self._events) > self.max_events or self._bytes > self.max_bytes
        ):
            self._bytes -= self._events.popleft()[1]
        for sub in tuple(self._subscriptions):
            if size > self.max_bytes:
                sub.resync()
            else:
                sub.push(event)
        return deepcopy(event)

    def subscribe(
        self,
        *,
        after_seq: int | None = None,
        server_epoch: str | None = None,
        conversation_id: str | None = None,
        branch_id: str | None = None,
    ) -> EventSubscription:
        if len(self._subscriptions) >= self.max_subscriptions:
            raise RuntimeError("subscription_capacity")
        sub = EventSubscription(self, conversation_id, branch_id)
        self._subscriptions.add(sub)
        cursor = self.seq if after_seq is None else after_seq
        oldest = self._events[0][0]["seq"] if self._events else self.seq + 1
        if (
            self._closed
            or server_epoch not in (None, self.epoch)
            or not isinstance(cursor, int)
            or cursor < oldest - 1
            or cursor > self.seq
        ):
            sub.resync()
        else:
            for event, _ in self._events:
                if event["seq"] > cursor:
                    sub.push(event)
        return sub

    def rotate_epoch(self) -> str:
        """Invalidate old cursors/subscriptions without touching the runtime."""
        for sub in tuple(self._subscriptions):
            sub.resync()
        self._events.clear()
        self._bytes = 0
        self.seq = 0
        self.epoch = uuid4().hex
        self._closed = False
        return self.epoch

    def invalidate(self) -> None:
        """Drop sensitive replay/queued data and deliver only the invalidation."""
        self.seq += 1
        self._events.clear()
        self._bytes = 0
        for sub in tuple(self._subscriptions):
            while not sub.queue.empty():
                sub.queue.get_nowait()
            event = self.envelope("runtime_unavailable", {})
            sub.bytes = self.size(event)
            sub.queue.put_nowait(event)
            sub.closed = True
        self._subscriptions.clear()
        self._closed = True

    def close(self) -> None:
        self._closed = True
        for sub in tuple(self._subscriptions):
            sub.close()
        self._events.clear()
        self._bytes = 0
