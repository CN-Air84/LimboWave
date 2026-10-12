from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.memory_service import MemoryService
from limbowave.domain.conversation import Branch, Conversation
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.web.app import create_app
from limbowave.web.auth import AuthStore
from limbowave.web.security import WebConfig


class MemoryFacade:
    epoch = "epoch"

    def __init__(self, service: MemoryService) -> None:
        self.memory_service = service
        self._revision = 0
        self._coordinator = self

    @property
    def revision(self) -> int:
        return self._revision

    def bump_revision(self) -> None:
        self._revision += 1

    async def conversations(self, **kwargs):
        return {"items": [{"id": "c"}], "next_cursor": None}


async def test_web_memory_crud_session_override_and_conflict(tmp_path: Path, vault_key) -> None:
    factory = sqlite_uow_factory(tmp_path / "memory.db", vault_key)
    with factory() as uow:
        now = datetime(2026, 10, 12, tzinfo=UTC)
        uow.conversations.add(Conversation("c", "chat", now))
        uow.branches.add(Branch("b", "c", now))
        uow.commit()
    service = MemoryService(
        factory, ConfigurationService(JsonConfigRepository(tmp_path / "config.json"))
    )
    global_item = service.save("global")
    facade = MemoryFacade(service)
    auth = AuthStore()
    issued = auth.issue("browser")
    config = WebConfig()
    app = create_app(facade, config, auth)
    headers = {"Origin": config.origin, "X-CSRF-Token": str(issued["csrf_token"])}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=config.origin
    ) as client:
        client.cookies.set("lw_session", str(issued["token"]))
        listed = await client.get("/api/v1/memories?conversation_id=c&branch_id=b")
        assert listed.status_code == 200
        assert listed.json()["global"][0]["content"] == "global"

        global_created = await client.post(
            "/api/v1/memories",
            headers=headers,
            json={"expected_revision": 0, "content": "another global"},
        )
        assert global_created.status_code == 200
        created = await client.post(
            "/api/v1/memories",
            headers=headers,
            json={
                "expected_revision": 1,
                "conversation_id": "c",
                "branch_id": "b",
                "content": "branch memory",
            },
        )
        assert created.status_code == 200
        item_id = created.json()["item"]["id"]
        stale = await client.post(
            "/api/v1/memories",
            headers=headers,
            json={
                "expected_revision": 0,
                "conversation_id": "c",
                "branch_id": "b",
                "content": "stale",
            },
        )
        assert stale.status_code == 409

        overridden = await client.post(
            "/api/v1/memories/session",
            headers=headers,
            json={
                "expected_revision": 2,
                "conversation_id": "c",
                "item_id": global_item.id,
                "content": "session override",
            },
        )
        assert overridden.status_code == 200
        promoted = await client.post(
            f"/api/v1/memories/{item_id}/promote",
            headers=headers,
            json={"expected_revision": 3, "conversation_id": "c", "branch_id": "b"},
        )
        assert promoted.status_code == 200
        assert promoted.json()["item"]["content"] == "branch memory"

        deleted = await client.post(
            f"/api/v1/memories/{item_id}/delete",
            headers=headers,
            json={"expected_revision": 4, "conversation_id": "c", "branch_id": "b"},
        )
        assert deleted.status_code == 200

        denied = await client.get("/api/v1/memories?conversation_id=c&branch_id=b")
        assert denied.status_code == 200

    factory.close()


async def test_web_memory_requires_authorization(tmp_path: Path, vault_key) -> None:
    factory = sqlite_uow_factory(tmp_path / "memory.db", vault_key)
    service = MemoryService(
        factory, ConfigurationService(JsonConfigRepository(tmp_path / "config.json"))
    )
    config = WebConfig()
    app = create_app(MemoryFacade(service), config, AuthStore())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=config.origin
    ) as client:
        response = await client.get("/api/v1/memories?conversation_id=c&branch_id=b")
    assert response.status_code == 401
    factory.close()
