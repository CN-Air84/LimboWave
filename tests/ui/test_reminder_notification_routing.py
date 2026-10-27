from datetime import UTC, datetime, timedelta

from limbowave.application.services.reminder_service import ReminderService
from limbowave.domain.reminder import ReminderContext
from limbowave.ui.reminder_notifications import ReminderNotifications
from tests.ui.test_reminder_notifications import Balloons, Sound
from tests.ui.test_send_queue_integration import wired_queue as queue_fixture
from tests.ui.test_session_switch_during_generation import wait_for

wired_queue = queue_fixture


async def test_notification_click_opens_old_chat_without_interrupting_live_run(
    wired_queue, monkeypatch,
):
    window, session, kernel, first, _, _ = wired_queue
    notifier = window.findChild(ReminderNotifications)
    sound, balloons = Sound(), Balloons()
    notifier._sound_factory = lambda: sound
    notifier._balloon_factory = lambda: balloons
    factory = session.coordinator()._uow_factory
    now = datetime.now(UTC)
    service = ReminderService(factory, clock=lambda: now - timedelta(minutes=2))
    item = service.set_reminder(ReminderContext(*first),
                                reason="private reminder text", delay_seconds=1)
    assert item.source_message_id is not None
    from limbowave.ui.chat_view import ChatView

    focused = []
    original_focus = ChatView.focus_message
    def focus(view, message_id):
        focused.append(message_id)
        return original_focus(view, message_id)
    monkeypatch.setattr(ChatView, "focus_message", focus)
    message = service.deliver_due(now=now)[0]
    scope = session.coordinator().event_scope()
    sent = list(kernel.sent)
    window.chat._input.setPlainText("draft must survive")
    session.notify_reminder(message)
    notifier._timer.stop()
    notifier._flush()
    assert len(balloons.messages) == 1 and sound.plays == 1
    assert "private reminder text" not in balloons.messages[0][1]
    # Navigation is not permission to dismiss an outstanding confirmation.
    from PySide6.QtWidgets import QApplication, QDialog

    dialog = QDialog(window)
    dialog.setModal(True)
    dialog.open()
    try:
        await wait_for(lambda: QApplication.activeModalWidget() is dialog)
        balloons.messages[0][2]()
        assert QApplication.activeModalWidget() is dialog
        assert not focused
        assert session.coordinator().event_scope() == scope
        assert window.chat._input.toPlainText() == "draft must survive"
    finally:
        dialog.close()
        dialog.deleteLater()
    await wait_for(lambda: QApplication.activeModalWidget() is None)
    balloons.messages[0][2]()
    await wait_for(lambda: window.history_preview is not None and any(
        row._message_id == message.id for row in window.history_preview._rows
    ))
    assert window.isVisible()
    assert session.coordinator().event_scope() == scope
    assert session.busy and kernel.sent == sent
    assert window.chat._input.toPlainText() == "draft must survive"
    assert any("private reminder text" in row.content_text()
               for row in window.history_preview._rows)

    assert focused[-1] == item.source_message_id
    row = next(row for row in window.history_preview._rows if row._message_id == message.id)
    assert row._reminder_btn.isEnabled()
    count = len(focused)
    row._reminder_btn.click()
    await wait_for(lambda: len(focused) > count)
    assert focused[-1] == item.source_message_id
    assert session.coordinator().event_scope() == scope
    assert session.busy and kernel.sent == sent
    assert window.chat._input.toPlainText() == "draft must survive"
