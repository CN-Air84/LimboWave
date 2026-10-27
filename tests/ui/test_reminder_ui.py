from limbowave.ui.chat_view import ChatView


def test_reminder_does_not_replace_live_stream_or_duplicate(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.add_user_message("question", "u1")
    view.set_busy(True)
    view.begin_assistant()
    view.append_assistant_delta("before")
    live = view._stream_row
    tail = view._run_tail
    run_rows = list(view._run_rows)
    view.add_reminder("⏰ 系统定时提醒\n喝水", "notice_reminder_test")
    assert view._stream_row is live and view._run_tail is tail
    assert view._run_rows == run_rows
    assert view._busy
    assert view._rows[-1]._message_id == "notice_reminder_test"
    assert not view._rows[-1]._can_fork
    assert "喝水" in view._rows[-1].content_text()
    assert view._rows[-1]._final
    view.add_reminder("duplicate", "notice_reminder_test")
    assert sum(r._message_id == "notice_reminder_test" for r in view._rows) == 1
    view.append_assistant_delta(" after")
    view.end_assistant("before after", "a1")
    assert live._message_id == "a1"
    assert view._rows[-1]._message_id == "notice_reminder_test"
    view.set_busy(False)



def test_live_notice_has_keyboard_accessible_context_action(qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    opened = []
    view.reminder_context_requested.connect(opened.append)
    view.add_reminder("系统定时提醒", "notice_reminder_example")
    row = view._rows[-1]
    button = row._reminder_btn
    assert button.text() == "回到当初的约定"
    assert button.accessibleName() == "回到设定此提醒时的上下文"
    assert button.isVisible() and button.isEnabled()
    button.setFocus()
    QTest.keyClick(button, Qt.Key.Key_Space)
    assert opened == ["notice_reminder_example"]
    assert not row._fork_btn.isVisible()


def test_context_action_restores_in_history_and_read_only_preview(qtbot):
    from limbowave.application.history_payload import HistoryEntry

    view = ChatView(read_only=True)
    qtbot.addWidget(view)
    view.show()
    view.load_history([
        HistoryEntry("assistant", "普通模型回复", "", "ordinary", can_fork=False),
        HistoryEntry("assistant", "系统定时提醒", "", "notice_reminder_example__occurrence",
                     can_fork=False),
    ])
    opened = []
    view.reminder_context_requested.connect(opened.append)
    ordinary, notice = view._rows
    assert ordinary._reminder_btn is None
    assert notice._reminder_btn.isEnabled() and notice._reminder_btn.isVisible()
    notice._reminder_btn.click()
    assert opened == ["notice_reminder_example__occurrence"]



def test_context_focus_can_reveal_origin_outside_recent_history_page(qtbot):
    from limbowave.application.history_payload import HistoryEntry
    from limbowave.ui.chat_view import HISTORY_PAGE

    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.load_history([
        HistoryEntry("user", "当初的约定", "", "origin"),
        *[HistoryEntry("assistant", "稍后的消息", "", f"a{i}") for i in range(HISTORY_PAGE + 5)],
        HistoryEntry("assistant", "提醒", "", "notice_reminder_test", can_fork=False),
    ])
    assert not any(row._message_id == "origin" for row in view._rows)
    view.reminder_context_requested.connect(lambda _: view.focus_message("origin"))
    view._rows[-1]._reminder_btn.click()
    assert any(row._message_id == "origin" for row in view._rows)
    assert view._user_scrolled_up and not view._stick_to_bottom
