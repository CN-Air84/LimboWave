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


@pytest.mark.parametrize("scan_part", ["conversation", "message"])
def test_snapshot_cursor_cannot_skip_a_concurrent_message(
    tmp_path, vault_key, monkeypatch, scan_part
):
    from limbowave.infrastructure.database.sqlite_repositories import _Conversations, _Messages

    db = tmp_path / "sync.db"
    factory = sqlite_uow_factory(db, vault_key)
    now = datetime(2026, 10, 12, tzinfo=UTC)
    with factory() as uow:
        uow.conversations.add(Conversation("c", "chat", now))
        uow.branches.add(Branch("b", "c", now))
        uow.commit()
    repository, method = (
        (_Conversations, "list_all")
        if scan_part == "conversation"
        else (_Messages, "list_for_branch")
    )
    original = getattr(repository, method)

    def racing_list(repo, *args):
        result = original(repo, *args)
        with factory() as writer:
            writer.messages.add(Message("raced", "c", "b", MessageRole.USER, "new", now))
            writer.commit()
        return result

    monkeypatch.setattr(repository, method, racing_list)
    store = ChangeLogStore(db, factory, vault_key)
    snapshot = store.snapshot()
    # The read view must stay fixed even across repository reads.
    assert not any(m["id"] == "raced" for m in snapshot["messages"])
    assert any(item["entity_id"] == "raced" for item in store.page(snapshot["cursor"])["items"])


def test_device_changes_do_not_advance_content_cursor(tmp_path):
    from limbowave.server.auth import PersistentAuthStore

    db = tmp_path / "sync.db"
    store = ChangeLogStore(db)
    auth = PersistentAuthStore(db)
    issued = auth.issue("phone")
    auth.authenticate(issued["token"])
    auth.revoke(issued["device_id"])
    assert store.page(0)["items"] == []
    assert store.page(0)["latest_cursor"] == 0


def test_snapshot_cursor_survives_pruning_all_history(tmp_path, vault_key):
    db = tmp_path / "sync.db"
    factory = sqlite_uow_factory(db, vault_key)
    with factory() as uow:
        uow.conversations.add(Conversation("c", "chat", datetime.now(UTC)))
        uow.commit()
    store = ChangeLogStore(db, factory, vault_key)
    cursor = store.snapshot()["cursor"]
    store.prune_through(cursor)
    assert store.snapshot()["cursor"] == cursor
    assert store.page(cursor)["latest_cursor"] == cursor
    with pytest.raises(CursorExpired):
        store.page(0)


@pytest.mark.parametrize("limit", ["entities", "bytes"])
def test_snapshot_checks_budget_before_decryption(tmp_path, vault_key, monkeypatch, limit):
    from limbowave.infrastructure.database.change_log import SnapshotTooLarge
    from limbowave.infrastructure.database.sqlite_repositories import _Conversations

    db = tmp_path / "sync.db"
    factory = sqlite_uow_factory(db, vault_key)
    with factory() as uow:
        for index in range(3):
            uow.conversations.add(Conversation(str(index), "chat" * 100, datetime.now(UTC)))
        uow.commit()

    def unexpected_read(repo):
        pytest.fail("over-budget snapshot should not decrypt entities")

    monkeypatch.setattr(_Conversations, "list_all", unexpected_read)
    store = ChangeLogStore(
        db,
        factory,
        vault_key,
        **({"max_snapshot_entities": 2} if limit == "entities" else {"max_snapshot_bytes": 100}),
    )
    with pytest.raises(SnapshotTooLarge):
        store.snapshot()


def test_production_log_retention_expires_old_cursors(tmp_path, vault_key):
    from contextlib import closing

    from limbowave.infrastructure.database.sqlite_repositories import open_connection

    db = tmp_path / "sync.db"
    factory = sqlite_uow_factory(db, vault_key)
    with factory() as uow:
        uow.conversations.add(Conversation("c", "chat", datetime.now(UTC)))
        uow.commit()
    # Jump the sequence to exercise the real production trigger without 50k writes.
    with closing(open_connection(db)) as conn, conn:
        conn.execute("UPDATE sqlite_sequence SET seq = 50000 WHERE name = 'change_log'")
    with factory() as uow:
        uow.conversations.add(Conversation("new", "chat", datetime.now(UTC)))
        uow.commit()
    store = ChangeLogStore(db, factory, vault_key)
    with pytest.raises(CursorExpired):
        store.page(0)
    assert store.page(50000)["items"][0]["entity_id"] == "new"


def test_upgrade_removes_device_logs_without_rewinding_existing_cursors(tmp_path, vault_key):
    from contextlib import closing

    from limbowave.infrastructure.database.migrations import migrate
    from limbowave.infrastructure.database.sqlite_repositories import open_connection

    db = tmp_path / "sync.db"
    with closing(open_connection(db)) as conn, conn:
        migrate(conn, target=16)
        conn.execute(
            "INSERT INTO server_devices VALUES ('d', 'phone', 'hash', 'csrf', 1, 1, 99999, NULL)"
        )
        cursor = conn.execute("SELECT MAX(seq) FROM change_log").fetchone()[0]
        assert cursor > 0
    factory = sqlite_uow_factory(db, vault_key)
    store = ChangeLogStore(db, factory, vault_key)
    assert store.snapshot()["cursor"] == cursor
    assert store.page(cursor)["items"] == []
    with closing(open_connection(db)) as conn, conn:
        assert (
            conn.execute("SELECT name FROM server_devices WHERE id = 'd'").fetchone()[0] == "phone"
        )
        conn.execute("UPDATE server_devices SET touched_at = 2 WHERE id = 'd'")
    assert store.snapshot()["cursor"] == cursor
    with factory() as uow:
        uow.conversations.add(Conversation("c", "chat", datetime.now(UTC)))
        uow.commit()
    change = store.page(cursor)["items"][0]
    assert change["entity_type"] == "conversation"
    assert change["cursor"] > cursor
