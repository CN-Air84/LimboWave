"""Compaction separators stay outside message bubbles and survive view lifecycle."""
from datetime import UTC, datetime

import pytest

from limbowave.application.history_payload import HistoryEntry
from limbowave.domain.compaction import CompressionContext, CompressionStatus, CompressionVersion
from limbowave.ui.chat_view import HISTORY_PAGE, ChatView
from limbowave.ui.compression_widgets import CompressionDivider


def marker(vid="cmp1", active=True):
    return CompressionContext(CompressionVersion(
        id=vid, conversation_id="c1", branch_id="b1", created_at=datetime.now(UTC),
        status=CompressionStatus.ACCEPTED, input_message_ids=("m1", "m2"),
        tokens_before=1000, tokens_after=200,
    ), is_active=active)


def entries(*markers):
    return [HistoryEntry("user", "before", "", "m1"),
            HistoryEntry("assistant", "answer", "", "m2", compressions=tuple(markers)),
            HistoryEntry("user", "after", "", "m3")]


def layout_widgets(view):
    return [view._transcript.itemAt(i).widget() for i in range(view._transcript.count() - 1)]


def test_divider_sits_between_before_and_after_messages(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(entries(marker()))
    widgets = layout_widgets(view)
    assert len(view._rows) == 3
    assert isinstance(widgets[2], CompressionDivider)
    assert widgets[1]._message_id == "m2" and widgets[3]._message_id == "m3"
    assert "会话已压缩" in widgets[2]._label.text()
    assert "估算" in widgets[2].toolTip()
    assert "1,000" in widgets[2].toolTip() and "200" in widgets[2].toolTip()


def test_live_sync_preserves_bubbles_and_is_idempotent(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(entries())
    rows = list(view._rows)
    for _ in range(2):
        view.sync_compression_markers(entries(marker()))
        assert view._rows == rows
        assert len(view.findChildren(CompressionDivider)) == 1
    view.sync_compression_markers(entries(marker(active=False)))
    divider = view.findChildren(CompressionDivider)[0]
    assert "历史压缩" in divider._label.text()
    assert "非当前生效版本" in divider.toolTip()
    view.clear_transcript()
    assert not view.findChildren(CompressionDivider)


def test_multiple_markers_and_branch_reuse_do_not_leak(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(entries(marker("old", False), marker("new")))
    assert len(view.findChildren(CompressionDivider)) == 2
    assert view.apply_branch_history(entries()[:2], "m2")
    assert not view.findChildren(CompressionDivider)


def test_marker_is_restored_when_older_page_is_loaded(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    history = entries(marker()) + [HistoryEntry("user", str(i), "", f"later{i}")
                                   for i in range(HISTORY_PAGE)]
    view.load_history(history)
    assert not view.findChildren(CompressionDivider)
    view._load_earlier()
    assert len(view.findChildren(CompressionDivider)) == 1


@pytest.mark.asyncio
async def test_incremental_render_and_same_run_boundary(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    history = entries(marker())[:2]
    history[1] = history[1]._replace(run_id="run")
    history.append(HistoryEntry("assistant", "after", "", "m3", run_id="run"))
    assert await view.load_history_incrementally(history)
    widgets = layout_widgets(view)
    assert isinstance(widgets[2], CompressionDivider)
    assert widgets[3]._message_id == "m3"
