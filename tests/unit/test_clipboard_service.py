"""剪贴板临时文档验收（Task 4.3 / 设计计划 §八.3）。

锁住的不变量：
- 保存即加密：磁盘 blob 里没有明文；
- 稳定 ID + 行号，读取走同一个 FileService.read；
- 重命名/删除检查；
- 删除前引用检查：被意图快照引用时拒绝；
- 删除后 blob 从加密仓清掉。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from limbowave.application.services.clipboard_service import ClipboardService, DocumentInUse
from limbowave.application.services.file_service import FileService
from limbowave.domain.conversation import Branch, Conversation
from limbowave.domain.files import FileKind
from limbowave.domain.run import RunRecord, RunStatus
from limbowave.domain.snapshots import RequestIntentSnapshot
from limbowave.infrastructure.crypto.blob_store import BlobStore
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory

T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def blob_store(tmp_path: Path, vault_key: VaultKey) -> BlobStore:
    return BlobStore(tmp_path / "blobs", vault_key)


@pytest.fixture
def clipboard(store: InMemoryStore, blob_store: BlobStore) -> ClipboardService:
    return ClipboardService(in_memory_uow_factory(store), blob_store)


def test_save_encrypts_content(clipboard: ClipboardService, tmp_path: Path) -> None:
    """保存即加密：blob 文件里不出现明文。"""
    secret_text = "这段剪贴板内容绝不能明文落盘"
    document = clipboard.save(secret_text, display_name="笔记")

    assert document.kind is FileKind.CLIPBOARD
    assert document.blob_id is not None
    blob_file = tmp_path / "blobs" / f"{document.blob_id}.bin"
    assert blob_file.is_file()
    assert secret_text.encode("utf-8") not in blob_file.read_bytes()


def test_save_assigns_stable_id_and_line_numbers(clipboard: ClipboardService) -> None:
    document = clipboard.save("第一行\n第二行\n第三行")
    assert document.id.startswith("clip_")
    assert document.line_count == 3


def test_read_via_same_file_service(
    clipboard: ClipboardService, store: InMemoryStore, blob_store: BlobStore
) -> None:
    """剪贴板文档用**同一个**读取工具，行号语义一致。"""
    document = clipboard.save("第一行\n第二行\n第三行")
    reader = FileService(in_memory_uow_factory(store), blob_store=blob_store)
    result = reader.read(document.id, 2, 2)
    assert result.text == "第二行"
    assert result.actual_range == (2, 2)


def test_save_rejects_empty(clipboard: ClipboardService) -> None:
    with pytest.raises(ValueError, match="为空"):
        clipboard.save("   ")


def test_rename(clipboard: ClipboardService) -> None:
    document = clipboard.save("内容", display_name="旧名")
    assert clipboard.rename(document.id, "新名")
    assert clipboard.list_clipboards()[0].display_name == "新名"


def test_rename_rejects_empty_and_unknown(clipboard: ClipboardService) -> None:
    document = clipboard.save("内容")
    assert not clipboard.rename(document.id, "   ")
    assert not clipboard.rename("nope", "x")


def test_delete_removes_blob(clipboard: ClipboardService, blob_store: BlobStore) -> None:
    document = clipboard.save("要删掉的内容")
    blob_id = document.blob_id
    assert blob_id is not None and blob_store.exists(blob_id)

    assert clipboard.delete(document.id)
    assert clipboard.list_clipboards() == []
    assert not blob_store.exists(blob_id)  # blob 也清了


def test_delete_rejects_when_referenced(clipboard: ClipboardService, store: InMemoryStore) -> None:
    """删除前检查引用：被意图快照引用的文档拒绝删除。"""
    document = clipboard.save("被引用的内容")

    # 造一条引用它的意图快照
    with in_memory_uow_factory(store)() as uow:
        uow.conversations.add(Conversation(id="c1", title="t", created_at=T0))
        uow.branches.add(Branch(id="b1", conversation_id="c1", created_at=T0))
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
                logical_model_id="m",
                endpoint_id="e",
                routing_reason="r",
                created_at=T0,
                attachment_ids=(document.id,),
            )
        )
        uow.commit()

    with pytest.raises(DocumentInUse, match="引用"):
        clipboard.delete(document.id)
    # 仍在
    assert len(clipboard.list_clipboards()) == 1


def test_delete_unknown_returns_false(clipboard: ClipboardService) -> None:
    assert not clipboard.delete("nope")


def test_blob_dedup_same_content(clipboard: ClipboardService, blob_store: BlobStore) -> None:
    """同内容两次保存：blob 内容寻址，磁盘只有一个 blob。"""
    d1 = clipboard.save("相同内容")
    d2 = clipboard.save("相同内容")
    assert d1.blob_id == d2.blob_id
    assert d1.id != d2.id  # 但登记卡是两份（各自可重命名/删除）
