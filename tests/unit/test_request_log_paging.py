"""Bounded metadata queries must not decrypt full request/response bodies."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from limbowave.application.services.request_log_service import RequestLogService
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.domain.run import RunRecord, RunStatus
from limbowave.domain.snapshots import TransportSnapshot
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory

T0 = datetime(2026, 10, 4, tzinfo=UTC)


@pytest.fixture(params=["memory", "sqlite"])
def paged_service(request, tmp_path, vault_key):
    factory = (
        sqlite_uow_factory(tmp_path / "logs.db", vault_key)
        if request.param == "sqlite" else in_memory_uow_factory()
    )
    with factory() as uow:
        for conversation in ("c1", "c2"):
            uow.conversations.add(Conversation(id=conversation, title="test", created_at=T0))
            uow.branches.add(Branch(id=conversation, conversation_id=conversation, created_at=T0))
        # Identical timestamps exercise stable ordering at page boundaries.
        for index in range(206):
            conversation = "c1" if index < 205 else "c2"
            uow.messages.add(Message(
                id=f"m{index}", conversation_id=conversation, branch_id=conversation,
                role=MessageRole.USER, content="test", created_at=T0,
            ))
            uow.runs.add(RunRecord(
                id=f"r{index:03}", conversation_id=conversation, branch_id=conversation,
                status=RunStatus.COMPLETED, created_at=T0, user_message_id=f"m{index}",
                error="encrypted error must not be read for the list",
            ))
        for index in range(2):
            uow.snapshots.add_transport(TransportSnapshot(
                id=f"t{index}", run_id="r000", created_at=T0, sequence=index,
                body={"messages": ["large request " * 10000]},
                response_body={"content": ["large response " * 10000]},
            ))
        uow.commit()
    service = RequestLogService(factory)
    yield service, factory, vault_key
    service.close()
    if hasattr(factory, "close"):
        factory.close()


def test_pages_are_bounded_stable_and_do_not_decrypt(paged_service, monkeypatch):
    service, factory, key = paged_service

    def forbidden(*args, **kwargs):
        raise AssertionError("list display must not read payloads")

    monkeypatch.setattr(type(key), "decrypt", forbidden)
    with factory() as uow:
        monkeypatch.setattr(type(uow.snapshots), "get_intent", forbidden)
        monkeypatch.setattr(type(uow.snapshots), "list_transport", forbidden)
    pages = [service.list_summaries("c1", offset=n) for n in (0, 100, 200)]
    assert [len(page.entries) for page in pages] == [100, 100, 5]
    assert [page.has_more for page in pages] == [True, True, False]
    assert [row.id for page in pages for row in page.entries] == [f"r{i:03}" for i in range(205)]
    assert pages[0].entries[0].transport_count == 2
    assert pages[0].entries[1].transport_count == 0
    assert not service.list_summaries("missing").entries


def test_background_queries_close_their_own_connections(paged_service):
    service, factory, _key = paged_service
    assert len(service.load_page_async("c1").result(timeout=5).entries) == 100
    detail = service.load_run_async("r000").result(timeout=5)
    assert detail is not None
    assert len(detail.transports) == 2
    if hasattr(factory, "_units"):
        assert not factory._units
    service.close()
    with pytest.raises(RuntimeError):
        service.load_page_async("c1")


@pytest.mark.parametrize("kwargs", [{"offset": -1}, {"limit": 0}, {"limit": 101}])
def test_invalid_page_sizes_are_rejected(paged_service, kwargs):
    with pytest.raises(ValueError):
        paged_service[0].list_summaries("c1", **kwargs)
