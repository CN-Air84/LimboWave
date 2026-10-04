"""图片附件验收（Task 4.4 / 设计计划 §八.4）。

锁住的不变量：
- 原图直传：读出的字节与导入的字节逐字节相同（不压缩不缩放）；
- 尺寸/格式解析：PNG 与 JPEG 头部手工解析；
- 加密存储：blob 无明文；
- 视觉能力检查：不支持视觉的模型明确拒绝，不静默 OCR；
- 上下文占用是**估算**（标记为估算，不伪装精确）；
- 非法字节流明确拒绝（NotAnImage），不错误解析。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from limbowave.application.services.image_service import (
    ImageService,
    NotAnImage,
    NoVisionCapability,
    estimate_context_tokens,
    parse_image,
)
from limbowave.domain.files import ImageFormat
from limbowave.infrastructure.crypto.blob_store import BlobStore
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory


def _make_png(width: int, height: int) -> bytes:
    """造一个最小合法 PNG（1×1 像素数据无所谓，头部尺寸是重点）。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_scan = b"\x00" + b"\xff\x00\x00" * width  # 每行一个 filter 字节 + RGB
    idat = zlib.compress(raw_scan * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _make_jpeg(width: int, height: int) -> bytes:
    """造一个最小合法 JPEG 头（SOI + SOF0）。"""
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 8 + 3)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )
    return b"\xff\xd8" + sof0 + b"\xff\xd9"


@pytest.fixture
def service(tmp_path: Path, vault_key: VaultKey) -> ImageService:
    return ImageService(
        in_memory_uow_factory(InMemoryStore()), BlobStore(tmp_path / "blobs", vault_key)
    )


# ---------- 尺寸/格式解析 ----------


def test_parse_png_dimensions() -> None:
    fmt, w, h = parse_image(_make_png(1920, 1080))
    assert fmt is ImageFormat.PNG
    assert (w, h) == (1920, 1080)


def test_parse_jpeg_dimensions() -> None:
    fmt, w, h = parse_image(_make_jpeg(800, 600))
    assert fmt is ImageFormat.JPEG
    assert (w, h) == (800, 600)


def test_parse_rejects_garbage() -> None:
    with pytest.raises(NotAnImage):
        parse_image(b"this is not an image at all")


def test_parse_rejects_truncated_png() -> None:
    with pytest.raises(NotAnImage):
        parse_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4)  # 头被截断


# ---------- 导入与原图直传 ----------


def test_import_stores_original_bytes_encrypted(service: ImageService, tmp_path: Path) -> None:
    raw = _make_png(100, 50)
    attachment = service.import_bytes(raw, source_path="test.png")

    assert attachment.format is ImageFormat.PNG
    assert (attachment.width, attachment.height) == (100, 50)
    assert attachment.size_bytes == len(raw)

    # 加密：blob 文件里不是原图字节
    blob_file = tmp_path / "blobs" / f"{attachment.blob_id}.bin"
    assert blob_file.is_file()
    assert blob_file.read_bytes() != raw
    assert raw[:16] not in blob_file.read_bytes()


def test_read_returns_exact_original(service: ImageService) -> None:
    """原图直传：读出的字节与导入的逐字节相同。"""
    raw = _make_png(640, 480)
    attachment = service.import_bytes(raw)
    assert service.read_bytes(attachment.id) == raw


def test_import_file_from_disk(service: ImageService, tmp_path: Path) -> None:
    path = tmp_path / "photo.png"
    path.write_bytes(_make_png(320, 200))
    attachment = service.import_file(path)
    assert attachment.source_path == str(path)
    assert attachment.dimensions == "320×200"


# ---------- 视觉能力检查 ----------


def test_vision_check_passes_for_vision_model(service: ImageService) -> None:
    attachment = service.import_bytes(_make_png(10, 10))
    service.check_vision(attachment.id, supports_images=True)  # 不抛即通过


def test_vision_check_rejects_non_vision_model(service: ImageService) -> None:
    """不支持视觉的模型：明确拒绝，提示切换，**不做 OCR**。"""
    attachment = service.import_bytes(_make_png(10, 10))
    with pytest.raises(NoVisionCapability, match="不会对该图片做 OCR"):
        service.check_vision(attachment.id, supports_images=False)


# ---------- 上下文占用估算 ----------


def test_estimate_is_proportional_and_marked_estimate() -> None:
    """估算随面积增长；零尺寸返回 0。这是估算不是精确值。"""
    small = estimate_context_tokens(512, 512)
    large = estimate_context_tokens(2048, 2048)
    assert small == 1000  # 一个 512×512 图块 ≈ 1k token
    assert large > small
    assert estimate_context_tokens(0, 0) == 0


def test_summarize_shows_estimate_and_size(service: ImageService) -> None:
    attachment = service.import_bytes(_make_png(1024, 1024))
    summary = service.summarize(attachment.id)
    assert summary.context_tokens_estimate > 0
    assert summary.size_text.endswith(("KB", "B", "MB"))
    assert summary.attachment.dimensions == "1024×1024"


# ---------- 删除 ----------


def test_delete_removes_blob(service: ImageService, tmp_path: Path, vault_key: VaultKey) -> None:
    blob_store = BlobStore(tmp_path / "blobs", vault_key)
    attachment = service.import_bytes(_make_png(10, 10))
    blob_id = attachment.blob_id
    assert blob_store.exists(blob_id)
    assert service.delete(attachment.id)
    assert not blob_store.exists(blob_id)
    assert service.list_all() == []
