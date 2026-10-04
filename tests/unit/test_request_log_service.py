"""请求日志查询验收（Task 3.3）。

锁住的不变量：
- 一轮 run 聚合意图快照 + 按 sequence 排序的传输快照；
- 分支过滤正确（分支专属请求记录）；
- 日志里出现的是**脱敏后**的传输快照——服务层只读快照，密钥无从谈起；
- 缺失意图快照（异常中断的 run）如实呈现为 None，不伪造。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from limbowave.application.services.request_log_service import RequestLogService
from limbowave.domain.conversation import Branch, Conversation
from limbowave.domain.run import RunRecord, RunStatus
from limbowave.domain.snapshots import RequestIntentSnapshot, TransportSnapshot
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory

T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def service() -> RequestLogService:
    store = InMemoryStore()
    with in_memory_uow_factory(store)() as uow:
        uow.conversations.add(Conversation(id="c1", title="t", created_at=T0))
        uow.branches.add(Branch(id="b1", conversation_id="c1", created_at=T0))
        uow.branches.add(Branch(id="b2", conversation_id="c1", created_at=T0))

        uow.runs.add(
            RunRecord(
                id="r1",
                conversation_id="c1",
                branch_id="b1",
                status=RunStatus.COMPLETED,
                created_at=T0,
                user_message_id="m1",
            )
        )
        uow.snapshots.add_intent(
            RequestIntentSnapshot(
                id="i1",
                run_id="r1",
                conversation_id="c1",
                branch_id="b1",
                logical_model_id="deepseek-chat",
                endpoint_id="relay-a",
                routing_reason="默认绑定",
                created_at=T0,
                app_params={"model": "deepseek-chat", "max_tokens": 4096},
            )
        )
        # 两条传输快照（如工具往返），乱序写入，读取必须按 sequence 排好
        uow.snapshots.add_transport(
            TransportSnapshot(
                id="t2",
                run_id="r1",
                created_at=T0,
                sequence=2,
                body={"model": "deepseek-chat"},
                headers={
                    "Authorization": {"present": True, "scheme": "Bearer", "value": "[REDACTED]"}
                },
                response_status=200,
            )
        )
        uow.snapshots.add_transport(
            TransportSnapshot(
                id="t1",
                run_id="r1",
                created_at=T0,
                sequence=1,
                body={"model": "deepseek-chat", "messages": []},
                headers={
                    "Authorization": {"present": True, "scheme": "Bearer", "value": "[REDACTED]"}
                },
            )
        )

        # b2 上的 run：中断，没有意图快照
        uow.runs.add(
            RunRecord(
                id="r2",
                conversation_id="c1",
                branch_id="b2",
                status=RunStatus.INTERRUPTED,
                created_at=T0 + timedelta(hours=1),
                user_message_id="m9",
                error="Runtime 异常退出",
            )
        )
        uow.commit()
    return RequestLogService(in_memory_uow_factory(store))


def test_assembles_intent_and_ordered_transports(service: RequestLogService) -> None:
    entry = service.get_for_run("r1")
    assert entry is not None
    assert entry.intent is not None
    assert entry.intent.logical_model_id == "deepseek-chat"
    assert [t.id for t in entry.transports] == ["t1", "t2"]  # 乱序写入，按 sequence 读出


def test_list_for_conversation_chronological(service: RequestLogService) -> None:
    entries = service.list_for_conversation("c1")
    assert [e.run.id for e in entries] == ["r1", "r2"]


def test_list_for_branch_filters(service: RequestLogService) -> None:
    entries = service.list_for_branch("c1", "b2")
    assert [e.run.id for e in entries] == ["r2"]
    assert entries[0].run.error == "Runtime 异常退出"


def test_interrupted_run_shows_missing_intent(service: RequestLogService) -> None:
    """中断的 run 没有意图快照——如实为 None，不伪造。"""
    entry = service.get_for_run("r2")
    assert entry is not None
    assert entry.intent is None
    assert entry.transports == []
    assert entry.run.status is RunStatus.INTERRUPTED


def test_transports_are_redacted_views(service: RequestLogService) -> None:
    """快照里只有脱敏视图：认证头是结构化占位，不是密钥本体。"""
    entry = service.get_for_run("r1")
    assert entry is not None
    auth = entry.transports[0].headers["Authorization"]
    assert auth == {"present": True, "scheme": "Bearer", "value": "[REDACTED]"}


def test_unknown_run_returns_none(service: RequestLogService) -> None:
    assert service.get_for_run("nope") is None
