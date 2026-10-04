"""附件贯穿验收（Task 4.4 + Phase 4 接缝）。

锁住的不变量：
- 图片载荷（base64 原图）经 ``send_message(images=...)`` 到达内核；
- 附件 id 记进请求意图快照（审计 + 引用检查的依据）；
- 模型不支持视觉时带图发送被**拒绝**（不静默 OCR），且不落任何 run；
- AttachmentService：图片进载荷，文本/剪贴板只记 id（不内联进 prompt）。
"""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path

import pytest

from limbowave.application.services.attachment_service import AttachmentService
from limbowave.application.services.file_service import FileService
from limbowave.application.services.image_service import ImageService
from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.infrastructure.crypto.blob_store import BlobStore
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from tests.unit.test_run_coordinator import FakeKernel


def _make_png(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_scan = b"\x00" + b"\xff\x00\x00" * width
    idat = zlib.compress(raw_scan * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


def _context(*, supports_images: bool = False) -> RunContext:
    return RunContext(
        logical_model_id="deepseek-chat",
        endpoint_id="relay-a",
        routing_reason="默认绑定",
        app_params={"model": "deepseek-chat"},
        supports_images=supports_images,
    )


class ImageCapturingKernel(FakeKernel):
    def __init__(self) -> None:
        super().__init__()
        self.images_received: list[list[dict]] = []

    async def send_message(self, text: str, *, images: list[dict] | None = None) -> None:
        self.images_received.append(images or [])
        await super().send_message(text, images=images)


# ---------- 协调器：图片贯穿 ----------


async def test_images_reach_kernel(store: InMemoryStore) -> None:
    kernel = ImageCapturingKernel()
    coord = RunCoordinator(
        kernel, in_memory_uow_factory(store), context=lambda: _context(supports_images=True)
    )
    payload = [
        {"type": "image", "data": base64.b64encode(b"png-bytes").decode(), "mimeType": "image/png"}
    ]
    run_id = await coord.send("看图说话", images=payload)

    assert run_id is not None
    assert kernel.images_received[0] == payload  # 原样到达内核


async def test_attachment_ids_recorded_in_intent(store: InMemoryStore) -> None:
    kernel = ImageCapturingKernel()
    coord = RunCoordinator(
        kernel, in_memory_uow_factory(store), context=lambda: _context(supports_images=True)
    )
    payload = [{"type": "image", "data": "x", "mimeType": "image/png"}]
    run_id = await coord.send("看图", attachment_ids=["img_1", "img_2"], images=payload)
    assert run_id is not None

    with in_memory_uow_factory(store)() as uow:
        intent = uow.snapshots.get_intent(run_id)
        assert intent is not None
        assert intent.attachment_ids == ("img_1", "img_2")


async def test_no_vision_rejects_and_records_nothing(store: InMemoryStore) -> None:
    """不支持视觉：拒绝发送，且**不落任何 run/消息**（拒绝发生在落库前）。"""
    kernel = ImageCapturingKernel()
    coord = RunCoordinator(
        kernel, in_memory_uow_factory(store), context=lambda: _context(supports_images=False)
    )
    errors: list[str] = []
    coord.subscribe(
        lambda e: errors.append(e.data.get("message", "")) if e.kind == "error" else None
    )

    payload = [{"type": "image", "data": "x", "mimeType": "image/png"}]
    run_id = await coord.send("看图", images=payload)

    assert run_id is None
    assert kernel.images_received == []  # 内核没收到
    assert any("不支持图片" in m and "OCR" in m for m in errors)
    with in_memory_uow_factory(store)() as uow:
        assert uow.conversations.list_all() == []  # 没落库


async def test_text_only_still_works_without_vision(store: InMemoryStore) -> None:
    """无图纯文本：视觉能力无关，照常发送。"""
    kernel = ImageCapturingKernel()
    coord = RunCoordinator(
        kernel, in_memory_uow_factory(store), context=lambda: _context(supports_images=False)
    )
    assert await coord.send("纯文本") is not None
    assert kernel.images_received[0] == []


# ---------- AttachmentService：装配 ----------


@pytest.fixture
def services(tmp_path: Path, vault_key: VaultKey):
    store = InMemoryStore()
    blob = BlobStore(tmp_path / "blobs", vault_key)
    files = FileService(in_memory_uow_factory(store), blob_store=blob)
    images = ImageService(in_memory_uow_factory(store), blob)
    return files, images, AttachmentService(files, images)


def test_build_image_payload_original_bytes(services) -> None:
    """图片进载荷：base64 解回就是原图字节（不压缩）。"""
    _files, images, attachments = services
    raw = _make_png(64, 64)
    image = images.import_bytes(raw, source_path="photo.png")

    payload = attachments.build([image.id])
    assert payload.attachment_ids == [image.id]
    assert len(payload.images) == 1
    decoded = base64.b64decode(payload.images[0]["data"])
    assert decoded == raw  # 原图，逐字节
    assert payload.images[0]["mimeType"] == "image/png"
    assert payload.display_names[image.id] == "photo.png"


def test_build_text_document_only_records_id(services, tmp_path: Path) -> None:
    """文本/剪贴板文档：只记 id，**不**变成图片载荷，也不内联进 prompt。"""
    files, _images, attachments = services
    path = tmp_path / "notes.md"
    path.write_text("# 笔记\n内容", encoding="utf-8")
    document = files.index_path(path)

    payload = attachments.build([document.id])
    assert payload.attachment_ids == [document.id]
    assert payload.images == []  # 文本不进图片载荷
    assert payload.display_names[document.id] == "notes.md"


def test_build_skips_unknown_ids(services) -> None:
    _, _, attachments = services
    payload = attachments.build(["no-such-id"])
    assert payload.attachment_ids == []
    assert payload.images == []


def test_build_mixed(services, tmp_path: Path) -> None:
    """混合附件：图片进载荷，文本记 id，顺序保持。"""
    files, images, attachments = services
    image = images.import_bytes(_make_png(8, 8))
    path = tmp_path / "a.txt"
    path.write_text("文本", encoding="utf-8")
    document = files.index_path(path)

    payload = attachments.build([document.id, image.id])
    assert payload.attachment_ids == [document.id, image.id]
    assert len(payload.images) == 1


# ---------- 文档注记：模型得知道有附件可读 ----------


def test_build_text_document_composes_note(services, tmp_path: Path) -> None:
    """文本文档生成注记：file_id/名称/行数齐备，**不含文件内容**。"""
    files, _images, attachments = services
    path = tmp_path / "notes.md"
    path.write_text("# 笔记\n机密内容第一行\n机密内容第二行\n", encoding="utf-8")
    document = files.index_path(path)

    payload = attachments.build([document.id])
    note = payload.document_note
    assert document.id in note
    assert "notes.md" in note
    assert "read_document" in note  # 告诉模型用哪个工具读
    assert "3 行" in note
    assert "机密内容" not in note  # 注记只列清单，不内联内容


def test_build_images_only_has_empty_note(services) -> None:
    """只有图片：无文本文档，注记为空（图片本身随消息走）。"""
    _files, images, attachments = services
    image = images.import_bytes(_make_png(8, 8), source_path="pic.png")
    payload = attachments.build([image.id])
    assert payload.images != []
    assert payload.document_note == ""


async def test_document_note_reaches_kernel_prompt_not_persisted_text(
    store: InMemoryStore,
) -> None:
    """注记进内核 prompt（模型能看见），落库的用户消息保持原文（UI/审计看原文）。"""
    kernel = FakeKernel()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=lambda: _context())
    note = "[用户附加了文档]\n- file_id: file_x · 名称: notes.md · 3 行"
    run_id = await coord.send("总结附件", attachment_ids=["file_x"], document_note=note)

    assert run_id is not None
    assert kernel.sent[0].startswith("总结附件\n\n[用户附加了文档]")
    assert "file_x" in kernel.sent[0]

    with in_memory_uow_factory(store)() as uow:
        intent = uow.snapshots.get_intent(run_id)
        assert intent is not None
        assert intent.attachment_ids == ("file_x",)
        user_message = uow.messages.get(intent.message_ids[-1])
        assert user_message is not None
        assert user_message.content == "总结附件"  # 原文，不含注记


async def test_document_only_send_reaches_kernel(store: InMemoryStore) -> None:
    """只附文档不打字：注记本身就是 prompt，可以发送。"""
    kernel = FakeKernel()
    coord = RunCoordinator(kernel, in_memory_uow_factory(store), context=lambda: _context())
    run_id = await coord.send("", attachment_ids=["file_x"], document_note="[用户附加了文档]")
    assert run_id is not None
    assert kernel.sent[0] == "[用户附加了文档]"
