"""Build a current-memory gallery without reviving snapshots or deleted content."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal

from limbowave.application.repositories import UnitOfWork
from limbowave.domain.conversation import Conversation
from limbowave.domain.footprint import FootprintLocation, MemoryFootprint, memory_datetime
from limbowave.domain.memory import MemoryItem


def build_memory_timeline(uow: UnitOfWork) -> list[MemoryFootprint]:
    conversations: dict[str, Conversation | None] = {}
    locations: dict[str, FootprintLocation | None] = {}

    def location(branch_id: str) -> FootprintLocation | None:
        if branch_id not in locations:
            branch = uow.branches.get(branch_id)
            locations[branch_id] = None
            if branch is not None:
                cid = branch.conversation_id
                if cid not in conversations:
                    conversations[cid] = uow.conversations.get(cid)
                conversation = conversations[cid]
                if conversation is not None:
                    locations[branch_id] = FootprintLocation(
                        cid,
                        branch.id,
                        conversation.title,
                        branch.title or "未命名分支",
                    )
        return locations[branch_id]

    grouped: dict[tuple[str, MemoryItem], MemoryFootprint] = {}
    originals: dict[tuple[str, str], MemoryItem] = {}
    for document in uow.memories.list_current():
        scope: Literal["global", "session"] = "global" if document.id == "global" else "session"
        loc = location(document.branch_id) if document.branch_id else None
        if scope == "session" and (loc is None or loc.conversation_id != document.conversation_id):
            continue
        for payload in json.loads(document.payload):
            item = MemoryItem(**payload)
            key = scope, item
            row = grouped.get(key, MemoryFootprint(item, scope))
            if loc is not None:
                row = replace(row, locations=(*row.locations, loc))
                originals[(loc.branch_id, item.id)] = item
            grouped[key] = row

    origins: dict[tuple[str, str], tuple[FootprintLocation | None, str | None, str | None]] = {}

    def model_origin(
        item: MemoryItem,
        fallback: FootprintLocation | None,
    ) -> tuple[FootprintLocation | None, str | None, str | None]:
        cid = fallback.conversation_id if fallback else ""
        key = item.source, cid
        if key not in origins:
            result: tuple[FootprintLocation | None, str | None, str | None] = (None, None, None)
            parts = item.source.split(":", 2)
            if len(parts) == 3 and parts[0] == "model":
                run = uow.runs.get(parts[1])
                if run is not None and run.conversation_id == cid:
                    origin = location(run.branch_id)
                    message = uow.messages.get(run.user_message_id)
                    if (
                        origin is not None
                        and message is not None
                        and message.conversation_id == cid
                        and message.branch_id == run.branch_id
                    ):
                        result = origin, message.id, message.created_at.isoformat()
            origins[key] = result
        origin, message_id, written_at = origins[key]
        return origin or fallback, message_id, written_at

    rows = []
    for row in grouped.values():
        fallback = row.locations[0] if row.locations else None
        item = row.memory
        if row.source_kind == "promoted":
            parts = item.source.split(":", 2)
            if len(parts) == 3:
                fallback = location(parts[1])
                item = originals.get((parts[1], parts[2]), item)
        origin, message_id, written_at = model_origin(item, fallback)
        rows.append(
            replace(row, origin=origin, source_message_id=message_id, written_at=written_at)
        )
    # Invalid legacy dates are retained, but always placed after known instants.
    floor = datetime.min.replace(tzinfo=UTC)
    return sorted(
        rows,
        key=lambda row: (
            memory_datetime(row.memory.created_at) or floor,
            row.memory.id,
            row.scope,
            row.memory.content,
        ),
        reverse=True,
    )
