"""图片附件服务（Task 4.4 / 设计计划 §八.4）。

规则逐条落地：

- **视觉模型优先发送原图**：不压缩不缩放；任何处理都必须显式记录（本阶段
  根本不做处理——发送的就是 blob 仓里的原始字节）。
- **原图保留在加密 blob 仓**（与剪贴板文档同一加密边界）。
- **不支持视觉的模型不得静默执行 OCR**：发送前做能力检查，不支持就明确
  拒绝并提示切换视觉模型。
- 尺寸/格式/大小/估算上下文占用都展示为**估算**，不伪装成精确值。

尺寸解析不依赖 Qt（架构守卫：application 层不碰 Qt）——PNG/JPEG 头部
手工解析，小而可测。
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from limbowave.application.repositories import UnitOfWorkFactory
from limbowave.domain.files import ImageAttachment, ImageFormat
from limbowave.infrastructure.crypto.blob_store import BlobStore


class ImageError(Exception):
    """图片处理失败的基类。"""


class NotAnImage(ImageError):
    """字节流不是合法的 PNG/JPEG（头部校验失败）。"""


class NoVisionCapability(ImageError):
    """目标模型不支持视觉，且不允许静默 OCR。"""


# ---------- 尺寸解析（纯字节，无外部依赖） ----------


def _png_dimensions(raw: bytes) -> tuple[int, int] | None:
    """PNG：IHDR 块在固定偏移（签名 8 字节 + 长度 4 + 类型 4，宽高各 4 字节大端）。"""
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if raw[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", raw[16:24])
    return (width, height) if width > 0 and height > 0 else None


def _jpeg_dimensions(raw: bytes) -> tuple[int, int] | None:
    """JPEG：扫 SOF 标记（0xFFC0-0xC3 等），取高度/宽度（大端 16 位）。"""
    if len(raw) < 4 or raw[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 9 < len(raw):
        if raw[index] != 0xFF:
            index += 1
            continue
        marker = raw[index + 1]
        # SOF 标记（不含 DHT/DAC/RST/TEM）
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if index + 9 >= len(raw):
                return None
            height, width = struct.unpack(">HH", raw[index + 5 : index + 9])
            return (width, height) if width > 0 and height > 0 else None
        # 跳过该段：长度字段含自身 2 字节
        if index + 3 >= len(raw):
            return None
        (segment_length,) = struct.unpack(">H", raw[index + 2 : index + 4])
        index += 2 + segment_length
    return None


def parse_image(raw: bytes) -> tuple[ImageFormat, int, int]:
    """解析格式与尺寸。不是合法 PNG/JPEG 抛 NotAnImage。"""
    if (dims := _png_dimensions(raw)) is not None:
        return ImageFormat.PNG, dims[0], dims[1]
    if (dims := _jpeg_dimensions(raw)) is not None:
        return ImageFormat.JPEG, dims[0], dims[1]
    raise NotAnImage("不是合法的 PNG 或 JPEG 文件")


# ---------- 上下文占用估算 ----------

# 估算而非精确：各站点的图片 token 化方式不同且多不公开。
# 取一个保守的公开量级参考（约每 512×512 图块 ~1k token），并如实标记为估算。
_TOKEN_PER_512_TILE = 1000


def estimate_context_tokens(width: int, height: int) -> int:
    """估算图片占用的上下文 token。**这是估算，不是精确值**（设计计划 §8.4 要求展示为估算）。"""
    if width <= 0 or height <= 0:
        return 0
    tiles = (width * height) / (512 * 512)
    return max(1, round(tiles * _TOKEN_PER_512_TILE))


@dataclass(frozen=True, slots=True)
class ImageSummary:
    """界面展示用的图片摘要。context_tokens 是估算值。"""

    attachment: ImageAttachment
    context_tokens_estimate: int
    size_text: str


class ImageService:
    """图片附件的导入、检查、读取。发送原图，永不压缩。"""

    def __init__(self, uow_factory: UnitOfWorkFactory, blob_store: BlobStore) -> None:
        self._uow_factory = uow_factory
        self._blob_store = blob_store

    # ---------- 导入 ----------

    def import_file(self, path: Path) -> ImageAttachment:
        """从磁盘导入图片。格式/尺寸校验失败抛 NotAnImage。"""
        raw = path.read_bytes()
        return self._register(raw, source_path=str(path))

    def import_bytes(self, raw: bytes, *, source_path: str | None = None) -> ImageAttachment:
        """从字节导入（如粘贴板图片）。"""
        return self._register(raw, source_path=source_path)

    def _register(self, raw: bytes, *, source_path: str | None) -> ImageAttachment:
        format_, width, height = parse_image(raw)  # 非法图片在这里抛 NotAnImage
        blob_id = self._blob_store.put(raw)
        attachment = ImageAttachment(
            id=f"img_{uuid4().hex[:16]}",
            format=format_,
            blob_id=blob_id,
            created_at=datetime.now(UTC),
            width=width,
            height=height,
            size_bytes=len(raw),
            content_hash=hashlib.sha256(raw).hexdigest(),
            source_path=source_path,
        )
        with self._uow_factory() as uow:
            uow.image_attachments.add(attachment)
            uow.commit()
        return attachment

    # ---------- 视觉能力检查 ----------

    def check_vision(self, attachment_id: str, *, supports_images: bool) -> None:
        """发送前检查：目标模型不支持视觉时明确拒绝，不静默 OCR。

        调用方（RunCoordinator）把当前模型的 ``supports_images`` 传进来。
        """
        if not supports_images:
            raise NoVisionCapability(
                "当前模型不支持图片输入。请在「站点与模型」里切换到支持视觉的模型，"
                "或移除图片附件。不会对该图片做 OCR。"
            )

    # ---------- 读取与展示 ----------

    def read_bytes(self, attachment_id: str) -> bytes:
        """读出原图字节（发送用）。**就是 blob 仓里的原始字节**。"""
        attachment = self._get(attachment_id)
        return self._blob_store.get(attachment.blob_id)

    def summarize(self, attachment_id: str) -> ImageSummary:
        attachment = self._get(attachment_id)
        return ImageSummary(
            attachment=attachment,
            context_tokens_estimate=estimate_context_tokens(attachment.width, attachment.height),
            size_text=_human_size(attachment.size_bytes),
        )

    def list_all(self) -> list[ImageAttachment]:
        with self._uow_factory() as uow:
            return uow.image_attachments.list_all()

    def delete(self, attachment_id: str) -> bool:
        """删除附件与 blob。引用检查由调用方负责（与 ClipboardService 同一规则）。"""
        attachment = self._get(attachment_id)
        with self._uow_factory() as uow:
            uow.image_attachments.delete(attachment_id)
            uow.commit()
        self._blob_store.delete(attachment.blob_id)
        return True

    def _get(self, attachment_id: str) -> ImageAttachment:
        with self._uow_factory() as uow:
            attachment = uow.image_attachments.get(attachment_id)
        if attachment is None:
            raise ImageError(f"图片附件不存在：{attachment_id}")
        return attachment


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"
