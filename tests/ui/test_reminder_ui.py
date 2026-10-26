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
