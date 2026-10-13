from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from limbowave.application.services.sync_client import SyncClient
from limbowave.infrastructure.database.remote_cache import RemoteCache


def test_remote_cache_stores_snapshot_state_and_changes_encrypted(
    tmp_path: Path, vault_key
) -> None:
    cache = RemoteCache(tmp_path / "remote.db", vault_key)
    cache.save_snapshot({"conversations": [{"id": "c"}]})
    cache.save_state(12, 34)
    cache.save_change({"cursor": 13, "entity_type": "message", "entity_id": "m"})

    assert cache.load_snapshot() == {"conversations": [{"id": "c"}]}
    assert cache.state().cursor == 12
    assert cache.state().revision == 34
    assert cache.changes()[0]["entity_id"] == "m"
    raw = (tmp_path / "remote.db").read_bytes()
    assert b"conversations" not in raw


@pytest.mark.asyncio
async def test_sync_client_prefers_snapshot_then_incremental_changes(
    tmp_path: Path, vault_key
) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/snapshot"):
            return httpx.Response(
                200,
                json={
                    "cursor": 10,
                    "revision": 10,
                    "conversations": [],
                    "branches": [],
                    "messages": [],
                    "runs": [],
                    "memories": [],
                    "truncated": False,
                },
            )
        assert request.url.params["cursor"] == "10"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "cursor": 11,
                        "entity_type": "message",
                        "entity_id": "m",
                        "scope": "conversation:c:branch:b:message:m",
                        "operation": "upsert",
                        "revision": 11,
                        "created_at": "2026-10-13T00:00:00",
                    }
                ],
                "cursor": 11,
                "latest_cursor": 11,
                "has_more": False,
                "resync_required": False,
            },
        )

    transport = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://limbowave.example.com"
    )
    cache = RemoteCache(tmp_path / "remote.db", vault_key)
    client = SyncClient(transport, cache)

    await client.sync()
    assert calls[-1] == "/api/v1/sync/snapshot"
    assert cache.state().cursor == 10

    await client.sync()
    assert calls[-1] == "/api/v1/sync/changes"
    assert cache.state().cursor == 11
    assert cache.changes()[0]["entity_id"] == "m"


@pytest.mark.parametrize("status,code", [(413, "snapshot_too_large"), (503, "snapshot_busy")])
async def test_failed_snapshot_keeps_existing_cache_and_cursor(tmp_path, vault_key, status, code):
    cache = RemoteCache(tmp_path / "remote.db", vault_key)
    original = {"conversations": [{"id": "existing"}], "cursor": 12, "revision": 34}
    cache.save_snapshot(original)
    cache.save_state(12, 34)

    def handler(request):
        return httpx.Response(status, json={"code": code})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://limbowave.example.com"
    ) as transport:
        with pytest.raises(httpx.HTTPStatusError):
            await SyncClient(transport, cache).snapshot()
    assert cache.load_snapshot() == original
    assert cache.state().cursor == 12
    assert cache.state().revision == 34
