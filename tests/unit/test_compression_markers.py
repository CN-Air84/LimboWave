"""Accepted compactions project onto transcript boundaries, never into messages."""
from dataclasses import replace

import pytest

from limbowave.application.history_payload import history_payload
from limbowave.application.services.compression_service import CompressionService
from limbowave.domain.compaction import CompressionStatus
from limbowave.domain.conversation import Branch, Message, MessageRole
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.unit.test_compression_service import T0, _seed


@pytest.fixture(params=["memory", "sqlite"])
def factory(request, tmp_path, vault_key):
    factory = (in_memory_uow_factory(InMemoryStore()) if request.param == "memory"
               else sqlite_uow_factory(tmp_path / "markers.db", vault_key))
    _seed(factory)
    return factory


def version(factory, status=CompressionStatus.ACCEPTED):
    service = CompressionService(factory)
    draft = service.create_version("c1", "b1", tokens_before=1000,
                                   compression_model_id="model", compression_endpoint_id="ep")
    with factory() as uow:
        uow.compressions.update(replace(draft, status=status, generated_summary="summary",
                                       tokens_after=200))
        if status is CompressionStatus.ACCEPTED:
            uow.compressions.set_active("b1", draft.id)
        uow.commit()
    return draft.id


def payload(factory, branch_id="b1"):
    with factory() as uow:
        messages = uow.messages.list_for_branch("b1")
    return history_payload(messages, uow_factory=factory, branch_id=branch_id)


def test_boundary_survives_reload_and_precedes_new_messages(factory):
    vid = version(factory)
    with factory() as uow:
        uow.messages.add(Message(id="m4", conversation_id="c1", branch_id="b1",
                                 role=MessageRole.ASSISTANT, content="after",
                                 created_at=T0.replace(minute=3)))
        uow.commit()
    for _ in range(2):
        entries = payload(factory)
        assert [e.message_id for e in entries] == ["m1", "m2", "m3", "m4"]
        assert [len(e.compressions) for e in entries] == [0, 0, 1, 0]
        assert entries[2].compressions[0].version.id == vid
        assert entries[2].compressions[0].is_active
    with factory() as uow:
        assert len(uow.messages.list_for_branch("b1")) == 4


@pytest.mark.parametrize("status", [CompressionStatus.DRAFT, CompressionStatus.PREVIEWED,
                                    CompressionStatus.REJECTED, CompressionStatus.FAILED])
def test_unsuccessful_or_unaccepted_versions_have_no_marker(factory, status):
    version(factory, status)
    assert not any(e.compressions for e in payload(factory))


def test_repeated_compression_and_rollback_keep_historical_boundaries(factory):
    first, second = version(factory), version(factory)
    markers = payload(factory)[-1].compressions
    assert [m.version.id for m in markers] == [first, second]
    assert [m.is_active for m in markers] == [False, True]
    assert CompressionService(factory).rollback("b1")
    markers = payload(factory)[-1].compressions
    assert [m.version.id for m in markers] == [first, second]
    assert not any(m.is_active for m in markers)


def test_branch_scope_does_not_infer_parent_from_inherited_messages(factory):
    version(factory)
    with factory() as uow:
        uow.branches.add(Branch(id="child", conversation_id="c1", created_at=T0,
                               parent_branch_id="b1", forked_from_message_id="m3"))
        uow.commit()
    assert not any(e.compressions for e in payload(factory, "child"))
    assert not any(e.compressions for e in payload(factory, None))


def test_truncated_history_does_not_move_a_later_boundary(factory):
    version(factory)
    with factory() as uow:
        messages = uow.messages.list_for_branch("b1")[:2]
    entries = history_payload(messages, uow_factory=factory, branch_id="b1")
    assert not any(e.compressions for e in entries)
