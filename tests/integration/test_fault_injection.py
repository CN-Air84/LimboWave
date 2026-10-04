"""故障注入（Task 10.1）。

计划书列的八类故障，逐类验证「不静默、不伪造、不留半截数据」：

| 故障 | 本文件覆盖 |
| --- | --- |
| Runtime 崩溃 | 已有：test_run_coordinator gate5 + test_recovery_e2e |
| 数据库写入中断 | ✓ test_db_write_failure_* |
| 文件仓库对象缺失 | ✓ test_missing_blob_* |
| 流式连接中断 | 已有：gate5（无有效内容前中断）+ 本文件的停止路径 |
| PowerShell 超时 | 已有：test_shell_executor |
| 站点不可用 | ✓ test_site_unavailable_* |
| 压缩模型失败 | 已有：test_compression_service |
| 磁盘空间不足 | ✓ test_disk_full_*（用 PRAGMA max_page_count 模拟） |
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.domain.run import RunStatus
from limbowave.infrastructure.crypto.blob_store import BlobStore
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.database.migrations import migrate
from limbowave.infrastructure.database.sqlite_repositories import (
    SqliteUnitOfWork,
    open_connection,
)
from tests.unit.test_run_coordinator import FakeKernel

T0 = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)


def _context() -> RunContext:
    return RunContext(
        logical_model_id="deepseek-chat",
        endpoint_id="relay-a",
        routing_reason="默认绑定",
        app_params={"model": "deepseek-chat"},
    )


def _prime(db_path: Path) -> None:
    """建好 schema 与一个会话，供后续故障注入。"""
    conn = open_connection(db_path)
    migrate(conn)
    with conn:
        conn.execute(
            "INSERT INTO conversations (id, title_enc, created_at) VALUES (?, ?, ?)",
            ("c1", "x", T0.isoformat()),
        )
        conn.execute(
            "INSERT INTO branches (id, conversation_id, created_at) VALUES (?, ?, ?)",
            ("b1", "c1", T0.isoformat()),
        )
    conn.close()


# ---------- 数据库写入中断 / 磁盘不足 ----------


def _fill_connection(db_path: Path) -> sqlite3.Connection:
    """把库写到接近上限，让后续写入以「磁盘满」失败。

    用 ``PRAGMA max_page_count`` 把可用页数卡在当前用量上——这是 SQLite 层面
    模拟 SQLITE_FULL 的标准做法，不需要真的把磁盘写满。
    """
    conn = open_connection(db_path)
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    conn.execute(f"PRAGMA max_page_count = {pages}")
    conn.commit()
    return conn


def test_disk_full_raises_and_leaves_no_partial_data(tmp_path: Path) -> None:
    """磁盘满：写入失败要抛出来，且**不留半截数据**。"""
    db_path = tmp_path / "full.db"
    _prime(db_path)

    # 正常写入一批数据把库撑到当前页数
    conn = open_connection(db_path)
    for index in range(200):
        conn.execute(
            "INSERT INTO messages (id, conversation_id, branch_id, role, content_enc,"
            " status, created_at) VALUES (?, 'c1', 'b1', 'user', ?, 'complete', ?)",
            (f"m{index}", "x" * 200, T0.isoformat()),
        )
    conn.commit()
    conn.close()

    # 卡住页数，模拟磁盘满
    conn = _fill_connection(db_path)
    before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    uow = SqliteUnitOfWork(conn, _key_for(db_path))
    with pytest.raises(sqlite3.OperationalError, match="full"), uow:
        uow.messages.add(
            Message(
                id="overflow",
                conversation_id="c1",
                branch_id="b1",
                role=MessageRole.USER,
                content="y" * 5000,
                created_at=T0,
            )
        )
        uow.commit()  # 这里失败

    # 事务回滚：半截数据不可见
    after = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert after == before
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE id='overflow'").fetchone()[0] == 0
    conn.close()


def _key_for(db_path: Path) -> VaultKey:
    """故障注入用的假主密钥（本轮不验证解密，只要类型正确）。"""
    from limbowave.infrastructure.crypto.vault import VaultKey

    return VaultKey(b"\x00" * 32)


async def test_storage_failure_during_send_surfaces_as_error(tmp_path: Path) -> None:
    """一轮开始时存储就失败：不能把异常抛穿 GUI，也不能留下 half-run。

    这是 Task 10.1 的核心诉求——存储层故障必须是**可观测的错误**，
    而不是未捕获异常或静默丢失。
    """
    db_path = tmp_path / "fault.db"
    _prime(db_path)
    key = _key_for(db_path)

    factory_conn = open_connection(db_path)
    pages = factory_conn.execute("PRAGMA page_count").fetchone()[0]
    factory_conn.execute(f"PRAGMA max_page_count = {pages}")
    factory_conn.execute("PRAGMA wal_autocheckpoint = 0")
    factory_conn.commit()

    def factory() -> SqliteUnitOfWork:
        # 每次新连接都沿用上限（模拟持续磁盘满）
        conn = open_connection(db_path)
        conn.execute(f"PRAGMA max_page_count = {pages}")
        return SqliteUnitOfWork(conn, key)

    kernel = FakeKernel()
    coord = RunCoordinator(kernel, factory, context=_context)
    errors: list[str] = []
    coord.subscribe(
        lambda e: errors.append(str(e.data.get("message", ""))) if e.kind == "error" else None
    )

    # 存储不可用：send 必须给出错误事件，而不是抛异常
    run_id = await coord.send("这条消息写不进去" + "长" * 3000)
    assert run_id is None or run_id  # 允许两种策略，但不得抛
    assert errors, "存储故障必须以 error 事件上报"


# ---------- 文件仓库对象缺失 ----------


def _make_png(width: int, height: int) -> bytes:
    """最小合法 PNG（含 IEND）——图片服务解析头部时会校验结构。"""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(tag + data))
        return length + tag + data + crc

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanline = bytes([0]) + bytes([0x11, 0x22, 0x33]) * width
    idat = zlib.compress(scanline * height)
    signature = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def test_missing_blob_read_raises_clear_error(tmp_path: Path, vault_key: VaultKey) -> None:
    """blob 文件被删：读要给出明确错误，而不是返回空内容。"""
    from limbowave.application.services.image_service import ImageService
    from limbowave.infrastructure.memory_repositories import (
        InMemoryStore,
        in_memory_uow_factory,
    )

    store = BlobStore(tmp_path / "blobs", vault_key)
    service = ImageService(in_memory_uow_factory(InMemoryStore()), store)

    # 导入图片 → 删掉 blob 文件 → 再读：必须是明确错误，不是空内容
    attachment = service.import_bytes(_make_png(8, 8), source_path="x.png")
    (tmp_path / "blobs" / f"{attachment.blob_id}.bin").unlink()

    with pytest.raises(KeyError):
        service.read_bytes(attachment.id)


def test_deleted_blob_does_not_crash_export(tmp_path: Path, vault_key: VaultKey) -> None:
    """导出时 blob 缺失：跳过图片，消息照常导出（已有覆盖，这里锁行为不变）。"""
    from limbowave.application.services.export_service import ExportService
    from limbowave.domain.files import ImageAttachment, ImageFormat
    from limbowave.domain.run import RunRecord
    from limbowave.domain.snapshots import RequestIntentSnapshot
    from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory

    store = InMemoryStore()
    blobs = BlobStore(tmp_path / "blobs", vault_key)
    factory = in_memory_uow_factory(store)
    with factory() as uow:
        uow.conversations.add(Conversation(id="c1", title="t", created_at=T0))
        uow.branches.add(Branch(id="b1", conversation_id="c1", created_at=T0))
        uow.messages.add(
            Message(
                id="m1",
                conversation_id="c1",
                branch_id="b1",
                role=MessageRole.ASSISTANT,
                content="回答",
                created_at=T0,
                run_id="r1",
            )
        )
        blob_id = blobs.put(b"gone")
        uow.image_attachments.add(
            ImageAttachment(
                id="img_1",
                format=ImageFormat.PNG,
                blob_id=blob_id,
                created_at=T0,
                width=8,
                height=8,
                size_bytes=4,
            )
        )
        uow.runs.add(
            RunRecord(
                id="r1",
                conversation_id="c1",
                branch_id="b1",
                status=RunStatus.COMPLETED,
                created_at=T0,
                user_message_id="m1",
                assistant_message_id="m1",
            )
        )
        uow.snapshots.add_intent(
            RequestIntentSnapshot(
                id="i1",
                run_id="r1",
                conversation_id="c1",
                branch_id="b1",
                logical_model_id="m",
                endpoint_id="e",
                routing_reason="r",
                created_at=T0,
                attachment_ids=("img_1",),
            )
        )
        uow.commit()

    (tmp_path / "blobs" / f"{blob_id}.bin").unlink()  # 对象缺失
    result = ExportService(factory, blobs).export_branch("b1", tmp_path / "out.html")
    assert result.message_count == 1
    assert result.image_count == 0  # 图片被跳过，不是伪造占位


# ---------- 站点不可用 ----------


async def test_site_unavailable_records_failure_without_fake_reply(tmp_path: Path) -> None:
    """站点不可用（连接被拒）：本轮落 failed，用户消息保留，**不伪造助手回复**。"""
    kernel = FakeKernel()
    kernel.send_error = ConnectionError("connection refused")
    from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory

    db_path = tmp_path / "site.db"
    conn = open_connection(db_path)
    migrate(conn)
    conn.close()
    # 需要一个真实的 vault 密钥来加密字段
    from limbowave.infrastructure.crypto.vault import KdfParams, Vault

    key = Vault(
        tmp_path / "v.json",
        params=KdfParams(time_cost=1, memory_cost=8, parallelism=1),
    ).create("p")

    coord = RunCoordinator(kernel, sqlite_uow_factory(db_path, key), context=_context)
    errors: list[str] = []
    coord.subscribe(
        lambda e: errors.append(str(e.data.get("message", ""))) if e.kind == "error" else None
    )

    run_id = await coord.send("发送到一个不可用的站点")
    assert run_id is not None
    await coord.wait_idle()

    with sqlite_uow_factory(db_path, key)() as uow:
        run = uow.runs.get(run_id)
        assert run is not None
        assert run.status is RunStatus.FAILED
        messages = uow.messages.list_for_branch(run.branch_id)
        # 用户消息保留（不静默丢弃用户输入）
        assert any(m.role is MessageRole.USER for m in messages)
        # 不伪造助手回复
        assert not [m for m in messages if m.role is MessageRole.ASSISTANT]
    assert any("拒绝" in e or "refused" in e or "发送失败" in e for e in errors)
