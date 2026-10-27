"""只读检索应用权威聊天正文；不建立明文索引，不读取运行时私有 payload。"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any

from limbowave.application.repositories import UnitOfWork, UnitOfWorkFactory
from limbowave.application.services.chat_history_context import (
    DEFAULT_CONTEXT_TURNS,
    MAX_CONTEXT_TURNS,
    expand_context,
)
from limbowave.application.services.command_receipts import RuntimeConflict
from limbowave.application.services.history_pagination import _segments
from limbowave.application.services.privacy_service import allowed_messages, load_policy
from limbowave.domain.conversation import Message, MessageRole
from limbowave.domain.privacy import PrivacyPolicy

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_OFFSET = 10_000
MAX_QUERY_CHARS = 500
MAX_ID_CHARS = 200
MAX_CONTENT_CHARS = 2000
MAX_TITLE_CHARS = 200
# Provenance reads are independent of search hits, offset and page size.
MAX_PRIVACY_TURN_MESSAGES = 64
PRIVACY_TURN_PAGE_SIZE = 8
MAX_PRIVACY_CACHE_MESSAGES = 256


def _history_turn_access(
    uow: UnitOfWork, policy: PrivacyPolicy, anchor: Message,
) -> dict[str, bool]:
    """Resolve the owning user on the effective branch, bounded and fail closed.

    Newest-first search cannot carry a user's block forward to replies. Public
    pages supply the complete preceding turn and derived timestamp/run metadata.
    """
    try:
        segments = _segments(uow, anchor.conversation_id, anchor.branch_id)
    except RuntimeConflict:
        return {anchor.id: False}
    key = anchor.created_at, anchor.id
    if not segments or not segments[-1].contains(key):
        return {anchor.id: False}
    messages: list[Message] = []
    for index in range(len(segments) - 1, -1, -1):
        segment = segments[index]
        upper = (*key, True) if index == len(segments) - 1 else segment.upper
        cursor = None
        while len(messages) < MAX_PRIVACY_TURN_MESSAGES:
            page = uow.messages.page_chat_history(
                anchor.conversation_id, segment.branch_id,
                limit=min(PRIVACY_TURN_PAGE_SIZE, MAX_PRIVACY_TURN_MESSAGES - len(messages)),
                cursor=cursor, upper=upper,
            )
            if not page:
                break
            if not messages and page[0].id != anchor.id:
                return {anchor.id: False}
            for message in page:
                messages.append(message)
                if message.role is MessageRole.USER:
                    permitted = {m.id for m in allowed_messages(policy, list(reversed(messages)))}
                    return {m.id: m.id in permitted for m in messages}
            cursor = page[-1].created_at, page[-1].id
    # Unknown provenance is not evidence that an assistant paraphrase is safe.
    # A nearer, older reply may still find its user within its own budget; do
    # not poison the cache for the entire fragment when only this anchor failed.
    return {anchor.id: False}


def validate_history_params(params: dict[str, Any]) -> None:
    """语义校验可在权限弹窗前调用；bool 不能充当分页整数。"""
    allowed = {"query", "conversation_id", "branch_id", "limit", "offset",
               "message_id", "before_turns", "after_turns"}
    if params.keys() - allowed:
        raise ValueError("聊天记录查询存在未知参数")
    if "message_id" in params:
        anchor = params["message_id"]
        if not isinstance(anchor, str) or not anchor.strip() or len(anchor) > MAX_ID_CHARS:
            raise ValueError(f"参数 message_id 必须为1–{MAX_ID_CHARS}字的非空字符串")
        if params.keys() & {"query", "limit", "offset"}:
            raise ValueError("按消息展开前后文不能同时传入 query、limit 或 offset")
    elif params.keys() & {"before_turns", "after_turns"}:
        raise ValueError("before_turns / after_turns 必须与 message_id 一起使用")
    for key in ("before_turns", "after_turns"):
        value = params.get(key, DEFAULT_CONTEXT_TURNS)
        if type(value) is not int or not 0 <= value <= MAX_CONTEXT_TURNS:
            raise ValueError(f"参数 {key} 必须为 0..{MAX_CONTEXT_TURNS} 的整数")
    for key, maximum in (
        ("query", MAX_QUERY_CHARS),
        ("conversation_id", MAX_ID_CHARS),
        ("branch_id", MAX_ID_CHARS),
    ):
        value = params.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise ValueError(f"参数 {key} 必须为不超过 {maximum} 字的字符串")
    for key, minimum, maximum, default in (
        ("limit", 1, MAX_LIMIT, DEFAULT_LIMIT),
        ("offset", 0, MAX_OFFSET, 0),
    ):
        value = params.get(key, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"参数 {key} 必须为 {minimum}..{maximum} 的整数")


def _excerpt(content: str, query: str, size: int) -> tuple[str, int]:
    """保留命中附近正文；缩小 UTF-8 预算时也不退回不相关的消息开头。"""
    folded = content.casefold()
    position = folded.find(query) if query else -1
    if position >= 0 and len(folded) != len(content):
        # casefold 可扩展字符（如 ß -> ss）；折叠后的索引不能直接切原文。
        folded_offset = 0
        for index, char in enumerate(content):
            folded_offset += len(char.casefold())
            if folded_offset > position:
                position = index
                break
    start = max(0, position - size // 3) if position >= 0 else 0
    start = min(start, max(0, len(content) - size))
    return content[start : start + size], start


class ChatHistoryService:
    """在网关授权之后执行，搜索已保存的各分支消息，不复制分支继承前缀。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def query(
        self,
        *,
        query: str | None = None,
        conversation_id: str | None = None,
        branch_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        message_id: str | None = None,
        before_turns: int | None = None,
        after_turns: int | None = None,
        max_output_bytes: int = 64 * 1024,
        requester_conversation_id: str | None = None,
    ) -> dict[str, Any]:
        validate_history_params({key: value for key, value in {
            "query": query, "conversation_id": conversation_id, "branch_id": branch_id,
            "limit": limit, "offset": offset, "message_id": message_id,
            "before_turns": before_turns, "after_turns": after_turns,
        }.items() if value is not None})
        conversation_id = (conversation_id.strip() or None) if conversation_id else None
        branch_id = (branch_id.strip() or None) if branch_id else None
        if message_id is not None:
            return expand_context(
                self._uow_factory, message_id=message_id.strip(),
                conversation_id=conversation_id, branch_id=branch_id,
                before_turns=DEFAULT_CONTEXT_TURNS if before_turns is None else before_turns,
                after_turns=DEFAULT_CONTEXT_TURNS if after_turns is None else after_turns,
                max_output_bytes=max_output_bytes,
                requester_conversation_id=requester_conversation_id,
                content_limit=MAX_CONTENT_CHARS, title_limit=MAX_TITLE_CHARS,
            )
        query = (query or "").strip().casefold()
        limit = DEFAULT_LIMIT if limit is None else limit
        offset = 0 if offset is None else offset
        rows: list[dict[str, Any]] = []
        originals: list[Message] = []
        has_more = False
        skipped = 0
        with self._uow_factory() as uow:
            uow.begin_read()
            policy = load_policy(uow, requester_conversation_id)
            if policy.fresh_start:
                conversation_id = requester_conversation_id
            conversations = (
                {c.id: c for c in uow.conversations.list_all()} if conversation_id is None else {}
            )
            if conversation_id is not None:
                conversation = uow.conversations.get(conversation_id)
                if conversation is not None:
                    conversations[conversation.id] = conversation
            turn_cache: OrderedDict[tuple[str, str], bool] = OrderedDict()

            def permits(message: Message) -> bool:
                if not policy.rules:
                    return True
                if message.role is MessageRole.USER:
                    return not policy.blocks(message.content, message.id)
                key = message.branch_id, message.id
                if key not in turn_cache:
                    turn_access = _history_turn_access(uow, policy, message)
                    for mid, permitted in turn_access.items():
                        turn_cache[message.branch_id, mid] = permitted
                        while len(turn_cache) > MAX_PRIVACY_CACHE_MESSAGES:
                            turn_cache.popitem(last=False)
                return turn_cache.get(key, False)

            messages = uow.messages.iter_chat_history(
                conversation_id=conversation_id,
                branch_id=branch_id,
            )
            try:
                for message in messages:
                    conversation = conversations.get(message.conversation_id)
                    if (conversation is None or not message.content
                            or not permits(message)
                            or policy.blocks(conversation.title)):
                        continue
                    visible_title = "历史会话" if policy.rules else conversation.title
                    if (
                        query
                        and query not in visible_title.casefold()
                        and query not in message.content.casefold()
                    ):
                        continue
                    if skipped < offset:
                        skipped += 1
                        continue
                    if len(rows) == limit:
                        has_more = True
                        break
                    content, start = _excerpt(message.content, query, MAX_CONTENT_CHARS)
                    title = visible_title[:MAX_TITLE_CHARS]
                    rows.append(
                        {
                            "conversation_id": message.conversation_id,
                            "conversation_title": title,
                            "title_truncated": title != visible_title,
                            "branch_id": message.branch_id,
                            "message_id": message.id,
                            "role": message.role.value,
                            "created_at": message.created_at.isoformat(),
                            "status": message.status.value,
                            "content": content,
                            "content_offset": start,
                            "content_truncated": len(content) != len(message.content),
                        }
                    )
                    originals.append(message)
            finally:
                messages.close()

        output_truncated = False

        def payload() -> dict[str, Any]:
            next_offset = offset + len(rows) if has_more else None
            return {
                "source": "application_saved_messages",
                "order": "newest_first",
                "results": rows,
                "count": len(rows),
                "has_more": has_more,
                "next_offset": next_offset
                if next_offset is None or next_offset <= MAX_OFFSET
                else None,
                "offset_limit_reached": next_offset is not None and next_offset > MAX_OFFSET,
                "output_truncated": output_truncated,
            }

        def encoded_size() -> int:
            return len(json.dumps(payload(), ensure_ascii=False).encode("utf-8"))

        # 分页先限条数，输出再限总 UTF-8 字节；移出的消息在下一页仍可读取。
        while encoded_size() > max_output_bytes and len(rows) > 1:
            rows.pop()
            originals.pop()
            has_more = output_truncated = True
        if encoded_size() > max_output_bytes and rows:
            output_truncated = True
            original = originals[0]
            low, high = 0, len(rows[0]["content"])
            while low < high:
                size = (low + high + 1) // 2
                text, start = _excerpt(original.content, query, size)
                rows[0].update(content=text, content_offset=start, content_truncated=True)
                if encoded_size() <= max_output_bytes:
                    low = size
                else:
                    high = size - 1
            text, start = _excerpt(original.content, query, low)
            rows[0].update(content=text, content_offset=start, content_truncated=True)
        if encoded_size() > max_output_bytes or (rows and not rows[0]["content"]):
            raise ValueError("聊天记录输出上限过小，无法返回消息及定位信息")
        return payload()
