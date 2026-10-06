"""Bounded keyset history; branch ancestry is traversed using metadata only."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from limbowave.application.repositories import UnitOfWork, UnitOfWorkFactory
from limbowave.application.services.command_receipts import RuntimeConflict
from limbowave.application.services.public_tool_steps import public_tool_steps
from limbowave.domain.conversation import Branch, Message


@dataclass
class _Segment:
    branch_id: str
    upper: tuple[datetime, str, bool] | None = None

    def contains(self, key: tuple[datetime, str]) -> bool:
        return (
            self.upper is None or key < self.upper[:2] or (self.upper[2] and key == self.upper[:2])
        )


def _limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise RuntimeConflict("invalid_limit", 400)


def _encode(scope: list[str], branch: str, key: tuple[datetime, str]) -> str:
    payload = [1, scope, branch, key[0].astimezone(UTC).isoformat(), key[1]]
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _decode(cursor: str | None, scope: list[str]) -> tuple[str, datetime, str] | None:
    if cursor is None:
        return None
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw)
        if (
            not isinstance(data, list)
            or len(data) != 5
            or type(data[0]) is not int
            or data[0] != 1
            or data[1] != scope
            or not all(isinstance(x, str) for x in data[2:])
            or not data[4]
        ):
            raise ValueError
        timestamp = datetime.fromisoformat(data[3])
        if timestamp.tzinfo is None:
            raise ValueError
        return data[2], timestamp.astimezone(UTC), data[4]
    except (ValueError, TypeError, binascii.Error, UnicodeError) as exc:
        raise RuntimeConflict("invalid_cursor", 400) from exc


def _segments(uow: UnitOfWork, conversation_id: str, branch_id: str) -> list[_Segment]:
    chain: list[Branch] = []
    seen: set[str] = set()
    current: str | None = branch_id
    while current is not None:
        if current in seen or len(chain) >= 256:
            raise RuntimeConflict("branch_not_found", 404)
        branch = uow.branches.get(current)
        if branch is None or branch.conversation_id != conversation_id:
            raise RuntimeConflict("branch_not_found", 404)
        chain.append(branch)
        seen.add(current)
        current = branch.parent_branch_id
    segments: list[_Segment] = []
    for child in reversed(chain):
        if child.forked_from_message_id is not None:
            position = uow.messages.history_position(child.forked_from_message_id)
            index = next(
                (
                    i
                    for i, segment in enumerate(segments)
                    if position is not None
                    and position[0] == conversation_id
                    and segment.branch_id == position[1]
                    and segment.contains((position[2], child.forked_from_message_id))
                ),
                None,
            )
            if index is not None and position is not None:
                segments = segments[: index + 1]
                segments[-1].upper = (
                    position[2],
                    child.forked_from_message_id,
                    child.include_fork_message,
                )
            else:
                # Match branch_path's missing/non-visible fork fallback, without payload reads.
                bound = (child.created_at, "", False)
                for segment in segments:
                    if segment.upper is None or bound < segment.upper:
                        segment.upper = bound
        segments.append(_Segment(child.id))
    return segments


def page_conversations(
    factory: UnitOfWorkFactory,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    _limit(limit)
    scope = ["conversations"]
    decoded = _decode(cursor, scope)
    if decoded is not None and decoded[0] != "":
        raise RuntimeConflict("invalid_cursor", 400)
    before = (decoded[1], decoded[2]) if decoded else None
    with factory() as uow:
        rows = uow.conversations.page_summaries(limit=limit + 1, before=before)
    visible = rows[:limit]
    return {
        "items": [
            {"id": c.id, "title": c.title, "branch_id": b, "created_at": c.created_at.isoformat()}
            for c, b in visible
        ],
        "next_cursor": _encode(scope, "", (visible[-1][0].created_at, visible[-1][0].id))
        if len(rows) > limit
        else None,
    }


def page_messages(
    factory: UnitOfWorkFactory,
    conversation_id: str,
    branch_id: str,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    _limit(limit)
    scope = ["messages", conversation_id, branch_id]
    decoded = _decode(cursor, scope)
    with factory() as uow:
        segments = _segments(uow, conversation_id, branch_id)
        if decoded is not None:
            index = next((i for i, s in enumerate(segments) if s.branch_id == decoded[0]), None)
            if index is None or not segments[index].contains((decoded[1], decoded[2])):
                raise RuntimeConflict("invalid_cursor", 400)
            segments = segments[: index + 1]
        rows: list[Message] = []
        for segment in reversed(segments):
            before = (
                (decoded[1], decoded[2]) if decoded and segment.branch_id == decoded[0] else None
            )
            rows.extend(
                uow.messages.page_history(
                    conversation_id,
                    segment.branch_id,
                    limit=limit + 1 - len(rows),
                    before=before,
                    upper=segment.upper,
                )
            )
            if len(rows) > limit:
                break
    visible = rows[:limit]
    return {
        "items": [
            {
                "id": m.id,
                "role": m.role.value,
                "content": m.content,
                "thinking": m.thinking,
                "status": m.status.value,
                "run_id": m.run_id,
                "tools": public_tool_steps(m.tool_steps),
                "created_at": m.created_at.isoformat(),
            }
            for m in reversed(visible)
        ],
        "next_cursor": _encode(
            scope, visible[-1].branch_id, (visible[-1].created_at, visible[-1].id)
        )
        if len(rows) > limit
        else None,
    }
