"""SQLite 后端满足仓库端口契约的证明（设计计划 Task 1.1/1.2 + ADR-0002）。

用同一个 RunCoordinator 完整流程，驱动 SQLite 后端，逐项验证 Phase 1B 的不变量：

1. 原子创建（用户消息 + RunRecord + 意图快照）在 commit 前对 backing store 不可见。
2. settled 后一致提交（助手消息 + 传输快照 + 镜像）。
3. 字段级加密：消息正文、运行错误、请求体不以明文落盘。
4. 重新打开连接后数据仍在（持久化，不只是当前会话内存）。
5. Pi entry ID 不被当作应用主键。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.domain.conversation import MessageRole, MessageStatus
from limbowave.domain.run import RunStatus
from limbowave.infrastructure.database.migrations import CURRENT_VERSION, current_version, migrate
from limbowave.infrastructure.database.sqlite_repositories import (
    open_connection,
    sqlite_uow_factory,
)
from tests.unit.test_run_coordinator import FakeKernel

SECRET = "sk-live-NOT-IN-DB-FILE"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "limbowave.db"


def _open(db_path: Path):
    conn = open_connection(db_path)
    migrate(conn)
    return conn


def _context() -> RunContext:
    return RunContext(
        logical_model_id="deepseek-chat",
        endpoint_id="relay-a",
        routing_reason="默认绑定",
        app_params={"model": "deepseek-chat", "max_tokens": 4096},
    )


async def _drive_one_turn_async(
    kernel: FakeKernel, coord: RunCoordinator, user_text: str, reply: str
) -> str:
    run_id = await coord.send(user_text)
    assert run_id is not None
    kernel.observe({"kind": "provider.headers", "headers": {"Authorization": f"Bearer {SECRET}"}})
    kernel.observe(
        {"kind": "provider.request", "payload": {"model": "deepseek-chat", "api_key": SECRET}}
    )
    kernel.say(reply)
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    return run_id


def test_migration_version_bumps(db_path: Path) -> None:
    """迁移创建全部核心表并记录版本。"""
    conn = _open(db_path)
    assert current_version(conn) == CURRENT_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for expected in (
        "conversations",
        "branches",
        "messages",
        "runs",
        "request_intents",
        "transport_snapshots",
        "runtime_mirrors",
    ):
        assert expected in tables


async def test_full_turn_persists_and_encrypts(tmp_path: Path, vault_key, db_path: Path) -> None:
    """完整一轮在 SQLite 上满足 Phase 1B 的全部不变量。"""
    kernel = FakeKernel()
    kernel.entries = [
        {
            "id": "e1",
            "type": "message",
            "parentId": None,
            "message": {"role": "user", "content": "你好"},
        },
        {
            "id": "e2",
            "type": "message",
            "parentId": "e1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ACK"}]},
        },
    ]
    factory = sqlite_uow_factory(db_path, vault_key)
    coord = RunCoordinator(kernel, factory, context=_context)

    run_id = await _drive_one_turn_async(kernel, coord, "你好", "ACK")

    # ---- 通过端口读回：语义完整 ----
    uow = factory()
    run = uow.runs.get(run_id)
    assert run is not None
    assert run.status is RunStatus.COMPLETED

    msgs = uow.messages.list_for_branch(run.branch_id)
    assert [m.role for m in msgs] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert all(m.run_id == run_id for m in msgs)
    assert msgs[0].content == "你好"
    assert msgs[1].content == "ACK"
    assert msgs[1].status is MessageStatus.COMPLETE

    intent = uow.snapshots.get_intent(run_id)
    assert intent is not None
    assert intent.logical_model_id == "deepseek-chat"
    assert intent.endpoint_id == "relay-a"
    assert intent.message_ids[-1] == msgs[0].id

    transports = uow.snapshots.list_transport(run_id)
    assert len(transports) == 1
    # 密钥在落库的传输快照里**只有脱敏视图**
    assert transports[0].headers["Authorization"]["value"] == "[REDACTED]"
    assert transports[0].body["api_key"] == "[REDACTED]"

    mirrors = uow.runtime.list_for_run(run_id)
    assert {m.entry_id for m in mirrors} == {"e1", "e2"}
    assert all(m.id != m.entry_id for m in mirrors)
    uow.close()


async def test_plaintext_content_not_in_db_file(tmp_path: Path, vault_key, db_path: Path) -> None:
    """字段级加密：正文、运行错误、请求体不以明文出现在数据库文件里。"""
    kernel = FakeKernel()
    kernel.send_error = None
    coord = RunCoordinator(kernel, sqlite_uow_factory(db_path, vault_key), context=_context)
    secret_text = "这段话绝不能以明文出现在 .db 文件里"
    await _drive_one_turn_async(kernel, coord, secret_text, "回复也要加密")

    # WAL 模式下新数据可能还在 -wal 里，两个文件一起检查
    raw = db_path.read_bytes()
    wal = db_path.with_suffix(".db-wal")
    if wal.exists():
        raw += wal.read_bytes()
    assert secret_text.encode("utf-8") not in raw
    assert "回复也要加密".encode() not in raw
    assert SECRET.encode("utf-8") not in raw
    assert b"Bearer" not in raw  # 认证头值不落盘
    # schema（表名/列名）是明文的，这没问题
    assert b"transport_snapshots" in raw or b"messages" in raw


async def test_data_survives_reopen(tmp_path: Path, vault_key, db_path: Path) -> None:
    """持久化不只是当前会话内存：重新打开连接后数据仍在。"""
    kernel = FakeKernel()
    coord = RunCoordinator(kernel, sqlite_uow_factory(db_path, vault_key), context=_context)
    await _drive_one_turn_async(kernel, coord, "你好", "ACK")

    # 关闭全部连接后，用同一密钥重新打开
    conn = _open(db_path)
    rows = conn.execute(
        "SELECT id, content_enc, status FROM messages ORDER BY created_at"
    ).fetchall()
    assert len(rows) == 2
    assert vault_key.decrypt(rows[0][1]) == "你好"
    assert vault_key.decrypt(rows[1][1]) == "ACK"
    assert rows[1][2] == "complete"
    conn.close()


async def test_rollback_discards_everything(tmp_path: Path, vault_key, db_path: Path) -> None:
    """未 commit 的写入在 SQLite 上也不可见——事务语义与内存实现一致。"""
    from datetime import UTC, datetime

    from limbowave.domain.conversation import Conversation

    uow = sqlite_uow_factory(db_path, vault_key)()
    uow.conversations.add(Conversation(id="c1", title="x", created_at=datetime.now(UTC)))
    # 事务内可见
    assert uow.conversations.get("c1") is not None
    uow.rollback()
    uow.close()

    # 新连接看不到
    fresh = _open(db_path)
    assert not fresh.execute("SELECT id FROM conversations").fetchall()
    fresh.close()


async def test_interrupted_run_persists(tmp_path: Path, vault_key, db_path: Path) -> None:
    """Runtime 崩溃：本轮落成 interrupted 且持久化。"""
    kernel = FakeKernel()
    coord = RunCoordinator(kernel, sqlite_uow_factory(db_path, vault_key), context=_context)
    await coord.send("第一句")
    await coord.mark_interrupted("Pi 进程异常退出")

    uow = sqlite_uow_factory(db_path, vault_key)()
    run = uow.runs.get(coord.run_id)
    assert run is not None
    assert run.status is RunStatus.INTERRUPTED
    assert "Pi 进程异常退出" in (run.error or "")
    uow.close()


async def test_restart_marks_persisted_running_run_as_interrupted(
    tmp_path: Path, vault_key, db_path: Path
) -> None:
    """上次进程未收尾的 running 记录在下一次启动时必须收敛。"""
    factory = sqlite_uow_factory(db_path, vault_key)
    previous = RunCoordinator(FakeKernel(), factory, context=_context)
    run_id = await previous.send("发送后进程直接退出")
    assert run_id is not None

    restarted = RunCoordinator(FakeKernel(), factory, context=_context)
    await restarted.start()

    with factory() as uow:
        run = uow.runs.get(run_id)
    assert run is not None
    assert run.status is RunStatus.INTERRUPTED
    assert run.finished_at is not None
    assert run.stop_reason == "runtime_restart"
    assert run.error == "应用上次退出时运行未完成"


async def test_duplicate_settled_idempotent(tmp_path: Path, vault_key, db_path: Path) -> None:
    """重复终止事件在 SQLite 上同样幂等。"""
    kernel = FakeKernel()
    coord = RunCoordinator(kernel, sqlite_uow_factory(db_path, vault_key), context=_context)
    run_id = await coord.send("只回一次")
    assert run_id
    kernel.observe({"kind": "provider.request", "payload": {"model": "x"}})
    for _ in range(3):
        kernel.say("只回一次")
        kernel.emit("run.settled", {})
    await coord.wait_idle()

    uow = sqlite_uow_factory(db_path, vault_key)()
    run = uow.runs.get(run_id)
    assert run is not None
    msgs = [
        m for m in uow.messages.list_for_branch(run.branch_id) if m.role is MessageRole.ASSISTANT
    ]
    assert len(msgs) == 1
    assert len(uow.snapshots.list_transport(run_id)) == 1
    uow.close()


async def test_retry_link_survives_finalize_and_reopen(vault_key, db_path: Path) -> None:
    kernel = FakeKernel()
    factory = sqlite_uow_factory(db_path, vault_key)
    coord = RunCoordinator(kernel, factory, context=_context)
    original_id = await coord.send("重试问题")
    kernel.say("旧输出", stop="aborted")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    with factory() as uow:
        original = uow.runs.get(original_id)
    retry_id = await coord.retry_user_message(original.user_message_id)
    with factory() as uow:
        running = uow.runs.list_running()
    assert len(running) == 1
    assert running[0].retry_of_message_id == original.user_message_id
    kernel.say("新输出")
    kernel.emit("run.settled", {})
    await coord.wait_idle()

    with sqlite_uow_factory(db_path, vault_key)() as uow:
        retry = uow.runs.get(retry_id)
        runs = uow.runs.list_for_conversation(original.conversation_id)
        messages = uow.messages.list_for_branch(original.branch_id)
    assert retry.retry_of_message_id == original.user_message_id
    assert retry.status is RunStatus.COMPLETED
    assert [run.retry_of_message_id for run in runs] == [None, original.user_message_id]
    assert [message.content for message in messages] == ["重试问题", "旧输出", "重试问题", "新输出"]


def test_retry_migration_preserves_existing_runs(vault_key, db_path: Path) -> None:
    conn = open_connection(db_path)
    migrate(conn, target=9)
    stamp = "2026-09-29T00:00:00+00:00"
    conn.execute(
        "INSERT INTO conversations (id, title_enc, created_at) VALUES (?, ?, ?)",
        ("c", vault_key.encrypt("旧会话"), stamp),
    )
    conn.execute(
        "INSERT INTO branches (id, conversation_id, created_at) VALUES (?, ?, ?)",
        ("b", "c", stamp),
    )
    conn.execute(
        "INSERT INTO messages "
        "(id, conversation_id, branch_id, role, content_enc, status, created_at, run_id, "
        "tool_steps_enc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("u", "c", "b", "user", vault_key.encrypt("原问题"), "complete", stamp, "r",
         vault_key.encrypt("[]")),
    )
    conn.execute(
        "INSERT INTO runs (id, conversation_id, branch_id, status, created_at, user_message_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("r", "c", "b", "failed", stamp, "u"),
    )
    conn.commit()
    assert migrate(conn) == CURRENT_VERSION
    conn.close()
    with sqlite_uow_factory(db_path, vault_key)() as uow:
        run = uow.runs.get("r")
        message = uow.messages.get("u")
    assert run.retry_of_message_id is None
    assert run.status is RunStatus.FAILED
    assert message.content == "原问题"
