"""Durable, transaction-local change log and bounded snapshot for server sync."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.domain.conversation import AssistantMessageSegment
from limbowave.domain.tool_step import ToolStep
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.database.migrations import migrate
from limbowave.infrastructure.database.sqlite_repositories import SqliteUnitOfWork, open_connection


class CursorExpired(RuntimeError):
    """The client cursor is older than retained change-log history."""


class SnapshotTooLarge(RuntimeError):
    """A full snapshot exceeds the bounded export budget; no cursor may be saved."""


class ChangeLogStore:
    """Read the change log and build a full snapshot from the authoritative UoW."""

    def __init__(
        self,
        database: Path,
        uow_factory: UnitOfWorkFactory | None = None,
        key: VaultKey | None = None,
        *,
        max_snapshot_entities: int = 10_000,
        max_snapshot_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if max_snapshot_entities <= 0 or max_snapshot_bytes <= 0:
            raise ValueError("snapshot limits must be positive")
        self.max_snapshot_entities = max_snapshot_entities
        self.max_snapshot_bytes = max_snapshot_bytes
        self.database = database
        self._uow_factory = uow_factory
        self._key = key
        with closing(open_connection(database)) as conn, conn:
            migrate(conn)

    def page(self, after: int, *, limit: int = 100) -> dict[str, Any]:
        if type(after) is not int or after < 0 or not 1 <= limit <= 500:
            raise ValueError("invalid cursor")

        with closing(open_connection(self.database)) as conn:
            conn.execute("BEGIN")
            latest, _ = self._cursor_state(conn)
            bounds = conn.execute(
                "SELECT COALESCE(MIN(seq), 0) FROM change_log"
            ).fetchone()
            assert bounds is not None
            floor = int(bounds[0])
            if after < floor - 1 or (floor == 0 and after < latest):
                raise CursorExpired("cursor expired")

            rows = conn.execute(
                "SELECT seq, entity_type, entity_id, scope, operation, revision, created_at "
                "FROM change_log WHERE seq > ? ORDER BY seq LIMIT ?",
                (after, limit + 1),
            ).fetchall()

        page = rows[:limit]
        next_cursor = int(page[-1][0]) if page else after
        return {
            "items": [
                {
                    "cursor": int(row[0]),
                    "entity_type": row[1],
                    "entity_id": row[2],
                    "scope": row[3],
                    "operation": row[4],
                    "revision": int(row[5]),
                    "created_at": row[6],
                }
                for row in page
            ],
            "cursor": next_cursor,
            "latest_cursor": latest,
            "has_more": len(rows) > limit,
            "resync_required": False,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return the current authoritative state plus the latest sync cursor.

        The snapshot intentionally contains only entities the authenticated client is
        allowed to read. Device authorization is kept out of this payload.
        """
        if self._uow_factory is None or self._key is None:
            raise RuntimeError("snapshot unavailable")

        conversations: list[dict[str, Any]] = []
        branches: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        runs: list[dict[str, Any]] = []

        with self._uow_factory() as uow:
            if not isinstance(uow, SqliteUnitOfWork):
                raise RuntimeError("snapshot requires a SQLite read transaction")
            conn = uow.begin_read()
            cursor, revision = self._cursor_state(conn)
            self._check_snapshot_budget(conn)
            for conversation in uow.conversations.list_all():
                branch_rows = uow.branches.list_for_conversation(conversation.id)
                conversations.append(
                    {
                        "id": conversation.id,
                        "title": conversation.title,
                        "created_at": _iso(conversation.created_at),
                        "default_logical_model_id": conversation.default_logical_model_id,
                        "branches": [
                            {"id": branch.id, "title": branch.title} for branch in branch_rows
                        ],
                    }
                )
                for branch in branch_rows:
                    branches.append(_branch(branch))
                    for message in uow.messages.list_for_branch(branch.id):
                        messages.append(_message(message))
                for run in uow.runs.list_for_conversation(conversation.id):
                    runs.append(_run(run))

            memories = [_memory(document) for document in uow.memories.list_all()]

        return {
            "cursor": cursor,
            "revision": revision,
            "conversations": conversations,
            "branches": branches,
            "messages": messages,
            "runs": runs,
            "memories": memories,
            "truncated": False,
        }

    def prune_through(self, cursor: int) -> int:
        if type(cursor) is not int or cursor < 0:
            raise ValueError("invalid cursor")
        with closing(open_connection(self.database)) as conn, conn:
            result = conn.execute("DELETE FROM change_log WHERE seq <= ?", (cursor,))
            return result.rowcount

    def _check_snapshot_budget(self, conn: sqlite3.Connection) -> None:
        # Check persisted byte lengths before allocating/decrypting entity lists.
        # Table/column identifiers come only from this fixed local schema.
        entities = size = 0
        for table in ("conversations", "branches", "messages", "runs", "memory_documents"):
            columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
            lengths = " + ".join(
                f'COALESCE(length(CAST("{column[1]}" AS BLOB)), 0)' for column in columns
            )
            for row in conn.execute(
                f"SELECT {lengths} FROM {table} LIMIT ?",
                (self.max_snapshot_entities - entities + 1,),
            ):
                entities += 1
                size += int(row[0])
                if entities > self.max_snapshot_entities or size > self.max_snapshot_bytes:
                    raise SnapshotTooLarge("snapshot exceeds export budget")

    @staticmethod
    def _cursor_state(conn: sqlite3.Connection) -> tuple[int, int]:
        # sqlite_sequence preserves the high-water mark even if every row is pruned.
        row = conn.execute(
            "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'change_log'), 0), "
            "COALESCE((SELECT revision FROM sync_state WHERE id = 1), 0)"
        ).fetchone()
        assert row is not None
        return int(row[0]), int(row[1])


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _branch(branch: Any) -> dict[str, Any]:
    return {
        "id": branch.id,
        "conversation_id": branch.conversation_id,
        "created_at": _iso(branch.created_at),
        "parent_branch_id": branch.parent_branch_id,
        "forked_from_message_id": branch.forked_from_message_id,
        "title": branch.title,
        "include_fork_message": branch.include_fork_message,
    }


def _message(message: Any) -> dict[str, Any]:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "branch_id": message.branch_id,
        "role": message.role.value,
        "content": message.content,
        "thinking": message.thinking,
        "status": message.status.value,
        "created_at": _iso(message.created_at),
        "run_id": message.run_id,
        "tool_steps": [_tool_step(step) for step in message.tool_steps],
        "segments": [_segment(segment) for segment in message.segments],
    }


def _run(run: Any) -> dict[str, Any]:
    return {
        "id": run.id,
        "conversation_id": run.conversation_id,
        "branch_id": run.branch_id,
        "status": run.status.value,
        "created_at": _iso(run.created_at),
        "user_message_id": run.user_message_id,
        "assistant_message_id": run.assistant_message_id,
        "finished_at": _iso(run.finished_at),
        "stop_reason": run.stop_reason,
        "retry_of_message_id": run.retry_of_message_id,
    }


def _memory(document: Any) -> dict[str, Any]:
    return {
        "id": document.id,
        "payload": document.payload,
        "conversation_id": document.conversation_id,
        "branch_id": document.branch_id,
    }


def _tool_step(step: ToolStep) -> dict[str, Any]:
    return {
        "tool_call_id": step.tool_call_id,
        "name": step.name,
        "status": step.status.value if hasattr(step.status, "value") else step.status,
        "created_at_ms": step.created_at_ms,
        "duration_ms": step.duration_ms,
        "args": step.args,
        "result_summary": step.result_summary,
        "error": step.error,
    }


def _segment(segment: AssistantMessageSegment) -> dict[str, Any]:
    return {
        "content": segment.content,
        "thinking": segment.thinking,
        "tool_call_ids": list(segment.tool_call_ids),
    }
