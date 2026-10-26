from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from limbowave.ui.main_window import MainWindow


@pytest.fixture
def window(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    return window


def open_mini(window, qtbot):
    window._companion.open()
    mini = window._companion.window
    assert mini is not None
    qtbot.waitUntil(mini.isVisible)
    return mini


def test_shared_draft_and_exactly_once_send(window, qtbot):
    chat = window.chat
    chat._input.setPlainText("未发送的草稿")
    submitted = []
    window.command_requested.connect(submitted.append)
    mini = open_mini(window, qtbot)
    assert not window.isVisible()
    assert mini._input.document() is chat._input.document()
    mini._input.insertPlainText("继续")
    assert mini._input.toPlainText() == chat._input.toPlainText()
    qtbot.keyClick(mini._input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert len(submitted) == 1
    assert chat._input.toPlainText() == ""
    assert mini._input.toPlainText() == ""
    window._companion.restore()
    assert window.isVisible() and not mini.isVisible()
    assert len(submitted) == 1


def test_three_recent_messages_stream_stop_and_collapse(window, qtbot):
    chat = window.chat
    for i in range(5):
        chat.add_user_message(f"message {i}")
    mini = open_mini(window, qtbot)
    assert [text for _, text in mini.messages] == ["message 2", "message 3", "message 4"]
    chat.set_busy(True)
    chat.begin_assistant()
    chat.append_assistant_delta("正在回复")
    window._companion.refresh()
    assert mini._stop.isEnabled()
    stopped = []
    window.stop_requested.connect(lambda: stopped.append(True))
    mini._stop.click()
    assert stopped == [True]
    qtbot.keyClick(mini._input, Qt.Key.Key_Escape)
    assert mini.collapsed
    assert not mini._body.isVisible()
    assert mini.isVisible()
    mini._collapse.click()
    assert not mini.collapsed
    assert not stopped[1:]


def test_close_restores_geometry_without_cancelling_run(window, qtbot):
    geometry = window.geometry()
    window.chat.set_busy(True)
    stopped = []
    window.stop_requested.connect(lambda: stopped.append(True))
    mini = open_mini(window, qtbot)
    mini.close()
    assert window.isVisible() and not mini.isVisible()
    assert window.geometry() == geometry
    assert window.chat._busy
    assert stopped == []


def test_full_page_suspends_and_clears_sensitive_preview(window, qtbot):
    window.chat.add_user_message("private text")
    mini = open_mini(window, qtbot)
    window.show_full_page(QWidget())
    assert not mini.isVisible()
    assert not mini.messages
    assert mini._input.toPlainText() == ""
    assert not window._companion.active
    window._companion.open()
    assert not mini.isVisible()
    window.show_workspace()
    window._companion.open()
    assert mini.isVisible()


def test_history_preview_rebinds_before_deletion(window, qtbot):
    mini = open_mini(window, qtbot)
    window.chat._input.setPlainText("live draft")
    preview = window.prepare_history_preview()
    preview._input.setPlainText("history draft")
    window.show_history_preview()
    assert mini._input.document() is preview._input.document()
    window._companion.refresh()
    assert not mini._send.isEnabled()
    window.clear_history_preview()
    qtbot.wait(10)
    assert mini._input.document() is window.chat._input.document()
    assert mini._input.toPlainText() == "live draft"


def test_unavailable_loading_attachments_and_queue_are_not_bypassed(window, qtbot):
    chat = window.chat
    mini = open_mini(window, qtbot)
    mini._input.setPlainText("hello")
    chat.set_available(False, "未连接模型")
    window._companion.refresh()
    assert not mini._send.isEnabled()
    assert "未连接模型" in mini._status.text()
    chat.set_available(True)
    chat.set_history_loading(True)
    window._companion.refresh()
    assert not mini._send.isEnabled()
    assert not mini._input.isEnabled()
    chat.set_history_loading(False)
    chat.attachments.add_attachment("file-id", "draft.txt", "payload")
    window._companion.refresh()
    assert "附件" in mini._notice.text()
    chat.set_busy(True)
    chat.set_send_queue((), 0, "", can_enqueue=True)
    window._companion.refresh()
    assert not mini._send.isEnabled()  # Existing attachment queue restriction.
    chat.attachments.clear()
    queued = []
    chat.message_queued.connect(queued.append)
    window._companion.refresh()
    mini._send.click()
    assert queued == ["hello"]
    assert mini._input.toPlainText() == "hello"  # Cleared only after queue acceptance.


def test_close_guard_keeps_companion_alive_until_close_accepted(window, qtbot):
    mini = open_mini(window, qtbot)
    window.set_close_guard(lambda: False)
    window.close()
    assert mini.isVisible()
    window.set_close_guard(lambda: True)
    window.close()
    assert not mini.isVisible()
    assert not window._companion._timer.isActive()


def test_hidden_stream_pending_text_and_error_stay_visible(window, qtbot):
    chat = window.chat
    chat.set_busy(True)
    chat.begin_assistant()
    chat.append_assistant_delta("before")
    mini = open_mini(window, qtbot)
    row = chat._stream_row
    assert row._stream_buffer._paused
    chat.append_assistant_delta(" after")
    window._companion.refresh()
    assert mini.messages[-1][1] == "before after"
    assert row._stream_buffer._pending == [" after"]  # Projection never drains it.
    row.set_run_error("网络请求失败")
    window._companion.refresh()
    assert "网络请求失败" in mini.messages[-1][1]
    window._companion.restore()
    assert row.content_text() == "before after"


def test_undo_enter_and_ime_commit_share_the_original_document(window, qtbot):
    from PySide6.QtGui import QInputMethodEvent
    from PySide6.QtWidgets import QApplication

    window.chat._input.setPlainText("draft")
    mini = open_mini(window, qtbot)
    mini._input.moveCursor(mini._input.textCursor().MoveOperation.End)
    qtbot.keyClick(mini._input, Qt.Key.Key_Return)
    event = QInputMethodEvent()
    event.setCommitString("你好")
    QApplication.sendEvent(mini._input, event)
    assert window.chat._input.toPlainText() == "draft\n你好"
    window._companion.restore()
    window.chat._input.undo()
    assert window.chat._input.toPlainText() == "draft\n"
    mini = open_mini(window, qtbot)
    assert mini._input.toPlainText() == "draft\n"


def test_new_permission_prompt_restores_main_without_approving(window, qtbot):
    from limbowave.domain.permissions import Capability, ResourceScope, ToolRequest
    from limbowave.ui.permission_prompt import ask_permission

    mini = open_mini(window, qtbot)
    answers = []
    request = ToolRequest(tool_name="read", capability=Capability.FILE_READ,
                          scope=ResourceScope(paths=("C:/sample.txt",)), raw_params={})
    panel = ask_permission(window, request, "需要你的确认", None, answers.append)
    assert window.isVisible()
    assert not mini.isVisible()
    qtbot.waitUntil(panel.isVisible)
    assert answers == []
    window._companion.open()
    assert not mini.isVisible()  # Cannot hide an unresolved approval.
    panel.close_panel()
    assert answers == ["deny"]


def test_visible_geometry_handles_disconnected_and_negative_screens():
    from PySide6.QtCore import QRect

    from limbowave.ui.companion_window import visible_geometry

    left = QRect(-1920, 0, 1920, 1040)
    assert left.contains(visible_geometry(QRect(4000, 4000, 420, 480), left))
    assert visible_geometry(QRect(-1800, 20, 420, 480), left) == QRect(-1800, 20, 420, 480)
    small = QRect(0, 0, 360, 280)
    assert visible_geometry(QRect(300, -400, 420, 480), small) == small


def test_presentation_defaults_and_bounded_previews(window, qtbot, monkeypatch):
    window.chat.add_user_message("<a href='evil'>" + "x" * 5000)
    mini = open_mini(window, qtbot)
    assert not mini.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert mini.windowOpacity() == 1.0
    assert len(mini.messages[-1][1]) < 1300
    assert mini._cards[0].textFormat() == Qt.TextFormat.PlainText
    activated = []
    monkeypatch.setattr(mini, "activateWindow", lambda: activated.append(True))
    for _ in range(3):
        window._companion.refresh()
    assert not activated
    mini._pin.setChecked(True)
    assert mini.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    mini._translucent.setChecked(True)
    assert abs(mini.windowOpacity() - 0.90) < 0.01
    mini._translucent.setChecked(False)
    assert mini.windowOpacity() == 1.0


def test_local_shortcut_conflict_is_reported(window, qtbot):
    mini = open_mini(window, qtbot)
    window._companion.configure_shortcut("Ctrl+Shift+M")
    assert "已用于" in mini._notice.text()


def test_collapsed_geometry_and_refresh_lifecycle(window, qtbot):
    mini = open_mini(window, qtbot)
    expanded = mini.size()
    mini.set_collapsed(True)
    assert mini.height() <= 100
    mini.set_collapsed(False)
    assert mini.size() == expanded
    window._companion.restore()
    assert not window._companion._timer.isActive()
    window._companion.open()
    assert window._companion._timer.isActive()


def test_restore_preserves_maximized_but_never_leaves_main_minimized(window, qtbot):
    window.showMaximized()
    mini = open_mini(window, qtbot)
    mini.close()
    assert window.isMaximized()
    window.showMinimized()
    mini = open_mini(window, qtbot)
    mini.close()
    assert not window.isMinimized()
    assert window.isVisible()


def test_application_shortcut_round_trip(window, qtbot):
    window.activateWindow()
    qtbot.wait(20)
    qtbot.keyClick(window.chat._input, Qt.Key.Key_M,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    qtbot.waitUntil(lambda: window._companion.active)
    mini = window._companion.window
    mini.activateWindow()
    qtbot.wait(20)
    qtbot.keyClick(mini._input, Qt.Key.Key_M,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    qtbot.waitUntil(lambda: not window._companion.active)


def test_hidden_preview_detaches_before_document_owner_is_deleted(window, qtbot):
    preview = window.prepare_history_preview()
    preview._input.setPlainText("private preview draft")
    window.show_history_preview()
    mini = open_mini(window, qtbot)
    window._companion.restore()
    assert mini._input.toPlainText() == ""
    assert mini.messages == ()
    window.clear_history_preview()
    qtbot.wait(10)
    window.chat._input.setPlainText("live")
    open_mini(window, qtbot)
    assert mini._input.toPlainText() == "live"


def test_can_disable_global_shortcut_for_the_session(window, qtbot, monkeypatch):
    mini = open_mini(window, qtbot)
    released = []
    monkeypatch.setattr(window._companion._hotkey, "close", lambda: released.append(True))
    mini._global_shortcut.setChecked(False)
    assert released == [True]
    assert not window._companion._global_enabled
    assert "已关闭" in mini._options.toolTip()
    window._companion.restore()
    open_mini(window, qtbot)
    assert not mini._global_shortcut.isChecked()
    assert "已关闭" in mini._options.toolTip()


def test_loading_disables_mini_input_immediately_without_timer_tick(window, qtbot):
    mini = open_mini(window, qtbot)
    window.chat.set_history_loading(True)
    assert not mini._input.isEnabled()
    assert not mini._send.isEnabled()
    window.chat.set_history_loading(False)
    assert mini._input.isEnabled()


def test_small_work_area_falls_back_to_visible_bar(window, qtbot, monkeypatch):
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QApplication

    mini = open_mini(window, qtbot)
    area = QRect(-300, 0, 300, 200)

    class SmallScreen:
        def availableGeometry(self):
            return area

    monkeypatch.setattr(QApplication, "screens", lambda: [SmallScreen()])
    mini.ensure_on_screen()
    assert mini.collapsed
    assert area.contains(mini.frameGeometry())
    window.chat.set_status("这是一个非常长的运行阶段名称，不应该把展开按钮挤出屏幕")
    window._companion.refresh()
    qtbot.wait(10)
    assert mini.width() <= 300
    assert mini._restore.isVisible()
    assert mini._restore.mapTo(mini, mini._restore.rect().topRight()).x() < mini.width()
