"""Durable, transaction-local change log and bounded snapshot for server sync."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.domain.conversation import AssistantMessageSegment
from limbowave.domain.tool_step import ToolStep
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.database.migrations import migrate
from limbowave.infrastructure.database.sqlite_repositories import open_connection


class CursorExpired(RuntimeError):
    """The client cursor is older than retained change-log history."""


class ChangeLogStore:
    """Read the change log and build a full snapshot from the authoritative UoW."""

    def __init__(
        self,
        database: Path,
        uow_factory: UnitOfWorkFactory | None = None,
        key: VaultKey | None = None,
    ) -> None:
        self.database = database
        self._uow_factory = uow_factory
        self._key = key
        with closing(open_connection(database)) as conn, conn:
            migrate(conn)

    def page(self, after: int, *, limit: int = 100) -> dict[str, Any]:
        if type(after) is not int or after < 0 or not 1 <= limit <= 500:
            raise ValueError("invalid cursor")

        with closing(open_connection(self.database)) as conn:
            bounds = conn.execute(
                "SELECT COALESCE(MIN(seq), 0), COALESCE(MAX(seq), 0) FROM change_log"
            ).fetchone()
            assert bounds is not None
            floor, latest = int(bounds[0]), int(bounds[1])
            revision_row = conn.execute("SELECT revision FROM sync_state WHERE id = 1").fetchone()
            revision = int(revision_row[0]) if revision_row else 0
            if after < floor - 1 or (floor == 0 and after < revision):
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

        cursor, revision = self._cursor_state()
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

    def _cursor_state(self) -> tuple[int, int]:
        with closing(open_connection(self.database)) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0), "
                "COALESCE((SELECT revision FROM sync_state WHERE id = 1), 0) "
                "FROM change_log"
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
