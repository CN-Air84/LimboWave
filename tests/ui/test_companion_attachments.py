from __future__ import annotations

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage, QTextCursor
from PySide6.QtWidgets import QFileDialog, QPushButton

from limbowave.domain.files import ATTACHMENT_FILE_FILTER
from limbowave.ui.main_window import MainWindow


@pytest.fixture
def companion(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window._companion.open()
    return window, window._companion.window


def local_file_mime(path="C:/sample.txt"):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path)])
    return mime


def test_user_message_is_a_right_aligned_bubble_without_card_header(companion, qtbot):
    window, mini = companion
    window.chat.add_user_message("你好")
    window._companion.refresh()
    qtbot.wait(30)
    bubble = mini._cards[-1 if len(mini.messages) == 3 else 0]
    assert bubble.objectName() == "companionUserBubble"
    assert bubble.text() == "你好"
    assert bubble.width() < mini._scroll.viewport().width() * 0.8
    assert bubble.geometry().right() >= bubble.parentWidget().width() - 4
    window.chat.begin_assistant()
    window.chat.end_assistant("我在。")
    window._companion.refresh()
    assert mini._cards[1].objectName() == "companionMessage"
    assert mini._cards[1].text() == "我在。"


def test_picker_uses_mini_parent_and_existing_import_signal(companion, monkeypatch):
    window, mini = companion
    selected = []
    window.chat.files_dropped.connect(selected.append)

    def choose(parent, title, directory, file_filter):
        assert parent is mini
        assert file_filter == ATTACHMENT_FILE_FILTER
        return ["C:/one.txt", "C:/photo.png"], ""

    monkeypatch.setattr(QFileDialog, "getOpenFileNames", choose)
    mini._attach.click()
    assert selected == [["C:/one.txt", "C:/photo.png"]]
    assert mini.isVisible() and not window.isVisible()


def test_stale_file_picker_result_does_not_attach_to_replaced_draft(companion, monkeypatch):
    window, mini = companion
    selected = []
    window.chat.files_dropped.connect(selected.append)

    def choose(*_args):
        window.chat.attachments.clear()  # A new draft invalidates pending work.
        return ["C:/old.txt"], ""

    monkeypatch.setattr(QFileDialog, "getOpenFileNames", choose)
    mini._attach.click()
    assert not selected


def test_file_paste_uses_attachment_path_but_text_pastes_at_mini_cursor(companion):
    window, mini = companion
    paths = []
    window.chat.files_dropped.connect(paths.append)
    mini._input.setPlainText("left right")
    cursor = mini._input.textCursor()
    cursor.setPosition(5)
    mini._input.setTextCursor(cursor)
    window.chat._input.moveCursor(QTextCursor.MoveOperation.End)
    mime = QMimeData()
    mime.setText("middle ")
    mini._input.insertFromMimeData(mime)
    assert mini._input.toPlainText() == "left middle right"
    file_mime = local_file_mime()
    assert mini._input.canInsertFromMimeData(file_mime)
    mini._input.insertFromMimeData(file_mime)
    assert paths == [["C:/sample.txt"]]
    assert window.chat._input.toPlainText() == "left middle right"


def test_image_paste_reuses_original_async_encoder_and_pending_guard(companion, qtbot):
    window, mini = companion
    raw = []
    window.chat.image_pasted.connect(raw.append)
    image = QImage(80, 40, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    mime = QMimeData()
    mime.setImageData(image)
    mini._input.setPlainText("look")
    assert mini._input.canInsertFromMimeData(mime)
    mini._input.insertFromMimeData(mime)
    assert window.chat._pending_pastes == 1
    assert not mini._send.isEnabled()
    qtbot.waitUntil(lambda: len(raw) == 1)
    assert QImage.fromData(raw[0]).size() == image.size()
    assert window.chat._pending_pastes == 0


def test_attachment_strip_mirrors_removes_and_clears_on_restore(companion):
    window, mini = companion
    window.chat.attachments.add_attachment("one", "notes.txt", "2 lines")
    window._companion.refresh()
    assert mini._attachments.attachment_ids() == ["one"]
    chip = mini._attachments._chips["one"]
    chip.findChild(QPushButton).click()
    assert window.chat.attachments.attachment_ids() == []
    window.chat.attachments.add_attachment("two", "picture.png", "image")
    window._companion.refresh()
    window._companion.restore()
    assert mini._attachments.attachment_ids() == []
    assert window.chat.attachments.attachment_ids() == ["two"]


def test_dragging_local_files_imports_without_inserting_paths(companion):
    window, mini = companion
    paths = []
    window.chat.files_dropped.connect(paths.append)
    mime = local_file_mime()
    enter = QDragEnterEvent(QPoint(20, 20), Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    mini.dragEnterEvent(enter)
    assert enter.isAccepted()
    drop = QDropEvent(QPointF(20, 20), Qt.DropAction.CopyAction, mime,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    mini.dropEvent(drop)
    assert drop.isAccepted()
    assert paths == [["C:/sample.txt"]]
    assert mini._input.toPlainText() == ""


@pytest.mark.parametrize("blocked", ["unavailable", "loading", "preview"])
def test_attachment_controls_respect_current_source_availability(companion, blocked):
    window, mini = companion
    if blocked == "unavailable":
        window.chat.set_available(False)
    elif blocked == "loading":
        window.chat.set_history_loading(True)
    else:
        window.prepare_history_preview()
        window.show_history_preview()
    window._companion.refresh()
    assert not mini._attach.isEnabled()
    assert not mini.acceptDrops()
    assert not mini._input.canInsertFromMimeData(local_file_mime())
    sent = []
    window.chat.files_dropped.connect(sent.append)
    mini._input.insertFromMimeData(local_file_mime())
    assert not sent


def test_clipboard_image_with_text_keeps_text_precedence(companion):
    window, mini = companion
    raw = []
    window.chat.image_pasted.connect(raw.append)
    mime = QMimeData()
    mime.setImageData(QImage(10, 10, QImage.Format.Format_RGB32))
    mime.setText("copied text")
    mini._input.insertFromMimeData(mime)
    assert mini._input.toPlainText() == "copied text"
    assert raw == []
    assert window.chat._pending_pastes == 0


def test_image_encoding_finishing_after_draft_clear_is_discarded(companion, qtbot):
    window, mini = companion
    raw = []
    window.chat.image_pasted.connect(raw.append)
    mime = QMimeData()
    image = QImage(80, 40, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    mime.setImageData(image)
    mini._input.insertFromMimeData(mime)
    window.chat.attachments.clear()
    qtbot.waitUntil(lambda: window.chat._pending_pastes == 0)
    assert not raw


def test_long_user_bubble_wraps_inside_narrow_window(companion, qtbot):
    window, mini = companion
    mini.resize(340, 480)
    window.chat.add_user_message("长一点的消息 " * 80)
    window._companion.refresh()
    qtbot.wait(30)
    bubble = mini._cards[0]
    assert bubble.width() <= mini._scroll.viewport().width() * 0.8 + 1
    assert mini.width() == 340


def test_controls_overlay_content_without_title_or_footer_hint(companion, qtbot):
    _window, mini = companion
    qtbot.wait(20)
    assert not hasattr(mini, "_title")
    assert not hasattr(mini, "_hint")
    assert mini.layout().indexOf(mini._top_controls) == -1
    assert mini._top_controls.parentWidget() is mini
    assert mini._bottom_controls.parentWidget() is mini._input
    assert mini._attach.parentWidget() is mini._bottom_controls
    assert mini._send.parentWidget() is mini._bottom_controls
    assert mini._input.viewport().geometry().bottom() < mini._bottom_controls.geometry().top()
    mini.resize(340, 520)
    qtbot.wait(20)
    assert mini._top_controls.geometry().right() < mini.width()
    assert mini._bottom_controls.geometry().right() < mini._input.width()
    mini.set_collapsed(True)
    assert mini._top_controls.isVisible()
    assert not mini._bottom_controls.isVisible()
    assert mini.height() <= 60
    mini.set_collapsed(False)
    assert mini._bottom_controls.isVisible()
