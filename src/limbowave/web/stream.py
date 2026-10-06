"""Revocation-aware SSE; consumers never own the model execution task."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncGenerator, Callable
from typing import Any

from starlette.responses import StreamingResponse

from .auth import AuthStore, Device
from .dto import project


def encode(event: dict[str, Any]) -> bytes:
    epoch, seq = event.get("server_epoch", ""), event.get("seq", 0)
    # IDs and event names are never interpolated from free-form payload strings.
    kind = event.get("kind", "message")
    if not isinstance(kind, str) or not kind.replace("_", "").isalnum():
        kind = "message"
    cursor = f"{epoch}:{seq}"
    if "\n" in cursor or "\r" in cursor:
        cursor = ""
    return (
        f"id: {cursor}\nevent: {kind}\ndata: "
        + json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    ).encode()


async def event_stream(
    facade: Any,
    auth: AuthStore,
    device: Device,
    cursor: str | None,
    max_bytes: int,
    *,
    heartbeat_seconds: float = 15,
) -> AsyncGenerator[bytes, None]:
    subscription = None
    next_event = None
    revoked = asyncio.create_task(device.revoked.wait())
    try:
        current = await facade.state()
        if not current.get("available", True):
            auth.invalidate()
            return
        if cursor:
            epoch, seq_text = cursor.rsplit(":", 1)
            seq = int(seq_text)
            snapshot = None
        else:
            snapshot = project(current, "state")
            epoch, seq = snapshot["server_epoch"], snapshot["seq"]
        subscription = facade.events.subscribe(after_seq=seq, server_epoch=epoch)
        if snapshot is not None and auth.valid(device):
            initial = {
                "kind": "snapshot",
                "server_epoch": epoch,
                "seq": seq,
                "protocol_version": 1,
                "payload": snapshot,
            }
            data = encode(initial)
            if len(data) > max_bytes:
                yield encode({"kind": "resync_required", "server_epoch": epoch, "seq": seq})
                return
            yield data
        iterator = subscription.__aiter__()
        while auth.valid(device):
            next_event = asyncio.create_task(anext(iterator))
            while True:
                remaining = min(
                    auth.absolute_ttl - (auth.clock() - device.created),
                    auth.idle_ttl - (auth.clock() - device.touched),
                )
                done, _ = await asyncio.wait(
                    (next_event, revoked),
                    timeout=max(0, min(remaining, heartbeat_seconds)),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if revoked in done or not auth.valid(device):
                    return
                if not (await facade.state()).get("available", True):
                    auth.invalidate()
                    return
                if next_event in done:
                    break
                # Keep the SAME pending anext task across heartbeats; never drop an event.
                # A keepalive is transport activity, not authenticated user activity.
                yield b": keepalive\n\n"
            if not (await facade.state()).get("available", True):
                auth.invalidate()
                return
            try:
                event = project(next_event.result(), "event")
            except StopAsyncIteration:
                break
            if event.get("kind") in ("runtime_invalidated", "vault_locked", "runtime_unavailable"):
                auth.invalidate()
                break
            data = encode(event)
            if len(data) > max_bytes:
                yield encode({"kind": "resync_required", "server_epoch": epoch, "seq": seq})
                break
            if auth.valid(device):
                yield data
    finally:
        # Close the broker registration before an await: Starlette/AnyIO can
        # repeatedly cancel awaits while tearing down a disconnected response.
        # Otherwise cleanup itself may be interrupted and leave a live subscriber.
        if subscription is not None:
            result = subscription.close()
            if inspect.isawaitable(result):
                await result
        tasks = [task for task in (next_event, revoked) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class RevocableEventResponse(StreamingResponse):
    """Cancel blocked network writes on revoke/expiry, never a runtime task."""

    def __init__(
        self,
        iterator: AsyncGenerator[bytes, None],
        auth: AuthStore,
        device: Device,
        on_close: Callable[[], None],
    ) -> None:
        super().__init__(
            iterator, media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )
        self.auth = auth
        self.device = device
        self.iterator = iterator
        self.on_close = on_close

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        started = finished = False

        async def tracked_send(message: Any) -> None:
            nonlocal started, finished
            if message["type"] == "http.response.start":
                started = True
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                finished = True
            await send(message)

        async def invalidated() -> None:
            while self.auth.valid(self.device):
                remaining = min(
                    self.auth.absolute_ttl - (self.auth.clock() - self.device.created),
                    self.auth.idle_ttl - (self.auth.clock() - self.device.touched),
                )
                try:
                    await asyncio.wait_for(self.device.revoked.wait(), timeout=max(0, remaining))
                except TimeoutError:
                    continue
                break

        serving = asyncio.create_task(super().__call__(scope, receive, tracked_send))
        watcher = asyncio.create_task(invalidated())
        try:
            done, _ = await asyncio.wait((serving, watcher), return_when=asyncio.FIRST_COMPLETED)
            if serving in done:
                await serving
            else:
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)
                if started and not finished:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            for task in (serving, watcher):
                task.cancel()
            await asyncio.gather(serving, watcher, return_exceptions=True)
            try:
                await self.iterator.aclose()
            finally:
                self.on_close()
