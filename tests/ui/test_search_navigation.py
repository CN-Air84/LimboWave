from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from limbowave.application.history_payload import HistoryEntry
from limbowave.ui.chat_view import HISTORY_PAGE, ChatView
from limbowave.ui.sidebar import Sidebar
from tests.ui.test_session_switch_during_generation import wired  # noqa: F401


def test_sidebar_preserves_search_target_even_in_active_conversation(qtbot):
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.set_active("c1", "b1")
    target = ("c1", "b2", "m1", "needle")
    sidebar.show_search_results([("c1", "needle")], targets=[target])
    with qtbot.waitSignal(sidebar.search_hit_selected) as signal:
        sidebar._list.setCurrentRow(0)
    assert tuple(signal.args[0]) == target


def test_locates_older_message_and_restores_literal_text(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 600)
    view.show()
    original = "<b>needle</b> & needle"
    entries = [HistoryEntry("user", original, "", "old")]
    entries += [HistoryEntry("user", f"filler {i}", "", f"m{i}")
                for i in range(HISTORY_PAGE + 4)]
    entries += [HistoryEntry("assistant", "**needle** reply", "", "last")]
    view.load_history(entries)
    assert view._history_offset > 0
    assert view.show_search_match("needle", "old")
    qtbot.wait(100)
    row = next(row for row in view._rows if row._message_id == "old")
    assert isinstance(row.label, QLabel)
    assert row.label.textFormat() == Qt.TextFormat.RichText
    assert "&lt;b&gt;" in row.label.text()
    assert row.content_text() == original
    assert view._history_offset == 0
    assert "1/2" in view._search_label.text()
    assert view._scroll.verticalScrollBar().value() < 100
    view._move_search_match(1)
    assert "2/2" in view._search_label.text()
    assert view._rows[-1]._segments[0].content.extraSelections()
    view._move_search_match(1)
    assert "1/2" in view._search_label.text()
    view.clear_search_matches()
    assert row.label.text() == original
    assert row.label.textFormat() == Qt.TextFormat.PlainText
    assert not view._rows[-1]._segments[0].content.extraSelections()


def test_merged_assistant_segment_and_stale_target(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history([
        HistoryEntry("assistant", "first needle", "", "a1", run_id="run"),
        HistoryEntry("assistant", "😀 second needle", "", "a2", run_id="run"),
    ])
    assert len(view._rows) == 1
    assert view.show_search_match("needle", "a1")
    assert view.show_search_match("needle", "a2")
    segment = view._rows[0]._segments[1]
    selections = segment.content.extraSelections()
    assert selections[0].cursor.selectedText() == "needle"
    assert not view.show_search_match("needle", "deleted")
    assert not view._search_ids
    assert view.show_search_match("needle", "a1")
    view.load_history([HistoryEntry("user", "another branch", "", "b1")])
    assert not view._search_ids


async def test_search_jump_uses_history_preview_without_interrupting_run(wired):  # noqa: F811
    from tests.ui.test_session_switch_during_generation import wait_for

    window, controller, kernel, history_id, history_branch, _ = wired
    live_id = controller.conversation_id
    window.sidebar.conversation_selected.emit(history_id)
    await wait_for(lambda: window.history_preview is not None and not window.chat.history_loading)
    message_id = window.history_preview._full_history[0].message_id
    window.sidebar.search_hit_selected.emit((history_id, history_branch, message_id, "history"))
    await wait_for(lambda: window.history_preview._search_ids != [])
    assert window.history_preview._search_ids[0] == message_id
    assert controller.conversation_id == live_id
    assert controller.busy
    assert not kernel.aborted
