"""文件索引与精确读取服务（Task 4.1 / 4.2）。

规则（设计计划 §八.2，逐条编码，不让实现漂移）：

1. 行号从 **1** 开始。
2. 默认单次最多读取 200 行；全局可改默认上限；会话可覆盖上限。
3. 超过上限时**拒绝调用**——返回错误，说明申请范围、允许上限和建议分段。
4. **不擅自截取**前 200 行，**不自动拆**为多次调用。
5. 每次读取记录文件指纹（路径 + mtime + 内容哈希）、申请范围和实际范围。
6. 文件发生变化后，旧记录仍指向当时读取版本或明确标记版本不一致。
7. 空文件、超长单行、中文与混合换行符都是一等公民（见测试矩阵）。

稳定行号的实现：索引时按 **字节偏移** 记录每行起点（``line_offsets``），
读取时按偏移切片原始字节再按登记编码解码——行号与内容一一对应，
不受后续格式化影响。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.domain.files import (
    FileDocument,
    FileKind,
    UnsupportedFileType,
    classify_path,
    detect_encoding,
)

DEFAULT_MAX_LINES = 200
# 超长单行的保护上限：一行超过这个字节数时，读取该行会截断并如实标记
MAX_LINE_BYTES = 256 * 1024


class FileReadError(Exception):
    """读取失败的基类。"""


class RangeLimitExceeded(FileReadError):
    """申请范围超过上限。携带结构化信息供工具返回模型。"""

    def __init__(
        self,
        requested: tuple[int, int],
        limit: int,
        total_lines: int,
    ) -> None:
        self.requested = requested
        self.limit = limit
        self.total_lines = total_lines
        span = requested[1] - requested[0] + 1
        # 建议分段：按上限切成若干段的起点
        segments = []
        start = requested[0]
        while start <= requested[1]:
            end = min(start + limit - 1, requested[1])
            segments.append((start, end))
            start = end + 1
        self.suggested_segments = segments
        super().__init__(
            f"申请范围 {requested[0]}-{requested[1]}（{span} 行）超过单次上限 {limit} 行。"
            f"建议分段：{', '.join(f'{a}-{b}' for a, b in segments)}"
        )


class InvalidRange(FileReadError):
    """范围本身不合法（start < 1、end < start）。"""


class FileChanged(FileReadError):
    """读取时检测到文件与索引时不一致（内容哈希不同）。"""


@dataclass(frozen=True, slots=True)
class ReadResult:
    """一次精确读取的结果（设计计划 §八.2 的返回结构）。"""

    file_id: str
    path: str
    requested_range: tuple[int, int]
    actual_range: tuple[int, int]
    total_lines: int
    content_hash: str
    encoding: str
    text: str
    truncated_lines: tuple[int, ...] = ()  # 被截断的超长行号
    changed_since_index: bool = False  # 文件自索引以来是否变化


class FileService:
    """文件索引 + 精确读取。磁盘文件直接从 path 读；剪贴板文档走 blob 仓。"""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        blob_store: object | None = None,  # BlobStore，剪贴板文档用；None 则不支持剪贴板
        default_max_lines: int = DEFAULT_MAX_LINES,
    ) -> None:
        self._uow_factory = uow_factory
        self._blob_store = blob_store
        self._default_max_lines = default_max_lines

    # ---------- 索引（Task 4.1） ----------

    def index_path(self, path: Path) -> FileDocument:
        """索引一个磁盘文件。不支持的类型抛 UnsupportedFileType（含 DOCX）。

        重复索引同一路径：内容没变则返回既有登记卡，变了则**更新**登记卡
        （行号与哈希跟着新版本走——旧读取记录仍带旧哈希，可判定版本不一致）。
        """
        kind = classify_path(path)
        if not isinstance(kind, FileKind):
            raise UnsupportedFileType(f"不是文本文件：{path.name}")
        raw = path.read_bytes()
        stat = path.stat()
        encoding, _text = detect_encoding(raw)
        line_offsets = _compute_line_offsets(raw)
        content_hash = hashlib.sha256(raw).hexdigest()

        with self._uow_factory() as uow:
            # 同路径复用登记卡（稳定 id）
            existing = next(
                (d for d in uow.file_documents.list_all() if d.path == str(path)),
                None,
            )
            document = FileDocument(
                id=existing.id if existing else f"file_{uuid4().hex[:16]}",
                kind=kind,
                display_name=path.name,
                path=str(path),
                encoding=encoding,
                line_count=len(line_offsets),
                size_bytes=len(raw),
                content_hash=content_hash,
                mtime_ns=stat.st_mtime_ns,
                created_at=existing.created_at if existing else datetime.now(UTC),
                line_offsets=line_offsets,
            )
            if existing:
                uow.file_documents.update(document)
            else:
                uow.file_documents.add(document)
            uow.commit()
            return document

    def get(self, document_id: str) -> FileDocument | None:
        with self._uow_factory() as uow:
            return uow.file_documents.get(document_id)

    def list_all(self) -> list[FileDocument]:
        with self._uow_factory() as uow:
            return uow.file_documents.list_all()

    # ---------- 精确读取（Task 4.2） ----------

    def read(
        self,
        file_id: str,
        start_line: int = 1,
        end_line: int | None = None,
        *,
        session_max_lines: int | None = None,
    ) -> ReadResult:
        """按行范围精确读取。

        - 行号从 1 开始，[start_line, end_line] 闭区间；
        - end_line 缺省 = start_line + 上限 - 1；
        - 超过上限抛 :class:`RangeLimitExceeded`（含建议分段）——**不擅自截取**；
        - 文件被修改时先更新索引再读，并标记 changed_since_index。
        """
        with self._uow_factory() as uow:
            document = uow.file_documents.get(file_id)
        if document is None:
            raise FileReadError(f"文档不存在：{file_id}")

        limit = session_max_lines or self._default_max_lines
        end = end_line if end_line is not None else start_line + limit - 1
        if start_line < 1 or end < start_line:
            raise InvalidRange(f"非法范围：{start_line}-{end}")
        if end - start_line + 1 > limit:
            raise RangeLimitExceeded((start_line, end), limit, document.line_count)

        raw = self._read_raw(document)
        # 变化检测：内容哈希与索引时不一致 → 先重建索引（行号跟着新内容走）
        current_hash = hashlib.sha256(raw).hexdigest()
        changed = current_hash != document.content_hash
        if changed:
            document = self._reindex(document, raw)

        return self._slice(document, raw, start_line, end, changed)

    # ---------- 内部 ----------

    def _read_raw(self, document: FileDocument) -> bytes:
        """读原始字节。磁盘文件走 path；剪贴板文档走 blob 仓。"""
        if document.blob_id is not None:
            if self._blob_store is None:
                raise FileReadError("剪贴板文档需要 blob 仓，当前未配置")
            from limbowave.infrastructure.crypto.blob_store import BlobStore

            assert isinstance(self._blob_store, BlobStore)
            return self._blob_store.get(document.blob_id)
        if document.path is None:
            raise FileReadError(f"文档 {document.id} 既没有路径也没有 blob")
        return Path(document.path).read_bytes()

    def _reindex(self, document: FileDocument, raw: bytes) -> FileDocument:
        """内容变化后重建登记卡（新行偏移 + 新哈希），保留稳定 id。"""
        stat = Path(document.path).stat() if document.path else None
        rebuilt = FileDocument(
            id=document.id,
            kind=document.kind,
            display_name=document.display_name,
            path=document.path,
            blob_id=document.blob_id,
            encoding=document.encoding,
            line_count=len(_compute_line_offsets(raw)),
            size_bytes=len(raw),
            content_hash=hashlib.sha256(raw).hexdigest(),
            mtime_ns=stat.st_mtime_ns if stat else document.mtime_ns,
            created_at=document.created_at,
            line_offsets=_compute_line_offsets(raw),
        )
        with self._uow_factory() as uow:
            uow.file_documents.update(rebuilt)
            uow.commit()
        return rebuilt

    def _slice(
        self,
        document: FileDocument,
        raw: bytes,
        start_line: int,
        end_line: int,
        changed: bool,
    ) -> ReadResult:
        """按行偏移切片。实际范围收敛到文件真实行数内。

        行偏移是内容的纯函数，不持久化——每次读取从原始字节重算。
        这样 DB 往返（登记卡丢失 offsets）不影响读取正确性。
        """
        offsets = _compute_line_offsets(raw)
        total = len(offsets)
        if total == 0:
            # 空文件：申请任何范围都得空内容，实际范围如实为 (start, start-1)
            return ReadResult(
                file_id=document.id,
                path=document.path or document.display_name,
                requested_range=(start_line, end_line),
                actual_range=(start_line, start_line - 1),
                total_lines=0,
                content_hash=document.content_hash,
                encoding=document.encoding,
                text="",
                changed_since_index=changed,
            )

        actual_end = min(end_line, total)
        actual_start = min(start_line, total)
        truncated: list[int] = []
        parts: list[str] = []
        for line_no in range(actual_start, actual_end + 1):
            begin = offsets[line_no - 1]
            end = offsets[line_no] if line_no < total else len(raw)
            line_bytes = raw[begin:end]
            if len(line_bytes) > MAX_LINE_BYTES:
                line_bytes = line_bytes[:MAX_LINE_BYTES]
                truncated.append(line_no)
            parts.append(line_bytes.decode(document.encoding, errors="replace").rstrip("\r\n"))

        return ReadResult(
            file_id=document.id,
            path=document.path or document.display_name,
            requested_range=(start_line, end_line),
            actual_range=(actual_start, actual_end),
            total_lines=total,
            content_hash=document.content_hash,
            encoding=document.encoding,
            text="\n".join(parts),
            truncated_lines=tuple(truncated),
            changed_since_index=changed,
        )


def _compute_line_offsets(raw: bytes) -> tuple[int, ...]:
    """每行的起始字节偏移。混合换行符（\\r\\n / \\n / \\r）都按行界处理。

    空文件返回空 tuple。末尾无换行的最后一行也计入。
    """
    if not raw:
        return ()
    offsets = [0]
    index = 0
    length = len(raw)
    while index < length:
        byte = raw[index]
        if byte == 0x0D:  # \r（可能跟着 \n）
            index += 2 if index + 1 < length and raw[index + 1] == 0x0A else 1
            if index < length:
                offsets.append(index)
        elif byte == 0x0A:  # \n
            index += 1
            if index < length:
                offsets.append(index)
        else:
            index += 1
    return tuple(offsets)
