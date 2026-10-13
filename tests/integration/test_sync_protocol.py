from __future__ import annotations

import runpy
from datetime import UTC, datetime
from pathlib import Path

import httpx

from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.application.services.runtime_facade import RuntimeFacade
from limbowave.application.services.session_controller import SessionController
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.infrastructure.database.change_log import ChangeLogStore
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.web.app import create_app
from limbowave.web.auth import AuthStore
from limbowave.web.security import WebConfig

FakeKernel = runpy.run_path(str(Path(__file__).parents[1] / "unit" / "test_run_coordinator.py"))[
    "FakeKernel"
]


async def test_authenticated_sync_paging_snapshot_and_expiry(tmp_path: Path, vault_key) -> None:
    db = tmp_path / "sync.db"
    factory = sqlite_uow_factory(db, vault_key)
    now = datetime(2026, 10, 12, tzinfo=UTC)
    with factory() as uow:
        uow.conversations.add(Conversation("c", "chat", now))
        uow.branches.add(Branch("b", "c", now))
        uow.messages.add(Message("m", "c", "b", MessageRole.USER, "hello", now))
        uow.commit()

    kernel = FakeKernel()
    coordinator = RunCoordinator(kernel, factory, context=lambda: RunContext("m", "e", "test"))
    store = ChangeLogStore(db, factory, vault_key)
    facade = RuntimeFacade(
        SessionController(kernel, coordinator),
        factory,
        sync_store=store,
        ready=lambda: True,
    )
    auth = AuthStore()
    issued = auth.issue("desktop")
    config = WebConfig()
    app = create_app(facade, config, auth)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=config.origin
    ) as client:
        assert (await client.get("/api/v1/sync/changes")).status_code == 401
        client.cookies.set("lw_session", str(issued["token"]))

        first = await client.get("/api/v1/sync/changes?cursor=0&limit=2")
        assert first.status_code == 200
        assert first.json()["has_more"] is True

        second = await client.get(f"/api/v1/sync/changes?cursor={first.json()['cursor']}&limit=10")
        assert second.status_code == 200
        assert second.json()["items"][-1]["entity_type"] == "message"

        snapshot = await client.get("/api/v1/sync/snapshot")
        assert snapshot.status_code == 200
        body = snapshot.json()
        assert body["conversations"][0]["id"] == "c"
        assert body["messages"][0]["content"] == "hello"
        assert body["cursor"] >= second.json()["cursor"]

        assert store.prune_through(int(second.json()["cursor"])) > 0
        expired = await client.get("/api/v1/sync/changes?cursor=0&limit=10")
        assert expired.status_code == 409
        assert expired.json()["resync_required"] is True

    await facade.close()
    factory.close()
