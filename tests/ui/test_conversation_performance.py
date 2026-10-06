"""Conversation responsiveness contracts: bounded UI work without losing state."""
from __future__ import annotations

from unittest.mock import Mock

from PySide6.QtCore import QPoint
from PySide6.QtGui import QTextCursor
from pytestqt.qtbot import QtBot

from limbowave.ui.chat_view import HISTORY_PAGE, ChatView


def _history(count: int = 240) -> list[tuple[str, str, str, str]]:
    return [("user" if i % 2 == 0 else "assistant", f"message {i}\n\nbody", "", f"m{i}")
            for i in range(count)]


def test_first_body_delta_is_immediate_and_bursts_are_batched(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.set_busy(True)
    view.append_assistant_delta("first")
    row = view._stream_row
    assert row is not None and row.label is not None
    assert row.label.toPlainText() == "first"
    changes = []
    row.label.document().contentsChanged.connect(lambda: changes.append(1))
    for _ in range(2_000):
        view.append_assistant_delta("字")
    assert len(changes) <= 1
    qtbot.waitUntil(lambda: row.label.toPlainText() == "first" + "字" * 2_000)
    assert len(changes) <= 2
    assert not row.label.document().isUndoAvailable()


def test_pending_body_preserves_selection_and_literal_text(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.append_assistant_delta("select me\n")
    body = view._stream_row.label
    cursor = body.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(6, QTextCursor.MoveMode.KeepAnchor)
    body.setTextCursor(cursor)
    for text in ("<b>literal</b>", "\n", "🙂\u2028", "last"):
        view.append_assistant_delta(text)
    expected = "select me\n<b>literal</b>\n🙂\nlast"
    qtbot.waitUntil(lambda: body.toPlainText() == expected)
    assert body.textCursor().selectedText() == "select"


def test_final_text_replaces_pending_deltas_without_late_append(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.set_busy(True)
    view.append_assistant_delta("draft")
    view.append_assistant_delta(" stale tail")
    view.end_assistant("**authoritative final**", "m1")
    view.set_busy(False)
    qtbot.wait(80)
    assert view._rows[-1].content_text() == "authoritative final"
    assert view._rows[-1]._segments[0].raw_text == "**authoritative final**"


def test_settle_without_message_end_flushes_all_partial_text(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.set_busy(True)
    view.append_assistant_delta("partial")
    view.append_assistant_delta(" tail")
    view.set_busy(False)
    assert view._rows[-1].content_text() == "partial tail"
    qtbot.wait(80)
    assert view._rows[-1].content_text() == "partial tail"


def test_segment_boundary_flushes_the_previous_segment(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.set_busy(True)
    view.append_assistant_delta("one")
    view.append_assistant_delta(" pending")
    view.begin_assistant()
    view.append_assistant_delta("two")
    view.append_assistant_delta(" pending")
    view.set_busy(False)
    row = view._rows[-1]
    assert [part.raw_text for part in row._segments] == ["one pending", "two pending"]
    assert row.content_text() == "one pending\n\ntwo pending"


def test_retry_discards_pending_text_from_the_previous_attempt(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.set_busy(True)
    view.append_assistant_delta("old")
    view.append_assistant_delta(" pending")
    row = view._stream_row
    row.reset_for_retry()
    row.begin_segment()
    view.append_assistant_delta("new")
    view.append_assistant_delta(" tail")
    qtbot.waitUntil(lambda: row.content_text() == "new tail")
    assert len(row._segments) == 1


def test_history_replacement_discards_pending_stream_work(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.set_busy(True)
    view.append_assistant_delta("old")
    view.append_assistant_delta(" pending")
    view.load_history([("assistant", "other conversation", "", "m2")])
    qtbot.wait(80)
    assert [row.content_text() for row in view._rows] == ["other conversation"]


def test_deltas_do_not_schedule_scroll_work_per_token(qtbot: QtBot, monkeypatch) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.set_busy(True)
    view.append_assistant_delta("first")
    sync = Mock(wraps=view._sync_jump_button)
    monkeypatch.setattr(view, "_sync_jump_button", sync)
    for _ in range(1_000):
        view.append_assistant_delta("x")
        view.append_thinking_delta("thinking")
    assert sync.call_count <= 2


def test_empty_deltas_do_not_create_a_reply(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.append_assistant_delta("")
    view.append_thinking_delta("")
    assert view._rows == []


def test_load_earlier_keeps_existing_rows_and_documents(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history())
    previous = list(view._rows)
    changes = []
    for row in previous:
        if row._role == "assistant":
            row.label.document().contentsChanged.connect(lambda: changes.append(1))
    view._load_earlier()
    assert len(view._rows) == 2 * HISTORY_PAGE
    assert view._rows[HISTORY_PAGE:] == previous
    assert changes == []
    view._load_earlier()
    assert view._rows[2 * HISTORY_PAGE:] == previous


def test_pagination_preserves_expansion_and_selection(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    entries = _history()
    entries[-1] = ("assistant", "selected body", "reasoning", "m239")
    view.load_history(entries)
    row = view._rows[-1]
    row._thinking._on_toggle()
    cursor = row.label.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(8, QTextCursor.MoveMode.KeepAnchor)
    row.label.setTextCursor(cursor)
    view._load_earlier()
    assert view._rows[-1] is row
    assert not row._thinking._content.isHidden()
    assert row.label.textCursor().selectedText() == "selected"


def test_pagination_does_not_reset_an_active_reply(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history())
    view.set_busy(True)
    view.append_assistant_delta("live")
    view.append_assistant_delta(" pending")
    row = view._stream_row
    view._load_earlier()
    assert view._stream_row is row
    assert view._run_tail is row
    view.append_assistant_delta(" continued")
    view.set_busy(False)
    assert view._rows[-1] is row
    assert row.content_text() == "live pending continued"


def test_loading_past_start_is_a_noop(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history(12))
    previous = list(view._rows)
    view._load_earlier()
    assert view._rows == previous


def test_pagination_keeps_the_visible_message_anchored(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(760, 650)
    view.show()
    view.load_history(_history())
    qtbot.wait(100)
    bar = view._scroll.verticalScrollBar()
    bar.setValue(0)
    anchor = view._rows[0]
    viewport = view._scroll.viewport()
    old_y = anchor.mapTo(viewport, QPoint()).y()
    view._load_earlier()
    qtbot.wait(120)
    assert view._rows[HISTORY_PAGE] is anchor
    assert abs(anchor.mapTo(viewport, QPoint()).y() - old_y) <= 2
    assert not view._stick_to_bottom


def test_hidden_reply_defers_layout_and_flushes_when_shown(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.append_assistant_delta("visible")
    row = view._stream_row
    body = row.label
    view.hide()
    for _ in range(500):
        view.append_assistant_delta(" buffered")
    qtbot.wait(80)
    assert body.toPlainText() == "visible"
    view.show()
    assert body.toPlainText() == "visible" + " buffered" * 500


def test_upward_scroll_survives_a_temporarily_empty_scroll_range(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    bar = view._scroll.verticalScrollBar()
    bar.setRange(0, 14)
    assert bar.value() == 14
    bar.setValue(0)  # even a small explicit upward scroll is user intent
    assert not view._stick_to_bottom
    bar.setRange(0, 0)  # the dock animation temporarily increases the viewport
    bar.setRange(0, 1_000)  # the buffered reply now arrives
    assert bar.value() == 0
    assert not view._stick_to_bottom
    bar.setValue(1_000)
    assert view._stick_to_bottom


def test_user_scroll_cancels_pending_history_anchor_restore(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(760, 650)
    view.show()
    view.load_history(_history())
    qtbot.wait(80)
    view._load_earlier()
    view._scroll_to_bottom(force=True)
    qtbot.wait(80)
    bar = view._scroll.verticalScrollBar()
    assert bar.value() == bar.maximum()
    assert view._history_scroll_anchor is None
    assert view._stick_to_bottom


def test_tool_boundary_flushes_text_before_showing_tool(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.set_busy(True)
    view.append_assistant_delta("before")
    view.append_assistant_delta(" tool")
    view.note_tool_step("read_file", "c1", is_error=False, phase="start")
    assert view._stream_row.content_text() == "before tool"
    qtbot.wait(80)
    assert view._stream_row.content_text() == "before tool"


def test_continuous_output_flushes_before_the_producer_stops(qtbot: QtBot) -> None:
    from PySide6.QtCore import QTimer

    view = ChatView()
    qtbot.addWidget(view)
    view.append_assistant_delta("first")
    body = view._stream_row.label
    seen = []
    count = 0
    producer = QTimer(view)
    producer.setInterval(5)

    def produce() -> None:
        nonlocal count
        count += 1
        view.append_assistant_delta("x")
        if count == 30:
            producer.stop()

    body.document().contentsChanged.connect(lambda: seen.append(count))
    producer.timeout.connect(produce)
    producer.start()
    qtbot.waitUntil(lambda: body.toPlainText() == "first" + "x" * 30)
    assert any(1 < value < 30 for value in seen), "A debounce would starve a continuous stream"
    assert len(seen) < 30
    qtbot.wait(80)
    view.append_assistant_delta(" idle")
    assert body.toPlainText().endswith(" idle")


def test_removed_history_anchor_does_not_receive_a_late_callback(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history())
    view._load_earlier()
    anchor, _y = view._history_scroll_anchor
    view._drop_row(anchor)
    qtbot.wait(80)
    assert view._history_scroll_anchor is None


def test_clear_releases_pending_stream_buffers(qtbot: QtBot) -> None:
    import gc
    import weakref

    view = ChatView()
    qtbot.addWidget(view)
    view.append_assistant_delta("first")
    view.append_assistant_delta("queued" * 1_000)
    row_ref = weakref.ref(view._stream_row)
    buffer_ref = weakref.ref(view._stream_row._stream_buffer)
    view.clear_transcript()
    qtbot.wait(80)
    gc.collect()
    assert row_ref() is None
    assert buffer_ref() is None
