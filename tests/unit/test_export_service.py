"""单文件 HTML 导出验收（Phase 8 / §十四.2）。

锁住的不变量：
- 只导出**当前分支**（其他分支的消息不出现）；
- 单文件：无外部资源引用（样式内联、图片 data URI）；
- 图片内嵌成 data URI；
- 思考内容与工具步骤默认折叠（<details>）；
- 保留模型与站点信息；
- **不导出**密钥、完整请求头、原始请求体；
- 隐私提示存在且明说「文件是明文」。
"""

from __future__ import annotations

import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from limbowave.application.services.export_service import ExportService, privacy_notice
from limbowave.domain.conversation import (
    Branch,
    Conversation,
    Message,
    MessageRole,
    MessageStatus,
)
from limbowave.domain.run import RunRecord, RunStatus
from limbowave.domain.snapshots import RequestIntentSnapshot, TransportSnapshot
from limbowave.infrastructure.crypto.blob_store import BlobStore
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory

T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
SECRET = "sk-live-MUST-NOT-BE-IN-EXPORT"


def _make_png(width: int = 8, height: int = 8) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress((b"\x00" + b"\x11\x22\x33" * width) * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.fixture
def seeded(tmp_path: Path, vault_key: VaultKey):
    """一条主分支 + 一条分叉分支，主分支有图片附件与完整双快照。"""
    store = InMemoryStore()
    blobs = BlobStore(tmp_path / "blobs", vault_key)
    factory = in_memory_uow_factory(store)
    with factory() as uow:
        uow.conversations.add(Conversation(id="c1", title="部署讨论", created_at=T0))
        uow.branches.add(Branch(id="b1", conversation_id="c1", created_at=T0))
        uow.branches.add(
            Branch(
                id="b2",
                conversation_id="c1",
                created_at=T0 + timedelta(minutes=5),
                parent_branch_id="b1",
                forked_from_message_id="m1",
            )
        )
        uow.messages.add(
            Message(
                id="m1",
                conversation_id="c1",
                branch_id="b1",
                role=MessageRole.USER,
                content="看这张图",
                created_at=T0,
            )
        )
        uow.messages.add(
            Message(
                id="m2",
                conversation_id="c1",
                branch_id="b1",
                role=MessageRole.ASSISTANT,
                content="**收到**，这是图。",
                created_at=T0 + timedelta(seconds=1),
                run_id="r1",
                status=MessageStatus.COMPLETE,
                thinking="先看图再答",
            )
        )
        # 分叉分支上的消息（不该出现在 b1 的导出里）
        uow.messages.add(
            Message(
                id="m3",
                conversation_id="c1",
                branch_id="b2",
                role=MessageRole.USER,
                content="分叉分支上的问题",
                created_at=T0 + timedelta(minutes=5),
            )
        )

        # 图片附件
        raw = _make_png()
        blob_id = blobs.put(raw)
        from limbowave.domain.files import ImageAttachment, ImageFormat

        uow.image_attachments.add(
            ImageAttachment(
                id="img_1",
                format=ImageFormat.PNG,
                blob_id=blob_id,
                created_at=T0,
                width=8,
                height=8,
                size_bytes=len(raw),
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
                assistant_message_id="m2",
            )
        )
        uow.snapshots.add_intent(
            RequestIntentSnapshot(
                id="i1",
                run_id="r1",
                conversation_id="c1",
                branch_id="b1",
                logical_model_id="deepseek-chat",
                endpoint_id="relay-a",
                routing_reason="默认绑定",
                created_at=T0,
                app_params={"model": "deepseek-chat", "max_tokens": 4096},
                attachment_ids=("img_1",),
            )
        )
        # 传输快照里带密钥（脱敏视图）——导出绝不能把它带出去
        uow.snapshots.add_transport(
            TransportSnapshot(
                id="t1",
                run_id="r1",
                created_at=T0,
                sequence=1,
                url="https://relay-a.example.com/v1/chat/completions",
                headers={
                    "Authorization": {"present": True, "scheme": "Bearer", "value": "[REDACTED]"}
                },
                body={"model": "deepseek-chat", "api_key": SECRET, "messages": []},
                response_status=200,
            )
        )
        uow.commit()
    return ExportService(factory, blobs), factory, tmp_path


def test_exports_only_current_branch(seeded, tmp_path: Path) -> None:
    export, _, _ = seeded
    result = export.export_branch("b1", tmp_path / "out.html")

    page = result.path.read_text(encoding="utf-8")
    assert "看这张图" in page
    assert "收到" in page
    # 分叉分支的消息不得出现
    assert "分叉分支上的问题" not in page
    assert result.message_count == 2


def test_single_file_no_external_resources(seeded, tmp_path: Path) -> None:
    """单文件：不引用外部 CSS/JS/图片。"""
    export, _, _ = seeded
    page = export.export_branch("b1", tmp_path / "out.html").path.read_text(encoding="utf-8")
    assert "<style>" in page
    assert 'rel="stylesheet"' not in page
    assert "<script" not in page
    # 没有 http 资源引用（说明文字里的 URL 不算）
    assert 'src="http' not in page
    assert 'href="http' not in page


def test_images_embedded_as_data_uri(seeded, tmp_path: Path) -> None:
    export, _, _ = seeded
    result = export.export_branch("b1", tmp_path / "out.html")
    page = result.path.read_text(encoding="utf-8")
    assert "data:image/png;base64," in page
    assert result.image_count == 1


def test_thinking_and_toolsteps_collapsed(seeded, tmp_path: Path) -> None:
    """思考内容与工具步骤默认折叠（<details>）。"""
    export, _, _ = seeded
    page = export.export_branch("b1", tmp_path / "out.html").path.read_text(encoding="utf-8")
    assert "<details" in page
    assert "思考过程" in page
    assert "工具步骤" in page
    assert "先看图再答" in page  # 内容在，只是折叠


def test_model_and_site_info_retained(seeded, tmp_path: Path) -> None:
    export, _, _ = seeded
    page = export.export_branch("b1", tmp_path / "out.html").path.read_text(encoding="utf-8")
    assert "deepseek-chat" in page
    assert "relay-a" in page
    assert "默认绑定" in page


def test_no_secrets_headers_or_raw_payload(seeded, tmp_path: Path) -> None:
    """默认不包含密钥、完整请求头、原始请求体。"""
    export, _, _ = seeded
    page = export.export_branch("b1", tmp_path / "out.html").path.read_text(encoding="utf-8")
    assert SECRET not in page
    assert "Authorization" not in page
    assert "[REDACTED]" not in page
    assert "/v1/chat/completions" not in page  # 原始 URL 也不导出
    assert "已省略完整请求头与请求体" in page  # 明确说明省略了什么


def test_page_contains_privacy_notice(seeded, tmp_path: Path) -> None:
    export, _, _ = seeded
    page = export.export_branch("b1", tmp_path / "out.html").path.read_text(encoding="utf-8")
    assert "明文" in page


def test_privacy_notice_text_is_honest() -> None:
    """提示必须同时说明「含什么」与「不含什么」，并点明不加密。"""
    notice = privacy_notice()
    assert "明文" in notice and "不加密" in notice
    assert "不含" in notice
    assert "密钥" in notice


def test_export_is_plaintext_not_encrypted(seeded, tmp_path: Path) -> None:
    """导出文件是明文 HTML（设计明确要求它不是加密文件）。"""
    export, _, _ = seeded
    result = export.export_branch("b1", tmp_path / "out.html")
    head = result.path.read_bytes()[:64]
    assert head.startswith(b"<!DOCTYPE html>")
    assert "看这张图" in result.path.read_text(encoding="utf-8")


def test_missing_blob_does_not_break_export(seeded, tmp_path: Path) -> None:
    """blob 缺失时跳过图片而不是整体失败。"""
    export, factory, _ = seeded
    with factory() as uow:
        attachment = uow.image_attachments.get("img_1")
        assert attachment is not None
        uow.image_attachments.delete("img_1")  # 登记卡没了
        uow.commit()
    result = export.export_branch("b1", tmp_path / "out.html")
    assert result.image_count == 0
    assert result.message_count == 2  # 消息照常导出


def test_unknown_branch_raises(seeded, tmp_path: Path) -> None:
    export, _, _ = seeded
    with pytest.raises(ValueError, match="分支不存在"):
        export.export_branch("nope", tmp_path / "x.html")


def test_code_blocks_are_highlighted_inline(seeded, tmp_path: Path) -> None:
    """代码块用**内联样式**着色（单文件不能依赖外部样式表）。"""
    export, factory, _ = seeded
    with factory() as uow:
        uow.messages.add(
            Message(
                id="m9",
                conversation_id="c1",
                branch_id="b1",
                role=MessageRole.ASSISTANT,
                content="```python\ndef f(x):\n    return x + 1\n```",
                created_at=T0 + timedelta(seconds=9),
            )
        )
        uow.commit()
    page = export.export_branch("b1", tmp_path / "out.html").path.read_text(encoding="utf-8")
    assert '<span style="color: #' in page  # 内联着色
    assert "<style>" in page  # 页面样式也是内联的
    assert 'rel="stylesheet"' not in page  # 没有外部样式表
