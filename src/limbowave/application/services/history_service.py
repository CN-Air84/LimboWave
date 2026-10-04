"""会话历史搜索（设计计划 §十四.1 首版范围）。

首版只搜三样东西：**会话标题、用户消息、助手最终回答**。
明确不搜：文件片段、工具结果、请求日志、reasoning、压缩内部提示词。

实现路径（ADR-0002 的既定取舍）：字段级加密下 FTS5 索引不了密文，
因此搜索在**解密后的内存数据**上进行——``UoW`` 读出的消息本来就是明文的
（解密在仓库层完成），搜索只是纯字符串匹配。数据量上去之后再评估
「解密后建内存索引」的性能，现在不做提前优化。

时间筛选：按消息/会话的创建时间过滤，区间是 [start, end] 闭区间。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from limbowave.application.branch_path import branch_messages, current_branch
from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.domain.conversation import Conversation, Message, MessageRole


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """会话列表行。消息数与最后活动时间供列表展示与排序。"""

    conversation: Conversation
    message_count: int
    last_activity: datetime


@dataclass(frozen=True, slots=True)
class SearchHit:
    """一条搜索命中。``message`` 为 None 表示命中的是会话标题。"""

    conversation: Conversation
    message: Message | None
    matched_field: str  # "title" | "user" | "assistant"
    snippet: str


class HistoryService:
    """会话列表与历史搜索。只读，不改任何数据。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    # ---------- 会话列表 ----------

    def list_conversations(self) -> list[ConversationSummary]:
        """全部会话，按最后活动倒序。"""
        with self._uow_factory() as uow:
            summaries: list[ConversationSummary] = []
            for conversation in uow.conversations.list_all():
                count = 0
                last = conversation.created_at
                for branch in uow.branches.list_for_conversation(conversation.id):
                    messages = uow.messages.list_for_branch(branch.id)
                    count += len(messages)
                    for message in messages:
                        if message.created_at > last:
                            last = message.created_at
                summaries.append(
                    ConversationSummary(
                        conversation=conversation,
                        message_count=count,
                        last_activity=last,
                    )
                )
            summaries.sort(key=lambda s: s.last_activity, reverse=True)
            return summaries

    def open_conversation(self, conversation_id: str) -> tuple[str, list[Message]] | None:
        """取会话的当前分支与其**完整对话**（切会话时渲染用）。会话不存在返回 None。

        当前分支 = 最近有活动的分支；对话含分叉点之前从父分支继承的前缀。
        """
        with self._uow_factory() as uow:
            if uow.conversations.get(conversation_id) is None:
                return None
            branch = current_branch(uow, conversation_id)
            if branch is None:
                return None
            return branch.id, branch_messages(uow, branch.id)

    def branch_messages(self, branch_id: str) -> list[Message]:
        """指定分支的完整对话（含从父分支继承的前缀）。分支不存在返回空。"""
        with self._uow_factory() as uow:
            return branch_messages(uow, branch_id)

    def title(self, conversation_id: str) -> str | None:
        """读取单个会话标题；会话不存在返回 None。"""
        with self._uow_factory() as uow:
            conversation = uow.conversations.get(conversation_id)
            return conversation.title if conversation is not None else None

    def list_branches(self, conversation_id: str) -> list[tuple[str, str, int]]:
        """一个会话的分支列表：(branch_id, 描述, 消息数)，按创建时间正序。

        未自定义标题时，按本地时间使用 mm-dd hh:mm 新分支。
        """
        with self._uow_factory() as uow:
            conversation = uow.conversations.get(conversation_id)
            if conversation is None:
                return []
            branches = uow.branches.list_for_conversation(conversation_id)
            out: list[tuple[str, str, int]] = []
            for branch in branches:
                count = uow.messages.count_for_branch(branch.id)
                label = branch.title or f"{branch.created_at.astimezone():%m-%d %H:%M} 新分支"
                out.append((branch.id, label, count))
            return out

    def rename_branch(self, branch_id: str, title: str) -> bool:
        """重命名单个分支。空标题拒绝，分支不存在返回 False。"""
        title = title.strip()
        if not title:
            return False
        with self._uow_factory() as uow:
            branch = uow.branches.get(branch_id)
            if branch is None:
                return False
            uow.branches.update(replace(branch, title=title))
            uow.commit()
            return True

    def delete_branch(self, branch_id: str) -> bool:
        """删除单个分支及其直属数据；每个会话至少保留一条分支。"""
        with self._uow_factory() as uow:
            branch = uow.branches.get(branch_id)
            if branch is None:
                return False
            branches = uow.branches.list_for_conversation(branch.conversation_id)
            if len(branches) <= 1:
                return False
            uow.branches.delete(branch_id)
            uow.commit()
            return True

    # ---------- 会话管理（Task 3.1：重命名、删除） ----------

    def rename(self, conversation_id: str, title: str) -> bool:
        """重命名会话。空标题拒绝，会话不存在返回 False。"""
        title = title.strip()
        if not title:
            return False
        with self._uow_factory() as uow:
            conversation = uow.conversations.get(conversation_id)
            if conversation is None:
                return False
            uow.conversations.update(replace(conversation, title=title))
            uow.commit()
            return True

    def delete(self, conversation_id: str) -> bool:
        """删除会话及其全部下级数据（级联）。不存在返回 False。

        删除是**不可撤销**的，确认动作在 UI 层做；这里只执行。
        """
        with self._uow_factory() as uow:
            if uow.conversations.get(conversation_id) is None:
                return False
            uow.conversations.delete(conversation_id)
            uow.commit()
            return True

    # ---------- 搜索 ----------

    def search(
        self,
        query: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[SearchHit]:
        """搜索标题、用户消息、助手最终回答。空查询返回空。"""
        query = query.strip()
        if not query:
            return []
        needle = query.casefold()

        hits: list[SearchHit] = []
        with self._uow_factory() as uow:
            for conversation in uow.conversations.list_all():
                # 标题命中（不受时间筛选——标题属于会话本身）
                if needle in conversation.title.casefold():
                    hits.append(
                        SearchHit(
                            conversation=conversation,
                            message=None,
                            matched_field="title",
                            snippet=_snippet(conversation.title, query),
                        )
                    )

                for branch in uow.branches.list_for_conversation(conversation.id):
                    for message in uow.messages.list_for_branch(branch.id):
                        if not _in_range(message.created_at, start, end):
                            continue
                        # 只搜用户消息与助手终答；partial 的助手消息也算"最终回答"
                        # （它是用户停止后保留的部分，仍是可见回答）
                        if message.role is MessageRole.USER:
                            field = "user"
                        elif message.role is MessageRole.ASSISTANT:
                            field = "assistant"
                        else:
                            continue
                        if needle in message.content.casefold():
                            hits.append(
                                SearchHit(
                                    conversation=conversation,
                                    message=message,
                                    matched_field=field,
                                    snippet=_snippet(message.content, query),
                                )
                            )
        return hits


def _in_range(moment: datetime, start: datetime | None, end: datetime | None) -> bool:
    if start is not None and moment < start:
        return False
    return not (end is not None and moment > end)


def _snippet(text: str, query: str, radius: int = 40) -> str:
    """以命中处为中心截取片段。大小写不敏感定位，保留原文大小写。"""
    index = text.casefold().find(query.casefold())
    if index < 0:
        return text[: radius * 2]
    begin = max(0, index - radius)
    end = min(len(text), index + len(query) + radius)
    prefix = "…" if begin > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[begin:end] + suffix
