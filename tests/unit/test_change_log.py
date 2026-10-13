from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.domain.memory import MemoryDocument
from limbowave.infrastructure.database.change_log import ChangeLogStore, CursorExpired
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory


def test_change_log_orders_transactional_writes_and_deletes(tmp_path: Path, vault_key) -> None:
    db = tmp_path / "sync.db"
    factory = sqlite_uow_factory(db, vault_key)
    now = datetime(2026, 10, 12, tzinfo=UTC)
    with factory() as uow:
        uow.conversations.add(Conversation("c", "chat", now))
        uow.branches.add(Branch("b", "c", now))
        uow.commit()

    store = ChangeLogStore(db, factory, vault_key)
    first = store.page(0, limit=1)
    assert first["items"][0]["entity_type"] == "conversation"
    assert first["items"][0]["scope"] == "conversation:c"
    assert first["items"][0]["revision"] == 1
    assert first["has_more"] is True

    second = store.page(first["cursor"], limit=10)
    assert second["items"][0]["entity_type"] == "branch"
    assert second["items"][0]["scope"] == "conversation:c:branch:b"
    assert second["items"][0]["revision"] == 2

    with factory() as uow:
        uow.branches.delete("b")
        uow.commit()
    deleted = store.page(second["cursor"])
    assert deleted["items"][-1]["operation"] == "delete"
    assert deleted["items"][-1]["revision"] == 3


def test_snapshot_contains_current_conversations_messages_and_memory(
    tmp_path: Path, vault_key
) -> None:
    db = tmp_path / "sync.db"
    factory = sqlite_uow_factory(db, vault_key)
    now = datetime(2026, 10, 12, tzinfo=UTC)
    with factory() as uow:
        uow.conversations.add(Conversation("c", "chat", now))
        uow.branches.add(Branch("b", "c", now))
        uow.messages.add(Message("m", "c", "b", MessageRole.USER, "hello", now))
        uow.memories.put(MemoryDocument("global", "[]"))
        uow.commit()

    store = ChangeLogStore(db, factory, vault_key)
    snapshot = store.snapshot()
    assert snapshot["conversations"][0]["id"] == "c"
    assert snapshot["branches"][0]["id"] == "b"
    assert snapshot["messages"][0]["content"] == "hello"
    assert snapshot["memories"][0]["id"] == "global"
    assert snapshot["cursor"] >= 1
    assert snapshot["revision"] >= 1


def test_cursor_expiry_requires_snapshot(tmp_path: Path, vault_key) -> None:
    db = tmp_path / "sync.db"
    factory = sqlite_uow_factory(db, vault_key)
    now = datetime(2026, 10, 12, tzinfo=UTC)
    for index in range(3):
        with factory() as uow:
            uow.conversations.add(Conversation(f"c{index}", "chat", now))
            uow.commit()

    store = ChangeLogStore(db, factory, vault_key)
    assert store.prune_through(2) == 2
    with pytest.raises(CursorExpired):
        store.page(1)
