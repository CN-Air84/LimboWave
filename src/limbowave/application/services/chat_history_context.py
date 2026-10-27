"""Authorized context windows over public saved messages, not runtime history."""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from datetime import datetime
from typing import Any

from limbowave.application.repositories import UnitOfWork, UnitOfWorkFactory
from limbowave.application.services.command_receipts import RuntimeConflict
from limbowave.application.services.history_pagination import _Segment, _segments
from limbowave.application.services.privacy_service import allowed_messages, load_policy
from limbowave.domain.conversation import Message, MessageRole

DEFAULT_CONTEXT_TURNS = 2
MAX_CONTEXT_TURNS = 3
# Independent hard limits protect against malformed/imported runs with many replies.
MAX_CONTEXT_SIDE_MESSAGES = 64
CONTEXT_PAGE_SIZE = 8
UNAVAILABLE = "目标消息不存在、已删除或不在可读取范围内"


def _neighbors(
    uow: UnitOfWork, conversation_id: str, segments: list[_Segment],
    index: int, anchor_key: tuple[datetime, str], *, forward: bool,
) -> Generator[Message, None, None]:
    indices = range(index, len(segments)) if forward else range(index, -1, -1)
    for i in indices:
        cursor = anchor_key if i == index else None
        while True:
            rows = uow.messages.page_chat_history(
                conversation_id, segments[i].branch_id, limit=CONTEXT_PAGE_SIZE,
                cursor=cursor, forward=forward, upper=segments[i].upper,
            )
            yield from rows
            if len(rows) < CONTEXT_PAGE_SIZE:
                break
            cursor = rows[-1].created_at, rows[-1].id


def _before(
    anchor: Message, messages: Iterator[Message], turns: int,
) -> tuple[list[Message], bool, bool]:
    rows = [anchor]
    users = int(anchor.role is MessageRole.USER)
    while len(rows) < MAX_CONTEXT_SIDE_MESSAGES and users < turns + 1:
        message = next(messages, None)
        if message is None:
            return rows, False, False
        rows.append(message)
        users += int(message.role is MessageRole.USER)
    more = next(messages, None) is not None
    return rows, more, more and users < turns + 1


def _after(messages: Iterator[Message], turns: int) -> tuple[list[Message], bool, bool]:
    rows: list[Message] = []
    users = 0
    for message in messages:
        users += int(message.role is MessageRole.USER)
        if users > turns:
            return rows, True, False
        if len(rows) == MAX_CONTEXT_SIDE_MESSAGES:
            return rows, True, True
        rows.append(message)
    return rows, False, False


def expand_context(
    factory: UnitOfWorkFactory, *, message_id: str,
    conversation_id: str | None, branch_id: str | None,
    before_turns: int, after_turns: int, max_output_bytes: int,
    requester_conversation_id: str | None, content_limit: int, title_limit: int,
) -> dict[str, Any]:
    with factory() as uow:
        uow.begin_read()
        policy = load_policy(uow, requester_conversation_id)
        position = uow.messages.history_position(message_id)
        if (position is None or (conversation_id and conversation_id != position[0])
                or (policy.fresh_start and position[0] != requester_conversation_id)):
            raise ValueError(UNAVAILABLE)
        cid, owner, created = position
        selected_branch = branch_id or owner
        try:
            segments = _segments(uow, cid, selected_branch)
        except RuntimeConflict as exc:
            raise ValueError(UNAVAILABLE) from exc
        key = created, message_id
        index = next((i for i, segment in enumerate(segments)
                      if segment.branch_id == owner and segment.contains(key)), None)
        if index is None:
            raise ValueError(UNAVAILABLE)
        conversation = uow.conversations.get(cid)
        if conversation is None or policy.blocks(conversation.title):
            raise ValueError(UNAVAILABLE)
        # Anchor lookup projects public content only, under the same read transaction.
        anchor_rows = uow.messages.page_chat_history(
            cid, owner, limit=1, upper=(created, message_id, True),
        )
        if not anchor_rows or anchor_rows[0].id != message_id or not anchor_rows[0].content:
            raise ValueError(UNAVAILABLE)
        anchor = anchor_rows[0]
        if policy.blocks(anchor.content, anchor.id):
            raise ValueError(UNAVAILABLE)
        backward = _neighbors(uow, cid, segments, index, key, forward=False)
        forward = _neighbors(uow, cid, segments, index, key, forward=True)
        try:
            before, has_before, limited_before = _before(anchor, backward, before_turns)
            after, has_after, limited_after = _after(forward, after_turns)
        finally:
            backward.close()
            forward.close()
        candidates = [*reversed(before), *after]
        # A capped leading fragment has no reliable user-turn provenance. Do not let
        # a paraphrased assistant reply bypass a blocked user at the scan boundary.
        eligible = candidates
        if policy.rules and has_before:
            start = next((i for i, m in enumerate(candidates)
                          if m.role is MessageRole.USER), len(candidates))
            eligible = candidates[start:]
        allowed = allowed_messages(policy, eligible)
        visible = [m for m in allowed if m.content]
        if not any(m.id == message_id for m in visible):
            raise ValueError(UNAVAILABLE)
        privacy_filtered = bool(policy.rules) and len(allowed) != len(candidates)
        title = '历史会话' if policy.rules else conversation.title

    rows: list[dict[str, Any]] = [{
        'conversation_id': m.conversation_id,
        'conversation_title': title[:title_limit],
        'title_truncated': len(title) > title_limit,
        'branch_id': m.branch_id,
        'message_id': m.id,
        'role': m.role.value,
        'created_at': m.created_at.isoformat(),
        'status': m.status.value,
        'content': m.content[:content_limit],
        'content_offset': 0,
        'content_truncated': len(m.content) > content_limit,
        'is_anchor': m.id == message_id,
    } for m in visible]
    output_truncated = False
    window_truncated = limited_before or limited_after

    def payload() -> dict[str, Any]:
        return {
            'source': 'application_saved_messages', 'mode': 'context',
            'order': 'oldest_first', 'anchor_message_id': message_id,
            'context_branch_id': selected_branch,
            'requested_before_turns': before_turns, 'requested_after_turns': after_turns,
            'results': rows, 'count': len(rows),
            'has_more_before': has_before, 'has_more_after': has_after,
            'window_truncated': window_truncated, 'privacy_filtered': privacy_filtered,
            'output_truncated': output_truncated,
        }

    def encoded_size() -> int:
        return len(json.dumps(payload(), ensure_ascii=False).encode('utf-8'))

    while encoded_size() > max_output_bytes and len(rows) > 1:
        output_truncated = window_truncated = True
        anchor_index = next(i for i, r in enumerate(rows) if r['is_anchor'])
        if anchor_index >= len(rows) - anchor_index - 1 and anchor_index > 0:
            rows.pop(0)
            has_before = True
        else:
            rows.pop()
            has_after = True
    if encoded_size() > max_output_bytes:
        output_truncated = True
        text = rows[0]['content']
        rows[0]['content_truncated'] = True
        low, high = 0, len(text)
        while low < high:
            size = (low + high + 1) // 2
            rows[0]['content'] = text[:size]
            if encoded_size() <= max_output_bytes:
                low = size
            else:
                high = size - 1
        rows[0]['content'] = text[:low]
    if encoded_size() > max_output_bytes or not rows[0]['content']:
        raise ValueError('聊天记录输出上限过小，无法返回锚点消息及定位信息')
    return payload()
