"""Client for the server-authoritative incremental sync API."""

from __future__ import annotations

import httpx

from limbowave.infrastructure.database.remote_cache import RemoteCache


class SyncClient:
    """Pull a snapshot or incremental changes into an encrypted local cache."""

    def __init__(self, transport: httpx.AsyncClient, cache: RemoteCache) -> None:
        self.transport = transport
        self.cache = cache

    async def sync(self, *, force_snapshot: bool = False) -> None:
        state = self.cache.state()
        if force_snapshot or state.cursor == 0:
            await self.snapshot()
            return

        response = await self.transport.get(
            "/api/v1/sync/changes",
            params={"cursor": state.cursor, "limit": 100},
        )
        if response.status_code == 409:
            await self.snapshot()
            return
        response.raise_for_status()
        payload = response.json()
        for item in payload.get("items", []):
            self.cache.save_change(item)
        self.cache.save_state(int(payload["cursor"]), int(payload.get("revision", 0)))

    async def snapshot(self) -> None:
        response = await self.transport.get("/api/v1/sync/snapshot")
        response.raise_for_status()
        payload = response.json()
        self.cache.save_snapshot(payload)
        self.cache.save_state(int(payload["cursor"]), int(payload["revision"]))
