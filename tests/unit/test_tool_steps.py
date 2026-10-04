"""工具步骤验收（Phase 9 / 设计计划 §三.2）。

锁住的不变量：
- 按 toolCallId 配对 start/end，得到状态、结果摘要、耗时、错误；
- 迟到/孤立的结束事件也留审计（不丢记录）；
- **结果摘要不内联大段内容**（只给可读短描述）；
- 工具步骤随助手消息落库（加密），SQLite 与内存后端一致；
- 隐藏只影响展示——数据始终在消息里。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from limbowave.domain.conversation import (
    Branch,
    Conversation,
    Message,
    MessageRole,
    MessageStatus,
)
from limbowave.domain.tool_step import ToolStatus, ToolStep, finish
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory

T0 = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)


# ---------- 领域规则 ----------


def test_finish_marks_ok_with_summary() -> None:
    step = ToolStep(tool_call_id="c1", name="read", args={"path": "a.txt"})
    done = finish(step, result={"output": "文件内容"}, duration_ms=42)
    assert done.status is ToolStatus.OK
    assert done.duration_ms == 42
    assert done.summary == "文件内容"


def test_finish_marks_error_with_message() -> None:
    step = ToolStep(tool_call_id="c1", name="bash")
    done = finish(step, result={"error": "命令失败"}, is_error=True)
    assert done.status is ToolStatus.ERROR
    assert done.error == "命令失败"
    assert done.summary == "命令失败"


def test_running_step_summary_lists_args() -> None:
    """未结束的步骤：折叠行显示参数要点（不是空白）。"""
    step = ToolStep(tool_call_id="c1", name="read", args={"path": "notes.md"})
    assert step.status is ToolStatus.RUNNING
    assert "path=notes.md" in step.summary


def test_summary_truncates_large_result() -> None:
    """结果摘要**不内联大段内容**——避免把整份文件摊在折叠行上。"""
    step = ToolStep(tool_call_id="c1", name="read")
    done = finish(step, result={"output": "x" * 5000}, duration_ms=1)
    assert len(done.result_summary) <= 201  # 200 + 省略号
    assert done.result_summary.endswith("…")


def test_summary_collapses_whitespace() -> None:
    step = ToolStep(tool_call_id="c1", name="read")
    done = finish(step, result={"output": "line1\n\nline2\t\tline3"}, duration_ms=1)
    assert "\n" not in done.result_summary
    assert done.result_summary == "line1 line2 line3"


def test_result_list_reports_count() -> None:
    step = ToolStep(tool_call_id="c1", name="list")
    done = finish(step, result=[{"a": 1}, {"b": 2}], duration_ms=1)
    assert "2 项" in done.result_summary


def test_json_round_trip() -> None:
    step = ToolStep(
        tool_call_id="c9",
        name="edit",
        status=ToolStatus.ERROR,
        created_at_ms=120,
        duration_ms=30,
        args={"path": "x"},
        result_summary="",
        error="失败了",
    )
    restored = ToolStep.from_json(step.to_json())
    assert restored == step


# ---------- 随消息落库（两个后端） ----------


@pytest.fixture(params=["memory", "sqlite"])
def factory(request, tmp_path: Path, vault_key: VaultKey):
    if request.param == "memory":
        return in_memory_uow_factory(InMemoryStore())
    return sqlite_uow_factory(tmp_path / "ts.db", vault_key)


def _seed(factory, steps: tuple[ToolStep, ...]) -> str:
    with factory() as uow:
        uow.conversations.add(Conversation(id="c1", title="t", created_at=T0))
        uow.branches.add(Branch(id="b1", conversation_id="c1", created_at=T0))
        uow.messages.add(
            Message(
                id="m1",
                conversation_id="c1",
                branch_id="b1",
                role=MessageRole.ASSISTANT,
                content="结果如下",
                created_at=T0,
                status=MessageStatus.COMPLETE,
                tool_steps=steps,
            )
        )
        uow.commit()
    return "m1"


def test_tool_steps_persist(factory) -> None:
    steps = (
        ToolStep(
            tool_call_id="c1",
            name="read",
            status=ToolStatus.OK,
            duration_ms=12,
            args={"path": "a"},
            result_summary="内容",
        ),
        ToolStep(
            tool_call_id="c2",
            name="bash",
            status=ToolStatus.ERROR,
            duration_ms=30,
            error="非零退出",
        ),
    )
    message_id = _seed(factory, steps)
    with factory() as uow:
        loaded = uow.messages.get(message_id)
    assert loaded is not None
    assert len(loaded.tool_steps) == 2
    assert loaded.tool_steps[0].name == "read"
    assert loaded.tool_steps[0].result_summary == "内容"
    assert loaded.tool_steps[1].status is ToolStatus.ERROR
    assert loaded.tool_steps[1].error == "非零退出"


def test_message_without_tool_steps(factory) -> None:
    message_id = _seed(factory, ())
    with factory() as uow:
        loaded = uow.messages.get(message_id)
    assert loaded is not None
    assert loaded.tool_steps == ()


def test_update_replaces_tool_steps(factory) -> None:
    """更新消息时工具步骤一起更新（追加步骤的场景）。"""
    message_id = _seed(factory, ())
    with factory() as uow:
        message = uow.messages.get(message_id)
        assert message is not None
        from dataclasses import replace

        uow.messages.update(
            replace(
                message,
                tool_steps=(
                    ToolStep(tool_call_id="c1", name="read", status=ToolStatus.OK, duration_ms=5),
                ),
            )
        )
        uow.commit()
    with factory() as uow:
        loaded = uow.messages.get(message_id)
    assert loaded is not None
    assert len(loaded.tool_steps) == 1
    assert loaded.tool_steps[0].duration_ms == 5


def test_tool_steps_encrypted_at_rest(tmp_path: Path, vault_key: VaultKey) -> None:
    """工具步骤含参数与结果，可能带敏感内容 —— 落盘必须是密文。"""
    factory = sqlite_uow_factory(tmp_path / "enc.db", vault_key)
    secret = "SECRET-INSIDE-TOOL-ARGS"
    _seed(
        factory,
        (ToolStep(tool_call_id="c1", name="bash", args={"cmd": secret}),),
    )
    raw = (tmp_path / "enc.db").read_bytes()
    wal = tmp_path / "enc.db-wal"
    if wal.exists():
        raw += wal.read_bytes()
    assert secret.encode("utf-8") not in raw
