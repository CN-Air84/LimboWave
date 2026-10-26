"""Desktop compatibility with the published server-branch v18 schema."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from limbowave.domain.conversation import Conversation
from limbowave.infrastructure.database.migrations import (
    CURRENT_VERSION,
    MigrationError,
    current_version,
    migrate,
)
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory


def _schema(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()


def test_upgrade_v13_preserves_encrypted_content_and_adds_server_schema() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        migrate(conn, target=13)
        conn.execute(
            "INSERT INTO conversations (id, title_enc, created_at) VALUES (?, ?, ?)",
            ("existing", "opaque-encrypted-title", "2026-10-13"),
        )
        conn.commit()
        original = conn.execute("SELECT * FROM conversations").fetchall()

        assert migrate(conn) == CURRENT_VERSION
        assert current_version(conn) == CURRENT_VERSION
        assert conn.execute("SELECT * FROM conversations").fetchall() == original
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"server_devices", "server_commands", "sync_state", "change_log"} <= tables
        indexes = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {
            "idx_messages_conversation_activity",
            "idx_mirrors_conversation_topology",
        } <= indexes
        assert conn.execute("SELECT revision FROM sync_state").fetchone() == (0,)


def test_reopen_existing_v18_database_changes_neither_schema_nor_rows(tmp_path: Path) -> None:
    path = tmp_path / "server.db"
    with closing(sqlite3.connect(path)) as conn:
        assert migrate(conn, target=18) == 18
        conn.execute(
            "INSERT INTO conversations (id, title_enc, created_at) VALUES (?, ?, ?)",
            ("existing", "opaque-encrypted-title", "2026-10-13"),
        )
        conn.commit()
        original = list(conn.iterdump())
        schema = _schema(conn)
    with closing(sqlite3.connect(path)) as conn:
        assert migrate(conn, target=18) == 18
        assert conn.total_changes == 0
        assert _schema(conn) == schema
        assert list(conn.iterdump()) == original


def test_desktop_repository_writes_and_rollbacks_keep_sync_state_consistent(
    tmp_path: Path, vault_key
) -> None:
    path = tmp_path / "desktop.db"
    factory = sqlite_uow_factory(path, vault_key)
    conversation = Conversation(
        id="desktop", title="encrypted desktop title", created_at=datetime.now(UTC)
    )
    try:
        with factory() as uow:
            uow.conversations.add(conversation)
            uow.commit()
        with closing(sqlite3.connect(path)) as conn:
            assert (
                conn.execute("SELECT title_enc FROM conversations").fetchone()[0]
                != conversation.title
            )
            assert conn.execute("SELECT revision FROM sync_state").fetchone() == (1,)
            assert conn.execute(
                "SELECT entity_type, entity_id, scope, operation, revision FROM change_log"
            ).fetchall() == [("conversation", "desktop", "conversation:desktop", "upsert", 1)]
        with factory() as uow:
            assert uow.conversations.get(conversation.id) == conversation
            uow.conversations.update(replace(conversation, title="rolled back"))
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT revision FROM sync_state").fetchone() == (1,)
            assert conn.execute("SELECT COUNT(*) FROM change_log").fetchone() == (1,)
        with factory() as uow:
            assert uow.conversations.get(conversation.id) == conversation
    finally:
        factory.close()


def test_newer_library_is_still_rejected_without_changes() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        migrate(conn)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, datetime('now'))",
            (CURRENT_VERSION + 1,),
        )
        conn.commit()
        original = list(conn.iterdump())
        with pytest.raises(MigrationError, match="拒绝降级"):
            migrate(conn)
        assert list(conn.iterdump()) == original
