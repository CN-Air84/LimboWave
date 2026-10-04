"""历史搜索验收（设计计划 §十四.1 首版范围）。

锁住的不变量：
- 只搜标题、用户消息、助手终答；reasoning/工具类内容不在搜索面。
- 空查询返回空；大小写不敏感。
- 时间筛选是 [start, end] 闭区间，只作用于消息（标题属于会话本身）。
- 列表按最后活动倒序。
- 搜索跑在 SQLite 后端上同样成立（解密后内存搜索，ADR-0002 既定取舍）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from limbowave.application.services.history_service import HistoryService
from limbowave.domain.conversation import (
    Branch,
    Conversation,
    Message,
    MessageRole,
    MessageStatus,
)
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory

T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def _seed(store_factory) -> None:
    """两个会话：一个聊部署，一个聊烹饪。"""
    with store_factory() as uow:
        uow.conversations.add(Conversation(id="c1", title="部署讨论", created_at=T0))
        uow.branches.add(Branch(id="b1", conversation_id="c1", created_at=T0))
        uow.messages.add(
            Message(
                id="m1",
                conversation_id="c1",
                branch_id="b1",
                role=MessageRole.USER,
                content="怎么部署到生产环境？",
                created_at=T0,
                run_id="r1",
            )
        )
        uow.messages.add(
            Message(
                id="m2",
                conversation_id="c1",
                branch_id="b1",
                role=MessageRole.ASSISTANT,
                content="先配置 CI 流水线。",
                created_at=T0 + timedelta(minutes=1),
                run_id="r1",
                status=MessageStatus.COMPLETE,
            )
        )
        uow.conversations.add(Conversation(id="c2", title="周末计划", created_at=T0))
        uow.branches.add(Branch(id="b2", conversation_id="c2", created_at=T0))
        uow.messages.add(
            Message(
                id="m3",
                conversation_id="c2",
                branch_id="b2",
                role=MessageRole.USER,
                content="推荐一个红烧肉做法",
                created_at=T0 + timedelta(days=2),
                run_id="r2",
            )
        )
        uow.commit()


@pytest.fixture(params=["memory", "sqlite"])
def history(request, tmp_path: Path, vault_key: VaultKey) -> HistoryService:
    """同一组语义跑在两种后端上——端口契约的又一证据。"""
    if request.param == "memory":
        factory = in_memory_uow_factory(InMemoryStore())
    else:
        factory = sqlite_uow_factory(tmp_path / "history.db", vault_key)
    _seed(factory)
    return HistoryService(factory)


@pytest.fixture(params=["memory", "sqlite"])
def branch_history(request, tmp_path: Path, vault_key: VaultKey) -> HistoryService:
    if request.param == "memory":
        factory = in_memory_uow_factory(InMemoryStore())
    else:
        factory = sqlite_uow_factory(tmp_path / "branches.db", vault_key)
    with factory() as uow:
        uow.conversations.add(Conversation(id="c1", title="部署讨论", created_at=T0))
        uow.branches.add(Branch(id="b1", conversation_id="c1", created_at=T0))
        uow.branches.add(
            Branch(id="b2", conversation_id="c1", created_at=T0 + timedelta(minutes=5))
        )
        uow.messages.add(
            Message(
                id="m1",
                conversation_id="c1",
                branch_id="b2",
                role=MessageRole.USER,
                content="分支消息",
                created_at=T0 + timedelta(minutes=6),
            )
        )
        uow.commit()
    return HistoryService(factory)


def test_list_conversations_sorted_by_activity(history: HistoryService) -> None:
    summaries = history.list_conversations()
    assert [s.conversation.id for s in summaries] == ["c2", "c1"]  # c2 活动更晚
    assert summaries[1].message_count == 2


def test_search_title(history: HistoryService) -> None:
    hits = history.search("部署")
    assert any(h.matched_field == "title" and h.conversation.id == "c1" for h in hits)


def test_search_user_message(history: HistoryService) -> None:
    hits = history.search("生产环境")
    assert len(hits) == 1
    assert hits[0].matched_field == "user"
    assert hits[0].message is not None and hits[0].message.id == "m1"
    assert "生产环境" in hits[0].snippet


def test_search_assistant_final_answer(history: HistoryService) -> None:
    hits = history.search("流水线")
    assert len(hits) == 1
    assert hits[0].matched_field == "assistant"


def test_search_casefold(history: HistoryService) -> None:
    assert history.search("ci")  # 命中 "CI 流水线"
    assert history.search("CI")


def test_search_empty_query(history: HistoryService) -> None:
    assert history.search("   ") == []
    assert history.search("") == []


def test_time_filter_inclusive(history: HistoryService) -> None:
    day1 = T0 + timedelta(days=1)
    # c2 的用户消息在 T0+2d，被排除；c1 的在 T0，保留
    hits = history.search("环境", end=day1)
    assert all(h.message is None or h.message.created_at <= day1 for h in hits)

    hits = history.search("红烧肉", start=day1)
    assert len(hits) == 1


def test_time_filter_does_not_hide_title_hits(history: HistoryService) -> None:
    """标题命中不受时间筛选——它属于会话本身，不属于某条消息。"""
    far_future = T0 + timedelta(days=365)
    hits = history.search("部署", start=far_future)
    assert any(h.matched_field == "title" for h in hits)


# ---------- 会话管理：重命名、删除（Task 3.1） ----------


def test_rename_updates_title(history: HistoryService) -> None:
    assert history.rename("c1", "生产部署")
    summaries = history.list_conversations()
    titles = {s.conversation.id: s.conversation.title for s in summaries}
    assert titles["c1"] == "生产部署"


def test_rename_rejects_empty(history: HistoryService) -> None:
    assert not history.rename("c1", "   ")
    # 原标题不受影响
    titles = {s.conversation.id: s.conversation.title for s in history.list_conversations()}
    assert titles["c1"] == "部署讨论"


def test_rename_unknown_returns_false(history: HistoryService) -> None:
    assert not history.rename("nope", "x")


def test_branch_default_name_uses_local_time(branch_history: HistoryService) -> None:
    rows = branch_history.list_branches("c1")
    assert rows[0][1] == f"{T0.astimezone():%m-%d %H:%M} 新分支"
    assert rows[1][1] == f"{(T0 + timedelta(minutes=5)).astimezone():%m-%d %H:%M} 新分支"


def test_branch_counts_do_not_decrypt_message_rows(
    tmp_path: Path, vault_key: VaultKey
) -> None:
    base_factory = sqlite_uow_factory(tmp_path / "branch-count.db", vault_key)
    _seed(base_factory)

    def guarded_factory():
        uow = base_factory()

        def fail(_branch_id: str):
            pytest.fail("分支计数不应读取并解密消息正文")

        uow.messages.list_for_branch = fail
        return uow

    rows = HistoryService(guarded_factory).list_branches("c1")
    assert rows == [("b1", f"{T0.astimezone():%m-%d %H:%M} 新分支", 2)]


def test_rename_branch_persists(branch_history: HistoryService) -> None:
    assert branch_history.rename_branch("b2", "发布方案")
    labels = {branch_id: label for branch_id, label, _count in branch_history.list_branches("c1")}
    assert labels["b2"] == "发布方案"


def test_delete_branch_cascades_its_messages(branch_history: HistoryService) -> None:
    assert branch_history.delete_branch("b2")
    assert [row[0] for row in branch_history.list_branches("c1")] == ["b1"]
    assert branch_history.search("分支消息") == []


def test_delete_only_branch_is_rejected(history: HistoryService) -> None:
    assert not history.delete_branch("b1")


def test_delete_cascades_everything(history: HistoryService) -> None:
    """删除会话后：会话、消息都不可见，搜索也搜不到。"""
    assert history.delete("c1")
    assert [s.conversation.id for s in history.list_conversations()] == ["c2"]
    # 级联：消息没了（搜索用户消息无命中）
    assert history.search("生产环境") == []
    # 标题命中也没了
    assert not any(h.conversation.id == "c1" for h in history.search("部署"))


def test_delete_unknown_returns_false(history: HistoryService) -> None:
    assert not history.delete("nope")


def test_delete_is_idempotent(history: HistoryService) -> None:
    assert history.delete("c1")
    assert not history.delete("c1")  # 第二次删除返回 False（已不存在）
