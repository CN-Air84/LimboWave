"""只读检索应用权威聊天正文；不建立明文索引，不读取运行时私有 payload。"""

from __future__ import annotations

import json
from typing import Any

from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.domain.conversation import Message

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_OFFSET = 10_000
MAX_QUERY_CHARS = 500
MAX_ID_CHARS = 200
MAX_CONTENT_CHARS = 2000
MAX_TITLE_CHARS = 200


def validate_history_params(params: dict[str, Any]) -> None:
    """语义校验可在权限弹窗前调用；bool 不能充当分页整数。"""
    allowed = {"query", "conversation_id", "branch_id", "limit", "offset"}
    if params.keys() - allowed:
        raise ValueError("聊天记录查询存在未知参数")
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
        query: str = "",
        conversation_id: str | None = None,
        branch_id: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        max_output_bytes: int = 64 * 1024,
    ) -> dict[str, Any]:
        validate_history_params(
            {
                "query": query,
                "conversation_id": conversation_id,
                "branch_id": branch_id,
                "limit": limit,
                "offset": offset,
            }
        )
        query = query.strip().casefold()
        conversation_id = (conversation_id.strip() or None) if conversation_id else None
        branch_id = (branch_id.strip() or None) if branch_id else None
        rows: list[dict[str, Any]] = []
        originals: list[Message] = []
        has_more = False
        skipped = 0
        with self._uow_factory() as uow:
            conversations = (
                {c.id: c for c in uow.conversations.list_all()} if conversation_id is None else {}
            )
            if conversation_id is not None:
                conversation = uow.conversations.get(conversation_id)
                if conversation is not None:
                    conversations[conversation.id] = conversation
            messages = uow.messages.iter_chat_history(
                conversation_id=conversation_id,
                branch_id=branch_id,
            )
            try:
                for message in messages:
                    conversation = conversations.get(message.conversation_id)
                    if conversation is None or not message.content:
                        continue
                    if (
                        query
                        and query not in conversation.title.casefold()
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
                    title = conversation.title[:MAX_TITLE_CHARS]
                    rows.append(
                        {
                            "conversation_id": message.conversation_id,
                            "conversation_title": title,
                            "title_truncated": title != conversation.title,
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
