from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from limbowave.application.services.send_queue import SendQueue
from limbowave.ui.chat_view import ChatView


def test_queue_button_and_shortcut_do_not_clear_until_app_accepts(qtbot):
    view = ChatView(draft_only=True)
    qtbot.addWidget(view)
    view.set_send_queue((), 0, "", can_enqueue=True)
    view.show()
    queued, sent = [], []
    view.message_queued.connect(queued.append)
    view.message_submitted.connect(sent.append)
    view._input.setPlainText("queued text")
    assert view._send_btn.isEnabled()
    qtbot.keyClick(view._input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert queued == ["queued text"] and not sent
    assert view._input.toPlainText() == "queued text"
    view.set_history_loading(True)
    view._on_send()
    assert len(queued) == 1
    assert "依次自动发送" in view._draft_notice.text()


def test_live_busy_queue_disallows_attachments_and_edits(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.set_busy(True)
    view.set_send_queue((), 0, "", can_enqueue=True)
    view._input.setPlainText("follow up")
    assert not view._send_btn.isHidden() and not view._stop_btn.isHidden()
    assert view._send_btn.isEnabled()
    view.attachments.add_attachment("a", "file", "detail")
    assert not view._send_btn.isEnabled()
    view.attachments.clear()
    assert view._send_btn.isEnabled()
    view.begin_edit("m", "edit")
    assert not view._can_queue()


def test_queue_panel_shows_global_order_cancel_and_resume(qtbot):
    view = ChatView(draft_only=True)
    qtbot.addWidget(view)
    queue = SendQueue()
    one = queue.enqueue(("a", "b"), "first")
    two = queue.enqueue(("c", "d"), "<b>second</b>")
    queue.pause("stopped")
    view.set_send_queue(((2, two),), 2, queue.paused_reason, can_enqueue=True)
    cancelled, resumed = [], []
    view.queue_cancel_requested.connect(cancelled.append)
    view.queue_resume_requested.connect(lambda: resumed.append(True))
    panel = view._queue_panel
    assert "共 2 条" in panel._summary.text()
    assert "已暂停" in panel._summary.text()
    panel.findChild(QPushButton, f"cancel_queue_{two.id}").click()
    panel._resume.click()
    assert cancelled == [two.id] and resumed == [True]
    assert panel.findChild(QPushButton, f"cancel_queue_{one.id}") is None
