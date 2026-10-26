"""Footprints locate source messages without requiring a plaintext search query."""

from PySide6.QtCore import Qt

from limbowave.application.history_payload import HistoryEntry
from limbowave.ui.chat_view import HISTORY_PAGE, ChatView


def test_source_focus_loads_older_page_without_search_highlighting(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 600)
    view.show()
    original = "<b>这段原文不要被当成 HTML</b>"
    entries = [HistoryEntry("user", original, "", "source")]
    entries.extend(
        HistoryEntry("user", f"filler {i}", "", f"m{i}") for i in range(HISTORY_PAGE + 4)
    )
    view.load_history(entries)
    assert view._history_offset > 0
    assert view.focus_message("source")
    qtbot.wait(100)
    assert view._history_offset == 0
    row = next(row for row in view._rows if row._message_id == "source")
    assert row.label.textFormat() == Qt.TextFormat.PlainText
    assert row.label.text() == original
    assert not view._search_bar.isVisible()
    assert view._search_ids == []
    assert view._scroll.verticalScrollBar().value() < 100


def test_source_focus_handles_merged_segments_and_missing_messages(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(
        [
            HistoryEntry("assistant", "first segment", "", "a1", run_id="run"),
            HistoryEntry("assistant", "second segment", "", "a2", run_id="run"),
        ]
    )
    assert view.focus_message("a2")
    assert not view.focus_message("missing")
    assert not view._search_ids
    view.clear_transcript()
    qtbot.wait(20)
