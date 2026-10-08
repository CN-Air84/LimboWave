"""附件栏的 Qt 验收（Phase 4 / 设计计划 §三.1 输入区「添加附件」）。

锁住的不变量：
- 无附件时整体隐藏，有附件才显示；
- chip 显示标题 + 副信息；图片 chip 带缩略图；
- 移除按钮删掉 chip 并外发 attachment_removed；
- 附件 ID 列表供发送时装配；
- 发送后 clear 清空并隐藏。
"""

from __future__ import annotations

import struct
import zlib

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot

from limbowave.ui.attachment_bar import AttachmentBar


def _make_png_bytes(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_scan = b"\x00" + b"\xff\x00\x00" * width
    idat = zlib.compress(raw_scan * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def test_hidden_when_empty(qtbot: QtBot) -> None:
    bar = AttachmentBar()
    qtbot.addWidget(bar)
    assert bar.isHidden() or not bar.isVisible()
    assert bar.attachment_ids() == []


def test_add_text_attachment_shows_chip(qtbot: QtBot) -> None:
    bar = AttachmentBar()
    qtbot.addWidget(bar)
    bar.show()
    bar.add_attachment("file_1", "notes.md", "12 行 · 340 B")

    assert not bar.isHidden()
    assert bar.attachment_ids() == ["file_1"]
    assert "file_1" in bar._chips


def test_add_image_attachment_with_thumbnail(qtbot: QtBot) -> None:
    bar = AttachmentBar()
    qtbot.addWidget(bar)
    bar.show()
    pixmap = QPixmap()
    pixmap.loadFromData(_make_png_bytes(80, 60))

    bar.add_attachment(
        "img_1", "photo.png", "PNG · 80×60 · 2.1 KB · ~1000 tokens(估算)", thumbnail=pixmap
    )

    chip = bar._chips["img_1"]
    # 有缩略图标签
    from PySide6.QtWidgets import QLabel

    assert any(
        isinstance(w, QLabel) and w.pixmap() is not None and not w.pixmap().isNull()
        for w in chip.findChildren(QLabel)
    )


def test_many_attachments_scroll_horizontally_without_hiding_metadata(qtbot: QtBot) -> None:
    bar = AttachmentBar()
    qtbot.addWidget(bar)
    bar.resize(420, 100)
    bar.show()
    subtitle = "PNG · 1930x1203 · 535.8 KB · ~8857 tokens(估算)"
    bar.add_attachment("img_1", "pasted-image-1.png", subtitle)
    bar.add_attachment("img_2", "pasted-image-2.png", subtitle)

    scroll = bar._chips_scroll
    qtbot.waitUntil(lambda: scroll.horizontalScrollBar().maximum() > 0)
    assert scroll.horizontalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAsNeeded
    assert scroll.verticalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    labels = bar._chips["img_1"].findChildren(QLabel)
    assert any(label.text() == subtitle for label in labels)


def test_remove_chip_emits_and_hides(qtbot: QtBot) -> None:
    bar = AttachmentBar()
    qtbot.addWidget(bar)
    bar.show()
    bar.add_attachment("file_1", "a.txt", "3 行")

    with qtbot.waitSignal(bar.attachment_removed, timeout=1000) as blocker:
        bar._chips["file_1"].remove_clicked.emit("file_1")

    assert blocker.args == ["file_1"]
    assert bar.attachment_ids() == []
    assert not bar.isVisible()  # 空了又隐藏


def test_duplicate_id_ignored(qtbot: QtBot) -> None:
    bar = AttachmentBar()
    qtbot.addWidget(bar)
    bar.add_attachment("file_1", "a.txt", "3 行")
    bar.add_attachment("file_1", "a.txt", "3 行")
    assert bar.attachment_ids() == ["file_1"]


def test_clear_empties_all(qtbot: QtBot) -> None:
    bar = AttachmentBar()
    qtbot.addWidget(bar)
    bar.show()
    bar.add_attachment("f1", "a.txt", "1 行")
    bar.add_attachment("f2", "b.md", "5 行")

    bar.clear()

    assert bar.attachment_ids() == []
    assert not bar.isVisible()


def test_attach_signals(qtbot: QtBot) -> None:
    bar = AttachmentBar()
    qtbot.addWidget(bar)
    with qtbot.waitSignal(bar.attach_files_requested, timeout=1000):
        bar.attach_files_requested.emit()
    with qtbot.waitSignal(bar.attach_clipboard_requested, timeout=1000):
        bar.attach_clipboard_requested.emit()


def test_attach_button_opens_upward_menu_not_file_dialog(qtbot: QtBot) -> None:
    """附件按钮弹出上拉菜单，不直接发出文件选择意图。"""
    from limbowave.ui.chat_view import ChatView

    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    fired: list[bool] = []
    view.attachments.attach_files_requested.connect(lambda: fired.append(True))

    view._attach_btn.click()

    menu = view._attach_menu
    qtbot.waitUntil(menu.isVisible, timeout=1000)
    assert fired == []
    assert [a.text() for a in menu.actions() if not a.isSeparator()] == [
        "图片", "文档", "文件夹", "其他会话片段",
    ]
    button_top = view._attach_btn.mapToGlobal(view._attach_btn.rect().topLeft()).y()
    assert menu.geometry().bottom() <= button_top  # 向上展开
    menu.close()


def test_attach_menu_actions_emit_bar_signals(qtbot: QtBot) -> None:
    from limbowave.ui.chat_view import ChatView

    view = ChatView()
    qtbot.addWidget(view)
    bar = view.attachments
    actions = [a for a in view._attach_menu.actions() if not a.isSeparator()]
    signals = [
        bar.attach_images_requested,
        bar.attach_documents_requested,
        bar.attach_folder_requested,
        bar.attach_snippet_requested,
    ]
    for action, signal in zip(actions, signals, strict=True):
        with qtbot.waitSignal(signal, timeout=1000):
            action.trigger()


def test_word_support_is_visible_in_attachment_menu(qtbot: QtBot) -> None:
    from limbowave.domain.files import ATTACHMENT_FILE_FILTER, DOCUMENT_FILE_FILTER
    from limbowave.ui.attachment_bar import AttachmentMenu
    menu = AttachmentMenu()
    qtbot.addWidget(menu)
    action = next(item for item in menu.actions() if item.text() == "文档")
    assert "DOC/DOCX" in action.toolTip()
    assert "暂不支持 PDF" in action.toolTip()
    assert menu.toolTipsVisible()
    for file_filter in (ATTACHMENT_FILE_FILTER, DOCUMENT_FILE_FILTER):
        assert "*.doc " in file_filter and "*.docx" in file_filter
        assert "*.pdf" not in file_filter
    with qtbot.waitSignal(menu.documents_requested):
        action.trigger()
