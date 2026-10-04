"""剪贴板临时文档服务（Task 4.3 / 设计计划 §八.3）。

规则逐条落地：

- 长剪贴板文本保存为应用托管的临时文档：内容进**加密 blob 仓**，
  登记卡（FileDocument, kind=clipboard）进数据库。
- 分配稳定 ID 和行号——行号由 FileService 的行偏移机制保证，
  读取走**同一个** :meth:`FileService.read`（blob 路径）。
- 用户可重命名（更新登记卡的 display_name）或删除。
- **删除前检查是否仍被会话引用**：请求意图快照的 ``attachment_ids``
  引用了该文档时拒绝删除。
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.application.services.file_service import _compute_line_offsets
from limbowave.domain.files import FileDocument, FileKind
from limbowave.infrastructure.crypto.blob_store import BlobStore


class DocumentInUse(Exception):
    """文档仍被会话引用，拒绝删除。"""


class ClipboardService:
    """剪贴板临时文档的创建、重命名、删除。读取复用 FileService。"""

    def __init__(self, uow_factory: UnitOfWorkFactory, blob_store: BlobStore) -> None:
        self._uow_factory = uow_factory
        self._blob_store = blob_store

    def save(self, text: str, *, display_name: str | None = None) -> FileDocument:
        """把一段剪贴板文本保存为加密临时文档。空文本拒绝。"""
        if not text.strip():
            raise ValueError("剪贴板内容为空")
        raw = text.encode("utf-8")
        blob_id = self._blob_store.put(raw)
        document = FileDocument(
            id=f"clip_{uuid4().hex[:16]}",
            kind=FileKind.CLIPBOARD,
            display_name=display_name or f"剪贴板 {datetime.now(UTC):%m-%d %H:%M}",
            created_at=datetime.now(UTC),
            blob_id=blob_id,
            encoding="utf-8",
            line_count=len(_compute_line_offsets(raw)),
            size_bytes=len(raw),
            content_hash=hashlib.sha256(raw).hexdigest(),
        )
        with self._uow_factory() as uow:
            uow.file_documents.add(document)
            uow.commit()
        return document

    def rename(self, document_id: str, new_name: str) -> bool:
        """重命名。空名拒绝，不存在返回 False。"""
        new_name = new_name.strip()
        if not new_name:
            return False
        with self._uow_factory() as uow:
            document = uow.file_documents.get(document_id)
            if document is None:
                return False
            uow.file_documents.update(replace(document, display_name=new_name))
            uow.commit()
            return True

    def delete(self, document_id: str) -> bool:
        """删除文档。**删除前检查是否仍被会话引用**：被引用则抛 DocumentInUse。

        引用来源：请求意图快照的 ``attachment_ids``（文档随消息发送时记录）。
        blob 一并从加密仓清掉。
        """
        with self._uow_factory() as uow:
            document = uow.file_documents.get(document_id)
            if document is None:
                return False
            referencing = [
                intent.run_id
                for intent in uow.snapshots.list_all_intents()
                if document_id in intent.attachment_ids
            ]
            if referencing:
                raise DocumentInUse(f"文档仍被 {len(referencing)} 次请求引用，不能删除")
            uow.file_documents.delete(document_id)
            uow.commit()
        if document.blob_id is not None:
            self._blob_store.delete(document.blob_id)
        return True

    def list_clipboards(self) -> list[FileDocument]:
        """全部剪贴板临时文档。"""
        with self._uow_factory() as uow:
            return [d for d in uow.file_documents.list_all() if d.kind is FileKind.CLIPBOARD]
