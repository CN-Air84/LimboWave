"""压缩 UI 组件的 Qt 验收（Phase 5）。

锁住的不变量：
- 占用条：展示为估算（「估算」字样），阈值变色（90% 危险色）；
- 压缩按钮：短按不触发，长按到点才 confirmed；
- 预览对话框：摘要可编辑、接受存编辑版并启用、失败版本显示错误且不可接受、
  回退清启用、版本历史列出全部版本。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from limbowave.application.services.compression_service import CompressionService
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from limbowave.ui import theme
from limbowave.ui.compression_widgets import (
    CompressButton,
    CompressionPreviewDialog,
    ContextUsageBar,
)

T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def service() -> CompressionService:
    factory = in_memory_uow_factory(InMemoryStore())
    with factory() as uow:
        uow.conversations.add(Conversation(id="c1", title="t", created_at=T0))
        uow.branches.add(Branch(id="b1", conversation_id="c1", created_at=T0))
        uow.messages.add(
            Message(
                id="m1",
                conversation_id="c1",
                branch_id="b1",
                role=MessageRole.USER,
                content="内容",
                created_at=T0,
            )
        )
        uow.commit()
    return CompressionService(factory)


# ---------- 占用条 ----------


def test_usage_bar_shows_estimate(qtbot: QtBot) -> None:
    bar = ContextUsageBar()
    qtbot.addWidget(bar)
    bar.set_usage(42.0, "约 42%（42000/128000 tokens，估算）")
    assert "估算" in bar._label.text()
    assert bar._bar.value() == 42


def test_usage_bar_unknown_when_unsupported(qtbot: QtBot) -> None:
    bar = ContextUsageBar()
    qtbot.addWidget(bar)
    bar.set_usage(None, "未知")
    assert bar._label.text() == "未知"
    assert bar._bar.value() == 0


def test_usage_bar_threshold_color(qtbot: QtBot) -> None:
    """90% 以上变危险色。"""
    bar = ContextUsageBar()
    qtbot.addWidget(bar)
    bar.set_usage(95.0, "约 95%")
    assert theme.DANGER_TEXT in bar._bar.styleSheet()


# ---------- 长按压缩按钮 ----------


def test_compress_button_requires_hold(qtbot: QtBot) -> None:
    """短按（点击）不触发压缩——必须长按。"""
    btn = CompressButton()
    qtbot.addWidget(btn)
    fired = []
    btn.confirmed.connect(lambda: fired.append(1))
    qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)  # 短按
    qtbot.wait(CompressButton.HOLD_MS + 200)  # 等超过长按阈值
    assert fired == []


def test_compress_button_hold_fires(qtbot: QtBot) -> None:
    """长按到阈值才触发。"""
    btn = CompressButton()
    qtbot.addWidget(btn)
    btn.show()
    fired = []
    btn.confirmed.connect(lambda: fired.append(1))
    btn._hold_timer.start()  # 模拟按住不放
    qtbot.wait(CompressButton.HOLD_MS + 200)
    assert fired == [1]


# ---------- 预览对话框 ----------


def _make_preview_version(service: CompressionService) -> str:
    v = service.create_version(
        "c1",
        "b1",
        tokens_before=42000,
        compression_model_id="glm-4.6",
        compression_endpoint_id="relay-a",
    )
    service.record_result(v.id, "生成摘要", 8000)
    return v.id


def test_preview_shows_summary_and_meta(qtbot: QtBot, service: CompressionService) -> None:
    version_id = _make_preview_version(service)
    dialog = CompressionPreviewDialog(service, version_id)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._versions.count() > 0)

    assert dialog._summary.toPlainText() == "生成摘要"
    assert "42000" in dialog._meta.text()
    assert "8000" in dialog._meta.text()
    assert "估算" in dialog._meta.text()
    assert "glm-4.6" in dialog._meta.text()


def test_preview_accept_stores_edit_and_activates(
    qtbot: QtBot, service: CompressionService
) -> None:
    """用户编辑后接受：编辑版另存，版本启用。"""
    version_id = _make_preview_version(service)
    dialog = CompressionPreviewDialog(service, version_id)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._versions.count() > 0)

    dialog._summary.setPlainText("用户改过的摘要")
    with qtbot.waitSignal(dialog.accepted):
        dialog._on_accept()

    version = service.get(version_id)
    assert version is not None
    assert version.edited_summary == "用户改过的摘要"
    assert version.generated_summary == "生成摘要"  # 生成版不动
    active = service.get_active("b1")
    assert active is not None and active.id == version_id


def test_preview_failed_version_shows_error_not_acceptable(
    qtbot: QtBot, service: CompressionService
) -> None:
    """失败版本：显示错误、不可接受、摘要不伪造。"""
    v = service.create_version(
        "c1", "b1", tokens_before=100, compression_model_id="m", compression_endpoint_id="e"
    )
    service.record_failure(v.id, "模型超时")
    dialog = CompressionPreviewDialog(service, v.id)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._versions.count() > 0)

    assert "模型超时" in dialog._meta.text()
    assert not dialog._accept_btn.isEnabled()
    assert dialog._summary.toPlainText() == ""


def test_preview_version_history_lists_all(qtbot: QtBot, service: CompressionService) -> None:
    """版本历史列出全部版本，含失败。"""
    v1 = _make_preview_version(service)
    v2 = service.create_version(
        "c1", "b1", tokens_before=90, compression_model_id="m", compression_endpoint_id="e"
    )
    service.record_failure(v2.id, "失败")

    dialog = CompressionPreviewDialog(service, v1)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._versions.count() > 0)
    assert dialog._versions.count() == 2
    texts = [dialog._versions.item(i).text() for i in range(2)]
    assert any("失败" in t for t in texts)


def test_preview_rollback_clears_active(qtbot: QtBot, service: CompressionService) -> None:
    version_id = _make_preview_version(service)
    service.accept(version_id)
    assert service.get_active("b1") is not None

    dialog = CompressionPreviewDialog(service, version_id)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._versions.count() > 0)
    rolled: list[str] = []
    dialog.rolled_back.connect(rolled.append)
    with qtbot.waitSignal(dialog.rolled_back):
        dialog._on_rollback()

    assert service.get_active("b1") is None
    assert rolled == ["b1"]
