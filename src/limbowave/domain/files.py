"""文件与附件的领域模型（Phase 4）。

核心区分：
- ``FileDocument``：被索引的 TXT/MD/Word 文件（或剪贴板临时文档）的**登记卡**——
  稳定 id、路径、行数、内容哈希、修改时间。文件内容本身不内嵌在登记卡里，
  磁盘文件直接读；剪贴板文档的内容在加密 blob 仓。
- ``ImageAttachment``：图片附件登记卡。原图字节在加密 blob 仓。

设计约束（设计计划 §八）：
- **稳定行号**：行号从 1 开始，索引时按内容切行固定。
- **内容哈希 + mtime** 用于变化检测：文件被改后旧读取记录仍指向当时版本，
  或明确标记版本不一致。
- 图片不压缩不缩放（原图直传）；任何处理都必须显式记录。
- Word 先提取正文与表格文字；PDF 等未支持类型明确拒绝。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class FileKind(enum.StrEnum):
    TEXT = "text"  # .txt，以及提取为文本的 .doc/.docx
    MARKDOWN = "markdown"  # .md
    CLIPBOARD = "clipboard"  # 剪贴板临时文档


class ImageFormat(enum.StrEnum):
    JPEG = "jpeg"
    PNG = "png"


class UnsupportedFileType(Exception):
    """不支持的文件类型（如 PDF）。明确拒绝，不尝试错误解析。"""


# 支持的后缀 → 类型
WORD_SUFFIXES = frozenset({".doc", ".docx"})
DOCUMENT_FILE_FILTER = "文档 (*.txt *.md *.doc *.docx)"
ATTACHMENT_FILE_FILTER = "支持的文件 (*.txt *.md *.doc *.docx *.jpg *.jpeg *.png)"
SUPPORTED_ATTACHMENTS_HINT = (
    "支持 TXT、Markdown、Word（DOC/DOCX）、JPG、PNG；"
    "Word 仅提取正文与表格文字，不含图片识别和排版；暂不支持 PDF"
)
_TEXT_SUFFIXES = {
    ".txt": FileKind.TEXT,
    ".md": FileKind.MARKDOWN,
    ".doc": FileKind.TEXT,
    ".docx": FileKind.TEXT,
}
_IMAGE_SUFFIXES = {".jpg": ImageFormat.JPEG, ".jpeg": ImageFormat.JPEG, ".png": ImageFormat.PNG}


def classify_path(path: Path) -> FileKind | ImageFormat:
    """按后缀分类。不支持的类型抛 UnsupportedFileType（如 PDF）。"""
    suffix = path.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return _TEXT_SUFFIXES[suffix]
    if suffix in _IMAGE_SUFFIXES:
        return _IMAGE_SUFFIXES[suffix]
    raise UnsupportedFileType(f"不支持的文件类型：{suffix or path.name}")


@dataclass(frozen=True, slots=True)
class FileDocument:
    """TXT/MD/Word/剪贴板文档的登记卡。不可变；文件变化后重建新卡（索引是版本化的）。"""

    id: str
    kind: FileKind
    display_name: str  # 剪贴板文档可重命名；磁盘文件默认文件名
    created_at: datetime
    path: str | None = None  # 磁盘文件路径；剪贴板文档为 None
    blob_id: str | None = None  # 剪贴板文档：内容所在的加密 blob
    encoding: str = "utf-8"
    line_count: int = 0
    size_bytes: int = 0
    content_hash: str = ""  # sha256，变化检测用
    mtime_ns: int = 0  # 磁盘文件的修改时间（ns）；剪贴板文档为 0
    line_offsets: tuple[int, ...] = ()  # 每行的字节偏移（稳定行号的基础）


@dataclass(frozen=True, slots=True)
class ImageAttachment:
    """图片附件登记卡。原图在加密 blob 仓，这里只有元数据。"""

    id: str
    format: ImageFormat
    blob_id: str  # 加密 blob 仓的键
    created_at: datetime
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    content_hash: str = ""
    source_path: str | None = None  # 导入来源（仅记录，不作权威）

    @property
    def dimensions(self) -> str:
        """展示用尺寸文本，如 "1920×1080"。未知尺寸返回空串。"""
        return f"{self.width}×{self.height}" if self.width and self.height else ""


# ---------- 编码检测 ----------


def detect_encoding(raw: bytes) -> tuple[str, str]:
    """确定性编码检测。返回 (编码名, 解码后的文本)。

    顺序固定（确定性，不猜概率）：
    1. BOM：UTF-8 BOM / UTF-16 LE / UTF-16 BE；
    2. UTF-8 strict 试解；
    3. GBK 试解（中文 Windows 常见）；
    4. 都失败：UTF-8 + replace，**如实标记**为 "utf-8?"（问号表示有替换字符）。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", raw.decode("utf-8-sig")
    # UTF-16：用 "utf-16" 编解码器（按 BOM 判向并剥掉 BOM），
    # 显式 utf-16-le/be 会把 BOM 字节误解为 U+FEFF 字符
    if raw.startswith(b"\xff\xfe"):
        return "utf-16-le", raw.decode("utf-16")
    if raw.startswith(b"\xfe\xff"):
        return "utf-16-be", raw.decode("utf-16")
    try:
        return "utf-8", raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return "gbk", raw.decode("gbk")
    except UnicodeDecodeError:
        pass
    return "utf-8?", raw.decode("utf-8", errors="replace")
